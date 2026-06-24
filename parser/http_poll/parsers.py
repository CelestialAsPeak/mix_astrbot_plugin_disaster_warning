"""
HTTP 轮询解析器合集（9 个数据源）。

每个解析器接收 HTTP 请求返回的 JSON 或 HTML 原始数据，
解析为统一 EarthquakeReport 领域模型。

数据源:
  - funvisis_http: 委内瑞拉 (JSON)
  - cenais_http: 古巴 (JSON)
  - csnc_http: 智利 (HTML)
  - phivolcs_http: 菲律宾 (HTML)
  - tmd_http: 泰国 (HTML)
  - geonet_http: 新西兰 (JSON, GeoJSON)
  - nrcan_http: 加拿大 (XML → dict)
  - usgs_weekly: USGS 周报 (GeoJSON)
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

try:
    from ...domain.models import EarthquakeReport, EventEnvelope, EventIdentity, SourcePayload
except ImportError:
    from domain.models import EarthquakeReport, EventEnvelope, EventIdentity, SourcePayload

try:
    from ..base import BaseParser
    from ..registry import ParserRegistry
    from ...utils.convert import to_float, to_str
    from ...utils.time import parse_ts

except ImportError:
    from parser.base import BaseParser
    from parser.registry import ParserRegistry
    from utils.convert import to_float, to_str
    from utils.time import parse_ts

class HttpJsonReportParser(BaseParser):
    """通用 HTTP JSON 地震报告解析器。"""

    def parse(self, raw: any) -> list[EventEnvelope] | None:
        if isinstance(raw, list):
            results = []
            for item in raw:
                r = self._parse_one(item)
                if r:
                    results.append(r)
            return results if results else None
        if isinstance(raw, dict):
            r = self._parse_one(raw)
            return [r] if r else None
        return None

    def _parse_one(self, data: dict) -> EventEnvelope | None:
        raise NotImplementedError


# ── FUNVISIS (委内瑞拉) ──
#
# 实际 JSON 格式: 非标准 GeoJSON FeatureCollection
#   features[].properties:
#     phone → 震级
#     phoneFormatted → 深度 (带 "km" 后缀)
#     address → 位置描述
#     city → 时间 (HH:MM)
#     postalCode → 日期 (DD-MM-YYYY)
#   features[].geometry.coordinates → [lon, lat]


@ParserRegistry.register("funvisis_http")
class FunvisisParser(BaseParser):
    """委内瑞拉地震研究基金会 (非标准 GeoJSON)."""

    VET_TZ = timezone(timedelta(hours=-4))

    def parse(self, raw: any) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None
        features = raw.get("features", [])
        if not isinstance(features, list) or not features:
            return None
        results = []
        for feat in features:
            env = self._parse_feature(feat)
            if env:
                results.append(env)
        return results if results else None

    def _parse_feature(self, feat: dict) -> EventEnvelope | None:
        props = feat.get("properties", {}) or {}
        if not isinstance(props, dict):
            return None

        magnitude = to_float(props.get("phone"))
        if magnitude is None:
            return None

        depth_str = str(props.get("phoneFormatted", "") or "")
        depth = to_float(depth_str.replace("km", "").strip())

        # 坐标: 优先 properties 中的 lat/long, 兜底 geometry.coordinates
        latitude = to_float(props.get("lat"))
        longitude = to_float(props.get("long"))
        if latitude is None or longitude is None:
            geom = feat.get("geometry", {}) or {}
            coords = geom.get("coordinates", [])
            if isinstance(coords, (list, tuple)) and len(coords) >= 2:
                longitude = to_float(coords[0])
                latitude = to_float(coords[1])
        if latitude is None or longitude is None:
            return None

        place_name = str(props.get("address", "") or "") or None

        # 时间: date=DD-MM-YYYY (postalCode), time=HH:MM (city)
        date_str = str(props.get("postalCode", "") or "")
        time_str = str(props.get("city", "") or "")
        occurred_at = self._parse_datetime(date_str, time_str)

        event_id = f"{date_str}_{time_str}_{latitude}_{longitude}".replace(" ", "_").replace(".", "_")

        event = EarthquakeReport(
            source_id=self.source_id,
            event_id=event_id,
            occurred_at=occurred_at,
            latitude=latitude,
            longitude=longitude,
            depth=depth,
            magnitude=magnitude,
            place_name=place_name,
            raw=feat,
        )
        return EventEnvelope(
            identity=EventIdentity(event_id=event_id, source_id=self.source_id, event_type="earthquake"),
            event=event,
        )

    @staticmethod
    def _parse_datetime(date_str: str, time_str: str) -> datetime | None:
        """解析日期时间。date_str: DD-MM-YYYY, time_str: HH:MM (VET, UTC-4)"""
        if not date_str or not time_str:
            return None
        try:
            parts = date_str.split("-")
            if len(parts) != 3:
                return None
            day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
            time_parts = time_str.split(":")
            if len(time_parts) != 2:
                return None
            hour, minute = int(time_parts[0]), int(time_parts[1])
            dt = datetime(year, month, day, hour, minute, tzinfo=FunvisisParser.VET_TZ)
            return dt.astimezone(timezone.utc)
        except (ValueError, TypeError):
            return None


# ── CENAIS (古巴) ──
#
# 实际 JSON 格式: JSON 数组
#   每条记录:
#     tiempoutc: UTC时间 (格式 "2026/06/06T22:55:16")
#     latitud: 纬度
#     longitud: 经度
#     profundidad: 深度 (km)
#     magnitud: 震级
#     nombre: 地名
#     provincia: 省份


@ParserRegistry.register("cenais_http")
class CenaisParser(BaseParser):
    """古巴国家地震局 (西班牙语 JSON keys)."""

    def parse(self, raw: any) -> list[EventEnvelope] | None:
        items = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else None)
        if not items:
            return None
        results = []
        for item in items:
            if not isinstance(item, dict):
                continue
            env = self._parse_item(item)
            if env:
                results.append(env)
        return results if results else None

    def _parse_item(self, item: dict) -> EventEnvelope | None:
        magnitude = to_float(item.get("magnitud"))
        if magnitude is None:
            return None

        latitude = to_float(item.get("latitud"))
        longitude = to_float(item.get("longitud"))
        if latitude is None or longitude is None:
            return None

        depth = to_float(item.get("profundidad"))
        occurred_at = self._parse_time(item.get("tiempoutc"))

        place_name = str(item.get("nombre", "") or "").strip()
        provincia = str(item.get("provincia", "") or "").strip()
        if provincia:
            place_name = f"{place_name}, {provincia}" if place_name else provincia

        event_id = ""
        if occurred_at:
            event_id = f"cenais_{occurred_at.strftime('%Y%m%d%H%M%S')}_{latitude}_{longitude}"

        event = EarthquakeReport(
            source_id=self.source_id,
            event_id=event_id,
            occurred_at=occurred_at,
            latitude=latitude,
            longitude=longitude,
            depth=depth,
            magnitude=magnitude,
            place_name=place_name if place_name else None,
            raw=item,
        )
        return EventEnvelope(
            identity=EventIdentity(event_id=event_id, source_id=self.source_id, event_type="earthquake"),
            event=event,
        )

    @staticmethod
    def _parse_time(time_str: str | None) -> datetime | None:
        if not time_str:
            return None
        try:
            dt = datetime.strptime(str(time_str).strip(), "%Y/%m/%dT%H:%M:%S")
            return dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None


# ── CSNC (智利, HTML 表格) ──

@ParserRegistry.register("csnc_http")
class CsncParser(BaseParser):
    """智利大学国家地震中心 (HTML 表格)."""
    _CHILE_TZ = "America/Santiago"

    def parse(self, raw: any) -> list[EventEnvelope] | None:
        if not isinstance(raw, str):
            return None
        try:
        # 定位 <table class="sismologia">
            table = re.search(
                r'<table\s+class="sismologia"[^>]*>(.*?)</table>',
                raw, re.DOTALL | re.IGNORECASE,
            )
            if not table:
                return None
            results = []
            for tr in re.finditer(r'<tr[^>]*>(.*?)</tr>', table.group(1), re.DOTALL):
                event = self._parse_row(tr.group(0))
                if event:
                    results.append(event)
            return results if results else None
        except Exception:
            return None

    def _parse_row(self, tr: str) -> EventEnvelope | None:
        if re.search(r'<th[> >]', tr, re.IGNORECASE):
            return None
        tds = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.DOTALL)
        if len(tds) < 3:
            return None
        td0 = tds[0].strip()
        a = re.search(r'<a\s+href\s*=\s*"([^"]*)"[^>]*>\s*([^<]+)\s*</a>', td0, re.DOTALL)
        if not a:
            return None
        time_str = a.group(2).strip()
        href = a.group(1).strip()
        rid_m = re.search(r'/(\d+)\.html', href)
        report_id = rid_m.group(1) if rid_m else ""
        br_m = re.search(r'<br\s*/?>\s*([^<]*)', td0, re.DOTALL | re.IGNORECASE)
        place = br_m.group(1).strip() if br_m else ""
        depth_m = re.search(r'([\d.]+)', tds[1].strip())
        depth = depth_m.group(1) if depth_m else "0"
        mag = tds[2].strip()
        occurred_at = self._parse_chile_time(time_str)
        if not occurred_at:
            return None
        eid = report_id or f"csnc_{occurred_at.strftime('%Y%m%d%H%M%S')}"
        event = EarthquakeReport(
            source_id=self.source_id, event_id=eid,
            occurred_at=occurred_at, depth=to_float(depth),
            magnitude=to_float(mag), place_name=place, raw={"report_url": href},
        )
        return EventEnvelope(
            identity=EventIdentity(event_id=eid, source_id=self.source_id, event_type="earthquake"),
            event=event,
        )

    @staticmethod
    def _parse_chile_time(s: str):
        try:
            from zoneinfo import ZoneInfo
            return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("America/Santiago"))
        except Exception:
            try:
                naive = datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S")
                off = -4 if 4 <= naive.month <= 9 else -3
                return naive.replace(tzinfo=timezone(timedelta(hours=off)))
            except Exception:
                return None


# ── PHIVOLCS (菲律宾, HTML 表格) ──

_MONTHS = { 'january':1,'february':2,'march':3,'april':4,'may':5,'june':6,
            'july':7,'august':8,'september':9,'october':10,'november':11,'december':12 }
_TZ_PH = timezone(timedelta(hours=8))

@ParserRegistry.register("phivolcs_http")
class PhivolcsParser(BaseParser):
    """菲律宾火山地震研究所 (HTML 表格)."""

    def parse(self, raw: any) -> list[EventEnvelope] | None:
        if not isinstance(raw, str):
            return None
        try:
            results = []
            for tr in re.finditer(
                r'<tr[^>]*>.*?<a\s+href\s*=\s*"([^"]*Earthquake_Information[^"]*)"[^>]*>.*?</tr>',
                raw, re.DOTALL | re.IGNORECASE,
            ):
                event = self._parse_row(tr.group(0))
                if event:
                    results.append(event)
            return results if results else None
        except Exception:
            return None

    def _parse_row(self, tr: str) -> EventEnvelope | None:
        tds = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.DOTALL)
        if len(tds) < 6:
            return None
        a = re.search(
            r'<a\s+href\s*=\s*"([^"]*)"[^>]*>\s*<span[^>]*>\s*([^<]+)\s*</span>',
            tds[0], re.DOTALL | re.IGNORECASE,
        )
        if not a:
            return None
        time_str = a.group(2).strip()
        href = a.group(1).strip()
        rid_m = re.search(r'(\d{8}_\d{4})', href)
        report_id = rid_m.group(1) if rid_m else ""
        lat = to_float(re.sub(r'<[^>]+>', '', tds[1]).strip())
        lon = to_float(re.sub(r'<[^>]+>', '', tds[2]).strip())
        depth_m = re.search(r'([\d.]+)', re.sub(r'<[^>]+>', '', tds[3]).strip())
        mag_m = re.search(r'([\d.]+)', re.sub(r'<[^>]+>', '', tds[4]).strip())
        loc = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', tds[5])).strip()
        occurred_at = self._parse_ph_time(time_str)
        if not occurred_at:
            return None
        eid = report_id or f"phivolcs_{occurred_at.strftime('%Y%m%d%H%M%S')}"
        event = EarthquakeReport(
            source_id=self.source_id, event_id=eid,
            occurred_at=occurred_at, latitude=lat, longitude=lon,
            depth=to_float(depth_m.group(1) if depth_m else "0"),
            magnitude=to_float(mag_m.group(1) if mag_m else "0"),
            place_name=loc, raw={"report_url": href},
        )
        return EventEnvelope(
            identity=EventIdentity(event_id=eid, source_id=self.source_id, event_type="earthquake"),
            event=event,
        )

    @staticmethod
    def _parse_ph_time(s: str):
        if not s:
            return None
        m = re.match(r'(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\s*-\s*(\d{1,2}):(\d{2})\s*(AM|PM)', s.strip())
        if not m:
            return None
        mon = _MONTHS.get(m.group(2).lower())
        if not mon:
            return None
        h = int(m.group(4))
        if m.group(6).upper() == 'PM' and h != 12:
            h += 12
        elif m.group(6).upper() == 'AM' and h == 12:
            h = 0
        return datetime(int(m.group(3)), mon, int(m.group(1)), h, int(m.group(5)), tzinfo=_TZ_PH)


# ── TMD (泰国, HTML 表格) ──

_TZ_THAI = timezone(timedelta(hours=7))

@ParserRegistry.register("tmd_http")
class TmdParser(BaseParser):
    """泰国气象局地震监测中心 (HTML 表格)."""

    def parse(self, raw: any) -> list[EventEnvelope] | None:
        if not isinstance(raw, str):
            return None
        try:
            results = []
            for tr in re.finditer(
                r'<tr[^>]*onclick="[^"]*earthquake=(\d+)[^"]*"[^>]*>(.*?)</tr>',
                raw, re.DOTALL | re.IGNORECASE,
            ):
                event = self._parse_row(tr.group(0), tr.group(1))
                if event:
                    results.append(event)
            return results if results else None
        except Exception:
            return None

    def _parse_row(self, tr: str, report_id: str) -> EventEnvelope | None:
        if 'colspan' in tr:
            return None
        tds = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.DOTALL)
        if len(tds) < 6:
            return None
        t_m = re.search(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', tds[0])
        if not t_m:
            return None
        time_str = t_m.group(1)
        mag_m = re.search(r'([\d.]+)', tds[1].strip())
        mag = mag_m.group(1) if mag_m else "0"
        lat_raw = tds[2].strip()
        lat_v = to_float(re.search(r'([\d.]+)', lat_raw).group(1)) if re.search(r'([\d.]+)', lat_raw) else None
        if lat_v is not None and "S" in lat_raw:
            lat_v = -lat_v
        lon_raw = tds[3].strip()
        lon_v = to_float(re.search(r'([\d.]+)', lon_raw).group(1)) if re.search(r'([\d.]+)', lon_raw) else None
        if lon_v is not None and "W" in lon_raw:
            lon_v = -lon_v
        depth_m = re.search(r'([\d.]+)', tds[4].strip())
        depth = depth_m.group(1) if depth_m else "0"
        fonts = re.findall(r'<font[^>]*>\s*(.*?)\s*</font>', tds[5], re.DOTALL)
        region_en = re.sub(r'<[^>]+>', '', fonts[1]).strip() if len(fonts) >= 2 else ""
        try:
            occurred_at = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_TZ_THAI)
        except Exception:
            return None
        eid = report_id or f"tmd_{occurred_at.strftime('%Y%m%d%H%M%S')}"
        event = EarthquakeReport(
            source_id=self.source_id, event_id=eid,
            occurred_at=occurred_at, latitude=lat_v, longitude=lon_v,
            depth=to_float(depth), magnitude=to_float(mag),
            place_name=region_en, raw={"report_id": report_id},
        )
        return EventEnvelope(
            identity=EventIdentity(event_id=eid, source_id=self.source_id, event_type="earthquake"),
            event=event,
        )


# ── GeoNet (新西兰, GeoJSON) ──

@ParserRegistry.register("geonet_http")
class GeonetParser(BaseParser):
    """新西兰 GeoNet (GeoJSON)."""

    def parse(self, raw: any) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None
        features = raw.get("features", [])
        if not isinstance(features, list):
            return None
        results = []
        for feat in features:
            props = feat.get("properties", {})
            geo = feat.get("geometry", {})
            coords = geo.get("coordinates", []) if isinstance(geo, dict) else []

            event_id = to_str(props.get("publicID")) or to_str(props.get("id")) or ""
            if not event_id:
                continue

            event = EarthquakeReport(
                source_id=self.source_id,
                event_id=event_id,
                occurred_at=parse_ts(props.get("time", props.get("origintime", ""))),
                latitude=to_float(coords[1]) if len(coords) > 1 else None,
                longitude=to_float(coords[0]) if len(coords) > 0 else None,
                depth=to_float(coords[2]) if len(coords) > 2 else to_float(props.get("depth")),
                magnitude=to_float(props.get("magnitude", props.get("mag"))),
                place_name=str(props.get("locality", props.get("place", "")) or ""),
                region=str(props.get("region", "") or ""),
                mmi=to_float(props.get("mmi")),
                raw=props,
            )
            results.append(EventEnvelope(
                identity=EventIdentity(event_id=event_id, source_id=self.source_id, event_type="earthquake"),
                event=event,
            ))
        return results if results else None


# ── NRCan (加拿大, QuakeML XML) ──
#
# 数据源返回 QuakeML 1.2 XML 格式
# 命名空间: http://quakeml.org/xmlns/bed/1.2


@ParserRegistry.register("nrcan_http")
class NrcanParser(BaseParser):
    """加拿大自然资源部 (QuakeML XML)."""

    QUAKEML_NS = "http://quakeml.org/xmlns/bed/1.2"

    def parse(self, raw: any) -> list[EventEnvelope] | None:
        if isinstance(raw, dict):
            # 向后兼容 dict 输入
            return self._parse_dict(raw)
        if isinstance(raw, str):
            return self._parse_xml(raw)
        return None

    def _parse_dict(self, raw: dict) -> list[EventEnvelope] | None:
        entries = raw.get("entries", raw.get("features", [raw]))
        if not isinstance(entries, list):
            entries = [raw]
        results = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            event_id = to_str(entry.get("id")) or hashlib.md5(str(entry).encode()).hexdigest()[:12]
            event = EarthquakeReport(
                source_id=self.source_id,
                event_id=event_id,
                occurred_at=parse_ts(entry.get("time", entry.get("datetime", ""))),
                latitude=to_float(entry.get("lat", entry.get("latitude"))),
                longitude=to_float(entry.get("lon", entry.get("longitude"))),
                depth=to_float(entry.get("depth")),
                magnitude=to_float(entry.get("mag", entry.get("magnitude"))),
                place_name=str(entry.get("place", entry.get("location", "")) or ""),
                region=str(entry.get("region", "") or ""),
                raw=entry,
            )
            results.append(EventEnvelope(
                identity=EventIdentity(event_id=event_id, source_id=self.source_id, event_type="earthquake"),
                event=event,
            ))
        return results if results else None

    def _parse_xml(self, raw: str) -> list[EventEnvelope] | None:
        """解析 QuakeML XML 文本。"""
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return None

        ns = {"q": self.QUAKEML_NS}
        results = []

        for event_elem in root.findall(".//q:event", ns):
            env = self._parse_xml_event(event_elem, ns)
            if env:
                results.append(env)

        return results if results else None

    def _parse_xml_event(self, elem: ET.Element, ns: dict) -> EventEnvelope | None:
        raw_event_id = elem.get("publicID", "")
        if not raw_event_id:
            return None
        event_id = raw_event_id.split("/")[-1] if "/" in raw_event_id else raw_event_id

        event_type = elem.findtext("q:type", "", ns)
        if event_type not in ("earthquake", ""):
            return None

        # 地名
        desc = elem.find("q:description", ns)
        place_name = ""
        if desc is not None:
            place_name = desc.findtext("q:text", "", ns).strip()

        # 震源
        origin = elem.find("q:origin", ns)
        if origin is None:
            return None

        occurred_at = self._parse_xml_time(origin.find("q:time/q:value", ns))
        latitude = self._parse_xml_float(origin.find("q:latitude/q:value", ns))
        longitude = self._parse_xml_float(origin.find("q:longitude/q:value", ns))
        depth_m = self._parse_xml_float(origin.find("q:depth/q:value", ns))
        depth_km = depth_m / 1000.0 if depth_m is not None else None

        # 震级
        mag_elem = elem.find("q:magnitude", ns)
        magnitude = None
        if mag_elem is not None:
            magnitude = self._parse_xml_float(mag_elem.find("q:mag/q:value", ns))

        if magnitude is None:
            return None

        event = EarthquakeReport(
            source_id=self.source_id,
            event_id=event_id,
            occurred_at=occurred_at,
            latitude=latitude,
            longitude=longitude,
            depth=depth_km,
            magnitude=magnitude,
            place_name=place_name or None,
            raw={"publicID": raw_event_id},
        )
        return EventEnvelope(
            identity=EventIdentity(event_id=event_id, source_id=self.source_id, event_type="earthquake"),
            event=event,
        )

    @staticmethod
    def _parse_xml_time(time_elem: ET.Element | None) -> datetime | None:
        if time_elem is None or not time_elem.text:
            return None
        try:
            ts = time_elem.text.strip()
            if ts.endswith("Z"):
                ts = ts[:-1] + "+00:00"
            return datetime.fromisoformat(ts).astimezone(timezone.utc)
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _parse_xml_float(elem: ET.Element | None) -> float | None:
        if elem is None or not elem.text:
            return None
        try:
            return float(elem.text.strip())
        except (ValueError, TypeError):
            return None


# ── USGS Weekly (GeoJSON) ──

@ParserRegistry.register("usgs_weekly")
class UsgsWeeklyParser(BaseParser):
    """USGS 周报 (GeoJSON)."""

    def parse(self, raw: any) -> list[EventEnvelope] | None:
        if not isinstance(raw, dict):
            return None
        features = raw.get("features", [])
        if not isinstance(features, list):
            return None
        results = []
        for feat in features:
            props = feat.get("properties", {})
            geo = feat.get("geometry", {})
            coords = geo.get("coordinates", []) if isinstance(geo, dict) else []

            event_id = to_str(props.get("code")) or to_str(props.get("id")) or ""
            if not event_id:
                continue

            event = EarthquakeReport(
                source_id=self.source_id,
                event_id=event_id,
                occurred_at=parse_ts(props.get("time", props.get("origintime", ""))),
                latitude=to_float(coords[1]) if len(coords) > 1 else None,
                longitude=to_float(coords[0]) if len(coords) > 0 else None,
                depth=to_float(coords[2]) if len(coords) > 2 else to_float(props.get("depth")),
                magnitude=to_float(props.get("mag", props.get("magnitude"))),
                place_name=str(props.get("place", props.get("location", "")) or ""),
                url=to_str(props.get("url")),
                mmi=to_float(props.get("mmi")),
                alert_level=to_str(props.get("alert")),
                status=to_str(props.get("status")),
                raw=props,
            )
            results.append(EventEnvelope(
                identity=EventIdentity(event_id=event_id, source_id=self.source_id, event_type="earthquake"),
                event=event,
            ))
        return results if results else None
