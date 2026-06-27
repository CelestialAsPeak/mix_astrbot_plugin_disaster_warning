"""
message/push.py — 消息推送服务。

包含推流编排、推送执行、会话发送。
"""

from __future__ import annotations

import base64
import os
from typing import Any, Callable

try:
    from astrbot.api import logger
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Image, Plain
except ImportError:
    import logging as logger

    # 模拟 MessageChain 和 Plain/Image 用于独立测试
    class Plain:
        def __init__(self, text): self.text = text

    class Image:
        @staticmethod
        def fromBase64(data): return type('Img', (), {'base64': data})()

    class MessageChain:
        def __init__(self, chain): self.chain = chain if isinstance(chain, list) else [chain]

try:
    from ..domain.models import EventEnvelope, EewEvent, EarthquakeReport
    from ..message.presenters import present
    from ..message.big_earthquake_alert import BigEarthquakeAlertService
except ImportError:
    from domain.models import EventEnvelope, EewEvent, EarthquakeReport
    from message.presenters import present
    from message.big_earthquake_alert import BigEarthquakeAlertService


class SessionSender:
    """会话发送器 — 最底层的消息下发。"""

    def __init__(self, context):
        self.context = context

    async def send(self, session_id: str, message: str | list | MessageChain) -> bool:
        """发送消息到指定会话。"""
        try:
            await self.context.send_message(session_id, message)
            return True
        except Exception as e:
            logger.error(f"[Push] 发送到 {session_id} 失败: {e}")
            return False


