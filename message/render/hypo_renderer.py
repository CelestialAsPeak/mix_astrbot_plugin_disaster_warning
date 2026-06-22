"""
message/render/hypo_renderer.py — JMA 震央分布图渲染器（PIL 版）。

移植自旧版 core/services/jma_hypo/jma_hypo_renderer.py。
从 JMA 每日 GeoJSON 获取地震数据，绘制震央分布地图。

配色风格与 CAPQuake/S-Net 保持一致。
支持单日、日期区间、多日叠加显示（最多30天）。
"""

from __future__ import annotations

import gzip
import json
import os
import re
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp
try:
    from astrbot.api import logger
except ImportError:
    import logging as logger
from PIL import Image, ImageDraw, ImageFont

# ============================================================
# 画布与地图范围
# ============================================================
CANVAS_WIDTH = 1500
CANVAS_HEIGHT = 1000
MARGIN = 50
MAP_LEFT = 30
MAP_RIGHT = 1080
MAP_WIDTH = MAP_RIGHT - MAP_LEFT
MAP_HEIGHT = CANVAS_HEIGHT - 2 * MARGIN

MAP_LON_MIN = 124.0
MAP_LON_MAX = 149.0
MAP_LAT_MIN = 26.0
MAP_LAT_MAX = 46.0

PANEL_LEFT = MAP_RIGHT + 10
PANEL_WIDTH = CANVAS_WIDTH - PANEL_LEFT - 20
PANEL_PAD = 14

# ============================================================
# 配色
# ============================================================
COLOR_BG_DARK = (18, 18, 22)
COLOR_PANEL_BG = (30, 32, 38)
COLOR_CARD_BG = (45, 45, 55)
COLOR_BORDER = (60, 60, 70)
COLOR_TEXT = (220, 225, 230)
COLOR_TEXT_SEC = (150, 150, 160)
COLOR_TEXT_DIM = (100, 100, 110)
COLOR_OCEAN = (6, 6, 6)
COLOR_LAND = (30, 33, 42)
COLOR_COAST = (75, 80, 95)
CARD_RADIUS = 20
CARD_BORDER_W = 1

MAG_COLORS: list[tuple[float, tuple[int, int, int], str]] = [
    (0.0, (60, 70, 90), "M<0.5"),
    (0.5, (80, 120, 180), "M0.5~"),
    (1.0, (30, 180, 100), "M1.0~"),
    (2.0, (200, 200, 50), "M2.0~"),
    (3.0, (220, 140, 30), "M3.0~"),
    (4.0, (220, 70, 40), "M4.0~"),
    (5.0, (210, 30, 30), "M5.0~"),
    (6.0, (180, 0, 0), "M6.0~"),
]

MIN_DOT_RADIUS = 2.0
MAX_DOT_RADIUS = 12.0

JMA_HYPO_BASE = "https://www.jma.go.jp/bosai/hypo/data"


def _get_mag_color(magnitude: float) -> tuple[int, int, int]:
    for threshold, color, _ in reversed(MAG_COLORS):
        if magnitude >= threshold:
            return color
    return MAG_COLORS[0][1]


def _get_mag_label(magnitude: float) -> str:
    for threshold, _, label in reversed(MAG_COLORS):
        if magnitude >= threshold:
            return label
    return MAG_COLORS[0][2]


def _dot_radius(magnitude: float) -> float:
    if magnitude <= 0:
        return MIN_DOT_RADIUS
    r = MIN_DOT_RADIUS + (MAX_DOT_RADIUS - MIN_DOT_RADIUS) * (magnitude ** 0.5 / 6.0 ** 0.5)
    return min(r, MAX_DOT_RADIUS)


def _lonlat_to_xy(lon: float, lat: float) -> tuple[float, float]:
    x = MAP_LEFT + (lon - MAP_LON_MIN) / (MAP_LON_MAX - MAP_LON_MIN) * MAP_WIDTH
    y = MARGIN + (MAP_LAT_MAX - lat) / (MAP_LAT_MAX - MAP_LAT_MIN) * MAP_HEIGHT
    return x, y


