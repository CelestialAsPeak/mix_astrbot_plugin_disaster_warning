"""
地图瓦片源URL配置

注: FAN Studio 代理的瓦片服务器已全部失能（tilemap.fanstudio.tech 不可用），
直连高德地图也无法解析（DNS 限制），目前无可用瓦片源。
地图底图将在后续仿照 S-NET 方式重新实现（不依赖 FAN）。
"""

# 无可用瓦片源（等待自定义地图底图实现）
MAP_SOURCE_NAME_TO_ID: dict[str, str] = {}

MAP_TILE_SOURCES: dict[str, str] = {}


def normalize_map_source(map_source: str) -> str:
    """
    将中文地图源名称转换为英文标识
    如果输入已经是英文标识，则直接返回

    Args:
        map_source: 地图源名称（中文或英文）

    Returns:
        英文标识符
    """
    # 如果是中文名称，转换为英文标识
    if map_source in MAP_SOURCE_NAME_TO_ID:
        return MAP_SOURCE_NAME_TO_ID[map_source]
    # 否则假定已经是英文标识，直接返回
    return map_source


def get_tile_url(map_source: str) -> str:
    """
    获取指定地图源的瓦片URL模板

    注：所有 FAN 代理瓦片源已失能，目前无可用瓦片源。
    返回空字符串（浏览器渲染不加载底图，仅显示矢量路径）。

    Args:
        map_source: 地图源标识符（中文名称或英文标识）

    Returns:
        空字符串（无可用瓦片源）
    """
    return ""


def get_tile_url_js(map_source: str) -> str:
    """
    为JavaScript生成瓦片URL（处理特殊占位符）

    注：所有 FAN 代理瓦片源已失能，目前无可用瓦片源。

    Args:
        map_source: 地图源标识符（中文名称或英文标识）

    Returns:
        空字符串（无可用瓦片源）
    """
    return ""
