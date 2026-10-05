"""内存仓库：线程安全地保存实体。"""
from __future__ import annotations

import threading
from typing import Iterable

from .models import (
    Evidence,
    Incident,
    Person,
    Reminder,
    SourceReport,
    Task,
    User,
    VerificationRequest,
)


class Repository:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.users: dict[str, User] = {}
        self.incidents: dict[str, Incident] = {}
        self.tasks: dict[str, Task] = {}
        self.verifications: dict[str, VerificationRequest] = {}
        self.evidence: dict[str, Evidence] = {}
        self.reminders: list[Reminder] = []
        self.counters: dict[str, int] = {}

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def next_id(self, prefix: str) -> str:
        with self._lock:
            n = self.counters.get(prefix, 0) + 1
            self.counters[prefix] = n
            return f"{prefix}-{n:04d}"

    # ---- users ----
    def add_user(self, user: User) -> None:
        with self._lock:
            self.users[user.user_id] = user

    def get_user(self, user_id: str) -> User | None:
        return self.users.get(user_id)

    # ---- incidents ----
    def add_incident(self, incident: Incident) -> None:
        with self._lock:
            self.incidents[incident.incident_id] = incident

    def get_incident(self, incident_id: str) -> Incident | None:
        return self.incidents.get(incident_id)

    def active_incidents(self) -> Iterable[Incident]:
        with self._lock:
            return [i for i in self.incidents.values() if i.merged_into is None]

    # ---- tasks ----
    def add_task(self, task: Task) -> None:
        with self._lock:
            self.tasks[task.task_id] = task

    def get_task(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    # ---- verifications ----
    def add_verification(self, req: VerificationRequest) -> None:
        with self._lock:
            self.verifications[req.request_id] = req

    def get_verification(self, request_id: str) -> VerificationRequest | None:
        return self.verifications.get(request_id)

    # ---- evidence ----
    def add_evidence(self, ev: Evidence) -> None:
        with self._lock:
            self.evidence[ev.evidence_id] = ev

    # ---- reminders ----
    def add_reminder(self, reminder: Reminder) -> None:
        with self._lock:
            self.reminders.append(reminder)
