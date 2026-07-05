"""
parser/http_poll/cenc_eew.py — CENC 省网 EEW（alarm_type=1）解析器。

API: POST https://yjfw.cenc.ac.cn/api/earthquake/event/v1/list
     {"app_id": "xxx", "alarm_type": 1, "page_query": {"page_no": 1, "page_size": 5}}

字段:
  id (str)            — 事件唯一标识（eventId）
  third_id (str)      — 第三方事件 ID，前缀 CD=成都ICL，CC=中国地震台网
  location (str)      — 震中地名
  level (str → float) — 震级
  depth (int/float)   — 深度 (km)
  lat/lng (float)     — 经纬度
  created_at (int)    — 发震时间 (epoch 秒)

注意: alarm_type=1 模式无 epicenter_intensity 和 serial_number。
"""

from __future__ import annotations

from datetime import datetime, timezone

try:
    from ...domain.models import EewEvent, EventEnvelope, EventIdentity, SourcePayload
except ImportError:
    from domain.models import EewEvent, EventEnvelope, EventIdentity, SourcePayload

try:
    from ..base import BaseParser
    from ..registry import ParserRegistry
    from ...utils.convert import to_float, to_str, to_int
except ImportError:
    from parser.base import BaseParser
    from parser.registry import ParserRegistry
    from utils.convert import to_float, to_str, to_int


_THIRD_ID_PREFIX = {
    "CD": "成都ICL",
    "CC": "中国地震台网",
}


class CencEewParser(BaseParser):
    """CENC 省网 EEW 警报解析器（alarm_type=1）。"""

    SOURCE_ID = "cenc_eew_http"

    def _make_envelope(self, eew: EewEvent, item: dict, occurred_at) -> EventEnvelope:
        return EventEnvelope(
            identity=EventIdentity(
                event_id=eew.event_id,
                source_id=eew.source_id,
                event_type="eew",
                report_num=eew.report_num,
                published_at=occurred_at,
            ),
            event=eew,
            payload=SourcePayload(
                source_id=eew.source_id,
                provider_family="direct_http",
                raw=item,
            ),
        )

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

        third_id = to_str(item.get("third_id")) or ""
        sub_source = ""
        if third_id and "." in third_id:
            prefix = third_id.split(".")[0]
            sub_source = _THIRD_ID_PREFIX.get(prefix, "")

        serial_number = to_int(item.get("serial_number"))
        report_num = serial_number if serial_number and serial_number > 0 else None

        eew = EewEvent(
            source_id=self.SOURCE_ID,
            event_id=event_id,
            occurred_at=occurred_at,
            announced_time=occurred_at,
            latitude=latitude,
            longitude=longitude,
            depth=depth,
            magnitude=magnitude,
            magnitude_type="Ml",
            place_name=place_name,
            province=sub_source,
            serial=report_num,
            report_num=report_num,
            raw=item,
        )

        return self._make_envelope(eew, item, occurred_at)


@ParserRegistry.register("cenc_eew_http")
class CencEewNationalParser(CencEewParser):
    """CENC 全国 EEW（对应 FAN CEA）。"""
    SOURCE_ID = "cenc_eew_http"


@ParserRegistry.register("cenc_eew_province")
class CencEewProvinceParser(CencEewParser):
    """CENC 省网 EEW（对应 FAN CEA-PR）。"""
    SOURCE_ID = "cenc_eew_province"
