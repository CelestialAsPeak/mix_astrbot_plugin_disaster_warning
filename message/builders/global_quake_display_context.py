"""
GlobalQuake 展示上下文构建器 — 将 EewEvent + metadata 展平为卡片模板所需的 dict。

适配自旧版 astrbot_plugin_disaster_warning 的 GlobalQuakeDisplayContextBuilder，
针对 mix 版的 EewEvent / metadata 结构做了调整。
"""

from __future__ import annotations

from datetime import datetime, timezone


def _format_coordinates(latitude: float, longitude: float) -> str:
    """格式化坐标显示。"""
    lat_dir = "N" if latitude >= 0 else "S"
    lon_dir = "E" if longitude >= 0 else "W"
    return f"{abs(latitude):.2f}°{lat_dir}, {abs(longitude):.2f}°{lon_dir}"


def _format_time(dt: datetime | None, tz_offset: int = 8) -> str:
    """格式化为本地时间字符串。"""
    if dt is None:
        return "Unknown Time"
    return dt.strftime("%Y/%m/%d %H:%M:%S")


def build_gq_card_context(
    event,
    *,
    metadata: dict | None = None,
    identity=None,
    timezone_offset: int = 8,
    zoom_level: int = 5,
    map_source: str = "",
    tile_url: str = "",
) -> dict:
    """构建 GlobalQuake 卡片渲染上下文。

    Args:
        event: EewEvent 实例
        metadata: EventEnvelope.metadata
        identity: EventIdentity（可选，用于取 event_id）
        timezone_offset: 显示时区偏移（小时）
        zoom_level: 地图缩放级别
        map_source: 地图源名称
        tile_url: 地图瓦片 URL（JS 格式）
    """
    meta = metadata or {}
    mag = event.magnitude or 0

    # 震级背景色
    if mag < 5:
        mag_class = "bg-low"
    elif mag < 7:
        mag_class = "bg-med"
    else:
        mag_class = "bg-high"

    # 时间
    time_str = _format_time(event.occurred_at, timezone_offset)

    # 台站统计
    stations = meta.get("stations") or {}
    stations_used = stations.get("used", 0) if isinstance(stations, dict) else 0
    stations_total = stations.get("total", 0) if isinstance(stations, dict) else 0

    # 质量
    quality = meta.get("quality") or {}
    quality_pct = "N/A"
    location_error = "N/A"
    if isinstance(quality, dict):
        pct = quality.get("pct")
        if pct is not None:
            quality_pct = f"{pct}%"
        err_origin = quality.get("err_origin")
        if err_origin is not None:
            location_error = f"{err_origin:.1f} km"

    # PGA
    pga = meta.get("max_pga")
    pga_str = f"{pga:.1f} gal" if pga is not None else "N/A"

    # 深度置信区间（新增字段）
    depth_conf = meta.get("depth_confidence")
    if isinstance(depth_conf, dict) and depth_conf.get("min") is not None:
        location_error = f"{depth_conf['min']:.1f}–{depth_conf['max']:.1f} km"

    return {
        "magnitude": f"{mag:.1f}",
        "mag_class": mag_class,
        "intensity": str(event.max_intensity or ""),
        "region": str(event.place_name or "") if hasattr(event, "place_name") else str(getattr(event, "region", "") or ""),
        "is_update": (meta.get("report_num", 1) or 1) > 1,
        "revision": meta.get("report_num", 1) or 1,
        "time_str": time_str,
        "depth": f"{event.depth:.0f} km" if event.depth not in (None, 0.0) else ("极浅" if event.depth == 0.0 else "N/A"),
        "latitude": f"{event.latitude:.4f}",
        "longitude": f"{event.longitude:.4f}",
        "epicenter_str": _format_coordinates(event.latitude, event.longitude),
        "pga": pga_str,
        "location_error": location_error,
        "stations_used": stations_used,
        "stations_total": stations_total,
        "quality_pct": quality_pct,
        "event_id": str(identity.event_id) if identity else (str(getattr(event, "event_id", ""))),
        # 地图渲染参数
        "zoom_level": zoom_level,
        "map_source": map_source,
        "tile_url": tile_url,
        # Leaflet 资源（由调用者传入或使用默认值）
        "leaflet_js_url": "",
        "leaflet_css_url": "",
        "map_render_helper_js": "",
    }
