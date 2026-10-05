"""可注入时钟，便于确定性地测试逾期提醒与时间线。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """真实部署使用的 UTC 时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """测试用固定时钟，可手动推进。"""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> datetime:
        self._now = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return self._now

    def advance(self, **kwargs: float) -> datetime:
        self._now += timedelta(**kwargs)
        return self._now
