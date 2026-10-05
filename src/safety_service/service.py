"""领域服务：事件工作流、合单留源、独立核验、逾期提醒、权限裁剪。"""
from __future__ import annotations

from datetime import datetime, timedelta

from .clock import Clock
from .errors import Conflict, NotFound, PermissionDenied, ValidationError
from .models import (
    ARCHIVED,
    CONFIRMED,
    DRAFT,
    FULL_HEALTH_ROLES,
    FULL_IDENTITY_ROLES,
    IN_PROGRESS,
    PENDING_VERIFY,
    STAFF_ROLES,
    VERIFIER_ROLES,
    Evidence,
    Incident,
    Measure,
    Person,
    Reminder,
    RequestStatus,
    SourceReport,
    Task,
    TimelineEvent,
    User,
    VerificationRequest,
)
from .repository import Repository

DUPLICATE_WINDOW = timedelta(hours=24)
DUE_SOON_WINDOW = timedelta(hours=24)
SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3}


class SafetyService:
    def __init__(self, repo: Repository, clock: Clock) -> None:
        self.repo = repo
        self.clock = clock

    # ================= 事件建档 =================
    def create_incident(self, user: User, data: dict) -> Incident:
        if user.role not in STAFF_ROLES:
            raise PermissionDenied("仅工作人员（场馆/安全员/学校）可以建档")
        occurred_at = self._parse_dt(data.get("occurred_at")) or self.clock.now()
        incident = Incident(
            incident_id=self.repo.next_id("INC"),
            title=self._require_str(data, "title", max_len=120),
            description=str(data.get("description", "")),
            location=self._require_str(data, "location", max_len=120),
            severity=data.get("severity", "low") if data.get("severity") in SEVERITY_ORDER else "low",
            occurred_at=occurred_at,
            owner_org=getattr(user, "org", "") or str(data.get("owner_org", "")),
            created_by=user.user_id,
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
        )
        report_content = str(data.get("report_content", data.get("description", "")))
        incident.sources.append(self._make_source(user, report_content))
        self._timeline(incident, "created", user, f"{user.name} 在 {incident.location} 建档", {"state": incident.state})
        self.repo.add_incident(incident)
        return self._resolve(incident)

    def add_source_report(self, user: User, incident_id: str, data: dict) -> tuple[Incident, SourceReport]:
        """场馆、安全员、学校可分别对同一事件补充上报，来源逐条保留。"""
        incident = self._must_incident(incident_id)
        report = self._make_source(user, self._require_str(data, "content", max_len=2000))
        incident.sources.append(report)
        incident.updated_at = self.clock.now()
        self._timeline(incident, "source_added", user, f"收到来自 {report.org} 的报告", {"report_id": report.report_id})
        return incident, report

    def find_duplicate_candidates(self, user: User, location: str, occurred_at: datetime | None) -> list[Incident]:
        at = occurred_at or self.clock.now()
        location_key = location.strip().lower()
        out: list[Incident] = []
        for inc in self.repo.active_incidents():
            if inc.state == ARCHIVED:
                continue
            if inc.location.strip().lower() == location_key and abs(inc.occurred_at - at) <= DUPLICATE_WINDOW:
                out.append(inc)
        return out

    def merge_incidents(self, user: User, source_id: str, target_id: str, reason: str = "") -> Incident:
        """合并重复建档：来源全部保留，来源事件标记为已并入，不再单独可见。"""
        if user.role not in STAFF_ROLES:
            raise PermissionDenied("仅工作人员可以合并事件")
        if source_id == target_id:
            raise ValidationError("不能将事件合并到自身")
        source = self._must_incident(source_id)
        target = self._resolve(self._must_incident(target_id))
        if source.merged_into or target.merged_into:
            raise Conflict("已合并的事件不能再次作为合并主体")

        target.sources.extend(source.sources)
        for person in source.people:
            if not any(p.name == person.name and p.kind == person.kind for p in target.people):
                target.people.append(person)
        target.evidence.extend(source.evidence)
        target.measures.extend(source.measures)
        for task in source.tasks:
            task.incident_id = target.incident_id
            target.tasks.append(task)
        target.timeline.extend(source.timeline)
        target.merged_from.append(source.incident_id)
        source.merged_into = target.incident_id
        source.state = ARCHIVED
        source.updated_at = self.clock.now()
        target.updated_at = self.clock.now()
        self._timeline(
            target, "merged", user,
            f"合并重复建档 {source.incident_id}（{len(source.sources)} 条来源已保留）" + (f"：{reason}" if reason else ""),
            {"merged_from": source.incident_id, "source_count": len(target.sources)},
        )
        return target

    # ================= 人员 / 证据 =================
    def add_person(self, user: User, incident_id: str, data: dict) -> Person:
        if user.role not in STAFF_ROLES:
            raise PermissionDenied("仅工作人员可以登记涉及人员")
        incident = self._must_incident(incident_id)
        kind = data.get("kind", "witness")
        if kind not in {"injured_student", "student", "teacher", "staff", "guardian", "witness"}:
            raise ValidationError("未知人员类型")
        person = Person(
            person_id=self.repo.next_id("PER"),
            name=self._require_str(data, "name", max_len=60),
            kind=kind,
            contact=str(data.get("contact", "")),
            is_injured=bool(data.get("is_injured", kind == "injured_student")),
            injury_description=str(data.get("injury_description", "")),
            medical_notes=str(data.get("medical_notes", "")),
            guardian_user_id=data.get("guardian_user_id"),
        )
        incident.people.append(person)
        incident.updated_at = self.clock.now()
        self._timeline(incident, "person_added", user, f"登记涉及人员（{person.kind}）", {"person_id": person.person_id})
        return person

    def add_evidence(self, user: User, incident_id: str, data: dict) -> Evidence:
        if user.role not in STAFF_ROLES:
            raise PermissionDenied("仅工作人员可以登记证据")
        incident = self._must_incident(incident_id)
        ev = Evidence(
            evidence_id=self.repo.next_id("EV"),
            kind=data.get("kind", "other") if data.get("kind") in {"photo", "witness_statement", "document", "other"} else "other",
            summary=self._require_str(data, "summary", max_len=1000),
            ref=self._require_str(data, "ref", max_len=500),
            created_by=user.user_id,
            created_at=self.clock.now(),
        )
        incident.evidence.append(ev)
        self.repo.add_evidence(ev)
        incident.updated_at = self.clock.now()
        self._timeline(incident, "evidence_added", user, f"登记证据：{ev.kind}", {"evidence_id": ev.evidence_id})
        return ev

    # ================= 临时措施 =================
    def add_measure(self, user: User, incident_id: str, data: dict) -> Measure:
        if user.role not in STAFF_ROLES:
            raise PermissionDenied("仅工作人员可以记录临时措施")
        incident = self._must_incident(incident_id)
        measure = Measure(
            measure_id=self.repo.next_id("MEA"),
            title=self._require_str(data, "title", max_len=120),
            description=str(data.get("description", "")),
            decided_by=user.user_id,
            decided_at=self.clock.now(),
            basis_note=str(data.get("basis_note", "")),
            basis_evidence_ids=list(data.get("basis_evidence_ids", [])),
        )
        incident.measures.append(measure)
        incident.updated_at = self.clock.now()
        self._timeline(incident, "measure_added", user, f"采取临时措施：{measure.title}", {"measure_id": measure.measure_id})
        return measure

    def resolve_measure(self, user: User, incident_id: str, measure_id: str, data: dict) -> Measure:
        """标记措施完成（或不适用），必须给出结论依据，可关联证据。"""
        if user.role not in STAFF_ROLES:
            raise PermissionDenied("仅工作人员可以更新措施结论")
        incident = self._must_incident(incident_id)
        measure = self._find(incident.measures, measure_id, "措施", "measure_id")
        status = data.get("status", "completed")
        if status not in {"completed", "not_applicable"}:
            raise ValidationError("status 只能是 completed 或 not_applicable")
        note = self._require_str(data, "completion_note", max_len=1000)
        evidence_ids = list(data.get("basis_evidence_ids", []))
        for eid in evidence_ids:
            if not any(e.evidence_id == eid for e in incident.evidence):
                raise ValidationError(f"依据证据不存在：{eid}")
        measure.status = status
        measure.completed_by = user.user_id
        measure.completed_at = self.clock.now()
        measure.completion_note = note
        if evidence_ids:
            measure.basis_evidence_ids = evidence_ids
        incident.updated_at = self.clock.now()
        label = "完成" if status == "completed" else "判定不适用"
        self._timeline(
            incident, "measure_resolved", user, f"临时措施「{measure.title}」{label}，依据：{note}",
            {"measure_id": measure.measure_id, "status": status, "basis_evidence_ids": evidence_ids},
        )
        return measure

    # ================= 责任任务与逾期提醒 =================
    def add_task(self, user: User, incident_id: str, data: dict) -> Task:
        if user.role not in STAFF_ROLES:
            raise PermissionDenied("仅工作人员可以分派责任任务")
        incident = self._must_incident(incident_id)
        due_at = self._parse_dt(data.get("due_at"))
        task = Task(
            task_id=self.repo.next_id("TSK"),
            incident_id=incident.incident_id,
            title=self._require_str(data, "title", max_len=120),
            assignee=self._require_str(data, "assignee", max_len=60),
            description=str(data.get("description", "")),
            due_at=due_at,
            created_by=user.user_id,
            created_at=self.clock.now(),
        )
        incident.tasks.append(task)
        self.repo.add_task(task)
        incident.updated_at = self.clock.now()
        self._timeline(incident, "task_added", user, f"分派责任任务：{task.title}（负责人 {task.assignee}）", {"task_id": task.task_id})
        return task

    def complete_task(self, user: User, task_id: str, note: str = "") -> Task:
        task = self.repo.get_task(task_id)
        if task is None:
            raise NotFound("任务不存在")
        if user.role not in STAFF_ROLES and task.assignee != user.name and task.assignee != user.user_id:
            raise PermissionDenied("只能由负责人或工作人员完成任务")
        if task.status != "open":
            raise Conflict(f"任务已处于 {task.status} 状态")
        task.status = "completed"
        task.completed_at = self.clock.now()
        incident = self._must_incident(task.incident_id)
        incident.updated_at = self.clock.now()
        self._timeline(incident, "task_completed", user, f"责任任务完成：{task.title}" + (f"（{note}）" if note else ""), {"task_id": task.task_id})
        return task

    def scan_due_tasks(self, user: User, window: timedelta = DUE_SOON_WINDOW) -> list[Reminder]:
        """按注入时钟扫描：临期与逾期各产生一次提醒（按任务去重）。"""
        if user.role not in STAFF_ROLES:
            raise PermissionDenied("仅工作人员可以查看逾期提醒")
        now = self.clock.now()
        produced: list[Reminder] = []
        for task in self.repo.tasks.values():
            if task.status != "open" or not task.due_at:
                continue
            if now >= task.due_at and "overdue" not in task.emitted_kinds:
                rem = self._reminder(task, "overdue", f"任务「{task.title}」已逾期（截止 {task.due_at:%Y-%m-%d %H:%M}）", now)
                task.emitted_kinds.add("overdue")
                produced.append(rem)
            elif task.due_at - now <= window and "due_soon" not in task.emitted_kinds and "overdue" not in task.emitted_kinds:
                rem = self._reminder(task, "due_soon", f"任务「{task.title}」即将到期（截止 {task.due_at:%Y-%m-%d %H:%M}）", now)
                task.emitted_kinds.add("due_soon")
                produced.append(rem)
        return produced

    def _reminder(self, task: Task, kind: str, message: str, now: datetime) -> Reminder:
        rem = Reminder(
            reminder_id=self.repo.next_id("RMD"),
            task_id=task.task_id,
            incident_id=task.incident_id,
            kind=kind,
            message=message,
            emitted_at=now,
            due_at=task.due_at,
        )
        self.repo.add_reminder(rem)
        return rem

    # ================= 提报与确认 =================
    def submit_for_verification(self, user: User, incident_id: str) -> Incident:
        if user.role not in STAFF_ROLES:
            raise PermissionDenied("仅工作人员可以提交核验")
        incident = self._must_incident(incident_id)
        if incident.state != DRAFT:
            raise Conflict(f"当前状态 {incident.state} 不能提交核验")
        incident.state = PENDING_VERIFY
        incident.submitted_by = user.user_id
        incident.submitted_at = self.clock.now()
        incident.updated_at = self.clock.now()
        self._timeline(incident, "submitted", user, "提交待核验", {"state": incident.state})
        return incident

    def confirm_incident(self, user: User, incident_id: str, note: str = "") -> Incident:
        """确认事件：核验人必须具备核验角色，且不能是提报人本人（独立核验）。"""
        incident = self._must_incident(incident_id)
        self._require_verifier(user)
        if incident.state != PENDING_VERIFY:
            raise Conflict(f"当前状态 {incident.state} 不能确认")
        if incident.submitted_by and incident.submitted_by == user.user_id:
            raise PermissionDenied("独立核验要求确认人不能是提报人本人")
        incident.state = CONFIRMED
        incident.confirmed_by = user.user_id
        incident.confirmed_at = self.clock.now()
        incident.updated_at = self.clock.now()
        self._timeline(incident, "confirmed", user, "事件已确认" + (f"：{note}" if note else ""), {"state": incident.state})
        return incident

    def start_execution(self, user: User, incident_id: str) -> Incident:
        if user.role not in STAFF_ROLES:
            raise PermissionDenied("仅工作人员可以启动执行")
        incident = self._must_incident(incident_id)
        if incident.state != CONFIRMED:
            raise Conflict(f"当前状态 {incident.state} 不能启动执行")
        incident.state = IN_PROGRESS
        incident.updated_at = self.clock.now()
        self._timeline(incident, "execution_started", user, "进入执行中", {"state": incident.state})
        return incident

    # ================= 升级 / 转交 / 复开 / 结案（独立核验） =================
    def request_verification(self, user: User, incident_id: str, data: dict) -> VerificationRequest:
        if user.role not in STAFF_ROLES:
            raise PermissionDenied("仅工作人员可以发起该类申请")
        action = data.get("action")
        if action not in {"escalate", "transfer", "reopen", "close"}:
            raise ValidationError("action 必须是 escalate / transfer / reopen / close")
        incident = self._must_incident(incident_id)
        params = dict(data.get("params", {}))

        if action == "escalate":
            target = params.get("severity", "high")
            if target not in SEVERITY_ORDER or SEVERITY_ORDER[target] <= SEVERITY_ORDER[incident.severity]:
                raise ValidationError("升级后的严重程度必须高于当前级别")
        elif action == "transfer":
            if not str(params.get("target_org", "")).strip():
                raise ValidationError("转交必须提供 target_org")
        elif action == "close":
            if incident.state not in {CONFIRMED, IN_PROGRESS}:
                raise Conflict(f"当前状态 {incident.state} 不能结案")
        elif action == "reopen":
            if incident.state != ARCHIVED:
                raise Conflict(f"当前状态 {incident.state} 不能复开")

        req = VerificationRequest(
            request_id=self.repo.next_id("VRF"),
            incident_id=incident.incident_id,
            action=action,
            params=params,
            reason=self._require_str(data, "reason", max_len=500),
            requested_by=user.user_id,
            requested_by_name=user.name,
            requested_at=self.clock.now(),
        )
        incident.verifications.append(req)
        self.repo.add_verification(req)
        self._timeline(incident, "verification_requested", user, f"申请{self._action_label(action)}，等待独立核验", {"request_id": req.request_id})
        return req

    def decide_verification(self, user: User, request_id: str, approve: bool, note: str = "") -> VerificationRequest:
        req = self.repo.get_verification(request_id)
        if req is None:
            raise NotFound("核验申请不存在")
        self._require_verifier(user)
        if req.status != RequestStatus.PENDING.value:
            raise Conflict("该核验申请已处理")
        if req.requested_by == user.user_id:
            raise PermissionDenied("独立核验要求核验人不能是申请人本人")

        incident = self._must_incident(req.incident_id)

        # 前置条件在决定时复核：申请与核验之间状态可能变化
        if req.action == "close" and incident.state not in {CONFIRMED, IN_PROGRESS}:
            raise Conflict(f"当前状态 {incident.state} 不能结案")
        if req.action == "close" and any(t.status == "open" for t in incident.tasks):
            raise Conflict("仍有未完成的责任任务，不能结案")
        if req.action == "reopen" and incident.state != ARCHIVED:
            raise Conflict(f"当前状态 {incident.state} 不能复开")

        req.status = RequestStatus.APPROVED.value if approve else RequestStatus.REJECTED.value
        req.verifier_user_id = user.user_id
        req.verifier_name = user.name
        req.verifier_note = note
        req.decided_at = self.clock.now()

        if approve:
            self._apply_action(incident, req, user)
        else:
            self._timeline(incident, "verification_rejected", user, f"驳回{self._action_label(req.action)}申请" + (f"：{note}" if note else ""), {"request_id": req.request_id})
        incident.updated_at = self.clock.now()
        return req

    def _apply_action(self, incident: Incident, req: VerificationRequest, verifier: User) -> None:
        if req.action == "escalate":
            old = incident.severity
            incident.severity = req.params["severity"]
            self._timeline(incident, "escalated", verifier, f"核验通过：严重程度 {old} → {incident.severity}", {"request_id": req.request_id})
        elif req.action == "transfer":
            old = incident.owner_org
            incident.owner_org = req.params["target_org"]
            self._timeline(incident, "transferred", verifier, f"核验通过：责任方 {old or '（空）'} → {incident.owner_org}", {"request_id": req.request_id})
        elif req.action == "close":
            incident.state = ARCHIVED
            incident.closed_by = verifier.user_id
            incident.closed_at = self.clock.now()
            self._timeline(incident, "closed", verifier, "核验通过：事件结案归档", {"request_id": req.request_id, "state": ARCHIVED})
        elif req.action == "reopen":
            incident.state = IN_PROGRESS
            self._timeline(incident, "reopened", verifier, "核验通过：事件复开，重新进入执行中", {"request_id": req.request_id, "state": IN_PROGRESS})

    # ================= 查询 =================
    def list_incidents(self, user: User) -> list[Incident]:
        return [self._resolve(i) for i in self.repo.incidents.values() if i.merged_into is None]

    def get_incident(self, user: User, incident_id: str) -> Incident:
        return self._resolve(self._must_incident(incident_id))

    def _resolve(self, incident: Incident) -> Incident:
        """合并后的事件访问会被导向主事件。"""
        seen = set()
        cur = incident
        while cur.merged_into and cur.incident_id not in seen:
            seen.add(cur.incident_id)
            nxt = self.repo.get_incident(cur.merged_into)
            if nxt is None:
                break
            cur = nxt
        return cur

    # ================= 工具 =================
    def _make_source(self, user: User, content: str) -> SourceReport:
        return SourceReport(
            report_id=self.repo.next_id("RPT"),
            reporter_user_id=user.user_id,
            reporter_name=user.name,
            org=getattr(user, "org", ""),
            content=content,
            created_at=self.clock.now(),
        )

    def _timeline(self, incident: Incident, kind: str, user: User, summary: str, ref: dict | None = None) -> None:
        incident.timeline.append(TimelineEvent(
            event_id=self.repo.next_id("TL"),
            at=self.clock.now(),
            kind=kind,
            actor_name=user.name,
            summary=summary,
            ref=ref or {},
        ))

    def _must_incident(self, incident_id: str) -> Incident:
        inc = self.repo.get_incident(incident_id)
        if inc is None:
            raise NotFound("事件不存在")
        return inc

    @staticmethod
    def _find(items, item_id: str, label: str, attr: str):
        for item in items:
            if getattr(item, attr) == item_id:
                return item
        raise NotFound(f"{label}不存在")

    @staticmethod
    def _require_verifier(user: User) -> None:
        if user.role not in VERIFIER_ROLES:
            raise PermissionDenied("该操作需要场馆负责人、安全员或学校负责人独立核验")

    @staticmethod
    def _action_label(action: str) -> str:
        return {"escalate": "升级", "transfer": "转交", "reopen": "复开", "close": "结案"}[action]

    @staticmethod
    def _require_str(data: dict, key: str, max_len: int) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"缺少必填字段：{key}")
        value = value.strip()
        if len(value) > max_len:
            raise ValidationError(f"字段 {key} 长度不能超过 {max_len}")
        return value

    @staticmethod
    def _parse_dt(value) -> datetime | None:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"时间格式错误：{value}") from exc
