#!/usr/bin/env python3
"""
backfill_voting_data.py - 从 decisions.db 补全 trade_records 的投票信息

Phase 2 (2026-05-31): 完整交易链路归因

策略：
1. 查询所有 trade_records 中 voting_result IS NULL 的记录
2. 根据 symbol + 时间窗口 (entry_time ±5min) 匹配 decisions.db 中的记录
3. 将匹配到的投票数据写入 voting_result / model_scores / consensus_level / final_strength

幂等性：
- 只更新 voting_result IS NULL 的记录
- 重复运行不会重复写入

用法：
    python scripts/backfill_voting_data.py --dry-run        # 仅打印匹配情况
    python scripts/backfill_voting_data.py                  # 实际写入
    python scripts/backfill_voting_data.py --window 600     # 自定义匹配窗口 (秒)
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ───────────────────────────────────────────────────────────────
# 路径解析: 优先用 app.core.paths, fallback 到默认相对路径
# ───────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from app.core.paths import get_decisions_db, get_trade_records_db
    DECISIONS_DB = str(get_decisions_db())
    TRADE_DB = str(get_trade_records_db())
except Exception:  # 配置加载失败时兜底
    DECISIONS_DB = str(PROJECT_ROOT / "data" / "decisions.db")
    TRADE_DB = str(PROJECT_ROOT / "data" / "ftseek_us.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("backfill_voting_data")


# ───────────────────────────────────────────────────────────────
# 时间窗口匹配
# ───────────────────────────────────────────────────────────────

def _parse_iso(ts: str) -> Optional[datetime]:
    """解析 ISO 时间戳, 容错处理多种格式"""
    if not ts:
        return None
    try:
        # 兼容 'YYYY-MM-DDTHH:MM:SS' / '...+00:00' / 'YYYY-MM-DD HH:MM:SS'
        t = ts.replace("T", " ").split(".")[0]
        # 去掉时区后缀
        for tz in ("+00:00", "+08:00", "Z"):
            if t.endswith(tz):
                t = t[: -len(tz)]
        return datetime.fromisoformat(t.strip())
    except Exception:
        try:
            return datetime.strptime(ts[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None


def load_unmatched_trades(trade_db: str) -> List[Dict[str, Any]]:
    """加载所有 voting_result IS NULL 的 trade_records"""
    conn = sqlite3.connect(trade_db)
    try:
        rows = conn.execute("""
            SELECT id, trade_date, symbol, direction, entry_time
            FROM trade_records
            WHERE voting_result IS NULL OR voting_result = '' OR voting_result = '{}'
            ORDER BY entry_time ASC
        """).fetchall()
        return [
            {
                "id": r[0],
                "trade_date": r[1],
                "symbol": r[2],
                "direction": r[3],
                "entry_time": r[4],
            }
            for r in rows
        ]
    finally:
        conn.close()


def load_decisions_for_symbol(
    decisions_db: str, symbol: str, dt_lower: datetime, dt_upper: datetime
) -> List[Dict[str, Any]]:
    """从 decisions.db 加载指定 symbol 在时间窗口内的决策记录"""
    conn = sqlite3.connect(decisions_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT decision_id, symbol, direction, final_strength, consensus_level,
                   std_dev, llm_weight, llm_strength, market_regime, vix, created_at
            FROM decisions
            WHERE symbol = ?
              AND created_at BETWEEN ? AND ?
            ORDER BY created_at ASC
        """, (symbol, dt_lower.isoformat(), dt_upper.isoformat())).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def load_model_votes(decisions_db: str, decision_id: str) -> List[Dict[str, Any]]:
    """加载某 decision 的所有 model_votes"""
    conn = sqlite3.connect(decisions_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT model_id, model_role, strength, direction, confidence, weight, reasoning
            FROM model_votes
            WHERE decision_id = ?
        """, (decision_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def find_best_match(
    trade: Dict[str, Any],
    decisions: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """从候选 decisions 中挑选与 trade 最近且方向匹配的记录"""
    if not decisions:
        return None
    entry_dt = _parse_iso(trade.get("entry_time"))
    if entry_dt is None:
        return None
    trade_dir = (trade.get("direction") or "").upper()

    best, best_delta = None, None
    for d in decisions:
        d_dt = _parse_iso(d.get("created_at"))
        if d_dt is None:
            continue
        # 优先方向一致, 不一致也保留作为兜底
        delta = abs((entry_dt - d_dt).total_seconds())
        dir_match = (d.get("direction") or "").upper() == trade_dir
        # 方向匹配的优先级远高于时间差: 方向不匹配的 delta + 1e6
        score = delta if dir_match else delta + 1_000_000
        if best is None or score < best_delta:
            best = d
            best_delta = score
    return best


# ───────────────────────────────────────────────────────────────
# 写入逻辑
# ───────────────────────────────────────────────────────────────

def update_trade_voting(
    trade_db: str,
    trade_id: int,
    decision: Dict[str, Any],
    model_votes: List[Dict[str, Any]],
) -> None:
    """把投票信息写入 trade_records (仅当原值为 NULL)"""
    voting_result = {
        "decision_id": decision.get("decision_id"),
        "direction": decision.get("direction"),
        "final_strength": decision.get("final_strength"),
        "consensus_level": decision.get("consensus_level"),
        "std_dev": decision.get("std_dev"),
        "llm_weight": decision.get("llm_weight"),
        "llm_strength": decision.get("llm_strength"),
        "market_regime": decision.get("market_regime"),
        "vix": decision.get("vix"),
        "created_at": decision.get("created_at"),
        "model_votes_count": len(model_votes),
    }
    model_scores = [
        {
            "model_id": v.get("model_id"),
            "model_role": v.get("model_role"),
            "strength": v.get("strength"),
            "direction": v.get("direction"),
            "confidence": v.get("confidence"),
            "weight": v.get("weight"),
        }
        for v in model_votes
    ]

    conn = sqlite3.connect(trade_db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            UPDATE trade_records
            SET voting_result = ?,
                model_scores = ?,
                consensus_level = COALESCE(consensus_level, ?),
                final_strength = COALESCE(NULLIF(final_strength, 0), ?)
            WHERE id = ?
              AND (voting_result IS NULL OR voting_result = '' OR voting_result = '{}')
        """, (
            json.dumps(voting_result, ensure_ascii=False),
            json.dumps(model_scores, ensure_ascii=False),
            decision.get("consensus_level"),
            decision.get("final_strength"),
            trade_id,
        ))
        conn.commit()
    finally:
        conn.close()


