"""
message/presenters.py — 消息展示格式器。

将各类型领域事件格式化为用户可读的文本消息。
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger

try:
    from ..domain.models import (
        EewEvent, EarthquakeReport, TsunamiEvent,
        WeatherEvent, TyphoonEvent, EventEnvelope,
    )
except ImportError:
    from domain.models import (
        EewEvent, EarthquakeReport, TsunamiEvent,
        WeatherEvent, TyphoonEvent, EventEnvelope,
    )


# ── 气象预警类型和等级映射 ──

WEATHER_TYPE_MAP: dict[str, str] = {
    "p0002001": "台风", "p0002002": "暴雨", "p0002003": "暴雪",
    "p0002004": "寒潮", "p0002005": "大风", "p0002006": "沙尘暴",
    "p0002007": "高温", "p0002008": "干旱", "p0002009": "雷电",
    "p0002010": "冰雹", "p0002011": "霜冻", "p0002012": "大雾",
    "p0002013": "霾", "p0002014": "道路结冰",
}

LEVEL_COLORS: dict[str, str] = {
    "蓝色": "🔵", "黄色": "🟡", "橙色": "🟠", "红色": "🔴",
}


# ── 源机构信息（从 sources.json 加载） ──

_SOURCE_DISPLAY_INFO: dict[str, dict] = {}
try:
    _sources_path = Path(__file__).parent.parent / "config" / "sources.json"
    if _sources_path.exists():
        with open(_sources_path, encoding="utf-8") as _f:
            _sources_data = json.load(_f)
        for _sid, _entry in _sources_data.items():
            if _sid.startswith("_"):
                continue
            _dn = _entry.get("display_name", "") or ""
            # CODE: 始终用 institution_key（比 display_name 的 (CODE) 更可靠）
            _ik = _entry.get("institution_key", "") or ""
            _code = _ik.upper() if _ik else ""
            # NAME: 去掉 display_name 末尾 (CODE) 后缀（如有）
            _m = re.search(r"\(([A-Z0-9+/]+)\)$", _dn)
            _name = _dn[:_m.start()].strip() if _m else _dn
            _SOURCE_DISPLAY_INFO[_sid] = {"code": _code, "name": _name}
except Exception:
    logger.warning("[Presenters] sources.json 加载失败")


# ── 各源事件标签映射（产品名） ──
# EEW 源的"地震预警/緊急地震速報/强震即时预警"等
# 不在此表中的源 fallback 到调用时传入的通用标签

_EVENT_LABELS: dict[str, str] = {
    "cea_fanstudio": "地震预警",
    "cwa_fanstudio": "强震即时预警",
    "jma_fanstudio": "緊急地震速報",
    "sa_fanstudio": "ShakeAlert",
    "kma_eew_fanstudio": "EEW",
    "global_quake": "GlobalQuake",
    "cea_pr_fanstudio": "地震预警(省)",
    "cenc_wolfx": "EEW",
    "cwa_wolfx": "EEW",
    "jma_wolfx": "緊急地震速報",
    "sc_wolfx_eew": "EEW",
    "fj_wolfx_eew": "EEW",
    "cq_wolfx_eew": "EEW",
}


def _get_event_label(source_id: str, fallback: str) -> str:
    """获取源特有的事件标签，无映射则用 fallback。"""
    return _EVENT_LABELS.get(source_id, fallback)

# 旧的 _SOURCE_NAMES 保留供 _SOURCE_DISPLAY_INFO 没加载到时的兜底
_SOURCE_NAMES: dict[str, str] = {
    "cea_fanstudio": "中国地震预警网", "cenc_fanstudio": "中国地震台网",
    "jma_fanstudio": "日本气象厅", "cwa_fanstudio": "台湾气象署",
    "usgs_fanstudio": "USGS", "emsc_fanstudio": "EMSC",
    "hko_fanstudio": "HKO", "gfz_fanstudio": "GFZ",
    "usp_fanstudio": "USP", "bcsf_fanstudio": "BCSF",
    "fssn_fanstudio": "FSSN", "kma_fanstudio": "KMA",
    "sa_fanstudio": "ShakeAlert", "kma_eew_fanstudio": "KMA EEW",
    "global_quake": "GlobalQuake",
    "funvisis_http": "FUNVISIS", "cenais_http": "CENAIS",
    "csnc_http": "CSNC", "phivolcs_http": "PHIVOLCS",
    "tmd_http": "TMD", "geonet_http": "GeoNet",
    "nrcan_http": "NRCan", "usgs_weekly": "USGS周报",
    "snet_http": "S-net", "icl_http": "ICL",
    "beijing_fanstudio": "北京", "guangxi_fanstudio": "广西",
    "ningxia_fanstudio": "宁夏", "shanxi_fanstudio": "山西",
    "yunnan_fanstudio": "云南",
    "cenc_wolfx": "CENC(Wolfx)", "cwa_wolfx": "CWA(Wolfx)",
    "china_tsunami_fanstudio": "海啸预警",
    "snet": "S-net",
    "cma_typhoon": "CMA台风", "jma_typhoon": "JMA台风",
    "jma_wolfx": "JMA(Wolfx)", "jma_wolfx_info": "JMA情报(Wolfx)",
    "sc_wolfx_eew": "四川", "fj_wolfx_eew": "福建", "cq_wolfx_eew": "重庆",
}


def _make_source_title(source_id: str, event_label: str) -> str:
    """生成标题：[CODE/机构名 事件标签] 或 [机构名 事件标签]"""
    info = _SOURCE_DISPLAY_INFO.get(source_id)
    code = info["code"] if info else ""
    name = info["name"] if info else ""
    if code and name:
        return f"[{code}/{name} {event_label}]"
    if name:
        return f"[{name} {event_label}]"
    # 兜底：用 _SOURCE_NAMES 的短名
    short = _SOURCE_NAMES.get(source_id, source_id)
    return f"[{short} {event_label}]"

# ── 格式常量（对齐 earthquake_presenter） ──

_SEPARATOR = "=" * 19
_FIELD_WIDTH = 6


def _field(label: str, value: object) -> str:
    w = sum(2 if ord(c) > 127 else 1 for c in label)
    pad = _FIELD_WIDTH * 2 - w
    if pad < 0:
        pad = 0
    return f"{label}{'　' * (pad // 2)}{' ' * (pad % 2)}| {value}"


def _make_title(text: str) -> str:
    return f"[{text}]"


def _format_coords(lat: float | None, lon: float | None) -> str:
    if lat is None or lon is None:
        return ""
    return f"{abs(lon):.2f}{'E' if lon >= 0 else 'W'} {abs(lat):.2f}{'N' if lat >= 0 else 'S'}"


def present_eew(event: EewEvent) -> str:
    """格式化 EEW 预警消息。"""
    label = _get_event_label(event.source_id, "EEW")
    parts = []
    if event.report_num:
        parts.append(f"第{event.report_num}报")
    if event.is_final:
        parts.append("最终报")
    report_tag = " ".join(parts)
    title_label = f"{label}-{report_tag}" if report_tag else label
    title = _make_source_title(event.source_id, title_label)
    lines = [title]
    lines.append(_SEPARATOR)
    if event.place_name:
        lines.append(_field("震中", event.place_name))
    if event.magnitude is not None:
        lines.append(_field("震级", f"M{event.magnitude:.1f}"))
    if event.depth is not None:
        lines.append(_field("深度", f"{event.depth:.0f} km"))
    if event.occurred_at:
        lines.append(_field("发震时间", event.occurred_at.strftime("%Y-%m-%d %H:%M:%S")))
    coords = _format_coords(event.latitude, event.longitude)
    if coords:
        lines.append(_field("经纬度", coords))
    if event.max_intensity:
        intensity_icons = {">7": "🟣", "7": "🔴", "6": "🟠", "5": "🟡", "4": "🟢", "3": "🔵", "2": "⚪", "1": "⚪"}
        icon = intensity_icons.get(str(event.max_intensity).split(".")[0], "")
        lines.append(_field("最大烈度", f"{event.max_intensity} {icon}".strip()))
    lines.append(_SEPARATOR)
    return "\n".join(lines)


def present_earthquake_report(event: EarthquakeReport) -> str:
    """格式化地震报告消息。"""
    parts = []
    if event.report_num:
        parts.append(f"第{event.report_num}报")
    if event.is_final:
        parts.append("最终报")
    report_tag = " ".join(parts)
    title_label = f"地震报告-{report_tag}" if report_tag else "地震报告"
    lines = [_make_source_title(event.source_id, title_label)]
    lines.append(_SEPARATOR)
    if event.place_name:
        lines.append(_field("震中", event.place_name))
    if event.region:
        lines.append(_field("区域", event.region))
    if event.magnitude is not None:
        lines.append(_field("震级", f"M{event.magnitude:.1f}"))
    if event.depth is not None:
        d_text = "极浅" if event.depth == 0.0 else f"{event.depth:.0f} km"
        lines.append(_field("深度", d_text))
    if event.occurred_at:
        lines.append(_field("发震时间", event.occurred_at.strftime("%Y-%m-%d %H:%M:%S")))
    coords = _format_coords(event.latitude, event.longitude)
    if coords:
        lines.append(_field("经纬度", coords))
    lines.append(_field("事件ID", event.event_id))
    if event.url:
        lines.append(_field("详情", event.url))
    lines.append(_SEPARATOR)
    return "\n".join(lines)


def present_tsunami(event: TsunamiEvent) -> str:
    """格式化海啸预警消息。"""
    level_icons = {0: "✅", 1: "🟡", 2: "🟠", 3: "🔴"}
    level_names = {0: "解除", 1: "注意报", 2: "警报", 3: "大海啸警报"}
    icon = level_icons.get(event.level, "⚪")
    title = _make_source_title(event.source_id, "海啸预警")
    lines = [title]
    lines.append(_SEPARATOR)
    lines.append(_field("级别", f"{icon} {event.title or level_names.get(event.level, '')}"))
    if event.areas:
        lines.append(_field("区域", f"{len(event.areas)} 个"))
    lines.append(_SEPARATOR)
    return "\n".join(lines)


def present_weather(event: WeatherEvent) -> str:
    """格式化气象预警消息。"""
    level_icon = LEVEL_COLORS.get(event.alert_level or "", "⚪")
    alert_name = WEATHER_TYPE_MAP.get(event.alert_type or "", event.alert_type or "气象")
    lines = [_make_source_title(event.source_id, "气象预警")]
    lines.append(_SEPARATOR)
    lines.append(_field("预警", f"{level_icon} {alert_name}{event.alert_level or ''}预警"))
    if event.alert_title:
        lines.append(_field("标题", event.alert_title))
    if event.headline:
        lines.append(_field("详情", event.headline[:200]))
    if event.effective_time:
        lines.append(_field("生效", event.effective_time.strftime("%Y-%m-%d %H:%M")))
    lines.append(_SEPARATOR)
    return "\n".join(lines)


def present_typhoon(event: TyphoonEvent) -> str:
    """格式化台风消息（/台风 命令使用）。"""
    cat_names = {0: "热带低压", 1: "热带风暴", 2: "强热带风暴",
                 3: "台风", 4: "强台风", 5: "超强台风"}
    lines = [_make_source_title(event.source_id, "台风")]
    lines.append(_SEPARATOR)
    lines.append(_field("名称", f"{event.name_cn or event.name_en}（{event.code}）"))
    lines.append(_field("级别", cat_names.get(event.category, '未知')))
    if event.latitude is not None and event.longitude is not None:
        lines.append(_field("位置", f"{event.latitude:.1f}°N, {event.longitude:.1f}°E"))
    if event.pressure is not None:
        lines.append(_field("气压", f"{event.pressure:.0f} hPa"))
    if event.wind_speed is not None:
        lines.append(_field("风速", f"{event.wind_speed:.0f} m/s"))
    if event.track_points:
        lines.append(_field("路径点", f"{len(event.track_points)} 个"))
    lines.append(_SEPARATOR)
    return "\n".join(lines)


def present_snet(event: EarthquakeReport) -> str:
    """SNET 专用格式 — 对齐旧版 NIED S-Net 海底震度分布展示器。"""
    raw = event.raw if isinstance(event.raw, dict) else {}
    stations = raw.get("stations", [])
    timestamp = raw.get("timestamp", "")
    triggered = raw.get("triggered", [])

    # 格式时间
    display_time = timestamp
    if timestamp:
        try:
            from datetime import datetime, timezone, timedelta
            dt = datetime.strptime(str(timestamp), "%Y%m%d%H%M00").replace(tzinfo=timezone.utc)
            dt_cst = dt + timedelta(hours=8)
            display_time = dt_cst.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            pass

    lines = []
    lines.append("[NIED S-Net海底震度分布]")
    lines.append(f"更新时间：{display_time}")
    lines.append(f"触发方式：摇晃检出")
    lines.append(f"触发测站数量：{len(triggered)}/{len(stations)}")
    lines.append(_SEPARATOR)
    lines.append("降序排名前10测站")

    sorted_stations = sorted(
        stations, key=lambda s: s.get("shindo", -10) if isinstance(s, dict) else -10, reverse=True
    )
    for s in sorted_stations[:10]:
        if not isinstance(s, dict):
            continue
        name = s.get("name", "?")
        shindo = s.get("shindo", 0)
        label = _shindo_label_str(shindo)
        lines.append(f"{name:<12s} {shindo:>7.3f}({label})")

    lines.append(_SEPARATOR)
    return "\n".join(lines)


def _shindo_label_str(shindo: float) -> str:
    """JMA 震度浮点值 → 中文震度等级标签。"""
    if shindo >= 6.5: return "震度7"
    if shindo >= 6.0: return "震度6強"
    if shindo >= 5.5: return "震度6弱"
    if shindo >= 5.0: return "震度5強"
    if shindo >= 4.5: return "震度5弱"
    if shindo >= 3.5: return "震度4"
    if shindo >= 2.5: return "震度3"
    if shindo >= 1.5: return "震度2"
    if shindo >= 0.5: return "震度1"
    if shindo >= 0.0: return "震度0"
    return "震度0以下"


# ── 台风自动推送格式（‖ 前缀风格） ──

_SEP_LINE = "=" * 19

_SOURCE_PREFIX = {
    "cma_typhoon": "CMA",
    "jma_typhoon": "JMA",
}

_PUSH_TYPE_LABELS = {
    "initial": "台风生成",
    "track_point": "路径更新",
    "forecast_update": "预报更新",
    "inactive": "台风消散／停编",
}

_CATEGORY_NAMES = {
    0: "热带低压以下", 1: "热带低压", 2: "热带风暴",
    3: "强热带风暴", 4: "台风", 5: "强台风", 6: "超强台风",
}


def present_typhoon_push(
    event: TyphoonEvent,
    push_type: str,
    metadata: dict | None = None,
) -> str:
    """‖ 前缀格式的台风推送文本。"""
    src = _SOURCE_PREFIX.get(event.source_id, event.source_id.upper())
    label = _PUSH_TYPE_LABELS.get(push_type, "更新")
    title = f"[{src} 台风{label}]"

    name = event.name_cn or event.name_en or event.code

    lines = [title]
    lines.append(f"‖ {name}（{event.code}）")
    lines.append(_SEP_LINE)

    if event.last_updated:
        lines.append(f"‖ 时间：{event.last_updated.strftime('%m月%d日 %H:%M')}")

    cat_name = _CATEGORY_NAMES.get(event.category, "")
    if cat_name:
        lines.append(f"‖ 级别：{cat_name}")

    if event.wind_speed is not None:
        lines.append(f"‖ 风速：{event.wind_speed:.0f}m/s")
    if event.pressure is not None:
        lines.append(f"‖ 气压：{event.pressure:.0f}hPa")
    if event.latitude is not None and event.longitude is not None:
        lat_dir = "N" if event.latitude >= 0 else "S"
        lon_dir = "E" if event.longitude >= 0 else "W"
        lines.append(f"‖ 位置：{abs(event.longitude):.1f}°{lon_dir} {abs(event.latitude):.1f}°{lat_dir}")
    if event.move_direction:
        s = f"‖ 移向：{event.move_direction}"
        if event.move_speed is not None:
            s += f" {event.move_speed:.0f}km/h"
        lines.append(s)

    # 预报点
    forecast = [p for p in event.track_points if p.is_forecast and p.timestamp is not None]
    if forecast:
        lines.append(_SEP_LINE)
        lines.append("‖ 预报：")
        for pt in forecast[:5]:
            ts = pt.timestamp.strftime("%m/%d %H:%M")
            parts = []
            if pt.wind_speed is not None:
                parts.append(f"{pt.wind_speed:.0f}m/s")
            if pt.pressure is not None:
                parts.append(f"{pt.pressure:.0f}hPa")
            cat = _CATEGORY_NAMES.get(pt.category, "")
            if cat:
                parts.append(cat)
            suffix = " ".join(parts)
            lines.append(f"‖   {ts}  {' '.join(parts) if parts else ''}".rstrip())

    lines.append(_SEP_LINE)
    return "\n".join(lines)


def present(envelope: EventEnvelope) -> str:
    """自动选择展示格式。"""
    event = envelope.event
    if isinstance(event, EewEvent):
        return present_eew(event)
    if isinstance(event, EarthquakeReport):
        # SNET 专用格式
        if event.source_id in ("snet_http", "snet") and isinstance(event.raw, dict) and event.raw.get("stations"):
            return present_snet(event)
        return present_earthquake_report(event)
    if isinstance(event, TsunamiEvent):
        return present_tsunami(event)
    if isinstance(event, WeatherEvent):
        return present_weather(event)
    if isinstance(event, TyphoonEvent):
        push_type = envelope.metadata.get("push_type", "") if envelope.metadata else ""
        if push_type:
            return present_typhoon_push(event, push_type, envelope.metadata)
        return present_typhoon(event)
    return f"[未识别的消息类型] source={envelope.source_id}"


__all__ = [
    "present", "present_eew", "present_earthquake_report",
    "present_tsunami", "present_weather", "present_typhoon",
    "present_typhoon_push",
    "WEATHER_TYPE_MAP", "LEVEL_COLORS",
]
