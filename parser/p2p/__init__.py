"""
parser/p2p/ — P2PQuake 数据源解析器。

P2P 使用 WebSocket 连接，消息格式为 JSON，通过 code 字段区分类型:
  - 556: EEW
  - 551: 地震情报
  - 552: 海啸预报
"""
