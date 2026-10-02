"""规则引擎：纯函数，无 IO、无时钟、无随机。

输出只取决于 (快照材料, 规则版本内容)。评估结果按 (rule_id, subject)
排序后哈希，是"周期重放"不变量的基础：任何人在任何时刻重放同一快照与
同一规则版本，必然得到逐字节一致的结论。

规则种类：
- core_share      核心学科占用资源占比低于下限
- noncore_share   非核心学科占用资源占比高于上限
- commitment      承诺目标完成率低于下限（逐目标）
- missing_core    使命核心学科在指定材料中完全没有投入/招生

注意：命中规则只产生"待核"发现，绝不等于违规定性。
"""
from __future__ import annotations

from typing import Any

from .errors import DomainError
from .hashing import canonical_json, digest

LEVELS = ("watch", "warn")
RULE_KINDS = ("core_share", "noncore_share", "commitment", "missing_core")

# 承诺指标 -> (材料来源, 字段)
COMMITMENT_METRICS = {
    "funding": ("investment", "funding"),
    "faculty": ("investment", "faculty"),
    "slots": ("investment", "slots"),
    "intake": ("enrollment", "intake"),
    "service_scale": ("service", "scale"),
}
SOURCE_FIELDS = {
    "investment": ("funding", "faculty", "slots"),
    "enrollment": ("intake",),
    "service": ("scale",),
}


