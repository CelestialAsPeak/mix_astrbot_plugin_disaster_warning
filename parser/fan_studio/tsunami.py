"""
FAN Studio — 海啸预警解析器。

数据源: china_tsunami_fanstudio
字段: warningInfo, code, timeInfo, level, areas, title
"""

from __future__ import annotations

try:
    from ...domain.models import TsunamiEvent, EventEnvelope, EventIdentity, SourcePayload
except ImportError:
    from domain.models import TsunamiEvent, EventEnvelope, EventIdentity, SourcePayload

try:
    from ..base import BaseParser
    from ..registry import ParserRegistry
    from ...utils.convert import to_int, to_str

except ImportError:
    from parser.base import BaseParser
    from parser.registry import ParserRegistry
    from utils.convert import to_int, to_str

@ParserRegistry.register("china_tsunami_fanstudio")
class TsunamiParser(BaseParser):
    """自然资源部海啸预警中心解析器。"""

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None
        if self._is_heartbeat(raw):
            return []

        event_id = to_str(raw.get("code")) or to_str(raw.get("eventId")) or ""
        if not event_id:
            return None

        time_info = raw.get("timeInfo", raw.get("timestamp", ""))
        if isinstance(time_info, dict):
            time_str = str(time_info.get("alarmDate", time_info.get("updateDate", "")))
        else:
            time_str = str(time_info) if time_info else ""
        timestamp = self._parse_datetime_cst(time_str)
        level = to_int(raw.get("level")) or 0

        event = TsunamiEvent(
            source_id=self.source_id,
            event_id=event_id,
            timestamp=timestamp,
            level=level,
            title=str(raw.get("warningInfo", raw.get("title", "")) or ""),
            source_name=str(raw.get("sourceName", "") or ""),
            areas=raw.get("areas"),
            shock_info=raw.get("shockInfo"),
            raw=raw,
        )

        identity = EventIdentity(
            event_id=event_id,
            source_id=self.source_id,
            event_type="tsunami",
            provider_family="fan_studio",
        )

        return [EventEnvelope(
            identity=identity,
            event=event,
            payload=SourcePayload(source_id=self.source_id, provider_family="fan_studio", raw=raw),
        )]
