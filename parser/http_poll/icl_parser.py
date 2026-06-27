"""
parser/http_poll/icl_parser.py — ICL（成都高新减灾研究所）地震预警网解析器。

API: https://mobile-new.chinaeew.cn/v1/earlywarnings?updates=3&start_at=1
返回 JSON: { "code": 0, "data": [
    { "eventId", "updates", "latitude", "longitude", "depth",
      "epicenter", "startAt", "updateAt", "magnitude", "sourceType", "epiIntensity" }
]}

⚠️ 法律警告: 此数据源不得以任何形式上传 GitHub 等公开仓库！
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

try:
    from ...domain.models import EewEvent, EventEnvelope, EventIdentity
except ImportError:
    from domain.models import EewEvent, EventEnvelope, EventIdentity

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


# 中国烈度标准：小数 epiIntensity → 罗马数字显示
_INTENSITY_LABELS: dict[str, str] = {
    "1": "Ⅰ度", "2": "Ⅱ度", "3": "Ⅲ度", "4": "Ⅳ度",
    "5": "Ⅴ度", "6": "Ⅵ度", "7": "Ⅶ度", "8": "Ⅷ度",
    "9": "Ⅸ度", "10": "Ⅹ度", "11": "Ⅺ度", "12": "Ⅻ度",
}

_SOURCE_TYPE_LABELS: dict[int, str] = {
    1: "四川",
    2: "新疆",
}


@ParserRegistry.register("icl_http")
class IclParser(BaseParser):
    """ICL 成都高新减灾研究所 EEW 解析器。

    HTTP 轮询 JSON API，返回 EewEvent（地震预警）。
    """

    def parse(self, raw: dict | str) -> list[EventEnvelope] | None:
        """解析 ICL API JSON → list[EventEnvelope] with EewEvent。"""
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return None

        if not isinstance(raw, dict):
            return None

        # API 响应格式: {"code": 0, "data": [...]}
        code = raw.get("code")
        if code != 0:
            return None

        items = raw.get("data")
        if not isinstance(items, list) or not items:
            return None

        results: list[EventEnvelope] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            env = self._parse_item(item)
            if env is not None:
                results.append(env)

        return results if results else None

    def _parse_item(self, item: dict[str, Any]) -> EventEnvelope | None:
        """解析单条 ICL 预警数据 → EventEnvelope(EewEvent)。"""
        # ── 必填字段 ──
        event_id = to_str(item.get("eventId"))
        if not event_id:
            return None

        magnitude = to_float(item.get("magnitude"))
        if magnitude is None or magnitude <= 0:
            return None

        # ── 经纬度 ──
        latitude = to_float(item.get("latitude"))
        longitude = to_float(item.get("longitude"))

        # ── 时间 (epoch ms, UTC) ──
        occurred_at = None
        start_ms = to_int(item.get("startAt"))
        if start_ms:
            occurred_at = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)

        announced_time = None
        update_ms = to_int(item.get("updateAt"))
        if update_ms:
            announced_time = datetime.fromtimestamp(update_ms / 1000, tz=timezone.utc)

        # ── 深度 / 地名 ──
        depth = to_float(item.get("depth"))
        place_name = to_str(item.get("epicenter")) or "中国"

        # ── 报次 ──
        updates = to_int(item.get("updates")) or 1

        # ── 烈度 ──
        epi_intensity = to_float(item.get("epiIntensity"))
        max_intensity: str | None = None
        if epi_intensity is not None:
            int_key = str(int(round(epi_intensity)))
            max_intensity = _INTENSITY_LABELS.get(int_key, f"{epi_intensity:.1f}")

        # ── 子源（四川/新疆） ──
        source_type = to_int(item.get("sourceType")) or 0
        sub_source = _SOURCE_TYPE_LABELS.get(source_type, f"type{source_type}")

        eew = EewEvent(
            source_id="icl_http",
            event_id=event_id,
            occurred_at=occurred_at,
            announced_time=announced_time,
            latitude=latitude,
            longitude=longitude,
            depth=depth,
            magnitude=magnitude,
            magnitude_type="ml",
            place_name=place_name,
            max_intensity=max_intensity,
            serial=updates,
            report_num=updates,
            raw=item,
        )

        return EventEnvelope(
            identity=EventIdentity(
                event_id=event_id,
                source_id="icl_http",
                event_type="eew",
                report_num=updates,
                published_at=announced_time,
            ),
            event=eew,
        )
