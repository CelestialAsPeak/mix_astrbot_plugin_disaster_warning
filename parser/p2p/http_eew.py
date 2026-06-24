"""
P2P — EEW HTTP 轮询解析器（备用）。

数据源: jma_p2p_http
API: https://api.p2pquake.net/v2/history?codes=556&limit=1
格式: [item1, ...]

P2P WebSocket 失能时作为 HTTP 轮询备用。
"""

from __future__ import annotations

try:
    from ...domain.models import EewEvent, EventEnvelope, EventIdentity, SourcePayload
except ImportError:
    from domain.models import EewEvent, EventEnvelope, EventIdentity, SourcePayload

try:
    from ..base import BaseParser
    from ..registry import ParserRegistry
    from ...utils.convert import to_float, to_int, to_str

except ImportError:
    from parser.base import BaseParser
    from parser.registry import ParserRegistry
    from utils.convert import to_float, to_int, to_str


@ParserRegistry.register("jma_p2p_http")
class P2pJmaEewHttpParser(BaseParser):
    """P2P JMA EEW HTTP 解析器（code 556 列表）。"""

    def parse_message(self, message: str | bytes | dict | list) -> list[EventEnvelope] | None:
        payload = self.decode_message(message)
        if payload is None:
            return None
        items = payload if isinstance(payload, list) else [payload]
        envelopes = []
        for item in items:
            if not isinstance(item, dict):
                continue
            result = self.parse(item)
            if result:
                envelopes.extend(result)
        return envelopes if envelopes else None

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None
        if raw.get("code") != 556:
            return None

        earthquake = raw.get("earthquake", {})
        if not isinstance(earthquake, dict):
            return None

        event_id = to_str(earthquake.get("id")) or to_str(raw.get("id")) or ""
        if not event_id:
            return None

        occurred_at = self._parse_datetime(earthquake.get("time", raw.get("time", "")))
        serial = to_int(raw.get("serial")) or 0

        event = EewEvent(
            source_id=self.source_id,
            event_id=event_id,
            occurred_at=occurred_at,
            latitude=to_float(earthquake.get("latitude")),
            longitude=to_float(earthquake.get("longitude")),
            depth=to_float(earthquake.get("depth")),
            magnitude=to_float(earthquake.get("magnitude")),
            place_name=str(earthquake.get("placeName", "") or ""),
            max_intensity=str(raw.get("maxIntensity", earthquake.get("maxScale", "")) or ""),
            serial=serial,
            is_final=bool(raw.get("isFinal", False)),
            is_warn=bool(raw.get("isWarn", False)),
            is_sea=earthquake.get("isSea"),
            report_num=serial,
            announced_time=self._parse_datetime(raw.get("announcedTime", "")),
            warn_areas=raw.get("warnAreas"),
            raw=raw,
        )

        identity = EventIdentity(
            event_id=event_id,
            source_id=self.source_id,
            event_type="eew",
            provider_family="p2p",
            report_num=serial,
            is_final=bool(raw.get("isFinal", False)),
            published_at=occurred_at,
        )

        return [EventEnvelope(
            identity=identity, event=event,
            payload=SourcePayload(source_id=self.source_id, provider_family="p2p", raw=raw),
        )]
