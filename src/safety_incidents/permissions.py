"""角色权限与敏感信息分层。

对应契约不变量「敏感信息分层」：

- 健康字段（伤情、医疗处置）仅场馆负责人、事件涉及学生的监护人可见，
  文博中心运营员在处置过程中可见但志愿者不可见；
- 身份字段（姓名、联系方式）对志愿者裁剪：志愿者只能看到角色标签
  （如“受伤学生”），看不到真实姓名与联系方式；
- 写操作按角色最小授权；升级/转交/复开/结案一律走独立核验，
  且核验人不能是申请人本人（见 ``service``）。
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Incident, PersonInvolved, User, UserRole

# 健康字段
HEALTH_FIELDS = ("health_summary", "medical_action")
# 身份敏感字段
IDENTITY_FIELDS = ("name", "contact")


@dataclass(frozen=True)
class FieldVisibility:
    show_identity: bool
    show_health: bool


def person_visibility(
    viewer: User, person: PersonInvolved, incident: Incident
) -> FieldVisibility:
    """计算观看者对某位涉事人员的字段可见性。"""
    role = viewer.role

    if role is UserRole.VENUE_MANAGER:
        return FieldVisibility(True, True)

    if role is UserRole.GUARDIAN:
        # 监护人只能看到自己被监护孩子的身份与健康信息
        if person.guardian_user_id == viewer.user_id:
            return FieldVisibility(True, True)
        return FieldVisibility(False, False)

    if role is UserRole.VENUE_OPERATOR:
        # 运营处置需要身份信息与健康信息以安排措施
        return FieldVisibility(True, True)

    # 普通志愿者：既看不到身份，也看不到健康信息
    return FieldVisibility(False, False)


def can_create_report(viewer: User) -> bool:
    return viewer.role in (
        UserRole.VENUE_OPERATOR,
        UserRole.VENUE_MANAGER,
        UserRole.GUARDIAN,
    )


def can_manage_incident(viewer: User, incident: Incident) -> bool:
    """是否可添加证据/措施/人员/任务等处置内容。"""
    if viewer.role is UserRole.VENUE_MANAGER:
        return True
    if viewer.role is UserRole.VENUE_OPERATOR:
        return viewer.organization == incident.owner_org or not incident.owner_org
    return False


def can_request_verification(viewer: User) -> bool:
    return viewer.role in (UserRole.VENUE_OPERATOR, UserRole.VENUE_MANAGER)


def can_verify(viewer: User) -> bool:
    """只有场馆负责人可作为独立核验人。"""
    return viewer.role is UserRole.VENUE_MANAGER


def can_view_full_timeline(viewer: User) -> bool:
    """时间线中的健康细节随观看者裁剪；志愿者看到的是脱敏版本。"""
    return viewer.role in (
        UserRole.VENUE_MANAGER,
        UserRole.VENUE_OPERATOR,
        UserRole.GUARDIAN,
    )
