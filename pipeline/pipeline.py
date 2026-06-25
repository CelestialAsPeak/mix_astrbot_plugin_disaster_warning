"""
pipeline/pipeline.py — 事件处理流水线主编排。

执行顺序:
  1. 规则链评估 → 过滤不符合条件的事件
  2. 去重检查 → 跳过已处理的重复事件
  3. 逐群推送 → 遍历群组，按群组阈值过滤后推送
  4. 统计记录 → 记录推送结果
  5. 入库 → 存储事件到数据库
  6. Web 广播 → 向管理员面板发送摘要
"""

from __future__ import annotations

from typing import Any, Callable

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger

try:
    from ..domain.models import EventEnvelope
    from .base_rule import RuleContext
except ImportError:
    from domain.models import EventEnvelope
    from pipeline.base_rule import RuleContext
try:
    from .rules import build_default_rule_chain
    from .dedup import EventDeduplicator
    from .fusion import FusionResult, FusionOrchestrator
except ImportError:
    from pipeline.rules import build_default_rule_chain
    from pipeline.dedup import EventDeduplicator
    from pipeline.fusion import FusionResult, FusionOrchestrator


def _summarize_envelope(envelope) -> str:
    """从 EventEnvelope 提取关键信息用于日志 — 时间/地名/震级/烈度。"""
    ev = envelope.event
    src = envelope.source_id or "?"
    if ev is None:
        return f"[{src}] 空事件"

    # 事件类型标签
    etype = envelope.identity.event_type if envelope.identity else ""
    if etype in ("earthquake_warning", "eew"):
        tag = "⚠️EEW"
    elif etype == "earthquake_report":
        tag = "📊报告"
    elif etype == "tsunami":
        tag = "🌊海啸"
    elif etype == "weather":
        tag = "🌤天气"
    elif etype == "typhoon":
        tag = "🌀台风"
    else:
        tag = etype or "?"

    # 公共字段
    mag = getattr(ev, "magnitude", None)
    place = getattr(ev, "place_name", None) or getattr(ev, "region", None) or ""
    depth = getattr(ev, "depth", None)
    occurred = getattr(ev, "occurred_at", None) or getattr(ev, "timestamp", None)

    parts = [f"[{src}]", tag]

    # 报次
    report = getattr(ev, "serial", None) or getattr(ev, "report_num", None)
    if report is not None:
        parts.append(f"#{report}")

    # 时间
    if occurred:
        try:
            parts.append(occurred.strftime("%H:%M:%S"))
        except Exception:
            pass

    # 震级
    if mag is not None:
        parts.append(f"M{mag:.1f}")

    # 地名
    if place:
        parts.append(place)
    else:
        lat = getattr(ev, "latitude", None)
        lon = getattr(ev, "longitude", None)
        if lat is not None and lon is not None:
            parts.append(f"({lat:.1f},{lon:.1f})")

    # 深度
    if depth is not None:
        parts.append(f"{depth:.0f}km")

    # 烈度
    intensity = getattr(ev, "max_intensity", None) or getattr(ev, "mmi", None)
    if intensity:
        parts.append(f"烈度={intensity}")

    # 海啸级别
    level = getattr(ev, "level", None)
    if level is not None and level > 0:
        parts.append(f"海啸级别={level}")

    return " ".join(parts)


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
        session_manager=None,
    ):
        self.config = config
        self.push_manager = push_manager
        self.stats_manager = stats_manager
        self.web_admin = web_admin
        self.database_manager = database_manager
        self.session_manager = session_manager  # SessionConfigManager（可选）

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
            target_sessions: 显式指定目标会话（None = 使用配置）
            session_config_getter: 按会话读取配置的函数

        Returns:
            bool: 是否至少成功推送到一个群组
        """
        self.events_processed += 1
        logger.info(f"[事件] {_summarize_envelope(envelope)}")

        # ── 1. 规则链过滤（全局规则） ──
        ctx = RuleContext(
            envelope=envelope,
            config=self.config,
        )
        # 先跑非阈值的全局规则（时间、数据源启用等）
        global_rules = [r for r in self._rule_chain.rules if r.name != "threshold"]
        for rule in global_rules:
            decision = rule.evaluate(ctx)
            if not decision.accepted:
                self.events_filtered += 1
                logger.debug(f"[Pipeline] 事件 {envelope.id} 被全局规则过滤: {decision.reason}")
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
                    logger.debug(f"[Pipeline] 事件 {envelope.id} 已由融合接管")
                    self.events_pushed += 1
                    if self.stats_manager:
                        try:
                            await self.stats_manager.record_push(envelope, pushed=True)
                        except Exception:
                            pass
                    if self.database_manager:
                        try:
                            await self.database_manager.insert_envelope(envelope)
                        except Exception:
                            pass
                    return True
                if fusion_result == FusionResult.SKIP:
                    logger.debug(f"[Pipeline] 事件 {envelope.id} 被融合跳过（副源）")
                    return False
            except Exception as e:
                logger.error(f"[Pipeline] 融合处理异常（降级为正常推送): {e}")

        # ── 3. 逐群组推送 ──
        push_result = False
        any_group_pushed = False

        # 收集阈值规则（群组级重新评估用）
        threshold_rules = [r for r in self._rule_chain.rules if r.name == "threshold"]

        # 获取群组列表
        groups = self._resolve_groups(target_sessions)

        for group_id, sessions in groups:
            if not sessions:
                continue

            # 将群组过滤配置合并到上下文中
            group_config = dict(self.config)
            if self.session_manager:
                # 睡眠模式: 同时支持群组ID匹配和会话ID匹配
                sleep_enabled = self.config.get("sleep_mode_enabled", False)
                sleep_mode_groups = self.config.get("sleep_mode_groups", []) or []
                sleep_mode = False
                if sleep_enabled:
                    if group_id in sleep_mode_groups:
                        sleep_mode = True
                    elif any(sg in s for s in sessions for sg in sleep_mode_groups):
                        # 按会话ID匹配（如 sleep_mode_groups=["985606083"] 匹配 "CAPbots:GroupMessage:985606083"）
                        sleep_mode = True
                if sleep_mode:
                    gf = self.session_manager.get_group_sleep_filters(group_id)
                else:
                    gf = self.session_manager.get_group_filters(group_id)
                if isinstance(gf, dict) and gf:
                    ef = dict(group_config.get("earthquake_filters", {}))
                    ef.update(gf)
                    group_config["earthquake_filters"] = ef
                    if sleep_mode:
                        logger.debug(f"[Pipeline] 群 {group_id} 睡眠模式启用")

            # 对阈值规则重新评估（用合并后的群组配置）
            group_accepted = True
            for rule in threshold_rules:
                ctx2 = RuleContext(envelope=envelope, config=group_config)
                d = rule.evaluate(ctx2)
                if not d.accepted:
                    logger.debug(
                        f"[Pipeline] 群 {group_id} 不满足阈值: {d.reason}"
                    )
                    group_accepted = False
                    break

            if not group_accepted:
                continue

            # 推送到该群组的会话
            if self.push_manager:
                try:
                    r = await self.push_manager.push_event(
                        envelope,
                        target_sessions=sessions,
                        session_config_getter=session_config_getter,
                    )
                    if r:
                        push_result = True
                        any_group_pushed = True
                        logger.debug(f"[Pipeline] 推送到群 {group_id} 成功")
                except Exception as e:
                    logger.error(f"[Pipeline] 群 {group_id} 推送失败: {e}")

        # 事件级计数器（无论推送多少群组，只计一次）
        if any_group_pushed:
            self.events_pushed += 1

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

    def _resolve_groups(
        self, target_sessions: list[str] | None
    ) -> list[tuple[str, list[str]]]:
        """解析推送目标群组列表。

        优先级:
          1. target_sessions 显式指定 → 用 "default" 群组包装
          2. config.groups 存在 → 遍历各群组
          3. config.target_sessions 存在 → 用 "default" 群组包装
          4. 兜底 → 空列表

        Returns:
            [(group_id, [sessions]), ...]
        """
        # 显式指定了目标会话
        if target_sessions is not None:
            return [("_explicit", list(target_sessions))]

        # 通过 SessionConfigManager 获取群组列表
        if self.session_manager:
            groups = self.session_manager.list_groups()
            result = []
            for gid, gcfg in groups.items():
                if isinstance(gcfg, dict) and gcfg.get("enabled", True):
                    sessions = gcfg.get("sessions", [])
                    if isinstance(sessions, list) and sessions:
                        result.append((gid, sessions))
            if result:
                return result

        # 兜底：直接从 config 读 target_sessions
        sessions = self.config.get("target_sessions", [])
        if isinstance(sessions, list) and sessions:
            return [("default", list(sessions))]

        return []

    def get_stats(self) -> dict[str, int]:
        """获取流水线统计。"""
        return {
            "processed": self.events_processed,
            "pushed": self.events_pushed,
            "filtered": self.events_filtered,
            "dedup_cache": self._dedup.size,
        }


__all__ = ["EventPipeline"]
