"""
好感度插件 - SQLite 数据存储

管理用户好感度数据的持久化，包括：
- 建表与 JSON 遗留数据迁移
- 用户数据的 CRUD 操作
- 好感度变化（delta）的写入与晋级校验
- 久未互动衰减的计算与应用

重构改进：
1. apply_delta 倍率计算改为"先乘后钳"（不再二次钳制使倍率失效）
2. 新增降级缓冲机制（高级档扣分先消耗缓冲再掉级）
3. 新增恋人档渐进减速（使用 lover_growth_rate 替代粗暴 cap=2）
4. 新增首因效应（新用户前 N 条评价有保护倍率）
5. 晋级门槛统一由 levels 模块查询，不再硬编码
6. 资源优化：使用持久连接 + WAL 模式 + 线程锁，避免频繁开关连接
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .config import FavorabilityConfig
from .constants import DATA_PATH, LEGACY_DATA_PATH
from .levels import (
    GATE_LEVELS, demotion_buffer_for_score, level_for_score,
    lover_growth_rate, score_threshold_for_level,
)
from .utils import clamp, clean_text, now, normalize_risk


class FavorabilityStore:
    """好感度 SQLite 数据存储。

    使用持久连接 + WAL 模式 + 线程锁，避免每次操作都开关连接。
    WAL 模式支持并发读，写操作通过 threading.Lock 串行化。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self.load()

    # ── 初始化与迁移 ─────────────────────────────────────────────

    def load(self) -> None:
        """初始化数据库 schema 并尝试从旧版 JSON 迁移数据"""
        conn = self._get_conn()
        self._init_schema(conn)
        self._migrate_legacy_json(conn)

    def save(self) -> None:
        """兼容旧版接口，SQLite 模式下无需手动保存"""
        return None

    def close(self) -> None:
        """关闭持久连接（插件卸载时调用）"""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _get_conn(self) -> sqlite3.Connection:
        """获取持久连接（惰性创建），启用 WAL 模式提升并发读性能"""
        if self._conn is None:
            conn = sqlite3.connect(
                str(self._path),
                check_same_thread=False,
                timeout=5,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-2000")  # 2MB 缓存
            conn.execute("PRAGMA temp_store=MEMORY")
            self._conn = conn
        return self._conn

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        """创建 users 表（如不存在）"""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                score INTEGER NOT NULL,
                message_count INTEGER NOT NULL,
                positive_eval_count INTEGER NOT NULL,
                negative_eval_count INTEGER NOT NULL,
                last_eval_at REAL NOT NULL,
                last_interaction_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                recent_reasons TEXT NOT NULL
            )
            """
        )
        conn.commit()

    def _migrate_legacy_json(self, conn: sqlite3.Connection) -> None:
        """从旧版 JSON 文件迁移数据到 SQLite（仅在表为空时执行）"""
        if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None:
            return
        if not LEGACY_DATA_PATH.exists():
            return
        try:
            raw = json.loads(LEGACY_DATA_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        users = raw.get("users") if isinstance(raw, dict) else None
        if not isinstance(users, dict):
            return
        for user_id, user in users.items():
            if not isinstance(user, dict):
                continue
            default = int(user.get("score", 0) or 0)
            self._upsert_user(conn, str(user_id), self._normalize_user(user, default))
        conn.commit()

    # ── 用户数据格式化 ───────────────────────────────────────────

    @staticmethod
    def _default_user(default_score: int) -> dict[str, Any]:
        """生成新用户的默认数据结构"""
        ts = now()
        return {
            "score": default_score, "message_count": 0,
            "positive_eval_count": 0, "negative_eval_count": 0,
            "last_eval_at": 0.0, "last_interaction_at": ts,
            "updated_at": ts, "recent_reasons": [],
        }

    @staticmethod
    def _normalize_user(user: dict[str, Any], default_score: int) -> dict[str, Any]:
        """将用户字典规范化，补全缺失字段并校验类型"""
        ts = now()
        reasons = user.get("recent_reasons")
        if not isinstance(reasons, list):
            reasons = []
        return {
            "score": int(user.get("score", default_score) or 0),
            "message_count": int(user.get("message_count", 0) or 0),
            "positive_eval_count": int(user.get("positive_eval_count", 0) or 0),
            "negative_eval_count": int(user.get("negative_eval_count", 0) or 0),
            "last_eval_at": float(user.get("last_eval_at", 0.0) or 0.0),
            "last_interaction_at": float(
                user.get("last_interaction_at", user.get("updated_at", ts)) or ts
            ),
            "updated_at": float(user.get("updated_at", ts) or ts),
            "recent_reasons": reasons,
        }

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> dict[str, Any]:
        """将数据库行转为用户字典，解析 recent_reasons JSON"""
        try:
            reasons = json.loads(str(row["recent_reasons"] or "[]"))
        except json.JSONDecodeError:
            reasons = []
        if not isinstance(reasons, list):
            reasons = []
        return {
            "score": int(row["score"]),
            "message_count": int(row["message_count"]),
            "positive_eval_count": int(row["positive_eval_count"]),
            "negative_eval_count": int(row["negative_eval_count"]),
            "last_eval_at": float(row["last_eval_at"]),
            "last_interaction_at": float(row["last_interaction_at"]),
            "updated_at": float(row["updated_at"]),
            "recent_reasons": reasons,
        }

    # ── 写入操作 ─────────────────────────────────────────────────

    @staticmethod
    def _upsert_user(conn: sqlite3.Connection, user_id: str, user: dict[str, Any]) -> None:
        """插入或更新用户记录（ON CONFLICT DO UPDATE）"""
        conn.execute(
            """
            INSERT INTO users (
                user_id, score, message_count, positive_eval_count, negative_eval_count,
                last_eval_at, last_interaction_at, updated_at, recent_reasons
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                score = excluded.score,
                message_count = excluded.message_count,
                positive_eval_count = excluded.positive_eval_count,
                negative_eval_count = excluded.negative_eval_count,
                last_eval_at = excluded.last_eval_at,
                last_interaction_at = excluded.last_interaction_at,
                updated_at = excluded.updated_at,
                recent_reasons = excluded.recent_reasons
            """,
            (
                user_id,
                int(user.get("score", 0) or 0),
                int(user.get("message_count", 0) or 0),
                int(user.get("positive_eval_count", 0) or 0),
                int(user.get("negative_eval_count", 0) or 0),
                float(user.get("last_eval_at", 0.0) or 0.0),
                float(user.get("last_interaction_at", now()) or now()),
                float(user.get("updated_at", now()) or now()),
                json.dumps(
                    user.get("recent_reasons") if isinstance(user.get("recent_reasons"), list) else [],
                    ensure_ascii=False,
                ),
            ),
        )

    def save_user(self, user_id: str, user: dict[str, Any]) -> None:
        """保存（更新）单个用户数据"""
        with self._lock:
            conn = self._get_conn()
            default = int(user.get("score", 0) or 0)
            self._upsert_user(conn, user_id, self._normalize_user(user, default))
            conn.commit()

    @staticmethod
    def _append_reason(user: dict[str, Any], record: dict[str, Any], max_records: int) -> None:
        """追加评分记录并裁剪数量。"""
        records = user.setdefault("recent_reasons", [])
        if not isinstance(records, list):
            records = []
            user["recent_reasons"] = records
        records.append(record)
        if len(records) > max_records:
            del records[:-max_records]

    # ── 读取操作 ─────────────────────────────────────────────────

    def get_user(self, user_id: str, default_score: int) -> dict[str, Any]:
        """获取用户数据，不存在则创建默认记录"""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if row is not None:
                return self._row_to_user(row)
            user = self._default_user(default_score)
            self._upsert_user(conn, user_id, user)
            conn.commit()
            return user

    # ── 好感度变更（重构核心） ───────────────────────────────────

    def set_score(self, user_id: str, score: int, cfg: FavorabilityConfig) -> dict[str, Any]:
        """直接设置用户好感度为指定值（管理员操作）"""
        user = self.get_user(user_id, cfg.score.default_score)
        user["score"] = clamp(int(score), cfg.score.min_score, cfg.score.max_score)
        user["updated_at"] = now()
        self.save_user(user_id, user)
        return user

    def reset(self, user_id: str, cfg: FavorabilityConfig) -> dict[str, Any]:
        """重置用户好感度为默认值（删除后重建）"""
        with self._lock:
            conn = self._get_conn()
            conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            conn.commit()
        return self.get_user(user_id, cfg.score.default_score)

    def apply_delta(
        self,
        user_id: str,
        delta: int,
        confidence: float,
        reason: str,
        risk: str,
        session_id: str,
        cfg: FavorabilityConfig,
    ) -> tuple[dict[str, Any], int, int]:
        """应用好感度变化值（重构版）。

        处理链路：
        1. 钳制原始 delta → 乘以倍率 → 再次钳制（修复：倍率后用 max_delta*multiplier 上限）
        2. 首因效应保护（新用户前几次评价有缩放）
        3. 高好感辱骂豁免
        4. 恋人档渐进减速（使用 lover_growth_rate 平滑衰减）
        5. 降级缓冲（高级档扣分先消耗缓冲再真正掉级）
        6. 晋级门槛校验（从 levels 模块统一查询）
        7. 写入变更 + 记录原因

        Returns:
            (更新后的用户字典, 实际变化值, 缓冲前变化值)
        """
        user = self.get_user(user_id, cfg.score.default_score)
        risk = normalize_risk(risk, reason)
        old_score = int(user.get("score", cfg.score.default_score) or 0)

        # ── 步骤 1：倍率计算（修复版） ──
        # 旧逻辑：先钳制到 [-8,8]，乘倍率，再钳制到 [-8,8] → 倍率几乎无效
        # 新逻辑：先钳制原始 delta，乘倍率，再用扩大后的上限钳制
        max_raw_delta = max(1, int(cfg.score.max_delta_per_eval))
        raw_delta = clamp(int(delta), -max_raw_delta, max_raw_delta)

        if raw_delta > 0:
            pos_mult = float(cfg.score.positive_delta_multiplier)
            adj_delta = max(1, round(raw_delta * pos_mult))
            # 扩大上限：原始上限 × 倍率，确保倍率真正生效
            adj_max = max(max_raw_delta, round(max_raw_delta * pos_mult))
            adj_delta = clamp(adj_delta, 1, adj_max)
        elif raw_delta < 0:
            neg_mult = float(cfg.score.negative_delta_multiplier)
            adj_delta = min(-1, round(raw_delta * neg_mult))
            adj_max = max(max_raw_delta, round(max_raw_delta * neg_mult))
            adj_delta = clamp(adj_delta, -adj_max, -1)
        else:
            adj_delta = 0

        # ── 步骤 2：首因效应保护 ──
        # 新用户前几次评价的负向 delta 缩小，避免初始印象被单条消息毁掉
        eval_count = int(user.get("positive_eval_count", 0) or 0) + int(user.get("negative_eval_count", 0) or 0)
        first_threshold = int(cfg.score.first_impression_eval_threshold)
        if adj_delta < 0 and first_threshold > 0 and eval_count < first_threshold:
            adj_delta = max(-2, round(adj_delta * 0.5))

        # ── 步骤 3：高好感辱骂/地狱笑话豁免 ──
        if old_score >= int(cfg.score.ignore_abuse_negative_min_score) and adj_delta < 0:
            if risk in {"insult", "sexual_harassment", "dark_humor"}:
                adj_delta = 0

        # ── 步骤 4：恋人档渐进减速 ──
        if adj_delta > 0 and old_score >= 91 and cfg.progression.lover_growth_slowdown:
            rate = lover_growth_rate(old_score)
            adj_delta = max(1, round(adj_delta * rate))

        # 保留缓冲前变化值，用于通知显示和评分记录。
        raw_delta = adj_delta

        # ── 步骤 5：降级缓冲 ──
        # 高级档扣分时先消耗缓冲区，缓冲区内只降到门槛线，不掉级
        if adj_delta < 0 and cfg.progression.demotion_buffer_enabled:
            buffer = demotion_buffer_for_score(old_score)
            if buffer > 0:
                threshold = score_threshold_for_level(level_for_score(old_score))
                if threshold is not None:
                    # 最低只降到当前等级的门槛分数，不超过缓冲值
                    floor = max(threshold, old_score - buffer)
                    candidate = old_score + adj_delta
                    if candidate < floor:
                        # 缓冲吸收了部分扣分
                        adj_delta = floor - old_score

        # ── 步骤 6：晋级门槛校验 ──
        candidate = clamp(old_score + adj_delta, cfg.score.min_score, cfg.score.max_score)
        new_level = level_for_score(candidate)
        old_level = level_for_score(old_score)

        if new_level != old_level and new_level in GATE_LEVELS:
            # 升入"喜欢的人"：需要最低置信度
            if new_level == "喜欢的人" and confidence < cfg.progression.liked_unlock_min_confidence:
                threshold = score_threshold_for_level("喜欢的人")
                if threshold is not None:
                    candidate = min(candidate, threshold - 1)
            # 升入"恋人"：需要置信度 + 正向评价次数
            if new_level == "恋人":
                enough_conf = confidence >= cfg.progression.lover_unlock_min_confidence
                enough_hist = int(user.get("positive_eval_count", 0) or 0) >= cfg.progression.lover_min_positive_eval_count
                if not (enough_conf and enough_hist):
                    threshold = score_threshold_for_level("恋人")
                    if threshold is not None:
                        candidate = min(candidate, threshold - 1)

        # ── 步骤 7：写入变更 ──
        actual_delta = candidate - old_score
        user["score"] = candidate
        user["updated_at"] = now()
        user["last_eval_at"] = now()
        if actual_delta > 0:
            user["positive_eval_count"] = int(user.get("positive_eval_count", 0) or 0) + 1
        elif actual_delta < 0:
            user["negative_eval_count"] = int(user.get("negative_eval_count", 0) or 0) + 1
        elif raw_delta < 0:
            # 扣分被缓冲完全吸收时，仍记录一次负面评价。
            user["negative_eval_count"] = int(user.get("negative_eval_count", 0) or 0) + 1

        # 记录评分原因
        if cfg.privacy.store_reasons and cfg.privacy.max_reason_records > 0:
            record = {
                "delta": actual_delta,
                "reason": clean_text(reason, 160),
                "confidence": round(float(confidence), 3),
                "risk": risk,
                "timestamp": now(),
                "session_id": session_id,
            }
            if raw_delta != actual_delta:
                record["raw_delta"] = raw_delta
                if raw_delta < 0 and actual_delta > raw_delta:
                    record["buffered"] = True
            self._append_reason(user, record, int(cfg.privacy.max_reason_records))

        self.save_user(user_id, user)
        return user, actual_delta, raw_delta

    # ── 久未互动衰减 ─────────────────────────────────────────────

    def _calculate_inactivity_decay(
        self, user: dict[str, Any], cfg: FavorabilityConfig, ts: float
    ) -> tuple[int, int, int]:
        """计算因长期未互动导致的好感度衰减。

        Returns:
            (新分数, 实际变化值, 已未互动天数)
        """
        last = float(user.get("last_interaction_at", user.get("updated_at", ts)) or ts)
        elapsed_days = int((ts - last) // 86400)
        old_score = int(user.get("score", cfg.score.default_score) or 0)

        if not cfg.inactivity_decay.enabled or elapsed_days <= int(cfg.inactivity_decay.grace_days):
            return old_score, 0, elapsed_days

        interval = max(1, int(cfg.inactivity_decay.interval_days))
        periods = 1 + (elapsed_days - int(cfg.inactivity_decay.grace_days)) // interval
        decay = min(periods * int(cfg.inactivity_decay.delta_per_interval), int(cfg.inactivity_decay.max_delta_once))
        floor = max(int(cfg.score.min_score), int(cfg.inactivity_decay.min_score))
        new_score = max(floor, old_score - decay)
        return new_score, new_score - old_score, elapsed_days

    def preview_inactivity_decay(
        self, user_id: str, cfg: FavorabilityConfig
    ) -> tuple[dict[str, Any], int, int, int]:
        """预览久未互动衰减效果（不写入数据库）"""
        user = self.get_user(user_id, cfg.score.default_score)
        new_score, actual_delta, elapsed = self._calculate_inactivity_decay(user, cfg, now())
        return user, new_score, actual_delta, elapsed

    def apply_inactivity_decay(
        self, user_id: str, cfg: FavorabilityConfig, session_id: str = ""
    ) -> tuple[dict[str, Any], int]:
        """应用久未互动衰减并写入数据库。

        优化：仅在真正发生衰减或距上次写入超过 1 小时时才写 DB，
        避免每条消息都触发无意义的写入。
        """
        user = self.get_user(user_id, cfg.score.default_score)
        ts = now()
        new_score, actual_delta, elapsed = self._calculate_inactivity_decay(user, cfg, ts)

        if actual_delta == 0:
            # 无衰减：仅当 last_interaction_at 超过 1 小时未更新时才刷新
            last_write = float(user.get("last_interaction_at", 0) or 0)
            if ts - last_write < 3600:
                return user, 0
            user["last_interaction_at"] = ts
            self.save_user(user_id, user)
            return user, 0

        user["score"] = new_score
        user["updated_at"] = ts
        user["last_interaction_at"] = ts

        # 记录衰减原因
        if cfg.privacy.store_reasons and cfg.privacy.max_reason_records > 0:
            self._append_reason(user, {
                "delta": actual_delta,
                "reason": f"连续 {elapsed} 天未互动，好感度自然衰减",
                "confidence": 1.0,
                "risk": "inactivity_decay",
                "timestamp": ts,
                "session_id": session_id,
            }, int(cfg.privacy.max_reason_records))

        self.save_user(user_id, user)
        return user, actual_delta
