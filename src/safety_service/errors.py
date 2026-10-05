"""领域错误类型，HTTP 层据此映射状态码。"""
from __future__ import annotations


class DomainError(Exception):
    """所有业务错误的基类。"""

    http_status = 400
    error_type = "invalid_request"


class ValidationError(DomainError):
    http_status = 400
    error_type = "validation_error"


class PermissionDenied(DomainError):
    http_status = 403
    error_type = "permission_denied"


class Unauthorized(DomainError):
    http_status = 401
    error_type = "unauthorized"


class NotFound(DomainError):
    http_status = 404
    error_type = "not_found"


class Conflict(DomainError):
    http_status = 409
    error_type = "conflict"
