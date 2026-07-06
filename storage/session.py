"""
storage/session.py — 会话/群组配置管理器。

群组差异配置模式：
  全局 default → 群组 override（只存差异补丁）
  运行时按群组合并得到 effective 配置。

JSON 持久化路径: data_dir/mix_astrbot_plugin_disaster_warning/group_overrides.json
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

try:
    from astrbot.api import logger
    from astrbot.api.star import StarTools
except ImportError:
    import logging as logger

    class StarTools:
        @staticmethod
        def get_data_dir(plugin_name: str) -> Path:
            return Path("data") / plugin_name


class SessionConfigManager:
    """群组配置管理器 — 全局默认 + 群组差异覆盖。"""

    OVERRIDES_FILE = "group_overrides.json"

    # 群组可覆盖的字段白名单
    ALLOWED_KEYS = {"earthquake_filters", "message_format", "enabled"}

    def __init__(self, global_config: dict[str, Any] | None = None, storage_path: str | Path | None = None):
        self.global_config = global_config or {}
        self._overrides: dict[str, dict[str, Any]] = {}
        self._storage_path_override = Path(storage_path) if storage_path else None
        self._load()

    # ── 持久化 ──

    def _storage_dir(self) -> Path:
        if self._storage_path_override:
            return self._storage_path_override
        try:
            return StarTools.get_data_dir("mix_astrbot_plugin_disaster_warning")
        except Exception:
            return Path("data") / "mix_disaster"

    def _overrides_path(self) -> Path:
        return self._storage_dir() / self.OVERRIDES_FILE

    def _load(self):
        path = self._overrides_path()
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._overrides = {
                        str(k): v for k, v in data.items() if isinstance(v, dict)
                    }
            except Exception as e:
                logger.warning(f"[Session] 读取群组覆盖失败: {e}")

    def _save(self):
        path = self._overrides_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._overrides, f, ensure_ascii=False, indent=2)
            tmp.replace(path)
        except Exception as e:
            logger.error(f"[Session] 保存群组覆盖失败: {e}")

    # ── 群组查询 ──

    def list_groups(self) -> dict[str, dict[str, Any]]:
        """返回所有群组配置（{group_id: {sessions, ...}}）。"""
        raw = self.global_config.get("groups", {})
        logger.info(f"[Session] list_groups raw type={type(raw).__name__} value={raw!r}")
        groups: dict[str, dict[str, Any]] = {}

        if isinstance(raw, list):
            # list 格式：群组名列表 → 从 group_sessions 读取会话
            _sessions_map = self.global_config.get("group_sessions", {})
            if not isinstance(_sessions_map, dict):
                _sessions_map = {}
            for gid in raw:
                gid = str(gid).strip()
                if gid:
                    ss = _sessions_map.get(gid, [])
                    groups[gid] = {"sessions": list(ss) if isinstance(ss, list) else []}
        elif isinstance(raw, dict):
            # dict 格式：直接读取
            for gid, gcfg in raw.items():
                gid = str(gid)
                groups[gid] = dict(gcfg) if isinstance(gcfg, dict) else {}
        else:
            # 兜底：target_sessions → default 群组
            sessions = self.global_config.get("target_sessions", [])
            if isinstance(sessions, list) and sessions:
                groups["default"] = {"sessions": list(sessions)}

        return groups

    def get_group_config(self, group_id: str) -> dict[str, Any]:
        """获取指定群组的完整配置（全局 + 差异覆盖合并）。"""
        all_groups = self.list_groups()
        base = dict(all_groups.get(group_id, {}))
        # 合并 override
        override = self._overrides.get(group_id, {})
        return self._deep_merge(base, override)

    def get_group_filters(self, group_id: str) -> dict[str, Any]:
        """获取指定群组的生效 earthquake_filters。"""
        # 先合并 override（优先级最高）
        merged = self._deep_merge(
            self.global_config.get("earthquake_filters", {}),
            self._overrides.get(group_id, {}).get("earthquake_filters", {}),
        )
        # 再合并群组静态配置中的 filters（如果没被 override 完全覆盖）
        all_groups = self.list_groups()
        static = all_groups.get(group_id, {})
        static_filters = static.get("earthquake_filters", {})
        if isinstance(static_filters, dict):
            merged = self._deep_merge(merged, static_filters)
        return merged

    def get_group_sleep_filters(self, group_id: str) -> dict[str, Any]:
        """获取睡眠模式阈值。

        用全局 sleep_earthquake_filters 覆盖到普通 filters 上，
        未在 sleep_earthquake_filters 中显式指定的源 → 回退到普通阈值。
        """
        normal = self.get_group_filters(group_id)
        sleep = self.global_config.get("sleep_earthquake_filters", {})
        if not isinstance(sleep, dict) or not sleep:
            return normal
        # 合并：sleep 覆盖到 normal 上（未指定源保持 normal）
        merged = self._deep_merge(normal, sleep)
        return merged

    def get_group_sessions(self, group_id: str) -> list[str]:
        """获取群组的推送会话列表。"""
        cfg = self.get_group_config(group_id)
        sessions = cfg.get("sessions", [])
        return list(sessions) if isinstance(sessions, list) else []

    # ── 差异覆盖管理 ──

    def set_group_filter(
        self,
        group_id: str,
        source_id: str,
        min_magnitude: float | None = None,
        enabled: bool | None = None,
    ) -> dict:
        """设置某个群组内某个数据源的阈值覆盖。

        Args:
            group_id: 群组ID
            source_id: 数据源ID (如 "jma_fanstudio")
            min_magnitude: 最低震级（None=不修改）
            enabled: 启用开关（None=不修改）

        Returns:
            该群组当前的完整 override 配置
        """
        override = self._overrides.get(group_id, {})
        if "earthquake_filters" not in override:
            override["earthquake_filters"] = {}
        ef = override["earthquake_filters"]
        if source_id not in ef:
            ef[source_id] = {}
        if min_magnitude is not None:
            ef[source_id]["min_magnitude"] = min_magnitude
        if enabled is not None:
            ef[source_id]["enabled"] = enabled
        self._overrides[group_id] = override
        self._save()
        return dict(override)

    def clear_group_filter(self, group_id: str, source_id: str | None = None) -> dict:
        """清除群组阈值覆盖。

        Args:
            group_id: 群组ID
            source_id: 指定数据源则只清除该源的覆盖；None 清除全部
        Returns:
            该群组当前的完整 override 配置
        """
        if source_id:
            override = self._overrides.get(group_id, {})
            ef = override.get("earthquake_filters", {})
            ef.pop(source_id, None)
            if not ef:
                override.pop("earthquake_filters", None)
            if override:
                self._overrides[group_id] = override
            else:
                self._overrides.pop(group_id, None)
        else:
            self._overrides.pop(group_id, None)
        self._save()
        return dict(self._overrides.get(group_id, {}))

    # ── 工具 ──

    @staticmethod
    def _deep_merge(base: Any, patch: Any) -> Any:
        """深合并：dict 递归合并，list/scalar 全量覆盖。"""
        if isinstance(base, dict) and isinstance(patch, dict):
            merged = copy.deepcopy(base)
            for k, v in patch.items():
                if k in merged:
                    merged[k] = SessionConfigManager._deep_merge(merged[k], v)
                else:
                    merged[k] = copy.deepcopy(v)
            return merged
        return copy.deepcopy(patch) if patch is not None else copy.deepcopy(base)

    @staticmethod
    def deep_merge(base: Any, patch: Any) -> Any:
        """公开的深合并工具方法（同 _deep_merge）。"""
        return SessionConfigManager._deep_merge(base, patch)

    def format_group_info(self, group_id: str) -> str:
        """格式化群组配置为可读文本。"""
        cfg = self.get_group_config(group_id)
        sessions = self.get_group_sessions(group_id)
        filters = self.get_group_filters(group_id)
        sleep_filters = self.get_group_sleep_filters(group_id)
        sleep_mode = group_id in (self.global_config.get("sleep_mode_groups", []) or [])

        lines = [f"📢 群组: {group_id}"]
        lines.append(f"  会话: {sessions}")
        lines.append(f"  推送达: {'开启' if cfg.get('enabled', True) else '关闭'}")
        lines.append(f"  睡眠模式: {'🌙 开启' if sleep_mode else '☀️ 关闭'}")

        if isinstance(filters, dict) and filters:
            lines.append("  阈值过滤:")
            for sid, fcfg in filters.items():
                if isinstance(fcfg, dict):
                    mag = fcfg.get("min_magnitude", "全局默认")
                    en = "✔" if fcfg.get("enabled", True) else "✘"
                    lines.append(f"    {sid}: M{mag}以上 {en}")

        if sleep_mode and isinstance(sleep_filters, dict) and sleep_filters:
            has_diff = False
            diff_lines = []
            for sid, fcfg in sleep_filters.items():
                if isinstance(fcfg, dict):
                    mag = fcfg.get("min_magnitude", "全局默认")
                    en = "✔" if fcfg.get("enabled", True) else "✘"
                    diff_lines.append(f"    {sid}: M{mag}以上 {en}")
                    # 检查是否与普通模式不同
                    normal_cfg = filters.get(sid, {})
                    normal_mag = normal_cfg.get("min_magnitude", "全局默认") if isinstance(normal_cfg, dict) else "全局默认"
                    if mag != normal_mag:
                        has_diff = True
            if has_diff or True:  # 有睡眠模式配置就显示
                lines.append("  🌙 睡眠模式阈值:")
                lines.extend(diff_lines)

        return "\n".join(lines)


__all__ = ["SessionConfigManager"]
