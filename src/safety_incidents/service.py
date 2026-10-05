"""用例编排：事件报告与合并、状态机、独立核验、措施任务、逾期提醒。

所有规则集中在本模块，模型保持贫血；时间一律来自注入的时钟。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import permissions
from .clock import Clock, SystemClock
from .models import (
    Evidence,
    Incident,
    IncidentStatus,
    Measure,
    PersonInvolved,
    Reminder,
    ReportSource,
    ReporterOrg,
    Task,
    TaskStatus,
    TimelineEntry,
    User,
    UserRole,
    VerificationAction,
    VerificationRequest,
    VerificationStatus,
)
from .repository import Repository


class DomainError(Exception):
    """业务规则冲突（权限、状态、参数等）。"""


# 逾期提醒阶梯：逾期当下、24 小时、72 小时各一次
REMINDER_LADDER = (
    timedelta(0),
    timedelta(hours=24),
    timedelta(hours=72),
)


class IncidentService:
    def __init__(self, repo: Repository, clock: Clock | None = None) -> None:
        self.repo = repo
        self.clock: Clock = clock or SystemClock()

    # -- 内部工具 ------------------------------------------------------------
    def _now(self) -> datetime:
        return self.clock.now()

    def _log(
        self,
        incident: Incident,
        kind: str,
        actor: str,
        summary: str,
        refs: dict[str, str] | None = None,
    ) -> TimelineEntry:
        entry = TimelineEntry(
            entry_id=self.repo.new_entry_id(),
            at=self._now(),
            kind=kind,
            actor=actor,
            summary=summary,
            refs=refs or {},
        )
        incident.timeline.append(entry)
        return entry

    def _load(self, incident_id: str) -> Incident:
        return self.repo.get_incident(incident_id)

    def _require_manage(self, user: User, incident: Incident) -> None:
        if not permissions.can_manage_incident(user, incident):
            raise DomainError(f"角色 {user.role.value} 无权处置该事件")

    def _require_status(self, incident: Incident, *statuses: IncidentStatus) -> None:
        if incident.status not in statuses:
            allowed = "、".join(s.value for s in statuses)
            raise DomainError(
                f"事件 {incident.incident_id} 当前状态为 {incident.status.value}，"
                f"要求状态：{allowed}"
            )

    # -- 建档 ----------------------------------------------------------------
    def report_incident(
        self,
        user: User,
        reporter_org: str,
        title: str,
        summary: str,
        *,
        occurred_at: datetime | None = None,
        external_ref: str = "",
        description: str = "",
    ) -> Incident:
        """场馆 / 安全员 / 学校分别建档，各自成为一条可追溯来源。"""
        if not permissions.can_create_report(user):
            raise DomainError(f"角色 {user.role.value} 不能提交事件报告")
        try:
            org = ReporterOrg(reporter_org)
        except ValueError:
            raise DomainError(
                f"未知建档来源：{reporter_org}（可选：场馆/安全员/学校）"
            ) from None

        staff = (UserRole.VENUE_OPERATOR, UserRole.VENUE_MANAGER)
        if org is ReporterOrg.SCHOOL and user.role is not UserRole.GUARDIAN:
            raise DomainError("学校渠道建档须由监护端账号提交")
        if org in (ReporterOrg.VENUE, ReporterOrg.SAFETY_OFFICER) and (
            user.role not in staff
        ):
            raise DomainError("场馆/安全员渠道建档须由场馆工作人员提交")

        now = self._now()
        incident_id = self.repo.new_incident_id()
        incident = Incident(
            incident_id=incident_id,
            title=title.strip() or "未命名事件",
            description=description.strip(),
            status=IncidentStatus.DRAFT,
            severity=1,
            created_at=now,
            owner_org=user.organization or org.value,
        )
        source = ReportSource(
            source_id=self.repo.new_source_id(),
            reporter_org=org,
            reporter=user,
            created_at=now,
            title=incident.title,
            summary=summary,
            external_ref=external_ref.strip(),
            occurred_at=occurred_at,
        )
        incident.sources.append(source)
        self._log(
            incident,
            "建报",
            user.name,
            f"{org.value}建档：{incident.title}",
            {"source_id": source.source_id},
        )
        self.repo.save_incident(incident)
        return incident

    # -- 状态机 --------------------------------------------------------------
    def submit_for_confirmation(self, user: User, incident_id: str) -> Incident:
        incident = self._load(incident_id)
        self._require_manage(user, incident)
        self._require_status(incident, IncidentStatus.DRAFT)
        incident.status = IncidentStatus.PENDING_VERIFICATION
        self._log(incident, "状态变更", user.name, "提交待核验")
        self.repo.save_incident(incident)
        return incident

    def confirm_incident(self, user: User, incident_id: str, note: str = "") -> Incident:
        """场馆负责人确认事件成立。确认人独立于后续四类核验流程。"""
        incident = self._load(incident_id)
        if not permissions.can_verify(user):
            raise DomainError("只有场馆负责人可以确认事件")
        self._require_status(incident, IncidentStatus.PENDING_VERIFICATION)
        incident.status = IncidentStatus.CONFIRMED
        self._log(incident, "状态变更", user.name, f"事件确认成立。{note}".rstrip("。 "))
        self.repo.save_incident(incident)
        return incident

    def start_execution(self, user: User, incident_id: str) -> Incident:
        incident = self._load(incident_id)
        self._require_manage(user, incident)
        self._require_status(incident, IncidentStatus.CONFIRMED)
        incident.status = IncidentStatus.IN_PROGRESS
        self._log(incident, "状态变更", user.name, "进入执行处置阶段")
        self.repo.save_incident(incident)
        return incident

    # -- 人员 / 证据 ---------------------------------------------------------
    def add_person(
        self,
        user: User,
        incident_id: str,
        label: str,
        name: str,
        *,
        contact: str = "",
        health_summary: str = "",
        medical_action: str = "",
        guardian_user_id: str | None = None,
    ) -> PersonInvolved:
        incident = self._load(incident_id)
        self._require_manage(user, incident)
        if guardian_user_id is not None:
            self.repo.get_user(guardian_user_id)
        person = PersonInvolved(
            person_id=self.repo.new_person_id(),
            label=label,
            name=name,
            contact=contact,
            health_summary=health_summary,
            medical_action=medical_action,
            guardian_user_id=guardian_user_id,
            added_at=self._now(),
            added_by=user.user_id,
        )
        incident.people.append(person)
        # 时间线只写标签，不把健康/身份信息写进所有人可见的摘要
        self._log(
            incident,
            "人员",
            user.name,
            f"登记涉事人员：{label}",
            {"person_id": person.person_id},
        )
        self.repo.save_incident(incident)
        return person

    def add_evidence(
        self,
        user: User,
        incident_id: str,
        summary: str,
        *,
        kind: str = "其他",
        source_ref: str = "",
        captured_at: datetime | None = None,
    ) -> Evidence:
        incident = self._load(incident_id)
        self._require_manage(user, incident)
        evidence = Evidence(
            evidence_id=self.repo.new_evidence_id(),
            summary=summary,
            captured_at=captured_at or self._now(),
            recorded_by=user.name,
            kind=kind,
            source_ref=source_ref,
        )
        incident.evidence.append(evidence)
        self._log(
            incident,
            "证据",
            user.name,
            f"登记证据（{kind}）：{summary[:40]}",
            {"evidence_id": evidence.evidence_id},
        )
        self.repo.save_incident(incident)
        return evidence

    # -- 临时措施 -------------------------------------------------------------
    def add_measure(
        self,
        user: User,
        incident_id: str,
        title: str,
        description: str,
        basis: str,
        *,
        related_evidence_ids: list[str] | None = None,
    ) -> Measure:
        """登记临时措施。措施必须注明依据（证据/伤情判断等）。"""
        incident = self._load(incident_id)
        self._require_manage(user, incident)
        self._require_status(
            incident,
            IncidentStatus.DRAFT,
            IncidentStatus.PENDING_VERIFICATION,
            IncidentStatus.CONFIRMED,
            IncidentStatus.IN_PROGRESS,
        )
        if not basis.strip():
            raise DomainError("措施必须填写依据")
        related = related_evidence_ids or []
        known = {e.evidence_id for e in incident.evidence}
        for ev_id in related:
            if ev_id not in known:
                raise DomainError(f"关联证据不存在：{ev_id}")
        measure = Measure(
            measure_id=self.repo.new_measure_id(),
            title=title,
            description=description,
            ordered_at=self._now(),
            ordered_by=user.name,
            basis=basis,
            related_evidence_ids=list(related),
        )
        incident.measures.append(measure)
        self._log(
            incident,
            "措施",
            user.name,
            f"采取临时措施：{title}",
            {"measure_id": measure.measure_id},
        )
        self.repo.save_incident(incident)
        return measure

    def complete_measure(
        self, user: User, incident_id: str, measure_id: str, completion_basis: str
    ) -> Measure:
        incident = self._load(incident_id)
        self._require_manage(user, incident)
        if not completion_basis.strip():
            raise DomainError("措施完成必须填写完成依据（复查记录/照片/签字单等）")
        measure = incident.get_measure(measure_id)
        if measure.completed_at is not None:
            raise DomainError("措施已标记完成")
        measure.completed_at = self._now()
        measure.completed_by = user.name
        measure.completion_basis = completion_basis
        self._log(
            incident,
            "措施完成",
            user.name,
            f"措施完成：{measure.title}",
            {"measure_id": measure.measure_id},
        )
        self.repo.save_incident(incident)
        return measure

    # -- 责任任务 -------------------------------------------------------------
    def add_task(
        self,
        user: User,
        incident_id: str,
        title: str,
        description: str,
        assignee_id: str,
        due_at: datetime,
        basis: str,
    ) -> Task:
        incident = self._load(incident_id)
        self._require_manage(user, incident)
        self._require_status(incident, IncidentStatus.CONFIRMED, IncidentStatus.IN_PROGRESS)
        if not basis.strip():
            raise DomainError("任务必须填写依据")
        if due_at.tzinfo is None:
            raise DomainError("截止时间必须带时区")
        assignee = self.repo.get_user(assignee_id)
        task = Task(
            task_id=self.repo.new_task_id(),
            title=title,
            description=description,
            assignee_id=assignee.user_id,
            assignee_name=assignee.name,
            due_at=due_at,
            created_at=self._now(),
            created_by=user.name,
            basis=basis,
        )
        incident.tasks.append(task)
        self._log(
            incident,
            "任务",
            user.name,
            f"指派责任任务：{title}（负责人 {assignee.name}）",
            {"task_id": task.task_id},
        )
        self.repo.save_incident(incident)
        return task

    def start_task(self, user: User, incident_id: str, task_id: str) -> Task:
        incident = self._load(incident_id)
        task = incident.get_task(task_id)
        if user.user_id != task.assignee_id and not permissions.can_manage_incident(
            user, incident
        ):
            raise DomainError("只有任务负责人或处置人员可以开始任务")
        if task.status is not TaskStatus.OPEN:
            raise DomainError("任务已开始或已完成")
        task.status = TaskStatus.IN_PROGRESS
        task.started_at = self._now()
        self._log(
            incident, "任务", user.name, f"开始执行任务：{task.title}",
            {"task_id": task.task_id},
        )
        self.repo.save_incident(incident)
        return task

    def complete_task(
        self, user: User, incident_id: str, task_id: str, completion_basis: str
    ) -> Task:
        incident = self._load(incident_id)
        task = incident.get_task(task_id)
        if user.user_id != task.assignee_id and not permissions.can_manage_incident(
            user, incident
        ):
            raise DomainError("只有任务负责人或处置人员可以完成任务")
        if task.status is TaskStatus.DONE:
            raise DomainError("任务已完成")
        if not completion_basis.strip():
            raise DomainError("任务完成必须填写完成依据")
        task.status = TaskStatus.DONE
        task.completed_at = self._now()
        task.completed_by = user.name
        task.completion_basis = completion_basis
        task.overdue = False
        self._log(
            incident,
            "任务完成",
            user.name,
            f"任务完成：{task.title}",
            {"task_id": task.task_id},
        )
        self.repo.save_incident(incident)
        return task

    # -- 合并 ----------------------------------------------------------------
    def merge_reports(
        self, user: User, primary_id: str, duplicate_id: str, reason: str = ""
    ) -> Incident:
        """把重复建档并入主事件。来源、证据、措施、任务、时间线全部保留。"""
        if not permissions.can_verify(user) and not permissions.can_manage_incident(
            user, self._load(primary_id)
        ):
            raise DomainError("无权合并事件")
        if primary_id == duplicate_id:
            raise DomainError("不能合并事件自身")
        primary = self._load(primary_id)
        duplicate = self._load(duplicate_id)
        if primary.merged_into or duplicate.merged_into:
            raise DomainError("已合并的事件不能再次参与合并")
        if IncidentStatus.ARCHIVED in (primary.status, duplicate.status):
            raise DomainError("已归档事件须先复开才能合并")
        for verification in duplicate.verifications:
            if verification.status is VerificationStatus.PENDING:
                raise DomainError("被合并事件存在待核验申请，请先处理")

        # 来源完整保留（含原建档人、原编号、外部单号）
        primary.sources.extend(duplicate.sources)
        primary.people.extend(duplicate.people)
        primary.evidence.extend(duplicate.evidence)
        primary.measures.extend(duplicate.measures)
        primary.tasks.extend(duplicate.tasks)
        primary.timeline.extend(duplicate.timeline)
        for verification in duplicate.verifications:
            verification.incident_id = primary_id
            primary.verifications.append(verification)
        primary.merged_source_incident_ids.append(duplicate_id)

        duplicate.merged_into = primary_id
        self._log(
            duplicate,
            "合并",
            user.name,
            f"重复建档并入 {primary_id}。{reason}".rstrip("。 "),
            {"merged_into": primary_id},
        )
        self._log(
            primary,
            "合并",
            user.name,
            (
                f"合并重复建档 {duplicate_id}，"
                f"保留来源 {len(duplicate.sources)} 条、证据 {len(duplicate.evidence)} 项、"
                f"措施 {len(duplicate.measures)} 项、任务 {len(duplicate.tasks)} 项。{reason}"
            ).rstrip("。 "),
            {"merged_from": duplicate_id},
        )
        self.repo.save_incident(primary)
        self.repo.save_incident(duplicate)
        return primary

    # -- 独立核验 -------------------------------------------------------------
    def request_verification(
        self,
        user: User,
        incident_id: str,
        action: str,
        reason: str,
        payload: dict | None = None,
    ) -> VerificationRequest:
        """升级 / 转交 / 复开 / 结案 必须发起独立核验。"""
        incident = self._load(incident_id)
        if not permissions.can_request_verification(user):
            raise DomainError(f"角色 {user.role.value} 不能发起核验申请")
        if not reason.strip():
            raise DomainError("核验申请必须说明理由")
        try:
            act = VerificationAction(action)
        except ValueError:
            raise DomainError(
                f"未知核验动作：{action}（升级/转交/复开/结案）"
            ) from None
        self._validate_request(incident, act, payload or {})

        request = VerificationRequest(
            verification_id=self.repo.new_verification_id(),
            action=act,
            incident_id=incident_id,
            requested_by=user.user_id,
            requested_by_name=user.name,
            requested_at=self._now(),
            reason=reason,
            payload=dict(payload or {}),
        )
        incident.verifications.append(request)
        self._log(
            incident,
            "核验申请",
            user.name,
            f"申请{act.value}，等待独立核验：{reason[:50]}",
            {"verification_id": request.verification_id},
        )
        self.repo.save_incident(incident)
        return request

    def _validate_request(
        self, incident: Incident, action: VerificationAction, payload: dict
    ) -> None:
        if action is VerificationAction.ESCALATE:
            target = payload.get("target_severity")
            if not isinstance(target, int) or not 1 <= target <= 4:
                raise DomainError("升级须提供 1-4 的目标等级 target_severity")
            if target <= incident.severity:
                raise DomainError(
                    f"升级目标等级须高于当前等级 {incident.severity}"
                )
        elif action is VerificationAction.TRANSFER:
            target_org = str(payload.get("target_org", "")).strip()
            if not target_org:
                raise DomainError("转交须提供目标组织 target_org")
            if target_org == incident.owner_org:
                raise DomainError("转交目标组织与当前负责组织相同")
        elif action is VerificationAction.REOPEN:
            if incident.status is not IncidentStatus.ARCHIVED:
                raise DomainError("只有已归档事件可以申请复开")
        elif action is VerificationAction.CLOSE:
            if incident.status not in (
                IncidentStatus.CONFIRMED,
                IncidentStatus.IN_PROGRESS,
            ):
                raise DomainError("已确认或执行中的事件才能申请结案")

    def decide_verification(
        self,
        verifier: User,
        verification_id: str,
        approve: bool,
        decision_note: str = "",
    ) -> VerificationRequest:
        """场馆负责人独立核验；核验人不能是申请人本人。"""
        if not permissions.can_verify(verifier):
            raise DomainError("只有场馆负责人可以作出核验决定")
        incident = self._find_incident_of_verification(verification_id)
        request = next(
            v for v in incident.verifications if v.verification_id == verification_id
        )
        if request.status is not VerificationStatus.PENDING:
            raise DomainError("该核验申请已作出决定")
        if verifier.user_id == request.requested_by:
            raise DomainError("核验人不能是申请人本人，必须独立核验")

        if not approve:
            request.status = VerificationStatus.REJECTED
            request.verifier_id = verifier.user_id
            request.verifier_name = verifier.name
            request.decided_at = self._now()
            request.decision_note = decision_note
            self._log(
                incident,
                "核验驳回",
                verifier.name,
                f"驳回{request.action.value}申请：{decision_note or '未说明'}",
                {"verification_id": request.verification_id},
            )
            self.repo.save_incident(incident)
            return request

        # 通过后按动作生效；规则不满足时抛出异常，申请保持待决且不写核验人
        self._apply_verified_action(incident, request)
        request.status = VerificationStatus.APPROVED
        request.verifier_id = verifier.user_id
        request.verifier_name = verifier.name
        request.decided_at = self._now()
        request.decision_note = decision_note
        self._log(
            incident,
            "核验通过",
            verifier.name,
            f"核准{request.action.value}：{decision_note or '同意'}",
            {"verification_id": request.verification_id},
        )
        self.repo.save_incident(incident)
        return request

    def _apply_verified_action(
        self, incident: Incident, request: VerificationRequest
    ) -> None:
        if request.action is VerificationAction.ESCALATE:
            target = request.payload["target_severity"]
            if target <= incident.severity:
                raise DomainError("事件等级已变化，升级申请不再适用")
            incident.severity = target
        elif request.action is VerificationAction.TRANSFER:
            target_org = str(request.payload["target_org"]).strip()
            incident.owner_org = target_org
        elif request.action is VerificationAction.REOPEN:
            if incident.status is not IncidentStatus.ARCHIVED:
                raise DomainError("事件已不在归档状态")
            incident.status = IncidentStatus.IN_PROGRESS
            incident.closed_at = None
        elif request.action is VerificationAction.CLOSE:
            if incident.status not in (
                IncidentStatus.CONFIRMED,
                IncidentStatus.IN_PROGRESS,
            ):
                raise DomainError("事件状态已变化，不能结案")
            pending = [
                t for t in incident.tasks if t.status is not TaskStatus.DONE
            ]
            if pending:
                raise DomainError(
                    f"尚有 {len(pending)} 项责任任务未完成，不能结案："
                    + "、".join(t.title for t in pending)
                )
            incident.status = IncidentStatus.ARCHIVED
            incident.closed_at = self._now()

    def _find_incident_of_verification(self, verification_id: str) -> Incident:
        for incident in self.repo.list_incidents(include_merged=True):
            if any(
                v.verification_id == verification_id
                for v in incident.verifications
            ):
                return incident
        raise KeyError(f"核验申请不存在：{verification_id}")

    # -- 逾期提醒 -------------------------------------------------------------
    def sweep_reminders(self) -> list[Reminder]:
        """按注入时钟扫描全部未完成任务，到达提醒阶梯时产生提醒。

        重复调用是幂等的：每个阶梯等级对同一任务只产生一次提醒。
        """
        now = self._now()
        created: list[Reminder] = []
        for incident in self.repo.list_incidents(include_merged=False):
            if incident.status is IncidentStatus.ARCHIVED:
                continue
            for task in incident.tasks:
                if task.status is TaskStatus.DONE:
                    continue
                if now <= task.due_at:
                    task.overdue = False
                    continue
                task.overdue = True
                overdue_for = now - task.due_at
                for level, threshold in enumerate(REMINDER_LADDER, start=1):
                    if len(task.reminders) >= level:
                        continue
                    if overdue_for >= threshold:
                        reminder = Reminder(
                            reminder_id=self.repo.new_reminder_id(),
                            task_id=task.task_id,
                            created_at=now,
                            due_at=task.due_at,
                            level=level,
                            note=(
                                f"任务「{task.title}」已逾期 "
                                f"{overdue_for.total_seconds() / 3600:.1f} 小时"
                                f"（第 {level} 次提醒）"
                            ),
                        )
                        task.reminders.append(reminder)
                        created.append(reminder)
                        self._log(
                            incident,
                            "逾期提醒",
                            "系统时钟",
                            reminder.note,
                            {"task_id": task.task_id, "reminder_id": reminder.reminder_id},
                        )
                self.repo.save_incident(incident)
        return created

    def list_overdue_tasks(self) -> list[tuple[Incident, Task]]:
        result: list[tuple[Incident, Task]] = []
        for incident in self.repo.list_incidents():
            for task in incident.tasks:
                if task.status is not TaskStatus.DONE and task.overdue:
                    result.append((incident, task))
        return result
