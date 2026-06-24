"""
message/push.py — 消息推送服务。

包含推流编排、推送执行、会话发送。
"""

from __future__ import annotations

import base64
import os
from typing import Any, Callable

try:
    from astrbot.api import logger
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Image, Plain
except ImportError:
    import logging as logger

    # 模拟 MessageChain 和 Plain/Image 用于独立测试
    class Plain:
        def __init__(self, text): self.text = text

    class Image:
        @staticmethod
        def fromBase64(data): return type('Img', (), {'base64': data})()

    class MessageChain:
        def __init__(self, chain): self.chain = chain if isinstance(chain, list) else [chain]

try:
    from ..domain.models import EventEnvelope, EewEvent, EarthquakeReport
    from ..message.presenters import present
except ImportError:
    from domain.models import EventEnvelope, EewEvent, EarthquakeReport
    from message.presenters import present


class SessionSender:
    """会话发送器 — 最底层的消息下发。"""

    def __init__(self, context):
        self.context = context

    async def send(self, session_id: str, message: str | list | MessageChain) -> bool:
        """发送消息到指定会话。"""
        try:
            await self.context.send_message(session_id, message)
            return True
        except Exception as e:
            logger.error(f"[Push] 发送到 {session_id} 失败: {e}")
            return False


class PushExecutionService:
    """推送执行服务。"""

    def __init__(self, config: dict, sender: SessionSender, map_builder=None, snet_renderer=None):
        self.config = config
        self.sender = sender
        self.map_builder = map_builder
        self.snet_renderer = snet_renderer

    async def _render_event_map(self, envelope: EventEnvelope) -> list[str] | None:
        """渲染震中地图（缩略图 + 细节图），返回 base64 列表。"""
        if not self.map_builder:
            return None

        event = envelope.event
        include_map = self.config.get("message_format", {}).get("include_map", True)
        if not include_map:
            return None
        if not isinstance(event, (EewEvent, EarthquakeReport)):
            return None

        lat, lon = event.latitude, event.longitude
        if lat is None or lon is None:
            return None
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            return None

        b64_list = []

        try:
            # 缩略图（zoom 4）
            thumb_cfg = dict(self.config.get("message_format", {}))
            thumb_cfg["map_zoom_level"] = 4
            thumb_path = await self.map_builder.render_map_image(lat, lon, thumb_cfg)
            if thumb_path and os.path.exists(thumb_path):
                with open(thumb_path, "rb") as f:
                    b64_list.append(base64.b64encode(f.read()).decode())
                try:
                    os.unlink(thumb_path)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[Push] 缩略图渲染失败: {e}")

        try:
            # 细节图（zoom 6）
            detail_cfg = dict(self.config.get("message_format", {}))
            detail_cfg["map_zoom_level"] = 8
            detail_path = await self.map_builder.render_map_image(lat, lon, detail_cfg)
            if detail_path and os.path.exists(detail_path):
                with open(detail_path, "rb") as f:
                    b64_list.append(base64.b64encode(f.read()).decode())
                try:
                    os.unlink(detail_path)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[Push] 细节图渲染失败: {e}")

        return b64_list if b64_list else None

    async def _render_snet_map(self, envelope: EventEnvelope) -> list[str] | None:
        """渲染 S-Net 测站分布图，返回 base64 列表。"""
        if not self.snet_renderer:
            return None
        stations = None
        ts_str = ""
        if envelope.metadata:
            stations = envelope.metadata.get("stations")
        if not stations and isinstance(envelope.event, EarthquakeReport):
            raw = envelope.event.raw if isinstance(envelope.event.raw, dict) else {}
            stations = raw.get("stations")
            ts_str = str(raw.get("timestamp", ""))
        if not stations:
            return None
        try:
            from datetime import datetime, timezone
            ts_str = ts_str or datetime.now(timezone.utc).strftime("%Y%m%d%H%M00")
            import tempfile, os, time as _time
            img_path = os.path.join(tempfile.gettempdir(), f"snet_{ts_str}_{int(_time.time())}.png")
            out = await self.snet_renderer.render(stations, img_path, ts_str)
            if out and os.path.exists(out):
                with open(out, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                try:
                    os.unlink(out)
                except Exception:
                    pass
                return [b64]
        except Exception as e:
            logger.error(f"[Push] S-Net 测站图渲染失败: {e}")
        return None

    async def execute_push(
        self,
        envelope: EventEnvelope,
        target_sessions: list[str] | None = None,
        session_config_getter: Callable | None = None,
        **kwargs: Any,
    ) -> bool:
        """执行推送。"""
        try:
            # 融合模式传入了额外数据，暂存到 envelope metadata 供 presenter 使用
            merge_data = kwargs.get("merge_data")
            if merge_data is not None:
                envelope = envelope.__class__(
                    identity=envelope.identity,
                    event=envelope.event,
                    received_at=envelope.received_at,
                    payload=envelope.payload,
                    metadata={**envelope.metadata, "_merge_data": merge_data},
                )
            text = present(envelope)
            if not text:
                return False

            sessions = target_sessions or self.config.get("target_sessions", [])
            if not sessions:
                return False

            # 构建消息链（文本 + 地图图片）
            chain_components = [Plain(text)]
            # S-Net 专用测站分布图
            is_snet = envelope.source_id in ("snet_http", "snet") and isinstance(envelope.event, EarthquakeReport)
            if is_snet:
                snet_b64 = await self._render_snet_map(envelope)
                if snet_b64:
                    for b64 in snet_b64:
                        chain_components.append(Image.fromBase64(b64))
            else:
                # 地震震中图
                map_b64_list = await self._render_event_map(envelope)
                if map_b64_list:
                    for b64 in map_b64_list:
                        chain_components.append(Image.fromBase64(b64))
            # 台风路径图（由 _typhoon_push_adapter 预渲染后塞入 metadata）
            typhoon_img = envelope.metadata.get("_typhoon_image") if envelope.metadata else None
            if typhoon_img:
                chain_components.append(Image.fromBase64(typhoon_img))

            message = MessageChain(chain_components)

            success = False
            for session_id in sessions:
                result = await self.sender.send(session_id, message)
                if result:
                    success = True

            return success

        except Exception as e:
            logger.error(f"[Push] 执行异常: {e}")
            return False


class PushOrchestrator:
    """推送编排器 — 决定推送路径（普通/融合）。"""

    def __init__(self, config: dict, execute_push: Callable, sender: SessionSender | None = None):
        self.config = config
        self._execute_push = execute_push
        self._sender = sender

    async def push_event(
        self,
        envelope: EventEnvelope,
        target_sessions: list[str] | None = None,
        session_config_getter: Callable | None = None,
        **kwargs: Any,
    ) -> bool:
        """编排推送（支持融合场景的 merge_data 参数）。"""
        return await self._execute_push(
            envelope,
            target_sessions=target_sessions,
            session_config_getter=session_config_getter,
            **kwargs,
        )

    async def send_to_admin(self, text: str) -> bool:
        """发送管理通知文本（供 SystemNotificationService 调用）。"""
        if not self._sender:
            return False
        sessions = self.config.get("target_sessions", [])
        if not sessions:
            return False
        success = False
        for session_id in sessions:
            if await self._sender.send(session_id, text):
                success = True
        return success
