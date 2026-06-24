"""
mix_astrbot_plugin_disaster_warning — 领域模型定义。

所有模型使用 @dataclass(frozen=True) 保证不可变，
source_id / event_id 为必填字段，
可选字段默认 None（区分"无数据"和"实际零值"），
raw 保留完整原始数据供调试与回溯。

命名规范：
  - 事件类型（系统内部触发）: EarthquakeEvent, TsunamiEvent, etc.
  - 数据载体: EarthquakeReport = 震后报告（已测定）
  - EewEvent = 地震早期预警（实时速报）

  mix_dev/
  ├── .git/
  ├── LICENSE
  ├── README.md
  ├── metadata.yaml
  ├── .gitignore
  ├── domain/
  │   ├── __init__.py
  │   └── models.py          ← 全部领域模型
  ├── config/
  │   ├── __init__.py
  │   ├── sources.json        ← 44 个数据源
  │   ├── defaults.json       ← 运行时默认值
  │   ├── loader.py
  │   ├── accessor.py
  │   └── validator.py
  ├── utils/
  │   ├── __init__.py
  │   ├── time.py
  │   ├── geo.py
  │   ├── convert.py
  │   └── telemetry.py

6.22 第一轮代码审查
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# ─────────────────────── 事件身份 ───────────────────────


@dataclass(frozen=True)
class EventIdentity:
    """事件身份标识 — 唯一确定一个事件在系统中的来源与业务身份。

    用于去重、路由、日志追踪和展示层索引。
    """

    event_id: str                  # 事件业务主标识（来源唯一）
    source_id: str                 # 数据源 ID（对应 sources.json 中的 source_id）
    event_type: str                # "earthquake" / "eew" / "tsunami" / "weather" / "typhoon"
    provider_family: str = ""      # 供应商家族（如 "fan_studio" / "wolfx" / "p2p"）
    report_num: int | None = None  # 第几报（EEW 的 serial / updates）
    published_at: datetime | None = None  # 发布时间（来源时区）
    is_final: bool = False         # 是否为终报
    is_cancel: bool = False        # 是否为取消报

    @property
    def unique_key(self) -> str:
        """统一去重键 — 用于去重索引和缓存键。"""
        report = f"|r{self.report_num}" if self.report_num is not None else ""
        return f"{self.source_id}|{self.event_type}|{self.event_id}{report}"


@dataclass(frozen=True)
class SourcePayload:
    """原始载荷包装 — 保留解析前的完整数据供日志和回溯。"""

    source_id: str
    raw: dict = field(default_factory=dict)
    provider_family: str = ""
    message_type: str = ""


# ─────────────────────── 地震相关 ───────────────────────


@dataclass(frozen=True)
class EarthquakeEvent:
    """地震基础事件 — 所有地震相关事件的基类信息集。"""

    source_id: str                 # 数据源 ID
    event_id: str                  # 事件唯一标识
    occurred_at: datetime | None = None   # 发震时间（UTC）
    latitude: float | None = None
    longitude: float | None = None
    depth: float | None = None     # 深度（公里）
    magnitude: float | None = None
    magnitude_type: str | None = None  # "M"/"Mj"/"Ml"/"Mw" 等
    place_name: str | None = None  # 震中地名
    region: str | None = None      # 区域描述
    is_sea: bool | None = None     # 是否海域
    is_final: bool | None = None   # 是否为终报
    is_cancel: bool | None = None  # 是否为取消报
    report_num: int | None = None  # 第几报
    accuracy: dict | None = None   # 精度信息（来源特定）
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EewEvent:
    """地震早期预警（EEW）— 实时速报。

    来源: Wolfx, P2P (code 556), FAN Studio (type="eew"), GlobalQuake
    """

    source_id: str
    event_id: str
    occurred_at: datetime | None = None
    latitude: float | None = None
    longitude: float | None = None
    depth: float | None = None
    magnitude: float | None = None
    magnitude_type: str | None = None
    place_name: str | None = None
    region: str | None = None
    is_sea: bool | None = None
    is_final: bool | None = None
    is_cancel: bool | None = None
    report_num: int | None = None
    max_intensity: str | None = None   # 最大震度（JMA 震度等级或 MMI）
    announced_time: datetime | None = None  # 发布时间（UTC）
    is_warn: bool | None = None        # 是否发布警报
    serial: int | None = None          # 报次序号
    province: str | None = None        # 省份（CEA 省级融合源专用）
    warn_areas: list | None = None     # 预警区域列
    accuracy: dict | None = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EarthquakeReport:
    """地震震后报告 — 正式测定情报。

    来源: USGS (GeoJSON), P2P (code 551), CENC, JMA, FAN -> FSSN BCSF HKO.....
    """

    source_id: str
    event_id: str
    occurred_at: datetime | None = None
    latitude: float | None = None
    longitude: float | None = None
    depth: float | None = None
    magnitude: float | None = None
    magnitude_type: str | None = None
    place_name: str | None = None
    region: str | None = None
    is_sea: bool | None = None
    is_final: bool | None = None
    is_cancel: bool | None = None
    report_num: int | None = None
    url: str | None = None             # 详情页 URL（USGS 使用）
    tsunami_warning: bool | None = None  # 是否引发海啸预警
    alert_level: str | None = None     # USGS: "green"/"yellow"/"red"
    mmi: float | None = None           # 最大仪器烈度
    status: str | None = None          # "automatic"/"reviewed"
    intensity_points: list | None = None  # JMA 震度观测点列表
    raw: dict = field(default_factory=dict)


# ─────────────────────── 海啸 ───────────────────────


@dataclass(frozen=True)
class TsunamiEvent:
    """海啸预警事件。"""

    source_id: str
    event_id: str
    timestamp: datetime | None = None  # 发布时间（UTC）
    level: int = 0                     # 0=解除/无, 1=注意报, 2=警报, 3=大海啸警报
    title: str | None = None           # 标题 "海啸注意报" / "津波注意報"
    title_text: str | None = None      # 通知正文
    source_name: str | None = None     # 来源显示名
    condition: str | None = None       # "warning"/"watch"/"advisory"/"none"
    class_name: str | None = None      # "gray"/"yellow"/"red"/"purple"
    areas: list | None = None          # [{name, grade, level, arrivalTime, ...}]
    shock_info: dict | None = None     # 引发地震信息
    raw: dict = field(default_factory=dict)


# ─────────────────────── 气象 ───────────────────────


@dataclass(frozen=True)
class WeatherEvent:
    """气象预警事件。"""

    source_id: str
    event_id: str                     # 预警唯一 ID
    alert_type: str | None = None     # 预警类型编码
    alert_level: str | None = None    # 等级: "红色"/"橙色"/"黄色"/"蓝色"
    alert_title: str | None = None    # 预警标题
    headline: str | None = None       # 预警简报正文
    description: str | None = None    # 详细描述
    effective_time: datetime | None = None  # 生效时间（CST UTC+8）
    latitude: float | None = None
    longitude: float | None = None
    raw: dict = field(default_factory=dict)


# ─────────────────────── 台风 ───────────────────────


@dataclass(frozen=True)
class TyphoonTrackPoint:
    """台风路径点。"""

    timestamp: datetime | None        # 观测时间（CST UTC+8）
    latitude: float                   # 纬度
    longitude: float                  # 经度
    pressure: float | None = None     # 中心气压（hPa）
    wind_speed: float | None = None   # 最大风速（m/s）
    category: int = 0                 # CMA 级别 0-6
    beaufort: int | None = None       # 蒲福风级
    direction: str | None = None      # 移向文字
    move_speed: float | None = None   # 移速（km/h）
    wind_radii_7: dict | None = None  # 7级风圈 {NE,SE,SW,NW} km
    wind_radii_10: dict | None = None
    wind_radii_12: dict | None = None
    is_forecast: bool = False         # 是否为预报点


@dataclass(frozen=True)
class TyphoonEvent:
    """台风领域事件 — 一个台风的完整状态。"""

    source_id: str
    event_id: str
    name_cn: str                     # 中文名称
    name_en: str                     # 英文名称
    code: str                        # 编号（CMA: "2607", JMA: "TC2608"）
    category: int = 0                # 当前级别 0-6
    pressure: float | None = None    # 最新中心气压
    wind_speed: float | None = None  # 最新最大风速
    latitude: float | None = None    # 最新纬度
    longitude: float | None = None   # 最新经度
    last_updated: datetime | None = None  # 更新时间
    move_direction: str | None = None
    move_speed: float | None = None
    track_points: tuple[TyphoonTrackPoint, ...] = ()
    is_active: bool = True
    raw: dict = field(default_factory=dict)


# ─────────────────────── 事件包裹层 ───────────────────────


@dataclass(frozen=True)
class EventEnvelope:
    """统一事件包裹 — 流水线传递的统一格式。

    流水线内部只传递 EventEnvelope，
    不直接传递原始 dict 或领域模型的裸实例。

    identity 描述"这是谁",
    event   描述"发生了什么"。
    """

    identity: EventIdentity
    event: EarthquakeEvent | EewEvent | EarthquakeReport | TsunamiEvent | WeatherEvent | TyphoonEvent
    received_at: datetime = field(default_factory=lambda: datetime.utcnow())
    payload: SourcePayload | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.identity.event_id

    @property
    def source_id(self) -> str:
        return self.identity.source_id

    @property
    def event_type(self) -> str:
        return self.identity.event_type

    @property
    def report_num(self) -> int | None:
        return self.identity.report_num


# ─────────────────────── 数据源描述 ───────────────────────


@dataclass(frozen=True)
class SourceEntry:
    """数据源注册项 — 描述一个数据源在系统中的静态画像。

    从 config/sources.json 加载后反序列化为该类型。
    """

    source_id: str
    source_type: str                 # "earthquake_warning" / "earthquake_info" / "tsunami" / "weather" / "typhoon"
    provider_family: str             # "fan_studio" / "wolfx" / "p2p" / "global_quake" / "direct_http"
    parser_name: str                 # 对应解析器注册名
    presentation_type: str           # 卡片展示类型
    config_group: str                # 配置父组名
    config_key: str                  # 配置开关 key
    priority: int = 0               # 推送/显示优先级
    display_name: str = ""           # 人类可读名称
    description: str = ""
    default_timezone: str = "Asia/Shanghai"

    # 连接信息
    connection_handler: str = ""
    connection_url: str = ""
    connection_backup_url: str = ""

    # 机构信息
    institution_key: str = ""
    institution_display_name: str = ""
    institution_active_name: str = ""

    # 路由与查询
    provider_source_names: tuple[str, ...] = ()
    routing_tags: tuple[str, ...] = ()
    dispatch_family: str = ""
    query_group: str = ""

    # 载荷签名（用于路由匹配）
    payload_signatures: tuple[tuple[str, ...], ...] = ()

    # 其他
    metadata: dict[str, str] = field(default_factory=dict)


__all__ = [
    "EventIdentity",
    "SourcePayload",
    "EarthquakeEvent",
    "EewEvent",
    "EarthquakeReport",
    "TsunamiEvent",
    "WeatherEvent",
    "TyphoonTrackPoint",
    "TyphoonEvent",
    "EventEnvelope",
    "SourceEntry",
]
