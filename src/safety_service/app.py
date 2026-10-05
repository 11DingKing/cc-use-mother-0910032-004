"""HTTP API：基于标准库 http.server，零第三方依赖。

鉴权：请求头 X-User-Id 对应用户令牌（见 seed_users）。
"""
from __future__ import annotations

import json
import re
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .clock import Clock, SystemClock
from .errors import DomainError, PermissionDenied, Unauthorized
from .repository import Repository
from .serializers import incident_to_dict
from .service import DUE_SOON_WINDOW, SafetyService


def build_service(clock: Clock | None = None) -> SafetyService:
    repo = Repository()
    svc = SafetyService(repo, clock or SystemClock())
    seed_users(repo)
    return svc


def seed_users(repo: Repository) -> None:
    """预置四类契约角色 + 安全员 / 学校负责人。"""
    from .models import User

    seeds = [
        ("u-op-1", "王敏", "venue_operator", "文博中心"),
        ("u-vol-1", "李小爱", "volunteer", "文博志愿者团队"),
        ("u-guard-1", "张建国", "guardian", ""),
        ("u-mgr-1", "赵馆长", "venue_manager", "文博中心"),
        ("u-safe-1", "陈安全", "safety_officer", "场馆安全办"),
        ("u-school-1", "孙教务", "school_admin", "第三中学"),
    ]
    for uid, name, role, org in seeds:
        repo.add_user(User(uid, name, role, org))


# (method, compiled_regex) -> handler 名称
def _route(method: str, pattern: str):
    rx = re.compile("^" + pattern + "$")

    def deco(fn):
        fn._route = (method, rx)
        return fn

    return deco