class PushExecutionService:
    """推送执行服务。"""

    def __init__(self, config: dict, sender: SessionSender, map_builder=None, snet_renderer=None, gq_card_builder=None, intensity_img_renderer=None):
        self.config = config
        self.sender = sender
        self.map_builder = map_builder
        self.snet_renderer = snet_renderer
        self.gq_card_builder = gq_card_builder
        self.intensity_img_renderer = intensity_img_renderer
        # JMA EEW 556 警报横幅图
        self._jma_eew_banner = os.path.join(
            os.path.dirname(__file__), "..", "resources", "images", "jma_eew_banner.jpg"
        )
        # NHK 图缓存: {source_id|event_id → [b64, b64] | None} 避免重复爆破
        self._nhk_cache: dict[str, list[str] | None] = {}
        self._nhk_cache_max = 128
        # 大地震大喇叭提醒
        self.big_alert_service = BigEarthquakeAlertService(config)

    async def _render_event_map(self, envelope: EventEnvelope) -> list[str] | None:
        """渲染震中地图（缩略图 + 细节图），返回 base64 列表。"""
        if not self.map_builder:
            return None

        event = envelope.event
        include_map = self.config.get("message_format", {}).get("include_map", True)
        if not include_map:
            return None
        if not isinstance(event, (EewEvent, EarthquakeReport)):
            return None

        lat, lon = event.latitude, event.longitude
        if lat is None or lon is None:
            return None
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            return None

        b64_list = []

        try:
            # 缩略图（zoom 4）
            thumb_cfg = dict(self.config.get("message_format", {}))
            thumb_cfg["map_zoom_level"] = 4
            thumb_path = await self.map_builder.render_map_image(lat, lon, thumb_cfg)
            if thumb_path and os.path.exists(thumb_path):
                with open(thumb_path, "rb") as f:
                    b64_list.append(base64.b64encode(f.read()).decode())
                try:
                    os.unlink(thumb_path)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[Push] 缩略图渲染失败: {e}")

        try:
            # 细节图（zoom 6）
            detail_cfg = dict(self.config.get("message_format", {}))
            detail_cfg["map_zoom_level"] = 8
            detail_path = await self.map_builder.render_map_image(lat, lon, detail_cfg)
            if detail_path and os.path.exists(detail_path):
                with open(detail_path, "rb") as f:
                    b64_list.append(base64.b64encode(f.read()).decode())
                try:
                    os.unlink(detail_path)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[Push] 细节图渲染失败: {e}")

        return b64_list if b64_list else None

    async def _render_snet_map(self, envelope: EventEnvelope) -> list[str] | None:
        """渲染 S-Net 测站分布图，返回 base64 列表。"""
        if not self.snet_renderer:
            return None
        stations = None
        ts_str = ""
        if envelope.metadata:
            stations = envelope.metadata.get("stations")
        if not stations and isinstance(envelope.event, EarthquakeReport):
            raw = envelope.event.raw if isinstance(envelope.event.raw, dict) else {}
            stations = raw.get("stations")
            ts_str = str(raw.get("timestamp", ""))
        if not stations:
            return None
        try:
            from datetime import datetime, timezone
            ts_str = ts_str or datetime.now(timezone.utc).strftime("%Y%m%d%H%M00")
            import tempfile, os, time as _time
            img_path = os.path.join(tempfile.gettempdir(), f"snet_{ts_str}_{int(_time.time())}.png")
            out = await self.snet_renderer.render(stations, img_path, ts_str)
            if out and os.path.exists(out):
                with open(out, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                try:
                    os.unlink(out)
                except Exception:
                    pass
                return [b64]
        except Exception as e:
            logger.error(f"[Push] S-Net 测站图渲染失败: {e}")
        return None

    async def _fetch_nhk_report_images(self, envelope: EventEnvelope) -> list[str] | None:
        """下载 JMA NHK 地震报告双图（概况图 + 区域图）。

        URL 模式:
          概况图: JS00cwA0{发震_yyMMddHHmmss}_{图片_YYYYmmddHHMMss}.jpg
          区域图: JS00clA0{发震_yyMMddHHmmss}_{图片_YYYYmmddHHMMss}.jpg

        策略:
          - issue.time 确定图片时间戳 ✅ 精确匹配
          - earthquake.time 确定事件分钟（缺秒）
          - 每秒 5 个 HEAD 扫秒数，15s 超时回退
        """
        import asyncio
        from ..utils.time import parse_ts

        event = envelope.event
        if not isinstance(event, EarthquakeReport):
            return None
        # 只处理 JMA P2P 报告源
        if event.source_id not in ("jma_p2p_info", "jma_p2p_info_http"):
            return None

        raw = event.raw if isinstance(event.raw, dict) else {}
        if not raw:
            return None

        # ── 缓存命中检查 ──
        cache_key = f"{event.source_id}|{event.event_id}"
        cached = self._nhk_cache.get(cache_key)
        if cached is not None:
            if cached:
                logger.info(f"[NHK] 缓存命中: {cache_key} ({len(cached)} 图)")
                return list(cached)
            logger.debug(f"[NHK] 缓存命中（无图）: {cache_key}")
            return None

        # ScalePrompt（震度速報）也试 NHK 图（NHK 可能有），失败不回退 PetalMap
        # （无经纬度，_render_event_map 自身会跳过）
        issue = raw.get("issue", {}) or {}

        # ── 提取 earthquake.time（事件基准分钟）──
        eq_raw = raw.get("earthquake", {}) or {}
        eq_time_str = eq_raw.get("time") if isinstance(eq_raw, dict) else None
        if not eq_time_str:
            return None
        dt_eq = parse_ts(eq_time_str)
        if dt_eq is None:
            return None
        base_ymdhms = dt_eq.strftime("%y%m%d%H%M")  # YYMMDDHHmm（不含秒）

        # ── 提取 issue.time（图片时间戳）──
        iss_time_str = issue.get("time") if isinstance(issue, dict) else None
        if not iss_time_str:
            return None
        dt_iss = parse_ts(iss_time_str)
        if dt_iss is None:
            return None
        img_ts = dt_iss.strftime("%Y%m%d%H%M%S")

        # ── 秒数地毯式扫描（5 HEAD/sec, 15s 超时）──
        # 同时试 issue.time / +1s / +2s（偏移因事件/阶段而异）
        import datetime as _dt_mod
        ts_list = [img_ts]
        for off in (1, 2):
            dt_off = dt_iss + _dt_mod.timedelta(seconds=off)
            ts_list.append(dt_off.strftime("%Y%m%d%H%M%S"))
        logger.info(f"[NHK] 开始爆破 — 基准={base_ymdhms}XX 时间戳列表={[t[-6:] for t in ts_list]}")
        found = await self._scan_nhk_sec(base_ymdhms, ts_list)
        if found is None:
            logger.warning(f"[NHK] 爆破失败 — 全部 404，回退 PetalMap")
            self._nhk_cache[cache_key] = None
            return None

        found_sec, found_ts = found
        logger.info(f"[NHK] 爆破成功！时间戳: {base_ymdhms}{found_sec:02d}_{found_ts}")

        # ── 双图下载 ──
        event_time = f"{base_ymdhms}{found_sec:02d}"
        base_url = "https://news.web.nhk/sokuho/jishin/data/"
        urls = [
            f"{base_url}JS00cwA0{event_time}_{found_ts}.jpg",
            f"{base_url}JS00clA0{event_time}_{found_ts}.jpg",
        ]

        import aiohttp
        b64_list = []
        async with aiohttp.ClientSession() as session:
            for i, url in enumerate(urls, 1):
                label = "概况图" if i == 1 else "区域图"
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            b64_list.append(base64.b64encode(data).decode())
                            logger.info(f"[NHK] {label}下载成功 ({len(data)} bytes)")
                except Exception as e:
                    logger.warning(f"[NHK] {label}下载失败: {e}")

        if len(b64_list) == 2:
            # 写入缓存，控制大小
            self._nhk_cache[cache_key] = list(b64_list)
            if len(self._nhk_cache) > self._nhk_cache_max:
                stale = next(iter(self._nhk_cache))
                del self._nhk_cache[stale]
            return b64_list
        # 双图不全仍缓存为 None 避免重试
        if b64_list:
            self._nhk_cache[cache_key] = None
        logger.warning(f"[NHK] 双图仅拿到 {len(b64_list)}/2，丢弃")
        return None

    async def _scan_nhk_sec(self, base_ymdhms: str, img_ts_list: list, timeout: int = 15) -> int | None:
        """HEAD 地毯扫秒数 (5/sec, 最多 15s)。支持多图片时间戳交替试探。"""
        import aiohttp, asyncio, time as _time

        BATCH = 5
        start = _time.time()
        deadline = start + timeout
        total_tried = 0
        # 确保至少有 1 个时间戳
        if not img_ts_list:
            img_ts_list = [""]
        n_ts = len(img_ts_list)

        async def _head(session, sec, ts_idx, batch_idx):
            ts = img_ts_list[ts_idx % n_ts]
            url = (f"https://news.web.nhk/sokuho/jishin/data/"
                   f"JS00cwA0{base_ymdhms}{sec:02d}_{ts}.jpg")
            try:
                async with session.head(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    ok = resp.status == 200
                    return (sec, ts, ok, batch_idx)
            except Exception:
                return (sec, ts, False, batch_idx)

        async with aiohttp.ClientSession() as session:
            batch_idx = 0
            # 主循环：全部 60 秒都用第 1 个时间戳
            ts_primary = img_ts_list[0]
            for batch_start in range(0, 60, BATCH):
                batch_idx += 1
                now = _time.time()
                if now >= deadline:
                    tried = min(total_tried + BATCH, 60)
                    logger.warning(f"[NHK] ⏰ 超时 ({timeout}s) — 已试 {tried}/60")
                    return None

                batch = list(range(batch_start, min(batch_start + BATCH, 60)))
                sec_str = ",".join(f"{s:02d}" for s in batch)
                total_tried += len(batch)
                logger.info(f"[NHK] 正在爆破NHK图片爬取({total_tried}/60) 批次#{batch_idx}@{ts_primary[-6:]}: {sec_str}")

                tasks = [_head(session, s, 0, batch_idx) for s in batch]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for r in results:
                    if isinstance(r, tuple) and len(r) >= 3 and r[2]:
                        elapsed = _time.time() - start
                        logger.info(f"[NHK] ✅ 爆破成功！事件秒={r[0]:02d} 时间戳={r[1]} (耗时{elapsed:.1f}s)")
                        return (r[0], r[1])

                elapsed = _time.time() - now
                if elapsed < 1.0:
                    await asyncio.sleep(1.0 - elapsed)

            # 第 1 个时间戳未命中，切到第 2+ 个时间戳重试
            for ts_idx in range(1, n_ts):
                ts = img_ts_list[ts_idx]
                logger.info(f"[NHK] 切到时间戳 {ts[-6:]} 重扫...")
                for batch_start in range(0, 60, BATCH):
                    batch_idx += 1
                    now = _time.time()
                    if now >= deadline:
                        logger.warning(f"[NHK] ⏰ 超时 ({timeout}s) — ts#{ts_idx} 未完成")
                        return None

                    batch = list(range(batch_start, min(batch_start + BATCH, 60)))
                    total_tried += len(batch)
                    logger.info(f"[NHK] 正在爆破NHK图片爬取(ts#{ts_idx}@{ts[-6:]}) 批次#{batch_idx}: "
                                f"{','.join(f'{s:02d}' for s in batch)}")

                    tasks = [_head(session, s, ts_idx, batch_idx) for s in batch]
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    for r in results:
                        if isinstance(r, tuple) and len(r) >= 3 and r[2]:
                            elapsed = _time.time() - start
                            logger.info(f"[NHK] ✅ 爆破成功！事件秒={r[0]:02d} 时间戳={r[1]} (耗时{elapsed:.1f}s)")
                            return (r[0], r[1])

                    elapsed = _time.time() - now
                    if elapsed < 1.0:
                        await asyncio.sleep(1.0 - elapsed)

        return None

    async def _render_gq_card(self, envelope: EventEnvelope) -> str | None:
        """渲染 GlobalQuake 专属卡片，返回 base64。"""
        if not self.gq_card_builder:
            return None
        try:
            msg_fmt = self.config.get("message_format", {})
            template_name = msg_fmt.get("global_quake_template", "DarkNight")
            return await self.gq_card_builder.build(
                envelope,
                template_name=template_name,
                map_source=msg_fmt.get("map_source", "PetalMap矢量图亮"),
                zoom_level=msg_fmt.get("gq_map_zoom_level", msg_fmt.get("map_zoom_level", 5)),
            )
        except Exception as e:
            logger.error(f"[Push] GQ 卡片渲染失败: {e}")
        return None

    async def execute_push(
        self,
        envelope: EventEnvelope,
        target_sessions: list[str] | None = None,
        session_config_getter: Callable | None = None,
        **kwargs: Any,
    ) -> bool:
        """执行推送。"""
        try:
            # 融合模式传入了额外数据，暂存到 envelope metadata 供 presenter 使用
            merge_data = kwargs.get("merge_data")
            if merge_data is not None:
                envelope = envelope.__class__(
                    identity=envelope.identity,
                    event=envelope.event,
                    received_at=envelope.received_at,
                    payload=envelope.payload,
                    metadata={**envelope.metadata, "_merge_data": merge_data},
                )
            text = present(envelope)
            if not text:
                return False

            sessions = target_sessions or self.config.get("target_sessions", [])
            if not sessions:
                return False

            # 构建消息链（文本 + 地图图片）
            chain_components = [Plain(text)]

            # ── 图片选择 ──
            is_eew = isinstance(envelope.event, EewEvent)
            is_snet = envelope.source_id in ("snet_http", "snet") and isinstance(envelope.event, EarthquakeReport)
            is_gq = envelope.source_id == "global_quake" and is_eew

            # 本地辅助：将图片路径附加到消息链
            def _img(path: str | None):
                if path and os.path.exists(path):
                    with open(path, "rb") as f:
                        chain_components.append(Image.fromBase64(base64.b64encode(f.read()).decode()))

            try:
                if is_gq and self.gq_card_builder:
                    # GlobalQuake 专属卡（含震中+震度烈度）
                    if self.intensity_img_renderer:
                        ev = envelope.event
                        if ev.magnitude is not None:
                            for p in self.intensity_img_renderer.render_both(ev.magnitude, ev.depth):
                                _img(p)
                    gq_b64 = await self._render_gq_card(envelope)
                    if gq_b64:
                        chain_components.append(Image.fromBase64(gq_b64))

                elif is_snet:
                    # S-Net 测站分布图
                    snet_b64 = await self._render_snet_map(envelope)
                    if snet_b64:
                        for b64 in snet_b64:
                            chain_components.append(Image.fromBase64(b64))

                elif is_eew and self.intensity_img_renderer:
                    # EEW → 震度+烈度图（本地缓存快，不用 Playwright 方位图）
                    ev = envelope.event
                    if ev.magnitude is not None:
                        # JMA 源：震度用 JMA 提供的 max_intensity，烈度继续 CSIS 估算
                        if ev.source_id.startswith("jma_") and getattr(ev, "max_intensity", None):
                            s = self.intensity_img_renderer.render_shindo_actual(
                                ev.max_intensity, "最大震度")
                            if s:
                                _img(s)
                            i = self.intensity_img_renderer.render_intensity(
                                ev.magnitude, ev.depth)
                            if i:
                                _img(i)
                        else:
                            for p in self.intensity_img_renderer.render_both(
                                    ev.magnitude, ev.depth):
                                _img(p)
                    # JMA 556 警报横幅图
                    if envelope.source_id in ("jma_p2p", "jma_p2p_http"):
                        _img(self._jma_eew_banner)

                elif isinstance(envelope.event, EarthquakeReport) and self.intensity_img_renderer:
                    ev = envelope.event
                    # ── JMA / CWA 报告源：用实际震度渲染图片（不用 CSIS 估算）──
                    if ev.source_id in ("jma_p2p_info", "jma_p2p_info_http"):
                        # 取实际最大震度：优先 mmi，其次 intensity_points
                        actual_shindo = None
                        if ev.mmi is not None:
                            actual_shindo = ev.mmi
                        elif ev.intensity_points:
                            max_s = max(
                                (p.get("scale") for p in ev.intensity_points
                                 if isinstance(p, dict) and p.get("scale") is not None),
                                default=None,
                            )
                            if max_s is not None:
                                actual_shindo = max_s
                        if actual_shindo is not None:
                            from ..message.presenters import _shindo_label_str
                            _img(self.intensity_img_renderer.render_shindo_actual(
                                _shindo_label_str(actual_shindo), "最大震度"))
                        else:
                            _img(self.intensity_img_renderer.render_shindo_actual("不明", "最大震度"))
                        nhk_b64 = await self._fetch_nhk_report_images(envelope)
                        if nhk_b64:
                            for b64 in nhk_b64:
                                chain_components.append(Image.fromBase64(b64))
                        else:
                            # NHK 失败，回退 PetalMap 双图
                            map_b64_list = await self._render_event_map(envelope)
                            if map_b64_list:
                                for b64 in map_b64_list:
                                    chain_components.append(Image.fromBase64(b64))
                    elif ev.source_id == "cwa_report_fanstudio":
                        # CWA 报告：从 raw 取 maxIntensity（如 "3級"）
                        raw_cwa = ev.raw if isinstance(ev.raw, dict) else {}
                        cwa_intensity = str(raw_cwa.get("maxIntensity", "") or "")
                        if cwa_intensity:
                            _img(self.intensity_img_renderer.render_shindo_actual(
                                cwa_intensity, "最大震度"))
                        else:
                            _img(self.intensity_img_renderer.render_shindo_actual("不明", "最大震度"))
                        map_b64_list = await self._render_event_map(envelope)
                        if map_b64_list:
                            for b64 in map_b64_list:
                                chain_components.append(Image.fromBase64(b64))
                    else:
                        # 非 JMA 源：震度+烈度图 + 方位图（原逻辑）
                        if ev.magnitude is not None:
                            for p in self.intensity_img_renderer.render_both(ev.magnitude, ev.depth):
                                _img(p)
                        map_b64_list = await self._render_event_map(envelope)
                        if map_b64_list:
                            for b64 in map_b64_list:
                                chain_components.append(Image.fromBase64(b64))

                else:
                    # 其他 → 震中方位图
                    map_b64_list = await self._render_event_map(envelope)
                    if map_b64_list:
                        for b64 in map_b64_list:
                            chain_components.append(Image.fromBase64(b64))
            except Exception as exc_img:
                logger.warning(f"[Push] 图片附加异常（不影响文字推送）: {exc_img}")

            # GlobalQuake 卡片（也可以由上游预渲染后塞入 metadata）
            gq_img = envelope.metadata.get("_gq_card") if envelope.metadata else None
            if gq_img and not is_gq:
                chain_components.append(Image.fromBase64(gq_img))
            # 台风路径图（由 _typhoon_push_adapter 预渲染后塞入 metadata）
            typhoon_img = envelope.metadata.get("_typhoon_image") if envelope.metadata else None
            if typhoon_img:
                chain_components.append(Image.fromBase64(typhoon_img))

            message = MessageChain(chain_components)

            success = False
            for session_id in sessions:
                result = await self.sender.send(session_id, message)
                if result:
                    success = True

            # ── 大地震大喇叭提醒（仅在推送成功后触发） ──
            if success and self.big_alert_service.should_alert(envelope):
                alert_text = self.big_alert_service.get_alert_message()
                alert_count = self.big_alert_service.get_alert_count()
                logger.info(f"[Push] 🚨 大地震大喇叭提醒 {alert_count} 次，目标 {len(sessions)} 会话")
                for i in range(alert_count):
                    for session_id in sessions:
                        try:
                            await self.sender.send(session_id, alert_text)
                        except Exception as e:
                            logger.error(f"[Push] 大喇叭第 {i+1} 次发送失败: {e}")

            return success

        except Exception as e:
            logger.error(f"[Push] 执行异常: {e}")
            return False


class PushOrchestrator:
    """推送编排器 — 决定推送路径（普通/融合）。"""

    def __init__(self, config: dict, execute_push: Callable, sender: SessionSender | None = None):
        self.config = config
        self._execute_push = execute_push
        self._sender = sender

    async def push_event(
        self,
        envelope: EventEnvelope,
        target_sessions: list[str] | None = None,
        session_config_getter: Callable | None = None,
        **kwargs: Any,
    ) -> bool:
        """编排推送（支持融合场景的 merge_data 参数）。"""
        return await self._execute_push(
            envelope,
            target_sessions=target_sessions,
            session_config_getter=session_config_getter,
            **kwargs,
        )

    async def send_to_admin(self, text: str) -> bool:
        """发送管理通知文本（供 SystemNotificationService 调用）。"""
        if not self._sender:
            return False
        sessions = self.config.get("target_sessions", [])
        if not sessions:
            return False
        success = False
        for session_id in sessions:
            if await self._sender.send(session_id, text):
                success = True
        return success
