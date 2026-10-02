"""高校使命漂移预警服务端。

职责边界与状态机以 domain/contract.json 为准：
角色 planning（发展规划处）/ department（院系负责人）/ supervisor（校级督导）；
案件状态 监测/预警/核实/整改/关闭。
"""
from .clock import FixedClock, SystemClock
from .services import Services
from .storage import Store

__all__ = ["Services", "Store", "SystemClock", "FixedClock"]
