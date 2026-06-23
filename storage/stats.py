"""
storage/stats.py — 事件统计管理器。

合并旧项目 10 个 stats 文件的逻辑为单个文件。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

try:
    from ..domain.models import EventEnvelope
except ImportError:
    from domain.models import EventEnvelope


class StatisticsManager:
    """事件统计管理器。"""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self._stats: dict[str, Any] = {
            "total_events": 0,
            "pushed_events": 0,
            "sources": {},
            "by_type": {},
            "daily": {},
        }

    async def record_push(
        self,
        envelope: EventEnvelope,
        pushed: bool = False,
        pushed_sessions: list[str] | None = None,
    ) -> None:
        """记录推送统计。"""
        self._stats["total_events"] += 1
        if pushed:
            self._stats["pushed_events"] += 1

        # 按数据源
        src = envelope.source_id
        if src not in self._stats["sources"]:
            self._stats["sources"][src] = {"total": 0, "pushed": 0}
        self._stats["sources"][src]["total"] += 1
        if pushed:
            self._stats["sources"][src]["pushed"] += 1

        # 按类型
        etype = envelope.event_type
        self._stats["by_type"][etype] = self._stats["by_type"].get(etype, 0) + 1

        # 按日
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if day not in self._stats["daily"]:
            self._stats["daily"][day] = 0
        self._stats["daily"][day] += 1

    def get_stats(self) -> dict[str, Any]:
        """获取统计快照。"""
        return dict(self._stats)

    def reset(self) -> None:
        """重置统计。"""
        self._stats = {
            "total_events": 0,
            "pushed_events": 0,
            "sources": {},
            "by_type": {},
            "daily": {},
        }