class ApiHandler(BaseHTTPRequestHandler):
    service: SafetyService = None  # 由 server 实例注入

    # ---- 路由表 ----
    @_route("GET", r"/api/health")
    def _health(self, _m):
        return 200, {"status": "ok"}

    @_route("GET", r"/api/incidents")
    def _list_incidents(self, _m):
        user = self._user()
        items = [incident_to_dict(i, user) for i in self.service.list_incidents(user)]
        return 200, {"incidents": items}

    @_route("POST", r"/api/incidents")
    def _create_incident(self, _m):
        user = self._user()
        data = self._body()
        incident = self.service.create_incident(user, data)
        at = self.service._parse_dt(data.get("occurred_at"))
        dupes = self.service.find_duplicate_candidates(user, incident.location, at)
        payload = incident_to_dict(incident, user)
        payload["duplicate_candidates"] = [d.incident_id for d in dupes if d.incident_id != incident.incident_id]
        return 201, payload

    @_route("POST", r"/api/incidents/merge")
    def _merge(self, _m):
        user = self._user()
        data = self._body()
        target = self.service.merge_incidents(
            user, data.get("source_id", ""), data.get("target_id", ""), str(data.get("reason", ""))
        )
        return 200, incident_to_dict(target, user)

    @_route("GET", r"/api/incidents/(?P<id>[^/]+)")
    def _get_incident(self, m):
        user = self._user()
        incident = self.service.get_incident(user, m.group("id"))
        return 200, incident_to_dict(incident, user)

    @_route("POST", r"/api/incidents/(?P<id>[^/]+)/reports")
    def _add_report(self, m):
        user = self._user()
        incident, report = self.service.add_source_report(user, m.group("id"), self._body())
        return 201, {"report_id": report.report_id, "incident": incident_to_dict(incident, user)}

    @_route("POST", r"/api/incidents/(?P<id>[^/]+)/people")
    def _add_person(self, m):
        user = self._user()
        person = self.service.add_person(user, m.group("id"), self._body())
        from .serializers import person_to_dict

        return 201, person_to_dict(person, user)

    @_route("POST", r"/api/incidents/(?P<id>[^/]+)/evidence")
    def _add_evidence(self, m):
        user = self._user()
        ev = self.service.add_evidence(user, m.group("id"), self._body())
        return 201, {"evidence_id": ev.evidence_id, "kind": ev.kind, "summary": ev.summary, "ref": ev.ref}

    @_route("POST", r"/api/incidents/(?P<id>[^/]+)/measures")
    def _add_measure(self, m):
        user = self._user()
        measure = self.service.add_measure(user, m.group("id"), self._body())
        from .serializers import measure_to_dict

        return 201, measure_to_dict(measure)

    @_route("POST", r"/api/incidents/(?P<id>[^/]+)/measures/(?P<mid>[^/]+)/resolve")
    def _resolve_measure(self, m):
        user = self._user()
        measure = self.service.resolve_measure(user, m.group("id"), m.group("mid"), self._body())
        from .serializers import measure_to_dict

        return 200, measure_to_dict(measure)

    @_route("POST", r"/api/incidents/(?P<id>[^/]+)/tasks")
    def _add_task(self, m):
        user = self._user()
        task = self.service.add_task(user, m.group("id"), self._body())
        from .serializers import task_to_dict

        return 201, task_to_dict(task)

    @_route("POST", r"/api/incidents/(?P<id>[^/]+)/submit")
    def _submit(self, m):
        user = self._user()
        return 200, incident_to_dict(self.service.submit_for_verification(user, m.group("id")), user)

    @_route("POST", r"/api/incidents/(?P<id>[^/]+)/confirm")
    def _confirm(self, m):
        user = self._user()
        note = str(self._body().get("note", ""))
        return 200, incident_to_dict(self.service.confirm_incident(user, m.group("id"), note), user)

    @_route("POST", r"/api/incidents/(?P<id>[^/]+)/start")
    def _start(self, m):
        user = self._user()
        return 200, incident_to_dict(self.service.start_execution(user, m.group("id")), user)

    @_route("POST", r"/api/incidents/(?P<id>[^/]+)/verifications")
    def _request_verification(self, m):
        user = self._user()
        req = self.service.request_verification(user, m.group("id"), self._body())
        return 201, {"request_id": req.request_id, "action": req.action, "status": req.status}

    @_route("POST", r"/api/verifications/(?P<rid>[^/]+)/decide")
    def _decide_verification(self, m):
        user = self._user()
        data = self._body()
        req = self.service.decide_verification(
            user, m.group("rid"), bool(data.get("approve", False)), str(data.get("note", ""))
        )
        from .serializers import verification_to_dict

        return 200, verification_to_dict(req, user)

    @_route("POST", r"/api/tasks/(?P<tid>[^/]+)/complete")
    def _complete_task(self, m):
        user = self._user()
        note = str(self._body().get("note", ""))
        task = self.service.complete_task(user, m.group("tid"), note)
        from .serializers import task_to_dict

        return 200, task_to_dict(task)

    @_route("GET", r"/api/reminders/due")
    def _scan_reminders(self, _m):
        user = self._user()
        qs = parse_qs(urlparse(self.path).query)
        window_hours = float(qs.get("window_hours", ["24"])[0])
        reminders = self.service.scan_due_tasks(user, timedelta(hours=window_hours))
        return 200, {
            "checked_at": self.service.clock.now().isoformat(),
            "reminders": [self._reminder_dict(r) for r in reminders],
        }

    @_route("GET", r"/api/reminders")
    def _list_reminders(self, _m):
        user = self._user()
        if user.role not in {"venue_operator", "venue_manager", "safety_officer", "school_admin"}:
            raise PermissionDenied("仅工作人员可以查看提醒记录")
        return 200, {"reminders": [self._reminder_dict(r) for r in self.service.repo.reminders]}

    @staticmethod
    def _reminder_dict(r) -> dict:
        return {
            "reminder_id": r.reminder_id,
            "task_id": r.task_id,
            "incident_id": r.incident_id,
            "kind": r.kind,
            "message": r.message,
            "emitted_at": r.emitted_at.isoformat(),
            "due_at": r.due_at.isoformat() if r.due_at else None,
        }

    # ---- 框架胶水 ----
    def _user(self):
        token = self.headers.get("X-User-Id", "")
        user = self.service.repo.get_user(token) if token else None
        if user is None:
            raise Unauthorized("缺少或无效的 X-User-Id 请求头")
        return user

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError("请求体必须是合法 JSON") from exc
        if not isinstance(value, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return value

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            for name in dir(self):
                fn = getattr(self, name)
                route = getattr(fn, "_route", None)
                if route and route[0] == method:
                    match = route[1].match(path)
                    if match:
                        status, payload = fn(match)
                        self._write(status, payload)
                        return
            self._write(404, {"error": {"type": "not_found", "message": f"无此路由：{method} {path}"}})
        except DomainError as exc:
            self._write(exc.http_status, {"error": {"type": exc.error_type, "message": str(exc)}})
        except Exception as exc:  # noqa: BLE001 — 兜底，避免连接挂起
            self._write(500, {"error": {"type": "internal_error", "message": str(exc)}})

    def _write(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # 安静日志
        return


def create_server(host: str = "127.0.0.1", port: int = 8080, clock: Clock | None = None) -> ThreadingHTTPServer:
    service = build_service(clock)
    server = ThreadingHTTPServer((host, port), ApiHandler)
    server.service = service  # type: ignore[attr-defined]
    ApiHandler.service = service
    return server


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="场馆安全事件协同服务端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    server = create_server(args.host, args.port)
    print(f"场馆安全事件协同服务已启动：http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
