"""
GlobalQuake 解析器 — 支持二进制 (protobuf) 和 JSON 两种格式。

数据源: global_quake
"""

from __future__ import annotations

import json
from datetime import datetime

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
        except Exception:
            pass
        return None

    def _parse_protobuf_eq(self, msg) -> list[EventEnvelope] | None:
        eq = msg.earthquake_data
        origin_time = self._parse_datetime(eq.origin_time_iso) if eq.origin_time_iso else None
        if origin_time is None and eq.origin_time_ms:
            origin_time = datetime.utcfromtimestamp(eq.origin_time_ms / 1000.0)

        event = EewEvent(
            source_id=self.source_id,
            event_id=str(eq.id) or str(eq.code) or str(msg.id),
            occurred_at=origin_time,
            latitude=to_float(eq.latitude),
            longitude=to_float(eq.longitude),
            depth=to_float(eq.depth),
            magnitude=to_float(eq.magnitude),
            place_name=str(eq.region or eq.location or ""),
            max_intensity=str(eq.mmi or ""),
            raw={"protobuf": True, "id": eq.id, "code": eq.code},
        )

        identity = EventIdentity(
            event_id=str(eq.id),
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
            max_intensity=str(data.get("mmi", "") or ""),
            raw=data,
        )

        identity = EventIdentity(
            event_id=to_str(data.get("id")) or "",
            source_id=self.source_id,
            event_type="eew",
            provider_family="global_quake",
        )

        return [EventEnvelope(identity=identity, event=event, payload=None)]
