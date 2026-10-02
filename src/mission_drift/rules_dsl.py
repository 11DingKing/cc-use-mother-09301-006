"""规则 DSL：安全、可审计、确定性的偏离判定表达式。

规则是结构化 JSON，而非代码，杜绝 ``eval``。一条规则形如::

    {
      "id": "rule-basic-enroll-share",
      "name": "基础学科招生占比持续走低",
      "input": {
        "basic_share": {"path": "enrollment.basic_discipline_ratio"},
        "baseline": {"path": "enrollment.basic_discipline_ratio", "baseline": true}
      },
      "condition": {"<": [{"ref": "basic_share"}, 0.30]},
      "trend": {"metric": "basic_share", "direction": "decreasing", "periods": 3},
      "severity": "high",
      "message": "基础学科招生占比 {basic_share:.1%} 低于阈值 30%，且连续三年下降"
    }

判定语义：

* ``condition`` 为真构成阈值偏离；``trend`` 存在时还要求指标在最近 N 个
  已封存周期单调（允许持平）朝指定方向变化。
* 基线周期数据不足时 ``baseline`` 引用解析失败，该规则不出警（防止误判）。

支持的操作符：``and or not > >= < <= == != in between pct_change``。
"""
from __future__ import annotations

from typing import Any

from .errors import ValidationError

ENGINE_VERSION = "1.0.0"

VALID_SEVERITIES = {"medium", "high"}
VALID_TREND_DIRECTIONS = {"increasing", "decreasing"}
ALLOWED_OPS = {"and", "or", "not", ">", ">=", "<", "<=", "==", "!=", "in", "between", "pct_change"}
SNAPSHOT_KINDS = ("mission", "commitment", "discipline_input", "enrollment", "service_output")


class RuleError(ValidationError):
    pass


def validate_definition(definition: dict) -> None:
    """入库前校验规则定义，拒绝无法理解的表达式。"""
    if not isinstance(definition, dict):
        raise RuleError("规则定义必须是对象")
    for key in ("id", "name", "input", "condition"):
        if key not in definition:
            raise RuleError(f"规则缺少字段：{key}")
    inputs = definition["input"]
    if not isinstance(inputs, dict) or not inputs:
        raise RuleError("input 必须是非空对象")
    for name, spec in inputs.items():
        if not isinstance(spec, dict) or "path" not in spec:
            raise RuleError(f"输入 {name} 必须包含 path")
        path = spec["path"]
        if not isinstance(path, str) or not path:
            raise RuleError(f"输入 {name} 的 path 非法")
        head = path.split(".", 1)[0]
        if head not in SNAPSHOT_KINDS:
            raise RuleError(f"输入 {name} 的根必须是五要素之一：{head}")
        if "baseline" in spec and not isinstance(spec["baseline"], bool):
            raise RuleError(f"输入 {name} 的 baseline 必须是布尔值")
    severity = definition.get("severity", "medium")
    if severity not in VALID_SEVERITIES:
        raise RuleError(f"severity 非法：{severity}")
    trend = definition.get("trend")
    if trend is not None:
        if not isinstance(trend, dict):
            raise RuleError("trend 必须是对象")
        if trend.get("metric") not in inputs:
            raise RuleError("trend.metric 必须引用 input 中声明的非基线指标")
        if inputs[trend["metric"]].get("baseline"):
            raise RuleError("trend.metric 不能引用基线输入")
        if trend.get("direction") not in VALID_TREND_DIRECTIONS:
            raise RuleError("trend.direction 只能是 increasing/decreasing")
        periods = trend.get("periods")
        if not isinstance(periods, int) or periods < 2:
            raise RuleError("trend.periods 必须是 >=2 的整数")
    _validate_expr(definition["condition"], set(inputs))


def _validate_expr(expr: Any, input_names: set[str]) -> None:
    if not isinstance(expr, dict) or len(expr) != 1:
        raise RuleError("表达式必须是恰好含一个操作符的对象")
    (op, args), = expr.items()
    if op not in ALLOWED_OPS:
        raise RuleError(f"不支持的操作符：{op}")
    if op == "not":
        if not isinstance(args, list) or len(args) != 1:
            raise RuleError("not 接受恰好 1 个参数")
        _validate_expr(args[0], input_names)
        return
    if not isinstance(args, list) or len(args) < 2:
        raise RuleError(f"{op} 至少需要 2 个参数")
    for arg in args:
        if isinstance(arg, dict) and len(arg) == 1 and "ref" in arg:
            if arg["ref"] not in input_names:
                raise RuleError(f"引用了未声明的输入：{arg['ref']}")
        elif isinstance(arg, dict):
            _validate_expr(arg, input_names)
        elif isinstance(arg, list):
            for item in arg:
                if isinstance(item, dict):
                    _validate_expr(item, input_names)
        # 字面量（数字/字符串）无需校验


