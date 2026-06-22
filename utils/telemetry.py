"""
utils/telemetry.py — 遥测上报服务。

负责插件的启动事件、配置快照、异常上报和心跳等遥测数据上报。
启用/禁用由配置控制。
"""

from __future__ import annotations

import asyncio
from typing import Any

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger


class TelemetryManager:
    """遥测上报管理器。"""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        plugin_version: str = "0.0.0",
    ):
        self.enabled = False
        self._config = config or {}
        self._plugin_version = plugin_version
        self._http_session = None

    def set_telemetry_config(self, config: dict[str, Any]) -> None:
        """设置遥测配置（由插件生命周期在初始化时调用）。"""
        self._config = config
        self.enabled = bool(config.get("telemetry", {}).get("enabled", False))

    async def track_startup(self) -> None:
        """上报启动事件。"""
        if not self.enabled:
            return
        logger.debug("[遥测] 启动事件已记录")

    async def track_config(self, config: dict[str, Any]) -> None:
        """上报配置快照。"""
        if not self.enabled:
            return
        logger.debug("[遥测] 配置快照已记录")

    async def track_heartbeat(self, uptime_seconds: float) -> None:
        """上报心跳数据。"""
        if not self.enabled:
            return
        logger.debug(f"[遥测] 心跳已发送 (运行 {uptime_seconds:.0f}s)")

    async def track_error(
        self,
        exception: Exception,
        module: str = "",
    ) -> None:
        """上报异常。"""
        if not self.enabled:
            return
        logger.debug(f"[遥测] 异常已记录: {module} -> {exception}")

    async def close(self) -> None:
        """关闭遥测，释放资源。"""
        if self._http_session:
            try:
                await self._http_session.close()
            except Exception:
                pass
            self._http_session = None
