"""
P2P — 海啸预报解析器。

数据源: jma_tsunami_p2p
P2P code: 552 (津波予報)
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

@ParserRegistry.register("jma_tsunami_p2p")
class P2pTsunamiParser(BaseParser):
    """P2P 海啸预报解析器 (code 552)。"""

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None

        code = raw.get("code")
        if code != 552:
            return None

        event_id = to_str(raw.get("id")) or ""
        if not event_id:
            return None

        timestamp = self._parse_datetime(raw.get("time", raw.get("issueTime", "")))
        level = to_int(raw.get("level")) or 0
        title = str(raw.get("title", "") or "")

        event = TsunamiEvent(
            source_id=self.source_id,
            event_id=event_id,
            timestamp=timestamp,
            level=level,
            title=title,
            source_name=str(raw.get("sourceName", "") or ""),
            condition=to_str(raw.get("condition")),
            class_name=to_str(raw.get("class")),
            areas=raw.get("areas"),
            raw=raw,
        )

        identity = EventIdentity(
            event_id=event_id,
            source_id=self.source_id,
            event_type="tsunami",
            provider_family="p2p",
        )

        return [EventEnvelope(
            identity=identity,
            event=event,
            payload=SourcePayload(source_id=self.source_id, provider_family="p2p", raw=raw),
        )]
