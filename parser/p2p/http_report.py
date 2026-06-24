"""
P2P — 地震情报 HTTP 轮询解析器（备用）。

数据源: jma_p2p_info_http
API: https://api.p2pquake.net/v2/jma/quake?limit=5
格式: [item1, item2, ...]

v2/jma/quake 格式（与 WS/v2/history 格式不同）:
  - earthquake.hypocenter.name  → 震源地名称
  - earthquake.hypocenter.lat/lng/depth/magnitude
  - earthquake.maxScale          → 最大震度×10 (-1=不明)
  - issue.type: ScalePrompt / Destination / DetailScale / ScaleAndDestination
  - points[]: 震度观测点 (addr, pref, scale, isArea)

P2P WebSocket 失能时作为 HTTP 轮询备用。
"""

from __future__ import annotations

try:
    from ...domain.models import EarthquakeReport, EventEnvelope
except ImportError:
    from domain.models import EarthquakeReport, EventEnvelope

try:
    from ..base import BaseParser
    from ..registry import ParserRegistry
    from ...utils.convert import to_float, to_str

except ImportError:
    from parser.base import BaseParser
    from parser.registry import ParserRegistry
    from utils.convert import to_float, to_str

# JMA 地震情报 issue.type → 中文标题
ISSUE_TITLE_MAP: dict[str, str] = {
    "ScalePrompt": "震度速報",
    "Destination": "震源に関する情報",
    "ScaleAndDestination": "震度・震源に関する情報",
    "DetailScale": "各地の震度に関する情報",
    "Foreign": "遠地地震に関する情報",
    "Other": "その他の情報",
}

# sentinel values（v2/jma/quake API用）
_SENTINEL_LAT = -200.0
_SENTINEL_LNG = -200.0
_SENTINEL_DEPTH = -1
_SENTINEL_MAG = -1
_SENTINEL_SCALE = -1


def _clean_float(val: object) -> float | None:
    """转换浮点值，排除 sentinel。"""
    if val is None:
        return None
    try:
        v = float(val)
    except (TypeError, ValueError):
        return None
    if v == _SENTINEL_LAT or v == _SENTINEL_LNG or v == _SENTINEL_DEPTH:
        return None
    return v


def _clean_int(val: object) -> int | None:
    """转换整数值，排除 sentinel。"""
    if val is None:
        return None
    try:
        v = int(val)
    except (TypeError, ValueError):
        return None
    if v == _SENTINEL_SCALE or v == _SENTINEL_DEPTH or v == _SENTINEL_MAG:
        return None
    return v


@ParserRegistry.register("jma_p2p_info_http")
class P2pJmaInfoHttpParser(BaseParser):
    """P2P JMA 地震情报 HTTP 解析器（v2/jma/quake 格式）。"""

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
        if raw.get("code") != 551:
            return None

        eq = raw.get("earthquake", {})
        if not isinstance(eq, dict):
            return None

        # event_id = 发震时间|issue.type（区分同地震多阶段）
        origin_time = to_str(eq.get("time")) or ""
        if not origin_time:
            return None

        issue = raw.get("issue", {})
        issue_type = to_str(issue.get("type")) or "DetailScale"
        event_id = f"{origin_time}|{issue_type}"

        # 发震时间
        occurred_at = self._parse_datetime(origin_time)

        # hypocenter 嵌套结构（v2/jma/quake 格式）
        hypocenter = eq.get("hypocenter", {})
        if not isinstance(hypocenter, dict):
            hypocenter = {}

        latitude = _clean_float(hypocenter.get("latitude"))
        longitude = _clean_float(hypocenter.get("longitude"))
        depth = _clean_float(hypocenter.get("depth"))
        magnitude = _clean_float(hypocenter.get("magnitude"))
        place_name = to_str(hypocenter.get("name")) or ""

        # maxScale → 最大震度（×10，-1=不明）
        max_scale_raw = eq.get("maxScale")
        max_scale = _clean_int(max_scale_raw)
        mmi = (max_scale / 10.0) if max_scale is not None else None

        # 震度观测点（points[]）
        raw_points = raw.get("points", [])
        intensity_points = []
        if isinstance(raw_points, list):
            for p in raw_points:
                if not isinstance(p, dict):
                    continue
                scale_val = p.get("scale")
                scale_float = (float(scale_val) / 10.0) if scale_val is not None else None
                intensity_points.append({
                    "addr": to_str(p.get("addr")) or "",
                    "pref": to_str(p.get("pref")) or "",
                    "scale": scale_float,
                    "isArea": bool(p.get("isArea", False)),
                })

        # issue 标题
        title = ISSUE_TITLE_MAP.get(issue_type, "地震情報")

        event = EarthquakeReport(
            source_id=self.source_id,
            event_id=event_id,
            occurred_at=occurred_at,
            latitude=latitude,
            longitude=longitude,
            depth=depth,
            magnitude=magnitude,
            magnitude_type="Mj" if magnitude is not None else None,
            place_name=place_name,
            region=to_str(eq.get("name")),
            mmi=mmi,
            intensity_points=intensity_points if intensity_points else None,
            raw=raw,
        )

        from ...domain.models import EventIdentity, SourcePayload
        identity = EventIdentity(
            event_id=event_id,
            source_id=self.source_id,
            event_type="earthquake",
            provider_family="p2p",
            published_at=occurred_at,
        )

        return [EventEnvelope(
            identity=identity,
            event=event,
            payload=SourcePayload(source_id=self.source_id, provider_family="p2p", raw=raw),
        )]
