"""
admin/server.py — FastAPI 管理服务器。

提供 REST API 和静态前端服务。
"""

from __future__ import annotations

import asyncio
from typing import Any

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger


class WebAdminServer:
    """Web 管理端服务器。"""

    def __init__(self, disaster_service=None, config: dict | None = None):
        self.disaster_service = disaster_service
        self.config = config or {}
        self._server = None
        self._task: asyncio.Task | None = None
        self._app = None

    def _setup_app(self):
        """初始化 FastAPI 应用。"""
        try:
            from fastapi import FastAPI
            from fastapi.middleware.cors import CORSMiddleware
            self._app = FastAPI(title="Mix灾害预警")
            self._app.add_middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )
            self._register_routes()
        except ImportError:
            logger.warning("[Admin] FastAPI 未安装，管理端不可用")

    def _register_routes(self):
        """注册 API 路由。"""
        if not self._app:
            return

        from fastapi import Request

        @self._app.get("/api/status")
        async def get_status():
            return {"status": "running"}

        @self._app.get("/api/stats")
        async def get_stats():
            return {"events": 0}

        @self._app.get("/health")
        async def health():
            return {"ok": True}

    async def start(self):
        """启动服务器。"""
        if not self._app:
            self._setup_app()
        if not self._app:
            return

        host = self.config.get("host", "127.0.0.1")
        port = self.config.get("port", 18331)

        try:
            import uvicorn
            config = uvicorn.Config(
                self._app, host=host, port=port,
                log_level="info", access_log=False,
            )
            self._server = uvicorn.Server(config)
            self._task = asyncio.create_task(self._server.serve())
            logger.info(f"[Admin] Web 管理端启动: http://{host}:{port}")
        except Exception as e:
            logger.warning(f"[Admin] 启动失败: {e}")

    async def stop(self):
        """停止服务器。"""
        if self._server:
            self._server.should_exit = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def notify_event(self, event_summary: dict):
        """向管理端广播事件。"""
        pass  # WebSocket 推送需额外实现
