"""
P2P — EEW 解析器。

数据源: jma_p2p
P2P code: 556 (緊急地震速報)
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

@ParserRegistry.register("jma_p2p")
class P2pJmaEewParser(BaseParser):
    """P2P 日本气象厅 EEW 解析器 (code 556)。"""

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None

        code = raw.get("code")
        if code != 556:
            return None

        earthquake = raw.get("earthquake", {})
        if not isinstance(earthquake, dict):
            return None

        # 真实数据确认：code 556 使用 issue.eventId + issue.serial + nested hypocenter
        # {"issue": {"eventId": "20260616194642", "serial": "1"},
        #  "earthquake": {"originTime": "...", "hypocenter": {latitude, longitude, depth, magnitude, name}}}
        issue = raw.get("issue", {}) or {}
        event_id = to_str(issue.get("eventId")) or to_str(raw.get("id")) or ""
        if not event_id:
            return None

        hypocenter = earthquake.get("hypocenter", {}) or {}
        occurred_at = self._parse_datetime(earthquake.get("originTime", raw.get("time", "")))

        # P2P report_num 从 issue.serial 提取
        serial = to_int(issue.get("serial")) or 0

        # 预警区域最高震度（areas[].scaleTo 取最大值 ÷10）
        areas = raw.get("areas", [])
        max_scale_val = None
        if isinstance(areas, list):
            for a in areas:
                if isinstance(a, dict):
                    st = to_float(a.get("scaleTo"))
                    if st is not None:
                        sv = st / 10.0
                        if max_scale_val is None or sv > max_scale_val:
                            max_scale_val = sv
        max_intensity_str = _shindo_label_float(max_scale_val) if max_scale_val is not None else ""

        event = EewEvent(
            source_id=self.source_id,
            event_id=event_id,
            occurred_at=occurred_at,
            latitude=to_float(hypocenter.get("latitude")),
            longitude=to_float(hypocenter.get("longitude")),
            depth=to_float(hypocenter.get("depth")),
            magnitude=to_float(hypocenter.get("magnitude")),
            place_name=str(hypocenter.get("name", "") or ""),
            max_intensity=max_intensity_str,
            serial=serial,
            is_final=bool(raw.get("isFinal", False)),
            is_warn=True,  # code 556 = 緊急地震速報（警報）
            is_cancel=bool(raw.get("cancelled", False)),
            is_sea=earthquake.get("isSea"),
            report_num=serial,
            announced_time=self._parse_datetime(issue.get("time", "")),
            warn_areas=areas,
            raw=raw,
        )

        identity = EventIdentity(
            event_id=event_id,
            source_id=self.source_id,
            event_type="eew",
            provider_family="p2p",
            report_num=serial,
            is_final=bool(raw.get("isFinal", False)),
        )

        return [EventEnvelope(
            identity=identity,
            event=event,
            payload=SourcePayload(source_id=self.source_id, provider_family="p2p", raw=raw),
        )]


def _shindo_label_float(shindo: float) -> str:
    """JMA 震度浮点值（÷10 后）→ 中文震度等级。"""
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
