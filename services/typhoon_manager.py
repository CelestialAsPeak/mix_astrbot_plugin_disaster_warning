"""
services/typhoon_manager.py — 台风路径跟踪管理器。

双源（CMA/JMA）HTTP 轮询 + 差异检测 + 推送/入库。

移植自旧版 core/services/typhoon/typhoon_manager.py。
"""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Any, Callable

import aiohttp
try:
    from astrbot.api import logger
except ImportError:
    import logging as logger

try:
    from domain.models import TyphoonTrackPoint, TyphoonEvent, EventEnvelope, EventIdentity
except ImportError:
    from ..domain.models import TyphoonTrackPoint, TyphoonEvent, EventEnvelope, EventIdentity


# CMA 风速(m/s) → 等级 (0-6)
def _wind_to_category(wind_speed: float | None) -> int:
    if wind_speed is None:
        return 0
    if wind_speed < 10.8:
        return 0
    if wind_speed < 17.2:
        return 1
    if wind_speed < 24.5:
        return 2
    if wind_speed < 32.7:
        return 3
    if wind_speed < 41.5:
        return 4
    if wind_speed < 51.0:
        return 5
    return 6


_BEAUFORT_TO_CATEGORY = {8: 1, 9: 2, 10: 3, 11: 4, 12: 5}


def _beaufort_to_category(bf: int | None) -> int:
    if bf is None:
        return 0
    return _BEAUFORT_TO_CATEGORY.get(bf, min(bf - 7, 6) if bf > 12 else 0)


def _parse_radii(val: str | None) -> float | None:
    if val is None or not val.strip():
        return None
    try:
        return float(val.strip())
    except (ValueError, TypeError):
        return None


def _radii_dict(en, es, ws, wn) -> dict[str, float] | None:
    result = {}
    for k, v in [("NE", en), ("SE", es), ("SW", ws), ("NW", wn)]:
        r = _parse_radii(v)
        if r is not None:
            result[k] = r
    return result if result else None


_DIRECTION_NAMES = {
    "N": "北", "NNE": "北北东", "NE": "东北", "ENE": "东北东",
    "E": "东", "ESE": "东南东", "SE": "东南", "SSE": "南南东",
    "S": "南", "SSW": "南南西", "SW": "西南", "WSW": "西南西",
    "W": "西", "WNW": "西北西", "NW": "西北", "NNW": "北北西",
}


def _deg_to_direction_str(deg_str: str | None) -> str | None:
    if not deg_str or not deg_str.strip():
        return None
    try:
        deg = float(deg_str.strip())
    except (ValueError, TypeError):
        return None
    idx = round(deg / 22.5) % 16
    names = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
             "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return _DIRECTION_NAMES.get(names[idx], f"{deg}°")


# JMA 方向英文 → 中文
_DIR_EN_TO_CN = {
    "N": "北", "NNE": "北北东", "NE": "东北", "ENE": "东北东",
    "E": "东", "ESE": "东南东", "SE": "东南", "SSE": "南南东",
    "S": "南", "SSW": "南南西", "SW": "西南", "WSW": "西南西",
    "W": "西", "WNW": "西北西", "NW": "西北", "NNW": "北北西",
    "CALM": "静止",
}


def _direction_en_to_cn(en_str: str | None) -> str | None:
    if not en_str:
        return None
    return _DIR_EN_TO_CN.get(en_str.strip(), en_str.strip())


_JMA_CAT_TO_CMA = {"TD": 1, "TS": 2, "STS": 3, "TY": 4}


def _jma_cat_to_cma(cat_str: str | None) -> int:
    if not cat_str:
        return 0
    return _JMA_CAT_TO_CMA.get(cat_str.strip().upper(), 0)


CATEGORY_NAMES = {
    0: "热带低压以下", 1: "热带低压", 2: "热带风暴",
    3: "强热带风暴", 4: "台风", 5: "强台风", 6: "超强台风",
}


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _parse_jst_to_dt(jst_str: str | None) -> datetime | None:
    if not jst_str:
        return None
    try:
        if "+" in jst_str:
            jst_str = jst_str.split("+")[0]
        dt = datetime.fromisoformat(jst_str)
        return dt
    except (ValueError, TypeError):
        return None


def _find_jma_past_track(past_data: list, tc_id: str) -> dict | None:
    for item in past_data or []:
        if item.get("tropicalCyclone", "") == tc_id:
            return {
                "preTyphoon": item.get("preTyphoon", []),
                "typhoon": item.get("typhoon", []),
            }
    return None


