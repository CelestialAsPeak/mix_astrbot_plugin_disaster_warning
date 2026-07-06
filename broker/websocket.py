"""
broker/websocket.py — WebSocket 连接管理器。

支持多URL故障转移、线性退避重连、连接超时检测。
管理 FAN Studio、Wolfx、P2P、GlobalQuake 等 WebSocket 数据源。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger


class WebSocketConnection:
    """单个 WebSocket 连接。"""

    def __init__(
        self,
        name: str,
        url: str,
        backup_url: str = "",
        on_message: Callable | None = None,
        on_status: Callable | None = None,
        reconnect_delay: int = 3,
        max_reconnect_delay: int = 30,
    ):
        self.name = name
        self.url = url
        self.backup_url = backup_url
        self._on_message = on_message
        self._on_status = on_status
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay

        self._ws = None
        self._session = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._current_delay = reconnect_delay
        self._current_url = url
        self._errors = 0

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def start(self):
        """启动连接循环。"""
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        """停止连接。"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._close_ws()

    async def _run(self):
        """运行循环：连接 → 接收消息 → 断线重连。"""
        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[WS:{self.name}] 连接异常: {e}")
                await self._notify_status("error", str(e))

            if not self._running:
                break

            # 退避重连
            await asyncio.sleep(self._current_delay)
            self._current_delay = min(
                self._current_delay * 1.5, self._max_reconnect_delay
            )

    async def _connect_and_listen(self):
        """连接并监听消息。"""
        try:
            import aiohttp

            self._session = aiohttp.ClientSession()
            urls = [self._current_url]
            if self.backup_url and self._current_url != self.backup_url:
                urls.append(self.backup_url)

            for url in urls:
                try:
                    self._ws = await self._session.ws_connect(
                        url, heartbeat=30, timeout=10
                    )
                    self._current_url = url
                    self._current_delay = self._reconnect_delay  # 重置退避
                    self._errors = 0
                    await self._notify_status("connected", url)
                    break
                except Exception as e:
                    logger.warning(f"[WS:{self.name}] 连接 {url} 失败: {e}")
                    continue
            else:
                # 所有 URL 都失败
                self._errors += 1
                await self._notify_status("disconnected", "all_urls_failed")
                return

            # 接收消息
            async for msg in self._ws:
                if not self._running:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await self._handle_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    break

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[WS:{self.name}] 监听循环异常: {e}")
        finally:
            await self._close_ws()
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None

    async def _handle_message(self, data: str | bytes):
        """处理收到的消息。"""
        if self._on_message:
            try:
                await self._on_message(self.name, data)
            except Exception as e:
                logger.error(f"[WS:{self.name}] 消息处理异常: {e}")

    async def _close_ws(self):
        """关闭 WebSocket。"""
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        await self._notify_status("disconnected", "closed")

    async def _notify_status(self, status: str, detail: str = ""):
        """通知状态变更。"""
        if self._on_status:
            try:
                await self._on_status(self.name, status, detail)
            except Exception:
                pass


class WebSocketManager:
    """WebSocket 连接管理器 — 管理多条连接。"""

    def __init__(self, config: dict | None = None):
        self._connections: dict[str, WebSocketConnection] = {}
        self.config = config or {}
        self._on_message: Callable | None = None
        self._on_status: Callable | None = None

    def set_message_handler(self, handler: Callable):
        """设置全局消息处理器。"""
        self._on_message = handler

    def set_status_handler(self, handler: Callable):
        """设置全局状态处理器。"""
        self._on_status = handler

    def add_connection(
        self,
        name: str,
        url: str,
        backup_url: str = "",
        reconnect_delay: int = 3,
    ) -> WebSocketConnection:
        """添加连接配置。"""
        conn = WebSocketConnection(
            name=name,
            url=url,
            backup_url=backup_url,
            on_message=self._on_message,
            on_status=self._on_status,
            reconnect_delay=reconnect_delay,
        )
        self._connections[name] = conn
        return conn

    async def start_all(self):
        """启动所有连接。"""
        for name, conn in self._connections.items():
            logger.info(f"[WS] 启动连接: {name}")
            await conn.start()
            await asyncio.sleep(0.5)  # 错峰

    async def stop_all(self):
        """停止所有连接。"""
        for name, conn in self._connections.items():
            await conn.stop()

    async def reconnect(self, name: str) -> bool:
        """重连指定连接。"""
        conn = self._connections.get(name)
        if conn is None:
            return False
        await conn.stop()
        await asyncio.sleep(1)
        await conn.start()
        return True

    def get_status(self) -> dict[str, bool]:
        """获取所有连接状态。"""
        return {name: conn.is_connected for name, conn in self._connections.items()}
