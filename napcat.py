"""
好感度插件 - NapCat 集成

封装与 NapCat HTTP API 的交互，包括：
- 图片发送（base64 编码上传）
- Action 调用与响应解析
- 发送目标缓存（群号/用户号）
- 图片自动撤回定时器
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any, TYPE_CHECKING

from .utils import extract_message_id, now, positive_int

if TYPE_CHECKING:
    from .plugin import FavorabilityPlugin


class NapCatHelper:
    """NapCat HTTP API 辅助类，负责图片发送、撤回和目标缓存。"""

    def __init__(self, plugin: FavorabilityPlugin) -> None:
        self._plugin = plugin
        # 发送目标缓存：session_id → (chat_type, target_id, cached_at)
        self._targets: dict[str, tuple[str, int, float]] = {}

    # ── 图片发送 ─────────────────────────────────────────────────

    async def send_image(self, session_id: str, image_bytes: bytes) -> str:
        """通过 NapCat 发送图片，返回 message_id 或空字符串。

        流程：
        1. 将图片编码为 base64
        2. 从缓存中查找发送目标（群号或用户号）
        3. 调用 NapCat send_msg Action
        """
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        target = self._targets.get(session_id)
        if not target:
            return ""
        chat_type, target_id, cached_at = target
        # 缓存超过 1 小时视为过期
        if now() - cached_at > 3600:
            return ""

        params: dict[str, Any] = {
            "message_type": chat_type,
            "message": [{"type": "image", "data": {"file": f"base64://{image_b64}"}}],
        }
        if chat_type == "group":
            params["group_id"] = target_id
        else:
            params["user_id"] = target_id

        try:
            response = await self.call_action("send_msg", params)
        except Exception:
            return ""
        if not isinstance(response, dict) or not response.get("success"):
            return ""
        return extract_message_id(response)

    # ── Action 调用 ──────────────────────────────────────────────

    async def call_action(
        self, action_name: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """调用 NapCat HTTP Action 并统一解析响应格式。

        NapCat 的响应格式不统一（有时 status=ok 在外层，有时在 result 中），
        本方法将其归一化为 {"success": bool, "result": ..., "error": ...}。
        """
        response = await self._plugin.ctx.api.call(
            "adapter.napcat.action.call", action_name=action_name, params=params
        )

        if not isinstance(response, dict):
            return {"success": False, "error": str(response)}

        # 外层 status=ok
        if str(response.get("status") or "").lower() == "ok":
            return {"success": True, "result": response}

        # 外层 success=false
        if response.get("success") is False:
            return response

        # 没有 success 字段 → 从 wording/message 取错误
        if "success" not in response:
            return {
                "success": False,
                "error": str(response.get("wording") or response.get("message") or response),
                "result": response,
            }

        # success=true 但需要检查内层 result
        if not response.get("success"):
            return response

        raw = response.get("result")
        if isinstance(raw, dict) and str(raw.get("status") or "").lower() == "ok":
            return {"success": True, "result": raw}
        return {
            "success": False,
            "error": (
                str(raw.get("wording") or raw.get("message") or raw)
                if isinstance(raw, dict) else str(raw)
            ),
            "result": raw,
        }

    # ── 自动撤回 ─────────────────────────────────────────────────

    def schedule_recall(
        self, message_id: str, spawn_task_fn: Any, delay: int
    ) -> None:
        """安排图片在指定秒数后自动撤回。

        Args:
            message_id: 要撤回的消息 ID
            spawn_task_fn: 用于创建异步任务的函数（来自 SpicyImageHandler）
            delay: 撤回延迟秒数
        """
        if delay <= 0:
            return
        normalized = positive_int(message_id)
        if normalized is None:
            return

        async def _recall() -> None:
            await asyncio.sleep(delay)
            try:
                await self.call_action("delete_msg", {"message_id": normalized})
            except Exception:
                pass

        spawn_task_fn(_recall())

    # ── 目标缓存 ─────────────────────────────────────────────────

    def cache_target(
        self, message: dict | None, session_id: str, user_id: str, is_group: bool
    ) -> None:
        """从消息中提取 NapCat 发送目标（群号或用户号）并缓存。

        后续发送图片时根据 session_id 查找目标。
        """
        if not isinstance(message, dict):
            return
        msg_info = message.get("message_info") or {}
        if not isinstance(msg_info, dict):
            return
        additional = msg_info.get("additional_config") or {}
        if not isinstance(additional, dict):
            additional = {}

        if is_group:
            # 群聊：从 additional_config 或 group_info 提取群号
            group_info = msg_info.get("group_info") or {}
            gid = str(
                additional.get("platform_io_target_group_id")
                or (group_info.get("group_id") if isinstance(group_info, dict) else "")
                or ""
            ).strip()
            tid = positive_int(gid)
            if tid is not None:
                self._targets[session_id] = ("group", tid, now())
        else:
            # 私聊：从 additional_config 或 user_id 提取用户号
            uid = str(additional.get("platform_io_target_user_id") or user_id).strip()
            tid = positive_int(uid)
            if tid is not None:
                self._targets[session_id] = ("private", tid, now())
