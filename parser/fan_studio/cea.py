"""
FAN Studio — 中国地震预警网 (CEA EEW) 解析器。

数据源: cea_fanstudio, cea_pr_fanstudio
原始字段: eventId, shockTime, latitude, longitude, magnitude, depth,
          placeName, epiIntensity, province, reportNum, updates, isFinal
"""

from __future__ import annotations

from datetime import datetime

try:
    from ...domain.models import (
        EewEvent, EventEnvelope, EventIdentity, SourcePayload,
    )
except ImportError:

    from domain.models import (
        EewEvent, EventEnvelope, EventIdentity, SourcePayload,
    )

try:
    from ..base import BaseParser
    from ..registry import ParserRegistry
    from ...utils.convert import to_float

except ImportError:
    from parser.base import BaseParser
    from parser.registry import ParserRegistry
    from utils.convert import to_float

class CEAEEWParser(BaseParser):
    """中国地震预警网 (CEA) 通用解析器。"""

    def parse(self, raw: dict) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None
        if self._is_heartbeat(raw):
            return []

        env = self._build_envelope(raw)
        return [env] if env else []

    def _build_envelope(self, data: dict) -> EventEnvelope | None:
        event_id = str(data.get("eventId", "") or data.get("id", "") or "")
        if not event_id:
            return None

        # 时间
        occurred_at = self._parse_datetime(data.get("shockTime", ""))

        # 报次
        try:
            report_num = int(data.get("reportNum", data.get("updates", 1)))
        except (TypeError, ValueError):
            report_num = 1
        if report_num <= 0:
            report_num = 1

        # 构建 EEW 领域事件
        is_final = bool(data.get("isFinal", False))
        event = EewEvent(
            source_id=self.source_id,
            event_id=event_id,
            occurred_at=occurred_at,
            latitude=to_float(data.get("latitude")),
            longitude=to_float(data.get("longitude")),
            depth=to_float(data.get("depth")),
            magnitude=to_float(data.get("magnitude")),
            place_name=str(data.get("placeName", "") or ""),
            max_intensity=str(data.get("epiIntensity", "") or ""),
            province=str(data.get("province", "") or ""),
            serial=report_num,
            is_final=is_final,
            report_num=report_num,
            raw=data,
        )

        identity = EventIdentity(
            event_id=event_id,
            source_id=self.source_id,
            event_type="eew",
            provider_family="fan_studio",
            report_num=report_num,
            published_at=occurred_at,
            is_final=is_final,
        )

        return EventEnvelope(
            identity=identity,
            event=event,
            payload=SourcePayload(
                source_id=self.source_id,
                provider_family="fan_studio",
                message_type=str(data.get("type", "update")),
                raw=data,
            ),
        )


@ParserRegistry.register("cea_fanstudio")
class CEAEEWMainParser(CEAEEWParser):
    """中国地震预警网主通道。"""
    pass


@ParserRegistry.register("cea_pr_fanstudio")
class CEAEEWPRParser(CEAEEWParser):
    """中国地震预警网省级融合源。"""
    pass
