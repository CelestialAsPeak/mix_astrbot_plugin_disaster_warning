"""
plugin.py — Mix灾害预警插件 AstrBot 入口。

命令全部基于旧 events.db 数据库查询实现。
当旧版插件运行时，新版可直接读取其数据。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter, MessageChain
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star

try:
    # 优先相对导入（确保加载本插件的模块，而非同名旧插件）
    from .config.loader import load_sources
    from .config.accessor import ConfigAccessor
    from .config.validator import ConfigValidator
    from .parser.registry import ParserRegistry
    from .pipeline.pipeline import EventPipeline
    from .pipeline.fusion import FusionOrchestrator
    from .broker.signal_bus import SignalBus
    from .broker.router import MessageRouter
    from .broker.websocket import WebSocketManager
    from .broker.http_poller import HttpPollManager
    from .message.push import SessionSender, PushExecutionService, PushOrchestrator
    from .domain.models import EewEvent
    from .message.presenters import present, present_eew, present_earthquake_report, WEATHER_TYPE_MAP, LEVEL_COLORS
    from .message.browser import BrowserManager
    from .message.render.typhoon_map_renderer import TyphoonMapRenderer
    from .message.render.snet_map_renderer import SnetMapRenderer
    from .message.notification import SystemNotificationService, NotificationCenter
    from .storage.database import DatabaseManager
    from .storage.stats import StatisticsManager
    from .storage.session import SessionConfigManager
    from .utils.version import get_plugin_version
    from .core.message.builders.map_attachment_builder import MapAttachmentBuilder
    from .services.typhoon_manager import TyphoonManager
except ImportError:
    from config.loader import load_sources
    from config.accessor import ConfigAccessor
    from config.validator import ConfigValidator
    from parser.registry import ParserRegistry
    from pipeline.pipeline import EventPipeline
    from pipeline.fusion import FusionOrchestrator
    from broker.signal_bus import SignalBus
    from broker.router import MessageRouter
    from broker.websocket import WebSocketManager
    from broker.http_poller import HttpPollManager
    from message.push import SessionSender, PushExecutionService, PushOrchestrator
    from domain.models import EewEvent
    from message.presenters import present, present_eew, present_earthquake_report, WEATHER_TYPE_MAP, LEVEL_COLORS
    from message.browser import BrowserManager
    from message.render.typhoon_map_renderer import TyphoonMapRenderer
    from message.render.snet_map_renderer import SnetMapRenderer
    from message.notification import SystemNotificationService, NotificationCenter
    from storage.database import DatabaseManager
    from storage.stats import StatisticsManager
    from storage.session import SessionConfigManager
    from utils.version import get_plugin_version
    from core.message.builders.map_attachment_builder import MapAttachmentBuilder
    from services.typhoon_manager import TyphoonManager

# 触发解析器注册（显式 import，AstrBot 最可靠）
from .parser.fan_studio import cea, cenc, cwa, jma, global_sources, provincial, generic_eew, tsunami, weather
from .parser.wolfx import eew as wolfx_eew, province as wolfx_province, report as wolfx_report
from .parser.p2p import eew as p2p_eew, report as p2p_report, tsunami as p2p_tsunami
from .parser import global_quake as gq_parser, snet as snet_parser
from .parser.http_poll import parsers as http_poll_parsers
from .parser.typhoon import cma as typhoon_cma, jma as typhoon_jma


# ── 格式工具（对齐旧版 earthquake_presenter） ──

_SEPARATOR = "=" * 19
_FIELD_WIDTH = 6  # 字段名对齐宽度（全角字符数）

def _field(label: str, value: object) -> str:
    """字段名 | 值（全角空格补齐）。"""
    w = sum(2 if ord(c) > 127 else 1 for c in label)
    pad = _FIELD_WIDTH * 2 - w
    if pad < 0:
        pad = 0
    return f"{label}{'　' * (pad // 2)}{' ' * (pad % 2)}| {value}"

def _make_title(text: str) -> str:
    return f"[{text}]"

def _fmt_time(ts: str | None) -> str:
    if not ts:
        return ""
    return ts.replace("T", " ")[:19]

def _fmt_time_short(ts: str | None) -> str:
    if not ts:
        return ""
    s = ts.replace("T", " ")[:16]
    return s[5:] if len(s) > 10 else s


# ── 源 → 显示名映射 ──

_SOURCE_DISPLAY: dict[str, str] = {
    "cea_fanstudio": "中国地震预警网", "cea_pr_fanstudio": "中国地震预警网(省)",
    "cenc_fanstudio": "中国地震台网", "cenc_wolfx": "CENC(Wolfx)",
    "cwa_fanstudio": "台湾气象署", "cwa_wolfx": "CWA(Wolfx)",
    "jma_fanstudio": "日本气象厅", "jma_wolfx": "JMA(Wolfx)", "jma_wolfx_info": "JMA情报(Wolfx)",
    "jma_p2p": "JMA(P2P)",
    "usgs_fanstudio": "USGS", "emsc_fanstudio": "EMSC",
    "hko_fanstudio": "HKO", "gfz_fanstudio": "GFZ",
    "usp_fanstudio": "USP", "bcsf_fanstudio": "BCSF",
    "fssn_fanstudio": "FSSN", "kma_fanstudio": "KMA",
    "sa_fanstudio": "ShakeAlert", "kma_eew_fanstudio": "KMA EEW",
    "global_quake": "GlobalQuake",
    "funvisis_http": "FUNVISIS", "cenais_http": "CENAIS",
    "csnc_http": "CSNC", "phivolcs_http": "PHIVOLCS", "snet_http": "S-net",
    "tmd_http": "TMD", "geonet_http": "GeoNet",
    "nrcan_http": "NRCan", "usgs_weekly": "USGS周报",
    "snet": "S-net", "icl_http": "ICL",
    "beijing_fanstudio": "北京", "guangxi_fanstudio": "广西",
    "ningxia_fanstudio": "宁夏", "shanxi_fanstudio": "山西",
    "yunnan_fanstudio": "云南",
    "china_tsunami_fanstudio": "海啸", "china_weather_fanstudio": "气象",
    "sc_wolfx_eew": "四川", "fj_wolfx_eew": "福建", "cq_wolfx_eew": "重庆",
}


# ── 帮助文本 ──

_PLUGIN_HELP = """🚨 Mix灾害预警使用说明

📋 管理:
  /灾害预警            帮助
  /灾害预警状态        服务状态
  /灾害预警重连        重连数据源（管理）
  /灾害预警统计        事件统计
  /灾害预警统计清除    清除统计（管理）
  /灾害预警配置 查看   查看配置

📋 查询:
  /地震列表 <源> <数量>  地震列表（默认 cenc 9条）
  /地震预警              EEW 状态
  /气象预警 <省份|全国>   气象预警
  /台风 [名称/编号]      台风路径（全部活跃逐张渲染，指定名称/编号查单个）
  /震央 [日期]            JMA 震央分布图（默认今天，支持 6月14日 / 6月14日-6月20日）

🔍 快捷 /cenc /cea /jma /usgs /cwa /emsc /hko /gfz /bcsf /fssn /kma /sa
      /geonet /nrcan /snet /海啸 /气象 /tg /zl /flb
      /北京 /广西 /宁夏 /山西 /云南

  v{version} | AGPL v3 | 数据源: 已接入46+
