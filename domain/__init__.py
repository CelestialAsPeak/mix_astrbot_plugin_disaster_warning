"""
mix_astrbot_plugin_disaster_warning — 领域模型。

所有模型为 @dataclass(frozen=True) 不可变对象。
source_id 和 event_id 为必填，可选字段默认 None。
raw: dict 保留完整原始数据，用于调试和回溯。
"""
