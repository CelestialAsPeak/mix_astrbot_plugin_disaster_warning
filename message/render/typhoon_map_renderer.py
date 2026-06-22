"""
message/render/typhoon_map_renderer.py — 台风路径图渲染器（Playwright 版）。

用 Leaflet + Canvas overlay + CSS 面板替代 PIL 渲染，
通过 BrowserManager 走 Playwright 截图输出 PNG。

移植自旧版 astrbot_plugin_disaster_warning/core/message/render/typhoon_map_renderer.py
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Template

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger

try:
    from domain.models import TyphoonEvent, TyphoonTrackPoint
except ImportError:
    from ...domain.models import TyphoonEvent, TyphoonTrackPoint

try:
    from utils.map_tile_sources import get_tile_url_js
except ImportError:
    from ...utils.map_tile_sources import get_tile_url_js


# ── 常量 ──
DEFAULT_LON_MIN = 100.0
DEFAULT_LON_MAX = 170.0
DEFAULT_LAT_MIN = 5.0
DEFAULT_LAT_MAX = 45.0

CAT_NAMES = {
    0: "", 1: "热带低压", 2: "热带风暴", 3: "强热带风暴",
    4: "台风", 5: "强台风", 6: "超强台风",
}
CAT_COLORS = {
    0: (120, 120, 130), 1: (50, 175, 255), 2: (50, 215, 115),
    3: (255, 215, 40), 4: (255, 160, 10), 5: (245, 95, 10), 6: (225, 40, 40),
}
RADII_COLORS = {
    "7级": (100, 180, 255),
    "10级": (255, 200, 50),
    "12级": (255, 80, 0),
}
TILE_SOURCE = "PetalMap矢量图暗"


class TyphoonMapRenderer:
    """台风路径图渲染器（Playwright）。"""

    def __init__(self, browser_manager, plugin_root: str):
        self.browser_manager = browser_manager
        self.plugin_root = plugin_root
        self._template_cache: str | None = None

    def _get_template(self) -> str:
        if self._template_cache is not None:
            return self._template_cache
        template_path = os.path.join(
            self.plugin_root, "resources", "card_templates", "Typhoon", "typhoon_track.html"
        )
        if not os.path.exists(template_path):
            raise FileNotFoundError(f"台风模板未找到: {template_path}")
        with open(template_path, encoding="utf-8") as f:
            self._template_cache = f.read()
        return self._template_cache

    async def render(
        self, event: TyphoonEvent, output_path: str, *, now: datetime | None = None
    ) -> str | None:
        """渲染台风路径图。"""
        now = now or datetime.now()
        if not event.track_points:
            logger.warning(f"[台风] 事件无路径点: {event.code}")
            return None

        past = [p for p in event.track_points if p.timestamp and p.timestamp <= now]
        fcst = [p for p in event.track_points if p not in past]
        allp = past + fcst
        if not allp:
            logger.warning(f"[台风] 全部路径点无效")
            return None

        lons = [p.longitude for p in allp if p.longitude is not None]
        lats = [p.latitude for p in allp if p.latitude is not None]
        if not lons or not lats:
            return None

        # 视角范围
        lo = max(min(lons) - 5.0, DEFAULT_LON_MIN)
        hi = min(max(lons) + 5.0, DEFAULT_LON_MAX)
        la = max(min(lats) - 5.0, DEFAULT_LAT_MIN)
        ha = min(max(lats) + 5.0, DEFAULT_LAT_MAX)
        if hi - lo < 8:
            c = (lo + hi) / 2
            lo, hi = c - 4, c + 4
        if ha - la < 8:
            c = (la + ha) / 2
            la, ha = c - 4, c + 4

        lpt = past[-1] if past else None

        # ── 准备模板数据 ──
        src = "CMA" if event.code.isdigit() else "JMA"
        name = event.name_cn or event.name_en or event.code

        # 趋势箭头
        ws_trend, pr_trend = "", ""
        if len(past) >= 2 and lpt is past[-1]:
            prev = past[-2]
            if prev.wind_speed is not None and lpt.wind_speed is not None:
                d = lpt.wind_speed - prev.wind_speed
                ws_trend = "↑" if d > 0.5 else ("↓" if d < -0.5 else "")
            if prev.pressure is not None and lpt.pressure is not None:
                d = lpt.pressure - prev.pressure
                pr_trend = "↓" if d < -0.5 else ("↑" if d > 0.5 else "")

        # 风圈文本
        wind_radii_data = []
        is_jma = src == "JMA"
        radii_labels = [
            ("7级", "wind_radii_7", RADII_COLORS["7级"]),
            ("暴风域(gale)" if is_jma else "10级", "wind_radii_10", RADII_COLORS["10级"]),
            ("台風域(storm)" if is_jma else "12级", "wind_radii_12", RADII_COLORS["12级"]),
        ]
        if lpt:
            for lb, attr, col in radii_labels:
                radii = getattr(lpt, attr, None)
                if radii:
                    parts = []
                    for k in ("NE", "SE", "SW", "NW"):
                        v = radii.get(k)
                        if v:
                            kn = {"NE": "东", "SE": "南", "SW": "西", "NW": "北"}.get(k, k)
                            parts.append(f"{kn}{int(v)}km")
                    if parts:
                        lines = []
                        for i in range(0, len(parts), 2):
                            lines.append(" ".join(parts[i:i + 2]))
                        wind_radii_data.append({
                            "label": lb,
                            "lines": lines,
                            "color_rgb": f"{col[0]},{col[1]},{col[2]}",
                        })

        # 折线图数据（最近24个点）
        chart_past = past[-24:] if len(past) >= 2 else past
        chart_winds = [p.wind_speed or 0 for p in chart_past]
        chart_press = [p.pressure or 0 for p in chart_past]

        def _point_to_dict(pt: TyphoonTrackPoint, with_radii=False) -> dict:
            d = {
                "lat": pt.latitude,
                "lon": pt.longitude,
                "cat": pt.category or 0,
                "ws": pt.wind_speed,
                "pr": pt.pressure,
                "ts": pt.timestamp.strftime("%H:%M") if pt.timestamp else "",
            }
            if with_radii:
                for attr, key in [("wind_radii_7", "r7"), ("wind_radii_10", "r10"), ("wind_radii_12", "r12")]:
                    r = getattr(pt, attr, None)
                    d[key] = r if r else None
            return d

        past_json = json.dumps([_point_to_dict(p, with_radii=True) for p in past], ensure_ascii=False)
        fcst_json = json.dumps([_point_to_dict(p, with_radii=True) for p in fcst], ensure_ascii=False)

        pos_text = ""
        if lpt and lpt.latitude is not None and lpt.longitude is not None:
            lat_dir = "N" if lpt.latitude >= 0 else "S"
            lon_dir = "E" if lpt.longitude >= 0 else "W"
            pos_text = f"{abs(lpt.latitude):.2f}{lat_dir} {abs(lpt.longitude):.2f}{lon_dir}"

        move_parts = []
        if lpt:
            if lpt.direction:
                move_parts.append(lpt.direction)
            if lpt.move_speed:
                move_parts.append(f"{lpt.move_speed}km/h")
        move_text = " ".join(move_parts)

        cat_color_map = {k: f"rgb{v}" for k, v in CAT_COLORS.items()}
        forecast_rows = []
        for fp in fcst[:12]:
            forecast_rows.append({
                "time": fp.timestamp.strftime("%m/%d %H:%M") if fp.timestamp else "",
                "pos": f"{abs(fp.latitude):.1f}{'N' if fp.latitude>=0 else 'S'},{abs(fp.longitude):.1f}{'E' if fp.longitude>=0 else 'W'}",
                "ws": str(fp.wind_speed) if fp.wind_speed is not None else "",
                "pr": str(fp.pressure) if fp.pressure is not None else "",
                "cat": CAT_NAMES.get(fp.category or 0, ""),
                "cat_color": cat_color_map.get(fp.category or 0, "#969696"),
            })

        all_hist = list(reversed(past))
        history_rows = []
        for pt in all_hist:
            history_rows.append({
                "time": pt.timestamp.strftime("%m/%d %H:%M") if pt.timestamp else "",
                "pos": f"{abs(pt.latitude):.1f}{'N' if pt.latitude>=0 else 'S'},{abs(pt.longitude):.1f}{'E' if pt.longitude>=0 else 'W'}",
                "ws": str(pt.wind_speed) if pt.wind_speed is not None else "",
                "pr": str(pt.pressure) if pt.pressure is not None else "",
                "cat": CAT_NAMES.get(pt.category or 0, ""),
                "cat_color": cat_color_map.get(pt.category or 0, "#969696"),
            })

        # 基础设施 — 使用 file:/// 路径（Path.as_uri() 确保跨平台正确）
        resources_dir = os.path.join(self.plugin_root, "resources", "card_templates")
        leaflet_js_path = Path(os.path.abspath(os.path.join(resources_dir, "leaflet.js"))).as_uri()
        leaflet_css_path = Path(os.path.abspath(os.path.join(resources_dir, "leaflet.css"))).as_uri()
        helper_path = os.path.abspath(os.path.join(resources_dir, "map_render_helper.js"))
        with open(helper_path, encoding="utf-8") as f:
            helper_js = f.read()

        context: dict[str, Any] = {
            "event_name": name,
            "event_code": event.code,
            "source": src,
            "category_name": CAT_NAMES.get(lpt.category, "") if lpt else "",
            "cat_color_rgb": f"{','.join(str(c) for c in CAT_COLORS.get(lpt.category if lpt else 0, (150, 150, 150)))}",
            "wind_speed": f"{lpt.wind_speed:.0f}m/s" if lpt and lpt.wind_speed else "--",
            "pressure": f"{lpt.pressure:.0f}hPa" if lpt and lpt.pressure else "--",
            "ws_trend": ws_trend,
            "pr_trend": pr_trend,
            "position": pos_text or "--",
            "move_dir": lpt.direction if lpt and lpt.direction else "",
            "move_speed": f"{lpt.move_speed:.0f}km/h" if lpt and lpt.move_speed else "",
            "update_time": lpt.timestamp.strftime("%Y年%m月%d日%H:%M:%S") if lpt and lpt.timestamp else "",
            "wind_radii": wind_radii_data,
            "chart_winds": json.dumps(chart_winds),
            "chart_pressures": json.dumps(chart_press),
            "past_json": past_json,
            "forecast_json": fcst_json,
            "has_past": len(past) > 0,
            "has_forecast": len(fcst) > 0,
            "view_lo": lo,
            "view_hi": hi,
            "view_la": la,
            "view_ha": ha,
            "forecast_rows": forecast_rows,
            "history_rows": history_rows,
            "leaflet_js_url": leaflet_js_path,
            "leaflet_css_url": leaflet_css_path,
            "map_render_helper_js": helper_js,
            "tile_url": get_tile_url_js(TILE_SOURCE),
        }

        try:
            template_str = self._get_template()
            template = Template(template_str)
            html_content = template.render(**context)

            logger.info(f"[台风] 开始 Playwright 渲染: {output_path}")
            result = await self.browser_manager.render_card(
                html_content,
                output_path,
                selector="#card-wrapper",
                viewport={"width": 1600, "height": 1200},
                wait_until="networkidle",
            )
            if result and os.path.exists(output_path):
                logger.info(f"[台风] 路径图已生成 ({os.path.getsize(output_path)} bytes): {output_path}")
                return output_path
            logger.warning(f"[台风] 路径图渲染未生成文件")
            return None
        except Exception as e:
            logger.error(f"[台风] 路径图渲染失败: {e}", exc_info=True)
            return None
