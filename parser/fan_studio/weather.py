"""
FAN Studio — 气象预警解析器。

数据源: china_weather_fanstudio
字段: title, description, type, level, effectiveTime, latitude, longitude
"""

from __future__ import annotations

try:
    from ...domain.models import WeatherEvent, EventEnvelope, EventIdentity, SourcePayload
except ImportError:
    from domain.models import WeatherEvent, EventEnvelope, EventIdentity, SourcePayload

try:
    from ..base import BaseParser
    from ..registry import ParserRegistry
    from ...utils.convert import to_float, to_str

except ImportError:
    from parser.base import BaseParser
    from parser.registry import ParserRegistry
    from utils.convert import to_float, to_str

@ParserRegistry.register("china_weather_fanstudio")
class WeatherParser(BaseParser):
    """中国气象局气象预警解析器。"""

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None
        if self._is_heartbeat(raw):
            return []

        event_id = to_str(raw.get("id")) or to_str(raw.get("eventId")) or ""
        if not event_id:
            return None

        effective_time = self._parse_datetime_cst(raw.get("effectiveTime", raw.get("time", "")))

        event = WeatherEvent(
            source_id=self.source_id,
            event_id=event_id,
            alert_type=to_str(raw.get("type")),
            alert_level=to_str(raw.get("level")),
            alert_title=str(raw.get("title", "") or ""),
            headline=str(raw.get("headline", raw.get("description", "")) or ""),
            description=str(raw.get("description", "") or ""),
            effective_time=effective_time,
            latitude=to_float(raw.get("latitude")),
            longitude=to_float(raw.get("longitude")),
            raw=raw,
        )

        identity = EventIdentity(
            event_id=event_id,
            source_id=self.source_id,
            event_type="weather",
            provider_family="fan_studio",
        )

        return [EventEnvelope(
            identity=identity,
            event=event,
            payload=SourcePayload(source_id=self.source_id, provider_family="fan_studio", raw=raw),
        )]
