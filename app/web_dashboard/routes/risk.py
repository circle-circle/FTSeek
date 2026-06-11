"""
风控路由 — 风控状态 + PnL 历史 + 策略状态

/api/risk/status 保留向后兼容, 内部使用 DataService 共享缓存
"""
import asyncio
import logging
from datetime import datetime
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.web_dashboard.data_service import get_data_service, safe_float, et_today, et_days_ago

logger = logging.getLogger("ftseek_us.routes.risk")
router = APIRouter()


def register(app, templates):
    @router.get("/risk")
    async def risk_page():
        return RedirectResponse(url="/", status_code=301)

    @router.get("/api/risk/status")
    async def risk_status():
        """风控状态 — 使用 DataService 共享缓存 (向后兼容)"""
        ds = get_data_service()
        cached = ds.cache.get("risk_status_v2")
        if cached is not None:
            return cached

        svc = ds.svc()
        is_live = svc._is_live() if svc._connected else False
        mode = svc._get_mode_label() if svc._connected else "simulate"

        # 并行获取数据 (DataService 有 TTL cache, 与 snapshot 共享)
        acc_raw, pos_raw = await asyncio.gather(
            ds.get_account_raw(),
            ds.get_positions_raw(),
        )

        from app.web_dashboard.routes.dashboard import (
            _parse_account, _parse_positions, _calc_margin,
            _calc_exposure, _calc_pnl, _calc_stop_distances, _parse_risk_level,
        )

        account = _parse_account(acc_raw)
        positions = await _parse_positions(ds, pos_raw)
        margin = _calc_margin(account)
        exposure = _calc_exposure(account)
        pnl = await _calc_pnl(ds, positions)
        stop_distances = await _calc_stop_distances(ds, positions, is_live)
        futu_risk = _parse_risk_level(account)

        result = {
            "mode": mode,
            "is_live": is_live,
            "account": account,
            "margin": margin,
            "exposure": exposure,
            "pnl": pnl,
            "stop_distances": stop_distances,
            "futu_risk": futu_risk,
        }
        ds.cache.set("risk_status_v2", result, ttl=10)
        return result

    @router.get("/api/risk/pnl-history")
    async def risk_pnl_history(days: int = 30):
        ds = get_data_service()
        start_date = et_days_ago(days)
        rows = await ds.db_query(
            "SELECT trade_date, COALESCE(SUM(realized_pnl), 0) as daily_pnl, "
            "COUNT(*) as trades, "
            "SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins "
            "FROM trade_records WHERE trade_date >= ? "
            "GROUP BY trade_date ORDER BY trade_date",
            (start_date,),
        )

        history = []
        # M2: 从 capital_state 获取实际起始资金，而非硬编码 $100K
        starting_equity = 100000  # 默认值
        try:
            first_cap = conn.execute(
                "SELECT current_equity FROM capital_state ORDER BY rowid LIMIT 1"
            ).fetchone()
            if first_cap:
                starting_equity = float(first_cap["current_equity"])
        except Exception:
            pass
        cumulative = starting_equity
        for row in rows:
            daily_pnl = float(row.get("daily_pnl", 0))
            cumulative += daily_pnl
            trades = int(row.get("trades", 0))
            wins = int(row.get("wins", 0))
            history.append({
                "date": row["trade_date"],
                "daily_pnl": round(daily_pnl, 2),
                "total_assets": round(cumulative, 2),
                "trades": trades,
                "win_rate": round(wins / trades * 100, 1) if trades > 0 else 0,
            })

        return {"equity_history": history}

    @router.get("/api/strategy/status")
    async def strategy_status():
        ds = get_data_service()
        today = et_today()
        result = {"strategies": [], "orb": {}, "signals": {}, "service": {}}

        # 1. 服务状态 (H2: non-blocking)
        try:
            import asyncio as _aio
            _proc = await _aio.create_subprocess_exec(
                "systemctl", "is-active", "ftseek-us",
                stdout=_aio.subprocess.PIPE, stderr=_aio.subprocess.PIPE,
            )
            _out, _ = await _aio.wait_for(_proc.communicate(), timeout=5)
            result["service"]["ftseek_us"] = _out.decode().strip()
        except Exception:
            result["service"]["ftseek_us"] = "unknown"

        # 2. 按策略统计
        strat_rows = await ds.db_query("""
            SELECT COALESCE(strategy_id, 'unknown') as strategy,
                   COUNT(*) as trades,
                   SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                   ROUND(SUM(realized_pnl), 2) as pnl,
                   ROUND(AVG(realized_pnl), 2) as avg_pnl
            FROM trade_records
            WHERE trade_date = (SELECT MAX(trade_date) FROM trade_records)
            GROUP BY strategy_id ORDER BY pnl DESC
        """)
        for r in strat_rows:
            trades = r["trades"]
            wins = r.get("wins", 0) or 0
            result["strategies"].append({
                "name": r["strategy"],
                "trades": trades,
                "wins": wins,
                "win_rate": round(wins / trades * 100, 1) if trades > 0 else 0,
                "pnl": float(r.get("pnl") or 0),
                "avg_pnl": float(r.get("avg_pnl") or 0),
            })

        # 3. 最近7天
        weekly = await ds.db_query("""
            SELECT COALESCE(strategy_id, 'unknown') as strategy,
                   COUNT(*) as trades,
                   SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                   ROUND(SUM(realized_pnl), 2) as pnl
            FROM trade_records WHERE trade_date >= ?
            GROUP BY strategy_id ORDER BY pnl DESC
        """, (et_days_ago(7),))
        result["weekly"] = [{
            "name": r["strategy"],
            "trades": r["trades"],
            "wins": r.get("wins", 0) or 0,
            "win_rate": round((r.get("wins", 0) or 0) / r["trades"] * 100, 1) if r["trades"] > 0 else 0,
            "pnl": float(r.get("pnl") or 0),
        } for r in weekly]

        # 4. 今日信号
        try:
            sig_row = await ds.db_query_one(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN status = 'ACCEPTED' THEN 1 ELSE 0 END) as accepted, "
                "SUM(CASE WHEN status = 'REJECTED' THEN 1 ELSE 0 END) as rejected "
                "FROM signal_outcomes WHERE date(timestamp) = ?",
                (today,),
            )
            if sig_row:
                result["signals"] = {
                    "total": sig_row.get("total", 0) or 0,
                    "accepted": sig_row.get("accepted", 0) or 0,
                    "rejected": sig_row.get("rejected", 0) or 0,
                }
        except Exception:
            pass

        # 5. 最近信号明细
        recent = await ds.db_query(
            "SELECT symbol, direction, confidence, strength, status, "
            "substr(timestamp, 12, 8) as time "
            "FROM signal_outcomes WHERE date(timestamp) = ? "
            "ORDER BY timestamp DESC LIMIT 10",
            (today,),
        )
        result["recent_signals"] = [{
            "symbol": r["symbol"],
            "direction": r["direction"],
            "confidence": round(float(r.get("confidence") or 0), 3),
            "strength": int(r.get("strength") or 0),
            "status": r["status"],
            "time": r["time"],
        } for r in recent]

        return result

    app.include_router(router)
