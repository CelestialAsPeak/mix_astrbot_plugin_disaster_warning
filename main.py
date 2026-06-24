"""
main.py — 兼容入口（代码实现在 plugin.py）

AstrBot StarManager 优先加载 main.py。
此文件仅将 MixDisasterWarningPlugin 从 plugin.py 导出，
保证无论 StarManager 加载 main 还是 plugin 模块，都执行同一份代码。

维护说明：
- 不要在此文件写业务逻辑。
- 所有改动请改 plugin.py。
"""

from plugin import MixDisasterWarningPlugin  # noqa
