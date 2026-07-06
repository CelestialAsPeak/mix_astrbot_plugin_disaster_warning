"""
Wolfx — JMA 地震情报 HTTP 轮询解析器（eqlist 格式备用）。

数据源: jma_wolfx_info_http
API: https://api.wolfx.jp/jma_eqlist.json
格式: {"No1": {"EventID": "...", "time": "...", ...}, "No2": {...}, ...}

当 Wolfx WS 失能时作为 HTTP 轮询备用。
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

try:
    from ...utils.time import parse_ts
except ImportError:
    from utils.time import parse_ts


@ParserRegistry.register("jma_wolfx_info_http")
class WolfxJmaInfoHttpParser(BaseParser):
    """Wolfx JMA 地震情报 HTTP 解析器（eqlist 格式）。"""

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None

        envelopes = []
        for key, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            event_id = to_str(entry.get("EventID")) or key
            mag = to_float(entry.get("magnitude"))
            if mag is None:
                continue

            occurred_at = self._parse_datetime(entry.get("time", ""))

            event = EarthquakeReport(
                source_id=self.source_id,
                event_id=event_id,
                occurred_at=occurred_at,
                latitude=to_float(entry.get("latitude")),
                longitude=to_float(entry.get("longitude")),
                depth=self._parse_depth(entry.get("depth")),
                magnitude=mag,
                place_name=to_str(entry.get("location")),
                region=to_str(entry.get("region")),
                raw=entry,
            )

            identity = EventIdentity(
                event_id=event_id,
                source_id=self.source_id,
                event_type="earthquake",
                provider_family="wolfx",
                published_at=occurred_at,
            )

            envelopes.append(EventEnvelope(
                identity=identity,
                event=event,
                payload=SourcePayload(source_id=self.source_id, provider_family="wolfx", raw=entry),
            ))

        return envelopes if envelopes else None

    @staticmethod
    def _parse_depth(depth_str: object) -> float | None:
        """解析深度，兼容 "30km" 后缀格式。"""
        if depth_str is None:
            return None
        s = str(depth_str).strip().rstrip("kmKM")
        try:
            return float(s)
        except (ValueError, TypeError):
            return None
