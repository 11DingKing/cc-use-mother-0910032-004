"""逾期提醒调度器。

周期调用 SafetyService.scan_due_tasks；时钟仍走可注入 Clock，
测试中可直接调用 run_once 做确定性验证，也可以用 FixedClock + start。
"""
from __future__ import annotations

import threading
from collections.abc import Callable

from .models import User
from .service import SafetyService


class ReminderScheduler:
    def __init__(
        self,
        service: SafetyService,
        operator: User,
        interval_seconds: float = 300.0,
        sink: Callable[[list], None] | None = None,
    ) -> None:
        self.service = service
        self.operator = operator
        self.interval_seconds = interval_seconds
        self.sink = sink
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> list:
        reminders = self.service.scan_due_tasks(self.operator)
        if reminders and self.sink:
            self.sink(reminders)
        return reminders

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 — 调度循环不能因单次异常退出
                continue

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="reminder-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
