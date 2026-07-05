"""
parser/wolfx/http_eew.py — Wolfx EEW HTTP 轮询解析器（通用版，支持多数据源）。

数据源: jma_wolfx_http, cwa_wolfx_http
       sc_wolfx_http, fj_wolfx_http, cq_wolfx_http

API: https://api.wolfx.jp/jma_eew.json
     https://api.wolfx.jp/cwa_eew.json
     https://api.wolfx.jp/sc_eew.json
     https://api.wolfx.jp/fj_eew.json
     https://api.wolfx.jp/cq_eew.json

注意:
  - JMA 源的 OriginTime/AnnouncedTime 为 UTC+9
  - CWA/SC/FJ/CQ 源的 OriginTime 为 UTC+8
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

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger


# ── JMA 专用时间解析（OriginTime 为 UTC+9） ──

_JST = timezone(timedelta(hours=9))


def _parse_jst_time(time_str: str) -> datetime | None:
    """解析 Wolfx JMA 时间（UTC+9），格式: "2026/07/03 13:04:47" """
    if not time_str:
        return None
    try:
        dt = datetime.strptime(time_str, "%Y/%m/%d %H:%M:%S")
        return dt.replace(tzinfo=_JST)
    except (ValueError, TypeError):
        pass
    try:
        from ...utils.time import parse_ts
    except ImportError:
        from utils.time import parse_ts
    return parse_ts(time_str)


class _WolfxEewHttpParserBase(BaseParser):
    """Wolfx EEW HTTP 通用解析器基类（PascalCase 格式）。"""

    def _parse_source_time(self, raw: dict) -> datetime | None:
        """解析时间，JMA 源用 JST，其他源用基础解析。"""
        time_str = raw.get("OriginTime", raw.get("shockTime", ""))
        if self.source_id and "jma" in self.source_id:
            return _parse_jst_time(time_str)
        return self._parse_datetime(time_str)

    def _parse_source_announced(self, raw: dict) -> datetime | None:
        time_str = raw.get("AnnouncedTime", "")
        if self.source_id and "jma" in self.source_id:
            return _parse_jst_time(time_str)
        return self._parse_datetime(time_str)

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None

        event_id = to_str(raw.get("EventID")) or to_str(raw.get("ID")) or to_str(raw.get("id")) or ""
        if not event_id:
            return None

        occurred_at = self._parse_source_time(raw)
        report_num = to_int(raw.get("Serial", 1)) or 1

        mag = to_float(raw.get("Magunitude")) or to_float(raw.get("Magnitude")) or to_float(raw.get("magnitude"))
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
            place_name=str(raw.get("Hypocenter") or raw.get("HypoCenter") or ""),
            max_intensity=max_int,
            serial=report_num,
            is_final=bool(raw.get("isFinal", False)),
            is_cancel=bool(raw.get("isCancel", False)),
            is_warn=bool(raw.get("isWarn", False)),
            is_sea=raw.get("isSea"),
            report_num=report_num,
            announced_time=self._parse_source_announced(raw),
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


# ── CWA 独立解析器（字段格式不同：ID / HypoCenter / ReportTime） ──

@ParserRegistry.register("cwa_wolfx_http")
class WolfxCwaEewHttpParser(BaseParser):
    """台湾中央气象署 (CWA) 強震即時警報 via Wolfx HTTP。

    Wolfx 格式字段: ID, ReportTime, ReportNum, OriginTime,
      HypoCenter, Latitude, Longitude, Magunitude, Depth,
      MaxIntensity, isCancel
    """

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None
        event_id = to_str(raw.get("ID")) or ""
        if not event_id:
            return None

        report_num = to_int(raw.get("ReportNum", 1)) or 1
        occurred_at = self._parse_datetime(raw.get("OriginTime", ""))
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
            announced_time=self._parse_datetime(raw.get("ReportTime", "")),
            raw=raw,
        )

        return [EventEnvelope(
            identity=EventIdentity(
                event_id=event_id, source_id=self.source_id,
                event_type="eew", provider_family="wolfx",
                report_num=report_num,
                published_at=occurred_at,
            ),
            event=event,
            payload=SourcePayload(source_id=self.source_id, provider_family="wolfx", raw=raw),
        )]


# 逐个注册其余各数据源（同一解析逻辑，不同 source_id）
for _sid in ["jma_wolfx_http", "sc_wolfx_http", "fj_wolfx_http", "cq_wolfx_http"]:
    ParserRegistry.register(_sid)(type(f"WolfxEewHttpParser_{_sid}", (_WolfxEewHttpParserBase,), {}))
