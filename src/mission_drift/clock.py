"""时钟抽象：业务逻辑只依赖 Clock，测试与重放可注入固定时间。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    def today(self): ...


class SystemClock:
    """生产时钟：UTC，秒级精度即可满足审计粒度。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc).replace(microsecond=0)

    def today(self):
        return self.now().date()


class FixedClock:
    """确定性时钟：测试与离线重放使用。"""

    def __init__(self, moment: datetime | str) -> None:
        if isinstance(moment, str):
            moment = datetime.fromisoformat(moment)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        self._moment = moment

    def now(self) -> datetime:
        return self._moment

    def today(self):
        return self._moment.date()

    def advance(self, **kwargs) -> None:
        self._moment = self._moment + timedelta(**kwargs)
