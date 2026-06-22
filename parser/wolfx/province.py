"""
Wolfx — 省级 EEW 解析器（四川/福建/重庆）。

数据源: sc_wolfx_eew, fj_wolfx_eew, cq_wolfx_eew
Wolfx type: sc_eew, fj_eew, cq_eew
字段: EventID, OriginTime, Latitude, Longitude, Depth, Magunitude/Magnitude,
      HypoCenter, MaxIntensity, ReportNum, isFinal
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

class WolfxProvinceEewParser(BaseParser):
    """Wolfx 省级地震预警通用解析器（四川/福建/重庆）。"""

    _TYPE_MAP = {
        "sc_wolfx_eew": "sc_eew",
        "fj_wolfx_eew": "fj_eew",
        "cq_wolfx_eew": "cq_eew",
    }

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None

        expected_type = self._TYPE_MAP.get(self.source_id)
        if raw.get("type") != expected_type:
            return None

        event_id = to_str(raw.get("EventID")) or ""
        if not event_id:
            return None

        occurred_at = self._parse_datetime(raw.get("OriginTime", ""))
        report_num = to_int(raw.get("ReportNum", 1)) or 1

        # 兼容 Magunitude（川/闽）和 Magnitude（渝）两种字段名
        mag = to_float(raw.get("Magunitude")) or to_float(raw.get("Magnitude")) or to_float(raw.get("magnitude"))

        event = EewEvent(
            source_id=self.source_id,
            event_id=event_id,
            occurred_at=occurred_at,
            latitude=to_float(raw.get("Latitude")),
            longitude=to_float(raw.get("Longitude")),
            depth=to_float(raw.get("Depth")),
            magnitude=mag,
            place_name=str(raw.get("HypoCenter", "") or ""),
            max_intensity=str(raw.get("MaxIntensity", "") or ""),
            serial=report_num,
            is_final=bool(raw.get("isFinal", False)),
            report_num=report_num,
            raw=raw,
        )

        identity = EventIdentity(
            event_id=event_id,
            source_id=self.source_id,
            event_type="eew",
            provider_family="wolfx",
            report_num=report_num,
            is_final=bool(raw.get("isFinal", False)),
        )

        return [EventEnvelope(
            identity=identity,
            event=event,
            payload=SourcePayload(source_id=self.source_id, provider_family="wolfx", raw=raw),
        )]


@ParserRegistry.register("sc_wolfx_eew")
class SichuanProvinceParser(WolfxProvinceEewParser):
    """四川省地震预警。"""
    pass


@ParserRegistry.register("fj_wolfx_eew")
class FujianProvinceParser(WolfxProvinceEewParser):
    """福建省地震预警。"""
    pass


@ParserRegistry.register("cq_wolfx_eew")
class ChongqingProvinceParser(WolfxProvinceEewParser):
    """重庆市地震预警。"""
    pass
