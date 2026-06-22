"""
FAN Studio — 中国地震台网 (CENC) 地震报告解析器。

数据源: cenc_fanstudio
字段: eventId, magnitude, placeName, depth, latitude, longitude, occurred_at
"""

from __future__ import annotations

try:
    from ...domain.models import EarthquakeReport, EventEnvelope, EventIdentity, SourcePayload
except ImportError:
    from domain.models import EarthquakeReport, EventEnvelope, EventIdentity, SourcePayload

try:
    from ..base import BaseParser
    from ..registry import ParserRegistry
    from ...utils.convert import to_float, to_str

except ImportError:
    from parser.base import BaseParser
    from parser.registry import ParserRegistry
    from utils.convert import to_float, to_str

@ParserRegistry.register("cenc_fanstudio")
class CencReportParser(BaseParser):
    """中国地震台网 (CENC) 地震报告解析器。"""

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None
        if self._is_heartbeat(raw):
            return []

        event_id = to_str(raw.get("eventId")) or to_str(raw.get("id")) or ""
        if not event_id:
            return None

        occurred_at = self._parse_datetime(raw.get("occurred_at") or raw.get("time") or raw.get("shockTime") or "")
        place_name = str(raw.get("placeName", raw.get("location", "")) or "")
        magnitude = to_float(raw.get("magnitude"))

        event = EarthquakeReport(
            source_id=self.source_id,
            event_id=event_id,
            occurred_at=occurred_at,
            latitude=to_float(raw.get("latitude")),
            longitude=to_float(raw.get("longitude")),
            depth=to_float(raw.get("depth")),
            magnitude=magnitude,
            magnitude_type=to_str(raw.get("magnitudeType")),
            place_name=place_name,
            region=to_str(raw.get("region")),
            raw=raw,
        )

        identity = EventIdentity(
            event_id=event_id,
            source_id=self.source_id,
            event_type="earthquake",
            provider_family="fan_studio",
        )

        return [EventEnvelope(
            identity=identity,
            event=event,
            payload=SourcePayload(source_id=self.source_id, provider_family="fan_studio", raw=raw),
        )]
