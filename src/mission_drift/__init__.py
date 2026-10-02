"""高校使命漂移预警服务端。

按周期封存使命与办学快照，以经批准的规则版本进行确定性评估，
将可疑偏离生成待核案件，并保留解释、豁免、整改的期限与审计轨迹。
"""
from .app import App, Actor
from .errors import DomainError, Forbidden, NotFound, Conflict, ValidationError

__all__ = ["App", "Actor", "DomainError", "Forbidden", "NotFound", "Conflict", "ValidationError"]
__version__ = "1.0.0"