def _get_font(size=14, bold=False):
    if bold:
        candidates = [
            "C:/Windows/Fonts/msyhbd.ttc",
            "C:/Windows/Fonts/msyh.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        ]
    else:
        candidates = [
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simsun.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


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
        if (max(lons) > MAP_LON_MIN - 2 and min(lons) < MAP_LON_MAX + 2
                and max(lats) > MAP_LAT_MIN - 2 and min(lats) < MAP_LAT_MAX + 2):
            visible.append(ring)
    return visible


# ── 数据获取 ──

async def fetch_hypo_data(session: aiohttp.ClientSession, target_date: date) -> list[dict[str, Any]]:
    yyyy = target_date.strftime("%Y")
    mm = target_date.strftime("%m")
    yyyymmdd = target_date.strftime("%Y%m%d")
    url = f"{JMA_HYPO_BASE}/{yyyy}/{mm}/hypo{yyyymmdd}.geojson"

    try:
        async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
            if resp.status != 200:
                return []
            raw = await resp.read()
            try:
                decoded = gzip.decompress(raw)
            except (gzip.BadGzipFile, OSError):
                decoded = raw
            geojson = json.loads(decoded)

        features = geojson.get("features", [])
        result = []
        for feat in features:
            props = feat.get("properties", {})
            geom = feat.get("geometry", {})
            if geom.get("type") != "Point":
                continue
            coords = geom.get("coordinates")
            if not coords or len(coords) < 2:
                continue

            lon, lat = float(coords[0]), float(coords[1])
            if not (MAP_LON_MIN - 5 <= lon <= MAP_LON_MAX + 5 and MAP_LAT_MIN - 5 <= lat <= MAP_LAT_MAX + 5):
                continue

            mag_str = str(props.get("mag", "0") or "0").strip()
            dep_str = str(props.get("dep", "0") or "0").strip()
            place = str(props.get("place", "") or "").strip()

            try:
                mag = float(mag_str) if mag_str else 0.0
            except (ValueError, TypeError):
                mag = 0.0
            try:
                dep = float(dep_str) if dep_str else 0.0
            except (ValueError, TypeError):
                dep = 0.0

            result.append({
                "lon": lon, "lat": lat, "mag": mag, "dep": dep,
                "place": place, "date_str": str(props.get("date", "") or "").strip(),
            })

        return result
    except Exception as e:
        logger.warning(f"[hypo] 获取 {yyyymmdd} 数据失败: {e}")
        return []


# ── 日期解析 ──

def parse_date_args(text: str) -> list[date]:
    today = date.today()
    text = text.strip().replace(" ", " ")

    if not text:
        return [today]

    text = re.sub(r"[－–—]", "-", text)

    year_prefix = None
    if "去年" in text:
        year_prefix = today.year - 1
        text = re.sub(r"去年", f"{year_prefix}年", text)
    else:
        ym = re.search(r"(\d{4})年", text)
        if ym:
            year_prefix = int(ym.group(1))

    yr = year_prefix if year_prefix is not None else today.year
    results: list[date] = []

    # 尝试完整区间 "2025年6月1日-2025年6月30日"
    m = re.match(r"^(\d{4})年(\d{1,2})月(\d{1,2})日\s*[-至到]\s*(\d{4})年(\d{1,2})月(\d{1,2})日$", text)
    if m:
        y1, m1, d1, y2, m2, d2 = map(int, m.groups())
        start, end = date(y1, m1, d1), date(y2, m2, d2)
        if end < start:
            start, end = end, start
        if (end - start).days <= 30:
            return [start + timedelta(days=i) for i in range((end - start).days + 1)]
        return []

    # "2025年6月1日-6月30日"
    m = re.match(r"^(\d{4})年(\d{1,2})月(\d{1,2})日\s*[-至到]\s*(\d{1,2})月(\d{1,2})日$", text)
    if m:
        y1, m1, d1, m2, d2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
        start, end = date(y1, m1, d1), date(y1, m2, d2)
        if end < start:
            start, end = end, start
        if (end - start).days <= 30:
            return [start + timedelta(days=i) for i in range((end - start).days + 1)]
        return []

    # "2025年6月14日"
    m = re.match(r"^(\d{4})年(\d{1,2})月(\d{1,2})日$", text)
    if m:
        return [date(int(m.group(1)), int(m.group(2)), int(m.group(3)))]

    # "6月14日-6月20日"
    m = re.match(r"^(\d{1,2})月(\d{1,2})日\s*[-至到]\s*(\d{1,2})月(\d{1,2})日$", text)
    if m:
        m1, d1, m2, d2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        start, end = date(yr, m1, d1), date(yr, m2, d2)
        if end < start:
            start, end = end, start
        if (end - start).days <= 30:
            return [start + timedelta(days=i) for i in range((end - start).days + 1)]
        return []

    # "6月14日-20日"
    m = re.match(r"^(\d{1,2})月(\d{1,2})日\s*[-至到]\s*(\d{1,2})日$", text)
    if m:
        m1, d1, d2 = int(m.group(1)), int(m.group(2)), int(m.group(3))
        start, end = date(yr, m1, d1), date(yr, m1, d2)
        if end < start:
            start, end = end, start
        if (end - start).days <= 30:
            return [start + timedelta(days=i) for i in range((end - start).days + 1)]
        return []

    # "6月14日 6月15日"（多个）
    multi = re.findall(r"(\d{1,2})月(\d{1,2})日", text)
    if multi:
        seen = set()
        for m_str, d_str in multi:
            try:
                d = date(yr, int(m_str), int(d_str))
                if d not in seen:
                    seen.add(d)
                    results.append(d)
            except (ValueError, TypeError):
                continue
        if results and len(results) <= 30:
            return sorted(results)

    return []


def _format_date_range(dates: list[date]) -> str:
    if not dates:
        return ""
    if len(dates) == 1:
        return f"{dates[0].month}月{dates[0].day}日"
    is_contiguous = all(
        (dates[i + 1] - dates[i]).days == 1 for i in range(len(dates) - 1)
    )
    if is_contiguous:
        return f"{dates[0].month}月{dates[0].day}日-{dates[-1].month}月{dates[-1].day}日（{len(dates)}天）"
    return f"{'、'.join(f'{d.month}/{d.day}' for d in dates)}（{len(dates)}天）"


# ══════════════════════════════════════════════════
# 渲染器
# ══════════════════════════════════════════════════

class HypoRenderer:
    """JMA 震央分布图渲染器。"""

    def __init__(self, plugin_root: str = ""):
        self._plugin_root = plugin_root
        self._japan_rings: list[list[tuple[float, float]]] | None = None

    def _get_japan_rings(self) -> list[list[tuple[float, float]]]:
        if self._japan_rings is not None:
            return self._japan_rings
        topo_path = os.path.join(
            self._plugin_root, "resources", "snet_data", "jp.pref.topo.json",
        )
        if not os.path.exists(topo_path):
            logger.warning(f"[hypo] TopoJSON 未找到: {topo_path}")
            self._japan_rings = []
            return self._japan_rings
        try:
            self._japan_rings = _decode_topojson_rings(topo_path)
        except Exception as e:
            logger.error(f"[hypo] 加载 TopoJSON 失败: {e}")
            self._japan_rings = []
        return self._japan_rings

    async def render(
        self,
        dates: list[date],
        output_path: str,
    ) -> dict | None:
        """获取数据并渲染震央分布图。返回 {path, total_events, covered_days} 或 None。"""
        rings = self._get_japan_rings()
        if not rings:
            logger.error("[hypo] TopoJSON 未加载")
            return None
        if not dates:
            logger.warning("[hypo] 日期列表为空")
            return None

        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            all_events: list[dict[str, Any]] = []
            day_counts: dict[str, int] = {}

            for d in dates:
                events = await fetch_hypo_data(session, d)
                if events:
                    all_events.extend(events)
                    day_counts[d.strftime("%Y%m%d")] = len(events)

            logger.info(f"[hypo] 共获取 {len(dates)} 天，{len(all_events)} 次地震")

            img = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), COLOR_OCEAN)
            draw = ImageDraw.Draw(img)

            self._draw_map(draw, rings)
            self._draw_watermark(draw)
            self._draw_epicenters(draw, all_events)
            self._draw_panel(draw, dates, all_events, day_counts)

            img.save(output_path, "PNG")
            logger.info(f"[hypo] 震央分布图已保存 ({os.path.getsize(output_path)} bytes): {output_path}")
            return {
                "path": output_path,
                "total_events": len(all_events),
                "covered_days": len(day_counts),
            }

    @staticmethod
    def _draw_map(draw: ImageDraw.ImageDraw, rings: list) -> None:
        for ring in rings:
            poly = [_lonlat_to_xy(lon, lat) for lon, lat in ring]
            if len(poly) >= 3:
                draw.polygon(poly, fill=COLOR_LAND)
                draw.line(poly, fill=COLOR_COAST, width=1)

    @staticmethod
    def _draw_watermark(draw: ImageDraw.ImageDraw) -> None:
        wm_font = _get_font(22, bold=True)
        text = "@CAPBot - @CelestialAsPeak"
        draw.text((MAP_LEFT + 12, MARGIN + 12), text, fill=COLOR_TEXT, font=wm_font)

    @staticmethod
    def _draw_epicenters(draw: ImageDraw.ImageDraw, events: list[dict]) -> None:
        for ev in events:
            x, y = _lonlat_to_xy(ev["lon"], ev["lat"])
            mag = ev["mag"]
            color = _get_mag_color(mag)
            r = _dot_radius(mag)
            if mag >= 4.0:
                outer_r = r * 1.8
                outer_color = (color[0], color[1], color[2], 60)
                draw.ellipse(
                    [(x - outer_r, y - outer_r), (x + outer_r, y + outer_r)],
                    fill=None, outline=outer_color, width=1,
                )
            draw.ellipse([(x - r, y - r), (x + r, y + r)], fill=color)

    @staticmethod
    def _draw_panel(
        draw: ImageDraw.ImageDraw,
        dates: list[date],
        events: list[dict],
        day_counts: dict[str, int],
    ) -> None:
        draw.rectangle([PANEL_LEFT, 0, CANVAS_WIDTH, CANVAS_HEIGHT], fill=COLOR_PANEL_BG)
        px = PANEL_LEFT + PANEL_PAD
        pw = PANEL_WIDTH - PANEL_PAD * 2

        # 标题卡片
        cy = PANEL_PAD
        ch = 120
        draw.rounded_rectangle([px, cy, px + pw, cy + ch], radius=CARD_RADIUS, fill=COLOR_CARD_BG, outline=COLOR_BORDER, width=CARD_BORDER_W)
        title_font = _get_font(36, bold=True)
        sub_font = _get_font(20)
        draw.text((px + 16, cy + 14), "震央分布", fill=COLOR_TEXT, font=title_font)
        draw.text((px + 16, cy + 14 + 48), "Japan Epicenter Distribution", fill=COLOR_TEXT_SEC, font=sub_font)
        date_str = _format_date_range(dates)
        draw.text((px + 16, cy + 14 + 48 + 32), date_str, fill=COLOR_TEXT_DIM, font=_get_font(17))

        # 统计卡片
        lcy = cy + ch + 10
        lch = 200
        draw.rounded_rectangle([px, lcy, px + pw, lcy + lch], radius=CARD_RADIUS, fill=COLOR_CARD_BG, outline=COLOR_BORDER, width=CARD_BORDER_W)
        total = len(events)
        max_mag = max(e["mag"] for e in events) if events else 0
        min_mag = min(e["mag"] for e in events) if events else 0
        avg_dep = sum(e["dep"] for e in events) / total if total > 0 else 0
        stats = [
            ("总地震数", f"{total} 次"),
            ("震级范围", f"M{min_mag:.1f} ~ M{max_mag:.1f}"),
            ("平均深度", f"{avg_dep:.0f} km"),
            ("覆盖天数", f"{len(dates)} 天"),
        ]
        stat_font = _get_font(20)
        stat_val_font = _get_font(28, bold=True)
        sy = lcy + 16
        for i, (label, value) in enumerate(stats):
            draw.text((px + 16, sy + i * 44), label, fill=COLOR_TEXT_SEC, font=stat_font)
            draw.text((px + pw - 16, sy + i * 44), value, fill=COLOR_TEXT, font=stat_val_font, anchor="rt")

        # 震级图例
        ly = lcy + lch + 10
        lh = 30 + len(MAG_COLORS) * 32 + 10
        draw.rounded_rectangle([px, ly, px + pw, ly + lh], radius=CARD_RADIUS, fill=COLOR_CARD_BG, outline=COLOR_BORDER, width=CARD_BORDER_W)
        draw.text((px + 16, ly + 10), "震级图例", fill=COLOR_TEXT, font=_get_font(18, bold=True))
        # 各区间计数
        mag_counts = [0] * len(MAG_COLORS)
        for ev in events:
            m = ev["mag"]
            for i in range(len(MAG_COLORS)):
                th = MAG_COLORS[i][0]
                if i == len(MAG_COLORS) - 1:
                    if m >= th:
                        mag_counts[i] += 1
                else:
                    nt = MAG_COLORS[i + 1][0]
                    if th <= m < nt:
                        mag_counts[i] += 1
        for i, (_, color, label) in enumerate(MAG_COLORS):
            iy = ly + 44 + i * 30
            r = _dot_radius(MAG_COLORS[i][0] + 0.3)
            draw.ellipse([(px + 20 - r, iy - r), (px + 20 + r, iy + r)], fill=color)
            range_label = f"≥ {label[1:]}" if label.startswith("M") else label
            draw.text((px + 38, iy - 8), range_label, fill=COLOR_TEXT, font=_get_font(16))
            cnt_text = f"{mag_counts[i]}次"
            draw.text((px + pw - 16, iy - 8), cnt_text, fill=COLOR_TEXT_SEC if mag_counts[i] == 0 else COLOR_TEXT, font=_get_font(16), anchor="rt")

        # 每日分布
        if day_counts:
            cy2 = ly + lh + 10
            sorted_days = sorted(day_counts.items())
            row_h = 26
            header_h = 44
            max_avail_h = CANVAS_HEIGHT - cy2 - PANEL_PAD - 10
            max_rows = max(1, (max_avail_h - header_h - 10) // row_h)
            visible_rows = min(len(sorted_days), max_rows, 30)
            ch2 = header_h + visible_rows * row_h + 10
            draw.rounded_rectangle([px, cy2, px + pw, cy2 + ch2], radius=CARD_RADIUS, fill=COLOR_CARD_BG, outline=COLOR_BORDER, width=CARD_BORDER_W)
            draw.text((px + 16, cy2 + 10), "每日分布", fill=COLOR_TEXT, font=_get_font(18, bold=True))
            day_font = _get_font(14)
            for i in range(visible_rows):
                dy = cy2 + header_h + i * row_h
                day_str, count = sorted_days[i]
                d = date(int(day_str[:4]), int(day_str[4:6]), int(day_str[6:8]))
                draw.text((px + 16, dy), f"{d.month}/{d.day}", fill=COLOR_TEXT_SEC, font=day_font)
                draw.text((px + pw - 16, dy), f"{count} 次", fill=COLOR_TEXT, font=day_font, anchor="rt")

        if not events:
            empty_font = _get_font(24)
            empty_text = "该时段无地震记录"
            tb = draw.textbbox((0, 0), empty_text, font=empty_font)
            tw = tb[2] - tb[0]
            map_cx = (MAP_LEFT + MAP_RIGHT) // 2
            draw.text((map_cx - tw // 2, CANVAS_HEIGHT // 2 - 20), empty_text, fill=COLOR_TEXT_DIM, font=empty_font)
