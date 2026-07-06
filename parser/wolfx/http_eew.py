"""
parser/wolfx/http_eew.py — Wolfx EEW HTTP 轮询解析器（通用版，支持多数据源）。

数据源: jma_wolfx_http, cwa_wolfx_http, kma_wolfx_http
       sc_wolfx_http, fj_wolfx_http, cq_wolfx_http

API: https://api.wolfx.jp/jma_eew.json
     https://api.wolfx.jp/cwa_eew.json
     https://api.wolfx.jp/kma_eew.json
     https://api.wolfx.jp/sc_eew.json
     https://api.wolfx.jp/fj_eew.json
     https://api.wolfx.jp/cq_eew.json

格式: 单条 JSON，PascalCase 字段
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

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger


class _WolfxEewHttpParserBase(BaseParser):
    """Wolfx EEW HTTP 通用解析器基类（PascalCase 格式）。"""

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None

        event_id = to_str(raw.get("EventID")) or to_str(raw.get("id")) or ""
        if not event_id:
            return None

        occurred_at = self._parse_datetime(raw.get("OriginTime", raw.get("shockTime", "")))
        report_num = to_int(raw.get("Serial", 1)) or 1

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
            place_name=str(raw.get("Hypocenter", raw.get("Hypocenter", "")) or ""),
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


# 逐个注册各数据源（同一解析逻辑，不同 source_id）
for _sid in ["jma_wolfx_http", "cwa_wolfx_http", "kma_wolfx_http",
             "sc_wolfx_http", "fj_wolfx_http", "cq_wolfx_http"]:
    ParserRegistry.register(_sid)(type(f"WolfxEewHttpParser_{_sid}", (_WolfxEewHttpParserBase,), {}))
