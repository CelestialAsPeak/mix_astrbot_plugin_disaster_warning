"""
message/render/snet_map_renderer.py — NIED S-Net 测站分布图渲染器（Playwright 版）。

用 Playwright + Canvas2D 替代 PIL：
- TopoJSON 日本都道府県多边形（Canvas 绘制）
- SREV SVG 震度图标（Base64 内嵌）
- CSS 右侧面板（三栏统计 + 测站列表）

移植自旧版 core/services/snet/snet_map_renderer.py
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from jinja2 import Template

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger


# ============================================================
# 常量
# ============================================================
MAP_LON_MIN = 135.0
MAP_LON_MAX = 149.0
MAP_LAT_MIN = 32.0
MAP_LAT_MAX = 44.0

# 图标文件映射: (shindo下限, SVG文件名)
_SNET_ICON_FILES = [
    (6.5, "C7.svg"), (6.0, "C6+.svg"), (5.5, "C6-.svg"),
    (5.0, "C5+.svg"), (4.5, "C5-.svg"), (3.5, "C4.svg"),
    (2.5, "C3.svg"), (1.5, "C2.svg"), (0.5, "C1.svg"),
    (0, "C0.svg"),   # 震度0（shindo >= 0 且 < 0.5 用这个图标）
]

# MSIL 震度→RGB
MSIL_SHINDO_TO_RGB: dict[int, tuple[int, int, int]] = {
    -30:(0,0,205),-25:(0,36,227),-20:(0,72,250),-15:(0,140,194),-10:(0,208,139),
    -5:(31,228,96),0:(63,250,54),5:(125,252,33),10:(189,255,12),15:(222,255,5),
    20:(255,255,0),25:(255,238,0),30:(255,221,0),35:(255,182,0),40:(255,144,0),
    45:(255,106,0),50:(255,68,0),55:(250,33,0),60:(245,0,0),65:(208,0,0),
    70:(170,0,0),
}


# ── TopoJSON 解码 ──

def _decode_topojson_rings(topo_path: str) -> list[list[tuple[float, float]]]:
    with open(topo_path, encoding="utf-8") as f:
        topo = json.load(f)
    sx, sy = topo["transform"]["scale"]
    tx, ty = topo["transform"]["translate"]
    decoded_arcs = []
    for arc in topo["arcs"]:
        coords = []
        cx = cy = 0.0
        for dx, dy in arc:
            cx += dx
            cy += dy
            coords.append((cx * sx + tx, cy * sy + ty))
        decoded_arcs.append(coords)

    def _get_arc(idx: int) -> list[tuple[float, float]]:
        if idx >= 0:
            return list(decoded_arcs[idx])
        return list(reversed(decoded_arcs[~idx]))

    obj = next(iter(topo["objects"].values()))
    geometries = obj["geometries"] if obj.get("type") == "GeometryCollection" else [obj]
    all_rings = []
    for geom in geometries:
        arcs_data = geom.get("arcs")
        if not arcs_data:
            continue
        rc = [arcs_data] if geom["type"] == "Polygon" else arcs_data
        for pr in rc:
            for ra in pr:
                ring = []
                for idx in ra:
                    ring.extend(_get_arc(idx))
                if len(ring) >= 3:
                    all_rings.append(ring)

    visible = []
    for ring in all_rings:
        lons = [p[0] for p in ring]
        lats = [p[1] for p in ring]
        if max(lons) > MAP_LON_MIN - 2 and min(lons) < MAP_LON_MAX + 2 \
           and max(lats) > MAP_LAT_MIN - 2 and min(lats) < MAP_LAT_MAX + 2:
            visible.append(ring)
    return visible


# ── SVG 图标加载（Base64 缓存） ──

def _load_icons_base64(icon_dir: str) -> dict[float, str]:
    result = {}
    if not os.path.isdir(icon_dir):
        logger.warning(f"[snet_map] 图标目录不存在: {icon_dir}")
        return result
    for threshold, fname in _SNET_ICON_FILES:
        svg_path = os.path.join(icon_dir, fname)
        if os.path.exists(svg_path):
            try:
                with open(svg_path, "rb") as f:
                    # key 用 .1f 格式化对齐 JS 的 .toFixed(1)
                    result[f"{float(threshold):.1f}"] = base64.b64encode(f.read()).decode()
            except Exception as e:
                logger.error(f"[snet_map] 加载 {fname} 失败: {e}")
    logger.info(f"[snet_map] SVG 图标加载: {len(result)}/{len(_SNET_ICON_FILES)}")
    return result


# ── 工具函数 ──

def _shindo_short_label(shindo: float) -> str:
    if shindo >= 6.5: return "7"
    if shindo >= 6.0: return "6強"
    if shindo >= 5.5: return "6弱"
    if shindo >= 5.0: return "5強"
    if shindo >= 4.5: return "5弱"
    if shindo >= 3.5: return "4"
    if shindo >= 2.5: return "3"
    if shindo >= 1.5: return "2"
    if shindo >= 0.5: return "1"
    if shindo >= 0.0: return "0"
    return ""

def _shindo_css_class(shindo: float) -> str:
    if shindo >= 6.5: return "shindo-s7"
    if shindo >= 6.0: return "shindo-s6p"
    if shindo >= 5.5: return "shindo-s6m"
    if shindo >= 5.0: return "shindo-s5p"
    if shindo >= 4.5: return "shindo-s5m"
    if shindo >= 3.5: return "shindo-s4"
    if shindo >= 2.5: return "shindo-s3"
    if shindo >= 1.5: return "shindo-s2"
    if shindo >= 0.5: return "shindo-s1"
    if shindo >= 0.0: return "shindo-s0"
    return ""

def _rgb_to_str(rgb: tuple[int, int, int] | list[int] | None) -> str:
    if not rgb or len(rgb) < 3:
        return "63,250,54"
    return f"{rgb[0]},{rgb[1]},{rgb[2]}"

def _format_display_time(timestamp: str) -> str:
    if not timestamp:
        return "——"
    try:
        from datetime import datetime, timezone, timedelta
        dt = datetime.strptime(str(timestamp), "%Y%m%d%H%M00")
        dt_utc8 = dt.replace(tzinfo=timezone.utc) + timedelta(hours=8)
        return dt_utc8.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, ImportError):
        return timestamp


# ── 渲染器 ──

class SnetMapRenderer:
    """NIED S-Net 测站分布图渲染器（Playwright 版）。"""

    def __init__(self, browser_manager=None, plugin_root: str = ""):
        self.browser_manager = browser_manager
        self.plugin_root = plugin_root
        self._template_cache: str | None = None
        self._japan_rings: list[list[tuple[float, float]]] | None = None
        self._icon_cache: dict[float, str] | None = None

    def _get_template(self) -> str:
        if self._template_cache is not None:
            return self._template_cache
        template_path = os.path.join(
            self.plugin_root,
            "resources", "card_templates", "SNET", "snet_station.html",
        )
        if not os.path.exists(template_path):
            raise FileNotFoundError(f"SNET 模板未找到: {template_path}")
        with open(template_path, encoding="utf-8") as f:
            self._template_cache = f.read()
        return self._template_cache

    def _get_japan_rings(self) -> list[list[tuple[float, float]]]:
        """加载并缓存 TopoJSON 日本轮廓。"""
        if self._japan_rings is not None:
            return self._japan_rings
        topo_path = os.path.join(self.plugin_root, "resources", "snet_data", "jp.pref.topo.json")
        if not os.path.exists(topo_path):
            logger.warning(f"[snet_map] TopoJSON 未找到: {topo_path}")
            self._japan_rings = []
            return self._japan_rings
        try:
            self._japan_rings = _decode_topojson_rings(topo_path)
        except Exception as e:
            logger.error(f"[snet_map] 加载 TopoJSON 失败: {e}")
            self._japan_rings = []
        return self._japan_rings

    def _get_icons(self) -> dict[float, str]:
        """加载并缓存 SVG 图标。"""
        if self._icon_cache is not None:
            return self._icon_cache
        icon_dir = os.path.join(self.plugin_root, "resources", "snet_data", "SREV")
        self._icon_cache = _load_icons_base64(icon_dir)
        return self._icon_cache

    async def render(
        self,
        stations: list[dict[str, Any]],
        output_path: str,
        timestamp: str = "",
    ) -> str | None:
        """渲染 SNET 测站分布图。"""
        ctx = self._build_context(stations, timestamp)

        try:
            template_str = self._get_template()
            template = Template(template_str)
            html_content = template.render(**ctx)
        except Exception as e:
            logger.error(f"[snet_map] 模板渲染失败: {e}", exc_info=True)
            return None

        if not self.browser_manager:
            logger.warning("[snet_map] 无浏览器管理器，跳过渲染")
            return None

        try:
            result = await self.browser_manager.render_card(
                html_content, output_path,
                selector="#card-wrapper",
                viewport={"width": 1400, "height": 1000},
                wait_until="networkidle",
            )
            if result and os.path.exists(output_path):
                logger.info(f"[snet_map] 测站图已生成 ({os.path.getsize(output_path)} bytes): {output_path}")
                return output_path
            logger.warning("[snet_map] 渲染未生成文件")
            return None
        except Exception as e:
            logger.error(f"[snet_map] Playwright 渲染失败: {e}", exc_info=True)
            return None

    def _build_context(self, stations: list[dict[str, Any]], timestamp: str) -> dict[str, Any]:
        rings_json = json.dumps(self._get_japan_rings())
        icons = self._get_icons()
        icon_svgs_json = json.dumps(icons)

        station_list = []
        for s in stations:
            name = s.get("name", "?")
            lat = s.get("lat", 0)
            lon = s.get("lon", 0)
            shindo = s.get("shindo", -999)
            rgb = s.get("rgb")
            rgb_str = _rgb_to_str(rgb)
            sc = _shindo_css_class(shindo)
            station_list.append({
                "name": name,
                "lat": lat, "lon": lon,
                "shindo": shindo,
                "rgb_str": rgb_str,
                "label": _shindo_short_label(shindo),
                "shindo_class": sc,
                "dot_class": sc.replace("shindo-", "dot-") if sc else "dot-none",
            })

        sorted_stations = sorted(station_list, key=lambda x: x["shindo"], reverse=True)
        triggered = [s for s in sorted_stations if s["shindo"] >= 0]
        triggered_count = len(triggered)
        total_stations = len(sorted_stations)
        top = triggered[0] if triggered else None
        max_shindo_text = f"{top['shindo']:.3f}" if top else "——"
        top_station_name = top["name"] if top else "——"
        display_time = _format_display_time(timestamp)
        triggered_color = "60b0ff" if triggered_count > 0 else "4a5470"

        return {
            "rings_json": rings_json,
            "stations_json": json.dumps(sorted(station_list, key=lambda x: x["shindo"])),
            "icon_svgs_json": icon_svgs_json,
            "display_time": display_time,
            "triggered_count": triggered_count,
            "total_stations": total_stations,
            "max_shindo_text": max_shindo_text,
            "top_station_name": top_station_name,
            "sorted_stations": sorted_stations,
            "triggered_color": triggered_color,
        }
