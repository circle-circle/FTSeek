"""
Plan Feedback — 收盘后反馈脚本 (Stage 闭环)

每日收盘后执行:
1. Plan Win Rate — target触及TP的比例
2. Plan Alpha — target累积收益 vs SPY
3. Factor IC — 每个因子与次日收益的Spearman相关
4. Plan→Trade匹配 — 标记当日交易的strategy=battle_plan

输出: data/plan_feedback/YYYY-MM-DD.json
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("ftseek_us.plan_feedback")

DB_PATH = Path(__file__).parent.parent.parent / "data" / "ftseek_us_live.db"
PLAN_PATH = Path(__file__).parent.parent.parent / "data" / "daily_battle_plan_us.json"
FACTOR_PATH = Path(__file__).parent.parent.parent / "data" / "factor_screen_latest.json"
OUTPUT_DIR = Path(__file__).parent.parent.parent / "data" / "plan_feedback"


def _get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _load_plan(date_str: str = None) -> Optional[Dict]:
    """加载当日或最近一日的作战计划"""
    if not PLAN_PATH.exists():
        return None
    try:
        plan = json.loads(PLAN_PATH.read_text())
        plan_date = plan.get("date", "")
        if date_str and plan_date != date_str:
            return None
        return plan
    except Exception:
        return None


def _load_factor_screen() -> Optional[Dict]:
    """加载因子筛选结果"""
    if not FACTOR_PATH.exists():
        return None
    try:
        return json.loads(FACTOR_PATH.read_text())
    except Exception:
        return None


def get_fwd_returns(symbols: List[str], days: int = 1) -> Dict[str, float]:
    """获取股票的前N日收益 (用Futu K线)"""
    results = {}
    try:
        # 2026-07-25: 改单例 + 不 disconnect (handoff 07-24 解锁spam同款反模式收敛)
        from app.web_dashboard.manual_order_service import get_manual_order_service
        svc = get_manual_order_service()
        if not svc.connect():
            return results

        from futu import RET_OK, SubType
        for symbol in symbols:
            try:
                svc._quote_ctx.subscribe([symbol], [SubType.K_DAY])
            except Exception:
                pass

        import time; time.sleep(0.5)

        for symbol in symbols:
            try:
                ret, data = svc._quote_ctx.get_cur_kline(symbol, days + 5, ktype="K_DAY")
                if ret == RET_OK and not data.empty and len(data) >= days + 1:
                    closes = [float(r["close"]) for _, r in data.iterrows()]
                    # 从倒数第days+1天到倒数第1天
                    fwd = (closes[-1] - closes[-(days + 1)]) / closes[-(days + 1)] * 100
                    results[symbol] = round(fwd, 2)
            except Exception:
                pass

    except Exception as e:
        logger.debug(f"[PlanFeedback] fwd returns error: {e}")
    return results


def compute_factor_ic(factor_screen: Dict) -> Dict[str, float]:
    """计算每个因子与次日收益的Spearman相关 (IC)"""
    candidates = factor_screen.get("candidates", [])
    if len(candidates) < 3:
        return {}

    symbols = [c["symbol"] for c in candidates]
    fwd_returns = get_fwd_returns(symbols, days=1)

    if len(fwd_returns) < 3:
        return {}

    # Build paired data
    factor_names = list(REGIME_WEIGHTS.get("NEUTRAL", {}).keys()) or [
        "sector_momentum", "relative_strength", "ma_alignment",
        "volume_breakout", "rsi_sweet_spot"
    ]

    from statistics import correlation
    ic_results = {}

    for fname in factor_names:
        scores = []
        returns = []
        for c in candidates:
            sym = c["symbol"]
            if sym not in fwd_returns:
                continue
            f = c.get("factors", {}).get(fname, {})
            score = f.get("score")
            if score is None:
                continue
            scores.append(score)
            returns.append(fwd_returns[sym])

        if len(scores) >= 3:
            try:
                # Spearman rank correlation
                n = len(scores)
                rank_s = _rank(scores)
                rank_r = _rank(returns)
                d_sq = sum((rs - rr) ** 2 for rs, rr in zip(rank_s, rank_r))
                rho = 1 - (6 * d_sq) / (n * (n * n - 1))
                ic_results[fname] = round(rho, 3)
            except Exception:
                ic_results[fname] = None

    return ic_results


def _rank(values: List[float]) -> List[float]:
    """Simple rank assignment (average for ties)"""
    indexed = sorted(enumerate(values), key=lambda x: x[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j < len(indexed) and indexed[j][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j + 1) / 2  # 1-based average
        for k in range(i, j):
            ranks[indexed[k][0]] = avg_rank
        i = j
    return ranks


def compute_plan_effectiveness(plan: Dict) -> Dict:
    """评估plan targets的有效性"""
    targets = plan.get("targets", [])
    if not targets:
        return {"count": 0}

    symbols = [t.get("symbol", "") for t in targets if t.get("symbol")]
    fwd_returns = get_fwd_returns(symbols, days=1)

    results = []
    for t in targets:
        sym = t.get("symbol", "")
        if sym not in fwd_returns:
            continue

        fwd = fwd_returns[sym]
        direction = t.get("direction", "LONG")
        entry_zone = t.get("entry_zone", [])
        tp = t.get("take_profit", 0)
        sl = t.get("stop_loss", 0)

        # 简化的hit判断
        entry_mid = sum(entry_zone) / len(entry_zone) if entry_zone else 0
        pnl_pct = fwd if direction == "LONG" else -fwd

        result = {
            "symbol": sym,
            "direction": direction,
            "entry_zone": entry_zone,
            "fwd_return": fwd,
            "pnl_pct": round(pnl_pct, 2),
            "hit_tp": (tp > 0 and pnl_pct > 0),  # 简化: 正收益=部分命中
            "hit_sl": (sl > 0 and pnl_pct < -2),  # 简化: 跌>2%=触及止损
        }
        results.append(result)

    if not results:
        return {"count": 0, "targets": []}

    win_count = sum(1 for r in results if r["pnl_pct"] > 0)
    avg_pnl = sum(r["pnl_pct"] for r in results) / len(results)

    return {
        "count": len(results),
        "win_count": win_count,
        "win_rate": round(win_count / len(results) * 100, 1),
        "avg_pnl_pct": round(avg_pnl, 2),
        "total_pnl_pct": round(sum(r["pnl_pct"] for r in results), 2),
        "targets": results,
    }


def match_plan_trades(plan: Dict) -> List[Dict]:
    """将当日交易与plan targets匹配"""
    targets = plan.get("targets", [])
    if not targets:
        return []

    target_symbols = {t.get("symbol", ""): t for t in targets}

    conn = _get_db()
    from app.utils.timezone import get_trade_date
    today = get_trade_date()
    rows = conn.execute(
        "SELECT symbol, direction, entry_price, exit_price, realized_pnl, "
        "close_time, strategy FROM trade_records "
        "WHERE trade_date = ? AND trade_mode != 'simulate'",
        (today,)
    ).fetchall()
    conn.close()

    matched = []
    for row in rows:
        sym = row["symbol"]
        if sym in target_symbols:
            target = target_symbols[sym]
            matched.append({
                "symbol": sym,
                "trade_direction": row["direction"],
                "plan_direction": target.get("direction"),
                "direction_match": row["direction"] == target.get("direction"),
                "entry_price": row["entry_price"],
                "plan_entry_zone": target.get("entry_zone", []),
                "realized_pnl": row["realized_pnl"],
                "strategy": row["strategy"],
            })

    return matched


def run_feedback(date_str: str = None) -> Dict:
    """
    执行收盘反馈全流程。
    
    Returns: {
        "date": "2026-06-14",
        "factor_ic": {...},
        "plan_effectiveness": {...},
        "plan_trades": [...],
        "regime": "...",
    }
    """
    if not date_str:
        from app.utils.timezone import get_trade_date
        date_str = get_trade_date()

    result = {
        "date": date_str,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # 1. Factor IC
    factor_screen = _load_factor_screen()
    if factor_screen:
        result["factor_ic"] = compute_factor_ic(factor_screen)
        result["regime"] = factor_screen.get("regime", "NEUTRAL")
    else:
        result["factor_ic"] = {}
        result["regime"] = "UNKNOWN"

    # 2. Plan effectiveness
    plan = _load_plan(date_str)
    if plan:
        result["plan_effectiveness"] = compute_plan_effectiveness(plan)
        result["plan_trades"] = match_plan_trades(plan)
    else:
        result["plan_effectiveness"] = {"count": 0}
        result["plan_trades"] = []

    # 3. Save
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUTPUT_DIR / f"feedback_{date_str}.json"
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        logger.info(f"[PlanFeedback] ✅ Saved to {out_path}")
    except Exception as e:
        logger.error(f"[PlanFeedback] save error: {e}")

    return result


# Import at module level for compute_factor_ic
from app.plan.factor_engine import REGIME_WEIGHTS


if __name__ == "__main__":
    """CLI: python scripts/plan_feedback.py [YYYY-MM-DD]"""
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    result = run_feedback(date_arg)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
