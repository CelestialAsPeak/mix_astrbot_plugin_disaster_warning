"""
FAN Studio — 日本气象厅 (JMA) 解析器。

数据源: jma_fanstudio (EEW), jma_report_fanstudio (地震报告)
"""

from __future__ import annotations

try:
    from ...domain.models import EewEvent, EarthquakeReport, EventEnvelope, EventIdentity, SourcePayload
except ImportError:
    from domain.models import EewEvent, EarthquakeReport, EventEnvelope, EventIdentity, SourcePayload

try:
    from ..base import BaseParser
    from ..registry import ParserRegistry
    from ...utils.convert import to_float, to_int, to_str

except ImportError:
    from parser.base import BaseParser
    from parser.registry import ParserRegistry
    from utils.convert import to_float, to_int, to_str

@ParserRegistry.register("jma_fanstudio")
class JMAEEWParser(BaseParser):
    """日本气象厅 (JMA) EEW 解析器。"""

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
        is_final = bool(raw.get("isFinal", False))

        event = EewEvent(
            source_id=self.source_id,
            event_id=event_id,
            occurred_at=occurred_at,
            latitude=to_float(raw.get("latitude")),
            longitude=to_float(raw.get("longitude")),
            depth=to_float(raw.get("depth")),
            magnitude=to_float(raw.get("magnitude")),
            magnitude_type=to_str(raw.get("magnitudeType")),
            place_name=str(raw.get("placeName", "") or ""),
            max_intensity=str(raw.get("intensity", raw.get("maxIntensity", "")) or ""),
            serial=report_num,
            is_final=is_final,
            is_warn=bool(raw.get("isWarn", False)),
            is_sea=raw.get("isSea"),
            report_num=report_num,
            accuracy=raw.get("accuracy"),
            raw=raw,
        )

        identity = EventIdentity(
            event_id=event_id,
            source_id=self.source_id,
            event_type="eew",
            provider_family="fan_studio",
            report_num=report_num,
            is_final=is_final,
        )

        return [EventEnvelope(
            identity=identity,
            event=event,
            payload=SourcePayload(source_id=self.source_id, provider_family="fan_studio", raw=raw),
        )]


@ParserRegistry.register("jma_report_fanstudio")
class JMAReportParser(BaseParser):
    """日本气象厅 (JMA) 地震报告解析器。"""

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None
        event_id = to_str(raw.get("eventId")) or to_str(raw.get("id")) or ""
        if not event_id:
            return None

        occurred_at = self._parse_datetime(raw.get("originTime") or raw.get("time") or raw.get("shockTime") or "")

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
            provider_family="fan_studio",
        )

        return [EventEnvelope(
            identity=identity,
            event=event,
            payload=SourcePayload(source_id=self.source_id, provider_family="fan_studio", raw=raw),
        )]
