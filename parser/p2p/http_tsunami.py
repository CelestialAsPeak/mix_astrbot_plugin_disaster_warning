"""
P2P — 海啸预报 HTTP 轮询解析器（备用）。

数据源: jma_tsunami_p2p_http
API: https://api.p2pquake.net/v2/history?codes=552&limit=1
格式: [item1, ...]

P2P WebSocket 失能时作为 HTTP 轮询备用。
"""

from __future__ import annotations

try:
    from ...domain.models import TsunamiEvent, EventEnvelope, EventIdentity, SourcePayload
except ImportError:
    from domain.models import TsunamiEvent, EventEnvelope, EventIdentity, SourcePayload

try:
    from ..base import BaseParser
    from ..registry import ParserRegistry
    from ...utils.convert import to_float, to_int, to_str

except ImportError:
    from parser.base import BaseParser
    from parser.registry import ParserRegistry
    from utils.convert import to_float, to_int, to_str

# grade → level
_GRADE_TO_LEVEL = {"MajorWarning": 3, "Warning": 2, "Watch": 1, "None": 0, "Unknown": 0}
_GRADE_TITLE = {"MajorWarning": "大津波警報", "Warning": "津波警報", "Watch": "津波注意報"}
_MAXSCALE_TO_SHINDO = {10: 1.0, 20: 2.0, 30: 3.0, 40: 4.0, 45: 5.0, 50: 5.5, 55: 6.0, 60: 6.5, 70: 7.0}
GRADE_ORDER = ["None", "Unknown", "Watch", "Warning", "MajorWarning"]


@ParserRegistry.register("jma_tsunami_p2p_http")
class P2pJmaTsunamiHttpParser(BaseParser):
    """P2P JMA 海啸预报 HTTP 解析器（code 552 列表）。"""

    def parse_message(self, message: str | bytes | dict | list) -> list[EventEnvelope] | None:
        payload = self.decode_message(message)
        if payload is None:
            return None
        items = payload if isinstance(payload, list) else [payload]
        envelopes = []
        for item in items:
            if not isinstance(item, dict):
                continue
            result = self._parse_one(item)
            if result:
                envelopes.append(result)
        return envelopes if envelopes else None

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        result = self._parse_one(raw)
        return [result] if result else None

    def _parse_one(self, raw: dict) -> EventEnvelope | None:
        if raw.get("code") != 552:
            return None
        event_id = to_str(raw.get("id")) or ""
        if not event_id:
            return None

        issue = raw.get("issue", {}) or {}
        issue_time = self._parse_datetime(issue.get("time", raw.get("time", "")))
        cancelled = bool(raw.get("cancelled", False))

        # areas
        raw_areas = raw.get("areas", [])
        areas = []
        for a in raw_areas if isinstance(raw_areas, list) else []:
            if not isinstance(a, dict):
                continue
            name = to_str(a.get("name")) or ""
            if not name:
                continue
            mh = a.get("maxHeight")
            fh = a.get("firstHeight", {}) or {}
            areas.append({
                "name": name,
                "grade": to_str(a.get("grade")) or "Unknown",
                "immediate": bool(a.get("immediate", False)),
                "condition": to_str(fh.get("condition")) or "",
                "maxHeight": mh.get("value") if isinstance(mh, dict) else mh,
                "maxHeightCondition": to_str(mh.get("condition")) if isinstance(mh, dict) else "",
                "arrivalTime": to_str(fh.get("arrivalTime")) or "",
            })
            del mh

        # 最高等级
        max_grade = max((a.get("grade", "Unknown") for a in areas),
                        key=lambda g: GRADE_ORDER.index(g) if g in GRADE_ORDER else -1,
                        default="Unknown")
        if cancelled:
            title, level, condition = "津波予報（解除）", 0, "none"
        else:
            level = _GRADE_TO_LEVEL.get(max_grade, 0)
            title = _GRADE_TITLE.get(max_grade, "津波予報")
            condition = max_grade.lower() if max_grade in ("Watch", "Warning", "MajorWarning") else "none"

        # earthquake shock info
        eq_raw = raw.get("earthquake", {}) or {}
        hypo = eq_raw.get("hypocenter", {}) if isinstance(eq_raw, dict) else {}
        ms = to_int(eq_raw.get("maxScale"))
        shindo_label = None
        if ms is not None:
            sv = _MAXSCALE_TO_SHINDO.get(ms)
            if sv is not None:
                shindo_label = ("震度7" if sv >= 6.5 else "震度6強" if sv >= 6.0 else
                                "震度6弱" if sv >= 5.5 else "震度5強" if sv >= 5.0 else
                                "震度5弱" if sv >= 4.5 else "震度4" if sv >= 3.5 else
                                "震度3" if sv >= 2.5 else "震度2" if sv >= 1.5 else
                                "震度1" if sv >= 0.5 else "震度0")
        shock_info = {
            "placeName": to_str(hypo.get("name")),
            "latitude": to_float(hypo.get("latitude")),
            "longitude": to_float(hypo.get("longitude")),
            "depth": to_float(hypo.get("depth")),
            "magnitude": to_float(hypo.get("magnitude")),
            "shindoLabel": shindo_label,
        }

        event = TsunamiEvent(
            source_id=self.source_id,
            event_id=event_id,
            timestamp=issue_time,
            level=level,
            title=title,
            source_name="日本气象厅",
            condition=condition,
            class_name="purple" if level == 3 else "red" if level == 2 else "yellow" if level == 1 else "gray",
            areas=areas if areas else None,
            shock_info=shock_info if any(v is not None for v in shock_info.values()) else None,
            raw=raw,
        )
        identity = EventIdentity(
            event_id=event_id,
            source_id=self.source_id,
            event_type="tsunami",
            provider_family="p2p",
            published_at=issue_time,
        )
        return EventEnvelope(
            identity=identity, event=event,
            payload=SourcePayload(source_id=self.source_id, provider_family="p2p", raw=raw),
        )
