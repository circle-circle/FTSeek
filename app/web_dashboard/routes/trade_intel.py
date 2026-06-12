"""
交易智能路由 — 下单时提供系统智能背书

GET /api/trade/symbol-intel/{symbol}
  返回: 系统信号 + 近期交易 + 当前持仓 + 战前计划匹配
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import APIRouter

from app.web_dashboard.data_service import get_data_service, et_today, et_days_ago, safe_float

logger = logging.getLogger("ftseek_us.routes.trade_intel")
router = APIRouter()

_BATTLE_PLAN_PATH = Path(__file__).parent.parent.parent.parent / "data" / "daily_battle_plan_us.json"


def register(app, templates):
    @router.get("/api/trade/symbol-intel/{symbol}")
    async def symbol_intel(symbol: str):
        """
        标的智能分析 — 输入标的后一站式展示系统认知

        数据源:
          1. signal_lifecycle: 系统信号 (方向/置信度/强度)
          2. trade_records: 近 7 天交易记录
          3. open_positions + batch_quotes: 当前持仓状态
          4. battle_plan: 战前计划匹配度
        """
        ds = get_data_service()
        symbol = symbol.upper()
        if not symbol.startswith("US."):
            symbol = "US." + symbol

        # 并行获取 4 个数据源
        signal_task = _get_signal_intel(ds, symbol)
        trades_task = _get_recent_trades(ds, symbol)
        position_task = _get_position_intel(ds, symbol)
        plan_task = _get_plan_match(symbol)
        quote_task = ds.get_batch_quotes([symbol])

        signal, trades, position, plan, quotes = await asyncio.gather(
            signal_task, trades_task, position_task, plan_task, quote_task,
        )

        cur_price = quotes.get(symbol, 0)

        return {
            "symbol": symbol,
            "cur_price": round(cur_price, 2),
            "signal": signal,
            "recent_trades": trades,
            "position": position,
            "plan": plan,
        }

    app.include_router(router)


async def _get_signal_intel(ds, symbol: str) -> dict:
    """系统信号: 最近的信号方向/置信度/来源"""
    try:
        row = await ds.db_query_one("""
            SELECT signal_direction, signal_confidence, signal_strength,
                   source_type, entry_price, realized_pnl, pnl_pct,
                   exit_reason, trade_date
            FROM signal_lifecycle
            WHERE symbol = ?
            ORDER BY created_at DESC LIMIT 1
        """, (symbol,))

        if not row:
            return {"has_signal": False}

        return {
            "has_signal": True,
            "direction": row.get("signal_direction", ""),
            "confidence": round(float(row.get("signal_confidence") or 0), 3),
            "strength": int(row.get("signal_strength") or 0),
            "source": row.get("source_type", ""),
            "date": row.get("trade_date", ""),
            "result_pnl": round(float(row.get("realized_pnl") or 0), 2),
        }
    except Exception as e:
        logger.debug(f"[Intel] signal query failed: {e}")
        return {"has_signal": False}


async def _get_recent_trades(ds, symbol: str) -> dict:
    """近 7 天交易统计"""
    try:
        week_ago = et_days_ago(7)
        stats = await ds.db_query_one("""
            SELECT COUNT(*) as cnt,
                   SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                   ROUND(SUM(realized_pnl), 2) as net_pnl,
                   ROUND(AVG(realized_pnl), 2) as avg_pnl
            FROM trade_records
            WHERE symbol = ? AND trade_date >= ?
        """, (symbol, week_ago))

        if not stats or stats.get("cnt", 0) == 0:
            return {"count": 0}

        cnt = stats["cnt"]
        wins = stats.get("wins", 0) or 0
        return {
            "count": cnt,
            "wins": wins,
            "win_rate": round(wins / cnt * 100, 1) if cnt > 0 else 0,
            "net_pnl": float(stats.get("net_pnl") or 0),
            "avg_pnl": float(stats.get("avg_pnl") or 0),
        }
    except Exception as e:
        logger.debug(f"[Intel] trades query failed: {e}")
        return {"count": 0}


async def _get_position_intel(ds, symbol: str) -> dict:
    """当前持仓状态"""
    try:
        # 从 DB 查持仓
        row = await ds.db_query_one("""
            SELECT direction, size, avg_entry_price, current_sl, initial_tp,
                   owner_strategy
            FROM open_positions
            WHERE symbol = ? AND (sync_status IS NULL OR sync_status NOT IN
                  ('closed_by_daemon','closed_by_scheduler','closed_by_sync',
                   'closed_emergency','stale_not_in_broker'))
        """, (symbol,))

        if not row:
            return {"held": False}

        size = float(row.get("size", 0))
        cost = float(row.get("avg_entry_price", 0))
        sl = float(row.get("current_sl") or 0)
        tp = float(row.get("initial_tp") or 0)

        # 获取实时价格
        quotes = await ds.get_batch_quotes([symbol])
        cur = quotes.get(symbol, 0)

        direction = row.get("direction", "LONG")
        is_short = direction.upper() == "SHORT"

        pnl = 0
        pnl_pct = 0
        if cur > 0 and cost > 0:
            if is_short:
                pnl = (cost - cur) * size
                pnl_pct = (cost - cur) / cost * 100
            else:
                pnl = (cur - cost) * size
                pnl_pct = (cur - cost) / cost * 100

        sl_dist = (cur - sl) / cur * 100 if cur > 0 and sl > 0 else 0

        return {
            "held": True,
            "direction": direction,
            "size": int(size),
            "cost": round(cost, 2),
            "cur_price": round(cur, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "stop_loss": round(sl, 2) if sl > 0 else None,
            "take_profit": round(tp, 2) if tp > 0 else None,
            "sl_distance_pct": round(sl_dist, 1),
            "owner": row.get("owner_strategy", "auto"),
        }
    except Exception as e:
        logger.debug(f"[Intel] position query failed: {e}")
        return {"held": False}


async def _get_plan_match(symbol: str) -> dict:
    """战前计划匹配"""
    try:
        if not _BATTLE_PLAN_PATH.exists():
            return {"in_plan": False}

        with open(_BATTLE_PLAN_PATH) as f:
            plan = json.load(f)

        for target in plan.get("targets", []):
            if target.get("symbol", "").upper() == symbol.upper():
                entry_zone = target.get("entry_zone", [])
                sl = target.get("stop_loss", 0)
                tp = target.get("take_profit", 0)
                status = target.get("open_check_status", "VALID")

                return {
                    "in_plan": True,
                    "direction": target.get("direction", ""),
                    "entry_zone": entry_zone,
                    "stop_loss": sl,
                    "take_profit": tp,
                    "status": status,
                    "cancelled": status == "CANCELLED",
                    "rationale": (target.get("rationale") or "")[:100],
                }

        return {"in_plan": False}
    except Exception:
        return {"in_plan": False}
