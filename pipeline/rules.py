"""
pipeline/rules.py — 8 个推送过滤规则。

规则执行顺序:
  0. BanWeatherRule    — 硬拦截气象预警（已弃用）
  1. EventTimeRule     — 事件时间有效性检查
  2. SourceEnabledRule  — 数据源启用开关检查
  3. WeatherRule        — 气象预警过滤（按省份/类型/级别）
  4. KeywordRule        — 关键词匹配（自定义关键词过滤）
  5. EarthquakeThresholdRule — 震级/烈度阈值过滤
  6. ReportRule         — 报次策略（首报/续报/终报过滤）
  7. LocalIntensityRule — 本地烈度阈值过滤
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger

try:
    from pipeline.base_rule import BaseRule, RuleContext, RuleDecision
    from domain.models import EewEvent, EarthquakeReport, TsunamiEvent, WeatherEvent
except ImportError:
    from .base_rule import BaseRule, RuleContext, RuleDecision
    from ..domain.models import EewEvent, EarthquakeReport, TsunamiEvent, WeatherEvent


# ─── 0. 气象预警硬拦截（已弃用） ───

class BanWeatherRule(BaseRule):
    """硬拦截所有气象预警 — 已弃用该数据源。"""

    name = "ban_weather"

    def evaluate(self, ctx: RuleContext) -> RuleDecision:
        if isinstance(ctx.event, WeatherEvent):
            return RuleDecision.reject("气象预警数据源已弃用", self.name)
        return RuleDecision.accept(rule_name=self.name)


# ─── 1. 事件时间规则 ───

class EventTimeRule(BaseRule):
    """检查事件时间是否在有效范围内（避免过期事件推送）。"""

    name = "event_time"
    MAX_AGE_SECONDS = 3600  # 1小时以上的事件忽略

    def evaluate(self, ctx: RuleContext) -> RuleDecision:
        event = ctx.event
        if hasattr(event, "occurred_at") and event.occurred_at:
            # parse_ts 返回 timezone-aware datetime，统一转 naive 再比较
            occurred = event.occurred_at
            if occurred.tzinfo is not None:
                occurred = occurred.replace(tzinfo=None)
            age = (datetime.utcnow() - occurred).total_seconds()
            if age > self.MAX_AGE_SECONDS:
                return RuleDecision.reject(
                    f"事件时间 {event.occurred_at} 已过期 ({age:.0f}s > {self.MAX_AGE_SECONDS}s)",
                    self.name,
                )
        return RuleDecision.accept(rule_name=self.name)


# ─── 2. 数据源启用规则 ───

class SourceEnabledRule(BaseRule):
    """检查数据源是否在配置中启用。"""

    name = "source_enabled"

    def evaluate(self, ctx: RuleContext) -> RuleDecision:
        data_sources = ctx.config.get("data_sources", {})
        if not isinstance(data_sources, dict):
            return RuleDecision.accept(rule_name=self.name)

        # 从 sources.json 获取 config_group + config_key
        # 这里简化：直接检查 data_sources 下有没有这个源的开关
        source_id = ctx.source_id
        for group_name, group_cfg in data_sources.items():
            if isinstance(group_cfg, dict):
                if group_cfg.get("enabled", True) is False:
                    # 如果整个组被禁用，组内所有源都禁用
                    # 需要知道 source 属于哪个组
                    # 这里由上层（pipeline）在构建 RuleContext 时注入
                    pass

        return RuleDecision.accept(rule_name=self.name)


# ─── 3. 气象预警规则 ───

class WeatherRule(BaseRule):
    """气象预警过滤 — 按省份、类型、级别。"""

    name = "weather"

    def evaluate(self, ctx: RuleContext) -> RuleDecision:
        if not isinstance(ctx.event, WeatherEvent):
            return RuleDecision.accept(rule_name=self.name)

        weather_cfg = ctx.config.get("weather_config", {})
        if not isinstance(weather_cfg, dict):
            return RuleDecision.accept(rule_name=self.name)

        # 级别过滤
        min_level = weather_cfg.get("min_alert_level", "")
        if min_level and ctx.event.alert_level:
            level_order = {"蓝色": 1, "黄色": 2, "橙色": 3, "红色": 4}
            if level_order.get(ctx.event.alert_level, 0) < level_order.get(min_level, 0):
                return RuleDecision.reject(
                    f"气象等级 {ctx.event.alert_level} < 最低 {min_level}",
                    self.name,
                )

        return RuleDecision.accept(rule_name=self.name)


# ─── 4. 关键词规则 ───

class KeywordRule(BaseRule):
    """关键词匹配 — 地震预警中的地名/关键词过滤。"""

    name = "keyword"

    def evaluate(self, ctx: RuleContext) -> RuleDecision:
        filters = ctx.config.get("earthquake_filters", {})
        if not isinstance(filters, dict):
            return RuleDecision.accept(rule_name=self.name)

        source_cfg = filters.get(ctx.source_id, {})
        if not isinstance(source_cfg, dict):
            return RuleDecision.accept(rule_name=self.name)

        keywords = source_cfg.get("keywords", [])
        if not keywords:
            return RuleDecision.accept(rule_name=self.name)

        # 如果配置了 allowlist 关键词，事件地名必须匹配其中至少一个
        place = ""
        if hasattr(ctx.event, "place_name"):
            place = ctx.event.place_name or ""
        elif hasattr(ctx.event, "headline"):
            place = ctx.event.headline or ""

        if keywords and place:
            matched = any(kw in place for kw in keywords)
            if not matched and source_cfg.get("keyword_mode", "allow") == "allow":
                return RuleDecision.reject(
                    f"地名 '{place}' 不匹配关键词列表", self.name
                )

        return RuleDecision.accept(rule_name=self.name)


# ─── 5. 震级阈值规则 ───

class EarthquakeThresholdRule(BaseRule):
    """震级/烈度阈值过滤（对齐旧插件策略）。"""

    name = "threshold"
    _PEAK_MAG: dict[str, float] = {}
    _FILTER_MAP: dict[str, str] = {
        "global_quake": "global_quake_filter", "emsc_fanstudio": "emsc_filter",
        "gfz_fanstudio": "gfz_filter", "bcsf_fanstudio": "bcsf_filter",
        "usp_fanstudio": "usp_filter", "hko_fanstudio": "hko_filter",
        "fssn_fanstudio": "fssn_filter", "kma_fanstudio": "kma_filter",
        "sa_fanstudio": "sa_filter", "kma_eew_fanstudio": "kma_eew_filter",
        "ningxia_fanstudio": "ningxia_filter", "guangxi_fanstudio": "guangxi_filter",
        "shanxi_fanstudio": "shanxi_filter", "beijing_fanstudio": "beijing_filter",
        "yunnan_fanstudio": "yunnan_filter",
        "geonet_http": "geonet_filter", "nrcan_http": "nrcan_filter",
        "usgs_weekly": "usgs_weekly_filter",
        "funvisis_http": "funvisis_filter", "cenais_http": "cenais_filter",
        "csnc_http": "csnc_filter", "tmd_http": "tmd_filter",
        "phivolcs_http": "phivolcs_filter", "snet_http": "snet_filter",
        "icl_http": "icl_filter",
        "sc_wolfx_eew": "sc_eew_filter", "fj_wolfx_eew": "fj_eew_filter",
        "cq_wolfx_eew": "cq_eew_filter",
    }

    def evaluate(self, ctx: RuleContext) -> RuleDecision:
        if not isinstance(ctx.event, (EewEvent, EarthquakeReport)):
            return RuleDecision.accept(rule_name=self.name)

        filters = ctx.config.get("earthquake_filters", {})
        if not isinstance(filters, dict):
            return RuleDecision.accept(rule_name=self.name)

        source_cfg = filters.get(ctx.source_id, {})
        if not isinstance(source_cfg, dict):
            return RuleDecision.accept(rule_name=self.name)

        min_mag = source_cfg.get("min_magnitude", 0)
        min_int = source_cfg.get("min_intensity", 0)

        # 读配置过滤器
        filters = ctx.config.get("earthquake_filters", {})
        if not isinstance(filters, dict):
            filters = {}

        source_id = ctx.source_id
        mag = ctx.event.magnitude

        # EEW 跨报追踪峰值
        event_id = str(getattr(ctx.envelope, "id", "") or getattr(getattr(ctx.envelope, "identity", None), "event_id", "") or "")
        is_eew = bool(event_id and event_id != source_id)
        if is_eew and mag is not None:
            old = self._PEAK_MAG.get(event_id, -1.0)
            if mag > old:
                self._PEAK_MAG[event_id] = mag
            check_mag = self._PEAK_MAG[event_id]
        else:
            check_mag = mag
        if len(self._PEAK_MAG) > 500:
            for k in list(self._PEAK_MAG.keys())[:-100]:
                self._PEAK_MAG.pop(k, None)

        # 1) 源级独立过滤器（对齐旧插件 _SOURCE_FILTER_MAP）
        filter_key = self._FILTER_MAP.get(source_id)
        if filter_key:
            f = filters.get(filter_key, {})
            if f.get("enabled", True):
                min_mag = f.get("min_magnitude", 4.5)
                if check_mag is not None and check_mag < min_mag:
                    return RuleDecision.reject(f"{filter_key}: {check_mag} < {min_mag}", self.name)
            return RuleDecision.accept(rule_name=self.name)

        # 2) earthquake_filters.{source_id} 直接配置
        direct = filters.get(source_id)
        if isinstance(direct, dict):
            min_mag = direct.get("min_magnitude", 0)
            min_int = direct.get("min_intensity", 0)
            if min_mag > 0 and check_mag is not None and check_mag < min_mag:
                return RuleDecision.reject(f"{source_id}: {check_mag} < {min_mag}", self.name)
            intensity = self._get_intensity(ctx)
            if min_int > 0 and intensity is not None and intensity < min_int:
                return RuleDecision.reject(f"{source_id} i {intensity} < {min_int}", self.name)
            return RuleDecision.accept(rule_name=self.name)

        # 3) 全局兜底 intensity_filter / magnitude_only_filter
        for gf_name in ("intensity_filter", "magnitude_only_filter", "scale_filter"):
            gf = filters.get(gf_name)
            if not isinstance(gf, dict):
                continue
            min_mag = gf.get("min_magnitude", 0)
            if min_mag > 0 and check_mag is not None and check_mag < min_mag:
                return RuleDecision.reject(f"{gf_name}: {check_mag} < {min_mag}", self.name)
            min_int = gf.get("min_intensity", 0)
            if min_int > 0:
                intensity = self._get_intensity(ctx)
                if intensity is not None and intensity < min_int:
                    return RuleDecision.reject(f"{gf_name}: i {intensity} < {min_int}", self.name)
            if min_mag > 0 or min_int > 0:
                break
        return RuleDecision.accept(rule_name=self.name)

    @staticmethod
    def _get_intensity(ctx):
        if isinstance(ctx.event, EewEvent):
            try:
                return float(ctx.event.max_intensity)
            except (TypeError, ValueError):
                pass
        return None


# ─── 6. 报次规则 ───

class ReportRule(BaseRule):
    """报次策略 — 首报/续报/终报过滤。"""

    name = "report"

    def evaluate(self, ctx: RuleContext) -> RuleDecision:
        if not isinstance(ctx.event, (EewEvent, EarthquakeReport)):
            return RuleDecision.accept(rule_name=self.name)

        strategies = ctx.config.get("strategies", {})
        if not isinstance(strategies, dict):
            return RuleDecision.accept(rule_name=self.name)

        report_strategy = strategies.get("report_strategy", "all")
        if report_strategy == "all":
            return RuleDecision.accept(rule_name=self.name)

        report_num = ctx.event.report_num if hasattr(ctx.event, "report_num") else None
        is_final = ctx.event.is_final if hasattr(ctx.event, "is_final") else None

        if report_strategy == "first_only" and report_num and report_num > 1:
            return RuleDecision.reject(f"非首报 (第 {report_num} 报)", self.name)

        if report_strategy == "final_only" and not is_final:
            return RuleDecision.reject("非终报", self.name)

        return RuleDecision.accept(rule_name=self.name)


# ─── 7. 本地烈度规则 ───

class LocalIntensityRule(BaseRule):
    """本地烈度阈值（按会话/地区）。"""

    name = "local_intensity"

    def evaluate(self, ctx: RuleContext) -> RuleDecision:
        if not isinstance(ctx.event, (EewEvent, EarthquakeReport)):
            return RuleDecision.accept(rule_name=self.name)

        # 会话配置覆盖
        session_cfg = ctx.session_config or {}
        min_local = session_cfg.get("min_local_intensity", 0)

        if min_local <= 0:
            return RuleDecision.accept(rule_name=self.name)

        # 需要计算本地烈度（基于震中距）
        # 会话配置中可能包含用户所在地坐标
        user_lat = session_cfg.get("latitude")
        user_lon = session_cfg.get("longitude")

        if user_lat is not None and user_lon is not None and ctx.event.latitude is not None:
            try:
                from utils.geo import haversine_km
            except ImportError:
                from ..utils.geo import haversine_km
            distance = haversine_km(
                user_lat, user_lon,
                ctx.event.latitude, ctx.event.longitude,
            )
            # 粗略估算本地烈度（参考）
            mag = ctx.event.magnitude or 0
            local_intensity = max(0, mag - 2 * (distance / 100.0) ** 0.5)
            if local_intensity < min_local:
                return RuleDecision.reject(
                    f"本地估算烈度 {local_intensity:.1f} < {min_local} (距离 {distance:.0f}km)",
                    self.name,
                )

        return RuleDecision.accept(rule_name=self.name)


# ─── 构建默认规则链 ───

def build_default_rule_chain():
    """构建默认推送规则链。"""
    try:
        from pipeline.base_rule import RuleChain
    except ImportError:
        from .base_rule import RuleChain
    return RuleChain([
        BanWeatherRule(),
        EventTimeRule(),
        SourceEnabledRule(),
        WeatherRule(),
        KeywordRule(),
        EarthquakeThresholdRule(),
        ReportRule(),
        LocalIntensityRule(),
    ])


__all__ = [
    "BanWeatherRule",
    "EventTimeRule",
    "SourceEnabledRule",
    "WeatherRule",
    "KeywordRule",
    "EarthquakeThresholdRule",
    "ReportRule",
    "LocalIntensityRule",
    "build_default_rule_chain",
]
