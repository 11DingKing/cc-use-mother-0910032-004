"""演示数据：还原「学生团队活动轻微受伤」三方分别建档的场景。"""
from __future__ import annotations

from .models import User, UserRole
from .repository import Repository

DEMO_USERS = (
    User("u-venue-op", "林运营", UserRole.VENUE_OPERATOR, "文博中心"),
    User("u-volunteer", "志愿者小陈", UserRole.VOLUNTEER, "文博中心"),
    User("u-guardian", "王同学家长", UserRole.GUARDIAN, "第三中学"),
    User("u-manager", "周馆长", UserRole.VENUE_MANAGER, "文博中心"),
    User("u-officer", "赵安全员", UserRole.VENUE_OPERATOR, "安全员办公室"),
    User("u-school", "李老师(监护端)", UserRole.GUARDIAN, "第三中学"),
)


def seed_users(repo: Repository) -> None:
    for user in DEMO_USERS:
        try:
            repo.add_user(user)
        except ValueError:
            pass
