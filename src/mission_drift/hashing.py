"""规范化序列化与内容寻址哈希。

全系统的哈希都建立在同一种规范化 JSON 之上：
键排序、无多余空白、不允许 NaN/Infinity，保证任何语言、
任何机器重放时对同一数据得到完全相同的摘要。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(obj: Any) -> str:
    """把对象序列化为唯一确定的字符串。"""
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest(obj: Any) -> str:
    """对任意可序列化对象求 SHA-256 摘要。"""
    return sha256_hex(canonical(obj))
