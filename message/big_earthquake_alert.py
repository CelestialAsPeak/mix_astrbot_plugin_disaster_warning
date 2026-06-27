"""
message/big_earthquake_alert.py — 大地震大喇叭提醒服务。

当高震级 EEW（P2P 556 等）通过推送时，向所有目标会话连续发送 N 次提醒消息。
同一源同一地震只触发一次（P0）。

触发条件（可配置）:
  - CEA 中国地震预警网/省级融合源: M ≥ 5.0（含新疆 ≥ 6.0）
  - JMA: M ≥ 6.0
  - CWA: M ≥ 5.5
  - GlobalQuake: M ≥ 7.0
"""

from __future__ import annotations

try:
    from astrbot.api import logger
except ImportError:
    import logging as logger

try:
    from ..domain.models import EventEnvelope, EewEvent
except ImportError:
    from domain.models import EventEnvelope, EewEvent


class BigEarthquakeAlertService:
    """大地震大喇叭提醒服务。

    用法:
        service = BigEarthquakeAlertService(config)
        if service.should_alert(envelope):
            for _ in range(service.get_alert_count()):
                await sender.send(session_id, service.get_alert_message())
    """

    # 数据源 → 源组映射（用于读取对应阈值）
    _SOURCE_GROUPS: dict[str, str] = {
        # CEA 中国地震预警网
        "cea_fanstudio": "cea",
        "cea_pr_fanstudio": "cea",
        "cenc_wolfx": "cea",
        # JMA EEW
        "jma_p2p": "jma",
        "jma_p2p_http": "jma",
        "jma_fanstudio": "jma",
        "jma_wolfx": "jma",
        "jma_wolfx_http": "jma",
        # CWA EEW
        "cwa_fanstudio": "cwa",
        "cwa_wolfx": "cwa",
        # GlobalQuake
        "global_quake": "gq",
    }

    def __init__(self, config: dict):
        self.config = config
        # 去重缓存：key=f"{source_id}|{event_id}" → True
        self._triggered: dict[str, bool] = {}
        self._max_cache = 500

    def should_alert(self, envelope: EventEnvelope) -> bool:
        """判断是否应触发大地震提醒。

        Returns:
            True 表示需要触发（首次、震级达标）。调用后内部去重标记已触发。
        """
        alert_cfg = self.config.get("big_earthquake_alert", {})
        if not alert_cfg.get("enabled", True):
            return False

        # 只对已配置的 EEW 源触发
        source_id = envelope.source_id
        group = self._SOURCE_GROUPS.get(source_id)
        if group is None:
            return False

        # 只对 EEW 事件
        if not isinstance(envelope.event, EewEvent):
            return False

        # 读该组阈值
        thresholds = alert_cfg.get("thresholds", {})
        if not isinstance(thresholds, dict):
            return False
        threshold = thresholds.get(group)
        if threshold is None or threshold <= 0:
            return False

        mag = envelope.event.magnitude
        if mag is None or mag < 0:
            return False

        # CEA 含新疆的特殊阈值
        if group == "cea":
            place = (envelope.event.place_name or "")
            if "新疆" in place:
                xj_threshold = thresholds.get("cea_xinjiang", threshold + 1)
                if mag < xj_threshold:
                    return False

        # 通用震级阈值检查
        if mag < threshold:
            return False

        # ── 去重检查：同一源同一地震只触发一次 ──
        dedup_key = f"{source_id}|{envelope.identity.event_id}"
        if dedup_key in self._triggered:
            logger.debug(f"[BigAlert] 已触发过: {dedup_key}，跳过")
            return False

        self._triggered[dedup_key] = True
        # 控制缓存大小（LRU 近似）
        if len(self._triggered) > self._max_cache:
            stale_keys = list(self._triggered.keys())[:-(self._max_cache // 2)]
            for k in stale_keys:
                self._triggered.pop(k, None)

        logger.info(f"[BigAlert] 🚨 大地震大喇叭触发: {source_id} M{mag} threshold={threshold}")
        return True

    def get_alert_message(self) -> str:
        """获取大喇叭提醒文本。"""
        alert_cfg = self.config.get("big_earthquake_alert", {})
        return str(alert_cfg.get("alert_text", "我草大大大喵！\n我草大大大喵！"))

    def get_alert_count(self) -> int:
        """获取连续发送次数。"""
        alert_cfg = self.config.get("big_earthquake_alert", {})
        try:
            return max(1, int(alert_cfg.get("alert_count", 5)))
        except (TypeError, ValueError):
            return 5
