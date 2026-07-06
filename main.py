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
    from .domain.models import EewEvent, EarthquakeReport, EventEnvelope
    from .message.presenters import present, present_eew, present_earthquake_report, WEATHER_TYPE_MAP, LEVEL_COLORS
    from .message.browser import BrowserManager
    from .message.render.typhoon_map_renderer import TyphoonMapRenderer
    from .message.render.snet_map_renderer import SnetMapRenderer
    from .message.render.hypo_renderer import HypoRenderer, parse_date_args
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
    from domain.models import EewEvent, EarthquakeReport, EventEnvelope
    from message.presenters import present, present_eew, present_earthquake_report, WEATHER_TYPE_MAP, LEVEL_COLORS
    from message.browser import BrowserManager
    from message.render.typhoon_map_renderer import TyphoonMapRenderer
    from message.render.snet_map_renderer import SnetMapRenderer
    from message.render.hypo_renderer import HypoRenderer, parse_date_args
    from message.notification import SystemNotificationService, NotificationCenter
    from storage.database import DatabaseManager
    from storage.stats import StatisticsManager
    from storage.session import SessionConfigManager
    from utils.version import get_plugin_version
    from core.message.builders.map_attachment_builder import MapAttachmentBuilder
    from services.typhoon_manager import TyphoonManager

# 触发解析器注册（显式 import，AstrBot 最可靠）
from .parser.fan_studio import cea, cenc, cwa, jma, global_sources, provincial, generic_eew, tsunami, weather
from .parser.wolfx import eew as wolfx_eew, province as wolfx_province, report as wolfx_report, http_report as wolfx_http_report, http_eew as wolfx_http_eew
from .parser.p2p import eew as p2p_eew, report as p2p_report, tsunami as p2p_tsunami
from .parser.p2p import http_eew as p2p_http_eew, http_report as p2p_http_report, http_tsunami as p2p_http_tsunami
from .parser import global_quake as gq_parser, snet as snet_parser
from .parser.http_poll import parsers as http_poll_parsers
from .parser.http_poll import icl_parser  # noqa: F401 — ICL 注册
from .parser.http_poll import cenc_eew  # noqa: F401 — CEA EEW Plan B
from .parser.http_poll import cenc_report  # noqa: F401 — CENC 报告 Plan B
from .parser.typhoon import cma as typhoon_cma, jma as typhoon_jma
from .services.cenc_eew_plan_b import CencEewPlanB, _CENC_SHORT_NAME, _CENC_PROVINCES, _CENC_BASE

import aiohttp


# ── 来源简写映射（供查询命令共享） ──

_SHORT_SRC_MAP: dict[str, str] = {
    "cenc": "cenc_fanstudio", "cea": "cea_fanstudio",
    "jma": "jma_fanstudio", "usgs": "usgs_fanstudio",
    "cwa": "cwa_fanstudio", "emsc": "emsc_fanstudio",
    "hko": "hko_fanstudio", "gfz": "gfz_fanstudio",
    "usp": "usp_fanstudio", "bcsf": "bcsf_fanstudio",
    "fssn": "fssn_fanstudio", "kma": "kma_fanstudio",
    "sa": "sa_fanstudio", "kma_eew": "kma_eew_fanstudio",
    "gq": "global_quake", "globalquake": "global_quake",
    "geonet": "geonet_http", "nrcan": "nrcan_http",
    "csnc": "csnc_http", "phivolcs": "phivolcs_http",
    "tmd": "tmd_http", "funvisis": "funvisis_http",
    "bmkg": "bmkg_http",
    "cenais": "cenais_http", "icl": "icl_http",
    "snet": "snet",
}


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


def _render_intensity_images(renderer, magnitude: float | None, depth: float | None) -> list[bytes]:
    """渲染烈度震度双图，返回 bytes 列表。"""
    import os
    if renderer is None or magnitude is None:
        return []
    try:
        s_path, i_path = renderer.render_both(magnitude, depth)
        result = []
        for p in (s_path, i_path):
            if p and os.path.exists(p):
                with open(p, "rb") as f:
                    result.append(f.read())
                try:
                    os.unlink(p)
                except Exception:
                    pass
        return result
    except Exception as ex:
        logger.warning(f"[双图] 渲染异常: {ex}")
        return []


# ── 源 → 显示名映射 ──

_SOURCE_DISPLAY: dict[str, str] = {
    "cea_fanstudio": "中国地震预警网", "cea_pr_fanstudio": "中国地震预警网(省)",
    "cenc_fanstudio": "中国地震台网", "cenc_wolfx": "CENC(Wolfx)",
    "cwa_fanstudio": "台湾气象署", "cwa_wolfx": "CWA(Wolfx)",
    "jma_fanstudio": "日本气象厅", "jma_wolfx": "JMA(Wolfx)", "jma_wolfx_info": "JMA情报(Wolfx)",
    "jma_wolfx_http": "JMA(Wolfx HTTP)", "jma_wolfx_info_http": "JMA情报(Wolfx HTTP)",
    "cwa_wolfx_http": "CWA(Wolfx HTTP)", "kma_wolfx_http": "KMA(Wolfx HTTP)",
    "sc_wolfx_http": "四川(Wolfx HTTP)", "fj_wolfx_http": "福建(Wolfx HTTP)", "cq_wolfx_http": "重庆(Wolfx HTTP)",
    "jma_p2p_http": "JMA(P2P HTTP)", "jma_p2p_info_http": "JMA情报(P2P HTTP)", "jma_tsunami_p2p_http": "JMA海啸(P2P HTTP)",
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
    "bmkg_http": "BMKG",
    "cenc_eew_http": "中国地震预警网", "cenc_eew_province": "中国地震预警网(省)",
    "snet": "S-net", "icl_http": "ICL",
    "beijing_fanstudio": "北京", "guangxi_fanstudio": "广西",
    "ningxia_fanstudio": "宁夏", "shanxi_fanstudio": "山西",
    "yunnan_fanstudio": "云南",
    "china_tsunami_fanstudio": "海啸", "china_weather_fanstudio": "气象",
    "sc_wolfx_eew": "四川", "fj_wolfx_eew": "福建", "cq_wolfx_eew": "重庆",
}


# ── EEW 机构分组（从 sources.json 动态构建，同 BAK get_institution_catalog） ──

def _build_eew_institutions() -> dict[str, dict]:
    """从 sources.json 读取所有 query_group=eew 的源，按 institution_key 分组。"""
    import json
    from pathlib import Path
    result: dict[str, dict] = {}
    path = Path(__file__).parent / "config" / "sources.json"
    if not path.exists():
        return result  # 空降级
    with open(path, encoding="utf-8") as f:
        sources = json.load(f)
    for sid, entry in sources.items():
        if sid.startswith("_"):
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("query_group") != "eew":
            continue
        ik = (entry.get("institution_key") or "").strip()
        if not ik:
            continue
        item = result.setdefault(ik, {
            "display_name": entry.get("institution_display_name") or ik,
            "active_name": entry.get("institution_active_name") or entry.get("institution_display_name") or ik,
            "source_ids": [],
        })
        if sid not in item["source_ids"]:
            item["source_ids"].append(sid)
    return result

_EEW_INSTITUTIONS: dict[str, dict] = _build_eew_institutions()


def _fmt_elapsed(seconds: int) -> str:
    """格式化秒数为中文时长。"""
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days > 0:
        return f"{days}天{hours}时{minutes}分{seconds}秒"
    if hours > 0:
        return f"{hours}时{minutes}分{seconds}秒"
    if minutes > 0:
        return f"{minutes}分{seconds}秒"
    return f"{seconds}秒"


# ── 帮助文本 ──

