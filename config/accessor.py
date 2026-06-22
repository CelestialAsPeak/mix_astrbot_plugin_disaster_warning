"""
ConfigAccessor — 统一配置访问服务。

封装常见配置分组的读取逻辑，作为运行时配置的标准入口。
避免上层重复书写键名和类型兜底逻辑。

配置层级（优先级从高到低）：
  1. 运行时 config 字典（由插件主入口注入）
  2. defaults.json 编译默认值
"""

from __future__ import annotations

from typing import Any


class ConfigAccessor:
    """统一配置访问服务。"""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}

    # ── 顶层分组 ──

    def get(self, key: str, default: Any = None) -> Any:
        """通用配置读取。"""
        return self.config.get(key, default)

    def web_admin_config(self) -> dict[str, Any]:
        return self._dict("web_admin")

    def message_format_config(self) -> dict[str, Any]:
        return self._dict("message_format")

    def event_deduplication_config(self) -> dict[str, Any]:
        return self._dict("event_deduplication")

    def weather_config(self) -> dict[str, Any]:
        return self._dict("weather_config")

    def debug_config(self) -> dict[str, Any]:
        return self._dict("debug_config")

    def local_monitoring_config(self) -> dict[str, Any]:
        return self._dict("local_monitoring")

    def data_sources_config(self) -> dict[str, Any]:
        """获取数据源配置总表。"""
        return self._dict("data_sources")

    def strategies_config(self) -> dict[str, Any]:
        return self._dict("strategies")

    def target_sessions(self) -> list[Any]:
        value = self.config.get("target_sessions", [])
        return value if isinstance(value, list) else []

    def web_admin_password(self) -> str:
        password = self.web_admin_config().get("password", "")
        return password if isinstance(password, str) else ""

    # ── 内部辅助 ──

    def _dict(self, key: str) -> dict[str, Any]:
        value = self.config.get(key, {})
        return value if isinstance(value, dict) else {}


__all__ = ["ConfigAccessor"]
