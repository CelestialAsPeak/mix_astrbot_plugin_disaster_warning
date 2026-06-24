"""
P2P — 地震情报解析器。

数据源: jma_p2p_info
P2P code: 551 (地震情報)

P2PQuake v2 格式:
{
  "id": 123456789,        // P2P 消息 ID（每次唯一）
  "code": 551,
  "time": "...",
  "issue": {
    "source": "気象庁",
    "time": "...",
    "type": "通常"        // "通常" / "訂正" / "取消"
  },
  "earthquake": {
    "id": "20240101000000",  // JMA 地震 ID（同一地震不变）
    "time": "...",
    "hypocenter": {...},
    "maxScale": 40
  },
  "points": [...]
}

JMA 地震情报的多阶段特性：
  - 通常: 初期报（首次发布）
  - 訂正: 修正报（震源/震度数据更新）
  - 取消: 取消报（误报取消）

同一 earthquake.id 会收到多次不同 issue.type 的消息，
所有阶段的 event_id 应区分对待以避免去重拦截。
"""

from __future__ import annotations

try:
    from ...domain.models import EarthquakeReport, EventEnvelope, EventIdentity, SourcePayload
except ImportError:
    from domain.models import EarthquakeReport, EventEnvelope, EventIdentity, SourcePayload

try:
    from ..base import BaseParser
    from ..registry import ParserRegistry
    from ...utils.convert import to_float, to_int, to_str

except ImportError:
    from parser.base import BaseParser
    from parser.registry import ParserRegistry
    from utils.convert import to_float, to_int, to_str

@ParserRegistry.register("jma_p2p_info")
class P2pJmaReportParser(BaseParser):
    """P2P 日本气象厅地震情报解析器 (code 551)。"""

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None

        code = raw.get("code")
        if code != 551:
            return None

        earthquake = raw.get("earthquake", {})
        if not isinstance(earthquake, dict):
            return None

        # 真实数据确认：code 551 使用嵌套 hypocenter，无 flat latitude/longitude/placeName
        # 也无 earthquake.id，用 earthquake.time（发震时间）作为 base event_id
        # {"earthquake": {"time": "2026/06/24 16:46:00",
        #                  "hypocenter": {latitude, longitude, depth, magnitude, name}}}
        origin_time_str = to_str(earthquake.get("time")) or ""
        p2p_msg_id = to_str(raw.get("id")) or ""
        if not origin_time_str and not p2p_msg_id:
            return None

        # 用 issue.type 区分同一地震的不同阶段（通常/訂正/取消）
        issue = raw.get("issue", {})
        issue_type = to_str(issue.get("type")) or "通常"
        if issue_type not in ("通常", "訂正", "取消"):
            issue_type = "通常"

        # event_id = 发震时间|阶段类型（确保各阶段都能通过去重）
        base_id = origin_time_str.replace(" ", "T").replace("/", "-") if origin_time_str else ""
        event_id = f"{base_id}|{issue_type}" if base_id else p2p_msg_id
        is_cancel = (issue_type == "取消")

        hypocenter = earthquake.get("hypocenter", {}) or {}
        occurred_at = self._parse_datetime(earthquake.get("time", ""))
        intensity_points = raw.get("points") or raw.get("intensityPoints")

        event = EarthquakeReport(
            source_id=self.source_id,
            event_id=event_id,
            occurred_at=occurred_at,
            latitude=to_float(hypocenter.get("latitude")),
            longitude=to_float(hypocenter.get("longitude")),
            depth=to_float(hypocenter.get("depth")),
            magnitude=to_float(hypocenter.get("magnitude")),
            place_name=str(hypocenter.get("name", "") or ""),
            region=to_str(earthquake.get("name", "")),
            is_cancel=is_cancel,
            intensity_points=intensity_points,
            raw=raw,
        )

        identity = EventIdentity(
            event_id=event_id,
            source_id=self.source_id,
            event_type="earthquake",
            provider_family="p2p",
            is_cancel=is_cancel,
        )

        return [EventEnvelope(
            identity=identity,
            event=event,
            payload=SourcePayload(source_id=self.source_id, provider_family="p2p", raw=raw),
        )]
