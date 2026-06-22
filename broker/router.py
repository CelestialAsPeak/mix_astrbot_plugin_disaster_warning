"""
broker/router.py — 消息路由器。

将 SignalBus 的原始数据分发到对应的解析器，
解析后的 EventEnvelope 送入 Pipeline。

职责:
  1. 接收 signal_bus 发射的 (source_id, raw_data)
  2. ParserRegistry.get(source_id) 获取解析器
  3. parser.parse(raw_data) → list[EventEnvelope]
  4. 每个 EventEnvelope → pipeline.handle()
"""

from __future__ import annotations

from typing import Any, Callable

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger

try:
    from parser.registry import ParserRegistry
    from pipeline.pipeline import EventPipeline
except ImportError:
    from ..parser.registry import ParserRegistry
    from ..pipeline.pipeline import EventPipeline


class MessageRouter:
    """消息路由器。"""

    def __init__(self, pipeline: EventPipeline | None = None):
        self._pipeline = pipeline
        self._source_filters: dict[str, Callable] = {}

    def set_pipeline(self, pipeline: EventPipeline):
        self._pipeline = pipeline

    def add_filter(self, source_id: str, filter_fn: Callable) -> None:
        """添加源级过滤器。"""
        self._source_filters[source_id] = filter_fn

    async def route(self, source_id: str, raw_data: Any) -> None:
        """路由一条原始消息到对应解析器。

        Args:
            source_id: 数据源 ID
            raw_data: 原始消息（dict / str / bytes）
        """
        # 源级预过滤
        filter_fn = self._source_filters.get(source_id)
        if filter_fn:
            try:
                if not filter_fn(raw_data):
                    return
            except Exception:
                pass

        # 获取解析器
        parser = ParserRegistry.get(source_id)
        if parser is None:
            logger.debug(f"[Router] 未找到 {source_id} 的解析器")
            return

        # 解析
        result = parser.parse_message(raw_data)
        if not result:
            return

        # 确保是列表
        envelopes = result if isinstance(result, list) else [result]

        # 送入 Pipeline
        if self._pipeline:
            for env in envelopes:
                try:
                    await self._pipeline.handle(env)
                except Exception as e:
                    logger.error(f"[Router] pipeline处理失败: {e}")
