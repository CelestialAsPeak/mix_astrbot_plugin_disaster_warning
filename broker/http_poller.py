"""
broker/http_poller.py — HTTP 轮询管理器。

管理所有 HTTP 轮询数据源：定时拉取、超时控制、重试。
支持错峰启动（按 source_id 哈希偏移），避免同时发出大量请求。
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Callable

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger


class HttpPoller:
    """单个 HTTP 轮询任务。"""

    def __init__(
        self,
        name: str,
        url: str,
        interval: int = 60,
        handler: Callable | None = None,
        raw_text: bool = False,
        headers: dict | None = None,
    ):
        self.name = name
        self.url = url
        self.interval = interval
        self._handler = handler
        self._raw_text = raw_text
        self._headers = headers or {}
        self._task: asyncio.Task | None = None
        self._running = False
        self._session = None

    async def start(self):
        """启动轮询。"""
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        """停止轮询。"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _run(self):
        """轮询循环。"""
        # 立即请求一次（错峰 ≤10s 防暴发）
        offset = (hashlib.md5(self.name.encode()).digest()[0] / 256.0) * min(self.interval, 10)
        await asyncio.sleep(offset)

        while self._running:
            try:
                await self._fetch()
            except Exception as e:
                logger.debug(f"[Poller:{self.name}] fetch error: {e}")

            await asyncio.sleep(self.interval)

    async def _fetch(self):
        """执行 HTTP 请求。"""
        import aiohttp

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self._headers)

        try:
            async with self._session.get(self.url, timeout=60) as resp:
                if resp.status != 200:
                    logger.warning(f"[Poller:{self.name}] HTTP {resp.status}")
                    return

                if self._raw_text:
                    data = await resp.text()
                else:
                    data = await resp.json()

                if self._handler:
                    await self._handler(self.name, data)

        except asyncio.TimeoutError:
            logger.warning(f"[Poller:{self.name}] 请求超时")
        except Exception as e:
            logger.warning(f"[Poller:{self.name}] 请求失败: {e}")


class HttpPollManager:
    """HTTP 轮询管理器。"""

    def __init__(self):
        self._pollers: dict[str, HttpPoller] = {}

    def add_poller(
        self,
        name: str,
        url: str,
        interval: int = 60,
        handler: Callable | None = None,
        raw_text: bool = False,
        headers: dict | None = None,
    ) -> HttpPoller:
        poller = HttpPoller(
            name=name,
            url=url,
            interval=interval,
            handler=handler,
            raw_text=raw_text,
            headers=headers,
        )
        self._pollers[name] = poller
        return poller

    async def start_all(self):
        for poller in self._pollers.values():
            await poller.start()

    async def fetch_one(self, name: str) -> bool:
        """立即触发单个轮询器的请求（不等待定时周期），返回是否成功触发。"""
        poller = self._pollers.get(name)
        if poller is None:
            return False
        try:
            await poller._fetch()
            return True
        except Exception:
            return False

    async def stop_all(self):
        for poller in self._pollers.values():
            await poller.stop()
