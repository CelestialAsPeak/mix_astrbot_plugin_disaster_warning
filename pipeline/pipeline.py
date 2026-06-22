"""
pipeline/pipeline.py — 事件处理流水线主编排。

执行顺序:
  1. 规则链评估 → 过滤不符合条件的事件
  2. 去重检查 → 跳过已处理的重复事件
  3. 推送执行 → 构建消息并推送到目标会话
  4. 统计记录 → 记录推送结果
  5. Web 广播 → 向管理员面板发送摘要

Pipeline 不直接依赖具体推送实现，
通过依赖注入接收 message_push_manager / statistics_manager 等。
"""

from __future__ import annotations

from typing import Any, Callable

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger

try:
    from domain.models import EventEnvelope
    from pipeline.base_rule import RuleContext
except ImportError:
    from ..domain.models import EventEnvelope
    from .base_rule import RuleContext
try:
    from pipeline.rules import build_default_rule_chain
    from pipeline.dedup import EventDeduplicator
    from pipeline.fusion import FusionResult, FusionOrchestrator
except ImportError:
    from .rules import build_default_rule_chain
    from .dedup import EventDeduplicator
    from .fusion import FusionResult, FusionOrchestrator


class EventPipeline:
    """事件处理流水线。"""

    def __init__(
        self,
        config: dict[str, Any],
        push_manager=None,
        stats_manager=None,
        web_admin=None,
        rule_chain=None,
        deduplicator=None,
        database_manager=None,
        fusion=None,
    ):
        self.config = config
        self.push_manager = push_manager
        self.stats_manager = stats_manager
        self.web_admin = web_admin
        self.database_manager = database_manager

        # 规则链和去重器
        self._rule_chain = rule_chain or build_default_rule_chain()
        self._dedup = deduplicator or EventDeduplicator(
            ttl_seconds=config.get("event_deduplication", {}).get("ttl_seconds", 300)
        )

        # 融合编排器
        self._fusion = fusion

        # 统计
        self.events_processed = 0
        self.events_pushed = 0
        self.events_filtered = 0

    async def handle(
        self,
        envelope: EventEnvelope,
        target_sessions: list[str] | None = None,
        session_config_getter: Callable | None = None,
    ) -> bool:
        """处理一个事件。

        Args:
            envelope: 统一事件包裹
            target_sessions: 目标会话列表（None = 全部）
            session_config_getter: 按会话读取配置的函数

        Returns:
            bool: 是否成功推送
        """
        self.events_processed += 1

        # ── 1. 规则链过滤 ──
        ctx = RuleContext(
            envelope=envelope,
            config=self.config,
        )
        decision = self._rule_chain.evaluate(ctx)
        if not decision.accepted:
            self.events_filtered += 1
            logger.debug(f"[Pipeline] 事件 {envelope.id} 被规则过滤: {decision.reason}")
            return False

        # ── 2. 去重检查 ──
        if self._dedup.is_duplicate(envelope):
            logger.debug(f"[Pipeline] 事件 {envelope.id} 重复，跳过")
            return False
        self._dedup.mark_processed(envelope)

        # ── 2.5 融合检查（双源去重 + 合并推送） ──
        if self._fusion and self._fusion.enabled:
            try:
                fusion_result = await self._fusion.intercept(envelope)
                if fusion_result == FusionResult.DEFERRED:
                    # 融合服务已启动 background task 处理推送，pipeline 仍然记录统计和入库
                    logger.debug(f"[Pipeline] 事件 {envelope.id} 已由融合接管")
                    self.events_pushed += 1
                    # 记录统计
                    if self.stats_manager:
                        try:
                            await self.stats_manager.record_push(envelope, pushed=True)
                        except Exception:
                            pass
                    # 入库
                    if self.database_manager:
                        try:
                            await self.database_manager.insert_envelope(envelope)
                        except Exception:
                            pass
                    return True
                if fusion_result == FusionResult.SKIP:
                    # 副源数据已被主源处理，完全跳过
                    logger.debug(f"[Pipeline] 事件 {envelope.id} 被融合跳过（副源）")
                    return False
                # PASS_THROUGH → 继续正常流程
            except Exception as e:
                logger.error(f"[Pipeline] 融合处理异常（降级为正常推送): {e}")

        # ── 3. 推送 ──
        push_result = False
        if self.push_manager:
            try:
                push_result = await self.push_manager.push_event(
                    envelope,
                    target_sessions=target_sessions,
                    session_config_getter=session_config_getter,
                )
                if push_result:
                    self.events_pushed += 1
            except Exception as e:
                logger.error(f"[Pipeline] 推送失败: {e}")

        # ── 4. 统计记录 ──
        if self.stats_manager:
            try:
                await self.stats_manager.record_push(
                    envelope,
                    pushed=push_result,
                )
            except Exception as e:
                logger.debug(f"[Pipeline] 统计记录失败: {e}")

        # ── 5. 入库 ──
        if self.database_manager:
            try:
                await self.database_manager.insert_envelope(envelope)
            except Exception as e:
                logger.debug(f"[Pipeline] 入库失败: {e}")

        # ── 6. Web 管理端广播 ──
        if self.web_admin:
            try:
                await self.web_admin.notify_event({
                    "id": envelope.id,
                    "type": envelope.event_type,
                    "source": envelope.source_id,
                    "pushed": push_result,
                })
            except Exception as e:
                logger.debug(f"[Pipeline] WebSocket 通知失败: {e}")

        return push_result

    def get_stats(self) -> dict[str, int]:
        """获取流水线统计。"""
        return {
            "processed": self.events_processed,
            "pushed": self.events_pushed,
            "filtered": self.events_filtered,
            "dedup_cache": self._dedup.size,
        }


__all__ = ["EventPipeline"]
