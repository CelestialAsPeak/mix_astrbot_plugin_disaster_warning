"""
P2P — EEW HTTP 轮询解析器（备用）。

数据源: jma_p2p_http
API: https://api.p2pquake.net/v2/history?codes=556&limit=1
格式: [item1, ...]

P2P WebSocket 失能时作为 HTTP 轮询备用。
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
    from ...utils.time import parse_jst_time

except ImportError:
    from parser.base import BaseParser
    from parser.registry import ParserRegistry
    from utils.convert import to_float, to_int, to_str
    from utils.time import parse_jst_time


@ParserRegistry.register("jma_p2p_http")
class P2pJmaEewHttpParser(BaseParser):
    """P2P JMA EEW HTTP 解析器（code 556 列表）。"""

    def parse_message(self, message: str | bytes | dict | list) -> list[EventEnvelope] | None:
        payload = self.decode_message(message)
        if payload is None:
            return None
        items = payload if isinstance(payload, list) else [payload]
        envelopes = []
        for item in items:
            if not isinstance(item, dict):
                continue
            result = self.parse(item)
            if result:
                envelopes.extend(result)
        return envelopes if envelopes else None

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None
        if raw.get("code") != 556:
            return None

        earthquake = raw.get("earthquake", {})
        if not isinstance(earthquake, dict):
            return None

        # 真实数据确认：/history?codes=556 和 WS 格式一致（nested hypocenter）
        issue = raw.get("issue", {}) or {}
        event_id = to_str(issue.get("eventId")) or to_str(raw.get("id")) or ""
        if not event_id:
            return None

        hypocenter = earthquake.get("hypocenter", {}) or {}
        occurred_at = parse_jst_time(earthquake.get("originTime", raw.get("time", ""))) or self._parse_datetime(earthquake.get("originTime", raw.get("time", "")))
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
            announced_time=parse_jst_time(issue.get("time", "")) or self._parse_datetime(issue.get("time", "")),
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
            published_at=occurred_at,
        )

        return [EventEnvelope(
            identity=identity, event=event,
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
