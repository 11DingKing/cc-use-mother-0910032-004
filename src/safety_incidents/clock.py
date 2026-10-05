"""时钟抽象。

逾期提醒等时间相关逻辑只依赖 :class:`Clock`，生产环境注入
:class:`SystemClock`，测试环境注入 :class:`FixedClock`，保证结果可复现。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """返回当前带时区的时间。"""


class SystemClock:
    """系统 UTC 时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """可手动拨动的固定时钟，用于测试与演练。"""

    def __init__(self, start: datetime | None = None) -> None:
        if start is None:
            start = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)
        if start.tzinfo is None:
            raise ValueError("FixedClock 初始时间必须带时区")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        self._now += delta
        return self._now

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("FixedClock 时间必须带时区")
        self._now = value
