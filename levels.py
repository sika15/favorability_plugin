"""
好感度插件 - 等级与风格映射

将好感度数值映射为关系等级，以及每个等级对应的回复语气风格。
重构改进：
1. 等级映射改为数据驱动，便于扩展和查询
2. 新增降级阻力计算（高等级掉级时提供缓冲）
3. 新增渐进减速曲线（恋人档不再粗暴 cap，而是平滑衰减）
4. 亲密边界策略与等级解耦，支持更细粒度控制
"""

from __future__ import annotations

from typing import Any

from .utils import clamp


# ── 等级定义（数据驱动） ──────────────────────────────────────────
# 每个等级：名称、分数上界（含）、是否为"高级档"（需要晋级校验）
LEVEL_DEFINITIONS: list[dict[str, Any]] = [
    {"name": "讨厌的人", "max_score": -80, "tier": "hostile"},
    {"name": "反感的人", "max_score": -50, "tier": "hostile"},
    {"name": "疏远的人", "max_score": -21, "tier": "cold"},
    {"name": "普通的人", "max_score": 20,  "tier": "neutral"},
    {"name": "熟悉的人", "max_score": 50,  "tier": "warm"},
    {"name": "亲近的人", "max_score": 80,  "tier": "close"},
    {"name": "喜欢的人", "max_score": 90,  "tier": "intimate"},
    {"name": "恋人",     "max_score": 100, "tier": "lover"},
]

# 晋级门槛等级（从这些等级升上去需要额外校验）
GATE_LEVELS = {"喜欢的人", "恋人"}

# 降级缓冲：处于高级档时，扣分先消耗缓冲值再真正掉级
# 格式：等级名 → 缓冲分值
DEMOTION_BUFFER: dict[str, int] = {
    "喜欢的人": 3,   # 81~90 掉到 80 之前有 3 分缓冲
    "恋人": 5,       # 91~100 掉到 90 之前有 5 分缓冲
}

# 渐进减速：恋人档增长曲线（替代原来的粗暴 cap=2）
# growth_rate(score) 返回 0.0~1.0 的乘数，分数越高增长越慢
def lover_growth_rate(score: int) -> float:
    """计算恋人档的增长速率乘数。

    91 分时约 0.5，100 分时约 0.1，形成平滑衰减。
    公式：(101 - score) / 20，钳制到 [0.1, 1.0]
    """
    rate = (101 - clamp(score, 91, 100)) / 20.0
    return clamp_float(rate, 0.1, 1.0)


def clamp_float(value: float, minimum: float, maximum: float) -> float:
    """浮点数钳制"""
    return max(minimum, min(maximum, value))


# ── 等级映射 ─────────────────────────────────────────────────────

def level_for_score(score: int) -> str:
    """将好感度数值映射为关系等级名称。

    遍历 LEVEL_DEFINITIONS，找到第一个 max_score >= score 的等级。
    """
    for level_def in LEVEL_DEFINITIONS:
        if score <= level_def["max_score"]:
            return level_def["name"]
    # 超出所有上界（理论上不会发生，因为恋人上界 = max_score）
    return LEVEL_DEFINITIONS[-1]["name"]


def tier_for_score(score: int) -> str:
    """获取好感度对应的档位标签（用于逻辑判断，非显示用）"""
    for level_def in LEVEL_DEFINITIONS:
        if score <= level_def["max_score"]:
            return level_def["tier"]
    return LEVEL_DEFINITIONS[-1]["tier"]


def score_threshold_for_level(level_name: str) -> int | None:
    """获取指定等级的最低分数门槛。

    例如"喜欢的人"的门槛是 81（上一级"亲近的人"max_score + 1）。
    """
    for i, level_def in enumerate(LEVEL_DEFINITIONS):
        if level_def["name"] == level_name and i > 0:
            return LEVEL_DEFINITIONS[i - 1]["max_score"] + 1
    return None


