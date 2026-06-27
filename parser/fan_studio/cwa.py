"""
FAN Studio — 台湾中央气象署 (CWA) 解析器。

数据源: cwa_fanstudio (EEW), cwa_report_fanstudio (地震报告)
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

@ParserRegistry.register("cwa_fanstudio")
class CWAEEWParser(BaseParser):
    """台湾中央气象署 (CWA) EEW 解析器。"""

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None
        if self._is_heartbeat(raw):
            return []

        event_id = to_str(raw.get("eventId")) or ""
        if not event_id:
            return None

        occurred_at = self._parse_datetime(raw.get("reportTime", raw.get("shockTime", "")))
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
            max_intensity=str(raw.get("reportIntensity", "") or ""),
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


@ParserRegistry.register("cwa_report_fanstudio")
class CWAReportParser(BaseParser):
    """台湾中央气象署 (CWA) 地震报告解析器。"""

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None
        event_id = to_str(raw.get("eventId")) or ""
        if not event_id:
            return None

        occurred_at = self._parse_datetime(raw.get("time") or raw.get("occurred_at") or raw.get("shockTime") or "")

        # CWA maxIntensity: "3級" → mmi float (3.0)
        max_intensity_str = str(raw.get("maxIntensity", "") or "")
        mmi_val = _parse_cwa_intensity(max_intensity_str)

        event = EarthquakeReport(
            source_id=self.source_id,
            event_id=event_id,
            occurred_at=occurred_at,
            latitude=to_float(raw.get("latitude")),
            longitude=to_float(raw.get("longitude")),
            depth=to_float(raw.get("depth")),
            magnitude=to_float(raw.get("magnitude")),
            place_name=str(raw.get("placeName", "") or ""),
            region=str(raw.get("region", "") or ""),
            mmi=mmi_val,
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


def _parse_cwa_intensity(val: str) -> float | None:
    """CWA 震度文字 → float。

    "3級" → 3.0,  "5弱" → 5.0,  "5強" → 5.5,  "7級" → 7.0
    "震度3級" → 3.0 等繁体/全角兼容。
    """
    s = val.strip()
    if not s:
        return None
    # 全角数字 → 半角
    s = s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    s = s.replace("震度", "").replace("級", "")
    # 5強/5弱 → 数值
    if "強" in s or "强" in s:
        try:
            return float(s.replace("強", "").replace("强", "")) + 0.5
        except (ValueError, TypeError):
            return None
    if "弱" in s:
        try:
            return float(s.replace("弱", ""))
        except (ValueError, TypeError):
            return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None
