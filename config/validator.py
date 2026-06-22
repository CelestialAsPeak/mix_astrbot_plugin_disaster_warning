"""
ConfigValidator — 配置校验器。

负责对插件配置进行合法性校验、范围修正和默认值填充。
作为运行时读取配置前的最后一道结构整理入口。
"""

from __future__ import annotations

from typing import Any

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger


class ConfigValidator:
    """配置校验器。"""

    @staticmethod
    def validate(config: dict[str, Any]) -> dict[str, Any]:
        """执行全部配置校验，返回修正后的配置副本。"""
        result = dict(config)

        if "local_monitoring" in result:
            result["local_monitoring"] = ConfigValidator._validate_local_monitoring(
                result["local_monitoring"]
            )
        if "web_admin" in result:
            result["web_admin"] = ConfigValidator._validate_web_admin(
                result["web_admin"]
            )
        if "strategies" in result:
            result["strategies"] = ConfigValidator._validate_strategies(
                result["strategies"]
            )
        if "weather_config" in result:
            result["weather_config"] = ConfigValidator._validate_weather_config(
                result["weather_config"]
            )
        if "debug_config" in result:
            result["debug_config"] = ConfigValidator._validate_debug_config(
                result["debug_config"]
            )
        if "earthquake_filters" in result:
            result["earthquake_filters"] = ConfigValidator._validate_earthquake_filters(
                result["earthquake_filters"]
            )

        return result

    @staticmethod
    def _validate_local_monitoring(cfg: Any) -> dict:
        """校验本地监控配置。"""
        if not isinstance(cfg, dict):
            return {"enabled": False}
        cfg.setdefault("enabled", False)
        cfg.setdefault("interval", 60)
        return cfg

    @staticmethod
    def _validate_web_admin(cfg: Any) -> dict:
        """校验 Web 管理端配置。"""
        if not isinstance(cfg, dict):
            return {"enabled": False}
        cfg.setdefault("enabled", False)
        cfg.setdefault("host", "127.0.0.1")
        cfg.setdefault("port", 18331)
        cfg.setdefault("password", "")
        return cfg

    @staticmethod
    def _validate_strategies(cfg: Any) -> dict:
        """校验策略配置。"""
        if not isinstance(cfg, dict):
            return {}
        for key in ("cenc_fusion", "cwa_eew_fusion"):
            sub = cfg.get(key, {})
            if isinstance(sub, dict):
                sub.setdefault("enabled", False)
        return cfg

    @staticmethod
    def _validate_weather_config(cfg: Any) -> dict:
        """校验气象配置。"""
        if not isinstance(cfg, dict):
            return {}
        cfg.setdefault("mention_province", False)
        return cfg

    @staticmethod
    def _validate_debug_config(cfg: Any) -> dict:
        """校验调试配置。"""
        if not isinstance(cfg, dict):
            return {}
        cfg.setdefault("startup_silence_duration", 0)
        return cfg

    @staticmethod
    def _validate_earthquake_filters(cfg: Any) -> dict:
        """校验地震过滤器配置。"""
        if not isinstance(cfg, dict):
            return {}
        for source_id, filter_cfg in cfg.items():
            if isinstance(filter_cfg, dict):
                filter_cfg.setdefault("min_magnitude", 0.0)
                filter_cfg.setdefault("min_intensity", 0.0)
        return cfg


__all__ = ["ConfigValidator"]
