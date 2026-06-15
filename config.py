"""
好感度插件 - 配置类定义

定义插件所有可配置项，使用 MaiBot SDK 的 PluginConfigBase 体系。
每个 Section 对应一个配置分组，在管理面板中以独立卡片展示。

重构变更：
1. ProgressionSection 新增 demotion_buffer 配置（降级缓冲分值）
2. ScoreSection 新增 first_impression_eval_threshold（首因效应保护次数）
3. InjectionSection 新增 show_progress_hint（是否显示升级进度提示）
"""

from __future__ import annotations

from maibot_sdk import Field, PluginConfigBase

# 配置 schema 版本号
CONFIG_SCHEMA_VERSION = "2.0.0"


class PluginSection(PluginConfigBase):
    """插件全局开关与基本设置"""
    __ui_label__ = "插件设置"
    enabled: bool = Field(default=True, description="是否启用好感度插件")
    bot_name: str = Field(default="麦麦", description="好感度关系中的机器人显示名称")
    config_version: str = Field(
        default=CONFIG_SCHEMA_VERSION,
        description="配置 schema 版本",
        json_schema_extra={"disabled": True},
    )


class ScoreSection(PluginConfigBase):
    """好感度分数核心参数"""
    __ui_label__ = "分数设置"
    min_score: int = Field(default=-100, ge=-1000, le=0, description="最低好感度")
    max_score: int = Field(default=100, ge=1, le=1000, description="最高好感度")
    default_score: int = Field(default=0, description="新用户默认好感度")
    max_delta_per_eval: int = Field(
        default=8, ge=1, le=50, description="单次 AI 评分最大变化值（原始 delta 上限）"
    )
    positive_delta_multiplier: float = Field(
        default=1.5, ge=0.1, le=10.0,
        description="正向变化倍率（修复后真正生效，最终上限 = max_delta × multiplier）",
    )
    negative_delta_multiplier: float = Field(
        default=1.5, ge=0.1, le=10.0,
        description="负向变化倍率（修复后真正生效，最终上限 = max_delta × multiplier）",
    )
    ignore_abuse_negative_min_score: int = Field(
        default=80, ge=-1000, le=1000,
        description="达到该好感度后，辱骂和性骚扰不再扣好感度",
    )
    # 重构新增：首因效应保护
    first_impression_eval_threshold: int = Field(
        default=5, ge=0, le=50,
        description="新用户前 N 次评价中，负向 delta 自动缩小（首因效应保护），0 表示关闭",
    )


class InactivityDecaySection(PluginConfigBase):
    """长期未互动的好感度自然衰减"""
    __ui_label__ = "久未互动衰减"
    enabled: bool = Field(default=True, description="是否启用长期未互动扣好感度")
    grace_days: int = Field(default=14, ge=1, le=3650, description="超过多少天未互动后开始扣分")
    interval_days: int = Field(default=7, ge=1, le=3650, description="超过宽限期后每隔多少天扣一次")
    delta_per_interval: int = Field(default=2, ge=1, le=1000, description="每个衰减周期扣多少好感度")
    max_delta_once: int = Field(default=10, ge=1, le=1000, description="单次触发最多扣多少好感度")
    min_score: int = Field(default=0, ge=-1000, le=1000, description="长期未互动最多扣到多少分")


class EvaluationSection(PluginConfigBase):
    """AI 自动评分相关参数"""
    __ui_label__ = "AI 评分"
    enabled: bool = Field(default=True, description="是否启用 AI 自动评分")
    messages_per_eval: int = Field(default=3, ge=1, le=50, description="累计多少条消息后触发一次评分")
    cooldown_seconds: int = Field(default=300, ge=0, le=86400, description="同一用户两次评分最小间隔秒数")
    recent_limit: int = Field(default=20, ge=1, le=100, description="参与评分的最近消息条数")
    model: str = Field(default="", description="评分模型任务名，留空使用默认模型")
    temperature: float = Field(default=0.2, ge=0.0, le=2.0, description="评分模型温度")
    max_tokens: int = Field(default=300, ge=64, le=2000, description="评分模型最大输出 token")
    min_confidence: float = Field(default=0.45, ge=0.0, le=1.0, description="低于该置信度不改变好感度")


class FeedbackSection(PluginConfigBase):
    """好感度变化时的反馈提示"""
    __ui_label__ = "变化反馈"
    enabled: bool = Field(default=True, description="好感度变化时是否发送简短提示")
    min_abs_delta_to_notify: int = Field(default=2, ge=1, le=1000, description="变化绝对值达到多少才提示")
    show_delta_value: bool = Field(default=True, description="提示中是否显示具体变化值")
    positive_template: str = Field(
        default="{bot_name}对你的好感度上升了{delta_text}。",
        description="好感度上升提示",
    )
    negative_template: str = Field(
        default="{bot_name}对你的好感度下降了{delta_text}。",
        description="好感度下降提示",
    )


class ProgressionSection(PluginConfigBase):
    """等级晋级门槛与降级保护"""
    __ui_label__ = "升级门槛"
    liked_unlock_min_confidence: float = Field(
        default=0.65, ge=0.0, le=1.0, description="升入喜欢档最低置信度"
    )
    lover_unlock_min_confidence: float = Field(
        default=0.8, ge=0.0, le=1.0, description="升入恋人档最低置信度"
    )
    lover_min_positive_eval_count: int = Field(
        default=10, ge=0, le=9999, description="升入恋人档所需正向评价次数"
    )
    lover_growth_slowdown: bool = Field(
        default=True, description="恋人档增长是否减速（渐进曲线替代粗暴 cap）"
    )
    # 重构新增：降级缓冲
    demotion_buffer_enabled: bool = Field(
        default=True, description="是否启用高级档降级缓冲（扣分先消耗缓冲再掉级）"
    )


