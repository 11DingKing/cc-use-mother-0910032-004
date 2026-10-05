"""场馆安全事件协同服务端。

模块划分：

- ``clock``：可注入时钟（逾期提醒按该时钟计算）。
- ``models``：领域枚举与实体。
- ``repository``：内存仓储（带稳定编号）。
- ``permissions``：角色边界与身份/健康字段分层。
- ``service``：用例编排、状态机、报告合并、独立核验、逾期提醒。
- ``serializers``：按观看者裁剪后的只读视图。
- ``api``：基于标准库 ``http.server`` 的 JSON API。
"""
from __future__ import annotations

from .clock import Clock, FixedClock, SystemClock
from .models import User, UserRole
from .repository import Repository
from .service import DomainError, IncidentService

__all__ = [
    "Clock",
    "DomainError",
    "FixedClock",
    "IncidentService",
    "Repository",
    "SystemClock",
    "User",
    "UserRole",
]
