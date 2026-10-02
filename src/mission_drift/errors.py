"""领域错误类型。"""
from __future__ import annotations


class DomainError(Exception):
    """所有业务规则违例的基类，HTTP 层映射为 4xx。"""

    status = 400

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status


class NotFoundError(DomainError):
    status = 404


class ConflictError(DomainError):
    status = 409


class PermissionError_(DomainError):
    status = 403
