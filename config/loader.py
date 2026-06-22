"""
ConfigLoader — 配置加载器。

负责从 JSON 文件加载 sources.json（数据源目录）和 defaults.json（运行时默认值）。
失败时返回空字典，确保插件在配置缺失时仍可降级启动。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger
    logger.warning = logger.warning
    logger.error = logger.error


def _resolve_path(relative_path: str) -> Path:
    """根据相对于本文件的路径解析绝对路径。"""
    return Path(__file__).resolve().parent / relative_path


def load_json(filename: str, default: Any = None) -> Any:
    """加载 JSON 文件，失败时返回 default。"""
    path = _resolve_path(filename)
    if not path.exists():
        logger.warning(f"[Mix] 配置文件不存在: {path}，使用默认值")
        return default if default is not None else {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"[Mix] 配置文件加载失败: {path} ({e})")
        return default if default is not None else {}


def load_sources() -> dict[str, Any]:
    """加载数据源目录。"""
    return load_json("sources.json", {})


def load_defaults() -> dict[str, Any]:
    """加载运行时配置默认值。"""
    return load_json("defaults.json", {})


def get_source_entry(sources: dict, source_id: str) -> dict | None:
    """从 sources 字典中获取指定数据源条目。"""
    entry = sources.get(source_id)
    if not isinstance(entry, dict):
        return None
    return entry


__all__ = ["load_sources", "load_defaults", "get_source_entry"]
