"""
parser/base.py — BaseParser 抽象基类。

所有解析器继承此类，只需实现 parse() 方法。
基类提供通用能力：JSON 解码、心跳检测、日志去噪、时间解析。
"""

from __future__ import annotations

import json
import time
import traceback
from typing import Any

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger

try:
    from ..utils.time import parse_ts
except ImportError:
    from utils.time import parse_ts


class BaseParser:
    """解析器基类。

    子类需实现:
        parse(self, raw: dict | str | bytes) -> list[领域模型]

    基类提供:
        decode_message() — JSON 解码
        _is_heartbeat()  — 心跳检测
        _parse_datetime() — 时间解析
        _log_once()      — 日志去重去噪
    """

    def __init__(self, source_id: str):
        self.source_id = source_id
        self._warning_cache: dict[str, tuple[float, str]] = {}
        self._warning_cache_timeout = 3600
        self._last_heartbeat_check: float = 0.0

    # ── 主入口 ──

    def parse_message(self, message: str | bytes | dict | list) -> Any | None:
        """解析原始消息，返回领域模型列表或 None。"""
        try:
            payload = self.decode_message(message)
            if payload is None:
                return None
            return self.parse(payload)
        except json.JSONDecodeError as exc:
            logger.error(f"[{self.source_id}] JSON解析失败: {exc}")
            return None
        except Exception as exc:
            logger.error(f"[{self.source_id}] 消息处理失败: {exc}")
            logger.error(traceback.format_exc())
            return None

    def decode_message(self, message: str | bytes | dict | list) -> Any:
        """解码原始消息。

        子类可覆写以支持 protobuf 等非 JSON 格式。

        自动识别 dict/list（HTTP poller 预解析）、bytes（protobuf）、
        JSON str、以及 HTML/XML str（JSON 解析失败时原样返回）。
        """
        if isinstance(message, (dict, list)):
            return message  # HTTP poller 已预解析
        if isinstance(message, bytes):
            return message  # protobuf / 二进制
        # 尝试 JSON，失败则原样返回（HTML/XML 解析器用）
        try:
            return json.loads(message)
        except (json.JSONDecodeError, TypeError) as e:
            if isinstance(message, str) and len(message) > 2:
                logger.debug(f"[{self.source_id}] 数据非 JSON，降级为原始字符串处理: {e}")
            return message

    def parse(self, raw: Any) -> list[Any] | None:
        """解析方法 — 子类实现。

        Args:
            raw: decode_message 后的数据（dict 或 bytes）。

        Returns:
            list[领域模型] — 可能为空列表（如心跳包）。
            None — 解析失败。
        """
        raise NotImplementedError

    # ── 心跳检测 ──

    def _is_heartbeat(self, data: dict) -> bool:
        """检测是否为心跳包或空载荷。"""
        now = time.time()
        if now - self._last_heartbeat_check < 30:
            return False
        self._last_heartbeat_check = now

        # 规则 1: 空坐标 (0,0)
        lat = data.get("latitude")
        lon = data.get("longitude")
        if lat == 0 and lon == 0:
            return True

        # 规则 2: 关键字段大面积为空
        critical = {
            "usgs_fanstudio": ["id", "magnitude", "placeName"],
            "china_tsunami_fanstudio": ["warningInfo", "code", "timeInfo"],
            "china_weather_fanstudio": ["title", "description"],
        }
        fields = critical.get(self.source_id, [])
        if fields:
            empty = sum(1 for f in fields if data.get(f) in ("", None, {}))
            if empty >= len(fields) / 2:
                return True
        return False

    # ── 辅助方法 ──

    def _parse_datetime(self, time_str: Any) -> Any:
        """解析时间（防御非字符串输入，支持 epoch 数字）。"""
        if isinstance(time_str, (int, float)):
            return parse_ts(time_str)
        if not isinstance(time_str, str):
            return None
        dt = parse_ts(time_str)
        if dt is None and time_str:
            logger.warning(f"[{self.source_id}] 时间解析失败: '{time_str}'")
        return dt

    def _log_once(self, key: str, msg: str) -> None:
        """1 小时内相同的警告只输出一次。"""
        now = time.time()
        cached = self._warning_cache.get(key)
        if cached and now - cached[0] < self._warning_cache_timeout and cached[1] == msg:
            return
        self._warning_cache[key] = (now, msg)
        logger.warning(f"[{self.source_id}] {msg}")
