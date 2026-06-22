"""
pipeline/fusion.py — CENC/CWA EEW 融合策略。

融合策略将来自多个来源的同一事件合并为一次推送，
避免同一地震的 Wolfx 和 FAN Studio 来源重复推送。

改写为 Pipeline-compatible 模式：
  - FusionResult enum 让 pipeline 知道如何处理
  - FusionOrchestrator 作为统一接口
  - 主源延迟等待使用 background task，不阻塞 pipeline
"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any, Callable

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger

try:
    from domain.models import EventEnvelope
except ImportError:
    from ..domain.models import EventEnvelope


class FusionResult(str, Enum):
    """融合结果 — 告诉 pipeline 如何处理当前事件。"""
    PASS_THROUGH = "pass_through"   # 不属融合范围，继续正常流程
    DEFERRED = "deferred"           # 融合已延迟推送（background task），pipeline 跳过推送但记录统计
    SKIP = "skip"                   # 副源数据已被主源合并，跳过


class FusionState:
    """融合状态 — 等待另一侧消息到达的暂存区。"""

    def __init__(self, ttl_seconds: int = 120):
        self._events: dict[str, dict[str, Any]] = {}
        self._ttl = ttl_seconds

    def store(self, event: EventEnvelope, role: str) -> None:
        key = event.identity.event_id
        now = __import__("time").time()
        if key not in self._events:
            self._events[key] = {"primary": None, "secondary": None, "time": now}
        self._events[key][role] = event
        self._events[key]["time"] = now

    def get_pair(self, event_id: str) -> dict | None:
        return self._events.get(event_id)

    def has_both(self, event_id: str) -> bool:
        state = self._events.get(event_id)
        if state is None:
            return False
        return state["primary"] is not None and state["secondary"] is not None

    def clean_expired(self) -> int:
        now = __import__("time").time()
        expired = [k for k, v in self._events.items() if now - v["time"] > self._ttl]
        for k in expired:
            del self._events[k]
        return len(expired)


class CencFusionService:
    """CENC 烈度融合 — Wolfx(主) + FAN Studio(次)。

    主源到达后启动 background task 等待副源，
    超时则单独推送主源；副源到达后检查主源是否已在，
    若在则跳过推送（由主源统一推送），若不在则单独推送副源。
    """

    def __init__(self, push_callback: Callable):
        self._state = FusionState(ttl_seconds=30)
        self._push = push_callback
        self._stopped = False

    async def intercept(self, envelope: EventEnvelope) -> str:
        eid = envelope.identity.event_id
        sid = envelope.source_id

        if sid == "cenc_wolfx":
            # Wolfx = 主源
            self._state.store(envelope, "primary")
            pair = self._state.get_pair(eid)
            if pair and pair["secondary"]:
                logger.debug(f"[Fusion] CENC 双方已到，立即融合推送: {eid}")
                asyncio.create_task(self._push_merged(envelope, pair["secondary"]))
                return FusionResult.DEFERRED
            asyncio.create_task(self._wait_primary(envelope))
            return FusionResult.DEFERRED

        if sid == "cenc_fanstudio":
            # FAN Studio = 副源
            self._state.store(envelope, "secondary")
            pair = self._state.get_pair(eid)
            if pair and pair["primary"]:
                logger.debug(f"[Fusion] CENC 副源到达，主源已在处理: {eid}")
                return FusionResult.SKIP
            # 主源还没到，直接推送副源
            return FusionResult.PASS_THROUGH

        return FusionResult.PASS_THROUGH

    async def _wait_primary(self, primary: EventEnvelope) -> None:
        """等待副源到达（最多 6 秒），然后推送。"""
        eid = primary.identity.event_id
        try:
            await asyncio.sleep(6)
            if self._stopped:
                return
            pair = self._state.get_pair(eid)
            if pair and pair["secondary"]:
                await self._push_merged(primary, pair["secondary"])
            else:
                # 超时无副源，单独推主源
                logger.debug(f"[Fusion] CENC 副源超时，单独推主源: {eid}")
                await self._push(primary)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[Fusion] CENC 延迟推送失败: {e}")

    async def _push_merged(self, primary: EventEnvelope, secondary: EventEnvelope) -> None:
        """推送融合数据。"""
        try:
            logger.debug(f"[Fusion] CENC 融合推送: {primary.identity.event_id}")
            await self._push(primary, merge_data=secondary.event.raw)
        except Exception as e:
            logger.error(f"[Fusion] CENC 融合推送异常: {e}")

    def stop(self):
        self._stopped = True


class CwaEewFusionService:
    """CWA 震度融合 — FAN Studio(主) + Wolfx(次)。

    主源到达后启动 background task 等待副源，
    超时则单独推送主源；副源到达后检查主源是否已在。
    """

    def __init__(self, push_callback: Callable):
        self._state = FusionState(ttl_seconds=30)
        self._push = push_callback
        self._stopped = False

    async def intercept(self, envelope: EventEnvelope) -> str:
        eid = envelope.identity.event_id
        sid = envelope.source_id

        if sid == "cwa_fanstudio":
            # FAN Studio = 主源
            self._state.store(envelope, "primary")
            pair = self._state.get_pair(eid)
            if pair and pair["secondary"]:
                asyncio.create_task(self._push_merged(envelope, pair["secondary"]))
                return FusionResult.DEFERRED
            asyncio.create_task(self._wait_primary(envelope))
            return FusionResult.DEFERRED

        if sid == "cwa_wolfx":
            # Wolfx = 副源
            self._state.store(envelope, "secondary")
            pair = self._state.get_pair(eid)
            if pair and pair["primary"]:
                return FusionResult.SKIP
            # Wolfx 副源单独到达（无主源），直接推
            return FusionResult.PASS_THROUGH

        return FusionResult.PASS_THROUGH

    async def _wait_primary(self, primary: EventEnvelope) -> None:
        eid = primary.identity.event_id
        try:
            await asyncio.sleep(3)
            if self._stopped:
                return
            pair = self._state.get_pair(eid)
            if pair and pair["secondary"]:
                await self._push_merged(primary, pair["secondary"])
            else:
                logger.debug(f"[Fusion] CWA 副源超时，单独推主源: {eid}")
                await self._push(primary)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[Fusion] CWA 延迟推送失败: {e}")

    async def _push_merged(self, primary: EventEnvelope, secondary: EventEnvelope) -> None:
        try:
            logger.debug(f"[Fusion] CWA 融合推送: {primary.identity.event_id}")
            await self._push(primary, merge_data=secondary.event.raw)
        except Exception as e:
            logger.error(f"[Fusion] CWA 融合推送异常: {e}")

    def stop(self):
        self._stopped = True


class FusionOrchestrator:
    """融合编排器 — pipeline 的唯一接口。

    按配置启用的融合策略创建对应服务，
    intercept() 自动路由到正确的服务。
    """

    def __init__(self, config: dict, push_callback: Callable):
        self._services: list = []
        strategies = config.get("strategies", {})
        if isinstance(strategies, dict):
            if strategies.get("cenc_fusion", {}).get("enabled"):
                self._services.append(CencFusionService(push_callback))
                logger.info("[Fusion] CENC 融合策略已启用")
            if strategies.get("cwa_eew_fusion", {}).get("enabled"):
                self._services.append(CwaEewFusionService(push_callback))
                logger.info("[Fusion] CWA EEW 融合策略已启用")
        if not self._services:
            logger.info("[Fusion] 未启用任何融合策略")

    async def intercept(self, envelope: EventEnvelope) -> str:
        """遍历所有服务，返回第一个非 PASS_THROUGH 的结果。"""
        for svc in self._services:
            result = await svc.intercept(envelope)
            if result != FusionResult.PASS_THROUGH:
                return result
        return FusionResult.PASS_THROUGH

    def stop(self):
        for svc in self._services:
            if hasattr(svc, "stop"):
                svc.stop()

    @property
    def enabled(self) -> bool:
        return len(self._services) > 0


__all__ = [
    "FusionResult",
    "FusionOrchestrator",
    "CencFusionService",
    "CwaEewFusionService",
]
