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

import math
from datetime import datetime, timezone, timedelta

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger

try:
    from .base_rule import BaseRule, RuleContext, RuleDecision
    from ..domain.models import EewEvent, EarthquakeReport, TsunamiEvent, WeatherEvent
except ImportError:
    from pipeline.base_rule import BaseRule, RuleContext, RuleDecision
    from domain.models import EewEvent, EarthquakeReport, TsunamiEvent, WeatherEvent


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
    """检查事件时间是否在有效范围内（避免过期事件推送）。

    注：部分数据源（如 FUNVISIS）返回列表型数据，事件可能跨越数天。
    放宽过期限制到 3 天，避免旧事件被误拦。
    """

    name = "event_time"
    MAX_AGE_SECONDS = 259200  # 3天（原 3600=1h，放宽后适配 FUNVISIS 等列表源）

    def evaluate(self, ctx: RuleContext) -> RuleDecision:
        event = ctx.event
        if hasattr(event, "occurred_at") and event.occurred_at:
            occurred = event.occurred_at
            # 统一转 UTC naive 再比较
            if occurred.tzinfo is not None:
                from datetime import timezone
                occurred = occurred.astimezone(timezone.utc).replace(tzinfo=None)
            age = (datetime.utcnow() - occurred).total_seconds()
            # 未来事件（age < 0）：可能是时区转换异常，放行并打警告
            if age < 0:
                logger.warning(
                    f"[EventTime] 事件时间 {event.occurred_at} 在未来 ({age:.0f}s)，"
                    f"可能是时区转换异常，已放行"
                )
                age = 0
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

        source_id = ctx.source_id
        for group_name, group_cfg in data_sources.items():
            if not isinstance(group_cfg, dict):
                continue
            if group_cfg.get("enabled", True) is False:
                # 检查 source_id 是否属于这个被禁用的组
                # 如果组内的源包含当前 source_id（模糊匹配），则拦截
                sources_in_group = group_cfg.get("sources", [])
                if isinstance(sources_in_group, list) and source_id in sources_in_group:
                    return RuleDecision.reject(
                        f"数据源 {source_id} 已被组 {group_name} 禁用", self.name)
                # 特殊情况：组配置的 key 等于 source_id 直接匹配
                if source_id == group_name:
                    return RuleDecision.reject(
                        f"数据源 {source_id} 已禁用", self.name)

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
        "usgs_fanstudio": "usgs_weekly_filter", "usgs_weekly": "usgs_weekly_filter",
        "funvisis_http": "funvisis_filter", "cenais_http": "cenais_filter",
        "csnc_http": "csnc_filter", "tmd_http": "tmd_filter",
        "phivolcs_http": "phivolcs_filter",
        "bmkg_http": "bmkg_filter",
        # snet_http 故意不加入 _FILTER_MAP：
        # SNET 是海底震度监测（非地震测定），其 min_magnitude 实际是测站级 shindo 阈值
        # 已经在 _fetch_snet_once() 中用于测站过滤，这里不再重复当震级阈值用
        "icl_http": "icl_filter",
        "sc_wolfx_eew": "sc_eew_filter", "fj_wolfx_eew": "fj_eew_filter",
        "cq_wolfx_eew": "cq_eew_filter",
        "cwa_wolfx_http": "cwa_scale_filter", "kma_wolfx_http": "kma_eew_filter",
        "sc_wolfx_http": "sc_eew_filter", "fj_wolfx_http": "fj_eew_filter",
        "cq_wolfx_http": "cq_eew_filter",
        # JMA/CWA 震度过滤器
        "jma_fanstudio": "jma_scale_filter", "jma_wolfx": "jma_scale_filter",
        "jma_wolfx_info": "jma_scale_filter", "jma_p2p": "jma_scale_filter",
        "jma_wolfx_http": "jma_scale_filter", "jma_wolfx_info_http": "jma_scale_filter",
        "jma_p2p_http": "jma_scale_filter", "jma_p2p_info_http": "jma_scale_filter",
        "cwa_fanstudio": "cwa_scale_filter", "cwa_wolfx": "cwa_scale_filter",
        # CENC EEW
        "cenc_eew_http": "cenc_eew_filter",
        "cenc_eew_province": "cenc_eew_province_filter",
    }

    def evaluate(self, ctx: RuleContext) -> RuleDecision:
        # SNET 是震度监测，非地震，用 snet_filter.min_shindo 对比
        if ctx.source_id in ("snet", "snet_http"):
            _sn_filters = ctx.config.get("earthquake_filters", {})
            _sn_cfg = _sn_filters.get("snet_filter", {})
            if isinstance(_sn_cfg, dict):
                _min_shindo = float(_sn_cfg.get("min_shindo", 0.5))
                # 从 metadata 获取触发测站的最高震度
                _sn_meta = ctx.envelope.metadata or {}
                _sn_triggered = _sn_meta.get("triggered", [])
                if _sn_triggered and isinstance(_sn_triggered, list):
                    _max_shindo = max(s.get("shindo", 0) for s in _sn_triggered)
                    if _max_shindo < _min_shindo:
                        return RuleDecision.reject(
                            f"snet_filter: 最大震度{_max_shindo}<{_min_shindo}", self.name)
            return RuleDecision.accept(rule_name=self.name)

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
        # P2P ScalePrompt 等无震源数据阶段用 sentinel -1，转为 None 避免比较
        if mag is not None and mag < 0:
            mag = None

        # EEW 跨报追踪峰值
        event_id = str(getattr(ctx.envelope, "id", "") or getattr(getattr(ctx.envelope, "identity", None), "event_id", "") or "")
        is_eew = bool(event_id and event_id != source_id)
        if is_eew and mag is not None:
            old = self._PEAK_MAG.get(event_id, -1.0)
            if mag > old:
                self._PEAK_MAG[event_id] = mag
            check_mag = self._PEAK_MAG.get(event_id, mag)
        else:
            check_mag = mag
        if len(self._PEAK_MAG) > 500:
            for k in list(self._PEAK_MAG.keys())[:-100]:
                self._PEAK_MAG.pop(k, None)

        # 1) 源级独立过滤器（对齐旧插件 _SOURCE_FILTER_MAP）
        filter_key = self._FILTER_MAP.get(source_id)
        if filter_key:
            f = filters.get(filter_key, {})
            if not f.get("enabled", True):
                return RuleDecision.accept(rule_name=self.name)
            # 优先读取用户通过 schema UI 配置的直接源阈值 (earthquake_filters.{source_id})
            direct_cfg = filters.get(source_id)
            if isinstance(direct_cfg, dict) and "min_magnitude" in direct_cfg:
                min_mag = direct_cfg["min_magnitude"]
            else:
                min_mag = f.get("min_magnitude", 4.5)

            # GlobalQuake 地名白名单：中文地名包含关键词则直接推送
            if source_id == "global_quake":
                whitelist_str = f.get("gq_place_whitelist", "")
                if whitelist_str:
                    place = (ctx.event.place_name or "").strip()
                    if place:
                        keywords = [w.strip() for w in whitelist_str.split(",") if w.strip()]
                        for kw in keywords:
                            if kw in place:
                                logger.info(f"[GQ] 白名单命中「{kw}」→ {place}，跳过阈值检查")
                                return RuleDecision.accept(rule_name=self.name)

            # JMA/CWA 震度过滤器：min_magnitude OR min_shindo
            if filter_key in ("jma_scale_filter", "cwa_scale_filter"):
                if isinstance(direct_cfg, dict) and "min_shindo" in direct_cfg:
                    min_shindo_val = direct_cfg["min_shindo"]
                else:
                    min_shindo_val = f.get("min_shindo", 0)
                shindo = self._get_intensity(ctx)  # JMA/CWA 的 max_intensity = 震度
                mag_ok = (min_mag <= 0 or (check_mag is None and isinstance(ctx.event, EewEvent)) or (check_mag is not None and check_mag >= min_mag))
                shindo_ok = (min_shindo_val > 0 and shindo is not None and shindo >= min_shindo_val)
                if mag_ok or shindo_ok:
                    return RuleDecision.accept(rule_name=self.name)
                return RuleDecision.reject(
                    f"{filter_key}: 震级{check_mag}<{min_mag} 且 震度{shindo}<{min_shindo_val}", self.name)
            # 普通过滤器：震级 OR 烈度（对齐 section 2/3 的 OR 逻辑）
            if isinstance(direct_cfg, dict):
                min_int = self._get_field(direct_cfg, "最小烈度", "烈度", "min_intensity", default=0)
            else:
                min_int = self._get_field(f, "最小烈度", "烈度", "min_intensity", default=0)
            intensity = self._get_intensity(ctx)
            mag_ok = (min_mag <= 0 or (check_mag is None and isinstance(ctx.event, EewEvent)) or (check_mag is not None and check_mag >= min_mag))
            # min_int=0 表示未配置烈度阈值，不覆盖震级检查
            int_ok = (min_int > 0 and intensity is not None and intensity >= min_int)
            if mag_ok or int_ok:
                return RuleDecision.accept(rule_name=self.name)
            return RuleDecision.reject(
                f"{filter_key}: 震级{check_mag}<{min_mag} 且 烈度{intensity}<{min_int}", self.name)

        # 2) earthquake_filters.{source_id} 直接配置
        direct = filters.get(source_id)
        if isinstance(direct, dict):
            min_mag = direct.get("min_magnitude", 0)
            min_int = self._get_field(direct, "最小烈度", "烈度", "min_intensity", default=0)
            intensity = self._get_intensity(ctx)
            # OR逻辑：震级够 或 烈度够 即可推送
            mag_ok = (min_mag <= 0 or (check_mag is None and isinstance(ctx.event, EewEvent)) or (check_mag is not None and check_mag >= min_mag))
            int_ok = (min_int > 0 and intensity is not None and intensity >= min_int)
            if mag_ok or int_ok:
                return RuleDecision.accept(rule_name=self.name)
            return RuleDecision.reject(
                f"{source_id}: 震级{check_mag}<{min_mag} 且 烈度{intensity}<{min_int}", self.name)

        # 3) 全局兜底 intensity_filter / magnitude_only_filter / scale_filter
        for gf_name in ("intensity_filter", "magnitude_only_filter", "scale_filter"):
            gf = filters.get(gf_name)
            if not isinstance(gf, dict):
                continue
            min_mag = gf.get("min_magnitude", 0)
            min_int = self._get_field(gf, "最小烈度", "烈度", "min_intensity", default=0)
            intensity = self._get_intensity(ctx)
            # OR逻辑：震级够 或 烈度够 即通过
            mag_ok = (min_mag <= 0 or (check_mag is None and isinstance(ctx.event, EewEvent)) or (check_mag is not None and check_mag >= min_mag))
            int_ok = (min_int > 0 and intensity is not None and intensity >= min_int)
            if mag_ok or int_ok:
                continue  # 当前过滤器通过，继续检查下一个
            return RuleDecision.reject(
                f"{gf_name}: 震级{check_mag}<{min_mag} 且 烈度{intensity}<{min_int}", self.name)
        return RuleDecision.accept(rule_name=self.name)

    @staticmethod
    def _get_field(cfg: dict, *keys: str, default=0):
        """从配置中读取字段值，支持多个 key 名（新名优先，旧名兜底）。"""
        for k in keys:
            if k in cfg:
                return cfg[k]
        return default

    @staticmethod
    def _estimate_csis(mag: float, depth: float) -> float:
        """基于 CAPQuakeQt CSIS 公式估算震中烈度。"""
        R = 6371.0
        fault_len = 10 ** ((mag - 3.821) / 1.86)
        hypo_dis = max(
            depth - 10.0 - fault_len,
            0.0 - fault_len,
            0.2 * (depth - 10.0),
            0.0
        )
        cea1 = 1.297 * mag - 4.368 * math.log10(depth + 15.0) + 5.363
        cea2 = 1.297 * mag - 4.368 * math.log10(hypo_dis + 15.0) + 5.363
        return (cea1 + cea2) / 2.0

    @staticmethod
    def _get_intensity(ctx, use_estimate=True):
        if isinstance(ctx.event, EewEvent):
            try:
                return float(ctx.event.max_intensity)
            except (TypeError, ValueError):
                pass
        # 没有实测烈度时，用 CSIS 公式估算（CWA/JMA 除外）
        if use_estimate:
            mag = getattr(ctx.event, "magnitude", None)
            depth = getattr(ctx.event, "depth", None)
            if mag is not None and depth is not None:
                sid = ctx.source_id
                if not sid.startswith("cwa_") and not sid.startswith("jma_"):
                    try:
                        return EarthquakeThresholdRule._estimate_csis(mag, depth)
                    except Exception:
                        pass
        return None


