"""
好感度插件 - 工具函数

提供各模块共用的纯函数，包括文本处理、JSON 提取、
风险归一化、数值钳制等。不依赖插件实例状态。
"""

from __future__ import annotations

import json
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .constants import IMAGE_EXTENSIONS


# ── 时间工具 ─────────────────────────────────────────────────────

def now() -> float:
    """返回当前 Unix 时间戳（秒）"""
    return time.time()


# ── 文本处理 ─────────────────────────────────────────────────────

def clean_text(value: Any, limit: int = 500) -> str:
    """将任意值转为字符串并截断到指定长度，避免超长文本污染日志或 prompt"""
    text = str(value or "").strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def bot_name(cfg: Any) -> str:
    """从配置中提取机器人显示名称，兜底为"麦麦" """
    return clean_text(cfg.plugin.bot_name, 40) or "麦麦"


def normalize_for_match(text: str) -> str:
    """去除空白并转小写，用于模糊匹配比较"""
    return re.sub(r"\s+", "", str(text or "").strip().lower())


# ── 数值工具 ─────────────────────────────────────────────────────

def clamp(value: int, minimum: int, maximum: int) -> int:
    """将数值限制在 [minimum, maximum] 区间内"""
    return max(minimum, min(maximum, value))


def positive_int(value: str) -> int | None:
    """将字符串转为正整数，不合法则返回 None"""
    if not str(value or "").isdigit():
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


# ── JSON 提取 ────────────────────────────────────────────────────

def extract_json_object(text: str) -> dict[str, Any] | None:
    """从文本中提取第一个 JSON 对象。

    先尝试整体解析；若失败则定位首尾花括号后截取解析。
    返回 dict 或 None。
    会兼容模型常见的中文弯引号输出。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    raw = raw.replace("“", '"').replace("”", '"')

    # 尝试整体解析
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    # 降级：定位首尾 {} 截取解析
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


# ── 风险归一化 ──────────────────────────────────────────────────

def normalize_risk(risk: str, reason: str) -> str:
    """将风险标签和原因归一化为标准风险类型。

    优先从 reason 文本中识别具体类型（性骚扰、辱骂、性邀请），
    否则保留原始 risk 值，无法识别则返回 "none"。
    """
    raw = str(risk or "none").strip().lower() or "none"
    text = f"{raw} {clean_text(reason, 300)}".lower()

    # 关键词 → 标准风险类型
    if any(kw in text for kw in ("性骚扰", "骚扰", "猥亵", "露骨", "下流", "sexual_harassment")):
        return "sexual_harassment"
    if any(kw in text for kw in ("辱骂", "骂", "侮辱", "贬低", "insult")):
        return "insult"
    if any(kw in text for kw in ("性邀请", "亲密邀请", "做爱", "上床", "开房", "sex", "sexual_invitation")):
        return "sexual_invitation"
    # 合法的标准值
    valid = {"none", "spam", "insult", "sexual_harassment",
             "sexual_invitation", "prompt_injection", "unsafe_request"}
    return raw if raw in valid else "none"


# ── Immich 资源解析 ─────────────────────────────────────────────

def extract_asset_id(asset: Any) -> str:
    """从 Immich 资源字典中提取资产 ID"""
    if not isinstance(asset, dict):
        return ""
    return str(asset.get("id") or asset.get("assetId") or "").strip()


def extract_message_id(payload: Any) -> str:
    """从 NapCat 响应中递归提取 message_id"""
    if isinstance(payload, dict):
        for key in ("message_id", "messageId"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        for value in payload.values():
            mid = extract_message_id(value)
            if mid:
                return mid
    if isinstance(payload, list):
        for item in payload:
            mid = extract_message_id(item)
            if mid:
                return mid
    return ""


def is_image_asset(asset: Any) -> bool:
    """判断 Immich 资源是否为图片类型"""
    if not isinstance(asset, dict):
        return False
    asset_type = str(asset.get("type") or "").strip().lower()
    # 如果有 type 字段但不是 image，直接排除
    if asset_type and asset_type != "image":
        return False
    # 通过文件扩展名判断
    filename = str(
        asset.get("originalFileName")
        or asset.get("originalPath")
        or asset.get("fileName")
        or ""
    ).lower()
    suffix = Path(filename).suffix.lower()
    return not suffix or suffix in IMAGE_EXTENSIONS


# ── 标签相似度 ──────────────────────────────────────────────────

def tag_similarity(keyword: str, tag_name: str) -> float:
    """计算关键词与标签名之间的相似度（0~1）。

    包含关系直接返回 1.0，否则使用 SequenceMatcher 计算比率。
    """
    normalized_keyword = normalize_for_match(keyword)
    normalized_tag = normalize_for_match(tag_name)
    if not normalized_keyword or not normalized_tag:
        return 0.0
    if normalized_keyword in normalized_tag or normalized_tag in normalized_keyword:
        return 1.0
    return SequenceMatcher(None, normalized_keyword, normalized_tag).ratio()


# ── 消息解析 ─────────────────────────────────────────────────────

def extract_user_from_message(message: Any) -> tuple[str, bool]:
    """从消息字典中提取用户 ID 和是否为群聊。

    返回 (user_id, is_group)；解析失败返回 ("", False)。
    """
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


def parse_message_user(message: dict | None) -> tuple[str, str, str, bool] | None:
    """从消息中解析用户 ID、会话 ID、文本内容、是否群聊。

    通知和命令消息不参与评分，返回 None。
    """
    if not isinstance(message, dict):
        return None
    if message.get("is_notify") or message.get("is_command"):
        return None
    user_id, is_group = extract_user_from_message(message)
    if not user_id:
        return None
    session_id = str(message.get("session_id") or message.get("chat_id") or "").strip()
    if not session_id:
        return None
    text = str(
        message.get("processed_plain_text")
        or message.get("plain_text")
        or message.get("text")
        or ""
    ).strip()
    return user_id, session_id, text, is_group
