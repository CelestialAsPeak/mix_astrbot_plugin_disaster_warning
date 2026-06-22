"""
parser/registry.py — 解析器注册表 + 中央路由配置（CAPQuakeQt 风格）。

使用方式：

    @ParserRegistry.register("cea_fanstudio")
    class CeaEewParser(BaseParser):
        def parse(self, raw: dict) -> list[EewEvent]:
            ...

    # 获取解析器实例
    parser = ParserRegistry.get("cea_fanstudio")
    events = parser.parse(raw_data)
"""

from __future__ import annotations

from typing import Any

from .base import BaseParser

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger


class ParserRegistry:
    """解析器注册表。

    _parsers: source_id -> parser_class 的静态映射。
    解析器类通过 @register 装饰器自动注册。
    """

    _parsers: dict[str, type[BaseParser]] = {}

    @classmethod
    def register(cls, source_id: str):
        """装饰器：将解析器类注册到指定 source_id。

        用法:
            @ParserRegistry.register("cea_fanstudio")
            class CeaEewParser(BaseParser): ...
        """
        def wrapper(parser_cls):
            if source_id in cls._parsers:
                logger.warning(
                    f"[Registry] source_id '{source_id}' 重复注册，"
                    f"覆盖 {cls._parsers[source_id].__name__}"
                )
            cls._parsers[source_id] = parser_cls
            return parser_cls
        return wrapper

    @classmethod
    def get(cls, source_id: str) -> BaseParser | None:
        """获取解析器实例（每次创建新实例，线程安全）。"""
        parser_cls = cls._parsers.get(source_id)
        if parser_cls is None:
            logger.warning(f"[Registry] 未找到 source_id '{source_id}' 的解析器")
            return None
        return parser_cls(source_id)

    @classmethod
    def has(cls, source_id: str) -> bool:
        """检查是否存在指定 source_id 的解析器。"""
        return source_id in cls._parsers

    @classmethod
    def list_registered(cls) -> list[str]:
        """返回所有已注册的 source_id 列表。"""
        return sorted(cls._parsers.keys())

    @classmethod
    def validate_sources(cls, sources: dict[str, Any]) -> list[str]:
        """校验 sources.json 中的 parser_name 是否都有对应的注册解析器。

        Returns:
            缺失的 source_id 列表（空列表表示全部匹配）。
        """
        missing = []
        for source_id, entry in sources.items():
            if source_id.startswith("_"):
                continue
            parser_name = entry.get("parser_name", "")
            if parser_name and parser_name not in cls._parsers:
                missing.append(source_id)
        if missing:
            logger.warning(
                f"[Registry] 以下数据源缺少解析器: {missing}"
            )
        return missing


# ── 快捷函数（兼容旧代码调用方式）──

def create_parser(source_id: str) -> BaseParser | None:
    """按 source_id 创建解析器实例。"""
    return ParserRegistry.get(source_id)


__all__ = [
    "ParserRegistry",
    "create_parser",
]
