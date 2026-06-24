"""
utils/time.py — 时间解析工具。

所有函数为纯函数，不持有状态。
返回值一律为 UTC datetime 或带时区的 datetime。
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Union


def parse_epoch_ms(ms: Union[int, float]) -> datetime:
    """将 epoch 毫秒转换为 UTC datetime。"""
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def parse_ts(ts: Union[str, int, float, None]) -> datetime | None:
    """
    尝试多种格式解析时间戳。

    支持：
      - ISO 8601: "2024-01-15T12:30:00Z" / "2024-01-15T21:30:00+09:00"
      - epoch 秒 (int/float)
      - epoch 毫秒 (int > 1e11)
      - 紧凑格式: "20240115123000"
      - CENC: "2025-03-28 14:20:00"
    """
    if ts is None:
        return None

    if isinstance(ts, (int, float)):
        if ts > 1e11:
            return parse_epoch_ms(ts)
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    s = str(ts).strip()
    if not s:
        return None

    # ISO 8601
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        pass

    # "YYYY-MM-DD HH:MM:SS"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            pass

    # "YYYY-MM-DD HH:MM:SS.sss" / "YYYY/MM/DD HH:MM:SS.sss" (truncate ms)
    try:
        base = s.split(".")[0]
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                return datetime.strptime(base, fmt)
            except (ValueError, TypeError):
                pass
    except (ValueError, TypeError):
        pass

    # "YYYY/MM/DD HH:MM" (no seconds, Wolfx eqlist / P2P history 格式)
    for fmt in ("%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            pass

    # 紧凑格式 "20240115123000"
    try:
        return datetime.strptime(s[:14], "%Y%m%d%H%M%S")
    except (ValueError, TypeError):
        pass

    return None


def parse_cst_time(time_str: str) -> datetime | None:
    """
    CENC 等中国数据源时间格式: "2025-03-28 14:20:00"（北京时间 UTC+8）。
    返回带 CST 时区的 datetime。
    """
    if not time_str:
        return None
    dt = parse_ts(time_str)
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone(timedelta(hours=8)))
    return dt.astimezone(timezone(timedelta(hours=8)))


def parse_jst_time(time_str: str) -> datetime | None:
    """
    JMA 等日本数据源时间格式（JST UTC+9）。
    返回带 JST 时区的 datetime。
    """
    if not time_str:
        return None
    dt = parse_ts(time_str)
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone(timedelta(hours=9)))
    return dt.astimezone(timezone(timedelta(hours=9)))
