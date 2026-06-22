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

@ParserRegistry.register("funvisis_http")
class FunvisisParser(HttpJsonReportParser):
    """委内瑞拉地震研究基金会 (JSON)."""

    def _parse_one(self, data: dict) -> EventEnvelope | None:
        event_id = to_str(data.get("id")) or ""
        if not event_id:
            return None
        event = EarthquakeReport(
            source_id=self.source_id,
            event_id=event_id,
            occurred_at=parse_ts(data.get("time", data.get("datetime", ""))),
            latitude=to_float(data.get("lat", data.get("latitude"))),
            longitude=to_float(data.get("lon", data.get("longitude"))),
            depth=to_float(data.get("depth")),
            magnitude=to_float(data.get("mag", data.get("magnitude"))),
            place_name=str(data.get("place", data.get("location", "")) or ""),
            raw=data,
        )
        return EventEnvelope(
            identity=EventIdentity(event_id=event_id, source_id=self.source_id, event_type="earthquake"),
            event=event,
        )


# ── CENAIS (古巴) ──

@ParserRegistry.register("cenais_http")
class CenaisParser(HttpJsonReportParser):
    """古巴国家地震局 (JSON)."""

    def _parse_one(self, data: dict) -> EventEnvelope | None:
        event_id = to_str(data.get("id")) or ""
        if not event_id:
            return None
        event = EarthquakeReport(
            source_id=self.source_id,
            event_id=event_id,
            occurred_at=parse_ts(data.get("time", data.get("datetime", ""))),
            latitude=to_float(data.get("lat", data.get("latitude"))),
            longitude=to_float(data.get("lon", data.get("longitude"))),
            depth=to_float(data.get("depth")),
            magnitude=to_float(data.get("mag", data.get("magnitude"))),
            place_name=str(data.get("place", data.get("location", "")) or ""),
            raw=data,
        )
        return EventEnvelope(
            identity=EventIdentity(event_id=event_id, source_id=self.source_id, event_type="earthquake"),
            event=event,
        )


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


# ── NRCan (加拿大, XML → dict) ──

@ParserRegistry.register("nrcan_http")
class NrcanParser(BaseParser):
    """加拿大自然资源部 (XML → dict)."""

    def parse(self, raw: any) -> list[EventEnvelope] | None:
        if isinstance(raw, dict):
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
