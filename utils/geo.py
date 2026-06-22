"""
utils/geo.py — 地理坐标和烈度转换工具。

所有函数为纯函数。
"""

from __future__ import annotations

import math


# ── 坐标验证 ──


def validate_lat(lat: float) -> bool:
    """验证纬度 [-90, 90]"""
    return -90.0 <= lat <= 90.0


def validate_lon(lon: float) -> bool:
    """验证经度 [-180, 180]"""
    return -180.0 <= lon <= 180.0


# ── 距离计算 ──


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Haversine 公式计算两点间大圆距离（公里）。
    """
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── 烈度转换 ──

_JMA_TO_MMI: dict[str, float] = {
    "0": 1.0, "1": 2.5, "2": 3.5, "3": 4.5,
    "4": 5.5, "5-": 6.0, "5+": 6.5, "6-": 7.0,
    "6+": 8.0, "7": 9.0,
}


def jma_to_mmi(shindo: str) -> float | None:
    """将 JMA 震度等级转换为 MMI。"""
    return _JMA_TO_MMI.get(shindo.strip().replace(" ", ""))


def mmi_to_jma(mmi: float) -> str:
    """将 MMI 值转换为最近的 JMA 震度等级。"""
    if mmi >= 8.5:
        return "7"
    if mmi >= 7.5:
        return "6+"
    if mmi >= 6.5:
        return "6-"
    if mmi >= 6.0:
        return "5+"
    if mmi >= 5.5:
        return "5-"
    if mmi >= 4.5:
        return "4"
    if mmi >= 3.5:
        return "3"
    if mmi >= 2.5:
        return "2"
    return "1"


# ── 中国烈度（参考） ──

_INTENSITY_DESC: dict[str, str] = {
    "1": "无感", "2": "微感", "3": "有感", "4": "明显",
    "5": "摇晃", "6": "破坏", "7": "损害", "8": "严重",
    "9": "毁坏", "10": "毁灭", "11": "灾难", "12": "巨灾",
}


def intensity_description(value: float) -> str:
    """返回中国烈度对应的文字描述。"""
    key = str(int(round(value)))
    return _INTENSITY_DESC.get(key, str(value))
