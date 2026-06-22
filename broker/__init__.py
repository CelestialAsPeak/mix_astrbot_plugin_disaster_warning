"""
broker/ — 数据接入层。

负责网络 I/O、连接管理、原始数据分发。
架构: Connector → SignalBus → Router → Parser
"""