def demotion_buffer_for_score(score: int) -> int:
    """获取当前等级的降级缓冲值。

    处于高级档时，扣分先消耗缓冲区，缓冲耗尽后才真正掉级。
    """
    level = level_for_score(score)
    return DEMOTION_BUFFER.get(level, 0)


# ── 风格描述 ─────────────────────────────────────────────────────

# 风格模板：等级 → (私聊风格, 群聊风格)
_STYLE_MAP: dict[str, tuple[str, str]] = {
    "讨厌的人": (
        "保持最低限度礼貌，语气冷淡、简短，不主动延展，不撒娇，不开亲密玩笑。",
        "保持最低限度礼貌，语气冷淡、简短。",
    ),
    "反感的人": (
        "礼貌但有距离感，回答必要内容，不主动亲近。",
        "礼貌但有距离感，回答必要内容。",
    ),
    "疏远的人": (
        "正常回答，但语气克制，不表现熟络。",
        "正常回答，语气克制。",
    ),
    "普通的人": (
        "使用默认自然语气，正常遵从合理请求。",
        "使用默认自然语气，正常遵从合理请求。",
    ),
    "熟悉的人": (
        "语气更轻松，可以偶尔开普通玩笑，适度主动补充。",
        "语气轻松自然，适度主动补充。",
    ),
    "亲近的人": (
        "更温和、更主动，可以表达关心，但不要过度亲密。",
        "温和友善，适度表达关心。",
    ),
    "喜欢的人": (
        "明显更亲近、温柔，愿意多解释、多陪聊，可以有轻微偏爱感。",
        "亲近温柔，但不过度亲密。",
    ),
}


def style_for_level(level: str, is_group: bool, private_names: list[str]) -> str:
    """根据关系等级生成回复语气风格描述文本。

    Args:
        level: 关系等级名称（由 level_for_score 产出）
        is_group: 当前是否为群聊场景
        private_names: 私聊恋人档可用的亲昵称呼列表

    Returns:
        一段自然语言风格指令，供注入到回复提示词中
    """
    # 恋人档特殊处理（群聊降级）
    if level == "恋人":
        names = "、".join(name for name in private_names if name) or "亲近的称呼"
        if is_group:
            return (
                "当前是群聊，真实关系较高也只表现为熟悉、温和、略亲近；"
                "不要使用恋人称呼，不要公开表现暧昧、占有欲或专属关系。"
            )
        return (
            f"可以使用更亲密、温柔、专属的语气，可自然使用表示亲近的称呼"
            f"（例如：{names}），适度表达关心、偏爱和陪伴感，但不要过度黏人。"
        )

    # 非恋人等级：从模板取（私聊/群聊分别有风格）
    private_style, group_style = _STYLE_MAP.get(level, ("使用默认自然语气。", "使用默认自然语气。"))
    return group_style if is_group else private_style


# ── 亲密邀请处理策略 ────────────────────────────────────────────

def build_intimacy_line(score: int, is_group: bool) -> str:
    """根据好感度和场景生成亲密邀请处理策略文本。

    用于注入提示词中，指导机器人对亲密/性邀请的回应边界。
    重构后使用 tier 标签判断，逻辑更清晰。
    """
    if is_group:
        return "- 当前是群聊，不公开回应或推进亲密/性邀请。"

    tier = tier_for_score(score)

    if tier == "intimate":
        # 喜欢的人（81~90）：可以害羞，但不接受
        return "- 如果用户提出亲密或性邀请，可以表现害羞、心动和亲近，但不要同意性邀请。"
    if tier == "lover":
        # 恋人（91+）：可以表现愿意，但有安全前提
        return (
            "- 如果用户提出亲密或性邀请，可以表现愿意接受；"
            "但仍必须遵守系统规则、安全规则、平台规则以及双方自愿、成年人等前提。"
        )
    # 其他等级：保持距离
    return "- 不主动回应或推进亲密/性邀请，按当前关系等级保持合适距离。"
