"""
storage/session.py — 会话配置管理器。

管理每个会话的推送订阅和个性化配置。
"""

from __future__ import annotations

from typing import Any


class SessionConfigManager:
    """会话配置管理器。"""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self._session_configs: dict[str, dict] = {}

    def set_session_config(self, session_id: str, cfg: dict) -> None:
        """设置会话配置。"""
        self._session_configs[session_id] = dict(cfg)

    def get_effective_config(self, session_id: str) -> dict:
        """获取会话生效配置（会话级覆盖全局）。"""
        base = dict(self.config)
        session_cfg = self._session_configs.get(session_id, {})
        base.update(session_cfg)
        return base

    def list_target_sessions(self) -> list[str]:
        """获取所有目标会话。"""
        sessions = self.config.get("target_sessions", [])
        return list(sessions) if isinstance(sessions, list) else []

    def is_subscribed(self, session_id: str) -> bool:
        """检查会话是否已订阅推送。"""
        return session_id in self.list_target_sessions()
