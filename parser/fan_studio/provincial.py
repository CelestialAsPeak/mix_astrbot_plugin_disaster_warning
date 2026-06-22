"""
FAN Studio — 省级地震台网解析器。

数据源: beijing_fanstudio, guangxi_fanstudio, ningxia_fanstudio,
       shanxi_fanstudio, yunnan_fanstudio

字段: id, magnitude, placeName, depth, latitude, longitude, time
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

class ProvincialReportParser(BaseParser):
    """省级地震台网通用地震报告解析器。"""

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None

        event_id = to_str(raw.get("id")) or ""
        if not event_id:
            return None

        occurred_at = self._parse_datetime(raw.get("time") or raw.get("occurred_at") or raw.get("shockTime") or "")

        event = EarthquakeReport(
            source_id=self.source_id,
            event_id=event_id,
            occurred_at=occurred_at,
            latitude=to_float(raw.get("latitude")),
            longitude=to_float(raw.get("longitude")),
            depth=to_float(raw.get("depth")),
            magnitude=to_float(raw.get("magnitude")),
            place_name=str(raw.get("placeName", "") or ""),
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


# ── 注册各省台网 ──
@ParserRegistry.register("beijing_fanstudio")
class BeijingParser(ProvincialReportParser):
    pass


@ParserRegistry.register("guangxi_fanstudio")
class GuangxiParser(ProvincialReportParser):
    pass


@ParserRegistry.register("ningxia_fanstudio")
class NingxiaParser(ProvincialReportParser):
    pass


@ParserRegistry.register("shanxi_fanstudio")
class ShanxiParser(ProvincialReportParser):
    pass


@ParserRegistry.register("yunnan_fanstudio")
class YunnanParser(ProvincialReportParser):
    pass
