"""
FAN Studio — 全球地震报告解析器。

共享相同数据结构的多个数据源:
  usgs_fanstudio, emsc_fanstudio, hko_fanstudio, gfz_fanstudio,
  usp_fanstudio, bcsf_fanstudio, fssn_fanstudio, kma_fanstudio

字段: id, magnitude, placeName, depth, latitude, longitude, time, url, region
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

class FanEarthquakeParser(BaseParser):
    """FAN Studio 通用地震报告解析器基类。"""

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None
        if self._is_heartbeat(raw):
            return []

        event_id = to_str(raw.get("eventId")) or to_str(raw.get("id")) or ""
        if not event_id:
            return None

        occurred_at = self._parse_datetime_cst(
            raw.get("time") or raw.get("occurred_at") or raw.get("originTime") or raw.get("shockTime") or ""
        )
        place_name = str(raw.get("placeName", raw.get("region", "")) or "")
        url = to_str(raw.get("url"))

        event = EarthquakeReport(
            source_id=self.source_id,
            event_id=event_id,
            occurred_at=occurred_at,
            latitude=to_float(raw.get("latitude")),
            longitude=to_float(raw.get("longitude")),
            depth=to_float(raw.get("depth")),
            magnitude=to_float(raw.get("magnitude")),
            magnitude_type=to_str(raw.get("magnitudeType")),
            place_name=place_name,
            region=to_str(raw.get("region")),
            url=url,
            mmi=to_float(raw.get("mmi")),
            status=to_str(raw.get("status")),
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


# ── 注册所有全球地震源 ──
@ParserRegistry.register("usgs_fanstudio")
class UsgsParser(FanEarthquakeParser):
    """美国地质调查局 (USGS)。"""
    pass


@ParserRegistry.register("emsc_fanstudio")
class EmscParser(FanEarthquakeParser):
    """欧洲地中海地震中心 (EMSC)。"""
    pass


@ParserRegistry.register("hko_fanstudio")
class HkoParser(FanEarthquakeParser):
    """香港天文台 (HKO)。"""
    pass


@ParserRegistry.register("gfz_fanstudio")
class GfzParser(FanEarthquakeParser):
    """德国地学研究中心 (GFZ)。"""
    pass


@ParserRegistry.register("usp_fanstudio")
class UspParser(FanEarthquakeParser):
    """巴西圣保罗大学 (USP)。"""
    pass


@ParserRegistry.register("bcsf_fanstudio")
class BcsfParser(FanEarthquakeParser):
    """法国中央地震研究所 (BCSF)。"""
    pass


@ParserRegistry.register("fssn_fanstudio")
class FssnParser(FanEarthquakeParser):
    """FSSN 地震速报。"""
    pass


@ParserRegistry.register("kma_fanstudio")
class KmaParser(FanEarthquakeParser):
    """韩国气象厅 (KMA)。"""
    pass