"""


# ═══════════════════════════ 主插件 ═══════════════════════════


class MixDisasterWarningPlugin(Star):
    """Mix灾害预警插件。"""

    def __init__(self, context: Context, config: AstrBotConfig = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._config_accessor = ConfigAccessor(dict(self.config))

        # 运行时
        self.pipeline: EventPipeline | None = None
        self.signal_bus = SignalBus()
        self.ws_manager: WebSocketManager | None = None
        self.http_poll_manager: HttpPollManager | None = None
        self.browser_manager: BrowserManager | None = None
        self.database: DatabaseManager | None = None
        self.stats_manager: StatisticsManager | None = None
        self.session_config_manager: SessionConfigManager | None = None
        self.notification_center: NotificationCenter | None = None

        # 地图渲染（在 __init__ 初始化，不依赖 AstrBot 是否调 initialize）
        self._plugin_root = str(Path(__file__).parent)
        self._temp_dir = tempfile.mkdtemp(prefix="mix_map_")
        self.browser_manager = BrowserManager()
        self._map_builder = MapAttachmentBuilder(
            plugin_root=self._plugin_root,
            temp_dir=self._temp_dir,
            browser_manager=self.browser_manager,
            default_config=dict(self.config),
        )
        # 台风路径图渲染器（延迟初始化 — 在 initialize 中创建）
        self._typhoon_renderer: TyphoonMapRenderer | None = None
        # SNET 测站分布图渲染器
        self._snet_renderer: SnetMapRenderer | None = None
        import sys as _sys
        _sys.stderr.write(f"[MIX_DBG] __init__ _map_builder={self._map_builder is not None}\n")
        _sys.stderr.flush()
        logger.info(f"[Mix] 地图渲染器就绪: plugin_root={self._plugin_root}")

        self._service_task: asyncio.Task | None = None
        self._snet_task: asyncio.Task | None = None
        self._typhoon_cma_task: asyncio.Task | None = None
        self._typhoon_jma_task: asyncio.Task | None = None
        self._typhoon_cleanup_task: asyncio.Task | None = None
        self._typhoon_manager: TyphoonManager | None = None
        self._start_time: float = 0.0
        self._setup_done = False

    async def initialize(self):
        try:
            logger.info("[Mix] 初始化...")
            if not self.config.get("enabled", True):
                logger.info("[Mix] 已禁用")
                return

            sources = load_sources()
            self._sources = sources  # 供 _match_fan_by_signature 查询 payload_signatures
            validated = ConfigValidator.validate(dict(self.config))
            self.config.update(validated)

            self.database = DatabaseManager(self._get_storage_path() / "events.db")
            await self.database.initialize()

            self.stats_manager = StatisticsManager(dict(self.config))
            self.session_config_manager = SessionConfigManager(dict(self.config))
            self.notification_center = NotificationCenter()

            await self.browser_manager.initialize()

            # 台风路径图渲染器
            self._typhoon_renderer = TyphoonMapRenderer(self.browser_manager, self._plugin_root)
            logger.info("[Mix] 台风路径图渲染器就绪")
            self._snet_renderer = SnetMapRenderer(self.browser_manager, self._plugin_root)
            logger.info("[Mix] SNET 测站图渲染器就绪")

            # 台风管理器
            self._typhoon_manager = TyphoonManager(
                db=self.database,
                push_callback=self._typhoon_push_adapter,
            )
            logger.info("[Mix] 台风管理器就绪")
            # 立即创建台风轮询任务（不依赖 _run_service）
            self._typhoon_cma_task = asyncio.create_task(self._run_typhoon_poll("cma", 300))
            self._typhoon_jma_task = asyncio.create_task(self._run_typhoon_poll("jma", 600))
            self._typhoon_cleanup_task = asyncio.create_task(self._run_typhoon_cleanup())

            session_sender = SessionSender(self.context)
            push_svc = PushExecutionService(
                dict(self.config), session_sender, map_builder=self._map_builder,
            )
            self._orchestrator = PushOrchestrator(dict(self.config), push_svc.execute_push, sender=session_sender)

            # 融合编排器（需要在 pipeline 之前创建，因为 pipeline 依赖它）
            self._fusion = FusionOrchestrator(
                dict(self.config),
                push_callback=self._fusion_push_adapter,
            )

            self.pipeline = EventPipeline(
                config=dict(self.config), push_manager=self._orchestrator,
                stats_manager=self.stats_manager,
                database_manager=self.database,
                fusion=self._fusion,
            )

            router = MessageRouter(pipeline=self.pipeline)
            for sid in sources:
                if not sid.startswith("_"):
                    self.signal_bus.on(sid, router.route)

            # 构建 FAN Studio source_name → source_id 映射
            self._fan_source_map: dict[str, str] = {}
            for sid, entry in sources.items():
                if isinstance(entry, dict) and entry.get("provider_family") == "fan_studio":
                    for name in entry.get("provider_source_names", []):
                        self._fan_source_map[name] = sid
            logger.info(f"[Mix] FAN source map: {len(self._fan_source_map)} 条映射")

            # 构建 Wolfx type → source_id 映射
            self._wolfx_source_map: dict[str, str] = {}
            for sid, entry in sources.items():
                if isinstance(entry, dict) and entry.get("connection_handler") == "wolfx":
                    for t in entry.get("provider_message_types", []):
                        self._wolfx_source_map[t] = sid
                    # 同时用 provider_source_names 兜底
                    for name in entry.get("provider_source_names", []):
                        if name not in self._wolfx_source_map:
                            self._wolfx_source_map[name] = sid
            logger.info(f"[Mix] Wolfx source map: {len(self._wolfx_source_map)} 条映射")

            self.ws_manager = WebSocketManager(dict(self.config))
            self.ws_manager.set_message_handler(
                self._ws_message_handler
            )
            self._setup_ws_connections(sources)

            self.http_poll_manager = HttpPollManager()
            self._setup_http_pollers(sources, router)

            self._start_time = __import__("time").time()
            self._service_task = asyncio.create_task(self._run_service())
            self._setup_done = True
            logger.info("[Mix] 初始化完成")

        except Exception as e:
            logger.error(f"[Mix] 初始化失败: {e}")
            await self.terminate()
            raise

    async def terminate(self):
        logger.info("[Mix] 停止...")
        if self._service_task:
            self._service_task.cancel()
            try:
                await self._service_task
            except asyncio.CancelledError:
                pass
        if self._snet_task:
            self._snet_task.cancel()
            try:
                await self._snet_task
            except asyncio.CancelledError:
                pass
            self._snet_task = None
        # 停止台风任务
        for tname in ("_typhoon_cma_task", "_typhoon_jma_task", "_typhoon_cleanup_task"):
            t = getattr(self, tname, None)
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                setattr(self, tname, None)
        if self._typhoon_manager:
            await self._typhoon_manager.close()
        # 停止融合编排器（终止 background tasks）
        if hasattr(self, '_fusion') and self._fusion:
            self._fusion.stop()
        if self.ws_manager:
            await self.ws_manager.stop_all()
        if self.http_poll_manager:
            await self.http_poll_manager.stop_all()
        if self.browser_manager:
            await self.browser_manager.close()
        if self.database:
            await self.database.close()
        if self._temp_dir and os.path.isdir(self._temp_dir):
            try:
                import shutil
                shutil.rmtree(self._temp_dir, ignore_errors=True)
            except Exception:
                pass
        logger.info("[Mix] 已停止")

    async def _run_service(self):
        """后台服务循环。"""
        try:
            if self.ws_manager:
                await self.ws_manager.start_all()
            if self.http_poll_manager:
                await self.http_poll_manager.start_all()
            # SNET 专用轮询（不同于通用 HttpPoller，需要下载并合并 PNG 瓦片）
            if self._snet_task is None:
                self._snet_task = asyncio.create_task(self._poll_snet())
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass

    async def _run_typhoon_poll(self, source: str, interval: int):
        """台风轮询循环。"""
        first = True
        while True:
            try:
                if not first:
                    await asyncio.sleep(interval)
                first = False
                if source == "cma":
                    await self._typhoon_manager.poll_cma()
                else:
                    await self._typhoon_manager.poll_jma()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[台风] {source} 轮询异常: {e}", exc_info=True)
                await asyncio.sleep(interval)

    async def _run_typhoon_cleanup(self):
        """台风老化清理循环（每小时）。"""
        try:
            while True:
                await asyncio.sleep(3600)
                self._typhoon_manager.cleanup_stale(24)
        except asyncio.CancelledError:
            pass

    async def _fetch_snet_once(self, parse_result: bool = False):
        """立即抓一次 SNET 瓦片数据。

        Args:
            parse_result: True 则解析并返回 (stations, timestamp, triggered_count)，
                          False 则仅推入 pipeline（供后台轮询）。

        Returns:
            parse_result=True 时返回 (stations_list, timestamp_str, triggered_count) 或 None。
            parse_result=False 时返回 timestamp_str 或 None。
        """
        from datetime import datetime, timezone
        try:
            import aiohttp
        except ImportError:
            logger.error("[SNET] aiohttp 未安装")
            return None

        now = datetime.now(timezone.utc)
        # 往前试 3 个分钟（MSIL 瓦片可能延迟）
        for offset_min in range(3):
            try_ts = now.replace(second=0, microsecond=0)
            if offset_min > 0:
                try_ts -= __import__("datetime").timedelta(minutes=offset_min)
            ts = try_ts.strftime("%Y%m%d%H%M00")
            tiles = {}

            try:
                async with aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(ssl=False),
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as sess:
                    for tn, fn in [("y11", "11"), ("y12", "12")]:
                        url = f"https://www.msil.go.jp/data/tiles/smoni/tileimage/{ts}/{ts}/5/28/{fn}.png"
                        try:
                            async with sess.get(url) as r:
                                if r.status == 200:
                                    import base64
                                    tiles[tn] = base64.b64encode(await r.read()).decode()
                        except Exception:
                            pass
            except Exception as e:
                logger.error(f"[SNET] HTTP 异常: {e}")
                continue

            if len(tiles) >= 2:
                break
        else:
            logger.warning("[SNET] 所有时间尝试均无瓦片")
            return None

        min_shindo = 0.5
        try:
            ef = self.config.get("earthquake_filters", {})
            sf = ef.get("snet_filter", {}) if isinstance(ef, dict) else {}
            if isinstance(sf, dict) and sf.get("enabled", True):
                min_shindo = float(sf.get("min_magnitude", 0.5))
        except Exception:
            pass

        raw_dict = {"tiles": tiles, "timestamp": ts, "min_shindo": min_shindo}

        # 推入 pipeline（供自动推送和入库）
        await self.signal_bus.emit("snet", json.dumps(raw_dict))

        if not parse_result:
            return ts

        # 用 SnetParser 解析，返回 stations 数据
        try:
            from PIL import Image
            from .parser.snet import SnetParser, _build_stations, _decode_pixel
            import base64, io

            decoded = {}
            for tn in ("y11", "y12"):
                b64 = tiles.get(tn)
                if not b64:
                    continue
                try:
                    png = base64.b64decode(b64)
                    decoded[tn] = Image.open(io.BytesIO(png)).convert("RGB")
                except Exception:
                    continue

            if decoded:
                all_stations = _build_stations(decoded)
                triggered = [s for s in all_stations if s["shindo"] >= min_shindo]
                return (all_stations, ts, len(triggered))
        except Exception as e:
            logger.error(f"[SNET] 数据解析异常: {e}")

        return None

    async def _poll_snet(self):
        """SNET 后台轮询 — 先立即抓一次，之后每 60 秒轮询。"""
        from datetime import datetime, timezone

        try:
            import aiohttp
        except ImportError:
            logger.error("[SNET] aiohttp 未安装，无法轮询")
            return

        # 启动时立即抓一次
        if not self._in_silence_period():
            await self._fetch_snet_once()

        while True:
            try:
                await asyncio.sleep(60)
                if self._in_silence_period():
                    continue
                await self._fetch_snet_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[SNET] 轮询异常: {e}")

    async def _fusion_push_adapter(self, envelope, merge_data=None):
        """融合服务推送适配器 — 将融合后的数据传递给编排器。"""
        if merge_data is not None:
            # PushExecutionService.execute_push 会在 kwargs 中找到 merge_data
            return await self._orchestrator.push_event(
                envelope, merge_data=merge_data,
            )
        return await self._orchestrator.push_event(envelope)

    async def _typhoon_push_adapter(self, envelope) -> None:
        """台风推送适配器 — 将 TyhoonEvent envelope 送入 pipeline。"""
        if self._in_silence_period():
            return
        if self.pipeline:
            try:
                await self.pipeline.handle(envelope)
            except Exception as e:
                logger.error(f"[台风] pipeline 处理失败: {e}")

    async def _ws_message_handler(self, name: str, raw_data: str | bytes) -> None:
        """WebSocket 消息处理器 — 组级→源级路由。"""
        logger.info(f"[WS] 收到消息: {name} ({len(raw_data) if isinstance(raw_data, (str,bytes)) else type(raw_data).__name__})")

        # 统一转文本尝试 JSON 解析（需要提前解析以检查 initial_all）
        text = raw_data.decode("utf-8", errors="replace") if isinstance(raw_data, bytes) else raw_data
        data_for_check = None
        try:
            data_for_check = json.loads(text)
        except Exception:
            pass

        # initial_all 永远处理（不受静默期限制），直接入库不走推送
        if isinstance(data_for_check, dict) and name == "fan_studio" and data_for_check.get("type") == "initial_all":
            await self._route_fan_studio(data_for_check)
            return

        # 启动静默期：所有 WS 事件不处理（防重连 flood）
        if self._in_silence_period():
            elapsed = __import__("time").time() - self._start_time
            silence = self.config.get("debug_config", {}).get("startup_silence_duration", 0)
            logger.debug(f"[WS] 静默期 ({elapsed:.0f}s < {silence}s)，跳过: {name}")
            return

        # 已知二进制连接（GlobalQuake 发 protobuf），直接传原始数据
        if name == "global_quake" and isinstance(raw_data, bytes):
            await self.signal_bus.emit(name, raw_data)
            return

        # 继续处理 data_for_check（上面已解析）
        if data_for_check is not None:
            data = data_for_check
        else:
            # 非 JSON → 直接按组名发射
            await self.signal_bus.emit(name, raw_data)
            return

        # 非 dict 按组名发射
        if not isinstance(data, dict):
            await self.signal_bus.emit(name, raw_data)
            return

        # 多源一线需要内部路由
        if name == "fan_studio":
            await self._route_fan_studio(data)
        elif name == "wolfx":
            await self._route_wolfx(data)
        else:
            await self.signal_bus.emit(name, data)

    def _match_fan_by_signature(self, payload: dict) -> str | None:
        """按 payload 特征签名匹配 FAN Studio 源。"""
        if not isinstance(payload, dict):
            return None
        pkeys = set(payload.keys())
        for source_name, sid in self._fan_source_map.items():
            entry = self._sources.get(sid) if hasattr(self, '_sources') else None
            if not isinstance(entry, dict):
                continue
            sigs = entry.get("payload_signatures", [])
            for sig in sigs:
                if all(k in pkeys for k in sig):
                    return sid
        return None

    def _in_silence_period(self) -> bool:
        """检查是否处于启动静默期（启动后前 N 秒不推送）。"""
        if not hasattr(self, '_start_time') or self._start_time <= 0:
            return False
        silence = self.config.get("debug_config", {}).get("startup_silence_duration", 0)
        if not isinstance(silence, (int, float)) or silence <= 0:
            return False
        elapsed = __import__("time").time() - self._start_time
        return elapsed < silence

    async def _route_fan_studio(self, data: dict) -> None:
        """FAN Studio JSON 消息内部路由（兼容 initial_all 和 update）。"""
        msg_type = data.get("type", "unknown")
        logger.info(f"[Fan] 路由消息 type={msg_type}, keys={list(data.keys())[:6]}")

        if msg_type == "initial_all":
            # initial_all 是 WebSocket 重连后的全量快照
            # 直接解析入库，不走 pipeline（避免启动时全量推送）
            stored = 0
            for key, sid in self._fan_source_map.items():
                if sid == "china_weather_fanstudio":
                    continue  # 已弃用
                source_data = data.get(key)
                if isinstance(source_data, dict):
                    parser = ParserRegistry.get(sid)
                    if parser:
                        try:
                            result = parser.parse_message(source_data)
                            if result:
                                for env in (result if isinstance(result, list) else [result]):
                                    if self.database:
                                        await self.database.insert_envelope(env)
                                    stored += 1
                        except Exception:
                            pass
            logger.info(f"[Fan] initial_all 入库: {stored} 条")
            return

        # 心跳静默
        if msg_type in ("heartbeat", "ping", "pong"):
            return

        # 启动静默期检查
        if self._in_silence_period():
            elapsed = __import__("time").time() - self._start_time
            silence = self.config.get("debug_config", {}).get("startup_silence_duration", 0)
            logger.debug(f"[Fan] 静默期 ({elapsed:.0f}s < {silence}s)，跳过: type={msg_type}")
            return

        if msg_type == "update":
            # {"type":"update", "source":"cenc", "Data":{...}} 或平铺字段
            source_name = data.get("source", "")
            payload = data.get("Data") or data.get("data") or data
            if not isinstance(payload, dict):
                payload = data

            # 1. 按 source 字段精确匹配
            if source_name:
                sid = self._fan_source_map.get(source_name)
                if sid:
                    if sid == "china_weather_fanstudio":
                        return  # 已弃用
                    await self.signal_bus.emit(sid, payload)
                    return

            # 2. 按 payload 签名兜底（处理同族模糊源）
            sid = self._match_fan_by_signature(payload)
            if sid:
                if sid == "china_weather_fanstudio":
                    return  # 已弃用
                await self.signal_bus.emit(sid, payload)
                return

            logger.info(f"[Fan] update: 未匹配 source={source_name}")

        elif msg_type in ("heartbeat", "ping", "pong"):
            pass  # 心跳静默

        elif msg_type == "query_response":
            pass

        else:
            logger.info(f"[Fan] 未知消息类型: {msg_type}")

    async def _route_wolfx(self, data: dict) -> None:
        """Wolfx 消息内部路由 — 按 type 字段匹配源。"""
        msg_type = data.get("type", "unknown")
        if msg_type in ("heartbeat", "pong"):
            return  # 心跳静默

        sid = self._wolfx_source_map.get(msg_type)
        if sid:
            await self.signal_bus.emit(sid, data)
        else:
            logger.info(f"[Wolfx] 未匹配 type={msg_type}")

    def _get_storage_path(self) -> Path:
        try:
            from astrbot.api.star import StarTools
            return StarTools.get_data_dir("mix_astrbot_plugin_disaster_warning")
        except Exception:
            return Path("data") / "mix_disaster"

    def _setup_ws_connections(self, sources: dict):
        """配置 WebSocket 连接（仅启用的组，直接从配置文件读取）。"""
        # 直接从文件读，避免 self.config 为空或格式问题
        ds_cfg = {}
        try:
            import json
            cfg_path = Path(
                r"C:\Users\ZhuanZ.DESKTOP-PH97BKO\.astrbot\data\config"
                r"\mix_astrbot_plugin_disaster_warning_config.json"
            )
            if cfg_path.exists():
                with open(cfg_path, encoding="utf-8-sig") as f:
                    raw = json.load(f)
                ds_cfg = raw.get("data_sources", {})
        except Exception as e:
            logger.error(f"[Mix] 读取配置文件失败: {e}")

        groups: dict = {}
        for sid, entry in sources.items():
            if sid.startswith("_") or not isinstance(entry, dict):
                continue
            # 检查该数据源所属组是否启用
            group = entry.get("config_group", "")
            group_cfg = ds_cfg.get(group, {})
            if isinstance(group_cfg, dict) and group_cfg.get("enabled", True) is False:
                continue

            url = entry.get("connection_url", "")
            handler = entry.get("connection_handler", "")
            if handler and url:
                key = entry.get("connection_group", handler)
                if key not in groups:
                    groups[key] = {"url": url, "backup": entry.get("connection_backup_url", "")}
        for name, cfg in groups.items():
            self.ws_manager.add_connection(name, cfg["url"], cfg.get("backup", ""))

    def _setup_http_pollers(self, sources: dict, router: MessageRouter):
        POLLERS = {
            "funvisis_http": ("http://www.funvisis.gob.ve/maravilla.json", 120, False),
            "cenais_http": ("https://www.cenais.gob.cu/lastquake/php/lastweek.php", 120, False),
            "geonet_http": ("https://api.geonet.org.nz/quake?MMI=-1", 30, False),
            "nrcan_http": ("https://www.earthquakescanada.nrcan.gc.ca/cache/earthquakes/canada-30.xml", 60, True),
            "tmd_http": ("https://earthquake.tmd.go.th/", 120, True),
            "phivolcs_http": ("https://earthquake.phivolcs.dost.gov.ph/", 120, True),
            "csnc_http": ("https://www.sismologia.cl/index.html", 120, True),
        }
        for sid, (url, interval, raw_text) in POLLERS.items():
            if sid in sources:
                self.http_poll_manager.add_poller(
                    name=sid, url=url, interval=interval,
                    handler=lambda n, d: self.signal_bus.emit(n, d),
                    raw_text=raw_text,
                )

    # ═══════════════════ 数据查询 ═══════════════════

    async def _query_source_events(
        self, source_id: str, limit: int = 10, days: int = 7
    ) -> list[dict]:
        """按数据源查询最近地震事件。"""
        if not self.database:
            return []
        return await self.database.query_earthquakes(source_id, limit, days)

    async def _query_source_eew(self, source_id: str, limit: int = 10) -> list[dict]:
        """查询 EEW 事件。"""
        if not self.database:
            return []
        return await self.database.query_eew(source_id, limit)

    async def _query_weather_alarms(
        self, province: str = "", limit: int = 20
    ) -> list[dict]:
        """查询气象预警。"""
        if not self.database:
            return []
        return await self.database.query_weather(province, limit)

    async def _query_eew_status(self) -> str:
        """查询各机构 EEW 状态。"""
        sources_eew = [
            "cea_fanstudio", "cwa_fanstudio", "jma_fanstudio",
            "jma_p2p", "jma_wolfx", "sa_fanstudio",
            "kma_eew_fanstudio", "global_quake",
        ]
        lines = ["📡 EEW 状态"]
        for sid in sources_eew:
            rows = await self._query_source_eew(sid, 1)
            if rows:
                r = rows[0]
                t = _fmt_time_short(r.get("time", ""))
                mag = r.get("magnitude", "?")
                place = r.get("place_name", "") or r.get("description", "")[:20]
                lines.append(f"  ✅ {_SOURCE_DISPLAY.get(sid, sid)} M{mag} {place} ({t})")
            else:
                lines.append(f"  ⏳ {_SOURCE_DISPLAY.get(sid, sid)} 暂无数据")
        return "\n".join(lines)

    # ═══════════════════ 命令: 帮助 ═══════════════════

    @filter.regex(r"^/灾害预警$")
    async def help_cmd(self, event: AstrMessageEvent):
        yield event.plain_result(_PLUGIN_HELP.format(version=get_plugin_version()))

    # ═══════════════════ 命令: 管理 ═══════════════════

    @filter.regex(r"^/灾害预警状态$")
    async def status_cmd(self, event: AstrMessageEvent):
        lines = ["📊 Mix灾害预警状态"]
        if self.ws_manager:
            for name, ok in self.ws_manager.get_status().items():
                lines.append(f"  {'✅' if ok else '❌'} {name}")
        # Wolfx 源状态
        wolfx_sources = [k for k in _SOURCE_DISPLAY if "wolfx" in k]
        if wolfx_sources and self.database:
            lines.append("  ── Wolfx 源 ──")
            for sid in wolfx_sources:
                rows = await self._query_source_events(sid, 1)
                if rows:
                    r = rows[0]
                    t = _fmt_time_short(r.get("time", ""))
                    mag = r.get("magnitude", "?")
                    lines.append(f"    ✅ {_SOURCE_DISPLAY.get(sid, sid)} M{mag} ({t})")
                else:
                    lines.append(f"    ⏳ {_SOURCE_DISPLAY.get(sid, sid)} 暂无数据")
        # DB 统计
        rows = await self.database.execute_raw(
            "SELECT type, COUNT(*) as c FROM events GROUP BY type"
        ) if self.database else []
        for r in rows:
            lines.append(f"  📦 {r['type']}: {r['c']} 条")
        uptime = __import__("time").time() - self._start_time
        lines.append(f"⏱ 运行: {uptime / 3600:.1f}h")
        yield event.plain_result("\n".join(lines))

    @filter.regex(r"^/灾害预警重连$")
    async def reconnect_cmd(self, event: AstrMessageEvent):
        if not await self._is_admin(event):
            yield event.plain_result("❌ 仅管理员")
            return
        results = []
        if self.ws_manager:
            for name, ok in self.ws_manager.get_status().items():
                if not ok:
                    ok2 = await self.ws_manager.reconnect(name)
                    results.append(f"  {'✅' if ok2 else '❌'} {name}")
        yield event.plain_result(
            "🔄 重连:\n" + ("\n".join(results) if results else "  无离线连接")
        )

    @filter.regex(r"^/灾害预警统计$")
    async def stats_cmd(self, event: AstrMessageEvent):
        rows = await self.database.execute_raw(
            "SELECT source, COUNT(*) as c FROM events GROUP BY source ORDER BY c DESC LIMIT 15"
        ) if self.database else []
        lines = ["📊 数据源事件量 TOP15"]
        for r in rows:
            name = _SOURCE_DISPLAY.get(r["source"], r["source"])
            lines.append(f"  {name}: {r['c']}")
        yield event.plain_result("\n".join(lines))

    @filter.regex(r"^/灾害预警统计清除$")
    async def stats_clear_cmd(self, event: AstrMessageEvent):
        if not await self._is_admin(event):
            yield event.plain_result("❌ 仅管理员")
            return
        yield event.plain_result("✅ 统计清除功能需停用旧版插件后启用新版数据库")

    @filter.regex(r"^/灾害预警推送开关$")
    async def toggle_push_cmd(self, event: AstrMessageEvent):
        if not await self._is_admin(event):
            yield event.plain_result("❌ 仅管理员")
            return
        yield event.plain_result("🔔 推送开关待 WebSocket 接入后实现")

    @filter.regex(r"^/灾害预警配置(?:\s|$)")
    async def config_cmd(self, event: AstrMessageEvent, action: str = None, target: str = None):
        if action == "查看":
            yield event.plain_result(f"全局配置: {dict(self.config)}")
        else:
            yield event.plain_result("用法: /灾害预警配置 查看")

    # ═══════════════════ 命令: 查询 ═══════════════════

    @filter.regex(r"^/(?:地震列表查询|地震列表)(?:\s|$)")
    async def earthquake_list_cmd(
        self, event: AstrMessageEvent,
        source: str = "cenc", count: int = 9, mode: str = "text",
    ):
        # 解析 source 简写 → 完整 source_id
        src_map = {
            "cenc": "cenc_fanstudio", "cea": "cea_fanstudio",
            "jma": "jma_fanstudio", "usgs": "usgs_fanstudio",
            "cwa": "cwa_fanstudio", "emsc": "emsc_fanstudio",
        }
        sid = src_map.get(source, source)
        rows = await self._query_source_events(sid, min(count, 30))
        if not rows:
            yield event.plain_result(f"📋 {source} 暂无地震数据")
            return
        lines = [f"📋 {source} 最新地震 ({len(rows)} 条)"]
        for i, r in enumerate(rows[:count], 1):
            mag = r.get("magnitude", "?")
            place = (r.get("place_name") or r.get("description") or "未知")[:25]
            t = _fmt_time_short(r.get("time", ""))
            lines.append(f"  {i}. M{mag} {place} {t}")
        yield event.plain_result("\n".join(lines))

    @filter.regex(r"^/(?:地震预警查询|地震预警)(?:\s+(.+))?$")
    async def eew_query_cmd(self, event: AstrMessageEvent, source: str = None):
        if not source:
            text = await self._query_eew_status()
            yield event.plain_result(text)
            return

        src_map = {
            "cea": "cea_fanstudio", "cenc": "cenc_fanstudio",
            "jma": "jma_fanstudio", "cwa": "cwa_fanstudio",
            "usgs": "usgs_fanstudio", "emsc": "emsc_fanstudio",
            "hko": "hko_fanstudio", "sa": "sa_fanstudio",
            "kma": "kma_eew_fanstudio", "gq": "global_quake",
            "globalquake": "global_quake",
        }
        sid = src_map.get(source.strip().lower(), source.strip())

        rows = await self.database.execute_raw(
            "SELECT * FROM events WHERE source=? AND type='earthquake_warning' ORDER BY id DESC LIMIT 1",
            (sid,),
        )
        if not rows:
            yield event.plain_result(f"📡 {sid} 暂无地震预警数据")
            return

        r = rows[0]
        try:
            from datetime import datetime
            occurred_at = None
            ts = r.get("time") or ""
            if ts:
                try:
                    occurred_at = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    pass

            raw_json = r.get("raw_json")
            raw = {}
            if raw_json:
                try:
                    raw = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
                except Exception:
                    raw = {}

            eew = EewEvent(
                source_id=sid,
                event_id=r.get("real_event_id", "") or r.get("source", ""),
                occurred_at=occurred_at,
                latitude=r.get("latitude"),
                longitude=r.get("longitude"),
                depth=r.get("depth"),
                magnitude=r.get("magnitude"),
                place_name=r.get("place_name") or r.get("description"),
                report_num=r.get("report_num"),
                max_intensity=str(raw.get("maxIntensity", raw.get("max_intensity", ""))) or None,
                is_final=bool(raw.get("isFinal", raw.get("is_final", False))),
                raw=raw,
            )
            text = present_eew(eew)
            yield event.plain_result(text)
        except Exception as e:
            logger.error(f"[EEW] 格式展示异常: {e}", exc_info=True)
            yield event.plain_result(f"❌ 格式展示失败: {e}")

    @filter.regex(r"^/(?:气象预警查询|气象预警)(?:\s|$)")
    async def weather_query_cmd(
        self, event: AstrMessageEvent,
        province: str = "",
    ):
        province = province or ""
        rows = await self._query_weather_alarms(province, 20)
        if not rows:
            yield event.plain_result("🌤 暂无气象预警数据")
            return
        lines = [f"🌤 气象预警 {'全国' if not province else province} ({len(rows)} 条)"]
        for r in rows[:15]:
            level = r.get("level", "")
            icon = LEVEL_COLORS.get(level, "")
            type_code = r.get("weather_type_code", "")
            type_name = WEATHER_TYPE_MAP.get(type_code, "")
            desc = (r.get("subtitle") or r.get("description") or "")[:50]
            place = r.get("place_name", "") or ""
            t = _fmt_time_short(r.get("time", ""))
            lines.append(f"  {icon}{type_name}{level} {place} {desc} {t}")
        yield event.plain_result("\n".join(lines))

    @filter.regex(r"^/(?:台风|typhoon)")  # 参数从 message_str 自行解析
    async def typhoon_cmd(self, event: AstrMessageEvent):
        """台风查询。
        /台风            全部活跃台风 + 逐张渲染路径图
        /台风 米克拉     按名称/编号查询指定台风
        """
        raw = (event.message_str or "").strip()
        arg = ""
        for prefix in ("/台风", "/typhoon"):
            if raw.startswith(prefix):
                arg = raw[len(prefix):].strip()
                break

        if not self._typhoon_manager:
            yield event.plain_result("🌀 台风管理器未就绪")
            return

        # ── 按名称/编号查询单个 ──
        if arg:
            data = self._typhoon_manager.get_event(arg)
            if not data:
                yield event.plain_result(f"🌀 未找到台风: {arg}")
                return
            detail = self._typhoon_manager.get_event_detail(arg)
            te = self._typhoon_manager.get_typhoon_event_for_render(arg)
            img_b64 = None
            if te and te.track_points and self._typhoon_renderer:
                try:
                    img_path = os.path.join(
                        self._temp_dir,
                        f"typhoon_{te.code}_{int(__import__('time').time())}.png",
                    )
                    result = await self._typhoon_renderer.render(te, img_path)
                    if result and os.path.exists(result):
                        with open(result, "rb") as f:
                            img_b64 = base64.b64encode(f.read()).decode()
                        try:
                            os.unlink(result)
                        except Exception:
                            pass
                except Exception as e:
                    logger.warning(f"[台风] 单图渲染异常: {e}")
            if img_b64:
                yield event.chain_result([Plain(detail), Image.fromBase64(img_b64)])
            else:
                yield event.plain_result(detail)
            return

        # ── 列出全部活跃台风 ──
        active = self._typhoon_manager.get_active()
        if not active:
            # 后台轮询还没抓到数据？立即触发双源
            logger.info("[台风] 无数据，立即触发 CMA+JMA 轮询...")
            await asyncio.gather(
                self._typhoon_manager.poll_cma(),
                self._typhoon_manager.poll_jma(),
            )
            active = self._typhoon_manager.get_active()
            if not active:
                yield event.plain_result("🌀 暂无台风数据")
                return

        lines = [f"🌀 近期台风（{len(active)} 个活跃）"]
        for t in active:
            name = t["name_cn"] or t["name_en"] or t["code"]
            cat = t["category_name"]
            wind = f" {t['wind_speed']}m/s" if t["wind_speed"] else ""
            ts = t["last_updated"].strftime("%m/%d %H:%M") if t["last_updated"] else ""
            src = f"[{t.get('source', '').upper()}]" if t.get("source") else ""
            lines.append(f"  {src} {name}（{t['code']}）{cat}{wind} {ts}")
        text = "\n".join(lines)

        # ── 逐个渲染路径图（串行，防止同时开太多浏览器页面） ──
        if self._typhoon_renderer:
            images_b64 = []
            for t in active:
                te = self._typhoon_manager.get_typhoon_event_for_render(t["code"])
                if not te or not te.track_points:
                    continue
                try:
                    img_path = os.path.join(
                        self._temp_dir,
                        f"typhoon_{te.code}_{int(__import__('time').time())}.png",
                    )
                    result = await self._typhoon_renderer.render(te, img_path)
                    if result and os.path.exists(result):
                        with open(result, "rb") as f:
                            images_b64.append(base64.b64encode(f.read()).decode())
                        try:
                            os.unlink(result)
                        except Exception:
                            pass
                except Exception as e:
                    logger.warning(f"[台风] {t['code']} 路径图渲染异常: {e}")

            if images_b64:
                chain = [Plain(text)]
                for b64_img in images_b64:
                    chain.append(Image.fromBase64(b64_img))
                yield event.chain_result(chain)
                return

        yield event.plain_result(text)

    @filter.regex(r"^/(?:震央|hypo)\s*(.*)$")
    async def hypo_cmd(self, event: AstrMessageEvent):
        """震央分布图。"""
        import importlib
        from message.render import hypo_renderer as _hr_mod
        importlib.reload(_hr_mod)
        HypoRenderer = _hr_mod.HypoRenderer
        parse_date_args = _hr_mod.parse_date_args

        try:
            raw = (event.message_str or "").strip()
            arg = ""
            for prefix in ("/震央", "/hypo"):
                if raw.startswith(prefix):
                    arg = raw[len(prefix):].strip()
                    break

            dates = parse_date_args(arg)
            if not dates:
                yield event.plain_result("🗺️ 日期参数无法解析。示例：/震央、/震央 6月14日、/震央 6月14日-6月20日")
                return

            renderer = HypoRenderer(plugin_root=self._plugin_root)
            img_path = os.path.join(
                self._temp_dir,
                f"hypo_{int(__import__('time').time())}.png",
            )
            result = await renderer.render(dates, img_path)
            # 兼容新版 dict / 旧版 str 返回值
            if isinstance(result, dict):
                out_path, te, cd = result["path"], result["total_events"], result["covered_days"]
            elif isinstance(result, str):
                out_path, te, cd = result, 0, 0
            else:
                out_path = None
            if out_path and os.path.exists(out_path):
                with open(out_path, "rb") as f:
                    b64_img = base64.b64encode(f.read()).decode()
                try:
                    os.unlink(out_path)
                except Exception:
                    pass
                time_range = arg if arg else "今日"
                yield event.chain_result([
                    Plain(
                        f"[JMA/日本气象厅 震央分布绘制]\n"
                        f"时间范围:{time_range}共{len(dates)}天\n"
                        f"总震央数:{te}\n"
                        f"覆盖天数:{cd}"
                    ),
                    Image.fromBase64(b64_img),
                ])
            else:
                yield event.plain_result("🗺️ 震央分布图渲染失败")
        except ImportError as e:
            yield event.plain_result(f"🗺️ 缺少依赖: {e}（需要安装 Pillow）")
        except Exception as e:
            logger.error(f"[震央] 渲染异常: {e}", exc_info=True)
            yield event.plain_result(f"🗺️ 渲染异常: {e}")

    @filter.regex(r"^/(?:海啸|tsunami)(?:\s|$)")
    async def tsunami_cmd(self, event: AstrMessageEvent):
        rows = await self.database.execute_raw(
            "SELECT * FROM events WHERE type='tsunami' ORDER BY id DESC LIMIT 3"
        ) if self.database else []
        if not rows:
            yield event.plain_result("🌊 暂无海啸数据")
            return
        lines = ["🌊 海啸信息"]
        for r in rows:
            title = r.get("description", r.get("subtitle", ""))[:50]
            t = _fmt_time_short(r.get("time", ""))
            lines.append(f"  {title} {t}")
        yield event.plain_result("\n".join(lines))

    # ═══════════════════ 快捷查询 ═══════════════════

    async def _quick_query(self, event: AstrMessageEvent, source_id: str, display: str):
        rows = await self._query_source_events(source_id, 5)
        if not rows:
            yield event.plain_result(f"📡 {display} 暂无数据")
            return
        r = rows[0]

        from domain.models import EarthquakeReport, EewEvent
        from datetime import datetime
        raw_json = r.get("raw_json")
        raw = {}
        if raw_json:
            try:
                raw = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
            except Exception:
                raw = {}

        ts = r.get("time") or ""
        occurred_at = None
        if ts:
            try:
                occurred_at = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass

        event_type = str(r.get("type") or "")
        if event_type in ("earthquake_warning", "eew"):
            eew = EewEvent(
                source_id=source_id,
                event_id=r.get("real_event_id", "") or source_id,
                occurred_at=occurred_at,
                latitude=r.get("latitude"),
                longitude=r.get("longitude"),
                depth=r.get("depth"),
                magnitude=r.get("magnitude"),
                place_name=r.get("place_name") or r.get("description"),
                report_num=r.get("report_num"),
                max_intensity=str(raw.get("maxIntensity", raw.get("max_intensity", ""))) or None,
                is_final=bool(raw.get("isFinal", raw.get("is_final", False))),
                raw=raw,
            )
            text = present_eew(eew)
        else:
            rep = EarthquakeReport(
                source_id=source_id,
                event_id=r.get("real_event_id", "") or source_id,
                occurred_at=occurred_at,
                latitude=r.get("latitude"),
                longitude=r.get("longitude"),
                depth=r.get("depth"),
                magnitude=r.get("magnitude"),
                place_name=r.get("place_name") or r.get("description"),
                region=r.get("subtitle") or "",
                report_num=r.get("report_num"),
                raw=raw,
            )
            text = present_earthquake_report(rep)

        lat, lon = r.get("latitude"), r.get("longitude")
        if lat is not None and lon is not None and self._map_builder:
            try:
                msg_cfg = self.config.get("message_format", {})
                thumb_cfg = dict(msg_cfg)
                thumb_cfg["map_zoom_level"] = 4
                thumb_path = await self._map_builder.render_map_image(lat, lon, thumb_cfg)
                detail_cfg = dict(msg_cfg)
                detail_cfg["map_zoom_level"] = 8
                detail_path = await self._map_builder.render_map_image(lat, lon, detail_cfg)

                b64_list = []
                for p in (thumb_path, detail_path):
                    if p and os.path.exists(p):
                        with open(p, "rb") as f:
                            b64_list.append(base64.b64encode(f.read()).decode())
                        try:
                            os.unlink(p)
                        except Exception:
                            pass

                if b64_list:
                    yield event.chain_result([Plain(text)] + [Image.fromBase64(b) for b in b64_list])
                    return
            except Exception as e:
                logger.warning(f"[查询] {display} 地图渲染异常: {e}")

        yield event.plain_result(text)

    @filter.regex(r"^/cenc(?:\s|$)")
    async def q_cenc(self, e):
        async for r in self._quick_query(e, "cenc_fanstudio", "中国地震台网"): yield r

    @filter.regex(r"^/cea(?:\s|$)")
    async def q_cea(self, e):
        async for r in self._quick_query(e, "cea_fanstudio", "中国地震预警网"): yield r

    @filter.regex(r"^/jma(?:\s|$)")
    async def q_jma(self, e):
        async for r in self._quick_query(e, "jma_fanstudio", "日本气象厅"): yield r

    @filter.regex(r"^/usgs(?:\s|$)")
    async def q_usgs(self, e):
        async for r in self._quick_query(e, "usgs_fanstudio", "USGS"): yield r

    @filter.regex(r"^/cwa(?:\s|$)")
    async def q_cwa(self, e):
        async for r in self._quick_query(e, "cwa_fanstudio", "台湾气象署"): yield r

    @filter.regex(r"^/emsc(?:\s|$)")
    async def q_emsc(self, e):
        async for r in self._quick_query(e, "emsc_fanstudio", "EMSC"): yield r

    @filter.regex(r"^/hko(?:\s|$)")
    async def q_hko(self, e):
        async for r in self._quick_query(e, "hko_fanstudio", "HKO"): yield r

    @filter.regex(r"^/gfz(?:\s|$)")
    async def q_gfz(self, e):
        async for r in self._quick_query(e, "gfz_fanstudio", "GFZ"): yield r

    @filter.regex(r"^/bcsf(?:\s|$)")
    async def q_bcsf(self, e):
        async for r in self._quick_query(e, "bcsf_fanstudio", "BCSF"): yield r

    @filter.regex(r"^/fssn(?:\s|$)")
    async def q_fssn(self, e):
        async for r in self._quick_query(e, "fssn_fanstudio", "FSSN"): yield r

    @filter.regex(r"^/kma(?:\s|$)")
    async def q_kma(self, e):
        async for r in self._quick_query(e, "kma_fanstudio", "KMA"): yield r

    @filter.regex(r"^/sa(?:\s|$)")
    async def q_sa(self, e):
        async for r in self._quick_query(e, "sa_fanstudio", "ShakeAlert"): yield r

    @filter.regex(r"^/nrcan(?:\s|$)")
    async def q_nrcan(self, e):
        async for r in self._quick_query(e, "nrcan_http", "NRCan"): yield r

    @filter.regex(r"^/geonet(?:\s|$)")
    async def q_geonet(self, e):
        async for r in self._quick_query(e, "geonet_http", "GeoNet"): yield r

    @filter.regex(r"^/(?:气象|weather)(?:\s|$)")
    async def q_weather(self, e):
        async for r in self.weather_query_cmd(e): yield r

    @filter.regex(r"^/北京(?:\s|$)")
    async def q_bj(self, e):
        async for r in self._quick_query(e, "beijing_fanstudio", "北京台网"): yield r

    @filter.regex(r"^/广西(?:\s|$)")
    async def q_gx(self, e):
        async for r in self._quick_query(e, "guangxi_fanstudio", "广西台网"): yield r

    @filter.regex(r"^/宁夏(?:\s|$)")
    async def q_nx(self, e):
        async for r in self._quick_query(e, "ningxia_fanstudio", "宁夏台网"): yield r

    @filter.regex(r"^/山西(?:\s|$)")
    async def q_sx(self, e):
        async for r in self._quick_query(e, "shanxi_fanstudio", "山西台网"): yield r

    @filter.regex(r"^/云南(?:\s|$)")
    async def q_yn(self, e):
        async for r in self._quick_query(e, "yunnan_fanstudio", "云南台网"): yield r

    @filter.regex(r"^/tg(?:\s|$)")
    async def q_tg(self, e):
        async for r in self._quick_query(e, "tmd_http", "TMD"): yield r

    @filter.regex(r"^/zl(?:\s|$)")
    async def q_zl(self, e):
        async for r in self._quick_query(e, "csnc_http", "CSNC"): yield r

    @filter.regex(r"^/flb(?:\s|$)")
    async def q_flb(self, e):
        async for r in self._quick_query(e, "phivolcs_http", "PHIVOLCS"): yield r

    @filter.regex(r"^/gb(?:\s|$)")
    async def q_gb(self, e):
        async for r in self._quick_query(e, "cenais_http", "CENAIS"): yield r

    @filter.regex(r"^/(?:snet|s-net|S-Net)(?:\s|$)")
    async def q_snet(self, e):
        """S-Net 测站分布查询。

        用法:
          /snet         正常模式：下载 MSIL 瓦片并渲染
          /snet random  调试模式：随机震度伪数据
          /snet 7       调试模式：全部测站统一震度 7
          /snet 6+      调试模式：全部测站统一震度 6強
          /snet 6-      调试模式：全部测站统一震度 6弱
          /snet 5+      调试模式：全部测站统一震度 5強
          /snet 5-      调试模式：全部测站统一震度 5弱
          /snet 4       调试模式：全部测站统一震度 4
          /snet 3       调试模式：全部测站统一震度 3
          /snet 2       调试模式：全部测站统一震度 2
          /snet 1       调试模式：全部测站统一震度 1
          /snet 0       调试模式：全部测站统一震度 0
        """
        import random
        from datetime import datetime, timezone, timedelta
        from PIL import Image as PILImage

        raw_text = e.message_str if hasattr(e, 'message_str') else str(e.message_obj)
        parts = raw_text.strip().split()
        args = parts[-1] if len(parts) >= 2 and parts[-1] != "/snet" else ""

        # 调试震度参数映射
        SHINDO_MAP = {
            "7": 7.0, "6+": 6.2, "6-": 5.8,
            "5+": 5.2, "5-": 4.8, "4": 4.0,
            "3": 3.0, "2": 2.0, "1": 1.0, "0": 0.0,
        }

        # 引入 SNET 坐标和 MSIL RGB 表
        try:
            from .parser.snet import SNET_REAL_COORDS
            from .message.render.snet_map_renderer import MSIL_SHINDO_TO_RGB
        except ImportError:
            from parser.snet import SNET_REAL_COORDS
            from message.render.snet_map_renderer import MSIL_SHINDO_TO_RGB
        import io, base64

        stations = []
        ts_str = ""

        if args:
            # ── 调试模式：伪造测站数据 ──
            if args == "random":
                for nm, (lat, lon) in SNET_REAL_COORDS.items():
                    val = round(random.uniform(-3, 7), 3)
                    key = round(val * 10)
                    rgb = MSIL_SHINDO_TO_RGB.get(key, (63, 250, 54))
                    stations.append({"name": nm, "lat": lat, "lon": lon, "shindo": val, "rgb": rgb})
            elif args in SHINDO_MAP:
                val = SHINDO_MAP[args]
                key = round(val * 10)
                rgb = MSIL_SHINDO_TO_RGB.get(key, (63, 250, 54))
                stations = [
                    {"name": nm, "lat": lat, "lon": lon, "shindo": val, "rgb": rgb}
                    for nm, (lat, lon) in SNET_REAL_COORDS.items()
                ]
            else:
                yield e.plain_result(f"未知参数: {args}，支持: random, 7, 6+, 6-, 5+, 5-, 4, 3, 2, 1, 0")
                return
            ts_str = datetime.now(timezone.utc).strftime("%Y%m%d%H%M00")
        else:
            # ── 正常模式：下载 MSIL 瓦片 ──
            import aiohttp
            now = datetime.now(timezone.utc)
            # 往前试 3 分钟
            for offset_min in range(3):
                try_ts = now - timedelta(minutes=offset_min, seconds=90)
                try_ts = try_ts.replace(second=0, microsecond=0)
                ts_str = try_ts.strftime("%Y%m%d%H%M00")
                tiles = {}
                async with aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(ssl=False),
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as sess:
                    for tn, fn in [("y11", "11"), ("y12", "12")]:
                        url = f"https://www.msil.go.jp/data/tiles/smoni/tileimage/{ts_str}/{ts_str}/5/28/{fn}.png"
                        try:
                            async with sess.get(url) as r:
                                if r.status == 200:
                                    tiles[tn] = base64.b64encode(await r.read()).decode()
                        except Exception:
                            pass
                if len(tiles) >= 2:
                    break
            else:
                yield e.plain_result("📡 S-net 暂无数据（瓦片下载失败）")
                return

            decoded = {}
            for tn, b in tiles.items():
                decoded[tn] = PILImage.open(io.BytesIO(base64.b64decode(b))).convert("RGB")
            from .parser.snet import _build_stations as _snet_build_stations
            stations = _snet_build_stations(decoded)
            if not stations:
                yield e.plain_result("📡 S-net 未解析到测站")
                return

        triggered = [s for s in stations if s["shindo"] >= 0]
        text_parts = [f"[NIED S-Net海底震度分布]"]
        text_parts.append(f"触发方式：{'调试' if args else '手动触发'}")
        text_parts.append(f"触发测站数量：{len(triggered)}/{len(stations)}")
        text_parts.append("=" * 19)
        text_parts.append("降序排名前10测站")
        for s in sorted(stations, key=lambda x: x["shindo"], reverse=True)[:10]:
            nm = s["name"]
            sh = s["shindo"]
            lbl = ("震度7" if sh >= 6.5 else "震度6強" if sh >= 6.0 else "震度6弱" if sh >= 5.5
                   else "震度5強" if sh >= 5.0 else "震度5弱" if sh >= 4.5 else "震度4" if sh >= 3.5
                   else "震度3" if sh >= 2.5 else "震度2" if sh >= 1.5 else "震度1" if sh >= 0.5
                   else "震度0" if sh >= 0 else "震度0以下")
            text_parts.append(f"{nm:<12s} {sh:>7.3f}({lbl})")
        text_parts.append("=" * 19)
        text = "\n".join(text_parts)

        # 渲染测站图
        if self._snet_renderer:
            try:
                ts_str = ts_str or datetime.now(timezone.utc).strftime("%Y%m%d%H%M00")
                img_path = os.path.join(
                    self._temp_dir,
                    f"snet_{ts_str}_{int(__import__('time').time())}.png",
                )
                out = await self._snet_renderer.render(stations, img_path, ts_str)
                if out and os.path.exists(out):
                    with open(out, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode()
                    try:
                        os.unlink(out)
                    except Exception:
                        pass
                    yield e.chain_result([Plain(text), Image.fromBase64(b64)])
                    return
            except Exception as ex:
                logger.warning(f"[SNET] 渲染异常: {ex}")

        yield e.plain_result(text)

    # ═══════════════════ 辅助 ═══════════════════

    async def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            admins = self.config.get("admin_users", [])
            return event.get_sender_id() in admins
        except Exception:
            return False

    async def on_astrbot_loaded(self):
        logger.debug("[Mix] 准备就绪")
