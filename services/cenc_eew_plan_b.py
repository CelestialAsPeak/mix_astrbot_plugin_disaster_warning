"""
services/cenc_eew_plan_b.py — CENC/CEA EEW Plan B 独立模块。

FAN Studio 失效时的 HTTP 轮询替代方案，不依赖 FAN WS。
FAN 正常时此模块零负载（不启动轮询），仅提供 DB 查询能力。

全国源 (cenc_eew_http):
  POST https://yjfw.cenc.ac.cn/api/earthquake/event/v1/list
  {"app_id": "dkcxbftqof0h", "page_query": {"page_no":1, "page_size":10}}
  间隔: 2s

省网源 (cenc_eew_province):
  同上 API，各省独立 app_id，page_size=10
  高频省 (21省): 随机 3-6s，低频省 (9省): 随机 10-20s
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any, Callable

import aiohttp

try:
    from astrbot.api import logger
except ImportError:
    logger = logging.getLogger(__name__)

try:
    from ..parser.registry import ParserRegistry
except ImportError:
    from parser.registry import ParserRegistry


# ═══════════════════ 常量 ═══════════════════

_CENC_BASE = "https://yjfw.cenc.ac.cn"
_CENC_NATIONAL_APP_ID = "dkcxbftqof0h"

_CENC_PROVINCES: list[tuple[str, str]] = [
    ("吉林省地震预警网",     "dpxg86itciyp"),
    ("北京市地震预警网",     "dpy5rsc2zzep"),
    ("天津市地震预警网",     "dopmqd8mw0e9"),
    ("上海市地震预警网",     "dophp7576p6p"),
    ("重庆市地震预警网",     "dqiz92g2cw75"),
    ("河北省地震预警网",     "dkdc0x13wvsx"),
    ("山西省地震预警网",     "dopmdvabu9s1"),
    ("内蒙古地震预警网",     "dp4rw2h79c01"),
    ("辽宁省地震预警网",     "dpb408jj91q9"),
    ("黑龙江省地震预警网",   "drhl102u3qpt"),
    ("江苏省地震预警网",     "dkdd7p1kahoh"),
    ("浙江省地震预警网",     "dpqcusq8ctfl"),
    ("安徽省地震预警网",     "dopdvog4fim9"),
    ("福建省地震预警网",     "dopx7fmkf18h"),
    ("江西省地震预警网",     "dp045wriv6dd"),
    ("山东省地震预警网",     "dq3jb3raal8h"),
    ("河南省地震预警网",     "dpx8o9smyqdd"),
    ("湖北省地震预警网",     "dpw2zk8cu1oh"),
    ("湖南省地震预警网",     "dtuzi85h4g75"),
    ("广东省地震预警网",     "dkdccx9ldzwh"),
    ("广西省地震预警网",     "dpl2o80jzs6f"),
    ("海南省地震预警网",     "dkg67hzs5u69"),
    ("四川省地震预警网",     "dpcl7ht0gqjd"),
    ("贵州省地震预警网",     "doplp1n82sch"),
    ("云南省地震预警网",     "dpb48n2kqm6j"),
    ("西藏省地震预警网",     "doleg621s99b"),
    ("陕西省地震预警网",     "dopgbktlr2td"),
    ("甘肃省地震预警网",     "dp0bwm7l9b5x"),
    ("青海省地震预警网",     "dpwdh00eqjs9"),
    ("宁夏省地震预警网",     "donnrac7q4g1"),
    ("新疆省地震预警网",     "dp5xuz8iwxk4"),
    ("山东省地震预警网",     "dq3jb3raal8h"),  # 山东在列表中出现两次？从备份看确实如此，保留
]

# 低频省 app_id（地震活动少的省份，轮询间隔大一些）
_CEAPR_LOW_FREQ: set[str] = {
    "dpxg86itciyp", "dpy5rsc2zzep", "dopmqd8mw0e9",
    "dophp7576p6p", "dkdc0x13wvsx", "donnrac7q4g1",
    "dopgbktlr2td", "doplp1n82sch", "dkg67hzs5u69",
}

_CENC_SHORT_NAME: dict[str, str] = {
    "dpxg86itciyp": "吉林", "dpy5rsc2zzep": "北京",
    "dopmqd8mw0e9": "天津", "dophp7576p6p": "上海",
    "dqiz92g2cw75": "重庆", "dkdc0x13wvsx": "河北",
    "dopmdvabu9s1": "山西", "dp4rw2h79c01": "内蒙古",
    "dpb408jj91q9": "辽宁", "drhl102u3qpt": "黑龙江",
    "dkdd7p1kahoh": "江苏", "dpqcusq8ctfl": "浙江",
    "dopdvog4fim9": "安徽", "dopx7fmkf18h": "福建",
    "dp045wriv6dd": "江西", "dq3jb3raal8h": "山东",
    "dpx8o9smyqdd": "河南", "dpw2zk8cu1oh": "湖北",
    "dtuzi85h4g75": "湖南", "dkdccx9ldzwh": "广东",
    "dpl2o80jzs6f": "广西", "dkg67hzs5u69": "海南",
    "dpcl7ht0gqjd": "四川", "doplp1n82sch": "贵州",
    "dpb48n2kqm6j": "云南", "doleg621s99b": "西藏",
    "dopgbktlr2td": "陕西", "dp0bwm7l9b5x": "甘肃",
    "dpwdh00eqjs9": "青海", "donnrac7q4g1": "宁夏",
    "dp5xuz8iwxk4": "新疆",
}


# ═══════════════════ Plan B 服务类 ═══════════════════

class CencEewPlanB:
    """CENC/CEA EEW Plan B 服务。

    FAN 正常时：start() 不应被调用，零负载。
    FAN 失能时：调用 start() 启动轮询，stop() 停止。

    查询命令 (/cea /cenc) 始终可用，不依赖轮询状态。
    """

    def __init__(
        self,
        config: dict,
        database: Any = None,
        pipeline: Any = None,
        signal_bus: Any = None,
    ):
        self._config = config
        self._db = database
        self._pipeline = pipeline
        self._signal_bus = signal_bus
        self._running = False

        # 全国源
        self._cenc_task: asyncio.Task | None = None
        self._cenc_seen: set[str] = set()
        self._cenc_first_done = False

        # 省网源
        self._ceapr_tasks: dict[str, asyncio.Task] = {}
        self._ceapr_fusion: dict[str, dict] = {}
        self._ceapr_latest: dict[str, str] = {}
        self._ceapr_first_done = False

    # ── 属性 ──

    @property
    def is_running(self) -> bool:
        """轮询是否正在运行。"""
        return self._running

    # ── 启停 ──

    async def start(self):
        """启动 CENC 全国 + CEA-PR 各省轮询。"""
        if self._running:
            return
        self._running = True
        self._cenc_first_done = False
        self._ceapr_first_done = False

        # 检查开关
        ds = self._config.get("data_sources", {})
        dh = ds.get("direct_http", {}) if isinstance(ds, dict) else {}

        if dh.get("cenc_eew", True) is not False:
            logger.info("[PlanB] 启动全国 EEW 轮询 (2s)")
            self._cenc_task = asyncio.create_task(self._poll_national())
        else:
            logger.info("[PlanB] 全国 EEW 轮询已禁用")

        if dh.get("cenc_eew_province", True) is not False:
            logger.info(f"[PlanB] 启动 {len(_CENC_PROVINCES)} 省轮询")
            for name, app_id in _CENC_PROVINCES:
                task = asyncio.create_task(self._poll_province(name, app_id))
                self._ceapr_tasks[app_id] = task
        else:
            logger.info("[PlanB] 省网 EEW 轮询已禁用")

    async def stop(self):
        """停止所有轮询。"""
        self._running = False
        if self._cenc_task:
            self._cenc_task.cancel()
            self._cenc_task = None
        for task in self._ceapr_tasks.values():
            task.cancel()
        self._ceapr_tasks.clear()
        logger.info("[PlanB] 轮询已停止")

    # ── 全国源轮询 ──

    async def _poll_national(self):
        """全国 EEW 轮询，2s 间隔 + hash jitter。"""
        url = f"{_CENC_BASE}/api/earthquake/event/v1/list"
        headers = {"Content-Type": "application/json"}
        payload = {
            "app_id": _CENC_NATIONAL_APP_ID,
            "page_query": {"page_no": 1, "page_size": 10},
        }
        session = None
        while self._running:
            try:
                if session is None or session.closed:
                    session = aiohttp.ClientSession(headers=headers)
                async with session.post(url, json=payload, timeout=5) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        await self._handle_national(data)
            except asyncio.TimeoutError:
                pass
            except aiohttp.ClientError as e:
                logger.warning(f"[PlanB 全国] 请求失败: {e}")
                if session and not session.closed:
                    await session.close()
                session = None
            except Exception as e:
                logger.debug(f"[PlanB 全国] 错误: {e}")
            jitter = (hashlib.md5(b"cenc_n").digest()[0] / 256.0) * 0.5
            await asyncio.sleep(2 + jitter)

    async def poll_national_once(self) -> bool:
        """单次强制抓取（供查询命令按需调用）。"""
        url = f"{_CENC_BASE}/api/earthquake/event/v1/list"
        headers = {"Content-Type": "application/json"}
        payload = {
            "app_id": _CENC_NATIONAL_APP_ID,
            "page_query": {"page_no": 1, "page_size": 10},
        }
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.post(url, json=payload, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        await self._handle_national(data)
                        return True
        except Exception as e:
            logger.warning(f"[PlanB] 单次抓取失败: {e}")
        return False

    async def _handle_national(self, raw_data: dict):
        """处理全国 EEW 结果：解析 → 去重 → 入库 → 推送。"""
        parser = ParserRegistry.get("cenc_eew_http")
        if parser is None:
            return
        result = parser.parse_message(raw_data)
        if not result:
            return
        envelopes = result if isinstance(result, list) else [result]
        if not envelopes:
            return

        is_first = not self._cenc_first_done
        self._cenc_first_done = True

        new_envs = []
        for env in envelopes:
            raw = getattr(env.event, "raw", {}) or {}
            third_id = raw.get("third_id", "") or env.identity.event_id
            dedup_key = f"{third_id}|{env.identity.report_num or 0}"
            if dedup_key in self._cenc_seen:
                continue
            self._cenc_seen.add(dedup_key)
            new_envs.append(env)

        # 防内存泄漏
        if len(self._cenc_seen) > 10000:
            self._cenc_seen.clear()

        if not new_envs:
            return

        # 入库
        stored = 0
        if self._db:
            for env in new_envs:
                try:
                    await self._db.insert_envelope(env)
                    stored += 1
                except Exception:
                    pass

        if is_first:
            logger.info(f"[PlanB 全国] {stored} 条入库（首次静默）")
        elif stored and self._pipeline:
            logger.info(f"[PlanB 全国] {stored} 条入库，推送中...")
            for env in new_envs:
                try:
                    await self._pipeline.handle(env)
                except Exception as e:
                    logger.warning(f"[PlanB 全国] pipeline 推送失败: {e}")

    # ── 省网轮询 ──

    async def _poll_province(self, name: str, app_id: str):
        """单个省轮询（独立时间线）。"""
        url = f"{_CENC_BASE}/api/earthquake/event/v1/list"
        headers = {"Content-Type": "application/json"}
        # 错峰启动
        offset = (hashlib.md5(app_id.encode()).digest()[0] / 256.0) * 30
        await asyncio.sleep(offset)

        session = None
        while self._running:
            try:
                if session is None or session.closed:
                    session = aiohttp.ClientSession(headers=headers)
                payload = {
                    "app_id": app_id,
                    "page_query": {"page_no": 1, "page_size": 10},
                }
                async with session.post(url, json=payload, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        await self._handle_province(data, name, app_id)
            except asyncio.TimeoutError:
                pass
            except aiohttp.ClientError as e:
                logger.warning(f"[PlanB {name}] HTTP 错误: {e}")
                if session and not session.closed:
                    await session.close()
                session = None
            except Exception as e:
                logger.debug(f"[PlanB {name}] 错误: {e}")

            if app_id in _CEAPR_LOW_FREQ:
                await asyncio.sleep(random.uniform(10, 20))
            else:
                await asyncio.sleep(random.uniform(3, 6))

    async def _handle_province(self, raw_data: dict, name: str, app_id: str):
        """处理单个省 EEW：解析 → 指纹去重 → 入库 → 推送。"""
        if not isinstance(raw_data, dict) or raw_data.get("code") != 0:
            return
        items = (raw_data.get("data") or {}).get("spot_infos")
        if not isinstance(items, list) or not items:
            return
        item = items[0]
        if not isinstance(item, dict):
            return

        parser = ParserRegistry.get("cenc_eew_province")
        if parser is None:
            return
        result = parser.parse_message(raw_data)
        if not result:
            return
        envelopes = result if isinstance(result, list) else [result]
        if not envelopes:
            return
        env = envelopes[0]
        ev = env.event

        # 构建指纹
        mag = getattr(ev, "magnitude", None) or item.get("level")
        place = getattr(ev, "place_name", None) or item.get("location", "")
        depth = getattr(ev, "depth", None) or item.get("depth")
        lat = getattr(ev, "latitude", None) or item.get("latitude")
        lon = getattr(ev, "longitude", None) or item.get("longitude")
        ts = item.get("created_at", 0) or 0
        report_num = item.get("serial_number", 0) or 0

        try:
            mag_s = f"{float(mag):.1f}" if mag is not None else "?"
            lat_s = f"{float(lat):.3f}" if lat is not None else "?"
            lon_s = f"{float(lon):.3f}" if lon is not None else "?"
            dep_s = str(depth) if depth is not None else "?"
            fingerprint = f"{mag_s}|{place}|{dep_s}|{lat_s}|{lon_s}|{ts}|{report_num}"
        except (TypeError, ValueError):
            return

        # 省无变化 → 跳过
        prev_fp = self._ceapr_latest.get(app_id)
        if prev_fp == fingerprint:
            return
        self._ceapr_latest[app_id] = fingerprint

        # 注入省份名
        province_short = _CENC_SHORT_NAME.get(app_id, name.replace("地震预警网", ""))
        if hasattr(ev, "raw") and isinstance(ev.raw, dict):
            ev.raw["_province_name"] = province_short

        # 入库
        stored = False
        if self._db:
            try:
                await self._db.insert_envelope(env)
                stored = True
            except Exception as e:
                logger.warning(f"[PlanB {name}] 入库失败: {e}")

        # 日志
        occurred = getattr(ev, "occurred_at", None)
        time_s = occurred.strftime("%H:%M:%S") if occurred else "?"
        mag_log = f" M{float(mag):.1f}" if mag is not None else ""
        logger.info(f"[PlanB {name}]: {time_s}{mag_log} {place}")

        # ── 推送门 ──

        # 门1: 首次静默
        if not self._ceapr_first_done:
            self._ceapr_first_done = True
            return

        # 门2: 超 1 小时的历史事件不推
        now_ts = time.time()
        ts_raw = item.get("created_at", 0) or 0
        if isinstance(ts_raw, (int, float)) and ts_raw > 1000000000:
            event_ts = ts_raw / 1000 if ts_raw > 10000000000 else ts_raw
            age = now_ts - event_ts
            if age > 3600:
                return

        # 门3: 跨省融合去重（同事件只推第一个省的）
        fusion_fp = f"{mag_s}|{place}|{dep_s}|{lat_s}|{lon_s}|{ts}"
        if fusion_fp in self._ceapr_fusion:
            return
        self._ceapr_fusion[fusion_fp] = {"app_id": app_id, "time": now_ts}
        # 清理过期指纹
        if len(self._ceapr_fusion) > 500:
            cutoff = now_ts - 86400
            self._ceapr_fusion = {
                k: v for k, v in self._ceapr_fusion.items()
                if v.get("time", 0) > cutoff
            }

        # 推送
        if self._pipeline:
            try:
                await self._pipeline.handle(env)
            except Exception as e:
                logger.warning(f"[PlanB {name}] pipeline 推送失败: {e}")

    # ── DB 查询（不依赖轮询状态） ──

    async def query_national(self, limit: int = 5) -> list[dict]:
        """查询全国 EEW 最新记录。"""
        if not self._db:
            return []
        return await self._db.query_eew("cenc_eew_http", limit)

    async def query_province(self, province: str, limit: int = 5) -> list[dict]:
        """查询某省最新 EEW。province 可以是省名或 app_id。"""
        if not self._db:
            return []

        # 如果传的是省名，找对应 app_id
        app_id = province
        if province in _CENC_SHORT_NAME.values():
            rev = {v: k for k, v in _CENC_SHORT_NAME.items()}
            app_id = rev.get(province, province)
        elif province in dict(_CENC_PROVINCES):
            app_id = dict(_CENC_PROVINCES).get(province, province)

        if not self._db:
            return []
        rows = await self._db.query_eew("cenc_eew_province", limit)
        # 按 app_id 或省份名过滤
        result = []
        for r in rows:
            raw = r.get("raw", {}) or {}
            if isinstance(raw, str):
                try:
                    import json
                    raw = json.loads(raw)
                except Exception:
                    raw = {}
            rid = raw.get("app_id", "") or r.get("source", "")
            if rid == app_id or rid == province:
                result.append(r)
        return result[:limit]

    async def query_all_provinces(self) -> list[dict]:
        """查询所有省最新一条 EEW。"""
        if not self._db:
            return []
        rows = await self._db.query_eew("cenc_eew_province", 100)
        # 按 app_id 去重，每个省只保留最新
        seen: dict[str, dict] = {}
        for r in rows:
            raw = r.get("raw", {}) or {}
            if isinstance(raw, str):
                try:
                    import json
                    raw = json.loads(raw)
                except Exception:
                    raw = {}
            rid = raw.get("app_id", "")
            if rid and rid not in seen:
                seen[rid] = r
        return list(seen.values())
