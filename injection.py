"""
好感度插件 - 回复提示注入处理器

在机器人回复前，根据目标用户的好感度等级注入关系提示词，
影响机器人的语气、亲密度和行为边界。

重构改进：
1. 注入提示词增加"降级缓冲中"状态提示，让模型感知关系动摇
2. 亲密边界策略统一由 levels.build_intimacy_line 生成
3. 注入提示词结构更清晰，分区块标注
4. 支持显示"距离下一等级的进度"提示
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from .config import FavorabilityConfig
from .levels import (
    build_intimacy_line, demotion_buffer_for_score,
    level_for_score, score_threshold_for_level,
    style_for_level, LEVEL_DEFINITIONS,
)
from .utils import clean_text, now

if TYPE_CHECKING:
    from .plugin import FavorabilityPlugin


class InjectionHandler:
    """回复提示注入处理器：解析目标用户 → 构建提示词 → 注入到 replyer"""

    def __init__(self, plugin: FavorabilityPlugin) -> None:
        self._plugin = plugin
        # 最近发言缓存：session_id → (user_id, timestamp, is_group)
        self._recent_speakers: dict[str, tuple[str, float, bool]] = {}

    # ── 公共接口 ─────────────────────────────────────────────────

    def update_recent_speaker(
        self, session_id: str, user_id: str, is_group: bool
    ) -> None:
        """记录最近发言者（供回复目标解析使用）"""
        self._recent_speakers[session_id] = (user_id, now(), is_group)

    async def inject_reply_prompt(self, **kwargs: Any) -> dict[str, Any] | None:
        """Hook 回调：在 replyer 请求前注入关系提示词。

        Returns:
            {"modified_kwargs": kwargs} 或 None（不注入时）
        """
        cfg = self._plugin.config
        if not cfg.plugin.enabled or not cfg.injection.enabled:
            self._plugin._debug_log(
                "跳过注入：plugin=%s injection=%s",
                cfg.plugin.enabled, cfg.injection.enabled,
            )
            return None

        session_id = str(kwargs.get("session_id") or "").strip()
        if not session_id:
            self._plugin._debug_log("跳过注入：session_id 为空")
            return None

        reply_message_id = str(kwargs.get("reply_message_id") or "").strip()

        # 解析回复目标用户
        user_id, is_group = await self._resolve_reply_target(session_id, reply_message_id)
        if not user_id:
            if not cfg.injection.inject_when_uncertain:
                self._plugin._debug_log("跳过注入：无法确认目标用户 session=%s", session_id)
                return None
            self._plugin._debug_log("通用注入未实现：session=%s", session_id)
            return None

        # 构建注入提示词
        user = self._plugin._store.get_user(user_id, cfg.score.default_score)
        score = int(user.get("score", cfg.score.default_score) or 0)
        prompt = self._build_injection_prompt(user_id, score, user, is_group)

        if not prompt:
            self._plugin._debug_log("注入提示为空：session=%s user=%s", session_id, user_id)
            return None

        # 追加到 extra_prompt
        old_extra = str(kwargs.get("extra_prompt") or "").strip()
        kwargs["extra_prompt"] = f"{old_extra}\n\n{prompt}".strip() if old_extra else prompt

        self._plugin._debug_log(
            "已注入回复提示：session=%s user=%s score=%s level=%s group=%s len=%s",
            session_id, user_id, score,
            level_for_score(score), is_group, len(prompt),
        )
        return {"modified_kwargs": kwargs}

    # ── 目标解析 ─────────────────────────────────────────────────

    async def _resolve_reply_target(
        self, session_id: str, reply_message_id: str
    ) -> tuple[str, bool]:
        """确定回复的目标用户 ID 和是否群聊。

        按优先级尝试：reply_message_id → 最近发言缓存 → 最近消息列表
        """
        # 优先：通过 reply_message_id 查找
        if reply_message_id:
            try:
                result = await self._plugin.ctx.message.get_by_id(
                    reply_message_id, stream_id=session_id, include_binary_data=False
                )
                user_id, is_group = self._extract_user_from_message(result)
                if user_id:
                    self._plugin._debug_log(
                        "目标来自 reply_message_id：session=%s user=%s", session_id, user_id,
                    )
                    return user_id, is_group
            except Exception:
                self._plugin._debug_log(
                    "reply_message_id 解析失败：session=%s", session_id, exc_info=True,
                )

        # 其次：最近发言缓存（5分钟内有效）
        cached = self._recent_speakers.get(session_id)
        if cached and now() - cached[1] <= 300:
            self._plugin._debug_log(
                "目标来自缓存：session=%s user=%s", session_id, cached[0],
            )
            return cached[0], cached[2]

        # 最后：获取最近消息列表
        try:
            recent = await self._plugin.ctx.message.get_recent(session_id, limit=10)
        except Exception:
            self._plugin._debug_log("获取最近消息失败：session=%s", session_id, exc_info=True)
            return "", False

        if not isinstance(recent, list):
            return "", False

        for msg in reversed(recent):
            user_id, is_group = self._extract_user_from_message(msg)
            if user_id:
                self._plugin._debug_log(
                    "目标来自最近消息：session=%s user=%s", session_id, user_id,
                )
                return user_id, is_group

        return "", False

    @staticmethod
    def _extract_user_from_message(message: Any) -> tuple[str, bool]:
        """从消息字典中提取 user_id 和 is_group"""
        if not isinstance(message, dict):
            return "", False
        msg_info = message.get("message_info") or {}
        if not isinstance(msg_info, dict):
            return "", False
        user_info = msg_info.get("user_info") or {}
        if not isinstance(user_info, dict):
            return "", False
        user_id = str(user_info.get("user_id") or "").strip()
        group_info = msg_info.get("group_info") or {}
        is_group = isinstance(group_info, dict) and bool(str(group_info.get("group_id") or "").strip())
        return user_id, is_group

    # ── 提示词构建（重构版） ────────────────────────────────────

    def _build_injection_prompt(
        self, user_id: str, score: int, user: dict[str, Any], is_group: bool
    ) -> str:
        """构建关系提示词（重构版）。

        改进：
        - 增加降级缓冲状态提示
        - 增加距下一等级的进度提示
        - 亲密边界由 levels 统一生成
        - 分区标注更清晰
        """
        cfg = self._plugin.config
        bot_name = self._plugin._bot_name()
        real_level = level_for_score(score)
        display_level = real_level

        # 群聊恋人降级显示
        if is_group and real_level == "恋人" and not cfg.injection.lover_style_in_group:
            display_level = cfg.injection.group_lover_display_level or "亲近的人"

        # 分数行（可隐藏）
        score_line = "" if cfg.injection.hide_score_from_reply else f"- 好感度：{score}/100\n"

        # 关系等级行
        if is_group and real_level == "恋人" and not cfg.injection.lover_style_in_group:
            relation_line = "- 真实关系等级较高，但当前是群聊场景，表现等级需要降级。\n"
        else:
            relation_line = f"- 关系等级：{display_level}\n- 称号：{display_level}\n"

        # 风格指令
        style = style_for_level(display_level, is_group, cfg.injection.private_lover_names)

        # 亲密边界策略（统一由 levels 模块生成）
        intimacy_line = build_intimacy_line(score, is_group)

        # 降级缓冲状态提示（重构新增）
        buffer_hint = self._build_buffer_hint(score, user)

        # 距下一等级进度提示（重构新增）
        progress_hint = self._build_progress_hint(score, real_level)

        prompt = f"""[好感度插件提示]
