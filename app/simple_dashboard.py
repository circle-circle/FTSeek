"""
Dashboard 数据模块 — 基于实际 DB schema + Futu 实时数据
"""
import os
import sqlite3
import logging
from typing import Dict, List, Any

logger = logging.getLogger("ftseek_us.simple_dashboard")


class SimpleDashboard:
    """Dashboard 数据查询 (适配实际 ftseek_us.db schema)"""

    def __init__(self, db_path: str = None):
        if db_path is None:
            from app.core.db_utils import get_main_db
            db_path = str(get_main_db())
        self.db_path = db_path

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ── 账户概览 (从 Futu 实时获取) ──

    def get_account_summary(self) -> Dict[str, Any]:
        try:
            from app.web_dashboard.manual_order_service import get_manual_order_service
            svc = get_manual_order_service()
            if not svc.connect():
                return {}
            from futu import TrdEnv, RET_OK
            trd_env = TrdEnv.REAL if svc._is_live() else TrdEnv.SIMULATE
            acc_id = int(os.getenv(
                "REAL_ACCOUNT_ID" if svc._is_live() else "SIMULATE_ACCOUNT_ID", "0"
            ))
            ret, funds = svc._broker_ctx.accinfo_query(trd_env=trd_env, acc_id=acc_id)
            if ret != RET_OK:
                return {}
            row = funds.iloc[0]

            def _f(v):
                try:
                    return float(v) if v is not None and v != "N/A" else 0.0
                except (ValueError, TypeError):
                    return 0.0

            raw_cur = str(row.get("currency", "")).upper()
            is_hkd = raw_cur == "HKD"
            fx = 7.8 if is_hkd else 1.0

            usd_assets = _f(row.get("usd_assets")) if is_hkd else _f(row.get("total_assets"))
            usd_cash = _f(row.get("us_cash")) if is_hkd else _f(row.get("cash"))
            usd_power = _f(row.get("usd_net_cash_power")) if is_hkd and _f(row.get("usd_net_cash_power")) > 0 else _f(row.get("power")) / fx

            return {
                "total_assets": round(usd_assets, 2),
                "available_cash": round(usd_cash, 2),
                "market_value": round(_f(row.get("market_val")) / fx, 2),
                "power": round(usd_power, 2),
                "currency": "USD",
            }
        except Exception as e:
            logger.error(f"查询账户概览失败: {e}")
            return {}

    # ── 持仓列表 (DB + Futu 实时价格) ──

    def get_positions(self) -> List[Dict[str, Any]]:
        try:
            conn = self._conn()
            # 活跃持仓: sync_status 为空或不在已关闭状态
            rows = conn.execute("""
                SELECT symbol, direction, size, avg_entry_price,
                       entry_time, strategy_id, current_sl, initial_tp
                FROM open_positions
                WHERE sync_status IS NULL
                   OR sync_status NOT IN (
                       'closed_by_daemon','closed_by_scheduler','closed_by_sync',
                       'closed_emergency','stale_not_in_broker'
                   )
                ORDER BY entry_time DESC
            """).fetchall()
            conn.close()

            if not rows:
                return []

            # 批量获取实时价格
            symbols = [r["symbol"] for r in rows]
            price_map = self._batch_quotes(symbols)

            positions = []
            for r in rows:
                sym = r["symbol"]
                size = float(r["size"] or 0)
                cost = float(r["avg_entry_price"] or 0)
                cur = price_map.get(sym, 0)
                is_long = r["direction"] == "LONG"

                # 计算市值和盈亏
                market_val = cur * size if cur > 0 else 0
                if cur > 0 and cost > 0:
                    unrealized = (cur - cost) * size if is_long else (cost - cur) * size
                    pnl_pct = ((cur - cost) / cost * 100) if is_long else ((cost - cur) / cost * 100)
                else:
                    unrealized = 0
                    pnl_pct = 0

                positions.append({
                    "symbol": sym,
                    "direction": r["direction"],
                    "quantity": size,
                    "avg_entry_price": cost,
                    "current_price": cur,
                    "market_value": round(market_val, 2),
                    "unrealized_pnl": round(unrealized, 2),
                    "unrealized_pnl_percent": round(pnl_pct, 2),
                    "entry_time": r["entry_time"] or "",
                    "strategy_id": r["strategy_id"] or "",
                    "current_sl": float(r["current_sl"] or 0),
                    "initial_tp": float(r["initial_tp"] or 0),
                })

            return positions
        except Exception as e:
            logger.error(f"查询持仓失败: {e}")
            return []

    # ── 交易记录 ──

    def get_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        try:
            conn = self._conn()
            rows = conn.execute("""
                SELECT symbol, direction, quantity, entry_price, exit_price,
                       realized_pnl, pnl_pct, trade_date, entry_time, exit_time,
                       exit_reason, holding_hours, signal_strength
                FROM trade_records
                ORDER BY trade_date DESC, entry_time DESC
                LIMIT ?
            """, (limit,)).fetchall()
            conn.close()

            return [{
                "symbol": r["symbol"],
                "direction": r["direction"],
                "quantity": r["quantity"] or 0,
                "entry_price": float(r["entry_price"] or 0),
                "exit_price": float(r["exit_price"] or 0),
                "pnl": float(r["realized_pnl"] or 0),
                "pnl_percent": float(r["pnl_pct"] or 0),
                "trade_date": r["trade_date"] or "",
                "entry_time": r["entry_time"] or "",
                "exit_time": r["exit_time"] or "",
                "exit_reason": r["exit_reason"] or "",
                "holding_hours": float(r["holding_hours"] or 0),
            } for r in rows]
        except Exception as e:
            logger.error(f"查询交易记录失败: {e}")
            return []

    # ── 统计信息 ──

    def get_stats(self) -> Dict[str, Any]:
        trades = self.get_trades(limit=500)
        positions = self.get_positions()
        account = self.get_account_summary()

        winning = [t for t in trades if t.get("pnl", 0) > 0]
        losing = [t for t in trades if t.get("pnl", 0) < 0]
        total_pnl = sum(t.get("pnl", 0) for t in trades)

        return {
            "total_trades": len(trades),
            "winning_trades": len(winning),
            "losing_trades": len(losing),
            "win_rate": round(len(winning) / len(trades) * 100, 1) if trades else 0,
            "total_pnl": round(total_pnl, 2),
            "current_positions": len(positions),
            "total_assets": account.get("total_assets", 0),
            "available_cash": account.get("available_cash", 0),
            "usd_assets": account.get("usd_assets", 0),
        }

    # ── 批量报价 ──

    def _batch_quotes(self, symbols: List[str]) -> Dict[str, float]:
        """批量获取报价，返回 {symbol: price}"""
        if not symbols:
            return {}
        try:
            from app.web_dashboard.manual_order_service import get_manual_order_service
            svc = get_manual_order_service()
            if not svc.connect():
                return {}
            from futu import RET_OK
            ret, snap = svc._quote_ctx.get_market_snapshot(symbols)
            if ret != RET_OK or snap.empty:
                return {}

            def _f(v):
                try:
                    return float(v) if v is not None and v != "N/A" else 0.0
                except (ValueError, TypeError):
                    return 0.0

            prices = {}
            for _, row in snap.iterrows():
                code = row.get("code", "")
                cur = _f(row.get("cur_price"))
                if cur <= 0:
                    cur = _f(row.get("last_price"))
                if cur <= 0:
                    cur = _f(row.get("prev_close_price"))
                if cur > 0:
                    prices[code] = cur
            return prices
        except Exception as e:
            logger.error(f"批量报价失败: {e}")
            return {}