_PLUGIN_HELP = """🚨 Mix灾害预警使用说明

📋 管理:
  /灾害预警            帮助
  /灾害预警状态        服务状态
  /灾害预警重连        重连数据源（管理）
  /灾害预警统计        事件统计
  /灾害预警统计清除    清除统计（管理）
  /灾害预警配置 查看   查看配置

📋 多群组:
  /灾害预警群组                查看群组列表
  /灾害预警群组 <群组ID>       查看群组详情
  /灾害预警群组 <群组ID> set <源> <阈值>  设置阈值
  /灾害预警群组 <群组ID> clear [源]       清除覆盖

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

        # HTTP 轮询首条静默+变化追踪
        self._http_last_event: dict[str, dict] = {}

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
        # 震度/烈度图片渲染器
        self._intensity_img_renderer: "IntensityImageRenderer | None" = None
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

            # 从 AppData 生产配置文件加载全部设置（AstrBot 嵌套 schema 持久化不可靠）
            try:
                import json
                _prod_paths = [
                    Path(self._plugin_root).parent.parent.parent.parent / "AppData" / "Local" / "AstrBot" / "data" / "config" / "mix_astrbot_plugin_disaster_warning_config.json",
                    Path(self._plugin_root).parent.parent / "config" / "mix_astrbot_plugin_disaster_warning_config.json",
                ]
                for _cfg_path in _prod_paths:
                    if _cfg_path.exists():
                        with open(_cfg_path, encoding="utf-8-sig") as _f:
                            _file_cfg = json.load(_f)
                        # earthquake_filters: 用文件值覆盖，但跳过全是 0 的条目
                        # （AstrBot 持久化bug导致某些 filter 被写成了全零）
                        _file_ef = _file_cfg.get("earthquake_filters")
                        if isinstance(_file_ef, dict):
                            _cur_ef = self.config.get("earthquake_filters", {})
                            if not isinstance(_cur_ef, dict):
                                _cur_ef = {}
                            for _fid, _fcfg in _file_ef.items():
                                if _fid not in _cur_ef:
                                    _cur_ef[_fid] = _fcfg  # 补充缺失的 filter
                                elif isinstance(_fcfg, dict):
                                    # 已存在的 filter：只有文件有非零值才覆盖（用户通过 Web 界面设的）
                                    _fm = _fcfg.get("min_magnitude", 0)
                                    _fi = _fcfg.get("最小烈度", 0)
                                    if _fm != 0 or _fi != 0:
                                        _cur_ef[_fid] = _fcfg
                            self.config["earthquake_filters"] = _cur_ef
                        # groups: 支持 list 和 dict 两种格式
                        _file_grp = _file_cfg.get("groups")
                        if isinstance(_file_grp, list):
                            self.config["groups"] = list(_file_grp)
                        elif isinstance(_file_grp, dict):
                            self.config["groups"] = dict(_file_grp)
                        # 其他字段直接覆盖
                        for _k in ("push_frequency_control",
                                   "message_format", "weather_config", "strategies",
                                   "debug_config", "local_monitoring", "websocket_config",
                                   "display_timezone", "group_sessions"):
                            if _k in _file_cfg and isinstance(_file_cfg[_k], dict):
                                self.config[_k] = _file_cfg[_k]
                        # sleep_earthquake_filters: 用文件值覆盖，但跳过全是 0/默认值的条目
                        # （AstrBot 持久化bug导致嵌套 schema 的 default 覆盖用户设置）
                        _file_sleep = _file_cfg.get("sleep_earthquake_filters")
                        if isinstance(_file_sleep, dict):
                            _cur_sleep = self.config.get("sleep_earthquake_filters", {})
                            if not isinstance(_cur_sleep, dict):
                                _cur_sleep = {}
                            for _fid, _fcfg in _file_sleep.items():
                                if _fid not in _cur_sleep:
                                    _cur_sleep[_fid] = _fcfg
                                elif isinstance(_fcfg, dict):
                                    _fm = _fcfg.get("min_magnitude", 0)
                                    _fi = _fcfg.get("最小烈度", 0)
                                    if _fm != 0 or _fi != 0:
                                        _cur_sleep[_fid] = _fcfg
                            self.config["sleep_earthquake_filters"] = _cur_sleep
                            # 调试日志：追踪 GQ sleep filter 值
                            _gq_sleep = _cur_sleep.get("global_quake_filter", {})
                            if isinstance(_gq_sleep, dict):
                                logger.info(f"[Mix] sleep GQ 最小烈度 = {_gq_sleep.get('最小烈度', '未配置')} "
                                            f"(文件值={_file_sleep.get('global_quake_filter', {}).get('最小烈度', 'N/A')})")
                        logger.info(f"[Mix] 已从 {_cfg_path.name} 加载生产配置")
                        break
            except Exception as _e:
                logger.warning(f"[Mix] 无法从文件加载生产配置: {_e}")

            # ── sleep_earthquake_filters: 从独立文件加载（绕过 AstrBot 配置持久化 bug）──
            self._sleep_filters_path = self._get_storage_path() / "sleep_filters.json"
            try:
                if self._sleep_filters_path.exists():
                    with open(self._sleep_filters_path, encoding="utf-8") as _sf:
                        _sf_data = json.load(_sf)
                    if isinstance(_sf_data, dict) and _sf_data:
                        self.config["sleep_earthquake_filters"] = _sf_data
                        logger.info(f"[Mix] 已从 sleep_filters.json 加载睡眠阈值")
                        _gq = _sf_data.get("global_quake_filter", {})
                        if isinstance(_gq, dict):
                            logger.info(f"[Mix] sleep GQ 最小烈度（独立文件）= {_gq.get('最小烈度', '未配置')}")
            except Exception as _e:
                logger.warning(f"[Mix] 加载 sleep_filters.json 失败: {_e}")

            self.database = DatabaseManager(self._get_storage_path() / "events.db")
            await self.database.initialize()

            self.stats_manager = StatisticsManager(dict(self.config))
            self.session_config_manager = SessionConfigManager(dict(self.config), storage_path=self._get_storage_path())
            # 确认 groups 是否加载成功
            _loaded_groups = self.session_config_manager.list_groups()
            logger.info(f"[Mix] SessionConfigManager 群组: {list(_loaded_groups.keys())}")
            self.notification_center = NotificationCenter()

            await self.browser_manager.initialize()

            # 台风路径图渲染器
            self._typhoon_renderer = TyphoonMapRenderer(self.browser_manager, self._plugin_root)
            logger.info("[Mix] 台风路径图渲染器就绪")
            self._snet_renderer = SnetMapRenderer(self.browser_manager, self._plugin_root)
            logger.info("[Mix] SNET 测站图渲染器就绪")

            # 震度/烈度图片渲染器
            from .message.render.intensity_image_renderer import IntensityImageRenderer
            self._intensity_img_renderer = IntensityImageRenderer(os.path.join(self._plugin_root, "cache"))
            logger.info("[Mix] 震度/烈度图片渲染器就绪")

            # GlobalQuake 专属卡片构建器
            from .message.builders.global_quake_card_builder import GlobalQuakeCardBuilder
            self._gq_card_builder = GlobalQuakeCardBuilder(
                plugin_root=self._plugin_root,
                temp_dir=self._temp_dir,
                browser_manager=self.browser_manager,
            )
            logger.info("[Mix] GQ 卡片构建器就绪")

            # 区域名称翻译服务（用于 GQ 等地名中文化）
            from .utils.region_service import init_region_service
            _fe_path = Path(self._plugin_root) / "resources" / "fe_regions_data.json"
            if _fe_path.exists():
                init_region_service(str(_fe_path))
                logger.info("[Mix] 区域翻译服务就绪")
            else:
                logger.warning("[Mix] fe_regions_data.json 不存在，区域翻译不可用")

            # 台风管理器
            self._typhoon_manager = TyphoonManager(
                db=self.database,
                push_callback=self._typhoon_push_adapter,
            )
            logger.info("[Mix] 台风管理器就绪")
            # 台风轮询任务（受配置控制）
            _typhoon_enabled = self.config.get("data_sources", {}).get("typhoon", {}).get("enabled", True)
            if _typhoon_enabled:
                self._typhoon_cma_task = asyncio.create_task(self._run_typhoon_poll("cma", 300))
                self._typhoon_jma_task = asyncio.create_task(self._run_typhoon_poll("jma", 600))
                self._typhoon_cleanup_task = asyncio.create_task(self._run_typhoon_cleanup())
            else:
                logger.info("[台风] 数据源已禁用，不启动轮询")

            session_sender = SessionSender(self.context)
            push_svc = PushExecutionService(
                dict(self.config), session_sender, map_builder=self._map_builder,
                snet_renderer=self._snet_renderer, gq_card_builder=self._gq_card_builder,
                intensity_img_renderer=self._intensity_img_renderer,
            )
            self._push_svc = push_svc
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
                session_manager=self.session_config_manager,
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

            # ── CEA EEW Plan B（FAN 失能时启用） ──
            self.plan_b = CencEewPlanB(
                config=dict(self.config),
                database=self.database,
                pipeline=self.pipeline,
                signal_bus=self.signal_bus,
            )
            # 根据配置开关自动启停
            ds = dict(self.config).get("data_sources", {})
            dh = ds.get("direct_http", {}) if isinstance(ds, dict) else {}
            pb_enabled = dh.get("cenc_eew", False) or dh.get("cenc_eew_province", False) or dh.get("cenc_report", False)
            if pb_enabled:
                asyncio.create_task(self.plan_b.start())

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
        if hasattr(self, 'plan_b') and self.plan_b:
            await self.plan_b.stop()
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
                min_shindo = float(sf.get("min_shindo", 0.5))
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
        """台风推送适配器 — 渲染路径图后送入 pipeline。

        注意：自动推送走 present_typhoon_push()（‖ 前缀格式），
        与 /台风 查询用的 present_typhoon()（_field 排版）不同。
        mismatch 是预期行为——自动推送信息密度更高（含时间/移向/预报点）。
        """
        if self._in_silence_period():
            return
        if not self.pipeline:
            return

        # 渲染台风路径图
        img_b64 = None
        try:
            from .domain.models import TyphoonEvent
        except ImportError:
            from domain.models import TyphoonEvent
        if isinstance(envelope.event, TyphoonEvent) and self._typhoon_renderer:
            try:
                img_path = os.path.join(
                    self._temp_dir,
                    f"typhoon_push_{envelope.event.code}_{int(__import__('time').time())}.png",
                )
                result = await self._typhoon_renderer.render(envelope.event, img_path)
                if result and os.path.exists(result):
                    with open(result, "rb") as f:
                        img_b64 = base64.b64encode(f.read()).decode()
                    try:
                        os.unlink(result)
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"[台风] 推送图渲染异常: {e}")

        # 把图片塞进 envelope metadata
        if img_b64:
            envelope = EventEnvelope(
                identity=envelope.identity,
                event=envelope.event,
                received_at=envelope.received_at,
                payload=envelope.payload,
                metadata={**envelope.metadata, "_typhoon_image": img_b64},
            )

        try:
            await self.pipeline.handle(envelope)
        except Exception as e:
            logger.error(f"[台风] pipeline 处理失败: {e}")

    async def _ws_message_handler(self, name: str, raw_data: str | bytes) -> None:
        """WebSocket 消息处理器 — 组级→源级路由。"""
        # 统一转文本尝试 JSON 解析
        text = raw_data.decode("utf-8", errors="replace") if isinstance(raw_data, bytes) else raw_data
        data_for_check = None
        try:
            data_for_check = json.loads(text)
        except Exception:
            pass

        # 日志：WS 消息摘要（GQ 二进制源不逐条打日志）
        extra = ""
        if isinstance(data_for_check, dict):
            extra = f" type={data_for_check.get('type','?')}"
            if "source" in data_for_check:
                extra += f" source={data_for_check['source']}"
        if name != "global_quake":
            logger.info(f"[WS] ← {name}{extra}")

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
        elif name == "p2p":
            await self._route_p2p(data)
        else:
            await self.signal_bus.emit(name, data)

    def _match_fan_by_signature(self, payload: dict) -> str | None:
        """按 payload 特征签名匹配 FAN Studio 源（含排除条件）。"""
        if not isinstance(payload, dict):
            return None
        pkeys = set(payload.keys())
        for source_name, sid in self._fan_source_map.items():
            entry = self._sources.get(sid) if hasattr(self, '_sources') else None
            if not isinstance(entry, dict):
                continue

            # 排除条件：payload 包含某组中任一字段 → 跳过该源
            exclusions = entry.get("payload_exclusions", [])
            excluded = False
            for excl_group in exclusions:
                if any(f in pkeys for f in excl_group):
                    excluded = True
                    break
            if excluded:
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

        if msg_type == "initial_all":
            # initial_all 是 WebSocket 重连后的全量快照
            # 直接解析入库，不走 pipeline（避免启动时全量推送）
            stored = 0
            for key, sid in self._fan_source_map.items():
                if sid == "china_weather_fanstudio":
                    continue
                source_data = data.get(key)
                if isinstance(source_data, dict):
                    # initial_all 包了 Data 层，需解包（FAN: {"Data": {...}, "md5": "..."}）
                    inner = source_data.get("Data") or source_data.get("data") or source_data
                    if not isinstance(inner, dict):
                        continue
                    parser = ParserRegistry.get(sid)
                    if parser:
                        try:
                            result = parser.parse_message(inner)
                            if result:
                                for env in (result if isinstance(result, list) else [result]):
                                    if self.database:
                                        await self.database.insert_envelope(env)
                                    stored += 1
                        except Exception:
                            pass
            logger.info(f"[Fan] initial_all 入库: {stored} 条（静默期不推送）")
            return

        # 心跳静默（不打日志，刷屏）
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
                        return
                    logger.info(f"[Fan] ← {sid}")
                    await self.signal_bus.emit(sid, payload)
                    return

            # 2. 按 payload 签名兜底（处理同族模糊源）
            sid = self._match_fan_by_signature(payload)
            if sid:
                logger.info(f"[Fan] ← {sid}（签名匹配）")
                await self.signal_bus.emit(sid, payload)
                return

            logger.info(f"[Fan] update: 未匹配 source={source_name}")

        elif msg_type in ("heartbeat", "ping", "pong"):
            pass  # 心跳静默

        elif msg_type == "query_response":
            pass

        else:
            logger.info(f"[Fan] 未知消息类型: {msg_type}")

    async def _route_p2p(self, data: dict) -> None:
        """P2P 地震情報消息内部路由 — 按 code 字段匹配源。

        P2P 协议 codes:
          556 = 緊急地震速報 (EEW) → jma_p2p
          551 = 地震情報 (Report)  → jma_p2p_info
          552 = 津波予報 (Tsunami) → jma_tsunami_p2p
        """
        code = data.get("code")
        p2p_code_map: dict[int, str] = {
            556: "jma_p2p",
            551: "jma_p2p_info",
            552: "jma_tsunami_p2p",
        }
        sid = p2p_code_map.get(code)
        if sid:
            await self.signal_bus.emit(sid, data)
        else:
            logger.info(f"[P2P] 未匹配 code={code}")

    async def _route_wolfx(self, data: dict) -> None:
        """Wolfx 消息内部路由 — 按 type 字段匹配源。"""
        msg_type = data.get("type", "unknown")
        if msg_type in ("heartbeat", "pong"):
            return  # 心跳静默

        sid = self._wolfx_source_map.get(msg_type)
        if sid:
            if sid in ("jma_wolfx_info",) and msg_type in ("jma_report", "jma_info"):
                logger.info(f"[Wolfx] JMA 地震情报路由 → {sid} (type={msg_type})")
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
            if handler == "http_poll":
                continue  # HTTP 轮询源不走 WS 连接
            if handler == "p2p":
                continue  # P2P WS 已废弃（429 Too Many Requests），保留 HTTP 备用
            if handler and url:
                key = entry.get("connection_group", handler)
                if key not in groups:
                    groups[key] = {"url": url, "backup": entry.get("connection_backup_url", "")}
        for name, cfg in groups.items():
            self.ws_manager.add_connection(name, cfg["url"], cfg.get("backup", ""))

    async def _handle_http_poll_result(self, source_id: str, raw_data: Any) -> None:
        """HTTP 轮询结果处理：追踪所有 event_id，首次入库不推，新事件再推。"""
        import re
        from datetime import datetime, timezone
        try:
            from .parser.registry import ParserRegistry
        except ImportError:
            from parser.registry import ParserRegistry

        parser = ParserRegistry.get(source_id)
        if parser is None:
            logger.warning(f"[HTTP] {source_id} 无注册解析器")
            return

        result = parser.parse_message(raw_data)
        if not result:
            logger.info(f"[HTTP] {source_id} 解析器无返回（数据格式不匹配/空）")
            return

        envelopes = result if isinstance(result, list) else [result]
        if not envelopes:
            logger.info(f"[HTTP] {source_id} 解析结果为空列表")
            return

        # 该源所有见过的唯一键（event_id|report_num，EEW 报次更新不拦截）
        seen_key = f"{source_id}:seen"
        seen: set = self._http_last_event.get(seen_key)
        is_first = seen is None
        if seen is None:
            seen = set()
            self._http_last_event[seen_key] = seen

        new_envs = []
        for env in envelopes:
            uid = env.identity.unique_key
            if uid not in seen:
                seen.add(uid)
                new_envs.append(env)

        if not new_envs:
            return

        if is_first:
            stored = 0
            if self.database:
                for env in new_envs:
                    try:
                        await self.database.insert_envelope(env)
                        stored += 1
                    except Exception:
                        pass
            logger.info(f"[HTTP] << {source_id}: {len(new_envs)} 条入库{'（首条不推送）' if stored else '（无DB）'}")
        else:
            pushed = 0
            for env in new_envs:
                ev = env.event
                mag = getattr(ev, "magnitude", None)
                place = getattr(ev, "place_name", None) or getattr(ev, "region", None) or ""
                occurred = getattr(ev, "occurred_at", None) or getattr(ev, "timestamp", None)
                time_s = occurred.strftime("%H:%M:%S") if occurred else "?"
                mag_s = f" M{mag:.1f}" if mag is not None else ""
                if self.pipeline:
                    try:
                        await self.pipeline.handle(env)
                        pushed += 1
                        logger.info(f"[HTTP] << {source_id}: {time_s}{mag_s} {place}".strip())
                    except Exception as ex:
                        logger.error(f"[HTTP] {source_id} {env.identity.event_id} pipeline.handle 失败: {ex}")
                        import traceback
                        logger.error(traceback.format_exc())
            if pushed:
                logger.info(f"[HTTP] << {source_id}: {pushed}/{len(new_envs)} 条推送")

        # NRCan md5 回退警告
        if source_id == "nrcan_http":
            for env in new_envs:
                eid = env.identity.event_id
                if len(eid) == 12 and not re.search(r"\d{4,}", eid):
                    logger.warning(f"[HTTP] nrcan_http event_id 疑似 md5 回退: {eid}")
                    break
    def _setup_http_pollers(self, sources: dict, router: MessageRouter):
        # (name, url, interval, raw_text, ssl)
        POLLERS = {
            "funvisis_http": ("http://www.funvisis.gob.ve/maravilla.json", 10, True),
            "cenais_http": ("https://www.cenais.gob.cu/lastquake/php/lastweek.php", 10, True),
            "geonet_http": ("https://api.geonet.org.nz/quake?MMI=-1", 10, False),
            "nrcan_http": ("https://www.earthquakescanada.nrcan.gc.ca/cache/earthquakes/canada-30.xml", 10, True),
            "tmd_http": ("https://earthquake.tmd.go.th/", 10, True),
            "phivolcs_http": ("https://earthquake.phivolcs.dost.gov.ph/", 10, True),
            "csnc_http": ("https://www.sismologia.cl/index.html", 10, True),
            "usgs_weekly": ("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_week.geojson", 10, False),
            # Wolfx HTTP EEW（按需轮询，查的时候即时抓）
            "jma_wolfx_http": ("https://api.wolfx.jp/jma_eew.json", 86400, False),
            "cwa_wolfx_http": ("https://api.wolfx.jp/cwa_eew.json", 86400, False),
            "kma_wolfx_http": ("https://api.wolfx.jp/kma_eew.json", 86400, False),
            "sc_wolfx_http": ("https://api.wolfx.jp/sc_eew.json", 86400, False),
            "fj_wolfx_http": ("https://api.wolfx.jp/fj_eew.json", 86400, False),
            "cq_wolfx_http": ("https://api.wolfx.jp/cq_eew.json", 86400, False),
            # Wolfx HTTP 地震情报（备用）
            "jma_wolfx_info_http": ("https://api.wolfx.jp/jma_eqlist.json", 86400, False),
            # P2P HTTP 备用（EEW警报主源 + 情报）
            "jma_p2p_http": ("https://api.p2pquake.net/v2/history?codes=556&limit=1", 1, False),
            "jma_p2p_info_http": ("https://api.p2pquake.net/v2/jma/quake?limit=5", 1, False),
            "jma_tsunami_p2p_http": ("https://api.p2pquake.net/v2/history?codes=552&limit=1", 60, False),
            # BMKG 印尼气象局地震报告
            "bmkg_http": ("https://data.bmkg.go.id/DataMKG/TEWS/gempadirasakan.json", 5, False),
        }
        for sid, (url, interval, raw_text) in POLLERS.items():
            if sid in sources:
                self.http_poll_manager.add_poller(
                    name=sid, url=url, interval=interval,
                    handler=self._handle_http_poll_result,
                    raw_text=raw_text,
                )
        # ICL（成都高新减灾研究所）— URL 仅在生产配置文件（AppData）中设置
        icl_url = ""
        try:
            import json as _json
            # 尝试多个路径：AppData 生产配置优先，dev 配置兜底
            _paths = [
                os.path.normpath(os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "..", "..", "config", "mix_astrbot_plugin_disaster_warning_config.json"
                )),
                os.path.normpath(os.path.join(
                    os.environ.get("LOCALAPPDATA", ""),
                    "AstrBot", "data", "config", "mix_astrbot_plugin_disaster_warning_config.json"
                )),
            ]
            for _p in _paths:
                if not _p or not os.path.exists(_p):
                    continue
                with open(_p, encoding="utf-8-sig") as _f:  # utf-8-sig 兼容 BOM
                    _d = _json.load(_f)
                    icl_url = _d.get("icl_api_url", "") or ""
                    if icl_url:
                        logger.info(f"[ICL] 读取 ICL URL 成功")
                        break
                    else:
                        logger.warning(f"[ICL] 配置中存在但为空: {_p}")
        except Exception as _e:
            logger.warning(f"[ICL] 读取配置失败: {_e}")
        if icl_url and "icl_http" in sources:
            self.http_poll_manager.add_poller(
                name="icl_http", url=icl_url, interval=2,
                handler=self._handle_http_poll_result,
                raw_text=True, ssl=False,
            )
            logger.info("[ICL] 已加载 ICL 轮询器")

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
            "jma_p2p", "jma_p2p_http", "jma_wolfx", "jma_wolfx_http", "sa_fanstudio",
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

    # ── EEW 状态文本生成（BAK get_eew_query_text 移植） ──

    async def _get_eew_status_text(self) -> str:
        """生成各机构 EEW 状态文本（BAK 版 get_eew_query_text）。"""
        from datetime import datetime
        now = datetime.now()
        active = []
        inactive = []
        nodata = []

        for ik, meta in _EEW_INSTITUTIONS.items():
            dn = meta.get("display_name", ik)
            an = meta.get("active_name", dn)

            best = None
            for sid in meta.get("source_ids", []):
                rows = await self._query_source_eew(sid, 1)
                if rows:
                    r = rows[0]
                    ts = r.get("time", "")
                    try:
                        t = datetime.strptime(ts[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
                        el = int((now - t).total_seconds())
                        if best is None or el < best["elapsed"]:
                            best = {"elapsed": el, "mag": r.get("magnitude"), "place": r.get("place_name") or (r.get("description") or "")[:20]}
                    except Exception:
                        pass

            if best is None:
                nodata.append(f"- {dn}：暂未收到过 EEW 数据")
                continue

            if best["elapsed"] <= 300:
                mag_t = f"{best['mag']:.1f}" if best["mag"] is not None else "?"
                active.append(f"[{an}] 当前正在发布地震预警：M {mag_t} {best['place'] or '未知地点'}")
            else:
                inactive.append((best["elapsed"], f"{_fmt_elapsed(best['elapsed'])} 无 {dn}"))


        inactive.sort(key=lambda x: x[0])
        lines = []
        if active:
            lines.extend(active)
        if inactive:
            if active:
                lines.append("")
            lines.extend(t for _, t in inactive)
        if not active and not inactive:
            lines.append("当前没有正在生效的地震预警")
        if nodata:
            if lines:
                lines.append("")
            lines.append("以下机构暂未收到过 EEW 数据：")
            lines.extend(nodata)
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
                # EEW 源（含 _wolfx 后缀不含 _info 的）查 eew 表，情报源查 earthquake 表
                is_eew = "_info" not in sid
                if is_eew:
                    rows = await self._query_source_eew(sid, 1)
                else:
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

    # ═══════════════════ 命令: 群组管理 ═══════════════════

    @filter.regex(r"^/灾害预警群组$")
    async def groups_list_cmd(self, event: AstrMessageEvent):
        """查看全部群组配置。"""
        if not await self._is_admin(event):
            yield event.plain_result("❌ 仅管理员")
            return
        if not self.session_config_manager:
            yield event.plain_result("❌ 群组配置管理器未就绪")
            return
        groups = self.session_config_manager.list_groups()
        if not groups:
            yield event.plain_result("📢 未配置任何群组\n在配置文件中添加 groups 即可启用多群推送")
            return
        lines = ["📢 群组列表"]
        for gid in groups:
            sessions = self.session_config_manager.get_group_sessions(gid)
            # 检查群组是否在睡眠模式列表中
            sleep_mode_groups = self.session_config_manager.global_config.get("sleep_mode_groups", []) or []
            sleep_mode = gid in sleep_mode_groups
            gf = self.session_config_manager.get_group_filters(gid)
            filter_count = sum(1 for v in gf.values() if isinstance(v, dict)) if isinstance(gf, dict) else 0
            sleep_icon = "🌙" if sleep_mode else "☀️"
            lines.append(f"  {sleep_icon} {gid}: {len(sessions)} 会话, {filter_count} 个阈值覆盖")
        yield event.plain_result("\n".join(lines))

    @filter.regex(r"^/灾害预警群组\s+(\S+)(?:\s|$)")
    async def groups_detail_cmd(self, event: AstrMessageEvent, group_id: str):
        """查看指定群组详情。"""
        if not await self._is_admin(event):
            yield event.plain_result("❌ 仅管理员")
            return
        if not self.session_config_manager:
            yield event.plain_result("❌ 群组配置管理器未就绪")
            return

        # 检查群组是否存在
        groups = self.session_config_manager.list_groups()
        if group_id not in groups:
            yield event.plain_result(f"❌ 未找到群组: {group_id}，可用: {', '.join(groups.keys())}")
            return

        # 合并 override 显示完整配置
        info = self.session_config_manager.format_group_info(group_id)
        yield event.plain_result(info)

    @filter.regex(r"^/灾害预警群组\s+(\S+)\s+set\s+(\S+)\s+([\d.]+)(?:\s|$)")
    async def groups_set_threshold_cmd(
        self, event: AstrMessageEvent,
        group_id: str, source_id: str, magnitude: str,
    ):
        """设置群组内某数据源的震级阈值。
        用法: /灾害预警群组 A set jma_fanstudio 6.0
        """
        if not await self._is_admin(event):
            yield event.plain_result("❌ 仅管理员")
            return
        if not self.session_config_manager:
            yield event.plain_result("❌ 群组配置管理器未就绪")
            return

        try:
            mag = float(magnitude)
        except ValueError:
            yield event.plain_result(f"❌ 无效震级: {magnitude}")
            return

        groups = self.session_config_manager.list_groups()
        if group_id not in groups:
            yield event.plain_result(f"❌ 未找到群组: {group_id}，可用: {', '.join(groups.keys())}")
            return

        self.session_config_manager.set_group_filter(
            group_id=group_id, source_id=source_id, min_magnitude=mag,
        )
        yield event.plain_result(
            f"✅ 群组 {group_id} 的 {source_id} 阈值已设为 M{mag}+"
        )

    @filter.regex(r"^/灾害预警群组\s+(\S+)\s+clear(?:\s+(\S+))?(?:\s|$)")
    async def groups_clear_threshold_cmd(
        self, event: AstrMessageEvent,
        group_id: str, source_id: str = None,
    ):
        """清除群组阈值覆盖。
        用法: /灾害预警群组 A clear              全部清除
              /灾害预警群组 A clear jma_fanstudio  仅清除该源
        """
        if not await self._is_admin(event):
            yield event.plain_result("❌ 仅管理员")
            return
        if not self.session_config_manager:
            yield event.plain_result("❌ 群组配置管理器未就绪")
            return

        groups = self.session_config_manager.list_groups()
        if group_id not in groups:
            yield event.plain_result(f"❌ 未找到群组: {group_id}")
            return

        self.session_config_manager.clear_group_filter(group_id, source_id)
        if source_id:
            yield event.plain_result(f"✅ 已清除群组 {group_id} 的 {source_id} 阈值覆盖")
        else:
            yield event.plain_result(f"✅ 已清除群组 {group_id} 的所有阈值覆盖")

    # ═══════════════════ 命令: 睡眠阈值（独立文件，绕过 AstrBot 配置 bug） ═══════════════════

    @filter.regex(r"^/灾害预警睡眠阈值\s+(\S+)\s+(\S+)\s+([\d.]+)(?:\s|$)")
    async def sleep_threshold_set_cmd(
        self, event: AstrMessageEvent,
        filter_id: str, field: str, value: str,
    ):
        """设置睡眠模式过滤阈值（存独立文件，不受 AstrBot 配置污染）。
        用法: /灾害预警睡眠阈值 global_quake_filter 最小烈度 8.0
        """
        if not await self._is_admin(event):
            yield event.plain_result("❌ 仅管理员")
            return

        try:
            val = float(value)
        except ValueError:
            yield event.plain_result(f"❌ 无效值: {value}")
            return

        # 读现有文件
        sf_data = {}
        if self._sleep_filters_path and self._sleep_filters_path.exists():
            try:
                with open(self._sleep_filters_path, encoding="utf-8") as _sf:
                    sf_data = json.load(_sf)
            except Exception:
                sf_data = {}
        if not isinstance(sf_data, dict):
            sf_data = {}

        # 写入
        if filter_id not in sf_data or not isinstance(sf_data[filter_id], dict):
            sf_data[filter_id] = {}
        sf_data[filter_id][field] = val

        # 存回文件
        try:
            self._sleep_filters_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._sleep_filters_path, "w", encoding="utf-8") as _sf:
                json.dump(sf_data, _sf, ensure_ascii=False, indent=2)
        except Exception as e:
            yield event.plain_result(f"❌ 写入失败: {e}")
            return

        # 覆盖运行时 config 并重建 SessionConfigManager
        self.config["sleep_earthquake_filters"] = sf_data
        self.session_config_manager = SessionConfigManager(dict(self.config))
        logger.info(f"[Mix] 睡眠阈值已更新: {filter_id}.{field}={val}")

        yield event.plain_result(
            f"✅ 睡眠阈值已设置: {filter_id}.{field}={val}\n"
            f"📁 独立文件: {self._sleep_filters_path}"
        )

    # ═══════════════════ 命令: 查询 ═══════════════════

    @filter.regex(r"^/(?:地震列表查询|地震列表)(?:\s|$)")
    async def earthquake_list_cmd(
        self, event: AstrMessageEvent,
        source: str = "cenc", count: int = 9, mode: str = "text",
    ):
        # 解析 source 简写 → 完整 source_id（用共享映射）
        sid = _SHORT_SRC_MAP.get(source, source)
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
            **{k: v for k, v in _SHORT_SRC_MAP.items() if v not in ("snet",)},  # 共享映射（排除非 EEW 源）
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

    # ── /盼震 命令（BAK 版移植，多别名） ──

    @filter.regex(r"^/(?:[盼畔叛判拚潘攀盘磐蟠蹒槃鞶][震振镇阵圳朕鸩赈真针珍贞侦斟甄箴砧祯桢诊枕疹缜轸]|earth)(?:\s|$)")
    async def eew_pending_cmd(self, event: AstrMessageEvent):
        try:
            text = await self._get_eew_status_text()
            yield event.plain_result(f"正在盘阵中！\n{text}")
        except Exception as ex:
            logger.error(f"[盼震] 异常: {ex}", exc_info=True)
            yield event.plain_result(f"❌ 查询失败: {ex}")

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
        # fallback: EEW-only 源（如 sa_fanstudio）没有 earthquake 类型
        if not rows:
            rows = await self._query_source_eew(source_id, 1)
        # 查到的数据按时间排序（不是按入库顺序）
        if rows:
            rows.sort(key=lambda r: str(r.get("time", "") or ""), reverse=True)
        if not rows:
            # 对于 HTTP 轮询源，DB 无数据时立即触发一次抓取
            _HTTP_SOURCES = {
                "funvisis_http", "cenais_http", "geonet_http", "nrcan_http",
                "tmd_http", "phivolcs_http", "csnc_http", "usgs_weekly",
                "jma_wolfx_http", "jma_wolfx_info_http",
                "cwa_wolfx_http", "kma_wolfx_http",
                "sc_wolfx_http", "fj_wolfx_http", "cq_wolfx_http",
                "jma_p2p_http", "jma_p2p_info_http", "jma_tsunami_p2p_http",
		"bmkg_http", "icl_http",
            }
            if source_id in _HTTP_SOURCES and self.http_poll_manager:
                logger.info(f"[查询] {source_id} DB 无数据，触发即时抓取")
                try:
                    await self.http_poll_manager.fetch_one(source_id)
                except Exception as ex:
                    logger.warning(f"[查询] {source_id} 即时抓取失败: {ex}")
                # 抓取后重新查 DB
                rows = await self._query_source_events(source_id, 5)
                if not rows:
                    rows = await self._query_source_eew(source_id, 1)
        if not rows:
            yield event.plain_result(f"📡 {display} 暂无数据")
            return
        r = rows[0]

        from datetime import datetime
        raw_json = r.get("raw_json")
        raw = {}
        if raw_json:
            try:
                raw = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
            except Exception:
                raw = {}

        ts = r.get("time") or ""
        # 如果 time 列为空，尝试从 raw_json 中提取
        if not ts and raw:
            ts = str(raw.get("time") or raw.get("originTime") or raw.get("shockTime") or raw.get("tiempoutc") or "")
        occurred_at = None
        if ts:
            try:
                # DB 存的是 occurred_at.isoformat() 格式，如 "2026-06-24T15:30:00+00:00"
                # 优先用 fromisoformat 保留时区信息（否则 PHIVOLCS +08:00 等会在后面被误当 UTC 加倍偏移）
                try:
                    occurred_at = datetime.fromisoformat(ts)
                except (ValueError, TypeError):
                    # 降级：处理 "2026/06/06T22:55:16"（CENAIS 原始格式）等无时区格式
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
                        try:
                            occurred_at = datetime.strptime(ts[:19].replace("T", " "), fmt)
                            if occurred_at:
                                break
                        except ValueError:
                            continue
            except Exception:
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
            mmi_val = None
            if source_id == "bmkg_http":
                level_raw = r.get("level") or ""
                if level_raw:
                    try:
                        mmi_val = float(level_raw)
                    except (ValueError, TypeError):
                        pass
                if mmi_val is None:
                    from .parser.http_poll.parsers import _parse_max_mmi
                    dirasakan = str(raw.get("Dirasakan", "") or "")
                    if dirasakan:
                        mmi_val = _parse_max_mmi(dirasakan)
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
                mmi=mmi_val if source_id == "bmkg_http" else None,
                raw=raw,
            )
            text = present_earthquake_report(rep)

        # 震度/烈度预览图（地震报告 + GQ，非 JMA/CWA 源）
        intensity_b64 = []
        if self._intensity_img_renderer and source_id not in ("snet_http", "snet") and not source_id.startswith(("jma_", "cwa_")):
            is_eligible = (
                event_type not in ("earthquake_warning", "eew")
                or source_id == "global_quake"
            )
            if is_eligible:
                mag = r.get("magnitude")
                depth = r.get("depth")
                if mag is not None:
                    try:
                        if source_id == "bmkg_http" and mmi_val is not None:
                            s_path, _ = self._intensity_img_renderer.render_both(
                                mag, depth or 10.0,
                            )
                            i_path = self._intensity_img_renderer.render_intensity_actual(str(mmi_val), "最大烈度")
                        else:
                            s_path, i_path = self._intensity_img_renderer.render_both(
                                mag, depth or 10.0,
                            )
                        for p in (s_path, i_path):
                            if p and os.path.exists(p):
                                with open(p, "rb") as f:
                                    intensity_b64.append(base64.b64encode(f.read()).decode())
                    except Exception as ex:
                        logger.warning(f"[查询] {display} 烈度/震度图渲染异常: {ex}")
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
                    all_imgs = [Image.fromBase64(b) for b in intensity_b64] + [Image.fromBase64(b) for b in b64_list]
                    yield event.chain_result([Plain(text)] + all_imgs)
                    return
            except Exception as e:
                logger.warning(f"[查询] {display} 地图渲染异常: {e}")

        # 只有烈度/震度图（没有地图时）
        if intensity_b64:
            yield event.chain_result([Plain(text)] + [Image.fromBase64(b) for b in intensity_b64])
            return

        yield event.plain_result(text)

    @filter.regex(r"^/cenc(?:\s|$)")
    async def q_cenc(self, e):
        async for r in self._quick_query(e, "cenc_fanstudio", "中国地震台网"): yield r

    @filter.regex(r"^/cea(?:\s|$)")
    async def q_cea(self, e):
        """CEA 查询：/cea = 全国最新 /cea pr = 各省概览 /cea <省份> = 省详情。

        数据源为 Plan B HTTP 轮询（独立于 FAN Studio），FAN 通断均可查。
        """
        raw_text = e.message_str if hasattr(e, 'message_str') else str(e.message_obj)
        parts = raw_text.strip().split()
        args = parts[-1].lower() if len(parts) >= 2 else ""

        from datetime import datetime

        plan_b: CencEewPlanB | None = getattr(self, "plan_b", None)
        if plan_b is None:
            yield e.plain_result("❌ Plan B 未初始化")
            return

        # ── /cea pr → 各省概览（Nodes 聊天记录） ──
        if args == "pr":
            db_count = 0
            if self.database:
                try:
                    rows = await self.database.execute_raw(
                        "SELECT COUNT(*) as c FROM events WHERE source='cenc_eew_province'"
                    )
                    db_count = rows[0]["c"] if rows else 0
                except Exception:
                    pass
            now_str = datetime.now().strftime("%m-%d %H:%M")
            from astrbot.api.message_components import Node, Nodes
            bot_id = e.get_self_id() or "0"
            bot_name = "Mix灾害预警"
            nodes = Nodes([])
            header = (
                f"CEA-PR 中国地震预警网 省级融合源 最新数据\n"
                f"省份: {len(_CENC_PROVINCES)} | 入库: {db_count} | {now_str}"
            )
            nodes.nodes.append(Node(uin=bot_id, name=bot_name, content=[Plain(header)]))

            async with aiohttp.ClientSession() as session:

                async def _fetch_province(name: str, app_id: str) -> Node | None:
                    """单个省请求（并行用）。"""
                    short = _CENC_SHORT_NAME.get(app_id, name.replace("地震预警网", ""))
                    payload = {"app_id": app_id, "page_query": {"page_no": 1, "page_size": 10}}
                    try:
                        async with session.post(
                            f"{_CENC_BASE}/api/earthquake/event/v1/list",
                            json=payload, timeout=10
                        ) as resp:
                            if resp.status != 200:
                                return Node(uin=bot_id, name=bot_name, content=[Plain(f"✗ {short} HTTP{resp.status}")])
                            data = await resp.json()
                            if data.get("code") != 0:
                                return Node(uin=bot_id, name=bot_name, content=[Plain(f"✗ {short} {data.get('msg','?')}")])
                            infos = (data.get("data") or {}).get("spot_infos", [])
                            if not infos:
                                return Node(uin=bot_id, name=bot_name, content=[Plain(f"- {short} 无数据")])
                            eq = infos[0]
                            ts = datetime.fromtimestamp(eq.get("created_at", 0) / 1000).strftime("%m-%d %H:%M") if eq.get("created_at") else "?"
                            mag = eq.get("level", "?")
                            dep = eq.get("depth", "?")
                            loc = (eq.get("location") or "?")[:16]
                            epi = eq.get("epicenter_intensity")
                            epi_s = f"烈度{epi}" if epi is not None else "烈度不明"
                            serial = eq.get("serial_number")
                            serial_s = f" 第{serial}报" if serial else ""
                            lat, lon = eq.get("latitude"), eq.get("longitude")
                            lat_s = f"{abs(lat):.3f}{'N' if lat is not None and lat >= 0 else 'S'}" if lat is not None else "?"
                            lon_s = f"{abs(lon):.3f}{'E' if lon is not None and lon >= 0 else 'W'}" if lon is not None else "?"
                            eid = str(eq.get("id", "?"))[-12:]
                            node_text = (
                                f"{short}地震预警网{serial_s}\n"
                                f"时间: {ts} 震中: {loc}\n"
                                f"震级: M{mag} 深度: {dep}km\n"
                                f"经纬度: {lon_s} {lat_s} | {epi_s}\n"
                                f"ID: {eid}"
                            )
                            return Node(uin=bot_id, name=bot_name, content=[Plain(node_text)])
                    except Exception as ex:
                        return Node(uin=bot_id, name=bot_name, content=[Plain(f"✗ {short} {str(ex)[:30]}")])

                tasks = [_fetch_province(n, a) for n, a in _CENC_PROVINCES]
                results = await asyncio.gather(*tasks)
                for node in results:
                    if node:
                        nodes.nodes.append(node)
            yield e.chain_result([nodes])
            return

        # ── /cea <省名> → 实时抓取 + present_eew + 双图 ──
        if args and args != "cea":
            matched_app_id = None
            matched_name = None
            for name, app_id in _CENC_PROVINCES:
                if args in name or args in _CENC_SHORT_NAME.get(app_id, ""):
                    matched_app_id = app_id
                    matched_name = _CENC_SHORT_NAME.get(app_id, name)
                    break
            if not matched_app_id:
                yield e.plain_result(f"❌ 未找到省份: {args}")
                return

            url = f"{_CENC_BASE}/api/earthquake/event/v1/list"
            payload = {"app_id": matched_app_id, "page_query": {"page_no": 1, "page_size": 10}}
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, timeout=10) as resp:
                        if resp.status != 200:
                            yield e.plain_result(f"❌ {matched_name} HTTP {resp.status}")
                            return
                        data = await resp.json()
            except Exception as ex:
                yield e.plain_result(f"❌ {matched_name} 请求失败: {ex}")
                return

            parser = ParserRegistry.get("cenc_eew_province")
            result = parser.parse_message(data) if parser else None
            if not result:
                yield e.plain_result(f"📡 {matched_name} 暂无 EEW 数据")
                return
            envelopes = result if isinstance(result, list) else [result]
            if not envelopes:
                yield e.plain_result(f"📡 {matched_name} 暂无 EEW 数据")
                return
            ev = envelopes[0].event

            text = present_eew(ev)
            province_display = _CENC_SHORT_NAME.get(matched_app_id, matched_name)
            if province_display:
                import re as _re
                lines = text.split("\n")
                lines[0] = _re.sub(r"(中国地震预警网省级融合源)(\s)", rf"\1({province_display})\2", lines[0])
                text = "\n".join(lines)

            yield e.plain_result(text)
            imgs = _render_intensity_images(self._intensity_img_renderer, ev.magnitude, ev.depth)
            for img in imgs:
                yield e.image_result(img)
            return

        # ── /cea → 查全国源最新 EEW（present_eew + 烈度震度双图） ──
        rows = await plan_b.query_national(5)
        if not rows:
            yield e.plain_result("📡 正在首次抓取...")
            await plan_b.poll_national_once()
            rows = await plan_b.query_national(5)
        if not rows:
            yield e.plain_result("📡 暂无 CEA 数据")
            return
        r = rows[0]
        ts = r.get("time") or ""
        occurred_at = None
        if ts:
            try:
                occurred_at = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                try:
                    occurred_at = datetime.strptime(ts[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError):
                    pass
        eew = EewEvent(
            source_id="cenc_eew_http",
            event_id=r.get("real_event_id", "") or str(r.get("id", "")),
            occurred_at=occurred_at,
            latitude=r.get("latitude"),
            longitude=r.get("longitude"),
            depth=r.get("depth"),
            magnitude=r.get("magnitude"),
            place_name=r.get("place_name") or "",
            report_num=r.get("report_num", 0) or 0,
            raw=r.get("raw", {}),
        )
        text = present_eew(eew)
        yield e.plain_result(text)
        imgs = _render_intensity_images(self._intensity_img_renderer, eew.magnitude, eew.depth)
        for img in imgs:
            yield e.image_result(img)

    @filter.regex(r"^/jma(?:\s|$)")
    async def q_jma(self, e):
        """JMA 查询 — /jma = P2P JMA地震情报, /jma all = 综合概览。"""
        raw_text = e.message_str if hasattr(e, 'message_str') else str(e.message_obj)
        parts = raw_text.strip().split()
        args = parts[-1] if len(parts) >= 2 else ""

        if args in ("all", "综合", "alll"):
            # /jma all → 综合概览
            async for r in self._jma_overview(e):
                yield r
            return
        # /jma → 直接调 P2P 551 API 拿最新日本地震情报
        async for r in self._jma_live_query(e):
            yield r
        return

    async def _jma_overview(self, e):
        """JMA 综合概览 — EEW + 地震情报 + 海啸。"""
        lines = ["📡 JMA 日本气象厅综合"]
        lines.append(_SEPARATOR)

        # ── JMA EEW 源 ──
        eew_sources = [
            ("jma_fanstudio", "FAN Studio"),
            ("jma_p2p", "P2P WS"),
            ("jma_p2p_http", "P2P HTTP"),
            ("jma_wolfx", "Wolfx WS"),
            ("jma_wolfx_http", "Wolfx HTTP"),
        ]
        for sid, label in eew_sources:
            rows = await self._query_source_eew(sid, 1)
            if rows:
                r = rows[0]
                t = _fmt_time_short(r.get("time", ""))
                mag = r.get("magnitude", "?")
                place = r.get("place_name", "") or r.get("description", "")[:20]
                lines.append(f"  ✅ EEW({label}) M{mag} {place} ({t})")
            else:
                lines.append(f"  ⏳ EEW({label}) 暂无数据")

        lines.append(_SEPARATOR)

        # ── JMA 地震情报源 ──
        info_sources = [
            ("jma_p2p_info", "P2P WS"),
            ("jma_p2p_info_http", "P2P HTTP"),
            ("jma_wolfx_info", "Wolfx WS"),
            ("jma_wolfx_info_http", "Wolfx HTTP"),
        ]
        for sid, label in info_sources:
            rows = await self._query_source_events(sid, 1)
            if rows:
                r = rows[0]
                t = _fmt_time_short(r.get("time", ""))
                mag = r.get("magnitude", "?")
                place = r.get("place_name", "") or r.get("description", "")[:20]
                lines.append(f"  ✅ 情报({label}) M{mag} {place} ({t})")
            else:
                lines.append(f"  ⏳ 情报({label}) 暂无数据")

        # ── JMA 海啸 ──
        tsunami_sources = [("jma_tsunami_p2p", "P2P WS"), ("jma_tsunami_p2p_http", "P2P HTTP")]
        for sid, label in tsunami_sources:
            rows = await self._query_source_events(sid, 1)
            if rows:
                r = rows[0]
                t = _fmt_time_short(r.get("time", ""))
                title = r.get("description", r.get("subtitle", ""))[:30]
                lines.append(f"  🌊 海啸({label}) {title} ({t})")
            else:
                lines.append(f"  ⏳ 海啸({label}) 暂无数据")

        lines.append(_SEPARATOR)
        yield e.plain_result("\n".join(lines))

    async def _jma_live_query(self, e):
        """直接调 P2P 551 API，跳过 Foreign（遠地地震）。"""
        import aiohttp
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.get("https://api.p2pquake.net/v2/jma/quake?limit=5") as resp:
                    if resp.status != 200:
                        yield e.plain_result(f"❌ P2P API {resp.status}")
                        return
                    data = await resp.json()
        except Exception as ex:
            yield e.plain_result(f"❌ 请求失败: {ex}")
            return

        if not isinstance(data, list) or not data:
            yield e.plain_result("📡 暂无 JMA 地震情报")
            return

        parser = ParserRegistry.get("jma_p2p_info_http")
        if not parser:
            yield e.plain_result("❌ 解析器未注册")
            return

        envelopes = parser.parse_message(data) or []
        if not envelopes:
            yield e.plain_result("📡 暂无 JMA 地震情报")
            return

        # 跳过 Foreign（遠地地震）
        target = None
        for env in envelopes:
            raw = env.event.raw if isinstance(env.event.raw, dict) else {}
            issue = raw.get("issue", {}) or {}
            if isinstance(issue, dict) and issue.get("type") == "Foreign":
                continue
            target = env
            break

        if target is None:
            yield e.plain_result("📡 暂无日本本地 JMA 地震情报")
            return

        # 展示文本
        from .message.presenters import present
        text = present(target)
        if not text:
            yield e.plain_result("📡 暂无 JMA 地震情报")
            return

        # 图片
        from astrbot.api.message_components import Image
        chain = [Plain(text)]

        def _img(path):
            if path and os.path.exists(path):
                with open(path, "rb") as f:
                    chain.append(Image.fromBase64(base64.b64encode(f.read()).decode()))

        ev = target.event
        # 烈度/震度图：优先用实际最大震度
        if self._intensity_img_renderer:
            actual_shindo = None
            if hasattr(ev, 'mmi') and ev.mmi is not None:
                actual_shindo = ev.mmi
            elif hasattr(ev, 'intensity_points') and ev.intensity_points:
                max_s = max(
                    (p.get("scale") for p in ev.intensity_points
                     if isinstance(p, dict) and p.get("scale") is not None),
                    default=None,
                )
                if max_s is not None:
                    actual_shindo = max_s
            if actual_shindo is not None:
                from .message.presenters import _shindo_label_str
                _img(self._intensity_img_renderer.render_shindo_actual(
                    _shindo_label_str(actual_shindo), "最大震度"))
            elif ev.magnitude is not None:
                for p in self._intensity_img_renderer.render_both(ev.magnitude, ev.depth):
                    _img(p)
            else:
                _img(self._intensity_img_renderer.render_shindo_actual("不明", "最大震度"))

        # NHK 双图
        if self._push_svc:
            try:
                nhk_b64 = await self._push_svc._fetch_nhk_report_images(target)
                if nhk_b64:
                    for b64 in nhk_b64:
                        chain.append(Image.fromBase64(b64))
                else:
                    # NHK 失败 → PetalMap 双图
                    if self._map_builder and ev.latitude is not None:
                        msg_fmt = dict(self.config.get("message_format", {}))
                        for zoom in (4, 8):
                            try:
                                msg_fmt["map_zoom_level"] = zoom
                                path = await self._map_builder.render_map_image(
                                    ev.latitude, ev.longitude, msg_fmt)
                                _img(path)
                            except Exception as ex:
                                logger.warning(f"[JMA] 地图 zoom={zoom} 渲染失败: {ex}")
            except Exception as ex:
                logger.warning(f"[JMA] NHK 图异常: {ex}")
        else:
            # push_svc 未就绪，直接 PetalMap
            if self._map_builder and ev.latitude is not None:
                msg_fmt = dict(self.config.get("message_format", {}))
                for zoom in (4, 8):
                    try:
                        msg_fmt["map_zoom_level"] = zoom
                        path = await self._map_builder.render_map_image(
                            ev.latitude, ev.longitude, msg_fmt)
                        _img(path)
                    except Exception as ex:
                        logger.warning(f"[JMA] 地图 zoom={zoom} 渲染失败: {ex}")

        yield e.chain_result(chain)

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

    @filter.regex(r"^/usp(?:\s|$)")
    async def q_usp(self, e):
        async for r in self._quick_query(e, "usp_fanstudio", "USP"): yield r

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

    @filter.regex(r"^/(?:gq|globalquake)(?:\s|$)")
    async def q_gq(self, e):
        async for r in self._quick_query(e, "global_quake", "GlobalQuake"): yield r

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

    @filter.regex(r"^/(?:四川|sc)(?:\s|$)")
    async def q_sc(self, e):
        async for r in self._quick_query(e, "sc_wolfx_http", "四川地震预警"): yield r

    @filter.regex(r"^/(?:福建|fj)(?:\s|$)")
    async def q_fj(self, e):
        async for r in self._quick_query(e, "fj_wolfx_http", "福建地震预警"): yield r

    @filter.regex(r"^/(?:重庆|cq)(?:\s|$)")
    async def q_cq(self, e):
        async for r in self._quick_query(e, "cq_wolfx_http", "重庆地震预警"): yield r

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

    @filter.regex(r"^/tmd(?:\s|$)")
    async def q_tmd(self, e):
        async for r in self._quick_query(e, "tmd_http", "TMD"): yield r

    @filter.regex(r"^/csnc(?:\s|$)")
    async def q_csnc(self, e):
        async for r in self._quick_query(e, "csnc_http", "CSNC"): yield r

    @filter.regex(r"^/funvisis(?:\s|$)")
    async def q_funvisis(self, e):
        async for r in self._quick_query(e, "funvisis_http", "FUNVISIS"): yield r

    @filter.regex(r"^/wnrl(?:\s|$)")
    async def q_wnrl(self, e):
        async for r in self._quick_query(e, "funvisis_http", "FUNVISIS"): yield r

    @filter.regex(r"^/bmkg(?:\s|$)")
    async def q_bmkg(self, e):
        async for r in self._quick_query(e, "bmkg_http", "BMKG"): yield r

    @filter.regex(r"^/icl(?:\s|$)")
    async def q_icl(self, e):
        """ICL 成都高新减灾研究所 — 查询最新地震预警。"""
        async for r in self._quick_query(e, "icl_http", "ICL"): yield r

    @filter.regex(r"^/556(?:\s|$)")
    async def q_556(self, event: AstrMessageEvent):
        """抓取 JMA 紧急地震速报（556）并推送。"""
        try:
            import aiohttp
            import os, base64
            url = "https://api.p2pquake.net/v2/history?codes=556&limit=1"
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.get(url) as resp:
                    if resp.status != 200:
                        yield event.plain_result(f"❌ 556 API {resp.status}")
                        return
                    data = await resp.json()
            try:
                from .parser.registry import ParserRegistry
            except ImportError:
                from parser.registry import ParserRegistry
            parser = ParserRegistry.get("jma_p2p_http")
            if not parser:
                yield event.plain_result("❌ 556解析器未注册")
                return
            envelopes = parser.parse_message(data) or []
            if not envelopes:
                yield event.plain_result("📡 当前无556警报")
                return
            env = envelopes[0]
            from .message.presenters import present
            text = present(env)
            # 横幅图
            banner = os.path.join(
                os.path.dirname(__file__), "resources", "images", "jma_eew_banner.jpg"
            )
            if os.path.exists(banner):
                with open(banner, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                yield event.chain_result([Plain(text), Image.fromBase64(b64)])
            else:
                yield event.plain_result(text)
        except Exception as ex:
            yield event.plain_result(f"❌ 556抓取失败: {ex}")

    @filter.regex(r"^/NHK(?:\s|$)")
    async def q_nhk(self, event: AstrMessageEvent):
        """查询最新 JMA 551 事件的 NHK 双图（调试用）。"""
        if not self.database:
            yield event.plain_result("❌ 数据库未就绪")
            return

        # 查询最新 JMA 551 事件
        rows = await self._query_source_events("jma_p2p_info", 1, 1)
        if not rows:
            rows = await self._query_source_events("jma_p2p_info_http", 1, 1)
        if not rows:
            yield event.plain_result("📡 暂无 JMA 551 事件记录")
            return

        row = rows[0]
        import json
        raw_raw = row.get("raw_json") or "{}"
        if isinstance(raw_raw, str):
            try:
                raw = json.loads(raw_raw)
            except json.JSONDecodeError:
                raw = {}
        elif isinstance(raw_raw, dict):
            raw = raw_raw
        else:
            raw = {}

        if not raw:
            yield event.plain_result("❌ 事件无原始数据")
            return

        from .domain.models import EarthquakeReport
        ev = EarthquakeReport(
            source_id=row.get("source", "jma_p2p_info"),
            event_id=row.get("real_event_id", ""),
            occurred_at=None,
            latitude=row.get("latitude"),
            longitude=row.get("longitude"),
            magnitude=row.get("magnitude"),
            depth=row.get("depth"),
            place_name=row.get("place_name", ""),
            raw=raw,
        )
        from .domain.models import EventEnvelope, EventIdentity, SourcePayload
        env = EventEnvelope(
            identity=EventIdentity(
                event_id=ev.event_id, source_id=ev.source_id, event_type="earthquake",
            ),
            event=ev,
            payload=SourcePayload(source_id=ev.source_id, raw=raw),
        )

        # 提取调试信息
        eq_t = (raw.get("earthquake", {}) or {}).get("time", "N/A")
        iss_t = (raw.get("issue", {}) or {}).get("time", "N/A")
        iss_type = (raw.get("issue", {}) or {}).get("type", "N/A")
        mag = row.get("magnitude", "?")
        place = row.get("place_name", "") or row.get("region", "") or "?"

        lines = [
            f"🔍 NHK 图调试 — M{mag} {place}",
            f"  ├ earthquake.time: {eq_t}",
            f"  ├ issue.time:      {iss_t}",
            f"  ├ issue.type:      {iss_type}",
        ]

        if not hasattr(self, '_push_svc') or not self._push_svc:
            lines.append("  └ ❌ PushExecutionService 未就绪")
            yield event.plain_result("\n".join(lines))
            return

        from astrbot.api.message_components import Image, Plain
        try:
            b64_list = await self._push_svc._fetch_nhk_report_images(env)
            if b64_list and len(b64_list) == 2:
                import base64
                chain = [Plain("\n".join(lines) + "\n  └ ✅ NHK 双图获取成功")]
                for b64 in b64_list:
                    chain.append(Image.fromBase64(b64))
                yield event.chain_result(chain)
            elif b64_list:
                lines.append(f"  └ ⚠️ 仅获取 {len(b64_list)}/2 张图")
                yield event.plain_result("\n".join(lines))
            else:
                lines.append("  └ ❌ NHK 图未找到（15s 扫描无命中）")
                yield event.plain_result("\n".join(lines))
        except Exception as e:
            lines.append(f"  └ ❌ 异常: {e}")
            yield event.plain_result("\n".join(lines))

    @filter.regex(r"^/httpstatus(?:\s|$)")
    async def http_status_cmd(self, event: AstrMessageEvent):
        """HTTP 轮询源状态一览。"""
        lines = ["📡 HTTP 轮询源状态"]

        if not self.http_poll_manager:
            yield event.plain_result("❌ HTTP 轮询管理器未就绪")
            return

        pollers = self.http_poll_manager.get_status()
        if not pollers:
            yield event.plain_result("📡 没有注册的 HTTP 轮询器")
            return

        # 显示名映射（仅 HTTP 轮询源）
        display_map = {
            "funvisis_http": "FUNVISIS", "cenais_http": "CENAIS",
            "geonet_http": "GeoNet", "nrcan_http": "NRCan",
            "tmd_http": "TMD", "phivolcs_http": "PHIVOLCS",
            "csnc_http": "CSNC", "usgs_weekly": "USGS周报",
	    "bmkg_http": "BMKG",
            "jma_wolfx_http": "JMA(Wolfx HTTP)", "jma_wolfx_info_http": "JMA情报(Wolfx HTTP)",
            "jma_p2p_http": "JMA(P2P HTTP)", "jma_p2p_info_http": "JMA情报(P2P HTTP)",
            "jma_tsunami_p2p_http": "JMA海啸(P2P HTTP)",
        }

        # 哪些源有成功返回数据
        last_events = self._http_last_event  # {source_id: {event_id: ...}}
        for name, info in pollers.items():
            display = display_map.get(name, name)
            running = info["running"]
            interval = info["interval"]
            has_data = "✅" if name in last_events else "⏳"
            status = "🟢 运行中" if running else "🔴 已停止"
            lines.append(f"  {has_data} {display} {status} ({interval}s)")
            if name in last_events:
                last_eid = last_events[name].get("event_id", "?")
                lines.append(f"     最后事件: {last_eid}")

        # WS → HTTP 备用关系提示
        lines.append("")
        lines.append("注: JMA/P2P/Wolfx HTTP 是 WebSocket 备用源，WS 正常时可能无数据")
        yield event.plain_result("\n".join(lines))

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

        # 从配置读取用户设置的 min_shindo 阈值，替代硬编码 >= 0
        _qms = 0.0
        try:
            _qf = self.config.get("earthquake_filters", {}).get("snet_filter", {})
            if isinstance(_qf, dict):
                _qms = float(_qf.get("min_shindo", 0.0))
        except Exception:
            pass
        triggered = [s for s in stations if s["shindo"] >= _qms]
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

    # ═══════════════════ /nan shen 恶搞指令 ═══════════════════

    @filter.regex(r"^/(?:[男南难楠喃赧腩蝻囡][申伸身深呻绅砷莘神什审婶沈谂甚肾慎渗蜃生声升牲笙甥绳省胜圣盛剩])(?:\s|$)")
    async def q_nan_shen(self, e: AstrMessageEvent):
        """/nan shen（及同音字）— 恶搞指令：SNET 震度7 + 全屏地震预警"""
        import io, base64, random
        from datetime import datetime, timezone
        from PIL import Image as PILImage

        # 获取触发者名字
        try:
            sender_name = e.get_sender_name() or "未知用户"
        except Exception:
            sender_name = "未知用户"

        # ── 1. 执行 S-Net 震度 7 ──
        try:
            from .parser.snet import SNET_REAL_COORDS
            from .message.render.snet_map_renderer import MSIL_SHINDO_TO_RGB
        except ImportError:
            from parser.snet import SNET_REAL_COORDS
            from message.render.snet_map_renderer import MSIL_SHINDO_TO_RGB

        val = 7.0
        key = round(val * 10)
        rgb = MSIL_SHINDO_TO_RGB.get(key, (63, 250, 54))
        stations = [
            {"name": nm, "lat": lat, "lon": lon, "shindo": val, "rgb": rgb}
            for nm, (lat, lon) in SNET_REAL_COORDS.items()
        ]
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d%H%M00")

        # 渲染 SNET 图
        if self._snet_renderer:
            try:
                img_path = os.path.join(
                    self._temp_dir,
                    f"nanshen_{int(__import__('time').time())}.png",
                )
                out = await self._snet_renderer.render(stations, img_path, ts_str)
                if out and os.path.exists(out):
                    with open(out, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode()
                    try:
                        os.unlink(out)
                    except Exception:
                        pass
                    yield e.chain_result([
                        Image.fromBase64(b64),
                        Plain(f"{sender_name} 正在发布地震预警！M9.0!滚木！"),
                    ])
                    return
            except Exception as ex:
                logger.warning(f"[nan shen] 渲染异常: {ex}")

        # 渲染失败时纯文本兜底
        yield e.plain_result(f"{sender_name} 正在发布地震预警！M9.0!滚木！")

    # ── BAK 版迁移的缺失快捷指令 ──

    @filter.regex(r"^/kma_eew(?:\s|$)")
    async def q_kma_eew(self, e):
        async for r in self._quick_query(e, "kma_eew_fanstudio", "KMA EEW"): yield r

    @filter.regex(r"^/xxl(?:\s|$)")
    async def q_xxl(self, e):
        async for r in self._quick_query(e, "geonet_http", "GeoNet"): yield r

    @filter.regex(r"^/cnsc(?:\s|$)")
    async def q_cnsc(self, e):
        async for r in self._quick_query(e, "csnc_http", "CSNC"): yield r

    # ═══════════════════ 辅助 ═══════════════════

    async def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            admins = self.config.get("admin_users", [])
            return event.get_sender_id() in admins
        except Exception:
            return False

    async def on_astrbot_loaded(self):
        logger.debug("[Mix] 准备就绪")
