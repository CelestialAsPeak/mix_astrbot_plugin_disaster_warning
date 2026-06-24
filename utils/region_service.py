"""
区域名称翻译服务 — 经纬度 → 中文地名。

使用 F-E 区划数据（1°×1° 网格, 180x360 矩阵），
将经纬度映射到中文区域描述（如 "美国加利福尼亚附近"）。

数据来自旧版 astrbot_plugin_disaster_warning 的 fe_regions_data.json。
"""

from __future__ import annotations

import json
from pathlib import Path

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger


_DATA_FILE: str | None = None  # 由 init_region_service() 设置
_fe_numbers: list[list[int]] | None = None
_fe_names: list[str] | None = None


def init_region_service(data_path: str | Path) -> None:
    """初始化区域服务（加载 JSON 数据文件）。"""
    global _DATA_FILE, _fe_numbers, _fe_names
    _DATA_FILE = str(data_path)
    _load_data()


def _load_data() -> None:
    global _fe_numbers, _fe_names
    try:
        with open(_DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
        _fe_numbers = data["fe_numbers"]
        _fe_names = data["fe_names"]
        logger.info(f"[Region] 区域数据已加载: {len(_fe_names)} 个地名, {len(_fe_numbers)}x{len(_fe_numbers[0])} 网格")
    except Exception as e:
        logger.warning(f"[Region] 区域数据加载失败: {e}")
        _fe_numbers = [[729] * 360 for _ in range(180)]
        _fe_names = ["未定义"] * 729


def translate_place_name(
    original_name: str,
    lat: float,
    lng: float,
    fallback_to_original: bool = True,
) -> str:
    """经纬度 → 中文地名，失败回退到原文。"""
    global _fe_numbers, _fe_names
    if _fe_numbers is None or _fe_names is None:
        return original_name if fallback_to_original else ""

    try:
        lat_i = min(max(int(lat + 90), 0), 179)
        lng_i = min(max(int(lng + 180), 0), 359)
        region_number = _fe_numbers[lat_i][lng_i]
        if 1 <= region_number <= len(_fe_names):
            name = _fe_names[region_number - 1]
            if name and name != "未定义":
                if not name.endswith("附近"):
                    name += "附近"
                return name
    except (IndexError, ValueError, TypeError):
        pass

    return original_name if fallback_to_original else ""
