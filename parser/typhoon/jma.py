"""
JMA 台风解析器 — 日本气象厅 www.jma.go.jp。

数据源: jma_typhoon
"""

from __future__ import annotations

try:
    from ...domain.models import TyphoonTrackPoint, TyphoonEvent, EventEnvelope, EventIdentity
except ImportError:
    from domain.models import TyphoonTrackPoint, TyphoonEvent, EventEnvelope, EventIdentity

try:
    from ..base import BaseParser
    from ..registry import ParserRegistry
    from ...utils.convert import to_float, to_int, to_str
    from ...utils.time import parse_ts, parse_jst_time

except ImportError:
    from parser.base import BaseParser
    from parser.registry import ParserRegistry
    from utils.convert import to_float, to_int, to_str
    from utils.time import parse_ts, parse_jst_time

@ParserRegistry.register("jma_typhoon")
class JmaTyphoonParser(BaseParser):
    """JMA 台风解析器。"""

    def parse(self, raw: any) -> list[EventEnvelope] | None:
        if isinstance(raw, list):
            results = []
            for item in raw:
                r = self._parse_one(item)
                if r:
                    results.append(r)
            return results if results else None
        if isinstance(raw, dict):
            r = self._parse_one(raw)
            return [r] if r else None
        return None

    def _parse_one(self, data: dict) -> EventEnvelope | None:
        tc_id = to_str(data.get("tropicalCyclone", data.get("id", ""))) or ""
        if not tc_id:
            return None

        name_en = str(data.get("name", data.get("nameEn", "")) or "")
        name_cn = str(data.get("nameCn", name_en)) or ""

        # JMA has preTyphoon + typhoon track segments
        pre = data.get("preTyphoon", [])
        typh = data.get("typhoon", [])
        track = []

        for segment in (pre, typh):
            if isinstance(segment, list):
                for pt in segment:
                    if isinstance(pt, dict):
                        tp = TyphoonTrackPoint(
                            timestamp=parse_jst_time(pt.get("time", "")),
                            latitude=to_float(pt.get("lat", pt.get("latitude"))) or 0.0,
                            longitude=to_float(pt.get("lon", pt.get("longitude"))) or 0.0,
                            pressure=to_float(pt.get("pressure")),
                            wind_speed=to_float(pt.get("wind", pt.get("windSpeed"))),
                            category=to_int(pt.get("grade", pt.get("category"))) or 0,
                            wind_radii_7=pt.get("windRadius7", pt.get("wind_radii_7")),
                            wind_radii_10=pt.get("windRadius10", pt.get("wind_radii_10")),
                            is_forecast=bool(pt.get("forecast", pt.get("is_forecast", False))),
                        )
                        track.append(tp)

        event = TyphoonEvent(
            source_id=self.source_id,
            event_id=tc_id,
            name_cn=name_cn,
            name_en=name_en,
            code=tc_id,
            track_points=tuple(track),
            is_active=True,
            raw=data,
        )

        identity = EventIdentity(
            event_id=tc_id,
            source_id=self.source_id,
            event_type="typhoon",
            provider_family="direct_http",
        )

        return EventEnvelope(identity=identity, event=event)
