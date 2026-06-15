"""
好感度插件 - SQLite 数据存储

管理用户好感度数据的持久化：CRUD、delta写入与晋级校验、久未互动衰减。

重构改进：
1. apply_delta 倍率"先乘后钳"（不再二次钳制使倍率失效）
2. 降级缓冲（高级档扣分先消耗缓冲再掉级）
3. 恋人档渐进减速（lover_growth_rate 替代粗暴 cap=2）
4. 首因效应（新用户前 N 条评价有保护倍率）
5. 晋级门槛统一由 levels 模块查询
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .config import FavorabilityConfig
from .constants import DATA_PATH, LEGACY_DATA_PATH
from .levels import (
    GATE_LEVELS, demotion_buffer_for_score, level_for_score,
    lover_growth_rate, score_threshold_for_level,
)
from .utils import clamp, clean_text, now, normalize_risk

# ── SQL 模板 ────────────────────────────────────────────────────

_UPSERT_SQL = """
INSERT INTO users (
    user_id, score, message_count, positive_eval_count, negative_eval_count,
    last_eval_at, last_interaction_at, updated_at, recent_reasons
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(user_id) DO UPDATE SET
    score = excluded.score, message_count = excluded.message_count,
    positive_eval_count = excluded.positive_eval_count,
    negative_eval_count = excluded.negative_eval_count,
    last_eval_at = excluded.last_eval_at,
    last_interaction_at = excluded.last_interaction_at,
    updated_at = excluded.updated_at, recent_reasons = excluded.recent_reasons
"""


class FavorabilityStore:
    """好感度 SQLite 数据存储。

    每次操作都打开独立连接（SQLite WAL 模式下可安全并发读），
    避免长期持有连接导致锁问题。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        """初始化数据库 schema 并尝试从旧版 JSON 迁移数据"""
        with closing(self._connect()) as conn:
            self._init_schema(conn)
            self._migrate_legacy_json(conn)

    def save(self) -> None:
        """兼容旧版接口，SQLite 模式下无需手动保存"""
        return None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        """创建 users 表（如不存在）"""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY, score INTEGER NOT NULL,
                message_count INTEGER NOT NULL, positive_eval_count INTEGER NOT NULL,
                negative_eval_count INTEGER NOT NULL, last_eval_at REAL NOT NULL,
                last_interaction_at REAL NOT NULL, updated_at REAL NOT NULL,
                recent_reasons TEXT NOT NULL
            )
        """)
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
        for uid, u in users.items():
            if isinstance(u, dict):
                self._upsert_user(conn, str(uid), self._normalize_user(u, int(u.get("score", 0) or 0)))
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
            "score": int(row["score"]), "message_count": int(row["message_count"]),
            "positive_eval_count": int(row["positive_eval_count"]),
            "negative_eval_count": int(row["negative_eval_count"]),
            "last_eval_at": float(row["last_eval_at"]),
            "last_interaction_at": float(row["last_interaction_at"]),
            "updated_at": float(row["updated_at"]), "recent_reasons": reasons,
        }

    # ── 写入操作 ─────────────────────────────────────────────────

    @staticmethod
    def _upsert_user(conn: sqlite3.Connection, user_id: str, user: dict[str, Any]) -> None:
        """插入或更新用户记录（ON CONFLICT DO UPDATE）"""
        reasons_data = user.get("recent_reasons")
        conn.execute(_UPSERT_SQL, (
            user_id,
            int(user.get("score", 0) or 0), int(user.get("message_count", 0) or 0),
            int(user.get("positive_eval_count", 0) or 0), int(user.get("negative_eval_count", 0) or 0),
            float(user.get("last_eval_at", 0.0) or 0.0),
            float(user.get("last_interaction_at", now()) or now()),
            float(user.get("updated_at", now()) or now()),
            json.dumps(reasons_data if isinstance(reasons_data, list) else [], ensure_ascii=False),
        ))

    def save_user(self, user_id: str, user: dict[str, Any]) -> None:
        """保存（更新）单个用户数据"""
        with closing(self._connect()) as conn:
            self._upsert_user(conn, user_id, self._normalize_user(user, int(user.get("score", 0) or 0)))
            conn.commit()

    def _append_reason(self, user: dict[str, Any], record: dict[str, Any], max_records: int) -> None:
        """向用户记录追加一条评分原因，并裁剪到 max_records"""
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
        with closing(self._connect()) as conn:
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
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            conn.commit()
        return self.get_user(user_id, cfg.score.default_score)

    def apply_delta(
        self, user_id: str, delta: int, confidence: float, reason: str,
        risk: str, session_id: str, cfg: FavorabilityConfig,
    ) -> tuple[dict[str, Any], int, int]:
        """应用好感度变化值（重构版）。

        返回 (user, actual_delta, raw_delta)：
        - actual_delta: 实际写入的变化值（经过缓冲/减速等修正后）
        - raw_delta: 缓冲前的原始变化值（用于反馈通知判断）

        处理链路：
        1. 钳制原始 delta → 乘以倍率 → 用扩大上限钳制（修复倍率失效）
        2. 首因效应保护（新用户前几次评价缩小负向 delta）
        3. 高好感辱骂豁免
        4. 恋人档渐进减速（lover_growth_rate 平滑衰减）
        5. 降级缓冲（高级档扣分先消耗缓冲再掉级）
        6. 晋级门槛校验（从 levels 模块统一查询）
        7. 写入变更 + 记录原因
        """
        user = self.get_user(user_id, cfg.score.default_score)
        risk = normalize_risk(risk, reason)
        old_score = int(user.get("score", cfg.score.default_score) or 0)

        # 步骤 1：倍率计算（修复版）
        adj_delta = self._apply_multiplier(delta, cfg)

        # 步骤 2：首因效应保护
        eval_count = (int(user.get("positive_eval_count", 0) or 0)
                      + int(user.get("negative_eval_count", 0) or 0))
        if adj_delta < 0 and eval_count < 5:
            adj_delta = max(-2, round(adj_delta * 0.5))

        # 步骤 3：高好感辱骂豁免
        if old_score >= int(cfg.score.ignore_abuse_negative_min_score) and adj_delta < 0:
            if risk in {"insult", "sexual_harassment"}:
                adj_delta = 0

        # 步骤 4：恋人档渐进减速
        if adj_delta > 0 and old_score >= 91 and cfg.progression.lover_growth_slowdown:
            adj_delta = max(1, round(adj_delta * lover_growth_rate(old_score)))

        # 保存缓冲前原始变化值（用于反馈通知）
        raw_delta = adj_delta

        # 步骤 5：降级缓冲（可配置开关）
        if cfg.progression.demotion_buffer_enabled:
            adj_delta = self._apply_demotion_buffer(old_score, adj_delta)

        # 步骤 6：晋级门槛校验
        candidate = clamp(old_score + adj_delta, cfg.score.min_score, cfg.score.max_score)
        candidate = self._check_promotion_gates(old_score, candidate, confidence, user, cfg)

        # 步骤 7：写入变更 + 记录
        actual_delta = candidate - old_score
        user["score"] = candidate
        user["updated_at"] = now()
        user["last_eval_at"] = now()
        if actual_delta > 0:
            user["positive_eval_count"] = int(user.get("positive_eval_count", 0) or 0) + 1
        elif actual_delta < 0:
            user["negative_eval_count"] = int(user.get("negative_eval_count", 0) or 0) + 1
        elif raw_delta != 0 and actual_delta == 0:
            # 缓冲完全吸收了扣分，仍记一次负面评价
            user["negative_eval_count"] = int(user.get("negative_eval_count", 0) or 0) + 1

        # 记录评分原因，缓冲生效时额外标记
        if cfg.privacy.store_reasons and cfg.privacy.max_reason_records > 0:
            record: dict[str, Any] = {
                "delta": actual_delta, "reason": clean_text(reason, 160),
                "confidence": round(float(confidence), 3), "risk": risk,
                "timestamp": now(), "session_id": session_id,
            }
            if raw_delta != actual_delta:
                record["raw_delta"] = raw_delta
                record["buffered"] = True
            self._append_reason(user, record, int(cfg.privacy.max_reason_records))

        self.save_user(user_id, user)
        return user, actual_delta, raw_delta

    @staticmethod
    def _apply_multiplier(delta: int, cfg: FavorabilityConfig) -> int:
        """步骤 1：钳制原始 delta，乘倍率，用扩大上限再钳制"""
        max_raw = max(1, int(cfg.score.max_delta_per_eval))
        raw = clamp(int(delta), -max_raw, max_raw)
        if raw > 0:
            mult = float(cfg.score.positive_delta_multiplier)
            adj = max(1, round(raw * mult))
            return clamp(adj, 1, max(max_raw, round(max_raw * mult)))
        if raw < 0:
            mult = float(cfg.score.negative_delta_multiplier)
            adj = min(-1, round(raw * mult))
            return clamp(adj, -max(max_raw, round(max_raw * mult)), -1)
        return 0

    @staticmethod
    def _apply_demotion_buffer(old_score: int, adj_delta: int) -> int:
        """步骤 5：降级缓冲 — 高级档扣分先消耗缓冲再掉级"""
        if adj_delta >= 0:
            return adj_delta
        buffer = demotion_buffer_for_score(old_score)
        if buffer <= 0:
            return adj_delta
        threshold = score_threshold_for_level(level_for_score(old_score))
        if threshold is None:
            return adj_delta
        floor = max(threshold, old_score - buffer)
        candidate = old_score + adj_delta
        if candidate < floor:
            return floor - old_score
        return adj_delta

    @staticmethod
    def _check_promotion_gates(
        old_score: int, candidate: int, confidence: float,
        user: dict[str, Any], cfg: FavorabilityConfig,
    ) -> int:
        """步骤 6：晋级门槛校验"""
        new_level = level_for_score(candidate)
        old_level = level_for_score(old_score)
        if new_level == old_level or new_level not in GATE_LEVELS:
            return candidate
        if new_level == "喜欢的人" and confidence < cfg.progression.liked_unlock_min_confidence:
            threshold = score_threshold_for_level("喜欢的人")
            if threshold is not None:
                candidate = min(candidate, threshold - 1)
        if new_level == "恋人":
            enough_conf = confidence >= cfg.progression.lover_unlock_min_confidence
            enough_hist = int(user.get("positive_eval_count", 0) or 0) >= cfg.progression.lover_min_positive_eval_count
            if not (enough_conf and enough_hist):
                threshold = score_threshold_for_level("恋人")
                if threshold is not None:
                    candidate = min(candidate, threshold - 1)
        return candidate

    # ── 久未互动衰减 ─────────────────────────────────────────────

    def _calculate_inactivity_decay(
        self, user: dict[str, Any], cfg: FavorabilityConfig, ts: float
    ) -> tuple[int, int, int]:
        """计算因长期未互动导致的好感度衰减。返回 (新分数, 实际变化值, 未互动天数)"""
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
        """应用久未互动衰减并写入数据库"""
        user = self.get_user(user_id, cfg.score.default_score)
        ts = now()
        new_score, actual_delta, elapsed = self._calculate_inactivity_decay(user, cfg, ts)
        user["last_interaction_at"] = ts
        if actual_delta == 0:
            self.save_user(user_id, user)
            return user, 0
        user["score"] = new_score
        user["updated_at"] = ts
        if cfg.privacy.store_reasons and cfg.privacy.max_reason_records > 0:
            self._append_reason(user, {
                "delta": actual_delta,
                "reason": f"连续 {elapsed} 天未互动，好感度自然衰减",
                "confidence": 1.0, "risk": "inactivity_decay",
                "timestamp": ts, "session_id": session_id,
            }, int(cfg.privacy.max_reason_records))
        self.save_user(user_id, user)
        return user, actual_delta
