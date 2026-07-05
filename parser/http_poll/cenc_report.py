"""
parser/http_poll/cenc_report.py — CENC 省网正式地震报告（无 alarm_type）解析器。

API: POST https://yjfw.cenc.ac.cn/api/earthquake/event/v1/list
     {"app_id": "xxx", "page_query": {"page_no": 1, "page_size": 5}}

字段:
  id (str)            — 事件唯一标识
  third_id (str)      — 第三方事件 ID（格式: YYYYMMDDHHmm.序号）
  location (str)      — 震中地名
  level (str → float) — 震级
  depth (int/float)   — 深度 (km)
  lat/lng (float)     — 经纬度
  epicenter_intensity — 预估烈度
  serial_number (int) — 报次
  created_at (int)    — 发震时间 (epoch 秒)
"""

from __future__ import annotations

from datetime import datetime, timezone

try:
    from ...domain.models import EarthquakeReport, EventEnvelope, EventIdentity, SourcePayload
except ImportError:
    from domain.models import EarthquakeReport, EventEnvelope, EventIdentity, SourcePayload

try:
    from ..base import BaseParser
    from ..registry import ParserRegistry
    from ...utils.convert import to_float, to_str, to_int
except ImportError:
    from parser.base import BaseParser
    from parser.registry import ParserRegistry
    from utils.convert import to_float, to_str, to_int


@ParserRegistry.register("cenc_report_http")
class CencReportParser(BaseParser):
    """CENC 省网正式地震报告解析器（无 alarm_type）。"""

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None
        code = raw.get("code")
        if code != 0:
            return None
        data = raw.get("data")
        if not isinstance(data, dict):
            return None
        items = data.get("spot_infos")
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

    def _parse_item(self, item: dict) -> EventEnvelope | None:
        event_id = to_str(item.get("id"))
        if not event_id:
            return None

        magnitude = to_float(item.get("level"))
        if magnitude is None or magnitude <= 0:
            return None

        latitude = to_float(item.get("latitude"))
        longitude = to_float(item.get("longitude"))

        occurred_at = None
        ts = to_int(item.get("created_at"))
        if ts:
            occurred_at = datetime.fromtimestamp(ts, tz=timezone.utc)

        depth = to_float(item.get("depth"))
        place_name = to_str(item.get("location")) or ""

        # 正式测定特有字段
        epicenter_intensity = to_float(item.get("epicenter_intensity"))
        serial_number = to_int(item.get("serial_number"))

        report = EarthquakeReport(
            source_id="cenc_report_http",
            event_id=event_id,
            occurred_at=occurred_at,
            latitude=latitude,
            longitude=longitude,
            depth=depth,
            magnitude=magnitude,
            magnitude_type="Ml",
            place_name=place_name,
            mmi=epicenter_intensity,
            report_num=serial_number,
            raw=item,
        )

        return EventEnvelope(
            identity=EventIdentity(
                event_id=event_id,
                source_id="cenc_report_http",
                event_type="earthquake",
                report_num=serial_number,
                published_at=occurred_at,
            ),
            event=report,
            payload=SourcePayload(
                source_id="cenc_report_http",
                provider_family="direct_http",
                raw=item,
            ),
        )
