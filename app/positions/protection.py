"""保护状态机控制器 (2026-05-16)

API:
    ProtectionController.takeover(symbol, reason)   # PROTECTED → MANAGED
    ProtectionController.release(symbol, reason)    # MANAGED → PROTECTED
    ProtectionController.freeze(symbol, reason)     # 任意 → FROZEN
    ProtectionController.unfreeze(symbol, reason)   # FROZEN → 上一状态

所有变更写 audit log (decisions.db.position_protection_log) 留痕.
"""
from __future__ import annotations
import logging
import sqlite3
import threading
import time
from datetime import datetime
from typing import Optional, List, Dict

from .source import (
    PositionSource, ProtectionState,
    get_position_source, get_protection_state,
)

logger = logging.getLogger(__name__)


class ProtectionController:
    """全局保护开关 (单例) — 用户/CLI 调用入口"""

    def __init__(self, position_store=None, audit_db_path: Optional[str] = None):
        self.position_store = position_store
        self._lock = threading.Lock()
        self._audit_db = audit_db_path or self._default_audit_db()
        self._ensure_audit_table()

    @staticmethod
    def _default_audit_db() -> str:
        try:
            from app.core.paths import get_decisions_db
            return str(get_decisions_db())
        except Exception:
            from pathlib import Path
            return str(Path(__file__).resolve().parent.parent.parent / "data" / "decisions.db")

    def _ensure_audit_table(self):
        try:
            conn = sqlite3.connect(self._audit_db)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS position_protection_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT,
                    reason TEXT,
                    actor TEXT,
                    timestamp TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pp_symbol ON position_protection_log(symbol)")
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"[ProtectionController] schema init: {e}")

    def takeover(self, symbol: str, reason: str = "", actor: str = "user") -> bool:
        """显式接管 — PROTECTED/FROZEN → MANAGED.

        策略此后可全权管理此仓位 (close/add/move_sl).
        """
        return self._transition(symbol, ProtectionState.MANAGED, reason, actor)

    def release(self, symbol: str, reason: str = "", actor: str = "user") -> bool:
        """释放管理权 — MANAGED → PROTECTED.

        策略停止自动操作, 用户接管.
        """
        return self._transition(symbol, ProtectionState.PROTECTED, reason, actor)

    def freeze(self, symbol: str, reason: str = "", actor: str = "user") -> bool:
        """冻结 — 任意状态 → FROZEN.

        所有操作禁止 (策略 + 自动监控). 例: 财报前.
        """
        return self._transition(symbol, ProtectionState.FROZEN, reason, actor)

    def unfreeze(self, symbol: str, target_state: ProtectionState = ProtectionState.PROTECTED,
                 reason: str = "", actor: str = "user") -> bool:
        """解冻 → 默认回到 PROTECTED (用户重新决定接管或释放)"""
        return self._transition(symbol, target_state, reason, actor)

    def get_state(self, symbol: str) -> Optional[ProtectionState]:
        """查询当前 protection 状态"""
        if self.position_store is None:
            return None
        pos = self.position_store.get(symbol)
        if pos is None:
            return None
        return get_protection_state(pos)

    def list_protected(self) -> List[Dict]:
        """列出所有 PROTECTED + FROZEN 仓位 (用于 CLI 审计)"""
        if self.position_store is None:
            return []
        out = []
        for p in self.position_store.all():
            ps = get_protection_state(p)
            if ps in (ProtectionState.PROTECTED, ProtectionState.FROZEN):
                out.append({
                    "symbol": p.symbol,
                    "source": get_position_source(p).value,
                    "protection": ps.value,
                    "size": getattr(p, "size", 0),
                    "entry_price": getattr(p, "avg_entry_price", 0),
                })
        return out

    def _transition(self, symbol: str, target: ProtectionState,
                    reason: str, actor: str) -> bool:
        if self.position_store is None:
            logger.warning(f"[ProtectionController] position_store 未注入, {symbol} 操作跳过")
            return False

        with self._lock:
            pos = self.position_store.get(symbol)
            if pos is None:
                logger.warning(f"[ProtectionController] {symbol} 不存在持仓, 跳过")
                return False

            current = get_protection_state(pos)
            if current == target:
                logger.info(f"[ProtectionController] {symbol} 已是 {target.value}, 无需变更")
                return True

            # 修改 metadata
            meta = getattr(pos, "metadata", None) or {}
            if not isinstance(meta, dict):
                meta = {}
            meta["protection"] = target.value
            meta["protection_changed_at"] = datetime.now().isoformat()
            meta["protection_actor"] = actor
            try:
                pos.metadata = meta
            except Exception:
                pass

            # 审计落库
            try:
                conn = sqlite3.connect(self._audit_db)
                conn.execute(
                    "INSERT INTO position_protection_log "
                    "(symbol, from_state, to_state, reason, actor) VALUES (?,?,?,?,?)",
                    (symbol, current.value, target.value, reason, actor)
                )
                conn.commit()
                conn.close()
            except Exception as e:
                logger.warning(f"[ProtectionController·audit] {e}")

            logger.warning(
                f"[ProtectionController] {symbol} {current.value} → {target.value} "
                f"actor={actor} reason={reason}"
            )
            return True


_singleton: Optional[ProtectionController] = None
_singleton_lock = threading.Lock()


def get_protection_controller(position_store=None) -> ProtectionController:
    """全局单例 — 首次调用必须传 position_store"""
    global _singleton
    if _singleton is not None:
        if position_store is not None and _singleton.position_store is None:
            _singleton.position_store = position_store
        return _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = ProtectionController(position_store=position_store)
    return _singleton
