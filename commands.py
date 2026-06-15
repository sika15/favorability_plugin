"""
好感度插件 - 命令处理器

处理用户和管理员的好感度查询与管理命令：
- /好感度 [QQ号]       — 查询好感度概要
- /好感度详情 [QQ号]   — 查询好感度详情（含最近评分记录）
- /好感度调整 QQ号 值  — 管理员：增减好感度
- /好感度设置 QQ号 值  — 管理员：设置绝对值
- /好感度重置 QQ号     — 管理员：重置为默认

重构改进：
1. 使用 levels 模块的 level_for_score 统一等级映射
2. 详情中展示降级缓冲状态和首因效应保护状态
3. 格式化输出增加"距下一等级"进度提示
"""

from __future__ import annotations

import re
from typing import Any, TYPE_CHECKING

from .config import FavorabilityConfig
from .constants import QQ_PATTERN
from .levels import level_for_score, LEVEL_DEFINITIONS, score_threshold_for_level, demotion_buffer_for_score
from .utils import clean_text, bot_name

if TYPE_CHECKING:
    from .plugin import FavorabilityPlugin


class CommandHandler:
    """好感度命令处理器"""

    def __init__(self, plugin: FavorabilityPlugin) -> None:
        self._plugin = plugin

    # ── 命令入口 ─────────────────────────────────────────────────

    async def handle_favorability_command(
        self,
        stream_id: str = "",
        user_id: str = "",
        matched_groups: dict | None = None,
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """Command 回调：解析并分发好感度命令"""
        del kwargs
        if not self._plugin.config.plugin.enabled:
            return False, "好感度插件未启用", True

        command = str((matched_groups or {}).get("favorability_command") or "").strip()
        if not command:
            return False, "命令为空", True

        parts = command.split()
        head = parts[0]
        requester = str(user_id or "").strip()

        self._plugin._debug_log(
            "收到命令：head=%s requester=%s stream=%s parts=%s",
            head, requester, stream_id, parts,
        )

        # ── 查询类命令 ──
        if head in {"/好感度", "/好感度详情"}:
            detail = head == "/好感度详情"
            target = self._extract_target_qq(parts[1] if len(parts) > 1 else "") or requester
            if target != requester and not self._can_query_other(requester):
                await self._plugin.ctx.send.text("你只能查询自己的好感度。", stream_id)
                return False, "无权查询别人", True
            await self._plugin.ctx.send.text(
                self._format_user_status(target, detail, apply_decay=target == requester),
                stream_id,
            )
            return True, "查询完成", True

        # ── 管理类命令 ──
        if head in {"/好感度调整", "/好感度设置", "/好感度重置"}:
            if not self._plugin._is_admin(requester):
                await self._plugin.ctx.send.text("你没有权限管理好感度。", stream_id)
                return False, "无管理权限", True
            response = self._handle_admin_command(head, parts)
            await self._plugin.ctx.send.text(response, stream_id)
            return True, "管理完成", True

        # ── 未知命令 ──
        await self._plugin.ctx.send.text(
            "用法：/好感度、/好感度详情，管理员可用 /好感度调整|设置|重置 <QQ号> [数值]",
            stream_id,
        )
        return False, "命令不合法", True

    # ── 权限检查 ─────────────────────────────────────────────────

    def _can_query_other(self, requester: str) -> bool:
        """检查请求者是否有权查询他人的好感度"""
        if self._plugin._is_admin(requester) and self._plugin.config.privacy.allow_admin_query_others:
            return True
        return bool(self._plugin.config.privacy.allow_user_query_others)

    # ── 管理命令处理 ─────────────────────────────────────────────

    def _handle_admin_command(self, head: str, parts: list[str]) -> str:
        """执行管理员操作（调整/设置/重置）"""
        cfg = self._plugin.config

        if len(parts) < 2:
            return "请提供目标 QQ 号。"

        target = self._extract_target_qq(parts[1])
        if not target:
            return "目标 QQ 号不合法。"

        # 重置
        if head == "/好感度重置":
            if not cfg.admin.allow_reset:
                return "配置未允许重置好感度。"
            user = self._plugin._store.reset(target, cfg)
            return f"已重置 {target} 的好感度：{user['score']}/100（{level_for_score(int(user['score']))}）。"

        # 调整/设置
        if not cfg.admin.allow_manual_adjust:
            return "配置未允许手动调整好感度。"
        if len(parts) < 3:
            return "请提供数值。"
        try:
            value = int(parts[2])
        except ValueError:
            return "数值不合法。"

        current = self._plugin._store.get_user(target, cfg.score.default_score)
        old_score = int(current.get("score", cfg.score.default_score) or 0)

        if head == "/好感度调整":
            user = self._plugin._store.set_score(target, old_score + value, cfg)
        else:
            user = self._plugin._store.set_score(target, value, cfg)

        score = int(user.get("score", cfg.score.default_score) or 0)
        return f"已更新 {target} 的好感度：{old_score}/100 → {score}/100（{level_for_score(score)}）。"

    # ── 状态格式化（重构版） ─────────────────────────────────────

    def _format_user_status(
        self, user_id: str, detail: bool, apply_decay: bool = True
    ) -> str:
        """格式化用户好感度状态文本。

        重构改进：
        - 增加降级缓冲状态显示
        - 增加距下一等级的进度
        - 增加首因效应保护状态提示
        """
        cfg = self._plugin.config
        name = bot_name(cfg)

        # 衰减处理
        if apply_decay:
            user, _ = self._plugin._store.apply_inactivity_decay(user_id, cfg)
            score = int(user.get("score", cfg.score.default_score) or 0)
            preview_delta = 0
            elapsed_days = 0
        else:
            user, score, preview_delta, elapsed_days = self._plugin._store.preview_inactivity_decay(user_id, cfg)

        level = level_for_score(score)
        lines = [f"你和{name}当前的关系：{level}", f"好感度：{score}/100"]

        # 衰减预览
        if not apply_decay and preview_delta < 0:
            lines.append(
                f"按长期未互动规则预估：{preview_delta}（已 {elapsed_days} 天未互动，未写入数据）。"
            )

        # 降级缓冲状态（重构新增）
        buffer = demotion_buffer_for_score(score)
        if buffer > 0:
            lines.append(f"降级缓冲：{buffer} 分（扣分时先消耗缓冲再掉级）。")

        # 距下一等级进度（重构新增）
        progress = self._format_level_progress(score, level)
        if progress:
            lines.append(progress)

        # 恋人档特殊提示
        if level == "恋人":
            lines.append(
                f"{name}会在私聊中更亲近地回应你；在群聊中会保持自然，不公开表现特殊关系。"
            )

        # 首因效应保护状态（重构新增）
        eval_count = (
            int(user.get("positive_eval_count", 0) or 0)
            + int(user.get("negative_eval_count", 0) or 0)
        )
        first_threshold = int(cfg.score.first_impression_eval_threshold)
        if 0 < first_threshold and eval_count < first_threshold:
            lines.append(f"首因保护：前 {first_threshold} 次评价中（已 {eval_count} 次），负向扣分自动缩小。")

        # 详情：最近评分记录
        if detail:
            reasons = user.get("recent_reasons")
            if isinstance(reasons, list) and reasons:
                lines.append("最近记录：")
                for item in reasons[-5:]:
                    if not isinstance(item, dict):
                        continue
                    delta = int(item.get("delta", 0) or 0)
                    reason = clean_text(item.get("reason"), 80)
                    sign = "+" if delta > 0 else ""
                    lines.append(f"{sign}{delta} {reason}")
            else:
                lines.append("暂无评分记录。")

        return "\n".join(lines)

    @staticmethod
    def _format_level_progress(score: int, level: str) -> str:
        """生成距下一等级的进度文本"""
        # 找到当前等级位置
        current_idx = None
        for i, level_def in enumerate(LEVEL_DEFINITIONS):
            if level_def["name"] == level:
                current_idx = i
                break

        if current_idx is None or current_idx >= len(LEVEL_DEFINITIONS) - 1:
            return ""

        next_level = LEVEL_DEFINITIONS[current_idx + 1]
        threshold = score_threshold_for_level(next_level["name"])
        if threshold is None:
            return ""

        gap = threshold - score
        # 当前等级的分数范围
        current_max = LEVEL_DEFINITIONS[current_idx]["max_score"]
        current_min = threshold = score_threshold_for_level(level)
        if current_min is None:
            current_min = LEVEL_DEFINITIONS[0]["max_score"] + 1 if current_idx == 0 else score
        range_size = current_max - current_min + 1
        progress_pct = int((score - current_min + 1) / range_size * 100) if range_size > 0 else 0

        return f"距「{next_level['name']}」还差 {gap} 分（当前等级进度 {progress_pct}%）"

    # ── 工具方法 ─────────────────────────────────────────────────

    @staticmethod
    def _extract_target_qq(raw: str) -> str:
        """从字符串中提取合法的 QQ 号"""
        match = QQ_PATTERN.search(str(raw or ""))
        return match.group(0) if match else ""