# ─── 6. 报次规则 + 推送频率控制 ───

class ReportRule(BaseRule):
    """报次策略 — 首报/续报/终报过滤 + EEW 推送频率控制。"""

    name = "report"

    # 来源 → 频率分组映射
    _FREQ_GROUPS: dict[str, str] = {
        "cea_fanstudio": "cea_cwa", "cea_pr_fanstudio": "cea_cwa",
        "sc_wolfx_eew": "cea_cwa", "fj_wolfx_eew": "cea_cwa", "cq_wolfx_eew": "cea_cwa",
        "cwa_fanstudio": "cea_cwa", "cwa_wolfx": "cea_cwa",
        "jma_fanstudio": "jma", "jma_wolfx": "jma", "jma_wolfx_info": "jma",
        "jma_wolfx_http": "jma", "jma_wolfx_info_http": "jma",
        "jma_p2p": "jma", "jma_p2p_http": "jma", "jma_p2p_info_http": "jma",
        "global_quake": "gq",
    }

    def evaluate(self, ctx: RuleContext) -> RuleDecision:
        if not isinstance(ctx.event, (EewEvent, EarthquakeReport)):
            return RuleDecision.accept(rule_name=self.name)

        # ── strategies.report_strategy（与旧策略兼容） ──
        strategies = ctx.config.get("strategies", {})
        if isinstance(strategies, dict):
            report_strategy = strategies.get("report_strategy", "all")
            report_num = ctx.event.report_num if hasattr(ctx.event, "report_num") else None
            is_final = ctx.event.is_final if hasattr(ctx.event, "is_final") else None

            if report_strategy == "first_only" and report_num and report_num > 1:
                return RuleDecision.reject(f"非首报 (第 {report_num} 报)", self.name)
            if report_strategy == "final_only" and not is_final:
                return RuleDecision.reject("非终报", self.name)

        # ── push_frequency_control（EEW 报次限频） ──
        freq = ctx.config.get("push_frequency_control", {})
        if not isinstance(freq, dict):
            return RuleDecision.accept(rule_name=self.name)

        group = self._FREQ_GROUPS.get(ctx.source_id)
        if group is None:
            return RuleDecision.accept(rule_name=self.name)

        report_num = ctx.event.report_num if hasattr(ctx.event, "report_num") else None
        is_final = ctx.event.is_final if hasattr(ctx.event, "is_final") else None

        # 没有报次信息 → 无法限频，放行
        if report_num is None:
            return RuleDecision.accept(rule_name=self.name)

        # 最终报告优先推送（如果开启）
        if is_final and freq.get("final_report_always_push", True):
            return RuleDecision.accept(rule_name=self.name)

        # 首报始终推送
        if report_num <= 1:
            return RuleDecision.accept(rule_name=self.name)

        # ignore_non_final_reports（仅 JMA）：跳过所有非终报
        if group == "jma" and freq.get("ignore_non_final_reports", False):
            return RuleDecision.reject(
                f"JMA 忽略非最终报 (第 {report_num} 报)", self.name,
            )

        # 按分组读取 N 值
        n_map = {"cea_cwa": "cea_cwa_report_n", "jma": "jma_report_n", "gq": "gq_report_n"}
        key = n_map.get(group, "")
        n = int(freq.get(key, 1) or 1)

        # N=1 表示每次都推
        if n <= 1:
            return RuleDecision.accept(rule_name=self.name)

        # 每 N 报推一次：report_num % N == 0 时推
        if report_num % n == 0:
            return RuleDecision.accept(rule_name=self.name)

        return RuleDecision.reject(
            f"{group} 报次限频: 第 {report_num} 报跳过 (每 {n} 报推一次)", self.name,
        )


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
