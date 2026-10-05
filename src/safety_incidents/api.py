"""基于标准库 ``http.server`` 的 JSON API。

认证采用演示用请求头 ``X-User-Id``；所有响应经 ``serializers`` 按角色裁剪。
写操作在进程内串行化（一把锁），便于演示与集成测试。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from .clock import Clock, SystemClock
from .models import User, UserRole
from .repository import Repository
from .serializers import (
    serialize_incident,
    serialize_user,
)
from .service import DomainError, IncidentService


def parse_datetime(value: str | None, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise DomainError(f"{field} 必须是 ISO 8601 时间") from None
    if parsed.tzinfo is None:
        raise DomainError(f"{field} 必须带时区")
    return parsed


class ApiState:
    def __init__(self, clock: Clock | None = None) -> None:
        self.repo = Repository()
        self.service = IncidentService(self.repo, clock or SystemClock())
        self.lock = threading.RLock()


def create_handler(state: ApiState) -> type[BaseHTTPRequestHandler]:
    service = state.service

    class Handler(BaseHTTPRequestHandler):
        server_version = "SafetyIncidentAPI/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
            return

        # -- 基础工具 --------------------------------------------------------
        def _send_json(self, obj: Any, status: int = HTTPStatus.OK) -> None:
            body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                raise DomainError("请求体不是合法 JSON")
            if not isinstance(data, dict):
                raise DomainError("请求体必须是 JSON 对象")
            return data

        def _viewer(self):
            user_id = self.headers.get("X-User-Id", "").strip()
            if not user_id:
                raise DomainError("缺少请求头 X-User-Id")
            return state.repo.get_user(user_id)

        def _handle(self, fn: Callable[[], Any]) -> None:
            try:
                with state.lock:
                    result, status = fn()
                self._send_json(result, status)
            except DomainError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except KeyError as exc:
                self._send_json({"error": str(exc).strip("'")}, HTTPStatus.NOT_FOUND)
            except PermissionError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.FORBIDDEN)

        # -- 路由 ------------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"

            def route():
                if path == "/health":
                    return {"status": "ok"}, HTTPStatus.OK
                if path == "/users":
                    return {
                        "users": [serialize_user(u) for u in state.repo.list_users()]
                    }, HTTPStatus.OK
                if path == "/incidents":
                    viewer = self._viewer()
                    items = [
                        serialize_incident(i, viewer)
                        for i in state.repo.list_incidents()
                    ]
                    return {"incidents": items, "count": len(items)}, HTTPStatus.OK
                if path.startswith("/incidents/"):
                    incident_id = path.split("/")[2]
                    viewer = self._viewer()
                    incident = state.repo.get_incident(incident_id)
                    return serialize_incident(incident, viewer), HTTPStatus.OK
                raise KeyError(f"未知路径：{path}")

            self._handle(route)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            data = self._read_json_safe()
            if data is None:
                return

            def route():
                return self._route_post(path, data)

            self._handle(route)

        def _read_json_safe(self):
            try:
                return self._read_json()
            except DomainError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return None

        def _route_post(self, path: str, data: dict[str, Any]):
            s = service
            status = HTTPStatus.CREATED

            if path == "/users":
                user = self._create_user(data)
                return serialize_user(user), HTTPStatus.OK

            if path == "/incidents/report":
                viewer = self._viewer()
                incident = s.report_incident(
                    viewer,
                    data["reporter_org"],
                    data.get("title", ""),
                    data.get("summary", ""),
                    occurred_at=parse_datetime(data.get("occurred_at"), "occurred_at"),
                    external_ref=data.get("external_ref", ""),
                    description=data.get("description", ""),
                )
                return serialize_incident(incident, viewer), status

            if path == "/incidents/merge":
                viewer = self._viewer()
                incident = s.merge_reports(
                    viewer,
                    data["primary_id"],
                    data["duplicate_id"],
                    data.get("reason", ""),
                )
                return serialize_incident(incident, viewer), HTTPStatus.OK

            if path == "/system/sweep-reminders":
                viewer = self._viewer()
                if viewer.role not in (UserRole.VENUE_MANAGER, UserRole.VENUE_OPERATOR):
                    raise PermissionError("只有场馆工作人员可以触发提醒扫描")
                reminders = s.sweep_reminders()
                return {
                    "created_count": len(reminders),
                    "reminders": [
                        {
                            "reminder_id": r.reminder_id,
                            "task_id": r.task_id,
                            "created_at": r.created_at.isoformat(),
                            "due_at": r.due_at.isoformat(),
                            "level": r.level,
                            "note": r.note,
                        }
                        for r in reminders
                    ],
                }, HTTPStatus.OK

            parts = [p for p in path.split("/") if p]
            # /incidents/{id}/...
            if len(parts) >= 2 and parts[0] == "incidents":
                return self._route_incident_action(parts, data)

            # /verifications/{vid}/decision
            if len(parts) == 3 and parts[0] == "verifications" and parts[2] == "decision":
                viewer = self._viewer()
                request = s.decide_verification(
                    viewer,
                    parts[1],
                    bool(data["approve"]),
                    data.get("decision_note", ""),
                )
                return {
                    "verification_id": request.verification_id,
                    "action": request.action.value,
                    "status": request.status.value,
                    "verifier_name": request.verifier_name,
                    "decided_at": request.decided_at.isoformat(),
                }, HTTPStatus.OK

            raise KeyError(f"未知路径：{path}")

        def _create_user(self, data: dict[str, Any]):
            try:
                role = UserRole(data["role"])
            except KeyError:
                raise DomainError("缺少 role") from None
            except ValueError:
                raise DomainError(
                    f"未知角色：{data['role']}（文博中心运营员/志愿者/监护人/场馆负责人）"
                ) from None
            created = User(
                user_id=data["user_id"],
                name=data.get("name", data["user_id"]),
                role=role,
                organization=data.get("organization", ""),
            )
            try:
                state.repo.add_user(created)
            except ValueError as exc:
                raise DomainError(str(exc)) from None
            return created

        def _route_incident_action(self, parts: list[str], data: dict[str, Any]):
            s = service
            incident_id = parts[1]
            viewer = self._viewer()
            incident = state.repo.get_incident(incident_id)
            http_status = HTTPStatus.CREATED

            if len(parts) == 3 and parts[2] == "submit":
                return serialize_incident(
                    s.submit_for_confirmation(viewer, incident_id), viewer
                ), HTTPStatus.OK
            if len(parts) == 3 and parts[2] == "confirm":
                return serialize_incident(
                    s.confirm_incident(viewer, incident_id, data.get("note", "")),
                    viewer,
                ), HTTPStatus.OK
            if len(parts) == 3 and parts[2] == "start":
                return serialize_incident(
                    s.start_execution(viewer, incident_id), viewer
                ), HTTPStatus.OK

            if len(parts) == 3 and parts[2] == "people":
                person = s.add_person(
                    viewer,
                    incident_id,
                    data["label"],
                    data["name"],
                    contact=data.get("contact", ""),
                    health_summary=data.get("health_summary", ""),
                    medical_action=data.get("medical_action", ""),
                    guardian_user_id=data.get("guardian_user_id"),
                )
                return {"person_id": person.person_id, "label": person.label}, http_status

            if len(parts) == 3 and parts[2] == "evidence":
                ev = s.add_evidence(
                    viewer,
                    incident_id,
                    data["summary"],
                    kind=data.get("kind", "其他"),
                    source_ref=data.get("source_ref", ""),
                    captured_at=parse_datetime(data.get("captured_at"), "captured_at"),
                )
                return {"evidence_id": ev.evidence_id}, http_status

            if len(parts) == 3 and parts[2] == "measures":
                measure = s.add_measure(
                    viewer,
                    incident_id,
                    data["title"],
                    data.get("description", ""),
                    data["basis"],
                    related_evidence_ids=data.get("related_evidence_ids"),
                )
                return {
                    "measure_id": measure.measure_id,
                    "completed": False,
                }, http_status

            if len(parts) == 5 and parts[2] == "measures" and parts[4] == "complete":
                measure = s.complete_measure(
                    viewer, incident_id, parts[3], data["completion_basis"]
                )
                return {
                    "measure_id": measure.measure_id,
                    "completed": True,
                    "completed_at": measure.completed_at.isoformat(),
                    "completion_basis": measure.completion_basis,
                }, HTTPStatus.OK

            if len(parts) == 3 and parts[2] == "tasks":
                due_at = parse_datetime(data.get("due_at"), "due_at")
                if due_at is None:
                    raise DomainError("缺少 due_at")
                task = s.add_task(
                    viewer,
                    incident_id,
                    data["title"],
                    data.get("description", ""),
                    data["assignee_id"],
                    due_at,
                    data.get("basis", ""),
                )
                return {"task_id": task.task_id, "due_at": task.due_at.isoformat()}, http_status

            if len(parts) == 5 and parts[2] == "tasks" and parts[4] == "start":
                task = s.start_task(viewer, incident_id, parts[3])
                return {"task_id": task.task_id, "status": task.status.value}, HTTPStatus.OK

            if len(parts) == 5 and parts[2] == "tasks" and parts[4] == "complete":
                task = s.complete_task(
                    viewer, incident_id, parts[3], data["completion_basis"]
                )
                return {
                    "task_id": task.task_id,
                    "status": task.status.value,
                    "completion_basis": task.completion_basis,
                }, HTTPStatus.OK

            if len(parts) == 3 and parts[2] == "verifications":
                request = s.request_verification(
                    viewer,
                    incident_id,
                    data["action"],
                    data["reason"],
                    data.get("payload"),
                )
                return {
                    "verification_id": request.verification_id,
                    "action": request.action.value,
                    "status": request.status.value,
                }, http_status

            raise KeyError(f"未知路径：/{'/'.join(parts)}")

    return Handler


def build_server(host: str = "127.0.0.1", port: int = 8080, clock: Clock | None = None):
    state = ApiState(clock=clock)
    handler = create_handler(state)
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd, state