class TyphoonManager:
    """台风路径跟踪管理器。

    负责双源（CMA/JMA）HTTP 轮询、差异检测、推送/入库。
    存储活跃台风内存状态，供 /台风 命令直接查询。
    """

    CMA_LIST_URL = "https://typhoon.weather.com.cn/data/typhoonFlash/taifeng1.xml"
    CMA_DETAIL_TPL = "https://typhoon.weather.com.cn/data/typhoonFlash/{code}.xml"
    JMA_PAST_URL = "https://www.jma.go.jp/bosai/typhoon/data/pastTracks.json"
    JMA_SPECS_TPL = "https://www.jma.go.jp/bosai/typhoon/data/{tc_id}/specifications.json"
    JMA_PREV_TPL = "https://www.jma.go.jp/bosai/typhoon/data/{tc_id}/forecastPreviousIssue.json"

    def __init__(
        self,
        db=None,
        push_callback=None,
    ):
        self.db = db
        self._push_callback = push_callback
        self._http_session: aiohttp.ClientSession | None = None

        # 活跃台风：code -> MutableTyphoonData
        self._active: dict[str, dict[str, Any]] = {}

        # 已推送时间戳：code -> set of "YYYY-MM-DD HH:00"
        self._pushed_times: dict[str, set[str]] = {}

        # 预报指纹：code -> {ts_key: fingerprint}
        self._pushed_forecast: dict[str, dict[str, str]] = {}

        # 上次 code 列表（检测停止编号）
        self._last_seen: dict[str, set[str]] = {"cma": set(), "jma": set()}

    @property
    def http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers={"User-Agent": "AstrBot/MixDisasterWarning/1.0"},
            )
        return self._http_session

    async def close(self):
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

    # ────────────── 轮询入口 ──────────────

    async def poll_cma(self):
        """CMA 轮询周期。"""
        try:
            await self._do_cma_poll()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[CMA台风] 轮询异常: {e}")

    async def poll_jma(self):
        """JMA 轮询周期。"""
        try:
            await self._do_jma_poll()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[JMA台风] 轮询异常: {e}")

    # ────────────── CMA 轮询 ──────────────

    async def _do_cma_poll(self):
        session = self.http_session

        # 第一阶段：列表
        async with session.get(self.CMA_LIST_URL) as resp:
            if resp.status != 200:
                return
            list_xml = await resp.text()

        typhoon_list = self._parse_cma_list(list_xml)
        current_codes = {item["code"] for item in typhoon_list}

        # 第二阶段：每个台风详情
        for item in typhoon_list:
            code = item["code"]
            detail_url = self.CMA_DETAIL_TPL.format(code=code)
            try:
                async with session.get(detail_url) as dresp:
                    if dresp.status != 200:
                        continue
                    detail_xml = await dresp.text()

                event = self._parse_cma_detail(detail_xml)
                if event is None:
                    continue

                event["code"] = code
                title = item.get("title", "")
                if "(" in title:
                    event["name_cn"] = title.split("(")[0].strip()
                    en_part = title.split("(")[-1].rstrip(")")
                    if "，" in en_part:
                        event["name_en"] = en_part.split("，")[-1].strip()
                    elif "号台风" in en_part:
                        event["name_en"] = en_part.split("号台风")[-1].strip()

                await self._process_event(event, "cma_typhoon")

            except Exception as e:
                logger.warning(f"[CMA台风] {code} 详情拉取失败: {e}")

        # 检测停止编号
        old = self._last_seen.get("cma", set())
        disappeared = old - current_codes
        for code in disappeared:
            await self._push_event(code, "inactive", "cma_typhoon")
            self._active.pop(code, None)
            self._pushed_times.pop(code, None)
            self._pushed_forecast.pop(code, None)
        self._last_seen["cma"] = current_codes

    def _parse_cma_list(self, xml_text: str) -> list[dict]:
        """解析 taifeng1.xml → [{code, title}, ...]"""
        try:
            root = ET.fromstring(xml_text)
            result = []
            for tf in root.findall("tfProps"):
                code = tf.get("code", "")
                title = tf.get("title", "")
                if code:
                    result.append({"code": code, "title": title})
            return result
        except ET.ParseError as e:
            logger.error(f"[CMA台风] 列表XML解析失败: {e}")
            return []

    def _parse_cma_title(self, title: str) -> tuple[str, str]:
        """从标题提取中文/英文名。"""
        name_cn = title
        name_en = ""
        if "(" in title:
            name_cn = title.split("(")[0].strip()
            rest = title.split("(")[1]
            if "，" in rest:
                en_part = rest.split("，")[-1].rstrip(")")
                name_en = en_part.strip()
            elif "号台风" in rest:
                en_part = rest.split("号台风")[-1].rstrip(")")
                name_en = en_part.strip()
        return name_cn, name_en

    def _parse_cma_tf_props(self, elem: ET.Element) -> dict | None:
        """解析单个 <tfProps> → 路径点 dict"""
        try:
            y = int(elem.get("y", "0"))
            m = int(elem.get("m", "1"))
            d = int(elem.get("d", "1"))
            h = int(elem.get("h", "0"))
            jd = float(elem.get("jd", "0"))
            wd = float(elem.get("wd", "0"))
        except (ValueError, TypeError):
            return None

        try:
            timestamp = datetime(y, m, d, h)
        except (ValueError, OverflowError):
            timestamp = None

        fs = _parse_radii(elem.get("fs"))
        qy = _parse_radii(elem.get("qy"))
        fl = _parse_radii(elem.get("fl"))
        fl_int = int(fl) if fl is not None else None
        cat = _wind_to_category(fs) if fs is not None else _beaufort_to_category(fl_int)
        direction = _deg_to_direction_str(elem.get("fx"))
        move_speed = _parse_radii(elem.get("sd"))

        wind_radii_7 = _radii_dict(
            elem.get("en7"), elem.get("es7"), elem.get("ws7"), elem.get("wn7"),
        )
        wind_radii_10 = _radii_dict(
            elem.get("en10"), elem.get("es10"), elem.get("ws10"), elem.get("wn10"),
        )
        wind_radii_12 = _radii_dict(
            elem.get("en12"), elem.get("es12"), elem.get("ws12"), elem.get("wn12"),
        )

        now = datetime.now()
        is_fc = timestamp is not None and timestamp > now

        return {
            "timestamp": timestamp,
            "latitude": wd,
            "longitude": jd,
            "pressure": qy,
            "wind_speed": fs,
            "category": cat,
            "beaufort": fl_int,
            "direction": direction,
            "move_speed": move_speed,
            "wind_radii_7": wind_radii_7,
            "wind_radii_10": wind_radii_10,
            "wind_radii_12": wind_radii_12,
            "is_forecast": is_fc,
        }

    def _parse_cma_detail(self, xml_text: str) -> dict | None:
        """解析 {code}.xml → dict(name_cn, name_en, code, track_points)"""
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.error(f"[CMA台风] 详情XML解析失败: {e}")
            return None

        title = root.get("title", "")
        name_cn, name_en = self._parse_cma_title(title)

        track = []
        for tf in root.findall("tfProps"):
            pt = self._parse_cma_tf_props(tf)
            if pt:
                track.append(pt)

        if not track:
            return None

        latest = track[-1]
        return {
            "name_cn": name_cn,
            "name_en": name_en,
            "code": "",
            "category": latest["category"],
            "pressure": latest["pressure"],
            "wind_speed": latest["wind_speed"],
            "latitude": latest["latitude"],
            "longitude": latest["longitude"],
            "last_updated": latest["timestamp"],
            "move_direction": latest["direction"],
            "move_speed": latest["move_speed"],
            "track_points": track,
            "is_active": True,
            "source": "cma",
        }

    # ────────────── JMA 轮询 ──────────────

    async def _do_jma_poll(self):
        session = self.http_session

        async with session.get(self.JMA_PAST_URL) as resp:
            if resp.status != 200:
                logger.warning(f"[JMA台风] pastTracks 返回 {resp.status}")
                return
            past_data = await resp.json()

        tc_list = self._parse_jma_past_tracks(past_data)
        if not tc_list:
            return

        active_tc_ids: set[str] = set()
        recent_tc_ids = [t["tc_id"] for t in tc_list[-10:]]

        # 尝试后续编号
        last_num = 0
        for t in tc_list:
            try:
                n = int(t.get("typhoon_number", "0"))
                last_num = max(last_num, n)
            except (ValueError, TypeError):
                pass
        for i in range(1, 5):
            recent_tc_ids.append(f"TC{last_num + i:04d}")

        for tc_id in recent_tc_ids:
            try:
                specs_url = self.JMA_SPECS_TPL.format(tc_id=tc_id)
                async with session.get(specs_url) as sresp:
                    if sresp.status != 200:
                        continue
                    specs_json = await sresp.json()

                past_track = _find_jma_past_track(past_data, tc_id)
                if not past_track:
                    try:
                        prev_url = self.JMA_PREV_TPL.format(tc_id=tc_id)
                        async with session.get(prev_url) as prev_resp:
                            if prev_resp.status == 200:
                                prev_json = await prev_resp.json()
                                for item in (prev_json or []):
                                    if isinstance(item, dict) and item.get("advancedHours") == 0:
                                        track = item.get("track", {})
                                        if isinstance(track, dict):
                                            past_track = {
                                                "preTyphoon": track.get("preTyphoon", []),
                                                "typhoon": track.get("typhoon", []),
                                            }
                                            break
                    except Exception as prev_err:
                        logger.warning(f"[JMA台风] {tc_id} forecastPreviousIssue 失败: {prev_err}")

                event = self._parse_jma_specs(specs_json, tc_id, past_track=past_track)
                if event is None:
                    continue

                active_tc_ids.add(tc_id)
                await self._process_event(event, "jma_typhoon")

            except Exception as e:
                logger.warning(f"[JMA台风] {tc_id} 处理异常: {e}")
                continue

        # 检测停止编号
        old_jma = self._last_seen.get("jma", set())
        disappeared = old_jma - active_tc_ids
        for tc_id in disappeared:
            await self._push_event(tc_id, "inactive", "jma_typhoon")
            self._active.pop(tc_id, None)
            self._pushed_times.pop(tc_id, None)
            self._pushed_forecast.pop(tc_id, None)
        self._last_seen["jma"] = active_tc_ids

    def _parse_jma_past_tracks(self, json_data: list) -> list[dict]:
        result = []
        for item in json_data or []:
            tc_id = item.get("tropicalCyclone", "")
            num = item.get("typhoonNumber", "")
            if tc_id:
                result.append({"tc_id": tc_id, "typhoon_number": num})
        return result

    def _parse_jma_position(self, entry: dict) -> tuple[float, float] | None:
        pos = entry.get("position", {})
        if isinstance(pos, dict):
            deg = pos.get("deg")
            if isinstance(deg, list) and len(deg) == 2:
                try:
                    return float(deg[0]), float(deg[1])
                except (ValueError, TypeError):
                    return None
        return None

    def _parse_jma_warning_radii(self, entry: dict, warning_key: str) -> dict[str, float] | None:
        warnings = entry.get(warning_key)
        if not warnings or not isinstance(warnings, list):
            return None
        result = {}
        for w in warnings:
            area_raw = w.get("area", "")
            range_info = w.get("range", {})
            km = _safe_float(range_info.get("km"))
            area = area_raw if isinstance(area_raw, str) else ""
            is_all = False
            if not area and isinstance(area_raw, dict):
                if area_raw.get("jp") == "全域" or area_raw.get("en") == "All":
                    is_all = True
            if is_all and km:
                for direction in ("NE", "SE", "SW", "NW"):
                    result[direction] = km
            elif area and km:
                area_cn = area.replace("北東", "NE").replace("南東", "SE")
                area_cn = area_cn.replace("南西", "SW").replace("北西", "NW")
                area_cn = area_cn.replace("北东", "NE").replace("南东", "SE")
                area_cn = area_cn.replace("南西", "SW").replace("北西", "NW")
                if area_cn in ("NE", "SE", "SW", "NW"):
                    result[area_cn] = km
                elif area_cn == "全域" or area == "全域":
                    for direction in ("NE", "SE", "SW", "NW"):
                        result[direction] = km
        return result if result else None

    def _parse_jma_specs(
        self, specs: list, tc_id: str, past_track: dict | None = None
    ) -> dict | None:
        if not specs or len(specs) < 2:
            return None

        # Entry 0: title
        title_info = specs[0] if isinstance(specs[0], dict) and specs[0].get("part") == "title" else {}
        name_en = ""
        name_jp = ""
        if title_info:
            name = title_info.get("name", {})
            if isinstance(name, dict):
                name_en = name.get("en", "")
                name_jp = name.get("jp", "")
            typhoon_number = title_info.get("typhoonNumber", "")

        # Entry 1: current analysis
        analysis = specs[1] if len(specs) > 1 else {}
        analysis_pos = self._parse_jma_position(analysis)
        analysis_wind = _safe_float(
            analysis.get("maximumWind", {}).get("sustained", {}).get("m/s")
        )
        analysis_pres = _safe_float(analysis.get("pressure"))
        analysis_cat = _jma_cat_to_cma(analysis.get("category", {}).get("en"))
        analysis_time = _parse_jst_to_dt(analysis.get("validtime", {}).get("JST"))
        analysis_course = _direction_en_to_cn(analysis.get("course"))
        analysis_speed = _safe_float(analysis.get("speed", {}).get("km/h"))
        radii_10 = self._parse_jma_warning_radii(analysis, "galeWarning")
        radii_12 = self._parse_jma_warning_radii(analysis, "stormWarning")

        analysis_point = {
            "timestamp": analysis_time,
            "latitude": analysis_pos[0] if analysis_pos else 0,
            "longitude": analysis_pos[1] if analysis_pos else 0,
            "pressure": analysis_pres,
            "wind_speed": analysis_wind,
            "category": analysis_cat,
            "direction": analysis_course,
            "move_speed": analysis_speed,
            "wind_radii_10": radii_10,
            "wind_radii_12": radii_12,
            "is_forecast": False,
        }

        # 预报点
        forecast_points = []
        for entry in specs[2:]:
            if not isinstance(entry, dict):
                continue
            pos = self._parse_jma_position(entry)
            if not pos:
                continue
            f_wind = _safe_float(entry.get("maximumWind", {}).get("sustained", {}).get("m/s"))
            f_pres = _safe_float(entry.get("pressure"))
            f_cat = _jma_cat_to_cma(entry.get("category", {}).get("en"))
            f_time = _parse_jst_to_dt(entry.get("validtime", {}).get("JST"))
            f_course = _direction_en_to_cn(entry.get("course"))
            f_speed = _safe_float(entry.get("speed", {}).get("km/h"))
            f_radii_10 = self._parse_jma_warning_radii(entry, "galeWarning")
            f_radii_12 = self._parse_jma_warning_radii(entry, "stormWarning")
            if not f_radii_10 and f_radii_12:
                f_radii_10 = f_radii_12

            forecast_points.append({
                "timestamp": f_time,
                "latitude": pos[0],
                "longitude": pos[1],
                "pressure": f_pres,
                "wind_speed": f_wind,
                "category": f_cat,
                "direction": f_course,
                "move_speed": f_speed,
                "wind_radii_10": f_radii_10,
                "wind_radii_12": f_radii_12,
                "is_forecast": True,
            })

        all_points = []
        # 从 past_track 生成历史路径点
        if past_track:
            past_coords = []
            for coord in past_track.get("preTyphoon", []):
                if isinstance(coord, (list, tuple)) and len(coord) >= 2:
                    past_coords.append([float(coord[0]), float(coord[1])])
            for coord in past_track.get("typhoon", []):
                if isinstance(coord, (list, tuple)) and len(coord) >= 2:
                    past_coords.append([float(coord[0]), float(coord[1])])
            if len(past_coords) > 1 and analysis_time:
                for i, (lat, lon) in enumerate(past_coords):
                    hours_back = (len(past_coords) - 1 - i) * 3
                    ts = analysis_time - timedelta(hours=hours_back)
                    all_points.append({
                        "timestamp": ts, "latitude": lat, "longitude": lon,
                        "is_forecast": False,
                    })

        if analysis_point["latitude"] != 0:
            all_points.append(analysis_point)
        all_points.extend(forecast_points)

        if not all_points:
            return None

        latest = all_points[-1]
        return {
            "name_cn": name_jp,
            "name_en": name_en,
            "code": tc_id,
            "category": latest["category"],
            "pressure": latest["pressure"],
            "wind_speed": latest["wind_speed"],
            "latitude": latest["latitude"],
            "longitude": latest["longitude"],
            "last_updated": latest["timestamp"],
            "move_direction": latest["direction"],
            "move_speed": latest["move_speed"],
            "track_points": all_points,
            "is_active": True,
            "source": "jma",
            "typhoon_number": typhoon_number if title_info else "",
        }

    # ────────────── 活跃度判断 ──────────────

    @staticmethod
    def _is_active(data: dict) -> bool:
        now = datetime.now()
        pts = data.get("track_points", [])
        if not pts:
            return False
        last = pts[-1]
        ts = last.get("timestamp")
        if ts is None:
            return False
        return (now - ts).total_seconds() < 48 * 3600

    # ────────────── 核心处理 ──────────────

    async def _process_event(self, data: dict, source_id: str):
        code = data.get("code", "")
        if not code:
            return

        if code not in self._active:
            if not self._is_active(data):
                return
            self._active[code] = data
            self._pushed_times[code] = self._extract_times(data["track_points"])
            self._detect_forecast_change(code, data["track_points"])
            await self._persist(data, source_id)
            await self._push_event(code, "initial", source_id)
            return

        old_data = self._active[code]
        new_points = self._detect_new_points(code, data["track_points"])

        if new_points:
            old_history = [p for p in old_data["track_points"] if not p.get("is_forecast")]
            new_analysis = [p for p in new_points if not p.get("is_forecast")]
            new_fcs = [p for p in data["track_points"] if p.get("is_forecast")]
            merged = old_history + new_analysis + new_fcs
            data["track_points"] = merged
            self._active[code] = data
            for pt in new_points:
                pt_event = {
                    **data,
                    "latitude": pt.get("latitude", data["latitude"]),
                    "longitude": pt.get("longitude", data["longitude"]),
                    "category": pt.get("category", data["category"]),
                    "pressure": pt.get("pressure", data["pressure"]),
                    "wind_speed": pt.get("wind_speed", data["wind_speed"]),
                    "last_updated": pt.get("timestamp", data["last_updated"]),
                    "track_points": merged,
                }
                await self._persist(pt_event, source_id)
            self._detect_forecast_change(code, data["track_points"])
            await self._push_event(code, "track_point", source_id)
            return

        # 无新路径点：检查预报变化
        changed = self._detect_forecast_change(code, data["track_points"])
        if changed:
            old_history2 = [p for p in old_data["track_points"] if not p.get("is_forecast")]
            new_fcs2 = [p for p in data["track_points"] if p.get("is_forecast")]
            data["track_points"] = old_history2 + new_fcs2
            self._active[code] = data
            await self._persist(data, source_id)
            await self._push_event(code, "forecast_update", source_id)
            return

        self._active[code] = data

    def _detect_new_points(self, code: str, points: list[dict]) -> list[dict]:
        pushed = self._pushed_times.get(code, set())
        result = []
        for p in points:
            ts = p.get("timestamp")
            if ts is None:
                continue
            ts_key = ts.strftime("%Y-%m-%d %H:00")
            if ts_key not in pushed:
                pushed.add(ts_key)
                result.append(p)
            # 也检查 is_forecast 标记——老的点但刚变成预报也算新
            elif p.get("is_forecast") and ts_key not in pushed:
                pushed.add(ts_key)
                result.append(p)
        self._pushed_times[code] = pushed
        return result

    @staticmethod
    def _extract_times(points: list[dict]) -> set[str]:
        return {
            p["timestamp"].strftime("%Y-%m-%d %H:00")
            for p in points if p.get("timestamp") is not None
        }

    @staticmethod
    def _compute_fingerprint(pt: dict) -> str:
        lat = f"{pt.get('latitude', 0):.2f}" if pt.get("latitude") is not None else "?"
        lon = f"{pt.get('longitude', 0):.2f}" if pt.get("longitude") is not None else "?"
        ws = f"{pt.get('wind_speed', 0):.1f}" if pt.get("wind_speed") is not None else "?"
        pres = f"{pt.get('pressure', 0):.0f}" if pt.get("pressure") is not None else "?"
        cat = str(pt.get("category", 0))
        return f"{lat}|{lon}|{ws}|{pres}|{cat}"

    def _detect_forecast_change(self, code: str, points: list[dict]) -> list[str]:
        stored = self._pushed_forecast.get(code, {})
        changed: list[str] = []
        current: dict[str, str] = {}
        for pt in points:
            if not pt.get("is_forecast") or pt.get("timestamp") is None:
                continue
            ts_key = pt["timestamp"].strftime("%Y-%m-%d %H:00")
            fp = self._compute_fingerprint(pt)
            current[ts_key] = fp
            if ts_key in stored and stored[ts_key] != fp:
                changed.append(ts_key)
        self._pushed_forecast[code] = current
        return changed

    # ────────────── 持久化 ──────────────

    async def _persist(self, data: dict, source_id: str):
        """写入 DB，供 /台风 命令查询。"""
        if not self.db:
            return
        try:
            code = data.get("code", "")
            name_cn = data.get("name_cn", "")
            name_en = data.get("name_en", "")
            pts = data.get("track_points", [])
            last_pt = pts[-1] if pts else {}
            lt = last_pt.get("timestamp")

            # 构建 raw_json：含全部路径点
            raw_json = json.dumps({
                "code": code,
                "name_cn": name_cn,
                "name_en": name_en,
                "source": data.get("source", source_id),
                "track": [
                    {
                        "timestamp": p.get("timestamp").isoformat() if p.get("timestamp") else None,
                        "lat": p.get("latitude", 0),
                        "lon": p.get("longitude", 0),
                        "pressure": p.get("pressure"),
                        "windSpeed": p.get("wind_speed"),
                        "category": p.get("category", 0),
                        "windRadii7": p.get("wind_radii_7"),
                        "windRadii10": p.get("wind_radii_10"),
                        "windRadii12": p.get("wind_radii_12"),
                        "isForecast": p.get("is_forecast", False),
                    }
                    for p in pts
                ],
            })

            # UPSERT（用 insert_event 兼容新旧列名）
            exists = await self.db.execute_raw(
                "SELECT id FROM events WHERE real_event_id=? AND source=? AND type='typhoon' LIMIT 1",
                (code, source_id),
            )
            if exists:
                await self.db.execute_raw("""
                    UPDATE events SET description=?, subtitle=?, magnitude=?, time=?, raw_json=?
                    WHERE real_event_id=? AND source=? AND type='typhoon'
                """, (
                    name_cn or name_en or code,
                    f"{name_cn or ''} {name_en or ''}".strip(),
                    data.get("category", 0),
                    lt.isoformat() if lt else None,
                    raw_json,
                    code, source_id,
                ))
            else:
                await self.db.insert_event({
                    "real_event_id": code,
                    "event_id": code,
                    "source": source_id,
                    "source_id": source_id,
                    "type": "typhoon",
                    "event_type": "typhoon",
                    "description": name_cn or name_en or code,
                    "subtitle": f"{name_cn or ''} {name_en or ''}".strip(),
                    "magnitude": data.get("category", 0),
                    "time": lt.isoformat() if lt else None,
                    "raw": {
                        "code": code,
                        "name_cn": name_cn,
                        "name_en": name_en,
                        "source": data.get("source", source_id),
                        "track": [
                            {
                                "timestamp": p.get("timestamp").isoformat() if p.get("timestamp") else None,
                                "lat": p.get("latitude", 0),
                                "lon": p.get("longitude", 0),
                                "pressure": p.get("pressure"),
                                "windSpeed": p.get("wind_speed"),
                                "category": p.get("category", 0),
                                "windRadii7": p.get("wind_radii_7"),
                                "windRadii10": p.get("wind_radii_10"),
                                "windRadii12": p.get("wind_radii_12"),
                                "isForecast": p.get("is_forecast", False),
                            }
                            for p in pts
                        ],
                    },
                })
        except Exception as e:
            logger.error(f"[台风] 持久化失败 {code}: {e}")

    # ────────────── 查询接口 ──────────────

    def get_active(self) -> list[dict]:
        """返回活跃台风摘要列表。"""
        result = []
        for code, data in self._active.items():
            lt = data.get("track_points", [])
            last_pt = lt[-1] if lt else {}
            result.append({
                "code": code,
                "name_cn": data.get("name_cn", ""),
                "name_en": data.get("name_en", ""),
                "category": data.get("category", 0),
                "category_name": CATEGORY_NAMES.get(data.get("category", 0), ""),
                "wind_speed": data.get("wind_speed"),
                "pressure": data.get("pressure"),
                "last_updated": last_pt.get("timestamp"),
                "move_direction": data.get("move_direction"),
                "move_speed": data.get("move_speed"),
                "track_point_count": len(lt),
                "source": data.get("source", ""),
            })
        return result

    def get_summary(self) -> str:
        active = self.get_active()
        if not active:
            return "目前无活跃台风"

        lines = [f"当前活跃台风（{len(active)}个）"]
        for t in active:
            cat = t["category_name"]
            wind = f" {t['wind_speed']}m/s" if t["wind_speed"] else ""
            pres = f" {t['pressure']}hPa" if t["pressure"] else ""
            move = ""
            if t["move_direction"]:
                move = f" {t['move_direction']}"
                if t["move_speed"]:
                    move += f" {t['move_speed']}km/h"
            name = t["name_cn"] or t["name_en"]
            time_str = ""
            if t["last_updated"]:
                time_str = t["last_updated"].strftime("%m/%d %H:%M")
            src = t.get("source", "")
            src_tag = f"[{src.upper()}]" if src else ""
            lines.append(f"  {src_tag} {name}（{t['code']}）{cat}{wind}{pres}{move} {time_str}")
        return "\n".join(lines)

    def get_event(self, code_or_name: str) -> dict | None:
        for data in self._active.values():
            if (data["code"] == code_or_name
                    or data.get("name_cn", "") == code_or_name
                    or data.get("name_en", "").lower() == code_or_name.lower()):
                return data
        return None

    def get_event_detail(self, code_or_name: str) -> str:
        """返回旧版对齐的台风详情格式（status 样式）。"""
        data = self.get_event(code_or_name)
        if data is None:
            return f"未找到台风: {code_or_name}"

        PREFIX = "‖ "
        SEP = "-" * 22
        is_jma = data.get("source") == "jma"
        src_title = "[JMA/日本气象厅 台风预警情报]" if is_jma else "[CMA/中国气象局 台风预警情报]"
        name = data.get("name_cn", "") or data.get("name_en", "") or data["code"]
        code = data["code"]
        lt = data.get("last_updated")

        def _fmt_coords(lat, lon) -> str:
            if lat is None or lon is None:
                return ""
            lat_dir = "N" if lat >= 0 else "S"
            lon_dir = "E" if lon >= 0 else "W"
            return f"{abs(lon):.2f}°{lon_dir} {abs(lat):.2f}°{lat_dir}"

        def _fmt_time(dt) -> str:
            if dt is None:
                return ""
            return dt.strftime("%Y年%m月%d日%H:%M:%S")

        lines = [src_title]
        lines.append(f"{PREFIX}台风名字：{name}（{code}）")
        lines.append(f"{PREFIX}更新原因：台风生成")
        lines.append(SEP)
        lines.append(f"{PREFIX}预报更新时间：{_fmt_time(lt)}")
        lines.append(f"{PREFIX}台风级别：{CATEGORY_NAMES.get(data.get('category', 0), '')}")
        ws = data.get("wind_speed")
        if ws is not None:
            lines.append(f"{PREFIX}风速：{ws:.0f}m/s")
        pres = data.get("pressure")
        if pres is not None:
            lines.append(f"{PREFIX}气压：{pres:.0f}hPa")
        coords = _fmt_coords(data.get("latitude"), data.get("longitude"))
        if coords:
            lines.append(f"{PREFIX}当前位置：{coords}")
        md = data.get("move_direction")
        if md:
            lines.append(f"{PREFIX}后续移向：{md}")

        # 预报点
        forecast = [p for p in data.get("track_points", []) if p.get("is_forecast")]
        if forecast:
            lines.append(SEP)
            for pt in forecast:
                ts = pt.get("timestamp")
                if ts is None:
                    continue
                lines.append(f"{PREFIX}{ts.strftime('%m/%d %H:%M')}")
                vals = []
                if pt.get("wind_speed") is not None:
                    vals.append(f"{pt['wind_speed']:.0f}m/s")
                if pt.get("pressure") is not None:
                    vals.append(f"{pt['pressure']:.0f}hPa")
                cat = CATEGORY_NAMES.get(pt.get("category", 0), "")
                if cat:
                    vals.append(cat)
                if vals:
                    lines.append(f"{PREFIX} ↳{' '.join(vals)}")

        lines.append(SEP)
        lines.append(f"{PREFIX}输入/台风 {name}以查询台风相关信息")
        return "\n".join(lines)

    def get_typhoon_event_for_render(self, code_or_name: str) -> TyphoonEvent | None:
        """获取 TyphoonEvent 领域对象（供渲染器使用）。"""
        data = self.get_event(code_or_name)
        if data is None:
            return None
        return self._to_typhoon_event(data)

    @staticmethod
    def _to_typhoon_event(data: dict) -> TyphoonEvent:
        """构建 TyphoonEvent 领域对象。"""
        track = tuple(
            TyphoonTrackPoint(
                timestamp=p.get("timestamp"),
                latitude=p.get("latitude", 0),
                longitude=p.get("longitude", 0),
                pressure=p.get("pressure"),
                wind_speed=p.get("wind_speed"),
                category=p.get("category", 0),
                beaufort=p.get("beaufort"),
                direction=p.get("direction"),
                move_speed=p.get("move_speed"),
                wind_radii_7=p.get("wind_radii_7"),
                wind_radii_10=p.get("wind_radii_10"),
                wind_radii_12=p.get("wind_radii_12"),
                is_forecast=p.get("is_forecast", False),
            )
            for p in data.get("track_points", [])
        )
        return TyphoonEvent(
            source_id=f"{data.get('source', '')}_typhoon",
            event_id=data.get("code", ""),
            name_cn=data.get("name_cn", ""),
            name_en=data.get("name_en", ""),
            code=data.get("code", ""),
            category=data.get("category", 0),
            pressure=data.get("pressure"),
            wind_speed=data.get("wind_speed"),
            latitude=data.get("latitude"),
            longitude=data.get("longitude"),
            last_updated=data.get("last_updated"),
            move_direction=data.get("move_direction"),
            move_speed=data.get("move_speed"),
            track_points=track,
            is_active=data.get("is_active", True),
            raw=data,
        )

    # ────────────── 推送 ──────────────

    async def _push_event(self, code: str, push_type: str, source_id: str) -> None:
        """推送台风事件到 pipeline。"""
        if not self._push_callback:
            return
        data = self._active.get(code)
        if data is None:
            return
        envelope = self._build_push_envelope(data, push_type, source_id)
        if envelope is None:
            return
        try:
            await self._push_callback(envelope)
            name = data.get("name_cn", "") or data.get("name_en", "") or code
            logger.info(f"[台风] 推送 {push_type}: {name} ({code})")
        except Exception as e:
            logger.error(f"[台风] 推送失败 {push_type}: {e}")

    def _build_push_envelope(
        self, data: dict, push_type: str, source_id: str,
    ) -> EventEnvelope | None:
        """构建 EventEnvelope（含 push_type metadata）。"""
        code = data.get("code", "")
        if not code:
            return None

        event = self._to_typhoon_event(data)
        # 修正 source_id（_to_typhoon_event 从 data["source"] 拼的）
        event_id = f"{source_id}_{code}_{push_type}"
        event = TyphoonEvent(
            source_id=source_id,
            event_id=event_id,
            name_cn=event.name_cn,
            name_en=event.name_en,
            code=event.code,
            category=event.category,
            pressure=event.pressure,
            wind_speed=event.wind_speed,
            latitude=event.latitude,
            longitude=event.longitude,
            last_updated=event.last_updated,
            move_direction=event.move_direction,
            move_speed=event.move_speed,
            track_points=event.track_points,
            is_active=push_type != "inactive",
            raw=data,
        )

        identity = EventIdentity(
            event_id=event_id,
            source_id=source_id,
            event_type="typhoon",
            provider_family="direct_http",
        )

        return EventEnvelope(
            identity=identity,
            event=event,
            metadata={"push_type": push_type},
        )

    def cleanup_stale(self, max_age_hours: int = 24):
        now = datetime.now()
        stale = []
        for code, data in self._active.items():
            pts = data.get("track_points", [])
            if not pts:
                stale.append(code)
                continue
            last = pts[-1]
            ts = last.get("timestamp")
            if ts is None:
                stale.append(code)
                continue
            age = (now - ts).total_seconds() / 3600
            if age > max_age_hours:
                stale.append(code)

        for code in stale:
            self._active.pop(code, None)
            self._pushed_times.pop(code, None)
            self._pushed_forecast.pop(code, None)
            for fam in self._last_seen.values():
                fam.discard(code)


# 需要 json 序列化
import json  # noqa: E402