class InjectionSection(PluginConfigBase):
    """回复提示注入相关配置"""
    __ui_label__ = "回复影响"
    enabled: bool = Field(default=True, description="是否在 replyer 前注入关系提示")
    hide_score_from_reply: bool = Field(default=True, description="普通回复中是否隐藏具体好感度数值")
    inject_when_uncertain: bool = Field(default=False, description="无法确认目标用户时是否注入通用提示")
    max_prompt_length: int = Field(default=1000, ge=100, le=3000, description="关系提示最大长度")
    lover_style_in_group: bool = Field(default=False, description="群聊是否允许表现恋人风格")
    group_lover_display_level: str = Field(
        default="亲近的人", description="恋人在群聊中降级表现的等级"
    )
    private_lover_names: list[str] = Field(
        default_factory=lambda: ["亲爱的", "笨蛋", "最喜欢的人"],
        description="私聊恋人档可用亲昵称呼",
    )
    # 重构新增
    show_progress_hint: bool = Field(
        default=True, description="注入提示中是否显示距下一等级的进度"
    )
    show_buffer_hint: bool = Field(
        default=True, description="注入提示中是否显示关系动摇提示（降级缓冲激活时）"
    )


class PrivacySection(PluginConfigBase):
    """隐私与数据可见性"""
    __ui_label__ = "隐私"
    allow_user_query_self: bool = Field(default=True, description="是否允许用户查询自己")
    allow_user_query_others: bool = Field(default=False, description="是否允许普通用户查询别人")
    allow_admin_query_others: bool = Field(default=True, description="是否允许管理员查询别人")
    store_reasons: bool = Field(default=True, description="是否保存最近评分原因")
    max_reason_records: int = Field(default=20, ge=0, le=200, description="每个用户最多保存多少条评分原因")


class AdminSection(PluginConfigBase):
    """管理员权限配置"""
    __ui_label__ = "管理员"
    admin_user_ids: list[str] = Field(default_factory=list, description="可管理好感度的 QQ 号列表")
    allow_manual_adjust: bool = Field(default=True, description="是否允许管理员手动调整")
    allow_reset: bool = Field(default=True, description="是否允许管理员重置")


class DebugSection(PluginConfigBase):
    """调试日志开关"""
    __ui_label__ = "调试日志"
    enabled: bool = Field(default=False, description="是否输出好感度插件调试日志")
    log_level: str = Field(default="debug", description="调试日志级别：debug 或 info")
    include_message_preview: bool = Field(default=False, description="是否在调试日志中包含用户消息截断摘要")


class SpicyImageSection(PluginConfigBase):
    """涩图请求相关配置"""
    __ui_label__ = "涩图请求"
    enabled: bool = Field(default=True, description="是否启用自然语言涩图请求")
    immich_base_url: str = Field(default="", description="Immich 地址，例如 http://127.0.0.1:2283")
    immich_api_key: str = Field(default="", description="Immich API Key")
    normal_album: str = Field(default="正常", description="普通好感度相册名")
    negative_album: str = Field(default="屎", description="负好感度惩罚相册名")
    low_album: str = Field(default="涩涩-低", description="低涩度相册名")
    medium_album: str = Field(default="涩涩-中", description="中涩度相册名")
    high_album: str = Field(default="涩涩-高", description="高涩度相册名")
    cooldown_seconds: int = Field(default=10, ge=0, le=3600, description="同用户触发冷却秒数")
    preference_timeout_seconds: int = Field(default=60, ge=10, le=600, description="高好感偏好等待秒数")
    recall_after_seconds: int = Field(default=60, ge=0, le=3600, description="图片发送后撤回秒数，0 表示不撤回")
    request_model: str = Field(default="utils", description="请求识别与偏好解析模型任务名")
    request_temperature: float = Field(default=0.2, ge=0.0, le=2.0, description="解析模型温度")
    request_max_tokens: int = Field(default=500, ge=64, le=2000, description="解析模型最大输出 token")
    tag_match_threshold: float = Field(default=0.45, ge=0.0, le=1.0, description="标签相似匹配最低阈值")
    random_search_size: int = Field(default=50, ge=1, le=1000, description="标签搜索每次最多取多少候选")
    max_send_attempts: int = Field(default=3, ge=1, le=10, description="发送失败后最多换图重试次数")


class FavorabilityConfig(PluginConfigBase):
    """好感度插件总配置（聚合所有 Section）"""
    plugin: PluginSection = Field(default_factory=PluginSection)
    score: ScoreSection = Field(default_factory=ScoreSection)
    inactivity_decay: InactivityDecaySection = Field(default_factory=InactivityDecaySection)
    evaluation: EvaluationSection = Field(default_factory=EvaluationSection)
    feedback: FeedbackSection = Field(default_factory=FeedbackSection)
    progression: ProgressionSection = Field(default_factory=ProgressionSection)
    injection: InjectionSection = Field(default_factory=InjectionSection)
    privacy: PrivacySection = Field(default_factory=PrivacySection)
    admin: AdminSection = Field(default_factory=AdminSection)
    debug: DebugSection = Field(default_factory=DebugSection)
    spicy_image: SpicyImageSection = Field(default_factory=SpicyImageSection)
