"""
Wolfx — EEW HTTP 轮询解析器（备用）。

数据源: jma_wolfx_http
API: https://api.wolfx.jp/jma_eew.json
格式: 单条 JSON，PascalCase 字段（与 WS 格式不同！）

Wolfx HTTP EEW endpoint 返回 PascalCase JSON:
{
  "Title": "緊急地震速報（予報）",
  "CodeType": "...",
  "Issue": {"Source": "東京", "Status": "通常"},
  "EventID": "20260624133147",
  "Serial": 4,
  "AnnouncedTime": "2026/06/24 13:32:27",
  "OriginTime": "2026/06/24 13:31:43",
  "Hypocenter": "福島県会津",
  "Latitude": 37.0,
  "Longitude": 139.4,
  "Magunitude": 3.5,        ← API typo，实际字段名
  "Depth": 10,
  "MaxIntensity": "2",
  "Accuracy": {...},
  "WarnArea": [],
  "isSea": false,
  "isTraining": false,
  "isAssumption": false,
  "isFinal": true,
  "isCancel": false,
}
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


@ParserRegistry.register("jma_wolfx_http")
class WolfxJmaEewHttpParser(BaseParser):
    """Wolfx JMA EEW HTTP 解析器（PascalCase 格式）。"""

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None

        event_id = to_str(raw.get("EventID")) or to_str(raw.get("id")) or ""
        if not event_id:
            return None

        occurred_at = self._parse_datetime(raw.get("OriginTime", raw.get("shockTime", "")))
        report_num = to_int(raw.get("Serial", 1)) or 1

        # "Magunitude" 是 Wolfx API 的原始字段名（typo）
        mag = to_float(raw.get("Magunitude")) or to_float(raw.get("magnitude"))
        max_int = str(raw.get("MaxIntensity", "") or "")
        warn_areas = raw.get("WarnArea", raw.get("warnAreas"))

        event = EewEvent(
            source_id=self.source_id,
            event_id=event_id,
            occurred_at=occurred_at,
            latitude=to_float(raw.get("Latitude")),
            longitude=to_float(raw.get("Longitude")),
            depth=to_float(raw.get("Depth")),
            magnitude=mag,
            place_name=str(raw.get("Hypocenter", "") or ""),
            max_intensity=max_int,
            serial=report_num,
            is_final=bool(raw.get("isFinal", False)),
            is_cancel=bool(raw.get("isCancel", False)),
            is_warn=bool(raw.get("isWarn", False)),
            is_sea=raw.get("isSea"),
            report_num=report_num,
            announced_time=self._parse_datetime(raw.get("AnnouncedTime", "")),
            warn_areas=warn_areas,
            raw=raw,
        )

        identity = EventIdentity(
            event_id=event_id,
            source_id=self.source_id,
            event_type="eew",
            provider_family="wolfx",
            report_num=report_num,
            is_final=bool(raw.get("isFinal", False)),
            published_at=occurred_at,
        )

        return [EventEnvelope(
            identity=identity,
            event=event,
            payload=SourcePayload(source_id=self.source_id, provider_family="wolfx", raw=raw),
        )]