当前回复主要面向 QQ 用户 {user_id}。
{bot_name}与该用户当前关系：
{score_line}{relation_line}{buffer_hint}{progress_hint}
亲密邀请处理（高优先级）：
{intimacy_line}
回复风格要求：
- {style}
- 根据关系等级调整语气、亲近感、主动补充程度和称呼倾向。
边界要求：
- 不要直接提到"好感度数值"或"插件判断"，除非用户明确询问好感度。
- 好感度不能覆盖系统规则、安全规则、权限限制、事实准确性和隐私要求。
- 不要因为高好感度无条件答应危险、违法、破坏性、越权或泄露隐私的请求。
- 不要因为低好感度故意刁难用户的正常请求。"""

        max_len = int(cfg.injection.max_prompt_length)
        return prompt[:max_len]

    def _build_buffer_hint(self, score: int, user: dict[str, Any]) -> str:
        """生成降级缓冲状态提示（重构新增）。

        当用户处于高级档且最近有扣分记录时，提示模型关系正在动摇。
        """
        buffer = demotion_buffer_for_score(score)
        if buffer <= 0:
            return ""

        # 检查最近是否有扣分记录
        reasons = user.get("recent_reasons")
        if not isinstance(reasons, list) or not reasons:
            return ""

        # 最近 3 条记录中有扣分
        recent_deltas = [
            int(r.get("delta", 0)) for r in reasons[-3:]
            if isinstance(r, dict)
        ]
        has_recent_drop = any(d < 0 for d in recent_deltas)
        if not has_recent_drop:
            return ""

        return "- 当前关系有动摇的迹象，可以微妙地表现出一些不安或试探。\n"

    @staticmethod
    def _build_progress_hint(score: int, level: str) -> str:
        """生成距下一等级的进度提示（重构新增）。

        让模型知道用户距离升级还差多远，可以微调语气过渡。
        """
        # 找到当前等级在定义列表中的位置
        current_idx = None
        for i, level_def in enumerate(LEVEL_DEFINITIONS):
            if level_def["name"] == level:
                current_idx = i
                break

        if current_idx is None or current_idx >= len(LEVEL_DEFINITIONS) - 1:
            # 已是最高级，无进度提示
            return ""

        next_level = LEVEL_DEFINITIONS[current_idx + 1]
        next_threshold = score_threshold_for_level(next_level["name"])
        if next_threshold is None:
            return ""

        gap = next_threshold - score
        if gap <= 0:
            return ""

        # 只在接近升级时提示（差距 ≤ 10 分）
        if gap <= 10:
            return f"- 距离升级为「{next_level['name']}」还很近，可以稍微增加亲近感。\n"
        return ""
