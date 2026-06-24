"""
浏览器管理器。

负责管理浏览器实例、页面池、并发渲染与远程渲染模式切换，
为卡片、地图等图片渲染场景提供统一的浏览器基础设施。
"""

import asyncio
import os
import tempfile
import time
from pathlib import Path

import aiohttp
from playwright.async_api import Browser, Page, async_playwright

from astrbot.api import logger


class BrowserManager:
    """浏览器管理器。"""

    def __init__(
        self,
        pool_size: int = 8,
        telemetry=None,
        mode: str = "local",
        server_url: str = "",
    ):
        """初始化浏览器管理器。"""
        self.pool_size = pool_size
        self._browser: Browser | None = None
        self._playwright = None
        # 远程连接场景下可能需要保留上下文对象引用。
        self._context = None
        self._page_pool: asyncio.Queue = asyncio.Queue(maxsize=pool_size)
        # 信号量用于限制同时渲染数量，页面创建锁与初始化锁用于避免并发竞争。
        self._semaphore = asyncio.Semaphore(pool_size)
        self._page_creation_lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._initialized = False
        self._closed = False
        self._telemetry = telemetry
        self._mode = mode
        self._server_url = server_url

    def _truncate_debug_text(self, value, limit: int = 240) -> str:
        """截断浏览器侧日志文本，避免单条日志过长。"""
        text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
        if len(text) <= limit:
            return text
        return f"{text[:limit]}..."

    async def _log_page_diagnostics(self, page: Page, *, reason: str) -> None:
        """输出页面级诊断信息，辅助定位资源加载、脚本执行与选择器状态问题。"""
        try:
            diagnostics = await page.evaluate(
                """
                () => {
                    const mapEl = document.querySelector('#map-container');
                    const cardEl = document.querySelector('#card-wrapper') || document.querySelector('.quake-card');
                    const html = document.documentElement;
                    const body = document.body;
                    return {
                        title: document.title || '',
                        readyState: document.readyState,
                        bodyClasses: body ? body.className || '' : '',
                        mapReady: !!document.querySelector('.map-ready'),
                        mapContainerExists: !!mapEl,
                        mapContainerSize: mapEl ? {
                            width: mapEl.clientWidth,
                            height: mapEl.clientHeight,
                        } : null,
                        cardExists: !!cardEl,
                        cardSize: cardEl ? {
                            width: cardEl.clientWidth,
                            height: cardEl.clientHeight,
                        } : null,
                        htmlLength: html ? (html.outerHTML || '').length : 0,
                    };
                }
                """
            )
            logger.warning(
                f"[灾害预警] 页面诊断（{reason}）：当前文档状态为 {diagnostics.get('readyState')}，"
                f"地图就绪标记{'已出现' if diagnostics.get('mapReady') else '尚未出现'}，"
                f"地图容器{'已找到' if diagnostics.get('mapContainerExists') else '未找到'}，"
                f"地图区域尺寸为 {diagnostics.get('mapContainerSize')}，"
                f"卡片容器{'已找到' if diagnostics.get('cardExists') else '未找到'}，"
                f"卡片尺寸为 {diagnostics.get('cardSize')}，"
                f"页面 body 类名为“{self._truncate_debug_text(diagnostics.get('bodyClasses'))}”，"
                f"页面 HTML 长度约为 {diagnostics.get('htmlLength')} 个字符。"
            )
        except Exception as diag_err:
            logger.warning(f"[灾害预警] 页面诊断({reason})失败: {diag_err}")

    async def initialize(self):
        """初始化浏览器和页面池"""
        async with self._init_lock:
            if self._initialized:
                logger.debug("[灾害预警] 浏览器已初始化，跳过")
                return

            try:
                # 远程模式使用 HTTP API，不需要初始化 Playwright
                if self._mode == "remote":
                    logger.info(
                        f"[灾害预警] 远程模式：使用 browserless HTTP API ({self._server_url})"
                    )
                    self._initialized = True
                    return

                logger.info(f"[灾害预警] 正在启动浏览器（模式：{self._mode}）...")
                start_time = time.time()

                # 启动 Playwright
                self._playwright = await async_playwright().start()

                # 本地模式：启动本地浏览器
                self._browser = await self._playwright.chromium.launch(
                    args=["--no-sandbox", "--disable-setuid-sandbox"]
                )
                logger.info("[灾害预警] 本地浏览器启动成功")

                # 本地模式：直接创建页面池
                await self._initialize_local_page_pool()

                elapsed = time.time() - start_time
                self._initialized = True
                logger.info(
                    f"[灾害预警] 浏览器启动完成，耗时 {elapsed:.2f}秒，页面池大小: {self.pool_size}"
                )

            except Exception as e:
                logger.error(f"[灾害预警] 浏览器初始化失败: {e}")
                # 上报浏览器初始化错误到遥测
                if self._telemetry and self._telemetry.enabled:
                    await self._telemetry.track_error(
                        e, module="core.browser_manager.initialize"
                    )
                # 清理已创建的资源
                await self._cleanup()
                raise

    async def _initialize_local_page_pool(self):
        """初始化本地浏览器的页面池"""
        for i in range(self.pool_size):
            try:
                page = await asyncio.wait_for(
                    self._browser.new_page(
                        viewport={"width": 800, "height": 800}, device_scale_factor=2
                    ),
                    timeout=10.0,
                )
                await self._page_pool.put(page)
                logger.debug(f"[灾害预警] 页面 {i + 1}/{self.pool_size} 已创建")
            except asyncio.TimeoutError:
                logger.error(f"[灾害预警] 创建页面 {i + 1} 超时")
                if i == 0:
                    raise  # 如果第一个页面就失败，抛出异常
                break  # 部分页面创建成功，继续使用
            except Exception as e:
                logger.error(f"[灾害预警] 创建页面 {i + 1} 失败: {e}")
                if i == 0:
                    raise
                break

    async def _initialize_remote_page_pool(self):
        """初始化远程浏览器的页面池（兼容 browserless CDP）"""
        try:
            # browserless CDP：必须使用默认 context
            contexts = self._browser.contexts
            logger.debug(f"[灾害预警] 发现 {len(contexts)} 个现有 context")

            if contexts:
                # 使用第一个 context（browserless 的默认 context）
                self._context = contexts[0]
                logger.debug("[灾害预警] 使用现有 context")
            else:
                # 没有现有 context，创建新的
                logger.debug("[灾害预警] 创建新 context")
                self._context = await asyncio.wait_for(
                    self._browser.new_context(
                        viewport={"width": 800, "height": 800},
                        device_scale_factor=2,
                    ),
                    timeout=15.0,
                )

            # 从 context 创建页面
            for i in range(self.pool_size):
                try:
                    page = await asyncio.wait_for(
                        self._context.new_page(), timeout=10.0
                    )
                    await self._page_pool.put(page)
                    logger.debug(f"[灾害预警] 页面 {i + 1}/{self.pool_size} 已创建")
                except asyncio.TimeoutError:
                    logger.error(f"[灾害预警] 创建页面 {i + 1} 超时")
                    if i == 0:
                        raise
                    break
                except Exception as e:
                    logger.error(f"[灾害预警] 创建页面 {i + 1} 失败: {e}")
                    if i == 0:
                        raise
                    break

            # 检查是否至少有一个页面可用
            if self._page_pool.qsize() == 0:
                raise RuntimeError("无法创建任何可用页面")

            logger.info(
                f"[灾害预警] 远程浏览器页面池初始化完成，可用页面: {self._page_pool.qsize()}"
            )

        except asyncio.TimeoutError:
            logger.error("[灾害预警] 远程浏览器页面池初始化超时")
            raise RuntimeError(
                "远程浏览器页面池初始化超时，请检查网络或增加 browserless 超时设置"
            )
        except Exception as e:
            logger.error(f"[灾害预警] 远程浏览器页面池初始化失败: {e}")
            raise

    async def render_card(
        self,
        html_content: str,
        output_path: str,
        selector: str = "#card-wrapper",
        wait_until: str = "domcontentloaded",
        viewport: dict | None = None,
        goto_timeout: int = 90000,
    ) -> str | None:
        """把 HTML 内容渲染为图片文件。

        Args:
            html_content: HTML 字符串
            output_path: 输出图片路径
            selector: 截图目标元素选择器
            wait_until: 页面加载完成条件
            viewport: 可选，临时设置视口大小，如 {"width": 1600, "height": 1200}
            goto_timeout: page.goto 超时毫秒数，默认 90s
        """
        # 远程模式直接走 HTTP 渲染接口，本地模式则复用页面池执行截图。
        if self._mode == "remote":
            if not self._initialized:
                logger.warning("[灾害预警] 浏览器未初始化，尝试初始化...")
                await self.initialize()
            return await self._render_card_via_http(html_content, output_path, selector)

        # 本地模式：使用 Playwright
        if not self._initialized:
            logger.warning("[灾害预警] 浏览器未初始化，尝试初始化...")
            await self.initialize()

        if self._closed:
            logger.error("[灾害预警] 浏览器已关闭，无法渲染")
            return None

        page: Page | None = None
        start_time = time.time()

        acquired_semaphore = False
        page_broken = False  # 标记页面是否损坏，损坏的页面不放回池
        console_messages: list[str] = []
        page_errors: list[str] = []
        request_failures: list[str] = []
        try:
            # 并发控制 - 限制同时渲染的数量
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=20.0)
                acquired_semaphore = True
            except asyncio.TimeoutError:
                logger.error("[灾害预警] 等待渲染信号量超时，系统负载过高")
                return None

            try:
                # 本地模式：从池中获取页面
                try:
                    page = await asyncio.wait_for(self._page_pool.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.error("[灾害预警] 从池中获取页面对象超时")
                    return None

                try:

                    def _record_console(msg):
                        try:
                            location = msg.location or {}
                            entry = (
                                f"[{msg.type}] {self._truncate_debug_text(msg.text)}"
                                f" @ {location.get('url', '')}:{location.get('lineNumber', '')}:{location.get('columnNumber', '')}"
                            )
                            console_messages.append(entry)
                            if msg.type in {"error", "warning"}:
                                logger.warning(f"[灾害预警] 页面控制台{entry}")
                        except Exception as hook_err:
                            logger.debug(f"[灾害预警] 记录控制台日志失败: {hook_err}")

                    def _record_page_error(exc):
                        try:
                            text = self._truncate_debug_text(exc)
                            page_errors.append(text)
                            logger.warning(f"[灾害预警] 页面脚本异常: {text}")
                        except Exception as hook_err:
                            logger.debug(f"[灾害预警] 记录页面脚本异常失败: {hook_err}")

                    def _record_request_failed(req):
                        try:
                            failure = req.failure
                            failure_text = ""
                            if failure:
                                if isinstance(failure, dict):
                                    failure_text = failure.get("errorText", "")
                                else:
                                    failure_text = str(failure)
                            entry = f"{req.method} {req.url} -> {self._truncate_debug_text(failure_text or 'unknown failure')}"
                            request_failures.append(entry)
                            logger.warning(f"[灾害预警] 页面资源请求失败: {entry}")
                        except Exception as hook_err:
                            logger.debug(f"[灾害预警] 记录请求失败失败: {hook_err}")

                    page.on("console", _record_console)
                    page.on("pageerror", _record_page_error)
                    page.on("requestfailed", _record_request_failed)

                    # 如果调用方指定了临时视口尺寸，在导航前设置
                    original_viewport = None
                    if viewport:
                        try:
                            original_viewport = page.viewport_size
                            await page.set_viewport_size(viewport)
                            logger.debug(f"[灾害预警] 临时视口已设为 {viewport}")
                        except Exception as e:
                            logger.warning(f"[灾害预警] 设置临时视口失败: {e}")

                    # 本地模式：使用 file:// 协议（支持相对路径资源）
                    temp_html = None
                    try:
                        # 创建临时 HTML 文件
                        with tempfile.NamedTemporaryFile(
                            mode="w", suffix=".html", delete=False, encoding="utf-8"
                        ) as f:
                            temp_html = f.name
                            f.write(html_content)

                        # 使用 file:/// 协议加载（Path.as_uri() 生成正确格式）
                        file_url = Path(temp_html).as_uri()
                        await page.goto(file_url, wait_until=wait_until, timeout=goto_timeout)
                    finally:
                        # 清理临时 HTML 文件
                        if temp_html and os.path.exists(temp_html):
                            try:
                                os.unlink(temp_html)
                            except Exception:
                                pass

                    # 仅在页面实际包含地图区域时等待地图渲染完成标记。
                    # 地震列表等纯卡片模板并不包含地图容器，如果统一等待 .map-ready，
                    # 会在正常场景下产生误导性的“地图超时”诊断日志。
                    has_map_related_nodes = await page.evaluate("""
                        () => {
                            const selectors = [
                                '.map-ready',
                                '#map',
                                '.map-container',
                                '#map-container',
                                '.leaflet-container'
                            ];
                            return selectors.some((selector) => document.querySelector(selector));
                        }
                    """)
                    if has_map_related_nodes:
                        try:
                            await page.wait_for_selector(
                                ".map-ready", state="attached", timeout=30000
                            )
                            logger.debug("[灾害预警] 地图渲染标记已就绪")
                        except Exception:
                            logger.warning(
                                "[灾害预警] 等待 .map-ready 标记超时，地图可能未完全加载"
                            )
                            if request_failures:
                                logger.warning(
                                    f"[灾害预警] 地图等待超时期间捕获到资源失败: {' | '.join(request_failures[-5:])}"
                                )
                            if page_errors:
                                logger.warning(
                                    f"[灾害预警] 地图等待超时期间捕获到脚本异常: {' | '.join(page_errors[-3:])}"
                                )
                            await self._log_page_diagnostics(
                                page, reason="map-ready-timeout"
                            )
                            # 兜底等待，确保至少能看到部分内容
                            await asyncio.sleep(0.2)

                    # 等待卡片元素可见
                    try:
                        await page.wait_for_selector(
                            selector, state="visible", timeout=2000
                        )
                    except Exception:
                        # 兜底：尝试找常见的类名。该分支在部分模板中属于正常兼容路径，不额外输出诊断日志。
                        logger.debug(
                            f"[灾害预警] 选择器 {selector} 未找到，尝试备用选择器"
                        )
                        selector = ".quake-card"
                        await page.wait_for_selector(
                            selector, state="visible", timeout=1000
                        )

                    # 定位卡片元素
                    card = page.locator(selector)

                    # 截图：只截取元素，背景透明
                    await card.screenshot(path=output_path, omit_background=True)

                    elapsed = time.time() - start_time

                    if os.path.exists(output_path):
                        if request_failures:
                            logger.warning(
                                f"[灾害预警] 卡片渲染虽成功，但捕获到资源请求失败: {' | '.join(request_failures[-5:])}"
                            )
                        if page_errors:
                            logger.warning(
                                f"[灾害预警] 卡片渲染虽成功，但捕获到页面脚本异常: {' | '.join(page_errors[-3:])}"
                            )
                        logger.info(f"[灾害预警] 卡片渲染成功，耗时 {elapsed:.3f}秒")
                        return output_path
                    else:
                        logger.warning("[灾害预警] 截图未生成文件")
                        await self._log_page_diagnostics(
                            page, reason="screenshot-missing"
                        )
                        return None
                except Exception:
                    page_broken = True
                    raise
                finally:
                    # 恢复原始视口尺寸，避免影响池中其他页面使用
                    if original_viewport and page:
                        try:
                            await page.set_viewport_size(original_viewport)
                        except Exception:
                            pass
                    # 页面损坏时不放回池（避免下次取出坏页面）
                    if page:
                        if page_broken:
                            try:
                                await page.close()
                            except Exception:
                                pass
                        else:
                            await self._page_pool.put(page)
            finally:
                # 释放信号量
                if acquired_semaphore:
                    self._semaphore.release()

        except Exception as e:
            logger.error(f"[灾害预警] 卡片渲染失败: {e}")
            # 内层 except 已标记 page_broken=True，finally 已关闭坏页面
            # 这里只需补回一个页面到池（坏页面被丢弃了）
            if page_broken:
                async with self._page_creation_lock:
                    try:
                        if not self._browser or self._closed:
                            return None
                        if self._page_pool.qsize() < self.pool_size:
                            try:
                                new_page = await self._browser.new_page(
                                    viewport={"width": 800, "height": 800},
                                    device_scale_factor=2,
                                )
                                await self._page_pool.put(new_page)
                                logger.debug("[灾害预警] 已重新创建页面")
                            except Exception as browser_err:
                                logger.warning(f"[灾害预警] 浏览器可能已死，尝试重启: {browser_err}")
                                await self._restart_browser()
                    except Exception as recover_err:
                        logger.error(f"[灾害预警] 页面恢复失败: {recover_err}")
            return None

    async def _render_card_via_http(
        self, html_content: str, output_path: str, selector: str
    ) -> str | None:
        """使用 browserless HTTP API 渲染卡片"""
        start_time = time.time()

        # 构建请求 URL
        api_url = self._server_url
        if not api_url.endswith("/"):
            api_url += "/"
        api_url += "screenshot"

        try:
            # 构建请求体 - 使用 browserless screenshot API
            payload = {
                "html": html_content,
                "options": {
                    "type": "png",
                    "omitBackground": True,
                    "fullPage": False,
                },
                "gotoOptions": {
                    "waitUntil": "networkidle2",  # 等待网络几乎空闲（允许2个连接）
                    "timeout": 60000,
                },
                "viewport": {
                    "width": 800,
                    "height": 800,
                    "deviceScaleFactor": 2,
                },
                "waitForTimeout": 3000,  # 额外等待 3 秒，确保地图瓦片加载
            }

            # 如果指定了选择器，使用元素截图
            if selector and selector != ".card":
                payload["selector"] = selector
                # 使用 waitForSelector 确保元素可见
                payload["waitForSelector"] = {
                    "selector": selector,
                    "visible": True,
                    "timeout": 10000,
                }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    api_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=90),  # 增加到 90 秒
                ) as response:
                    if response.status == 200:
                        # 保存截图
                        image_data = await response.read()
                        with open(output_path, "wb") as f:
                            f.write(image_data)

                        elapsed = time.time() - start_time
                        logger.info(
                            f"[灾害预警] 卡片渲染成功（HTTP API），耗时 {elapsed:.3f}秒"
                        )
                        return output_path
                    else:
                        error_text = await response.text()
                        logger.error(
                            f"[灾害预警] browserless API 返回错误: {response.status} - {error_text}"
                        )
                        return None

        except asyncio.TimeoutError:
            logger.error("[灾害预警] browserless API 请求超时")
            return None
        except Exception as e:
            logger.error(f"[灾害预警] browserless API 请求失败: {e}")
            if self._telemetry and self._telemetry.enabled:
                await self._telemetry.track_error(
                    e, module="core.browser_manager._render_card_via_http"
                )
            return None

    async def close(self):
        """关闭浏览器管理器"""
        if self._closed:
            logger.debug("[灾害预警] 浏览器已关闭，跳过")
            return

        logger.info("[灾害预警] 正在关闭浏览器...")
        self._closed = True

        await self._cleanup()

        logger.info("[灾害预警] 浏览器已关闭")

    async def _cleanup(self):
        """清理资源，确保前一步失败也不影响后续步骤继续执行。"""
        cleanup_errors = []

        # 步骤 1: 关闭页面池中的所有页面
        try:
            while not self._page_pool.empty():
                try:
                    page = self._page_pool.get_nowait()
                    await page.close()
                except Exception as e:
                    cleanup_errors.append(f"关闭页面失败: {e}")
                    logger.debug(f"[灾害预警] 关闭页面失败: {e}")
        except Exception as e:
            cleanup_errors.append(f"清理页面池失败: {e}")
            logger.warning(f"[灾害预警] 清理页面池时发生异常: {e}")

        # 步骤 2: 关闭浏览器
        try:
            if self._browser:
                await self._browser.close()
                self._browser = None
        except Exception as e:
            cleanup_errors.append(f"关闭浏览器失败: {e}")
            logger.warning(f"[灾害预警] 关闭浏览器失败: {e}")
            # 即使关闭失败,也强制置空引用,防止后续误用
            self._browser = None

        # 步骤 3: 停止 Playwright
        try:
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
        except Exception as e:
            cleanup_errors.append(f"停止 Playwright 失败: {e}")
            logger.warning(f"[灾害预警] 停止 Playwright 失败: {e}")
            # 即使停止失败,也强制置空引用
            self._playwright = None

        # 标记为未初始化
        self._initialized = False

        # 如果有清理错误,记录汇总日志
        if cleanup_errors:
            logger.warning(
                f"[灾害预警] 资源清理过程中遇到 {len(cleanup_errors)} 个错误"
            )

    async def _restart_browser(self):
        """重启浏览器（浏览器进程死亡时调用）。"""
        logger.warning("[灾害预警] 正在重启浏览器...")
        await self._cleanup()
        try:
            await self.initialize()
            logger.info("[灾害预警] 浏览器重启成功")
        except Exception as e:
            logger.error(f"[灾害预警] 浏览器重启失败: {e}")

    def __del__(self):
        """析构函数 - 确保资源释放"""
        if self._browser or self._playwright:
            logger.warning(
                "[灾害预警] 检测到未正常关闭的浏览器资源，这可能导致进程泄漏"
            )
