"""
Wolfx — JMA 地震情报报告解析器（HTTP 轮询）。

数据源: jma_wolfx_info
字段: id, time, latitude, longitude, depth, magnitude, placeName, region
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

@ParserRegistry.register("jma_wolfx_info")
class WolfxJmaReportParser(BaseParser):
    """Wolfx JMA 地震情报报告解析器。"""

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None

        event_id = to_str(raw.get("id")) or ""
        if not event_id:
            return None

        occurred_at = self._parse_datetime(raw.get("time", raw.get("occurred_at", "")))

        event = EarthquakeReport(
            source_id=self.source_id,
            event_id=event_id,
            occurred_at=occurred_at,
            latitude=to_float(raw.get("latitude")),
            longitude=to_float(raw.get("longitude")),
            depth=to_float(raw.get("depth")),
            magnitude=to_float(raw.get("magnitude")),
            magnitude_type=to_str(raw.get("magnitudeType")),
            place_name=str(raw.get("placeName", "") or ""),
            region=str(raw.get("region", "") or ""),
            raw=raw,
        )

        identity = EventIdentity(
            event_id=event_id,
            source_id=self.source_id,
            event_type="earthquake",
            provider_family="wolfx",
        )

        return [EventEnvelope(
            identity=identity,
            event=event,
            payload=SourcePayload(source_id=self.source_id, provider_family="wolfx", raw=raw),
        )]
