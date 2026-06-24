"""
P2P — 海啸预报解析器。

数据源: jma_tsunami_p2p
P2P code: 552 (津波予報)

P2PQuake v2 格式:
{
  "id": "...", "code": 552,
  "time": "2024/01/01 00:00:00",
  "issue": {"source": "気象庁", "time": "...", "type": "通常"},
  "earthquake": {
    "time": "...",
    "hypocenter": {"name": "北海道東方沖", "latitude": 42.5, "longitude": 145.0, "depth": "10km", "magnitude": 7.8},
    "maxScale": 60
  },
  "areas": [
    {"name": "北海道太平洋沿岸", "grade": "MajorWarning", "immediate": true,
     "condition": "大海啸警报", "maxHeight": {"value": 5.0, "condition": "巨大"},
     "arrivalTime": "2024/01/01 00:30:00"}
  ],
  "cancelled": false
}
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

# JMA 震度文字 → 浮点值映射（maxScale → shindo）
_MAXSCALE_TO_SHINDO: dict[int, float] = {
    10: 1.0, 20: 2.0, 30: 3.0, 40: 4.0,
    45: 5.0, 50: 5.5, 55: 6.0, 60: 6.5, 70: 7.0,
}

# grade → level 数值映射
_GRADE_TO_LEVEL: dict[str, int] = {
    "MajorWarning": 3,
    "Warning": 2,
    "Watch": 1,
    "None": 0,
    "Unknown": 0,
}

# grade → 显示标题
_GRADE_TITLE: dict[str, str] = {
    "MajorWarning": "大津波警報",
    "Warning": "津波警報",
    "Watch": "津波注意報",
    "None": "津波予報",
    "Unknown": "津波予報",
}


@ParserRegistry.register("jma_tsunami_p2p")
class P2pTsunamiParser(BaseParser):
    """P2P 海啸预报解析器 (code 552)。"""

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None

        code = raw.get("code")
        if code != 552:
            return None

        event_id = to_str(raw.get("id")) or ""
        if not event_id:
            return None

        issue = raw.get("issue", {})
        issue_time = self._parse_datetime(
            issue.get("time", raw.get("time", ""))
        )

        cancelled = bool(raw.get("cancelled", False))

        # ── 解析 areas — 提取完整区域详情 ──
        raw_areas = raw.get("areas", [])
        areas = []
        if isinstance(raw_areas, list):
            for a in raw_areas:
                if not isinstance(a, dict):
                    continue
                area_name = to_str(a.get("name")) or ""
                if not area_name:
                    continue
                grade = to_str(a.get("grade")) or "Unknown"
                max_height = a.get("maxHeight")
                if isinstance(max_height, dict):
                    max_height_val = max_height.get("value")
                    max_height_cond = to_str(max_height.get("condition"))
                else:
                    max_height_val = max_height
                    max_height_cond = None

                areas.append({
                    "name": area_name,
                    "grade": grade,
                    "immediate": bool(a.get("immediate", False)),
                    "condition": to_str(a.get("condition")) or "",
                    "maxHeight": max_height_val,
                    "maxHeightCondition": max_height_cond or "",
                    "arrivalTime": to_str(a.get("arrivalTime")) or "",
                })

        # ── 确定最高警报等级 ──
        grade_order = ["None", "Unknown", "Watch", "Warning", "MajorWarning"]
        max_grade = "Unknown"
        for a in areas:
            g = a.get("grade", "Unknown")
            if g in grade_order and grade_order.index(g) > grade_order.index(max_grade):
                max_grade = g

        if cancelled:
            title = "津波予報（解除）"
            level = 0
            condition = "none"
        else:
            level = _GRADE_TO_LEVEL.get(max_grade, 0)
            title = _GRADE_TITLE.get(max_grade, "津波予報")
            condition = max_grade.lower() if max_grade in ("Watch", "Warning", "MajorWarning") else "none"

        # ── 解析 earthquake 块 ──
        eq_raw = raw.get("earthquake", {})
        hypocenter = eq_raw.get("hypocenter", {}) if isinstance(eq_raw, dict) else {}
        shock_info = {
            "placeName": to_str(hypocenter.get("name")),
            "latitude": to_float(hypocenter.get("latitude")),
            "longitude": to_float(hypocenter.get("longitude")),
            "depth": to_float(hypocenter.get("depth")),
            "magnitude": to_float(hypocenter.get("magnitude")),
            "time": to_str(eq_raw.get("time")),
            "maxScale": to_int(eq_raw.get("maxScale")),
        }
        # maxScale → shindo 文字
        ms = shock_info.get("maxScale")
        if ms is not None:
            shock_info["shindo"] = _MAXSCALE_TO_SHINDO.get(ms)
            shock_info["shindoLabel"] = _shindo_label(shock_info["shindo"]) if shock_info["shindo"] else None

        # ── 构造领域模型 ──
        event = TsunamiEvent(
            source_id=self.source_id,
            event_id=event_id,
            timestamp=issue_time,
            level=level,
            title=title,
            source_name="日本气象厅",
            condition=condition,
            class_name=("purple" if level == 3 else "red" if level == 2
                        else "yellow" if level == 1 else "gray"),
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

        return [EventEnvelope(
            identity=identity,
            event=event,
            payload=SourcePayload(source_id=self.source_id, provider_family="p2p", raw=raw),
        )]


def _shindo_label(shindo: float) -> str:
    """JMA 震度浮点值 → 中文震度等级标签。"""
    if shindo >= 6.5: return "震度7"
    if shindo >= 6.0: return "震度6強"
    if shindo >= 5.5: return "震度6弱"
    if shindo >= 5.0: return "震度5強"
    if shindo >= 4.5: return "震度5弱"
    if shindo >= 3.5: return "震度4"
    if shindo >= 2.5: return "震度3"
    if shindo >= 1.5: return "震度2"
    if shindo >= 0.5: return "震度1"
    return "震度0"
