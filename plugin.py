"""
好感度插件 - 主插件类（编排器）

负责：
- 插件生命周期管理（加载/卸载/配置更新）
- 注册 Hook 和 Command
- 将事件分发到各处理器模块
- 维护共享状态（store、immich client、napcat targets）

本文件只做编排，业务逻辑全部分发到对应处理器。
"""

from __future__ import annotations

import asyncio
from typing import Any

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin
from maibot_sdk.types import ErrorPolicy, HookMode

from .commands import CommandHandler
from .config import FavorabilityConfig
from .constants import DATA_PATH, SPICY_REQUEST_PATTERN
from .evaluation import EvaluationHandler
from .immich import ImmichClient
from .injection import InjectionHandler
from .levels import level_for_score
from .napcat import NapCatHelper
from .spicy import SpicyHandler
from .store import FavorabilityStore
from .utils import bot_name, normalize_for_match, now, parse_message_user


class FavorabilityPlugin(MaiBotPlugin):
    """好感度插件主类 — 编排器"""

    config_model = FavorabilityConfig

    def __init__(self) -> None:
        super().__init__()
        # 核心存储
        self._store = FavorabilityStore(DATA_PATH)

        # 处理器（在 on_load 中初始化，因为需要 self 引用）
        self._evaluation: EvaluationHandler | None = None
        self._injection: InjectionHandler | None = None
        self._spicy: SpicyHandler | None = None
        self._commands: CommandHandler | None = None
        self._napcat: NapCatHelper | None = None

        # 管理员 ID 集合
        self._admin_ids: set[str] = set()
        # Immich 客户端
        self._immich_client: ImmichClient | None = None

    # ── 辅助方法（供处理器访问） ─────────────────────────────────

    def _bot_name(self) -> str:
        """获取当前机器人名称"""
        return bot_name(self.config)

    def _is_admin(self, user_id: str) -> bool:
        """判断用户是否为管理员"""
        return str(user_id or "").strip() in self._admin_ids

    def _debug_log(self, message: str, *args: Any, exc_info: bool = False) -> None:
        """输出调试日志（受 debug.enabled 和 log_level 控制）"""
        debug_cfg = self.config.debug
        if not debug_cfg.enabled:
            return
        if str(debug_cfg.log_level).strip().lower() == "info":
            self.ctx.logger.info("[好感度调试] " + message, *args, exc_info=exc_info)
        else:
            self.ctx.logger.debug("[好感度调试] " + message, *args, exc_info=exc_info)

    def _message_preview(self, text: str) -> str:
        """生成消息预览（受 debug.include_message_preview 控制）"""
        if not self.config.debug.include_message_preview:
            return "<已隐藏>"
        from .utils import clean_text
        return clean_text(text, 120)

    # ── 生命周期 ─────────────────────────────────────────────────

    async def on_load(self) -> None:
        """插件加载：初始化处理器和缓存"""
        self._refresh_admin_ids()
        self._refresh_immich_client()

        # 初始化处理器
        self._napcat = NapCatHelper(self)
        self._evaluation = EvaluationHandler(self)
        self._injection = InjectionHandler(self)
        self._spicy = SpicyHandler(self)
        self._commands = CommandHandler(self)

        self.ctx.logger.info("好感度插件已加载。")

    async def on_unload(self) -> None:
        """插件卸载：取消所有异步任务并保存数据"""
        if self._evaluation:
            await self._evaluation.cancel_all()
        if self._spicy:
            await self._spicy.cancel_all()
        self._store.save()

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        """配置更新回调"""
        del config_data, version
        if scope == "self":
            self._refresh_admin_ids()
            self._refresh_immich_client()

    def _refresh_admin_ids(self) -> None:
        """刷新管理员 ID 集合"""
        self._admin_ids = {
            str(item).strip()
            for item in self.config.admin.admin_user_ids
            if str(item).strip()
        }

    def _refresh_immich_client(self) -> None:
        """刷新 Immich 客户端"""
        cfg = self.config.spicy_image
        base_url = cfg.immich_base_url.strip()
        api_key = cfg.immich_api_key.strip()
        self._immich_client = ImmichClient(base_url, api_key) if base_url and api_key else None
        # 清除 spicy 处理器的缓存
        if self._spicy:
            self._spicy.clear_cache()

    # ── Hook：消息观察 ───────────────────────────────────────────

    @HookHandler(
        "chat.receive.before_process",
        name="favorability_message_observer",
        description="处理好感度消息并拦截已消费的涩图请求",
        mode=HookMode.BLOCKING,
        order="late",
        timeout_ms=8000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def observe_message(self, message: dict | None = None, **kwargs: Any) -> None:
        """消息观察 Hook：衰减结算 → 涩图拦截 → 评分累积"""
        del kwargs
        cfg = self.config
        if not cfg.plugin.enabled:
            return None

        parsed = parse_message_user(message)
        if parsed is None:
            return None

        user_id, session_id, text, is_group = parsed

        # 更新最近发言缓存
        if self._injection:
            self._injection.update_recent_speaker(session_id, user_id, is_group)

        # 缓存 NapCat 目标
        if self._napcat:
            self._napcat.cache_target(message, session_id, user_id, is_group)

        if not text:
            return None

        # 久未互动衰减结算
        user, decay_delta = self._store.apply_inactivity_decay(user_id, cfg, session_id=session_id)
        if decay_delta:
            self._debug_log(
                "衰减已结算：user=%s delta=%s score=%s",
                user_id, decay_delta, user.get("score"),
            )

        # 涩图偏好消费
        if self._spicy and self._spicy.consume_preference(user_id, session_id, text):
            return {"action": "abort"}

        # 涩图请求检测
        if self._spicy and await self._spicy.maybe_handle_request(user_id, session_id, text, user):
            return {"action": "abort"}

        # AI 评分累积
        if not cfg.evaluation.enabled:
            return None

        user["message_count"] = int(user.get("message_count", 0) or 0) + 1
        self._store.save_user(user_id, user)

        if self._evaluation:
            self._evaluation.accumulate_and_maybe_eval(user_id, session_id, text, cfg)

        return None

    # ── Hook：回复提示注入 ───────────────────────────────────────

    @HookHandler(
        "maisaka.replyer.before_request",
        name="favorability_reply_prompt_injector",
        description="根据当前目标用户好感度追加 replyer 关系提示词",
        mode=HookMode.BLOCKING,
        order="normal",
        timeout_ms=2500,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_reply_prompt(self, **kwargs: Any) -> dict[str, Any] | None:
        """回复提示注入 Hook"""
        if self._injection:
            return await self._injection.inject_reply_prompt(**kwargs)
        return None

    # ── Command：好感度命令 ──────────────────────────────────────

    @Command(
        "favorability",
        description="查询或管理机器人好感度",
        pattern=r"(?P<favorability_command>^/好感度(?:详情|调整|设置|重置)?(?:\s+\S+){0,2}\s*$)",
    )
    async def handle_favorability_command(
        self,
        stream_id: str = "",
        user_id: str = "",
        matched_groups: dict | None = None,
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """好感度命令入口"""
        if self._commands:
            return await self._commands.handle_favorability_command(
                stream_id=stream_id,
                user_id=user_id,
                matched_groups=matched_groups,
                **kwargs,
            )
        return False, "命令处理器未初始化", True
