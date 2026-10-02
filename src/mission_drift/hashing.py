"""确定性哈希与规范化 JSON。

重放不变性依赖：同一输入永远得到同一字节串与摘要。
全链路一律使用 canonical_json，禁止直接 json.dumps 参与哈希。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    """键排序、无空白、非 ASCII 原样保留的稳定序列化。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    """对任意可 JSON 化的值求 SHA-256。"""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def chain_digest(prev_hash: str | None, value: Any) -> str:
    """把前一环节点哈希串入当前摘要，形成防篡改链。"""
    return hashlib.sha256(
        ((prev_hash or "") + "\n" + canonical_json(value)).encode("utf-8")
    ).hexdigest()
