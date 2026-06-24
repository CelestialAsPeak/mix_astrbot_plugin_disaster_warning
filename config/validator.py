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
        """校验地震过滤器配置。

        从 _conf_schema.json 读取所有 filter 定义及其默认值，
        确保缺失的 filter 条目被补全。
        """
        if not isinstance(cfg, dict):
            return {}

        # ═══════════════════════════════════════
        # 第 1 步：迁移旧版 key（schema 默认值加载前执行）
        # 这样即使 schema 有同名新 key 也不会干扰迁移
        # ═══════════════════════════════════════
        _SHINDO_FILTERS = {"jma_scale_filter", "cwa_scale_filter", "snet_filter"}
        for source_id, filter_cfg in cfg.items():
            if not isinstance(filter_cfg, dict):
                continue
            if source_id in _SHINDO_FILTERS:
                # 震度过滤器：min_magnitude → min_shindo（移除 != 0.0 守卫，0.0 也需迁移）
                if "min_magnitude" in filter_cfg and "min_shindo" not in filter_cfg:
                    filter_cfg["min_shindo"] = filter_cfg.pop("min_magnitude")
            else:
                # 普通过滤器：min_intensity/烈度 → 最小烈度
                if "最小烈度" not in filter_cfg:
                    if "烈度" in filter_cfg:
                        filter_cfg["最小烈度"] = filter_cfg.pop("烈度")
                    elif "min_intensity" in filter_cfg:
                        filter_cfg["最小烈度"] = filter_cfg.pop("min_intensity")

        # ═══════════════════════════════════════
        # 第 2 步：从 _conf_schema.json 加载默认值（仅缺失字段补全）
        # ═══════════════════════════════════════
        try:
            import json
            from pathlib import Path
            _schema_path = Path(__file__).parent.parent / "_conf_schema.json"
            if _schema_path.exists():
                with open(_schema_path, encoding="utf-8") as _f:
                    _schema = json.load(_f)
                _sf = _schema.get("earthquake_filters", {}).get("items", {})
                for _fname, _fcfg in _sf.items():
                    if _fname not in cfg:
                        cfg[_fname] = {}
                    _entry = cfg[_fname]
                    _items = _fcfg.get("items", {})
                    for _field, _def in _items.items():
                        if _field == "enabled":
                            _entry.setdefault("enabled", _def.get("default", True))
                        elif "default" in _def:
                            _entry.setdefault(_field, _def["default"])
        except Exception:
            logger.warning("[Validator] 无法从 schema 加载 filter 默认值")

        # ═══════════════════════════════════════
        # 第 3 步：兜底 setdefault + 清理不应出现的旧 key 残留
        # ═══════════════════════════════════════
        for source_id, filter_cfg in cfg.items():
            if not isinstance(filter_cfg, dict):
                continue
            if source_id in _SHINDO_FILTERS:
                filter_cfg.setdefault("min_shindo", 0.5)
                # 震度过滤器不应有烈度字段
                for _old_k in ("最小烈度", "烈度", "min_intensity"):
                    filter_cfg.pop(_old_k, None)
            else:
                filter_cfg.setdefault("min_magnitude", 0.0)
                filter_cfg.setdefault("最小烈度", 0.0)
        return cfg


__all__ = ["ConfigValidator"]
