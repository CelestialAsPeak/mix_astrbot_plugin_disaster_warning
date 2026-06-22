"""
CMA 台风解析器 — 中国气象局 typhoon.weather.com.cn。

数据源: cma_typhoon
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
    from ...utils.time import parse_ts, parse_cst_time

except ImportError:
    from parser.base import BaseParser
    from parser.registry import ParserRegistry
    from utils.convert import to_float, to_int, to_str
    from utils.time import parse_ts, parse_cst_time

@ParserRegistry.register("cma_typhoon")
class CmaTyphoonParser(BaseParser):
    """CMA 台风解析器。"""

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
        code = to_str(data.get("code", data.get("id", ""))) or ""
        if not code:
            return None

        name_cn = str(data.get("name", data.get("nameCn", "")) or "")
        name_en = str(data.get("nameEn", data.get("enName", "")) or "")
        points = data.get("track", data.get("points", data.get("path", [])))
        track = []

        if isinstance(points, list):
            for pt in points:
                if isinstance(pt, dict):
                    tp = TyphoonTrackPoint(
                        timestamp=parse_cst_time(pt.get("time", pt.get("timestamp", ""))),
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
            event_id=code,
            name_cn=name_cn,
            name_en=name_en,
            code=code,
            category=to_int(data.get("grade", data.get("category"))) or 0,
            pressure=to_float(data.get("pressure")),
            wind_speed=to_float(data.get("wind", data.get("windSpeed"))),
            latitude=to_float(data.get("lat", data.get("latitude"))),
            longitude=to_float(data.get("lon", data.get("longitude"))),
            last_updated=parse_ts(data.get("updateTime", data.get("lastUpdated", ""))),
            track_points=tuple(track),
            is_active=not bool(data.get("isStop", data.get("stopped", False))),
            raw=data,
        )

        identity = EventIdentity(
            event_id=code,
            source_id=self.source_id,
            event_type="typhoon",
            provider_family="direct_http",
        )

        return EventEnvelope(identity=identity, event=event)
