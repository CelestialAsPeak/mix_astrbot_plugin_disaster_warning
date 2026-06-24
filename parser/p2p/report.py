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

        # JMA 地震情报多阶段处理：
        #   earthquake.id = 发震时间（同一地震不变）
        #   issue.type    = "通常" / "訂正" / "取消"
        #   raw.id        = P2P 消息 ID（每次唯一）
        base_event_id = to_str(earthquake.get("id")) or ""
        p2p_msg_id = to_str(raw.get("id")) or ""
        if not base_event_id and not p2p_msg_id:
            return None

        # 用 issue.type 区分同一地震的不同阶段
        issue = raw.get("issue", {})
        issue_type = to_str(issue.get("type")) or "通常"
        if issue_type not in ("通常", "訂正", "取消"):
            issue_type = "通常"

        # event_id = 地震ID + 阶段类型（确保各阶段都能通过去重）
        event_id = f"{base_event_id}|{issue_type}" if base_event_id else p2p_msg_id
        is_cancel = (issue_type == "取消")

        occurred_at = self._parse_datetime(earthquake.get("time", ""))
        intensity_points = raw.get("points") or raw.get("intensityPoints")

        event = EarthquakeReport(
            source_id=self.source_id,
            event_id=event_id,
            occurred_at=occurred_at,
            latitude=to_float(earthquake.get("latitude")),
            longitude=to_float(earthquake.get("longitude")),
            depth=to_float(earthquake.get("depth")),
            magnitude=to_float(earthquake.get("magnitude")),
            place_name=str(earthquake.get("placeName", "") or ""),
            region=str(raw.get("regionName", "") or ""),
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
