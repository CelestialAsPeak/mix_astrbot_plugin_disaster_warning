"""
P2P — EEW 解析器。

数据源: jma_p2p
P2P code: 556 (緊急地震速報)
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

@ParserRegistry.register("jma_p2p")
class P2pJmaEewParser(BaseParser):
    """P2P 日本气象厅 EEW 解析器 (code 556)。"""

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None

        code = raw.get("code")
        if code != 556:
            return None

        earthquake = raw.get("earthquake", {})
        if not isinstance(earthquake, dict):
            return None

        event_id = to_str(earthquake.get("id")) or to_str(raw.get("id")) or ""
        if not event_id:
            return None

        occurred_at = self._parse_datetime(earthquake.get("time", raw.get("time", "")))

        # P2P report_num 从串行号提取
        serial = to_int(raw.get("serial")) or 0

        event = EewEvent(
            source_id=self.source_id,
            event_id=event_id,
            occurred_at=occurred_at,
            latitude=to_float(earthquake.get("latitude")),
            longitude=to_float(earthquake.get("longitude")),
            depth=to_float(earthquake.get("depth")),
            magnitude=to_float(earthquake.get("magnitude")),
            place_name=str(earthquake.get("placeName", "") or ""),
            max_intensity=str(raw.get("maxIntensity", earthquake.get("maxScale", "")) or ""),
            serial=serial,
            is_final=bool(raw.get("isFinal", False)),
            is_warn=bool(raw.get("isWarn", False)),
            is_sea=earthquake.get("isSea"),
            report_num=serial,
            announced_time=self._parse_datetime(raw.get("announcedTime", "")),
            warn_areas=raw.get("warnAreas"),
            raw=raw,
        )

        identity = EventIdentity(
            event_id=event_id,
            source_id=self.source_id,
            event_type="eew",
            provider_family="p2p",
            report_num=serial,
            is_final=bool(raw.get("isFinal", False)),
        )

        return [EventEnvelope(
            identity=identity,
            event=event,
            payload=SourcePayload(source_id=self.source_id, provider_family="p2p", raw=raw),
        )]
