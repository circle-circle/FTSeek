"""
信号收件箱路由 — 展示今日所有信号（已执行/已拒绝/待处理）

数据源:
  - signal_lifecycle (ftseek_us.db): 已执行的信号 + 交易结果
  - rejections (rejections.db): 被风控拒绝的信号 + 拒绝原因
  - trade_records (ftseek_us.db): 补充交易细节
"""
import asyncio
import logging
from datetime import datetime
from fastapi import APIRouter

from app.web_dashboard.data_service import get_data_service, et_today

logger = logging.getLogger("ftseek_us.routes.signal_inbox")
router = APIRouter()


def register(app, templates):
    @router.get("/api/signals/inbox")
    async def signals_inbox():
        """今日信号收件箱 — 合并已执行 + 已拒绝信号"""
        ds = get_data_service()
        cached = ds.cache.get("signals_inbox")
        if cached is not None:
            return cached

        today = et_today()

        # 并行查询 3 个数据源
        executed_task = _fetch_executed_signals(ds, today)
        rejected_task = _fetch_rejected_signals(ds, today)
        pending_task = _fetch_pending_signals(ds, today)

        executed, rejected, pending = await asyncio.gather(
            executed_task, rejected_task, pending_task,
        )

        # 合并 + 排序 (按时间倒序)
        all_signals = executed + rejected + pending
        all_signals.sort(key=lambda s: s.get("time", ""), reverse=True)

        # 统计
        stats = {
            "total": len(all_signals),
            "executed": len(executed),
            "rejected": len(rejected),
            "pending": len(pending),
            "pass_rate": round(len(executed) / len(all_signals) * 100, 1) if all_signals else 0,
            "total_pnl": round(sum(s.get("pnl", 0) or 0 for s in executed), 2),
        }

        result = {"signals": all_signals, "stats": stats}
        ds.cache.set("signals_inbox", result, ttl=15)
        return result

    app.include_router(router)


async def _fetch_executed_signals(ds, today: str) -> list:
    """从 signal_lifecycle 获取今日已执行的信号"""
    try:
        rows = await ds.db_query("""
            SELECT sl.symbol, sl.signal_direction, sl.trade_direction,
                   sl.signal_strength, sl.signal_confidence,
                   sl.entry_price, sl.exit_price, sl.realized_pnl,
                   sl.pnl_pct, sl.exit_reason, sl.holding_hours,
                   sl.owner_strategy, sl.source_type, sl.trade_mode,
                   sl.created_at, sl.trade_id
            FROM signal_lifecycle sl
            WHERE sl.trade_date = ?
            ORDER BY sl.created_at DESC
        """, (today,))

        signals = []
        for r in rows:
            pnl = float(r.get("realized_pnl") or 0)
            pnl_pct = float(r.get("pnl_pct") or 0) * 100
            direction = r.get("signal_direction") or r.get("trade_direction") or ""
            source = r.get("source_type") or r.get("owner_strategy") or ""

            signals.append({
                "time": (r.get("created_at") or "")[:19],
                "symbol": r.get("symbol", ""),
                "direction": direction,
                "confidence": round(float(r.get("signal_confidence") or 0), 3),
                "strength": int(r.get("signal_strength") or 0),
                "strategy": source,
                "status": "executed",
                "entry_price": round(float(r.get("entry_price") or 0), 2),
                "exit_price": round(float(r.get("exit_price") or 0), 2) if r.get("exit_price") else None,
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
                "exit_reason": r.get("exit_reason", ""),
                "holding_hours": round(float(r.get("holding_hours") or 0), 1),
                "is_open": r.get("exit_price") is None or float(r.get("exit_price") or 0) == 0,
            })
        return signals
    except Exception as e:
        logger.error(f"[SignalInbox] executed signals failed: {e}")
        return []


async def _fetch_rejected_signals(ds, today: str) -> list:
    """从 rejections.db 获取今日被拒绝的信号"""
    try:
        from app.core.paths import get_rejections_db
        rej_db = str(get_rejections_db())

        rows = await ds.db_query("""
            SELECT symbol, direction, reason, reason_category,
                   strength, confidence, timestamp
            FROM rejections
            WHERE date(timestamp) = ?
            ORDER BY timestamp DESC
        """, (today,), db_path=rej_db)

        signals = []
        for r in rows:
            direction = (r.get("direction") or "").upper()
            if direction == "BUY":
                direction = "LONG"
            elif direction == "SELL":
                direction = "SHORT"

            signals.append({
                "time": (r.get("timestamp") or "")[:19],
                "symbol": r.get("symbol", ""),
                "direction": direction,
                "confidence": round(float(r.get("confidence") or 0), 3),
                "strength": round(float(r.get("strength") or 0), 0),
                "strategy": "风控拒绝",
                "status": "rejected",
                "reject_reason": r.get("reason", ""),
                "reject_category": r.get("reason_category", ""),
                "entry_price": None,
                "exit_price": None,
                "pnl": 0,
                "pnl_pct": 0,
                "exit_reason": "",
                "holding_hours": 0,
                "is_open": False,
            })
        return signals
    except Exception as e:
        logger.error(f"[SignalInbox] rejected signals failed: {e}")
        return []


async def _fetch_pending_signals(ds, today: str) -> list:
    """从 signal_lifecycle 获取今日尚未平仓的信号 (持仓中)"""
    try:
        rows = await ds.db_query("""
            SELECT sl.symbol, sl.signal_direction, sl.trade_direction,
                   sl.signal_strength, sl.signal_confidence,
                   sl.entry_price, sl.owner_strategy, sl.source_type,
                   sl.created_at
            FROM signal_lifecycle sl
            WHERE sl.trade_date = ?
              AND sl.exit_price IS NULL
            ORDER BY sl.created_at DESC
        """, (today,))

        # 已执行的信号中，未平仓的标记为 pending
        # 但 signal_lifecycle 通常写入时就有 exit_price
        # 所以这里作为兜底，实际大部分信号已在 _fetch_executed_signals 中
        return []
    except Exception:
        return []
