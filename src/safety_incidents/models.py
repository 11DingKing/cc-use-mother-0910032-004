"""领域模型：事件、来源报告、人员、证据、措施、任务与核验记录。

所有时间均为带时区的 ``datetime``。模型只承载状态，业务规则位于
``service`` 模块；敏感字段（联系方式、健康信息）以显式分层存放，
由 ``permissions`` / ``serializers`` 决定可见性，而不是靠调用方自觉。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


class UserRole(str, enum.Enum):
    """与领域契约 ``actors`` 对齐的系统角色。"""

    VENUE_OPERATOR = "文博中心运营员"
    VOLUNTEER = "志愿者"
    GUARDIAN = "监护人"
    VENUE_MANAGER = "场馆负责人"


class ReporterOrg(str, enum.Enum):
    """建档来源组织：场馆、安全员、学校分别建档。"""

    VENUE = "场馆"
    SAFETY_OFFICER = "安全员"
    SCHOOL = "学校"


class IncidentStatus(str, enum.Enum):
    DRAFT = "草拟"
    PENDING_VERIFICATION = "待核验"
    CONFIRMED = "已确认"
    IN_PROGRESS = "执行中"
    ARCHIVED = "已归档"


class VerificationAction(str, enum.Enum):
    ESCALATE = "升级"
    TRANSFER = "转交"
    REOPEN = "复开"
    CLOSE = "结案"


class VerificationStatus(str, enum.Enum):
    PENDING = "待核验"
    APPROVED = "通过"
    REJECTED = "驳回"


class TaskStatus(str, enum.Enum):
    OPEN = "待执行"
    IN_PROGRESS = "执行中"
    DONE = "完成"


@dataclass(frozen=True)
class User:
    user_id: str
    name: str
    role: UserRole
    organization: str = ""


@dataclass
class ReportSource:
    """一份来源建档。事件合并后来源仍然完整保留。"""

    source_id: str
    reporter_org: ReporterOrg
    reporter: User
    created_at: datetime
    title: str
    summary: str
    external_ref: str = ""
    occurred_at: Optional[datetime] = None


@dataclass
class PersonInvolved:
    """涉事人员。

    身份字段（``name`` / ``contact``）与健康字段（``health_summary`` /
    ``medical_action``）分层存放，普通志愿者两类都不可见。
    """

    person_id: str
    label: str  # 例如：受伤学生、带队教师
    name: str
    contact: str = ""
    health_summary: str = ""  # 敏感：伤情/诊断
    medical_action: str = ""  # 敏感：已采取的医疗处置
    guardian_user_id: Optional[str] = None
    added_at: Optional[datetime] = None
    added_by: Optional[str] = None


@dataclass
class Evidence:
    evidence_id: str
    summary: str
    captured_at: datetime
    recorded_by: str
    kind: str = "其他"  # 照片/笔录/监控/其他
    source_ref: str = ""


@dataclass
class Measure:
    """临时措施，记录完成情况与依据。"""

    measure_id: str
    title: str
    description: str
    ordered_at: datetime
    ordered_by: str
    basis: str  # 依据：对应证据/伤情判断/任务要求
    completed_at: Optional[datetime] = None
    completed_by: Optional[str] = None
    completion_basis: str = ""  # 完成依据：现场复查/照片/签字单等
    related_evidence_ids: list[str] = field(default_factory=list)


@dataclass
class Task:
    """责任任务，带截止时间与逾期提醒记录。"""

    task_id: str
    title: str
    description: str
    assignee_id: str
    assignee_name: str
    due_at: datetime
    created_at: datetime
    created_by: str
    basis: str
    status: TaskStatus = TaskStatus.OPEN
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    completed_by: Optional[str] = None
    completion_basis: str = ""
    overdue: bool = False
    reminders: list["Reminder"] = field(default_factory=list)


@dataclass
class Reminder:
    reminder_id: str
    task_id: str
    created_at: datetime
    due_at: datetime
    note: str
    level: int = 1  # 第几次提醒


@dataclass
class TimelineEntry:
    entry_id: str
    at: datetime
    kind: str  # 建报/合并/状态变更/升级/转交/复开/结案/措施/任务/提醒/证据/人员
    actor: str
    summary: str
    refs: dict[str, str] = field(default_factory=dict)


@dataclass
class VerificationRequest:
    """升级、转交、复开、结案的独立核验申请。"""

    verification_id: str
    action: VerificationAction
    incident_id: str
    requested_by: str
    requested_by_name: str
    requested_at: datetime
    reason: str
    status: VerificationStatus = VerificationStatus.PENDING
    verifier_id: Optional[str] = None
    verifier_name: Optional[str] = None
    decided_at: Optional[datetime] = None
    decision_note: str = ""
    # 动作参数，例如转交目标组织、升级到的等级
    payload: dict = field(default_factory=dict)


@dataclass
class Incident:
    incident_id: str
    title: str
    description: str
    status: IncidentStatus
    severity: int  # 1-4，升级动作提升该等级
    created_at: datetime
    owner_org: str
    sources: list[ReportSource] = field(default_factory=list)
    people: list[PersonInvolved] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    measures: list[Measure] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    timeline: list[TimelineEntry] = field(default_factory=list)
    verifications: list[VerificationRequest] = field(default_factory=list)
    merged_source_incident_ids: list[str] = field(default_factory=list)
    merged_into: Optional[str] = None
    closed_at: Optional[datetime] = None

    def get_task(self, task_id: str) -> Task:
        for task in self.tasks:
            if task.task_id == task_id:
                return task
        raise KeyError(task_id)

    def get_measure(self, measure_id: str) -> Measure:
        for measure in self.measures:
            if measure.measure_id == measure_id:
                return measure
        raise KeyError(measure_id)
