"""
storage/database.py — SQLite 数据库管理器（aiosqlite）。

负责事件历史的建库、写入、查询。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger

try:
    import aiosqlite
except ImportError:
    aiosqlite = None


class DatabaseManager:
    """异步 SQLite 数据库管理器。"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn: Any = None

    async def initialize(self):
        """初始化数据库。"""
        if aiosqlite is None:
            logger.warning("[DB] aiosqlite 未安装，数据库功能不可用")
            return
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = await aiosqlite.connect(str(self.db_path), timeout=30)
            self.conn.row_factory = aiosqlite.Row
            await self._ensure_schema()
            logger.info(f"[DB] 初始化完成: {self.db_path}")
        except Exception as e:
            logger.error(f"[DB] 初始化失败: {e}")

    async def _ensure_schema(self):
        """确保表结构存在，自动迁移旧版本。"""
        cursor = await self.conn.cursor()
        # 建表（不变更已有表）
        await cursor.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                real_event_id TEXT,
                source TEXT NOT NULL,
                type TEXT NOT NULL,
                magnitude REAL,
                depth REAL,
                latitude REAL,
                longitude REAL,
                place_name TEXT,
                description TEXT,
                subtitle TEXT,
                level TEXT,
                time TEXT,
                report_num INTEGER DEFAULT 1,
                is_major INTEGER DEFAULT 0,
                weather_type_code TEXT,
                info_type TEXT,
                raw_json TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        # 自动迁移：检查旧版列名，补齐新列
        await cursor.execute("PRAGMA table_info(events)")
        existing = {row[1] for row in await cursor.fetchall()}
        needed = {
            "real_event_id": "TEXT",
            "source": "TEXT NOT NULL DEFAULT ''",
            "type": "TEXT NOT NULL DEFAULT ''",
            "description": "TEXT",
            "subtitle": "TEXT",
            "level": "TEXT",
            "time": "TEXT",
            "report_num": "INTEGER DEFAULT 1",
            "is_major": "INTEGER DEFAULT 0",
            "weather_type_code": "TEXT",
            "info_type": "TEXT",
        }
        for col, dtype in needed.items():
            if col not in existing:
                try:
                    await cursor.execute(f"ALTER TABLE events ADD COLUMN {col} {dtype}")
                    logger.info(f"[DB] 迁移: 添加列 {col}")
                except Exception as e:
                    logger.debug(f"[DB] 迁移: 添加列 {col} 失败（可能已存在）: {e}")

        # 旧列名兼容：将旧数据映射到新列
        if "event_id" in existing and "real_event_id" not in existing:
            try:
                await cursor.execute("UPDATE events SET real_event_id=event_id WHERE real_event_id IS NULL")
            except Exception:
                pass
        if "source_id" in existing and "source" not in existing:
            try:
                await cursor.execute("UPDATE events SET source=source_id WHERE source IS NULL OR source=''")
            except Exception:
                pass
        if "event_type" in existing and "type" not in existing:
            try:
                await cursor.execute("UPDATE events SET type=event_type WHERE type IS NULL OR type=''")
            except Exception:
                pass
        if "occurred_at" in existing and "time" not in existing:
            try:
                await cursor.execute("UPDATE events SET time=occurred_at WHERE time IS NULL")
            except Exception:
                pass

        await cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_source ON events(source, created_at)
        """)
        await self.conn.commit()

    async def insert_event(self, event_data: dict) -> int | None:
        """插入事件记录（兼容新旧列名）。"""
        if not self.conn:
            return None
        try:
            cursor = await self.conn.cursor()
            import json
            # 先尝试全量写入（含旧列名，兼容迁移后的表）
            try:
                await cursor.execute("""
                    INSERT INTO events (real_event_id, event_id, source, source_id,
                        type, event_type, magnitude, depth, latitude, longitude,
                        place_name, description, subtitle, level, time, occurred_at,
                        report_num, is_major, weather_type_code, info_type, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    event_data.get("real_event_id", event_data.get("event_id", "")),
                    event_data.get("event_id", event_data.get("real_event_id", "")),
                    event_data.get("source", event_data.get("source_id", "")),
                    event_data.get("source_id", event_data.get("source", "")),
                    event_data.get("type", event_data.get("event_type", "")),
                    event_data.get("event_type", event_data.get("type", "")),
                    event_data.get("magnitude"),
                    event_data.get("depth"),
                    event_data.get("latitude"),
                    event_data.get("longitude"),
                    event_data.get("place_name", ""),
                    event_data.get("description", ""),
                    event_data.get("subtitle", ""),
                    event_data.get("level", ""),
                    event_data.get("time", event_data.get("occurred_at", "")),
                    event_data.get("occurred_at", event_data.get("time", "")),
                    event_data.get("report_num", 1),
                    1 if (event_data.get("magnitude") or 0) >= 6.0 else 0,
                    event_data.get("weather_type_code", ""),
                    event_data.get("info_type", ""),
                    json.dumps(event_data.get("raw", {}), ensure_ascii=False),
                ))
            except Exception as e:
                err = str(e)
                # 新表没有旧列名 → 降级为只写新列名
                if "no such column" in err and any(
                    c in err for c in ("event_id", "source_id", "event_type", "occurred_at")
                ):
                    await cursor.execute("""
                        INSERT INTO events (real_event_id, source, type, magnitude,
                            depth, latitude, longitude, place_name, description,
                            subtitle, level, time, report_num, is_major,
                            weather_type_code, info_type, raw_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        event_data.get("real_event_id", event_data.get("event_id", "")),
                        event_data.get("source", event_data.get("source_id", "")),
                        event_data.get("type", event_data.get("event_type", "")),
                        event_data.get("magnitude"),
                        event_data.get("depth"),
                        event_data.get("latitude"),
                        event_data.get("longitude"),
                        event_data.get("place_name", ""),
                        event_data.get("description", ""),
                        event_data.get("subtitle", ""),
                        event_data.get("level", ""),
                        event_data.get("time", event_data.get("occurred_at", "")),
                        event_data.get("report_num", 1),
                        1 if (event_data.get("magnitude") or 0) >= 6.0 else 0,
                        event_data.get("weather_type_code", ""),
                        event_data.get("info_type", ""),
                        json.dumps(event_data.get("raw", {}), ensure_ascii=False),
                    ))
                else:
                    raise
            await self.conn.commit()
            return cursor.lastrowid
        except Exception as e:
            logger.error(f"[DB] insert_event 失败: {e}")
            return None

    async def insert_envelope(self, envelope) -> int | None:
        """从 EventEnvelope 提取数据并入库。"""
        if not self.conn:
            return None
        ev = envelope.event
        identity = envelope.identity
        occurred_at = ev.occurred_at
        time_str = occurred_at.isoformat() if hasattr(occurred_at, "isoformat") else str(occurred_at or "")
        return await self.insert_event({
            "real_event_id": identity.event_id,
            "source": identity.source_id,
            "type": identity.event_type,
            "magnitude": getattr(ev, "magnitude", None),
            "depth": getattr(ev, "depth", None),
            "latitude": getattr(ev, "latitude", None),
            "longitude": getattr(ev, "longitude", None),
            "place_name": getattr(ev, "place_name", None),
            "description": getattr(ev, "description", getattr(ev, "headline", "")),
            "subtitle": getattr(ev, "alert_title", getattr(ev, "title", "")),
            "level": getattr(ev, "alert_level", getattr(ev, "max_intensity", "")),
            "time": time_str,
            "report_num": identity.report_num or getattr(ev, "report_num", None),
            "weather_type_code": getattr(ev, "alert_type", ""),
            "info_type": identity.event_type,
            "raw": getattr(ev, "raw", {}),
        })

    async def query_events(
        self,
        source_id: str | None = None,
        limit: int = 20,
        days: int | None = None,
        event_type: str | None = None,
    ) -> list[dict]:
        """通用事件查询。

        Args:
            source_id: 按数据源过滤（对应 `source` 列）
            limit: 返回条数上限
            days: 只查最近 N 天（基于 created_at）
            event_type: 按类型过滤（`type` 列）
        """
        if not self.conn:
            return []
        try:
            cursor = await self.conn.cursor()
            where = []
            params: list[Any] = []
            if source_id:
                where.append("source=?")
                params.append(source_id)
            if event_type:
                where.append("type=?")
                params.append(event_type)
            if days is not None:
                where.append(f"created_at >= datetime('now', 'localtime', '-{days} days')")
            sql = "SELECT * FROM events"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            await cursor.execute(sql, params)
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"[DB] query_events 失败: {e}")
            return []

    async def query_earthquakes(
        self, source_id: str, limit: int = 10, days: int = 7
    ) -> list[dict]:
        """查询地震事件（带 magnitude 过滤）。"""
        if not self.conn:
            return []
        try:
            cursor = await self.conn.cursor()
            modifier = f"-{days} days"
            await cursor.execute("""
                SELECT real_event_id, source, type, magnitude, place_name,
                       time, depth, latitude, longitude, description,
                       report_num, level, raw_json, subtitle
                FROM events
                WHERE source=? AND type IN ('earthquake','earthquake_info')
                  AND magnitude IS NOT NULL
                  AND created_at >= datetime('now', 'localtime', ?)
                ORDER BY id DESC LIMIT ?
            """, (source_id, modifier, limit))
            return [dict(r) for r in await cursor.fetchall()]
        except Exception as e:
            logger.error(f"[DB] query_earthquakes 失败: {e}")
            return []

    async def query_eew(self, source_id: str, limit: int = 10) -> list[dict]:
        """查询 EEW 事件。"""
        if not self.conn:
            return []
        try:
            cursor = await self.conn.cursor()
            await cursor.execute("""
                SELECT real_event_id, source, type, magnitude, place_name,
                       time, depth, report_num, description, raw_json
                FROM events
                WHERE source=? AND type IN ('earthquake_warning', 'eew')
                ORDER BY id DESC LIMIT ?
            """, (source_id, limit))
            return [dict(r) for r in await cursor.fetchall()]
        except Exception as e:
            logger.error(f"[DB] query_eew 失败: {e}")
            return []

    async def query_weather(
        self, province: str = "", limit: int = 20
    ) -> list[dict]:
        """查询气象预警。"""
        if not self.conn:
            return []
        try:
            cursor = await self.conn.cursor()
            if province and province != "全国":
                like = f"%{province}%"
                await cursor.execute("""
                    SELECT real_event_id, source, type, description, subtitle,
                           place_name, level, time, weather_type_code, info_type
                    FROM events WHERE type='weather_alarm'
                      AND (place_name LIKE ? OR description LIKE ?)
                    ORDER BY id DESC LIMIT ?
                """, (like, like, limit))
            else:
                await cursor.execute("""
                    SELECT real_event_id, source, type, description, subtitle,
                           place_name, level, time, weather_type_code, info_type
                    FROM events WHERE type='weather_alarm'
                    ORDER BY id DESC LIMIT ?
                """, (limit,))
            return [dict(r) for r in await cursor.fetchall()]
        except Exception as e:
            logger.error(f"[DB] query_weather 失败: {e}")
            return []

    async def query_typhoon(self, code: str = "") -> list[dict]:
        """查询台风。"""
        if not self.conn:
            return []
        try:
            cursor = await self.conn.cursor()
            if code:
                like = f"%{code}%"
                await cursor.execute("""
                    SELECT real_event_id, source, type, description, subtitle,
                           place_name, magnitude, time, raw_json
                    FROM events WHERE type='typhoon'
                      AND (real_event_id LIKE ? OR description LIKE ?)
                    ORDER BY id DESC LIMIT 5
                """, (like, like))
            else:
                await cursor.execute("""
                    SELECT real_event_id, source, type, description, subtitle,
                           place_name, magnitude, time, raw_json
                    FROM events WHERE type='typhoon'
                    ORDER BY id DESC LIMIT 5
                """)
            return [dict(r) for r in await cursor.fetchall()]
        except Exception as e:
            logger.error(f"[DB] query_typhoon 失败: {e}")
            return []

    async def execute_raw(self, sql: str, params: tuple = ()) -> list[dict]:
        """执行原始 SQL 查询（通用）。"""
        if not self.conn:
            return []
        try:
            cursor = await self.conn.cursor()
            await cursor.execute(sql, params)
            return [dict(r) for r in await cursor.fetchall()]
        except Exception as e:
            logger.error(f"[DB] execute_raw 失败: {e}")
            return []

    async def close(self):
        """关闭数据库。"""
        if self.conn:
            try:
                await self.conn.close()
            except Exception:
                pass
            self.conn = None