def _num(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DomainError(f"{where} 必须是非负数字")
    if value < 0:
        raise DomainError(f"{where} 不能为负数")
    return float(value)


def _round6(x: float) -> float:
    return round(x + 0.0, 6)


def validate_payload(kind: str, payload: Any) -> None:
    """对五类材料做结构校验；封存前与提交时各调用一次。"""
    if not isinstance(payload, dict):
        raise DomainError(f"{kind} 材料必须是对象")
    if kind == "mission":
        codes = payload.get("core_discipline_codes")
        if not isinstance(codes, list) or not codes or any(
            not isinstance(c, str) or not c for c in codes
        ):
            raise DomainError("mission.core_discipline_codes 必须是非空字符串列表")
        if len(codes) != len(set(codes)):
            raise DomainError("mission.core_discipline_codes 不能重复")
        if "text" in payload and not isinstance(payload["text"], str):
            raise DomainError("mission.text 必须是字符串")
    elif kind == "investment":
        items = payload.get("disciplines")
        if not isinstance(items, list):
            raise DomainError("investment.disciplines 必须是列表")
        for i, it in enumerate(items):
            _validate_code_entry(it, f"investment.disciplines[{i}]", ("funding", "faculty", "slots"))
    elif kind == "enrollment":
        items = payload.get("programs")
        if not isinstance(items, list):
            raise DomainError("enrollment.programs 必须是列表")
        for i, it in enumerate(items):
            _validate_code_entry(it, f"enrollment.programs[{i}]", ("intake",))
    elif kind == "service":
        items = payload.get("projects")
        if not isinstance(items, list):
            raise DomainError("service.projects 必须是列表")
        for i, it in enumerate(items):
            where = f"service.projects[{i}]"
            if not isinstance(it, dict) or not it.get("id"):
                raise DomainError(f"{where} 缺少 id")
            if "core_related" in it and not isinstance(it["core_related"], bool):
                raise DomainError(f"{where}.core_related 必须是布尔值")
            _num(it.get("scale", 0), f"{where}.scale")
    elif kind == "commitments":
        targets = payload.get("targets")
        if not isinstance(targets, list) or not targets:
            raise DomainError("commitments.targets 必须是非空列表")
        ids = []
        for i, t in enumerate(targets):
            where = f"commitments.targets[{i}]"
            if not isinstance(t, dict) or not t.get("id") or not t.get("name"):
                raise DomainError(f"{where} 缺少 id/name")
            if t["metric"] not in COMMITMENT_METRICS:
                raise DomainError(f"{where}.metric 不支持：{t['metric']}")
            _num(t.get("target"), f"{where}.target")
            if t["target"] <= 0:
                raise DomainError(f"{where}.target 必须为正数")
            ids.append(t["id"])
        if len(ids) != len(set(ids)):
            raise DomainError("commitments.targets 的 id 不能重复")
    else:  # pragma: no cover - 存储层 CHECK 已兜底
        raise DomainError(f"未知材料类型：{kind}")


def _validate_code_entry(it: Any, where: str, fields: tuple[str, ...]) -> None:
    if not isinstance(it, dict) or not it.get("code"):
        raise DomainError(f"{where} 缺少 code")
    if "name" in it and not isinstance(it["name"], str):
        raise DomainError(f"{where}.name 必须是字符串")
    for f in fields:
        _num(it.get(f, 0), f"{where}.{f}")


def validate_rule(rule: Any) -> None:
    """校验单条规则；批准规则集前强制全量校验。"""
    if not isinstance(rule, dict):
        raise DomainError("规则必须是对象")
    rid = rule.get("id")
    if not isinstance(rid, str) or not rid:
        raise DomainError("规则缺少 id")
    if not rule.get("name"):
        raise DomainError(f"规则 {rid} 缺少 name")
    if rule.get("level", "warn") not in LEVELS:
        raise DomainError(f"规则 {rid} 的 level 只能是 watch/warn")
    kind = rule.get("kind")
    p = rule.get("params")
    if kind not in RULE_KINDS or not isinstance(p, dict):
        raise DomainError(f"规则 {rid} 的 kind 无效")
    if kind in ("core_share", "noncore_share"):
        if p.get("source") not in SOURCE_FIELDS:
            raise DomainError(f"规则 {rid} 的 source 无效")
        field = "field"
        if p.get(field) not in SOURCE_FIELDS[p["source"]]:
            raise DomainError(f"规则 {rid} 的 field 与 source 不匹配")
        key = "min_share" if kind == "core_share" else "max_share"
        v = _num(p.get(key), f"规则 {rid}.{key}")
        if not 0 <= v <= 1:
            raise DomainError(f"规则 {rid}.{key} 必须在 0~1 之间")
    elif kind == "commitment":
        v = _num(p.get("min_ratio"), f"规则 {rid}.min_ratio")
        if not 0 <= v <= 1:
            raise DomainError(f"规则 {rid}.min_ratio 必须在 0~1 之间")
    elif kind == "missing_core":
        srcs = p.get("sources")
        if not isinstance(srcs, list) or not srcs or any(
            s not in ("investment", "enrollment", "service") for s in srcs
        ):
            raise DomainError(f"规则 {rid}.sources 必须是来源列表")


def validate_settings(settings: Any) -> None:
    if not isinstance(settings, dict):
        raise DomainError("settings 必须是对象")
    for key in ("explanation_days", "rectification_days", "exemption_max_days"):
        v = settings.get(key)
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise DomainError(f"settings.{key} 必须是正整数（天）")


def _core_codes(items_payload: dict) -> set[str]:
    return set(items_payload["mission"]["core_discipline_codes"])


def _entries(payload: dict, source: str) -> list[dict]:
    key = {"investment": "disciplines", "enrollment": "programs", "service": "projects"}[source]
    return payload.get(source, {}).get(key, [])


def _is_core(entry: dict, core: set[str]) -> bool:
    if "core_related" in entry:  # service 项目可直接标注
        return bool(entry["core_related"]) or entry.get("code") in core
    return entry.get("code") in core


def _finding(rule: dict, subject: str, subject_name: str, metric: str,
             observed: float, threshold: float, extra: dict | None = None) -> dict:
    f = {
        "rule_id": rule["id"],
        "rule_name": rule["name"],
        "kind": rule["kind"],
        "level": rule.get("level", "warn"),
        "subject": subject,
        "subject_name": subject_name,
        "metric": metric,
        "observed": _round6(observed),
        "threshold": _round6(threshold),
    }
    if extra:
        f.update(extra)
    return f


def evaluate(items: dict[str, dict], rules: list[dict]) -> list[dict]:
    """对一份完整快照材料运行规则，返回排序后的待核发现列表。"""
    missing = {"mission", "commitments", "investment", "enrollment", "service"} - set(items)
    if missing:
        raise DomainError("快照材料不完整，缺少：" + "、".join(sorted(missing)))
    core = _core_codes(items)
    findings: list[dict] = []

    for rule in rules:
        p = rule["params"]
        kind = rule["kind"]

        if kind in ("core_share", "noncore_share"):
            source, field = p["source"], p["field"]
            entries = _entries(items, source)
            total = sum(_num(e.get(field, 0), field) for e in entries)
            if total <= 0:
                continue
            core_sum = sum(
                _num(e.get(field, 0), field) for e in entries if _is_core(e, core)
            )
            share = core_sum / total
            if kind == "core_share":
                if share < p["min_share"]:
                    findings.append(_finding(
                        rule, source, f"{source}/{field}", "core_share",
                        share, p["min_share"],
                    ))
            else:
                non_share = 1.0 - share
                if non_share > p["max_share"]:
                    findings.append(_finding(
                        rule, source, f"{source}/{field}", "noncore_share",
                        non_share, p["max_share"],
                    ))

        elif kind == "commitment":
            min_ratio = p["min_ratio"]
            for t in items["commitments"]["targets"]:
                source, field = COMMITMENT_METRICS[t["metric"]]
                entries = _entries(items, source)
                if t.get("code"):
                    actual = sum(
                        _num(e.get(field, 0), field)
                        for e in entries if e.get("code") == t["code"]
                    )
                else:
                    actual = sum(_num(e.get(field, 0), field) for e in entries)
                ratio = actual / float(t["target"])
                if ratio < min_ratio:
                    findings.append(_finding(
                        rule, t["id"], t["name"], "achievement_ratio",
                        ratio, min_ratio,
                        {"target": t["target"], "actual": _round6(actual)},
                    ))

        elif kind == "missing_core":
            for code in sorted(core):
                absent_sources: list[str] = []
                for source in p["sources"]:
                    field = SOURCE_FIELDS[source][0]
                    entries = _entries(items, source)
                    supported = any(
                        e.get("code") == code and _num(e.get(field, 0), field) > 0
                        for e in entries
                    )
                    if not supported:
                        absent_sources.append(source)
                if len(absent_sources) == len(p["sources"]):
                    findings.append(_finding(
                        rule, code, f"核心学科 {code}", "missing_core_support",
                        0.0, 1.0, {"absent_in": absent_sources},
                    ))

    findings.sort(key=lambda f: (f["rule_id"], f["subject"]))
    return findings


def findings_hash(findings: list[dict]) -> str:
    """对发现列表求稳定摘要，用于重放比对。"""
    return digest(canonical_json(findings))
