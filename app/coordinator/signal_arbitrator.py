"""
SignalArbitrator — 多策略对同 symbol 出信号时仲裁 (Phase A 2026-05-14)

按用户决策 (2026-05-14): priority × confidence 评分.
平手按 priority 决断, 再平按"先到先得".

观察模式: 仍然返回 winner, 同时记录 candidates_json 用于后续"被压制信号是否本可赚"分析.
"""
from __future__ import annotations
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ArbitrationResult:
    winner_strategy: Optional[str]
    winner_signal: Any  # SignalScore
    score: float
    candidates: List[Dict[str, Any]]  # [{strategy, priority, confidence, score, direction}]
    timestamp: float = field(default_factory=time.time)


class SignalArbitrator:
    """多信号仲裁 (无状态, 但有审计 DB 写入)"""

    def __init__(self, audit_db_path: Optional[str] = None, audit_enabled: bool = True):
        self._audit_path = audit_db_path
        self._audit_enabled = audit_enabled
        self._lock = threading.Lock()
        if audit_enabled and audit_db_path:
            self._ensure_schema()

    def _ensure_schema(self):
        try:
            conn = sqlite3.connect(self._audit_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signal_arbitration (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol          TEXT NOT NULL,
                    timestamp       REAL NOT NULL,
                    winner_strategy TEXT,
                    winner_score    REAL,
                    n_candidates    INTEGER,
                    candidates_json TEXT,
                    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_arb_symbol ON signal_arbitration(symbol)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_arb_ts ON signal_arbitration(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_arb_winner ON signal_arbitration(winner_strategy)")
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"[SignalArbitrator] schema 初始化失败 (审计降级): {e}")
            self._audit_enabled = False

    def arbitrate(
        self,
        symbol: str,
        candidates: List[Tuple[Any, Any]],  # [(strategy_obj, signal_obj)]
        ctx: Optional[Any] = None,  # T8b: 用于 effective_priority 计算
    ) -> Optional[ArbitrationResult]:
        """
        Args:
            candidates: [(strategy, signal)]
                strategy 必须有 .name 和 .priority 属性 (BaseStrategy 子类)
                signal 必须有 .confidence 和 .direction 属性 (SignalScore)
            ctx: 可选, 含 regime 等市场状态 → 走 strategy.effective_priority(ctx)
        Returns:
            None 如果 candidates 空, 否则 ArbitrationResult
        """
        if not candidates:
            return None

        scored: List[Tuple[float, int, Any, Any]] = []
        candidate_dicts: List[Dict[str, Any]] = []
        for strat, sig in candidates:
            # T8b (2026-05-16): regime-aware priority 优先, fallback 静态 priority
            if hasattr(strat, "effective_priority"):
                try:
                    priority = int(strat.effective_priority(ctx))
                except Exception:
                    priority = int(getattr(strat, "priority", 5))
            else:
                priority = int(getattr(strat, "priority", 5))
            confidence = float(getattr(sig, "confidence", 0.0) or 0.0)
            direction = str(getattr(sig, "direction", ""))
            score = priority * confidence
            scored.append((score, priority, strat, sig))
            candidate_dicts.append({
                "strategy": getattr(strat, "name", "?"),
                "priority": priority,
                "confidence": confidence,
                "direction": direction,
                "score": score,
            })

        scored.sort(key=lambda x: (-x[0], -x[1]))
        winner_score, _, winner_strat, winner_sig = scored[0]
        winner_name = getattr(winner_strat, "name", "?")

        result = ArbitrationResult(
            winner_strategy=winner_name,
            winner_signal=winner_sig,
            score=winner_score,
            candidates=candidate_dicts,
        )

        if self._audit_enabled and len(candidates) > 1:
            self._record_audit(symbol, result)

        if len(candidates) > 1:
            logger.info(
                f"[SignalArbitrator] {symbol} 仲裁: winner={winner_name} score={winner_score:.3f} "
                f"(candidates: {[c['strategy']+':'+str(round(c['score'],2)) for c in candidate_dicts]})"
            )
        return result

    def _record_audit(self, symbol: str, result: ArbitrationResult):
        try:
            with self._lock:
                conn = sqlite3.connect(self._audit_path)
                conn.execute(
                    "INSERT INTO signal_arbitration "
                    "(symbol, timestamp, winner_strategy, winner_score, n_candidates, candidates_json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        symbol,
                        result.timestamp,
                        result.winner_strategy,
                        result.score,
                        len(result.candidates),
                        json.dumps(result.candidates, ensure_ascii=False),
                    ),
                )
                conn.commit()
                conn.close()
        except Exception as e:
            logger.warning(f"[SignalArbitrator] 写审计失败: {e}")


_singleton: Optional[SignalArbitrator] = None


def get_signal_arbitrator(audit_db_path: Optional[str] = None) -> SignalArbitrator:
    global _singleton
    if _singleton is None:
        if audit_db_path is None:
            from app.core.paths import get_decisions_db
            audit_db_path = str(get_decisions_db())
        _singleton = SignalArbitrator(audit_db_path=audit_db_path)
    return _singleton
