"""
GlobalQuake 卡片构建器 — 用 Playwright 渲染 GQ 专属卡片（HTML → PNG）。
"""

from __future__ import annotations

import os
from datetime import datetime

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger

from jinja2 import Template

from ..builders.global_quake_display_context import build_gq_card_context


class GlobalQuakeCardBuilder:
    """GlobalQuake 卡片构建器。"""

    def __init__(self, *, plugin_root: str, temp_dir: str, browser_manager):
        self.plugin_root = plugin_root
        self.temp_dir = temp_dir
        self.browser_manager = browser_manager
        # 模板缓存，避免每次重新读盘
        self._template_cache: dict[str, str] = {}

    def _load_template(self, template_name: str) -> str | None:
        """加载 HTML 模板，带缓存。"""
        if template_name in self._template_cache:
            return self._template_cache[template_name]

        template_path = os.path.join(
            self.plugin_root, "resources", "card_templates",
            template_name, "global_quake.html",
        )
        if not os.path.exists(template_path):
            logger.warning(f"[GQ] 模板不存在: {template_path}")
            return None

        try:
            with open(template_path, encoding="utf-8") as f:
                content = f.read()
            self._template_cache[template_name] = content
            return content
        except Exception as e:
            logger.error(f"[GQ] 模板加载失败: {template_path}: {e}")
            return None

    async def build(
        self,
        envelope,
        *,
        template_name: str = "Aurora",
        map_source: str = "PetalMap矢量图亮",
        tile_url: str = "",
        zoom_level: int = 5,
        viewport: dict | None = None,
    ) -> str | None:
        """构建 GQ 卡片图片，返回 base64 字符串。

        Args:
            envelope: EventEnvelope（含 EewEvent）
            template_name: "Aurora" 或 "DarkNight"
            map_source: 地图源名称
            tile_url: 地图瓦片 URL（JS 模板），为空则从 map_source 自动获取
            zoom_level: 地图初始缩放级别
            viewport: 浏览器视口大小

        Returns:
            base64 编码的 PNG 图片字符串，失败返回 None
        """
        try:
            event = envelope.event
            metadata = envelope.metadata
            identity = envelope.identity

            # 自动解析 tile_url（如果未传入）
            if not tile_url:
                try:
                    from ...utils.map_tile_sources import get_tile_url_js
                    tile_url = get_tile_url_js(map_source)
                except Exception:
                    pass

            # 1. 构建展示上下文
            context = build_gq_card_context(
                event,
                metadata=metadata,
                identity=identity,
                zoom_level=zoom_level,
                map_source=map_source,
                tile_url=tile_url,
            )

            # 2. 加载 Leaflet 资源
            resources_dir = os.path.join(self.plugin_root, "resources", "card_templates")
            leaflet_js_path = os.path.join(resources_dir, "leaflet.js")
            leaflet_css_path = os.path.join(resources_dir, "leaflet.css")
            helper_path = os.path.join(resources_dir, "map_render_helper.js")

            if os.path.exists(leaflet_js_path):
                context["leaflet_js_url"] = f"file://{os.path.abspath(leaflet_js_path)}"
            if os.path.exists(leaflet_css_path):
                context["leaflet_css_url"] = f"file://{os.path.abspath(leaflet_css_path)}"
            if os.path.exists(helper_path):
                with open(helper_path, encoding="utf-8") as f:
                    context["map_render_helper_js"] = f.read()

            # 3. 加载并渲染模板
            template_content = self._load_template(template_name)
            if template_content is None:
                return None

            template = Template(template_content)
            html_content = template.render(**context)

            # 4. 用 Playwright 渲染为图片
            image_filename = f"gq_card_{identity.event_id}_{int(datetime.now().timestamp())}.png"
            image_path = os.path.join(self.temp_dir, image_filename)

            result_path = await self.browser_manager.render_card(
                html_content, image_path, selector="#card-wrapper",
                viewport=viewport,
            )

            if result_path and os.path.exists(result_path):
                import base64
                with open(result_path, "rb") as f:
                    b64_data = base64.b64encode(f.read()).decode()
                try:
                    os.unlink(result_path)
                except Exception:
                    pass
                logger.info(f"[GQ] 卡片渲染成功: {image_filename}")
                return b64_data

            logger.warning("[GQ] 卡片渲染失败: 未生成图片")
            return None

        except Exception as e:
            logger.error(f"[GQ] 卡片构建异常: {e}")
            return None
