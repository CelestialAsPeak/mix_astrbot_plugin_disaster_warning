"""
Wolfx — EEW 解析器（WebSocket 版）。

各源独立解析，不共享基类。
Wolfx 各源字段格式各有差异，独立解析更清晰。

数据源:
  jma_wolfx  — JMA 緊急地震速報（Magunitude/EventID/Hypocenter/WarnArea...）
  cenc_wolfx — CENC 地震预警（Magnitude/EventID/Hypocenter...）
  cwa_wolfx  — CWA 強震即時警報（ID/HypoCenter/Magunitude...）
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

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


_JST = timezone(timedelta(hours=9))
_CST = timezone(timedelta(hours=8))


def _parse_cst_time(time_str: str) -> datetime | None:
    """解析中国标准时间（UTC+8），格式: "2026-07-05 23:03:28" """
    if not time_str:
        return None
    try:
        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=_CST)
    except (ValueError, TypeError):
        pass
    try:
        from ...utils.time import parse_ts
    except ImportError:
        from utils.time import parse_ts
    return parse_ts(time_str)


def _parse_jst_time(time_str: str) -> datetime | None:
    """解析 Wolfx JMA 时间字符串（UTC+9），返回 timezone-aware datetime。

    Wolfx JMA 的 OriginTime/AnnouncedTime 格式如:
      "2026/07/03 13:04:47"  (UTC+9)
    """
    if not time_str:
        return None
    try:
        dt = datetime.strptime(time_str, "%Y/%m/%d %H:%M:%S")
        return dt.replace(tzinfo=_JST)
    except (ValueError, TypeError):
        pass
    # 兜底：标准格式
    try:
        from ...utils.time import parse_ts
    except ImportError:
        from utils.time import parse_ts
    return parse_ts(time_str)


# ── JMA EEW（Wolfx） ──

@ParserRegistry.register("jma_wolfx")
class WolfxJmaEewParser(BaseParser):
    """日本气象厅 (JMA) 緊急地震速報 via Wolfx。

    Wolfx 格式字段: EventID, Serial, AnnouncedTime, OriginTime,
      Hypocenter, Latitude, Longitude, Magunitude, Depth, MaxIntensity,
      isFinal, isCancel, isWarn, isSea, isTraining, isAssumption,
      WarnArea[], Accuracy{}, MaxIntChange{}, OriginalText

    注意:
      - OriginTime/AnnouncedTime 为 UTC+9（日本标准时）
      - Magunitude 是 Wolfx 的拼写错误（JMA 源如此）
      - isWarn=true → 警報, false → 予報
    """

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None
        event_id = to_str(raw.get("EventID")) or ""
        if not event_id:
            return None

        report_num = to_int(raw.get("Serial")) or 1
        # Wolfx JMA 的时间是 UTC+9，用专用解析函数
        occurred_at = _parse_jst_time(raw.get("OriginTime", ""))
        announced = _parse_jst_time(raw.get("AnnouncedTime", ""))
        mag = to_float(raw.get("Magunitude"))
        warn_areas = raw.get("WarnArea")

        event = EewEvent(
            source_id=self.source_id,
            event_id=event_id,
            occurred_at=occurred_at,
            latitude=to_float(raw.get("Latitude")),
            longitude=to_float(raw.get("Longitude")),
            depth=to_float(raw.get("Depth")),
            magnitude=mag,
            place_name=str(raw.get("Hypocenter", "") or ""),
            max_intensity=str(raw.get("MaxIntensity", "") or ""),
            serial=report_num,
            report_num=report_num,
            is_final=bool(raw.get("isFinal", False)),
            is_cancel=bool(raw.get("isCancel", False)),
            is_warn=bool(raw.get("isWarn", False)),
            is_sea=raw.get("isSea"),
            announced_time=announced,
            warn_areas=warn_areas,
            raw=raw,
        )

        return [EventEnvelope(
            identity=EventIdentity(
                event_id=event_id, source_id=self.source_id,
                event_type="eew", provider_family="wolfx",
                report_num=report_num, is_final=bool(raw.get("isFinal", False)),
                published_at=announced or occurred_at,
            ),
            event=event,
            payload=SourcePayload(source_id=self.source_id, provider_family="wolfx", raw=raw),
        )]


# ── CENC EEW（Wolfx） ──

@ParserRegistry.register("cenc_wolfx")
class WolfxCencEewParser(BaseParser):
    """中国地震预警网 (CENC) 地震预警 via Wolfx。

    Wolfx 格式字段: EventID, ID, ReportTime, ReportNum, OriginTime,
      HypoCenter, Latitude, Longitude, Magnitude, Depth, MaxIntensity, isCancel
    """

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None
        event_id = to_str(raw.get("EventID")) or to_str(raw.get("ID")) or ""
        if not event_id:
            return None

        report_num = to_int(raw.get("ReportNum", 1)) or 1
        # CENC 时间为 UTC+8
        occurred_at = _parse_cst_time(raw.get("OriginTime", ""))
        announced = _parse_cst_time(raw.get("ReportTime", ""))
        mag = to_float(raw.get("Magnitude"))

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
            report_num=report_num,
            is_cancel=bool(raw.get("isCancel", False)),
            announced_time=announced,
            raw=raw,
        )

        return [EventEnvelope(
            identity=EventIdentity(
                event_id=event_id, source_id=self.source_id,
                event_type="eew", provider_family="wolfx",
                report_num=report_num,
                published_at=announced or occurred_at,
            ),
            event=event,
            payload=SourcePayload(source_id=self.source_id, provider_family="wolfx", raw=raw),
        )]


# ── CWA EEW（Wolfx） ──

@ParserRegistry.register("cwa_wolfx")
class WolfxCwaEewParser(BaseParser):
    """台湾中央气象署 (CWA) 強震即時警報 via Wolfx。

    Wolfx 格式字段: ID, ReportTime, ReportNum, OriginTime,
      HypoCenter, Latitude, Longitude, Magunitude, Depth,
      MaxIntensity, isCancel
    """

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None
        event_id = to_str(raw.get("ID")) or to_str(raw.get("EventID")) or ""
        if not event_id:
            return None

        report_num = to_int(raw.get("ReportNum", 1)) or 1
        # CWA 时间为 UTC+8
        occurred_at = _parse_cst_time(raw.get("OriginTime", ""))
        announced = _parse_cst_time(raw.get("ReportTime", ""))
        mag = to_float(raw.get("Magunitude"))

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
            report_num=report_num,
            is_cancel=bool(raw.get("isCancel", False)),
            announced_time=announced,
            raw=raw,
        )

        return [EventEnvelope(
            identity=EventIdentity(
                event_id=event_id, source_id=self.source_id,
                event_type="eew", provider_family="wolfx",
                report_num=report_num,
                published_at=announced or occurred_at,
            ),
            event=event,
            payload=SourcePayload(source_id=self.source_id, provider_family="wolfx", raw=raw),
        )]
