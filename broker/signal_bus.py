"""
broker/signal_bus.py — 异步信号总线。

所有连接器的原始数据统一从这里进入，
router 注册回调来消费信号。

模式: 发布-订阅 (Pub/Sub)，适配 asyncio。
类似 CAPQuakeQt 的 RawSignalBus，但使用回调而非 Qt 信号。
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine

Handler = Callable[[str, Any], Coroutine[Any, Any, None]]


class SignalBus:
    """异步信号总线。"""

    def __init__(self):
        self._handlers: dict[str, list[Handler]] = {}

    def on(self, source_id: str, handler: Handler) -> None:
        """注册信号处理器。"""
        self._handlers.setdefault(source_id, []).append(handler)

    def off(self, source_id: str, handler: Handler) -> None:
        """移除信号处理器。"""
        handlers = self._handlers.get(source_id, [])
        if handler in handlers:
            handlers.remove(handler)

    async def emit(self, source_id: str, data: Any) -> None:
        """发射原始数据到所有已注册的处理器。"""
        handlers = self._handlers.get(source_id, [])[:]
        for handler in handlers:
            try:
                await handler(source_id, data)
            except Exception as e:
                try:
                    from astrbot.api import logger
                except ImportError:
                    import logging as logger
                logger.error(f"[SignalBus] handler error for {source_id}: {e}")

    def registered_sources(self) -> list[str]:
        """返回已注册的 source_id 列表。"""
        return list(self._handlers.keys())