# ───────────────────────────────────────────────────────────────
# 主流程
# ───────────────────────────────────────────────────────────────

def run(window_seconds: int, dry_run: bool, limit: Optional[int]) -> Dict[str, int]:
    logger.info(f"trade_db   = {TRADE_DB}")
    logger.info(f"decisions  = {DECISIONS_DB}")
    logger.info(f"window     = ±{window_seconds}s")
    logger.info(f"dry_run    = {dry_run}")

    if not Path(TRADE_DB).exists():
        logger.error(f"trade_db 不存在: {TRADE_DB}")
        return {"total": 0, "matched": 0, "written": 0, "skipped": 0}
    if not Path(DECISIONS_DB).exists():
        logger.error(f"decisions_db 不存在: {DECISIONS_DB}")
        return {"total": 0, "matched": 0, "written": 0, "skipped": 0}

    trades = load_unmatched_trades(TRADE_DB)
    if limit:
        trades = trades[:limit]
    logger.info(f"待回填交易: {len(trades)} 笔")

    matched, written, skipped = 0, 0, 0
    for t in trades:
        entry_dt = _parse_iso(t.get("entry_time"))
        if entry_dt is None:
            skipped += 1
            continue
        lower = entry_dt - timedelta(seconds=window_seconds)
        upper = entry_dt + timedelta(seconds=window_seconds)
        candidates = load_decisions_for_symbol(DECISIONS_DB, t["symbol"], lower, upper)
        match = find_best_match(t, candidates)

        if not match:
            skipped += 1
            logger.debug(
                f"  ⛔ no match id={t['id']} {t['symbol']} {t['entry_time']} "
                f"candidates={len(candidates)}"
            )
            continue

        matched += 1
        votes = load_model_votes(DECISIONS_DB, match["decision_id"])
        delta = abs((entry_dt - _parse_iso(match["created_at"])).total_seconds())
        logger.info(
            f"  ✓ id={t['id']:>5} {t['symbol']:<10} {t['direction']:<6} "
            f"entry={t['entry_time']} ↔ decision={match['decision_id']} "
            f"Δ{delta:>5.0f}s votes={len(votes)} strength={match.get('final_strength')}"
        )

        if not dry_run:
            try:
                update_trade_voting(TRADE_DB, t["id"], match, votes)
                written += 1
            except Exception as e:
                logger.error(f"  写入失败 id={t['id']}: {e}")

    summary = {
        "total": len(trades),
        "matched": matched,
        "written": written,
        "skipped": skipped,
    }
    logger.info(f"完成: {summary}")
    return summary


def main():
    parser = argparse.ArgumentParser(description="Backfill voting data from decisions.db to trade_records")
    parser.add_argument("--window", type=int, default=300,
                        help="时间匹配窗口 (秒), 默认 300 (即 ±5min)")
    parser.add_argument("--dry-run", action="store_true", help="只打印匹配情况, 不写入")
    parser.add_argument("--limit", type=int, default=None, help="限制处理条数 (调试用)")
    args = parser.parse_args()

    summary = run(window_seconds=args.window, dry_run=args.dry_run, limit=args.limit)
    return 0 if summary["total"] >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
