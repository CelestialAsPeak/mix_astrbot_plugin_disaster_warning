"""
utils/convert.py — 安全类型转换工具。

所有函数为纯函数。
失败/空值时返回 default（默认 None），不会抛异常。
"""

from __future__ import annotations


def to_float(value: object, default: float | None = None) -> float | None:
    """安全转换为 float，失败返回 default。"""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return default
        try:
            return float(s)
        except (ValueError, TypeError):
            pass
    return default


def to_int(value: object, default: int | None = None) -> int | None:
    """安全转换为 int，失败返回 default。"""
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return default
        try:
            return int(s)
        except (ValueError, TypeError):
            pass
    return default


def to_str(value: object, default: str | None = None) -> str | None:
    """安全转换为 str，None/空值时返回 default。"""
    if value is None:
        return default
    s = str(value)
    return s if s else default
