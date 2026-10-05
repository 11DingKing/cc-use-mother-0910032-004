"""内存仓储与稳定编号生成。

服务端不绑定具体数据库；仓储接口集中在此，替换为持久化实现时
领域层与 API 无需改动。编号按类别分配，便于审计追溯。
"""
from __future__ import annotations

from itertools import count

from .models import Incident, User


class Repository:
    def __init__(self) -> None:
        self._incidents: dict[str, Incident] = {}
        self._users: dict[str, User] = {}
        self._sequences: dict[str, count] = {}

    def _next_id(self, prefix: str) -> str:
        if prefix not in self._sequences:
            self._sequences[prefix] = count(1)
        return f"{prefix}-{next(self._sequences[prefix]):04d}"

    def new_incident_id(self) -> str:
        return self._next_id("INC")

    def new_source_id(self) -> str:
        return self._next_id("SRC")

    def new_person_id(self) -> str:
        return self._next_id("PSN")

    def new_evidence_id(self) -> str:
        return self._next_id("EV")

    def new_measure_id(self) -> str:
        return self._next_id("MSR")

    def new_task_id(self) -> str:
        return self._next_id("TSK")

    def new_reminder_id(self) -> str:
        return self._next_id("RMN")

    def new_entry_id(self) -> str:
        return self._next_id("TL")

    def new_verification_id(self) -> str:
        return self._next_id("VRF")

    # -- 用户 ----------------------------------------------------------------
    def add_user(self, user: User) -> None:
        if user.user_id in self._users:
            raise ValueError(f"用户已存在：{user.user_id}")
        self._users[user.user_id] = user

    def get_user(self, user_id: str) -> User:
        try:
            return self._users[user_id]
        except KeyError:
            raise KeyError(f"用户不存在：{user_id}") from None

    def list_users(self) -> list[User]:
        return list(self._users.values())

    # -- 事件 ----------------------------------------------------------------
    def save_incident(self, incident: Incident) -> None:
        self._incidents[incident.incident_id] = incident

    def get_incident(self, incident_id: str) -> Incident:
        incident = self._incidents.get(incident_id)
        if incident is None:
            raise KeyError(f"事件不存在：{incident_id}")
        return incident

    def list_incidents(self, include_merged: bool = False) -> list[Incident]:
        incidents = self._incidents.values()
        if not include_merged:
            incidents = (i for i in incidents if i.merged_into is None)
        return sorted(incidents, key=lambda i: i.created_at)
