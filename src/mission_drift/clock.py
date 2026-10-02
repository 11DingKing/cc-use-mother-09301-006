"""可注入、可拨快的时钟。

评估与重放引擎绝不读取时钟（保证结果确定性）；
时钟只用于审计轨迹和期限计算。测试与演示脚本可以把时间固定。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


class Clock:
    def __init__(self, fixed: str | None = None) -> None:
        self.fixed = fixed

    def now(self) -> str:
        if self.fixed is not None:
            return self.fixed
        return datetime.now(timezone.utc).isoformat()

    def set(self, value: str) -> None:
        self.fixed = value

    def advance(self, **delta) -> None:
        base = datetime.fromisoformat(self.fixed) if self.fixed else datetime.now(timezone.utc)
        self.fixed = (base + timedelta(**delta)).isoformat()

    def iso_days_from_now(self, days: int) -> str:
        return (parse_ts(self.now()) + timedelta(days=days)).isoformat()


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
