# Mix灾害预警插件 — 项目状态

## 生效路径

**AstrBot 实际加载目录：**
`C:\Users\ZhuanZ.DESKTOP-PH97BKO\AppData\Local\AstrBot\data\plugins\mix_astrbot_plugin_disaster_warning\`

**开发/Git 目录（改完需同步到 AppData）：**
`C:\Users\ZhuanZ.DESKTOP-PH97BKO\.astrbot\data\plugins\mix_astrbot_plugin_disaster_warning\`

> ⚠ 两个目录是独立副本。在 `.astrbot/` 改完必须 cp 到 AppData/ 才生效。
> `rsync -av --delete --exclude='__pycache__' --exclude='.git' .astrbot/.../ AppData/.../`

## Git 远程

```
origin: https://github.com/CelestialAsPeak/mix_astrbot_plugin_disaster_warning.git
```

## 已完工（截至 2026-06-23）

### 核心架构
- FAN Studio / Wolfx / P2P / GlobalQuake WebSocket 接入
- HTTP 轮询接入（geonet、nrcan、csnc、phivolcs、tmd、funvisis、cenais）
- SNET MSIL 瓦片轮询 + Playwright 测站分布图
- 台风 CMA/JMA 双源轮询 + Playwright 路径图
- 震央分布图（HypoRenderer）
- 双图推送（缩略图 zoom4 + 细节图 zoom8）
- EventPipeline 规则链 + 去重 + 融合

### 多群组推送（2026-06-23 新增）
- `groups` 配置：每个群组独立 sessions + earthquake_filters 覆盖
- Pipeline 逐群推：全局规则 → 逐群阈值重评估 → 逐群推送
- SessionConfigManager：差异覆盖模式、JSON 持久化、deep_merge
- 命令：`/灾害预警群组` list/detail/set/clear

### 阈值过滤
- 28 个数据源独立过滤器（含 slider UI）
- OR 逻辑：震级够 **或** 烈度够即推
- 群组级可覆盖全局阈值

### 推送
- HTTP 轮询源恢复正常推送（signal_bus.emit）
- 台风自动推送附带路径图
- 启动静默期可配置

## 未完工

### 设置页画了但代码没读的
- ~~`push_frequency_control`（EEW 报次限频）~~ ✅ 已实现（ReportRule，报次规则 + 频率控制合并）
- `local_monitoring`（本地经纬度 + 烈度阈值）
- `websocket_config`（重连/超时参数）
- `data_sources` 的细粒度 source 开关
- `offline_notification_sessions`
- `display_timezone`

### 注意
- ICL 数据源（成都高新所）因法律风险故意不接入，详见 `_setup_http_pollers` 注释
- SNET：`snet_filter` 字段 `min_magnitude` → `min_shindo`，shindo 负值箝位 ≥ 0。不在 `_FILTER_MAP` 中重复过滤（已在 `_fetch_snet_once()` 中用）（2026-06-24 修复）
- 台风自动推送走 `present_typhoon_push()`（`‖` 前缀格式），不同于查询用的 `_field()` 排版
- P2P WebSocket 路由已修复：`_route_p2p()` 按 code=556/551/552 分发到 jma_p2p/jma_p2p_info/jma_tsunami_p2p（2026-06-24 修复 Bug 1）
- USGS 周报轮询已启用：`_setup_http_pollers` 中添加了 usgs_weekly 轮询器（2026-06-24 修复 Bug 6）
- 标题格式统一：`_make_source_title` 使用 `institution_key.upper()` 作为 CODE（2026-06-24 修复 Bug 5）
- JMA/CWA 震度过滤器：新增 `jma_scale_filter`/`cwa_scale_filter`，使用 min_shindo（0-7）+min_magnitude OR 逻辑（2026-06-24 修复 Bug 3+7）
- 全部 22 个过滤器新增 `最小烈度`（slider+hint），含 3 个新增（csnc/tmd/phivolcs 过滤器）（2026-06-24 修复）
- `EarthquakeThresholdRule` section 1 全部改用 OR 逻辑：震级够 **或** 烈度够即推（2026-06-24 修复）
- 验证器 `_validate_earthquake_filters`：震度过滤器（jma/cwa/snet）不再添加 `最小烈度`；S-Net `min_magnitude` 自动迁移到 `min_shindo`（2026-06-24 修复）

## 踩坑记录

### 1. 两个目录
`.astrbot/data/plugins/` 和 `AppData/Local/AstrBot/data/plugins/` 是独立的。改错目录 = 白改。

### 2. `.BAK` 目录名导致 import 失败
Python `__import__('data.plugins.xxx.BAK.main')` 把 `BAK` 当子包解析。不改名永远加载不了。

### 3. `_conf_schema.json` 的 `items`
AstrBot 的 `_parse_schema` 对 `type: object` 必读 `v["items"]`，不加就 KeyError。动态 key（如 groups）也得加 `"items": {}`。

### 4. `EventEnvelope` 未导入
`_typhoon_push_adapter` 里 `EventEnvelope(...)` 没 import，渲染成功反而 NameError 吞推送。

### 5. `router._get_parser()` 不存在
`_handle_http_poll_result` 调了不存在的私有方法，应直接 `ParserRegistry.get()`。
