"""
GlobalQuake 解析器 — 支持二进制 (protobuf) 和 JSON 两种格式。

数据源: global_quake
"""

from __future__ import annotations

import json
from datetime import datetime

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger

try:
    from ..domain.models import EewEvent, EventEnvelope, EventIdentity, SourcePayload
except ImportError:
    from domain.models import EewEvent, EventEnvelope, EventIdentity, SourcePayload

try:
    from .base import BaseParser
    from .registry import ParserRegistry
    from ..utils.convert import to_float, to_str

except ImportError:
    from parser.base import BaseParser
    from parser.registry import ParserRegistry
    from utils.convert import to_float, to_str

@ParserRegistry.register("global_quake")
class GlobalQuakeParser(BaseParser):
    """GlobalQuake 解析器。"""

    def decode_message(self, message: str | bytes) -> any:
        """GlobalQuake 可能是二进制或 JSON。"""
        return message  # 保持原始格式，由 parse() 判断

    def parse(self, raw: any) -> list[EventEnvelope] | None:
        if isinstance(raw, bytes):
            return self._parse_binary(raw)
        if isinstance(raw, str):
            return self._parse_json_text(raw)
        if isinstance(raw, dict):
            return self._parse_json(raw)
        return None

    def _parse_binary(self, data: bytes) -> list[EventEnvelope] | None:
        """解析 protobuf 二进制消息。"""
        try:
            from ..models.websocket_message_pb2 import WsMessage, MessageType
            msg = WsMessage()
            msg.ParseFromString(data)
            if msg.type == MessageType.EARTHQUAKE:
                return self._parse_protobuf_eq(msg)
            if msg.type == MessageType.HEARTBEAT:
                logger.debug(f"[GQ] 心跳")
                return None
            if msg.type == MessageType.STATUS:
                logger.debug(f"[GQ] 状态: {msg.status_data.server_status}")
                return None
        except Exception as e:
            logger.error(f"[GQ] protobuf 解析异常: {e}")
        return None

    def _parse_protobuf_eq(self, msg) -> list[EventEnvelope] | None:
        eq = msg.earthquake_data
        origin_time = self._parse_datetime(eq.origin_time_iso) if eq.origin_time_iso else None
        if origin_time is None and eq.origin_time_ms:
            origin_time = datetime.utcfromtimestamp(eq.origin_time_ms / 1000.0)

        # 报次
        report_num = int(eq.revision_id) if eq.revision_id is not None else 1
        if report_num <= 0:
            report_num = 1

        # 烈度（protobuf 中是 intensity 字段，罗马数字如 "VII"）
        intensity_raw = str(eq.intensity or "")
        # 地点名
        place_name = str(eq.region or "")

        # 台站统计
        stations = {}
        if eq.HasField("station_count"):
            stations = {
                "total": eq.station_count.total,
                "selected": eq.station_count.selected,
                "used": eq.station_count.used,
            }
        # 质量信息
        quality = {}
        if eq.HasField("quality"):
            quality = {
                "err_origin": eq.quality.err_origin,
                "err_depth": eq.quality.err_depth,
                "pct": eq.quality.pct,
                "stations": eq.quality.stations,
            }
        # 深度置信区间
        depth_conf = None
        if eq.HasField("depth_confidence"):
            depth_conf = {
                "min": eq.depth_confidence.min_depth,
                "max": eq.depth_confidence.max_depth,
            }

        event = EewEvent(
            source_id=self.source_id,
            event_id=str(eq.id),
            occurred_at=origin_time,
            latitude=to_float(eq.latitude),
            longitude=to_float(eq.longitude),
            depth=to_float(eq.depth),
            magnitude=to_float(eq.magnitude),
            place_name=place_name,
            max_intensity=intensity_raw,
            report_num=report_num,
            raw={
                "protobuf": True,
                "id": eq.id,
                "revision_id": eq.revision_id,
                "region": eq.region,
            },
        )

        identity = EventIdentity(
            event_id=str(eq.id),
            source_id=self.source_id,
            event_type="eew",
            provider_family="global_quake",
            report_num=report_num,
        )

        metadata = {
            "report_num": report_num,
            "max_pga": eq.max_pga if eq.max_pga else None,
            "stations": stations or None,
            "quality": quality or None,
            "depth_confidence": depth_conf,
            "last_update_ms": eq.last_update_ms if eq.last_update_ms else None,
        }

        return [EventEnvelope(identity=identity, event=event, payload=SourcePayload(
            source_id=self.source_id,
            provider_family="global_quake",
            message_type="protobuf",
            raw={"revision_id": eq.revision_id, "region": eq.region},
        ), metadata=metadata)]

    def _parse_json(self, data: dict) -> list[EventEnvelope] | None:
        if data.get("type") != "earthquake":
            return None

        event = EewEvent(
            source_id=self.source_id,
            event_id=to_str(data.get("id")) or "",
            occurred_at=self._parse_datetime(data.get("time", "")),
            latitude=to_float(data.get("latitude")),
            longitude=to_float(data.get("longitude")),
            depth=to_float(data.get("depth")),
            magnitude=to_float(data.get("magnitude")),
            place_name=str(data.get("region", data.get("location", "")) or ""),
            max_intensity=str(data.get("intensity", data.get("mmi", "")) or ""),
            raw=data,
        )

        identity = EventIdentity(
            event_id=to_str(data.get("id")) or "",
            source_id=self.source_id,
            event_type="eew",
            provider_family="global_quake",
        )

        return [EventEnvelope(identity=identity, event=event, payload=None)]

    def _parse_json_text(self, text: str) -> list[EventEnvelope] | None:
        try:
            data = json.loads(text)
            return self._parse_json(data)
        except json.JSONDecodeError:
            return None
