"""领域错误类型与 HTTP 状态码映射。"""
from __future__ import annotations


class DomainError(Exception):
    """所有可预期业务错误的基类。"""

    http_status = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, http_status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message}


class ValidationError(DomainError):
    http_status = 400
    code = "validation_error"


class NotFound(DomainError):
    http_status = 404
    code = "not_found"


class Conflict(DomainError):
    http_status = 409
    code = "conflict"


class Forbidden(DomainError):
    http_status = 403
    code = "forbidden"
