"""
好感度插件 - MaiBot 好感度系统

基于 AI 评分的用户好感度管理插件，支持：
- AI 自动评分（根据用户发言计算好感度变化）
- 久未互动衰减（长期不互动自动扣减）
- 降级保护机制（高级别降级时受到阻力缓冲）
- 首因效应（新用户早期互动影响加大）
- 涩图请求分级（根据好感度选择不同级别的图片）
- 回复语气注入（根据好感度调整机器人回复风格）
- 管理员命令（查询、调整、重置好感度）

模块结构：
    constants    - 常量与正则模式
    config       - 配置类定义
    utils        - 纯工具函数
    levels       - 等级与风格映射（含降级保护门槛）
    immich       - Immich 图库 API 客户端
    napcat       - NapCat HTTP API 辅助
    store        - SQLite 数据存储（含 delta 重算逻辑）
    spicy        - 涩图请求处理器
    evaluation   - AI 评分处理器
    injection    - 回复提示注入处理器
    commands     - 命令处理器
    plugin       - 主插件类（编排器）
"""

from .plugin import FavorabilityPlugin

__all__ = ["FavorabilityPlugin"]
