"""
message/render/intensity_image_renderer.py

震度/烈度图片渲染器 — 使用 Pillow 生成带 CAPQuakeQt 配色的图片。
缓存已生成的图片，避免重复渲染。

图片尺寸: 600×120px（与震中地图同宽，高度约为其 30%）
"""

from __future__ import annotations

import math
import os

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger

from PIL import Image, ImageDraw, ImageFont


# ── CAPQuakeQt 暗色主题配色 ──

# 卡片背景 (COLOR_CARD_BG)
BG_COLOR = (25, 25, 35)
# 主要文字 (COLOR_TEXT)
LABEL_COLOR = (220, 225, 230)
# 次要文字 (COLOR_TEXT_DIM)
SUB_LABEL_COLOR = (100, 100, 110)

# 左侧装饰色条宽度
ACCENT_BAR_W = 6

# 图片尺寸
IMAGE_WIDTH = 600
IMAGE_HEIGHT = 120

# ── CSIS 12 级烈度配色（CAPQuakeQt CSIS_COLORS） ──
# 索引 0~12 对应 0 / I~XII
CSIS_COLORS: list[tuple[int, int, int]] = [
    (160, 160, 160),   # [0]  灰色
    (160, 160, 160),   # [1]  I 度  灰色
    (183, 217, 240),   # [2]  II 度 微感
    (123, 179, 217),   # [3]  III 度 少有感
    (126, 204, 196),   # [4]  IV 度 多有感
    (255, 213, 79),    # [5]  V 度  惊醒
    (255, 179, 0),     # [6]  VI 度 惊慌
    (255, 152, 0),     # [7]  VII 度 轻微破坏
    (245, 124, 0),     # [8]  VIII 度 中等破坏
    (244, 81, 30),     # [9]  IX 度 严重破坏
    (211, 47, 47),     # [10] X 度  房屋倒塌
    (136, 14, 79),     # [11] XI 度  灾难性破坏
    (62, 0, 40),       # [12] XII 度 毁灭性破坏
]

# ── JMA 10 级震度配色（CAPQuakeQt JMA_COLORS） ──
# 索引 0~9 对应震度 0~7 细分
JMA_COLORS: list[tuple[int, int, int]] = [
    (160, 160, 160),   # [0] 震度0   灰色
    (217, 227, 240),   # [1] 震度1   微震
    (173, 200, 224),   # [2] 震度2   軽震
    (124, 196, 181),   # [3] 震度3   弱震
    (255, 235, 59),    # [4] 震度4   中震
    (255, 183, 77),    # [5] 震度5弱  強震(弱)
    (255, 112, 67),    # [6] 震度5強  強震(強)
    (229, 57, 53),     # [7] 震度6弱  烈震(弱)
    (183, 28, 28),     # [8] 震度6強  烈震(強)
    (74, 0, 48),       # [9] 震度7   激震
]

# 震度文字 → JMA 色表索引
_SHINDO_INDEX: dict[str, int] = {
    "0": 0, "1": 1, "2": 2, "3": 3, "4": 4,
    "5-": 5, "5+": 6, "6-": 7, "6+": 8, "7": 9,
}

# 罗马数字
_ROMAN: list[str] = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII"]

# 微软雅黑粗体路径
_FONT_PATH = "C:/Windows/Fonts/msyhbd.ttc"


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """加载微软雅黑粗体。"""
    try:
        return ImageFont.truetype(_FONT_PATH, size)
    except (OSError, IOError):
        try:
            return ImageFont.truetype("msyhbd.ttc", size)
        except (OSError, IOError):
            return ImageFont.load_default()


def _estimate_csis(mag: float, depth_km: float = 10.0) -> float:
    """从震级+深度估算 CSIS 烈度（与 presenters._estimate_csis 一致）。"""
    fault_len = 10.0 ** ((mag - 3.821) / 1.86)
    line_dis = depth_km
    hypo_dis = max(
        line_dis - 10.0 - fault_len,
        0.0 - fault_len,
        0.2 * (line_dis - 10.0),
        0.0,
    )
    cea1 = 1.297 * mag - 4.368 * math.log10(0.0 + 15.0) + 5.363
    cea2 = 1.297 * mag - 4.368 * math.log10(hypo_dis + 15.0) + 5.363
    return (cea1 + cea2) / 2.0


def _csis_to_shindo(csis: float) -> str:
    """CSIS → JMA 震度文字。"""
    s = csis * 0.633 - 0.05
    if s >= 7.0:
        return "7"
    if s >= 6.5:
        return "6+"
    if s >= 5.5:
        return "6-"
    if s >= 5.0:
        return "5+"
    if s >= 4.5:
        return "5-"
    if s >= 3.5:
        return "4"
    if s >= 2.5:
        return "3"
    if s >= 1.5:
        return "2"
    if s >= 0.5:
        return "1"
    return "0"


