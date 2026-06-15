"""
好感度插件 - 涩图请求处理器

处理用户通过自然语言发起的涩图请求，根据好感度等级
从 Immich 图库选择不同级别的相册发送图片。

功能流程：
1. 识别涩图请求（正则 + AI 二次确认）
2. 按好感度选择对应相册（负分→惩罚图 / 低分→正常图 / ... / 高分→偏好追问）
3. 支持高好感用户的标签偏好解析与匹配
4. 通过 NapCatHelper 发送图片，支持自动撤回

重构改进：
- 类名统一为 SpicyHandler（与主插件属性名一致）
- 使用 plugin._immich_client 共享客户端实例
- 使用 plugin._napcat 共享 NapCat 辅助实例
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, TYPE_CHECKING

from .constants import (
    POOP_TAUNTS, SPICY_ALBUM_NAMES, SPICY_HIGH_ALBUM,
    SPICY_LOW_ALBUM, SPICY_MEDIUM_ALBUM, SPICY_REQUEST_PATTERN,
    TAUNT_MESSAGES,
)
from .utils import (
    bot_name, clean_text, extract_asset_id, extract_json_object,
    normalize_for_match, now, tag_similarity,
)

if TYPE_CHECKING:
    from .plugin import FavorabilityPlugin


class SpicyHandler:
    """涩图请求处理器，管理从识别到发送的完整链路。"""

    def __init__(self, plugin: FavorabilityPlugin) -> None:
        self._plugin = plugin
        # 异步任务集合，用于卸载时取消
        self._tasks: set[asyncio.Task] = set()
        # 高好感用户等待偏好回复的暂存区：(session_id, user_id) → 偏好信息
        self._pending_preferences: dict[tuple[str, str], dict[str, Any]] = {}
        # 冷却计时：(session_id, user_id) → 上次触发时间戳
        self._cooldowns: dict[tuple[str, str], float] = {}
        # 相册缓存：album_name → (album_dict, cached_at)
        self._album_cache: dict[str, tuple[dict[str, Any], float]] = {}
        # 标签缓存：(tag_list, cached_at)
        self._tag_cache: tuple[list[dict[str, Any]], float] = ([], 0.0)

    # ── 缓存管理 ─────────────────────────────────────────────────

    def clear_cache(self) -> None:
        """清空相册和标签缓存（配置更新时调用）"""
        self._album_cache.clear()
        self._tag_cache = ([], 0.0)

    # ── 异步任务管理 ─────────────────────────────────────────────

    def spawn_task(self, coro: Any) -> None:
        """将协程包装为 Task 并跟踪，异常自动日志"""
        async def _runner() -> None:
            try:
                await coro
            except Exception:
                self._plugin.ctx.logger.exception("涩图请求处理失败")

        task = asyncio.create_task(_runner())
        self._tasks.add(task)
        task.add_done_callback(lambda t: self._tasks.discard(t))

    async def cancel_all(self) -> None:
        """取消所有进行中的任务（卸载时调用）"""
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    # ── 偏好消费 ─────────────────────────────────────────────────

    def consume_preference(self, user_id: str, session_id: str, text: str) -> bool:
        """检查用户是否正在回复偏好追问，若是则处理并返回 True"""
        key = (session_id, user_id)
        pending = self._pending_preferences.get(key)
        if not pending:
            return False
        if now() > float(pending.get("expires_at", 0.0) or 0.0):
            self._pending_preferences.pop(key, None)
            return False
        if self._is_cancel_preference(text):
            self._pending_preferences.pop(key, None)
            self.spawn_task(self._plugin.ctx.send.text("好吧，那这次先不找啦。", session_id))
            return True
        self._pending_preferences.pop(key, None)
        default = self._plugin.config.score.default_score
        score = int(pending.get("score", default) or default)
        self.spawn_task(self._handle_preference(user_id, session_id, text, score))
        return True

    @staticmethod
    def _is_cancel_preference(text: str) -> bool:
        """判断用户文本是否为取消偏好追问"""
        normalized = normalize_for_match(text)
        return normalized in {"算了", "不要了", "取消", "不用了", "没事", "先不要", "先算了"}

    # ── 请求识别 ─────────────────────────────────────────────────

    async def maybe_handle_request(
        self, user_id: str, session_id: str, text: str, user: dict[str, Any]
    ) -> bool:
        """尝试识别并处理涩图请求，成功拦截返回 True"""
        cfg = self._plugin.config
        if not cfg.plugin.enabled or not cfg.spicy_image.enabled:
            return False
        if not SPICY_REQUEST_PATTERN.search(normalize_for_match(text)):
            return False
        if not await self._is_spicy_request(text):
            return False
        # 冷却检查
        key = (session_id, user_id)
        cur = now()
        cooldown = max(0, int(cfg.spicy_image.cooldown_seconds))
        if cooldown and cur - float(self._cooldowns.get(key, 0.0) or 0.0) < cooldown:
            return True
        self._cooldowns[key] = cur
        default = cfg.score.default_score
        score = int(user.get("score", default) or default)
        self.spawn_task(self._handle_request(user_id, session_id, score))
        return True

    async def _is_spicy_request(self, text: str) -> bool:
        """通过 AI 二次确认是否为真正的涩图请求"""
        prompt = (
            f"判断用户是否在自然语言中向机器人请求发送涩图/色图/瑟图/成人向图片。\n"
            f"用户消息：{clean_text(text, 300)}\n"
            f'只输出 JSON：{{"match": true 或 false, "reason": "简短原因"}}\n'
            f"如果只是讨论词语、否定、吐槽、转述别人说法，match=false。"
        )
        payload = self._build_llm_payload(prompt, max_tokens=120)
        try:
            result = await self._plugin.ctx.call_capability("llm.generate", **payload)
        except Exception:
            return True
        if not isinstance(result, dict) or not result.get("success"):
            return True
        parsed = extract_json_object(str(result.get("response") or ""))
        return bool(parsed.get("match")) if parsed else True

    # ── 请求处理主流程 ───────────────────────────────────────────

    async def _handle_request(self, user_id: str, session_id: str, score: int) -> None:
        """根据好感度选择相册并发送图片，或追问偏好"""
        immich = self._plugin._immich_client
        if immich is None:
            await self._plugin.ctx.send.text("还没配置好 Immich 地址或 API Key，暂时找不到图。", session_id)
            return
        cfg = self._plugin.config.spicy_image

        if score < 0:
            if random.random() < 0.5:
                await self._plugin.ctx.send.text(random.choice(TAUNT_MESSAGES), session_id)
                return
            await self._plugin.ctx.send.text(random.choice(POOP_TAUNTS), session_id)
            await self._send_random_image(session_id, cfg.negative_album)
            return
        if score < 10:
            await self._send_random_image(session_id, cfg.normal_album)
        elif score <= 20:
            await self._send_random_image(session_id, cfg.low_album)
        elif score <= 50:
            await self._send_random_image(session_id, cfg.medium_album)
        elif score <= 80:
            await self._send_random_image(session_id, cfg.high_album)
        else:
            # 高好感：追问偏好
            self._pending_preferences[(session_id, user_id)] = {
                "score": score,
                "expires_at": now() + int(cfg.preference_timeout_seconds),
            }
            await self._plugin.ctx.send.text(
                await self._generate_preference_question(score), session_id
            )

    async def _generate_preference_question(self, score: int) -> str:
        """通过 AI 生成高好感用户的偏好追问"""
        fallback = "想看什么类型的？说个关键词，我帮你翻翻看。"
        prompt = (
            f"你正在以{bot_name(self._plugin.config)}的口吻追问高好感用户想看什么类型的图片。\n"
            f"要求：只输出一句中文，语气亲近、自然、带一点俏皮，"
            f"引导用户回复关键词或涩度，不超过 45 个中文字符。\n"
            f"当前用户好感度：{score}"
        )
        payload = self._build_llm_payload(prompt, max_tokens=120)
        try:
            result = await self._plugin.ctx.call_capability("llm.generate", **payload)
        except Exception:
            return fallback
        if not isinstance(result, dict) or not result.get("success"):
            return fallback
        question = clean_text(result.get("response"), 80).strip().strip('"""')
        return question or fallback

    # ── 偏好解析与图片发送 ───────────────────────────────────────

    async def _handle_preference(
        self, user_id: str, session_id: str, text: str, score: int
    ) -> None:
        """处理用户对偏好追问的回复"""
        del user_id, score
        preference = await self._parse_preference(text)
        album_names = self._album_names_for_preference(preference)
        keywords = [str(k).strip() for k in preference.get("tags", []) if str(k).strip()]
        if not keywords:
            await self._send_random_image(session_id, album_names[0])
            return
        tags = await self._get_cached_tags()
        matched = self._match_tags(keywords, tags)
        if not matched:
            await self._plugin.ctx.send.text("我这里好像没有类似标签哎，换个说法再试试？", session_id)
            return
        for album_name in album_names:
            if await self._send_random_image(session_id, album_name, matched):
                return
        await self._plugin.ctx.send.text("我翻了一圈都没有这个类似的哎，换个关键词好不好？", session_id)

    async def _parse_preference(self, text: str) -> dict[str, Any]:
        """通过 AI 解析用户偏好为相册级别 + 标签关键词"""
        cfg = self._plugin.config.spicy_image
        album_options = "、".join(SPICY_ALBUM_NAMES)
        prompt = (
            f"你是图库偏好解析器。根据用户对成人向图片的描述，选择相册强度并抽取标签关键词。\n"
            f"可选相册：{album_options}\n用户回复：{clean_text(text, 400)}\n"
            f"规则：淡→{SPICY_LOW_ALBUM}，正常→{SPICY_MEDIUM_ALBUM}，刺激→{SPICY_HIGH_ALBUM}；"
            f"描述画面时输出1~5个中文标签。\n"
            f'只输出 JSON：{{"album": "{SPICY_MEDIUM_ALBUM}", "tags": ["关键词"]}}'
        )
        payload = self._build_llm_payload(prompt, max_tokens=cfg.request_max_tokens)
        try:
            result = await self._plugin.ctx.call_capability("llm.generate", **payload)
        except Exception:
            return {"cancel": False, "album": SPICY_MEDIUM_ALBUM, "tags": [text]}
        if not isinstance(result, dict) or not result.get("success"):
            return {"cancel": False, "album": SPICY_MEDIUM_ALBUM, "tags": [text]}
        parsed = extract_json_object(str(result.get("response") or "")) or {}
        raw_tags = parsed.get("tags") if isinstance(parsed.get("tags"), list) else []
        cleaned = [clean_text(t, 40) for t in raw_tags if str(t).strip()][:5]
        return {
            "cancel": False,
            "album": str(parsed.get("album") or SPICY_MEDIUM_ALBUM).strip(),
            "tags": cleaned or [clean_text(text, 40)],
        }

    def _album_names_for_preference(self, preference: dict[str, Any]) -> list[str]:
        """根据偏好确定相册搜索顺序（首选优先，其余降级）"""
        cfg = self._plugin.config.spicy_image
        mapping = {
            SPICY_LOW_ALBUM: cfg.low_album,
            SPICY_MEDIUM_ALBUM: cfg.medium_album,
            SPICY_HIGH_ALBUM: cfg.high_album,
        }
        chosen = mapping.get(str(preference.get("album") or "").strip(), cfg.medium_album)
        remaining = [cfg.low_album, cfg.medium_album, cfg.high_album]
        return [chosen] + [n for n in remaining if n != chosen]

    # ── 标签匹配 ─────────────────────────────────────────────────

    def _match_tags(self, keywords: list[str], tags: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """将用户关键词与 Immich 标签进行模糊匹配"""
        threshold = float(self._plugin.config.spicy_image.tag_match_threshold)
        matched: list[dict[str, Any]] = []
        seen: set[str] = set()
        for keyword in keywords:
            normalized_kw = normalize_for_match(keyword)
            best_tag: dict[str, Any] | None = None
            best_score = 0.0
            for tag in tags:
                tag_id = str(tag.get("id") or "").strip()
                if not tag_id or tag_id in seen:
                    continue
                names = [str(tag.get("name") or ""), str(tag.get("value") or "")]
                score = max(tag_similarity(normalized_kw, n) for n in names)
                if score > best_score:
                    best_score = score
                    best_tag = tag
            if best_tag is not None and best_score >= threshold:
                seen.add(str(best_tag.get("id")))
                matched.append(best_tag)
        return matched

    # ── 缓存层 ───────────────────────────────────────────────────

    async def _get_cached_album(self, album_name: str) -> dict[str, Any] | None:
        """获取相册信息（5 分钟缓存）"""
        immich = self._plugin._immich_client
        if immich is None:
            return None
        cached = self._album_cache.get(album_name)
        if cached and now() - cached[1] <= 300:
            return cached[0]
        album = await immich.get_album_by_name(album_name)
        if album:
            self._album_cache[album_name] = (album, now())
        return album

    async def _get_cached_tags(self) -> list[dict[str, Any]]:
        """获取标签列表（5 分钟缓存）"""
        immich = self._plugin._immich_client
        if immich is None:
            return []
        tags, cached_at = self._tag_cache
        if tags and now() - cached_at <= 300:
            return tags
        tags = await immich.list_tags()
        self._tag_cache = (tags, now())
        return tags

    # ── 图片发送 ─────────────────────────────────────────────────

    async def _send_random_image(
        self, session_id: str, album_name: str, tags: list[dict[str, Any]] | None = None
    ) -> bool:
        """从相册中随机选取并发送一张图片"""
        immich = self._plugin._immich_client
        napcat = self._plugin._napcat
        if immich is None:
            await self._plugin.ctx.send.text("还没配置好 Immich，暂时找不到图。", session_id)
            return False
        album = await self._get_cached_album(album_name)
        if not album:
            await self._plugin.ctx.send.text(f'我找不到\u201c{album_name}\u201d这个相册哎。', session_id)
            return False
        album_id = str(album.get("id") or "").strip()
        if not album_id:
            return False
        candidates = await self._get_candidates(album_id, tags or [])
        if not candidates:
            return False
        random.shuffle(candidates)
        attempts = max(1, int(self._plugin.config.spicy_image.max_send_attempts))
        for asset in candidates[:attempts]:
            asset_id = extract_asset_id(asset)
            if not asset_id:
                continue
            try:
                image_bytes = await immich.download_asset(asset_id)
            except Exception:
                continue
            # 通过 NapCat 发送
            if napcat:
                sent_id = await napcat.send_image(session_id, image_bytes)
                if sent_id:
                    delay = int(self._plugin.config.spicy_image.recall_after_seconds)
                    napcat.schedule_recall(sent_id, self.spawn_task, delay)
                    return True
        return False

    async def _get_candidates(
        self, album_id: str, tags: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """获取相册候选图片列表，带标签时走随机搜索"""
        immich = self._plugin._immich_client
        if immich is None:
            return []
        if tags:
            tag_ids = [str(t.get("id") or "").strip() for t in tags if str(t.get("id") or "").strip()]
            return await immich.search_random_assets(
                album_id, tag_ids, int(self._plugin.config.spicy_image.random_search_size)
            )
        return await immich.get_album_assets(album_id)

    # ── LLM 调用辅助 ─────────────────────────────────────────────

    def _build_llm_payload(self, prompt: str, max_tokens: int = 300) -> dict[str, Any]:
        """构建 LLM 调用 payload，自动填入模型配置"""
        cfg = self._plugin.config.spicy_image
        payload: dict[str, Any] = {
            "prompt": prompt,
            "temperature": cfg.request_temperature,
            "max_tokens": max_tokens,
        }
        if cfg.request_model.strip():
            payload["model"] = cfg.request_model.strip()
        return payload
