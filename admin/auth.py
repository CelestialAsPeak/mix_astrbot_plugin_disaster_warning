"""
admin/auth.py — 管理端认证。
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any


class AuthManager:
    """管理端认证管理器。"""

    def __init__(self, password: str = ""):
        self._password = password
        self._enabled = bool(password)
        self._tokens: dict[str, bool] = {}

    def authenticate(self, password: str) -> str | None:
        """验证密码，返回 token。"""
        if not self._enabled:
            return "public_token"
        if password == self._password:
            token = secrets.token_hex(16)
            self._tokens[token] = True
            return token
        return None

    def validate_token(self, token: str) -> bool:
        """验证 token。"""
        if not self._enabled:
            return True
        return token in self._tokens

    def revoke_token(self, token: str) -> None:
        self._tokens.pop(token, None)
