"""
message/render/intensity_image_renderer.py

震度/烈度图片渲染器 — 使用 Pillow 生成带 CAPQuakeQt 配色的图片。
缓存已生成的图片，避免重复渲染。

图片尺寸: 480×90px
设计：以震度/烈度色为背景，一行文本：标签 + 大号数值
"""

from __future__ import annotations

import math
import os
import re

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger

from PIL import Image, ImageDraw, ImageFont


# ── 图片尺寸 ──
IMAGE_WIDTH = 750
IMAGE_HEIGHT = 235

# ── CSIS 12 级烈度配色（CAPQuakeQt CSIS_COLORS） ──
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


def _normalize_shindo(label: str) -> str:
    """统一各种震度标签格式 → 内部键（"0"-"7"、"5-"/"5+"、"6-"/"6+"）。

    支持的输入格式：
      - "震度5弱" / "5弱" / "5-" → "5-"
      - "震度5強" / "5強" / "5+" → "5+"
      - "震度7"   / "7"          → "7"
      - "震度0"～"震度4"         → "0"～"4"
    """
    s = label.strip()
    # 去掉 "震度" 前缀
    s = s.replace("震度", "")
    # 中文字 → 符号
    s = s.replace("弱", "-").replace("強", "+").replace("强", "+")
    # 直接命中
    if s in _SHINDO_INDEX:
        return s
    # 尝试纯数字
    try:
        v = float(s)
    except ValueError:
        return "0"
    if v <= 0.5:
        return "0"
    if v <= 1.5:
        return "1"
    if v <= 2.5:
        return "2"
    if v <= 3.5:
        return "3"
    if v <= 4.5:
        return "4"
    if v <= 5.0:
        return "5-"
    if v <= 5.5:
        return "5+"
    if v <= 6.0:
        return "6-"
    if v <= 6.5:
        return "6+"
    return "7"

# 微软雅黑粗体路径
_FONT_PATH = "C:/Windows/Fonts/msyhbd.ttc"

# 缓存版本（改版时递增以丢弃旧缓存）
_CACHE_VERSION = "v2"


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


def _text_color_for_bg(r: int, g: int, b: int) -> tuple[int, int, int]:
    """根据背景色亮度选择文字颜色（CAPQuakeQt text_color_for_bg 算法）。"""
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    return (18, 18, 22) if lum > 140 else (220, 225, 230)


class IntensityImageRenderer:
    """震度/烈度图片渲染器。"""

    def __init__(self, cache_dir: str):
        self.cache_dir = os.path.join(cache_dir, "intensity_cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self._font_value: ImageFont.FreeTypeFont | None = None
        self._font_label: ImageFont.FreeTypeFont | None = None

    @property
    def font_value(self) -> ImageFont.FreeTypeFont:
        if self._font_value is None:
            self._font_value = _load_font(120)
        return self._font_value

    @property
    def font_label(self) -> ImageFont.FreeTypeFont:
        if self._font_label is None:
            self._font_label = _load_font(52)
        return self._font_label

    def _cache_key(self, prefix: str, mag: float, depth: float) -> str:
        """生成缓存文件名。"""
        return os.path.join(
            self.cache_dir, f"{prefix}_{_CACHE_VERSION}_m{mag:.1f}_d{depth:.0f}.png"
        )

    def _render_single(
        self, cache_path: str, label: str, value: str, bg_color: tuple[int, int, int],
    ) -> str | None:
        """渲染单张图：纯色背景 + 一行标签 大号数值靠右。"""
        if os.path.exists(cache_path):
            return cache_path

        img = Image.new("RGB", (IMAGE_WIDTH, IMAGE_HEIGHT), bg_color)
        draw = ImageDraw.Draw(img)
        txt_color = _text_color_for_bg(*bg_color)

        # 标签靠左垂直居中（用 bbox 精确居中）
        lb = self.font_label.getbbox(label)
        ly = (IMAGE_HEIGHT - (lb[3] - lb[1])) // 2 - lb[1]
        draw.text((36, ly), label, fill=txt_color, font=self.font_label)

        # 数值靠右垂直居中，但往左挪一些（右留空让视觉平衡）
        vb = self.font_value.getbbox(value)
        vw = vb[2] - vb[0]
        vy = (IMAGE_HEIGHT - (vb[3] - vb[1])) // 2 - vb[1]
        draw.text((IMAGE_WIDTH - vw - 80, vy), value, fill=txt_color, font=self.font_value)

        img.save(cache_path, "PNG")
        logger.info(f"[IntensityImg] 已生成 → {os.path.basename(cache_path)}")
        return cache_path

        img.save(cache_path, "PNG")
        logger.info(f"[IntensityImg] 已生成 → {os.path.basename(cache_path)}")
        return cache_path

    def render_shindo(self, mag: float, depth: float = 10.0) -> str | None:
        """渲染震度图片。"""
        if depth is None:
            return self._render_single(
                self._cache_key_actual("shindo_unknown", "不明"),
                "预估最大震度", "不明", (120, 120, 120),
            )
        csis = _estimate_csis(mag, depth)
        shindo_str = _csis_to_shindo(csis)
        color = _get_shindo_color(shindo_str)
        cache_path = self._cache_key("shindo", mag, depth)
        return self._render_single(cache_path, "预估最大震度", shindo_str, color)

    def render_intensity(self, mag: float, depth: float = 10.0) -> str | None:
        """渲染烈度图片（罗马数字）。"""
        if depth is None:
            return self._render_single(
                self._cache_key_actual("intensity_unknown", "不明"),
                "预估最大烈度", "不明", (120, 120, 120),
            )
        csis = _estimate_csis(mag, depth)
        val = max(1, min(12, int(round(csis))))
        roman = _ROMAN[val - 1]
        color = _get_csis_color(csis)
        cache_path = self._cache_key("intensity", mag, depth)
        return self._render_single(cache_path, "预估最大烈度", roman, color)

    def _cache_key_actual(self, prefix: str, value_str: str) -> str:
        """生成基于实际值（而非震级+深度）的缓存文件名。"""
        safe = re.sub(r'[\\/:*?"<>|]', '_', value_str)
        return os.path.join(self.cache_dir, f"{prefix}_{_CACHE_VERSION}_{safe}.png")

    def render_shindo_actual(self, shindo_label: str, display_label: str = "最大震度") -> str | None:
        """用实际 JMA 震度数据渲染（不估算）。"""
        shindo_key = _normalize_shindo(shindo_label)
        color = _get_shindo_color(shindo_key)
        cache_path = self._cache_key_actual("shindo_actual", shindo_label)
        return self._render_single(cache_path, display_label, shindo_label, color)

    def render_intensity_actual(self, mmi_str: str, display_label: str = "最大烈度") -> str | None:
        """用实际 MMI/烈度数据渲染（不估算）。"""
        try:
            mmi = float(mmi_str)
        except (ValueError, TypeError):
            mmi = 6.0
        val = max(1, min(12, int(round(mmi))))
        roman = _ROMAN[val - 1]
        color = _get_csis_color(float(val))
        cache_path = self._cache_key_actual("intensity_actual", str(val))
        return self._render_single(cache_path, display_label, roman, color)

    def render_both(self, mag: float, depth: float = 10.0) -> tuple[str | None, str | None]:
        """渲染震度+烈度两张图。"""
        try:
            s = self.render_shindo(mag, depth)
            i = self.render_intensity(mag, depth)
            return s, i
        except Exception as e:
            logger.error(f"[IntensityImg] 渲染异常: {e}")
            return None, None
