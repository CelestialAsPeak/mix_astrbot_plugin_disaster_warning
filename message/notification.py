"""
message/notification.py — 系统通知服务。
"""

from __future__ import annotations

from typing import Any


class SystemNotificationService:
    """系统通知服务。"""

    def __init__(self, push_manager=None):
        self._push_manager = push_manager

    async def notify_offline(self, source_name: str) -> None:
        """数据源离线通知。"""
        if self._push_manager:
            text = f"⚠️ 数据源离线: {source_name}"
            await self._push_manager.send_to_admin(text)

    async def notify_reconnect(self, source_name: str) -> None:
        """重连成功通知。"""
        if self._push_manager:
            text = f"✅ 数据源重连成功: {source_name}"
            await self._push_manager.send_to_admin(text)

    async def notify_system(self, message: str) -> None:
        """系统通知。"""
        if self._push_manager:
            await self._push_manager.send_to_admin(f"ℹ️ {message}")


class NotificationCenter:
    """通知管理中心（Web 管理端用）。"""

    def __init__(self):
        self._notifications: list[dict] = []
        self._max_size = 200

    def add(self, title: str, message: str, level: str = "info") -> None:
        self._notifications.append({
            "title": title,
            "message": message,
            "level": level,
            "time": __import__("time").time(),
        })
        if len(self._notifications) > self._max_size:
            self._notifications.pop(0)

    def get_all(self) -> list[dict]:
        return list(self._notifications)

    def clear(self) -> None:
        self._notifications.clear()
