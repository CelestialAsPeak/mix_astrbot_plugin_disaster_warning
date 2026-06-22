"""
pipeline/dedup.py — 事件去重服务。

使用 EventIdentity.unique_key 作为指纹，
通过 TTL 缓存跟踪已处理的事件。
"""

from __future__ import annotations

import time
from typing import Any

try:
    from domain.models import EventEnvelope
except ImportError:
    from ..domain.models import EventEnvelope


class EventDeduplicator:
    """事件去重器 — 基于 unique_key 的 TTL 缓存。"""

    def __init__(self, ttl_seconds: int = 300):
        self._cache: dict[str, float] = {}
        self._ttl = ttl_seconds

    def is_duplicate(self, envelope: EventEnvelope) -> bool:
        """检查事件是否重复（已在 TTL 窗口内处理过）。"""
        key = envelope.identity.unique_key
        now = time.time()

        # 清除过期条目
        if key in self._cache and now - self._cache[key] > self._ttl:
            del self._cache[key]
            return False

        if key in self._cache:
            return True

        # 新事件，记录
        self._cache[key] = now
        return False

    def mark_processed(self, envelope: EventEnvelope) -> None:
        """标记事件为已处理。"""
        key = envelope.identity.unique_key
        self._cache[key] = time.time()

    def clean_expired(self) -> int:
        """清理过期缓存，返回清理条目数。"""
        now = time.time()
        expired = [k for k, t in self._cache.items() if now - t > self._ttl]
        for k in expired:
            del self._cache[k]
        return len(expired)

    @property
    def size(self) -> int:
        return len(self._cache)