def _get_csis_color(csis: float) -> tuple[int, int, int]:
    """CSIS 值 → CSIS 色表 RGB。"""
    idx = max(1, min(12, int(round(csis))))
    return CSIS_COLORS[idx]


def _get_shindo_color(shindo_str: str) -> tuple[int, int, int]:
    """震度文字 → JMA 色表 RGB。"""
    idx = _SHINDO_INDEX.get(shindo_str, 0)
    return JMA_COLORS[idx]


class IntensityImageRenderer:
    """震度/烈度图片渲染器。"""

    def __init__(self, cache_dir: str):
        self.cache_dir = os.path.join(cache_dir, "intensity_cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        # 字体缓存（懒加载）
        self._font_large: ImageFont.FreeTypeFont | None = None
        self._font_label: ImageFont.FreeTypeFont | None = None
        self._font_sub: ImageFont.FreeTypeFont | None = None

    @property
    def font_large(self) -> ImageFont.FreeTypeFont:
        if self._font_large is None:
            self._font_large = _load_font(52)
        return self._font_large

    @property
    def font_label(self) -> ImageFont.FreeTypeFont:
        if self._font_label is None:
            self._font_label = _load_font(22)
        return self._font_label

    @property
    def font_sub(self) -> ImageFont.FreeTypeFont:
        if self._font_sub is None:
            self._font_sub = _load_font(16)
        return self._font_sub

    def _cache_key(self, prefix: str, mag: float, depth: float) -> str:
        """生成缓存文件名（基于震级+深度，相同值不重复生成）。"""
        return os.path.join(self.cache_dir, f"{prefix}_m{mag:.1f}_d{depth:.0f}.png")

    def _draw_base(self, accent_color: tuple[int, int, int]) -> tuple[Image.Image, ImageDraw.ImageDraw]:
        """创建基础画布 + 左侧色条。"""
        img = Image.new("RGB", (IMAGE_WIDTH, IMAGE_HEIGHT), BG_COLOR)
        draw = ImageDraw.Draw(img)
        # 左侧装饰条
        draw.rectangle([0, 0, ACCENT_BAR_W - 1, IMAGE_HEIGHT - 1], fill=accent_color)
        return img, draw

    def render_shindo(self, mag: float, depth: float = 10.0) -> str | None:
        """渲染震度图片，返回文件路径（缓存命中直接返回）。"""
        csis = _estimate_csis(mag, depth)
        shindo_str = _csis_to_shindo(csis)
        cache_path = self._cache_key("shindo", mag, depth)
        if os.path.exists(cache_path):
            return cache_path

        color = _get_shindo_color(shindo_str)
        img, draw = self._draw_base(color)

        # 左侧标签
        draw.text((22, 14), "预估最大震度", fill=LABEL_COLOR, font=self.font_label)
        draw.text((22, 42), "(推定)", fill=SUB_LABEL_COLOR, font=self.font_sub)

        # 右侧震度值（大号大字）
        bbox = self.font_large.getbbox(shindo_str)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        x = IMAGE_WIDTH - tw - 24
        y = (IMAGE_HEIGHT - th) // 2
        draw.text((x, y), shindo_str, fill=color, font=self.font_large)

        img.save(cache_path, "PNG")
        logger.info(f"[IntensityImg] 震度图已生成 → {cache_path}")
        return cache_path

    def render_intensity(self, mag: float, depth: float = 10.0) -> str | None:
        """渲染烈度图片（罗马数字），返回文件路径（缓存命中直接返回）。"""
        csis = _estimate_csis(mag, depth)
        cache_path = self._cache_key("intensity", mag, depth)
        if os.path.exists(cache_path):
            return cache_path

        val = max(1, min(12, int(round(csis))))
        roman = _ROMAN[val - 1]
        color = _get_csis_color(csis)
        img, draw = self._draw_base(color)

        # 左侧标签
        draw.text((22, 14), "预估最大烈度", fill=LABEL_COLOR, font=self.font_label)
        draw.text((22, 42), "(推定)", fill=SUB_LABEL_COLOR, font=self.font_sub)

        # 右侧烈度值（大号大字）
        bbox = self.font_large.getbbox(roman)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        x = IMAGE_WIDTH - tw - 24
        y = (IMAGE_HEIGHT - th) // 2
        draw.text((x, y), roman, fill=color, font=self.font_large)

        img.save(cache_path, "PNG")
        logger.info(f"[IntensityImg] 烈度图已生成 → {cache_path}")
        return cache_path

    def render_both(self, mag: float, depth: float = 10.0) -> tuple[str | None, str | None]:
        """渲染震度+烈度两张图，返回 (shindo_path, intensity_path)。"""
        try:
            s = self.render_shindo(mag, depth)
            i = self.render_intensity(mag, depth)
            return s, i
        except Exception as e:
            logger.error(f"[IntensityImg] 渲染异常: {e}")
            return None, None
