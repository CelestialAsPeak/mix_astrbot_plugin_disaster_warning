"""
Wolfx — JMA / CENC 地震情报 HTTP 轮询解析器（eqlist 格式）。

数据源: jma_wolfx_info_http
       cenc_wolfx_info_http  ← 新增

API: https://api.wolfx.jp/jma_eqlist.json
     https://api.wolfx.jp/cenc_eqlist.json

JMA 格式: {"No1": {"EventID": "...", "time": "...", ...}, "No2": {...}, ...}
CENC 格式: {"No1": {"type": "automatic", "time": "...", "location": "...", ...}, ...}
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

try:
    from ...domain.models import EarthquakeReport, EventEnvelope, EventIdentity, SourcePayload
except ImportError:
    from domain.models import EarthquakeReport, EventEnvelope, EventIdentity, SourcePayload

try:
    from ..base import BaseParser
    from ..registry import ParserRegistry
    from ...utils.convert import to_float, to_str

except ImportError:
    from parser.base import BaseParser
    from parser.registry import ParserRegistry
    from utils.convert import to_float, to_str

try:
    from ...utils.time import parse_ts
except ImportError:
    from utils.time import parse_ts


@ParserRegistry.register("jma_wolfx_info_http")
class WolfxJmaInfoHttpParser(BaseParser):
    """Wolfx JMA 地震情报 HTTP 解析器（eqlist 格式）。"""

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None

        envelopes = []
        for key, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            event_id = to_str(entry.get("EventID")) or key
            mag = to_float(entry.get("magnitude"))
            if mag is None:
                continue

            occurred_at = self._parse_datetime(entry.get("time", ""))

            event = EarthquakeReport(
                source_id=self.source_id,
                event_id=event_id,
                occurred_at=occurred_at,
                latitude=to_float(entry.get("latitude")),
                longitude=to_float(entry.get("longitude")),
                depth=self._parse_depth(entry.get("depth")),
                magnitude=mag,
                place_name=to_str(entry.get("location")),
                region=to_str(entry.get("region")),
                mmi=to_float(entry.get("shindo")),
                raw=entry,
            )

            identity = EventIdentity(
                event_id=event_id,
                source_id=self.source_id,
                event_type="earthquake",
                provider_family="wolfx",
                published_at=occurred_at,
            )

            envelopes.append(EventEnvelope(
                identity=identity,
                event=event,
                payload=SourcePayload(source_id=self.source_id, provider_family="wolfx", raw=entry),
            ))

        return envelopes if envelopes else None

    @staticmethod
    def _parse_depth(depth_str: object) -> float | None:
        """解析深度，兼容 '30km' 后缀格式。"""
        if depth_str is None:
            return None
        s = str(depth_str).strip().rstrip("kmKM")
        try:
            return float(s)
        except (ValueError, TypeError):
            return None


_CST = timezone(timedelta(hours=8))


def _parse_cst_time(time_str: str) -> datetime | None:
    """解析中国标准时间（UTC+8），格式: "2026-07-05 21:41:20" """
    if not time_str:
        return None
    try:
        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=_CST)
    except (ValueError, TypeError):
        pass
    try:
        from ...utils.time import parse_ts
    except ImportError:
        from utils.time import parse_ts
    return parse_ts(time_str)


@ParserRegistry.register("cenc_wolfx_info_http")
class WolfxCencInfoHttpParser(BaseParser):
    """Wolfx CENC 地震情报 HTTP 解析器（cenc_eqlist 格式）。

    字段说明:
      No(1~50)   地震信息条目数
      type       信息类型: automatic | reviewed
      time       发震时间(UTC+8)
      location   震源地（经处理）
      placeName  震源地（原始）
      magnitude  震级
      depth      震源深度
      latitude   纬度
      longitude  经度
      intensity  最大烈度
      md5        更新校验码
    """

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None

        envelopes = []
        for key, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            # 跳过根级 md5 字段
            if key == "md5":
                continue

            event_id = to_str(entry.get("EventID")) or key
            mag = to_float(entry.get("magnitude"))
            if mag is None:
                continue

            # CENC eqlist 的 time 为 UTC+8，用专用解析函数
            occurred_at = _parse_cst_time(entry.get("time", ""))

            event = EarthquakeReport(
                source_id=self.source_id,
                event_id=event_id,
                occurred_at=occurred_at,
                latitude=to_float(entry.get("latitude")),
                longitude=to_float(entry.get("longitude")),
                depth=self._parse_depth(entry.get("depth")),
                magnitude=mag,
                place_name=to_str(entry.get("location")) or to_str(entry.get("placeName")) or "",
                mmi=to_float(entry.get("intensity")),
                raw=entry,
            )

            identity = EventIdentity(
                event_id=event_id,
                source_id=self.source_id,
                event_type="earthquake",
                provider_family="wolfx",
                published_at=occurred_at,
            )

            envelopes.append(EventEnvelope(
                identity=identity,
                event=event,
                payload=SourcePayload(source_id=self.source_id, provider_family="wolfx", raw=entry),
            ))

        return envelopes if envelopes else None

    @staticmethod
    def _parse_depth(depth_str: object) -> float | None:
        if depth_str is None:
            return None
        s = str(depth_str).strip().rstrip("kmKM")
        try:
            return float(s)
        except (ValueError, TypeError):
            return None
