"""
FAN Studio — 通用 EEW 解析器。

数据源: sa_fanstudio (ShakeAlert), kma_eew_fanstudio (KMA EEW)
字段: eventId, magnitude, latitude, longitude, depth, placeName, originTime
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

class FanEewParser(BaseParser):
    """FAN Studio 通用 EEW 解析器（ShakeAlert / KMA EEW）。"""

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None
        if self._is_heartbeat(raw):
            return []

        event_id = to_str(raw.get("eventId")) or to_str(raw.get("id")) or ""
        if not event_id:
            return None

        occurred_at = self._parse_datetime(raw.get("originTime", raw.get("shockTime", "")))
        report_num = to_int(raw.get("reportNum", raw.get("updates", 1))) or 1

        event = EewEvent(
            source_id=self.source_id,
            event_id=event_id,
            occurred_at=occurred_at,
            latitude=to_float(raw.get("latitude")),
            longitude=to_float(raw.get("longitude")),
            depth=to_float(raw.get("depth")),
            magnitude=to_float(raw.get("magnitude")),
            place_name=str(raw.get("placeName", "") or ""),
            max_intensity=str(raw.get("maxIntensity", raw.get("intensity", "")) or ""),
            serial=report_num,
            is_final=bool(raw.get("isFinal", False)),
            report_num=report_num,
            raw=raw,
        )

        identity = EventIdentity(
            event_id=event_id,
            source_id=self.source_id,
            event_type="eew",
            provider_family="fan_studio",
            report_num=report_num,
            is_final=bool(raw.get("isFinal", False)),
        )

        return [EventEnvelope(
            identity=identity,
            event=event,
            payload=SourcePayload(source_id=self.source_id, provider_family="fan_studio", raw=raw),
        )]


@ParserRegistry.register("sa_fanstudio")
class ShakeAlertParser(FanEewParser):
    """ShakeAlert 地震预警 (美国西海岸)。"""
    pass


@ParserRegistry.register("kma_eew_fanstudio")
class KmaEewParser(FanEewParser):
    """韩国气象厅 (KMA) EEW。"""
    pass
