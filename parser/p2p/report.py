"""
P2P — 地震情报解析器。

数据源: jma_p2p_info
P2P code: 551 (地震情報)
"""

from __future__ import annotations

try:
    from ...domain.models import EarthquakeReport, EventEnvelope, EventIdentity, SourcePayload
except ImportError:
    from domain.models import EarthquakeReport, EventEnvelope, EventIdentity, SourcePayload

try:
    from ..base import BaseParser
    from ..registry import ParserRegistry
    from ...utils.convert import to_float, to_int, to_str

except ImportError:
    from parser.base import BaseParser
    from parser.registry import ParserRegistry
    from utils.convert import to_float, to_int, to_str

@ParserRegistry.register("jma_p2p_info")
class P2pJmaReportParser(BaseParser):
    """P2P 日本气象厅地震情报解析器 (code 551)。"""

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None

        code = raw.get("code")
        if code != 551:
            return None

        earthquake = raw.get("earthquake", {})
        if not isinstance(earthquake, dict):
            return None

        event_id = to_str(earthquake.get("id")) or to_str(raw.get("id")) or ""
        if not event_id:
            return None

        occurred_at = self._parse_datetime(earthquake.get("time", ""))
        intensity_points = raw.get("points") or raw.get("intensityPoints")

        event = EarthquakeReport(
            source_id=self.source_id,
            event_id=event_id,
            occurred_at=occurred_at,
            latitude=to_float(earthquake.get("latitude")),
            longitude=to_float(earthquake.get("longitude")),
            depth=to_float(earthquake.get("depth")),
            magnitude=to_float(earthquake.get("magnitude")),
            place_name=str(earthquake.get("placeName", "") or ""),
            region=str(raw.get("regionName", "") or ""),
            intensity_points=intensity_points,
            raw=raw,
        )

        identity = EventIdentity(
            event_id=event_id,
            source_id=self.source_id,
            event_type="earthquake",
            provider_family="p2p",
        )

        return [EventEnvelope(
            identity=identity,
            event=event,
            payload=SourcePayload(source_id=self.source_id, provider_family="p2p", raw=raw),
        )]
