"""
Wolfx — EEW 解析器。

数据源: jma_wolfx, cenc_wolfx, cwa_wolfx
格式: Wolfx WebSocket JSON，通过 source 字段区分子源
字段: id, shockTime, latitude, longitude, magnitude, depth, placeName,
      epiIntensity, createTime, final, cancel
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

class WolfxEewParser(BaseParser):
    """Wolfx EEW 通用解析器。"""

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None

        event_id = to_str(raw.get("id")) or ""
        if not event_id:
            return None

        occurred_at = self._parse_datetime(raw.get("shockTime", ""))
        report_num = to_int(raw.get("updates", 1)) or 1

        event = EewEvent(
            source_id=self.source_id,
            event_id=event_id,
            occurred_at=occurred_at,
            latitude=to_float(raw.get("latitude")),
            longitude=to_float(raw.get("longitude")),
            depth=to_float(raw.get("depth")),
            magnitude=to_float(raw.get("magnitude")),
            place_name=str(raw.get("placeName", "") or ""),
            max_intensity=str(raw.get("epiIntensity", "") or ""),
            serial=report_num,
            is_final=bool(raw.get("final", False)),
            is_cancel=bool(raw.get("cancel", False)),
            report_num=report_num,
            announced_time=self._parse_datetime(raw.get("createTime", "")),
            raw=raw,
        )

        identity = EventIdentity(
            event_id=event_id,
            source_id=self.source_id,
            event_type="eew",
            provider_family="wolfx",
            report_num=report_num,
            is_final=bool(raw.get("final", False)),
            published_at=self._parse_datetime(raw.get("createTime", "")),
        )

        return [EventEnvelope(
            identity=identity,
            event=event,
            payload=SourcePayload(source_id=self.source_id, provider_family="wolfx", raw=raw),
        )]


@ParserRegistry.register("jma_wolfx")
class WolfxJmaEewParser(WolfxEewParser):
    """日本气象厅 (JMA) EEW via Wolfx。"""
    pass


@ParserRegistry.register("cenc_wolfx")
class WolfxCencEewParser(WolfxEewParser):
    """中国地震预警网 (CENC) EEW via Wolfx。"""
    pass


@ParserRegistry.register("cwa_wolfx")
class WolfxCwaEewParser(WolfxEewParser):
    """台湾中央气象署 (CWA) EEW via Wolfx。"""
    pass
