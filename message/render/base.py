"""
message/render/base.py — 渲染引擎基类。

提供 Playwright 截图通用能力，子类只需实现 build_html()。
"""

from __future__ import annotations

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger


class BaseRender:
    """渲染器基类。"""

    def __init__(self, browser_manager=None):
        self._browser = browser_manager

    def set_browser(self, browser_manager):
        self._browser = browser_manager

    def build_html(self) -> str:
        """构建 HTML 字符串。"""
        raise NotImplementedError

    async def render(self, viewport: dict | None = None) -> bytes | None:
        """渲染为 PNG 图片。"""
        if not self._browser:
            return None
        html = self.build_html()
        return await self._browser.screenshot(html, viewport=viewport)