class _Missing:
    """基线数据不足等原因导致的输入缺失。"""


MISSING = _Missing()


def _get_path(data: dict, dotted: str) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return MISSING
        cur = cur[part]
    if isinstance(cur, (dict, list)) or cur is None:
        return MISSING
    return cur


def resolve_inputs(definition: dict, current: dict, history: list[dict]) -> dict[str, Any]:
    """解析规则输入。

    history 按周期序数升序，最后一个元素为当前周期。
    trend 需要的历史序列也在此预取；任何必需输入缺失则抛 ``KeyError``。
    """
    values: dict[str, Any] = {}
    trend = definition.get("trend")
    for name, spec in definition["input"].items():
        use_baseline = bool(spec.get("baseline"))
        source = _baseline_for(history) if use_baseline else current
        value = _get_path(source, spec["path"])
        if value is MISSING:
            raise KeyError(name)
        values[name] = value
    if trend:
        metric = trend["metric"]
        path = definition["input"][metric]["path"]
        series = []
        for snapshot in history:
            v = _get_path(snapshot, path)
            if v is MISSING:
                raise KeyError(f"trend:{metric}")
            series.append(v)
        values[f"__trend__{metric}"] = series
    return values


def _baseline_for(history: list[dict]) -> dict:
    """基线 = 当前周期之前最近一个已封存周期；没有则缺失。"""
    if len(history) < 2:
        return {}
    return history[-2]


def _pct_change(old: Any, new: Any) -> float:
    if old == 0:
        raise RuleError("pct_change 的基准值为 0，无法计算变化率")
    return (new - old) / abs(old)


def _eval(expr: Any, values: dict[str, Any]) -> Any:
    if isinstance(expr, dict) and "ref" in expr and len(expr) == 1:
        return values[expr["ref"]]
    if not isinstance(expr, dict) or len(expr) != 1:
        return expr
    (op, args), = expr.items()
    if op == "and":
        return all(_eval(a, values) for a in args)
    if op == "or":
        return any(_eval(a, values) for a in args)
    if op == "not":
        return not _eval(args[0], values)
    if op == "in":
        return _eval(args[0], values) in [_eval(a, values) for a in args[1]]
    if op == "between":
        x, low, high = (_eval(a, values) for a in args)
        return low <= x <= high
    if op == "pct_change":
        old, new = _eval(args[0], values), _eval(args[1], values)
        return _pct_change(old, new)
    left, right = _eval(args[0], values), _eval(args[1], values)
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    raise RuleError(f"不支持的操作符：{op}")


def _trend_matches(series: list, direction: str, periods: int) -> bool:
    window = series[-periods:]
    if len(window) < periods:
        return False
    for prev, cur in zip(window, window[1:]):
        if direction == "decreasing" and cur > prev:
            return False
        if direction == "increasing" and cur < prev:
            return False
    # 完全持平不算"漂移趋势"
    if len(set(window)) == 1:
        return False
    return True


def evaluate(definition: dict, current: dict, history: list[dict]) -> dict | None:
    """纯函数评估。命中返回 finding 详情，未命中或数据不足返回 None。"""
    try:
        values = resolve_inputs(definition, current, history)
    except KeyError:
        return None
    if not _eval(definition["condition"], values):
        return None
    trend = definition.get("trend")
    if trend and not _trend_matches(
        values[f"__trend__{trend['metric']}"], trend["direction"], trend["periods"]
    ):
        return None
    return {
        "value_path": _primary_path(definition),
        "values": {k: v for k, v in values.items() if not k.startswith("__")},
        "severity": definition.get("severity", "medium"),
        "message": _format_message(definition.get("message", ""), values),
    }


def _primary_path(definition: dict) -> str:
    for name, spec in definition["input"].items():
        if not spec.get("baseline"):
            return spec["path"]
    return next(iter(definition["input"].values()))["path"]


def _format_message(template: str, values: dict) -> str:
    if not template:
        return ""
    clean = {k: v for k, v in values.items() if not k.startswith("__")}
    try:
        return template.format(**clean)
    except (KeyError, IndexError, ValueError):
        return template
