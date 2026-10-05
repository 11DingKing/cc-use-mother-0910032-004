"""场馆安全事件协同的领域模型。

状态与角色沿用 domain/contract.json：
状态：草拟 / 待核验 / 已确认 / 执行中 / 已归档
角色：文博中心运营员 / 志愿者 / 监护人 / 场馆负责人，另补安全员、学校负责人。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

# ---- 角色 ----
VENUE_OPERATOR = "venue_operator"   # 文博中心运营员
VOLUNTEER = "volunteer"             # 志愿者
GUARDIAN = "guardian"               # 监护人
VENUE_MANAGER = "venue_manager"     # 场馆负责人
SAFETY_OFFICER = "safety_officer"   # 安全员
SCHOOL_ADMIN = "school_admin"       # 学校负责人

ROLES = {
    VENUE_OPERATOR: "文博中心运营员",
    VOLUNTEER: "志愿者",
    GUARDIAN: "监护人",
    VENUE_MANAGER: "场馆负责人",
    SAFETY_OFFICER: "安全员",
    SCHOOL_ADMIN: "学校负责人",
}

# 可查看完整健康信息的角色（敏感信息分层）
FULL_HEALTH_ROLES = {VENUE_MANAGER, SAFETY_OFFICER, SCHOOL_ADMIN}
# 可查看完整身份与联系方式的角色
FULL_IDENTITY_ROLES = {VENUE_OPERATOR, VENUE_MANAGER, SAFETY_OFFICER, SCHOOL_ADMIN}
# 可独立核验（四眼原则）的角色
VERIFIER_ROLES = {VENUE_MANAGER, SAFETY_OFFICER, SCHOOL_ADMIN}
# 可发起管理动作的角色
STAFF_ROLES = {VENUE_OPERATOR, VENUE_MANAGER, SAFETY_OFFICER, SCHOOL_ADMIN}

# ---- 事件状态 ----
DRAFT = "草拟"
PENDING_VERIFY = "待核验"
CONFIRMED = "已确认"
IN_PROGRESS = "执行中"
ARCHIVED = "已归档"

STATES = (DRAFT, PENDING_VERIFY, CONFIRMED, IN_PROGRESS, ARCHIVED)

SEVERITIES = ("low", "medium", "high")
SEVERITY_LABEL = {"low": "轻微", "medium": "中等", "high": "严重"}

# 需独立核验的动作
ESCALATE = "escalate"   # 升级
TRANSFER = "transfer"   # 转交
CLOSE = "close"         # 结案
REOPEN = "reopen"       # 复开
VERIFIABLE_ACTIONS = {ESCALATE, TRANSFER, CLOSE, REOPEN}


@dataclass
class User:
    user_id: str
    name: str
    role: str
    org: str


@dataclass
class Person:
    """事件涉及人员。健康字段为敏感字段，按角色裁剪。"""

    person_id: str
    name: str
    kind: str  # injured_student / student / teacher / staff / guardian / witness
    contact: str = ""
    is_injured: bool = False
    injury_description: str = ""   # 敏感：伤情描述
    medical_notes: str = ""        # 敏感：就医与健康记录
    guardian_user_id: str | None = None


@dataclass
class SourceReport:
    """建档来源（场馆 / 安全员 / 学校分别上报），合单后仍逐条保留。"""

    report_id: str
    reporter_user_id: str
    reporter_name: str
    org: str
    content: str
    created_at: datetime


@dataclass
class TimelineEvent:
    event_id: str
    at: datetime
    kind: str
    actor_name: str
    summary: str
    ref: dict = field(default_factory=dict)


@dataclass
class Evidence:
    evidence_id: str
    kind: str  # photo / witness_statement / document / other
    summary: str
    ref: str  # URI 或摘要哈希等定位信息
    created_by: str
    created_at: datetime


@dataclass
class Measure:
    """临时措施：是否完成 + 完成/判定依据。"""

    measure_id: str
    title: str
    description: str = ""
    status: str = "pending"  # pending / completed / not_applicable
    decided_by: str = ""
    decided_at: datetime | None = None
    basis_note: str = ""
    basis_evidence_ids: list[str] = field(default_factory=list)
    completed_by: str = ""
    completed_at: datetime | None = None
    completion_note: str = ""

    @property
    def is_completed(self) -> bool:
        return self.status == "completed"


@dataclass
class Task:
    """责任任务：带截止时间，由可注入时钟驱动逾期提醒。"""

    task_id: str
    incident_id: str
    title: str
    assignee: str
    description: str = ""
    due_at: datetime | None = None
    status: str = "open"  # open / completed / cancelled
    created_by: str = ""
    created_at: datetime | None = None
    completed_at: datetime | None = None
    emitted_kinds: set[str] = field(default_factory=set)


@dataclass
class Reminder:
    reminder_id: str
    task_id: str
    incident_id: str
    kind: str  # due_soon / overdue
    message: str
    emitted_at: datetime
    due_at: datetime | None


class RequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass
class VerificationRequest:
    """升级 / 转交 / 复开 / 结案 的独立核验申请（四眼原则）。"""

    request_id: str
    incident_id: str
    action: str
    params: dict
    reason: str
    requested_by: str
    requested_by_name: str
    requested_at: datetime
    status: str = RequestStatus.PENDING.value
    verifier_user_id: str = ""
    verifier_name: str = ""
    verifier_note: str = ""
    decided_at: datetime | None = None


@dataclass
class Incident:
    incident_id: str
    title: str
    description: str
    location: str
    severity: str
    occurred_at: datetime
    owner_org: str
    state: str = DRAFT
    created_by: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    submitted_by: str = ""
    submitted_at: datetime | None = None
    confirmed_by: str = ""
    confirmed_at: datetime | None = None
    closed_by: str = ""
    closed_at: datetime | None = None
    people: list[Person] = field(default_factory=list)
    sources: list[SourceReport] = field(default_factory=list)
    timeline: list[TimelineEvent] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    measures: list[Measure] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    verifications: list[VerificationRequest] = field(default_factory=list)
    merged_into: str | None = None
    merged_from: list[str] = field(default_factory=list)
