"""
分析路由 — 6 模块实时交易情报

资金曲线 + 持仓归因 + 资金流向 + 执行质量 + 信号质量 + 板块热力
"""
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from fastapi import APIRouter

from app.web_dashboard.data_service import get_data_service, safe_float, et_today, et_days_ago

logger = logging.getLogger("ftseek_us.routes.analytics")
router = APIRouter()


def register(app, templates):
    @router.get("/api/metrics/trading")
    async def get_trading_metrics():
        try:
            from app.storage.metrics_tables import MetricsTables
            from app.observability.metrics_api import MetricsAPI
            from app.core.paths import get_trade_records_db
            mt = MetricsTables(str(get_trade_records_db()))
            api = MetricsAPI(mt)
            status = api.get_latest_trading_status()
            return {
                "signals_count": int(status.get("signals_count", {}).get("value", 0)),
                "orders_count": int(status.get("orders_count", {}).get("value", 0)),
                "fills_count": int(status.get("fills_count", {}).get("value", 0)),
                "pending_orders": int(status.get("pending_orders", {}).get("value", 0)),
            }
        except Exception:
            return {"signals_count": 0, "orders_count": 0, "fills_count": 0, "pending_orders": 0}

    @router.get("/api/analytics/equity")
    async def analytics_equity():
        ds = get_data_service()
        cached = ds.cache.get("analytics_equity")
        if cached is not None:
            return cached
        try:
            rows = await ds.db_query(
                "SELECT id, current_equity, peak_equity, drawdown_pct, max_drawdown_pct, "
                "water_level, buying_power, last_update "
                "FROM capital_state ORDER BY id ASC LIMIT 500"
            )
            history = [{
                "ts": r.get("last_update", ""),
                "equity": round(float(r["current_equity"]), 2),
                "peak": round(float(r["peak_equity"]), 2),
                "drawdown_pct": round(float(r["drawdown_pct"]) * 100, 2),
                "water_level": r.get("water_level", "unknown"),
                "buying_power": round(float(r["buying_power"]), 2),
            } for r in rows]
            current = history[-1] if history else {}
            max_dd = min(h["drawdown_pct"] for h in history) if history else 0
            result = {
                "history": history,
                "current_equity": current.get("equity", 0),
                "peak_equity": current.get("peak", 0),
                "current_drawdown_pct": current.get("drawdown_pct", 0),
                "max_drawdown_pct": round(max_dd, 2),
                "water_level": current.get("water_level", "unknown"),
                "buying_power": current.get("buying_power", 0),
            }
            ds.cache.set("analytics_equity", result, ttl=30)
            return result
        except Exception as e:
            return {"error": str(e), "history": []}

    @router.get("/api/analytics/positions-attribution")
    async def analytics_positions():
        ds = get_data_service()
        cached = ds.cache.get("analytics_positions")
        if cached is not None:
            return cached
        try:
            svc = ds.svc()
            if not svc.connect():
                return {"error": "broker_not_connected", "positions": []}

            acc_raw, pos_raw = await asyncio.gather(
                ds.get_account_raw(),
                ds.get_positions_raw(),
            )

            total_equity = safe_float(acc_raw.get("total_assets", 0)) if acc_raw else 0

            # Build position list with prices
            codes = [str(p.get("code", "")) for p in pos_raw
                     if p.get("code") and str(p.get("code", "")).startswith("US.") and float(p.get("qty", 0)) != 0]
            price_map = await ds.get_batch_quotes(codes) if codes else {}

            positions = []
            long_mv = short_mv = total_unreal = 0

            for p in pos_raw:
                code = str(p.get("code", ""))
                qty = float(p.get("qty", 0))
                if qty == 0 or not code.startswith("US."):
                    continue
                cost = safe_float(p.get("cost_price", 0))
                cur = price_map.get(code, safe_float(p.get("cur_price", 0)))
                if cur <= 0:
                    cur = safe_float(p.get("val_price", 0)) / abs(qty) if qty != 0 else 0
                mv = abs(cur * qty)
                side = str(p.get("position_side", "")).upper()
                is_short = (qty < 0) or (side == "SHORT")
                is_long = not is_short
                unreal = (cur - cost) * qty if is_long else (cost - cur) * abs(qty)

                if is_long:
                    long_mv += mv
                else:
                    short_mv += mv
                total_unreal += unreal

                positions.append({
                    "symbol": code,
                    "direction": "LONG" if is_long else "SHORT",
                    "qty": abs(qty),
                    "cost": round(cost, 2),
                    "current": round(cur, 2),
                    "market_value": round(mv, 2),
                    "unrealized_pnl": round(unreal, 2),
                    "pnl_pct": round(unreal / (cost * abs(qty)) * 100, 2) if cost > 0 and qty != 0 else 0,
                    "weight_pct": round(mv / total_equity * 100, 2) if total_equity > 0 else 0,
                })

            positions.sort(key=lambda x: abs(x["unrealized_pnl"]), reverse=True)
            for p in positions:
                p["contribution_pct"] = round(p["unrealized_pnl"] / total_unreal * 100, 2) if total_unreal != 0 else 0

            result = {
                "positions": positions,
                "total_equity": round(total_equity, 2),
                "long_mv": round(long_mv, 2),
                "short_mv": round(short_mv, 2),
                "net_exposure": round(long_mv - short_mv, 2),
                "gross_exposure": round(long_mv + short_mv, 2),
                "total_unrealized": round(total_unreal, 2),
                "long_short_ratio": round(long_mv / short_mv, 2) if short_mv > 0 else None,
            }
            ds.cache.set("analytics_positions", result, ttl=10)
            return result
        except Exception as e:
            logger.error(f"[analytics/positions] {e}")
            return {"error": str(e), "positions": []}

    @router.get("/api/analytics/capital-flow")
    async def analytics_capital_flow():
        ds = get_data_service()
        cached = ds.cache.get("analytics_capital_flow")
        if cached is not None:
            return cached
        try:
            svc = ds.svc()
            if not svc.connect():
                return {"error": "broker_not_connected", "flows": []}

            from futu import TrdEnv, RET_OK
            is_live = svc._is_live()
            trd_env = TrdEnv.REAL if is_live else TrdEnv.SIMULATE
            acc_id = int(svc._read_env(
                "REAL_ACCOUNT_ID" if is_live else "SIMULATE_ACCOUNT_ID", "0"
            ))

            ret, pos_df = svc._broker_ctx.position_list_query(trd_env=trd_env, acc_id=acc_id)
            codes = []
            if ret == RET_OK and not pos_df.empty:
                codes = [str(r.get("code", "")) for _, r in pos_df.iterrows()
                         if r.get("code") and abs(float(r.get("qty", 0))) > 0][:8]

            flows = []
            if codes:
                # H3: 复用 DataService 线程池，避免每次请求创建新 ThreadPool
                import asyncio as _aio
                _loop = _aio.get_running_loop()
                _tasks = [_loop.run_in_executor(ds._pool, svc._quote_ctx.get_capital_distribution, code) for code in codes]
                _results = await _aio.gather(*_tasks, return_exceptions=True)
                for code, res in zip(codes, _results):
                    try:
                        if isinstance(res, Exception):
                            continue
                        ret_cf, cf_data = res
                        if ret_cf == RET_OK and not cf_data.empty:
                            row = cf_data.iloc[0]
                            flows.append({
                                "symbol": code,
                                "super_large_in": safe_float(row.get("capital_in_super", 0)),
                                "super_large_out": safe_float(row.get("capital_out_super", 0)),
                                "large_in": safe_float(row.get("capital_in_big", 0)),
                                "large_out": safe_float(row.get("capital_out_big", 0)),
                                "medium_in": safe_float(row.get("capital_in_mid", 0)),
                                "medium_out": safe_float(row.get("capital_out_mid", 0)),
                                "small_in": safe_float(row.get("capital_in_small", 0)),
                                "small_out": safe_float(row.get("capital_out_small", 0)),
                            })
                    except Exception:
                        pass

            for f in flows:
                f["net_main"] = round((f["super_large_in"] - f["super_large_out"] + f["large_in"] - f["large_out"]) / 10000, 2)
                f["net_retail"] = round((f["medium_in"] - f["medium_out"] + f["small_in"] - f["small_out"]) / 10000, 2)
                f["net_total"] = round(f["net_main"] + f["net_retail"], 2)

            flows.sort(key=lambda x: x["net_main"], reverse=True)
            result = {"flows": flows, "count": len(flows)}
            ds.cache.set("analytics_capital_flow", result, ttl=60)
            return result
        except Exception as e:
            logger.error(f"[analytics/capital-flow] {e}")
            return {"error": str(e), "flows": []}

    @router.get("/api/analytics/execution-quality")
    async def analytics_execution():
        ds = get_data_service()
        cached = ds.cache.get("analytics_execution")
        if cached is not None:
            return cached
        try:
            svc = ds.svc()
            if not svc.connect():
                return {"error": "broker_not_connected", "deals": []}
            from futu import TrdEnv, RET_OK
            is_live = svc._is_live()
            trd_env = TrdEnv.REAL if is_live else TrdEnv.SIMULATE
            acc_id = int(svc._read_env(
                "REAL_ACCOUNT_ID" if is_live else "SIMULATE_ACCOUNT_ID", "0"
            ))

            ret, deals_df = svc._broker_ctx.deal_list_query(trd_env=trd_env, acc_id=acc_id)
            deals = []
            if ret == RET_OK and not deals_df.empty:
                for _, d in deals_df.iterrows():
                    code = str(d.get("code", ""))
                    if not code.startswith("US."):
                        continue
                    deals.append({
                        "time": str(d.get("create_time", "")),
                        "symbol": code,
                        "side": str(d.get("trd_side", "")),
                        "qty": int(safe_float(d.get("qty", 0))),
                        "price": round(safe_float(d.get("price", 0)), 4),
                        "amount": round(safe_float(d.get("price", 0)) * int(safe_float(d.get("qty", 0))), 2),
                        "order_id": str(d.get("order_id", "")),
                    })

            # Slippage from DB
            slippage_data = []
            try:
                rows = await ds.db_query(
                    "SELECT symbol, direction, metadata_json FROM trade_records "
                    "WHERE metadata_json IS NOT NULL ORDER BY trade_date DESC LIMIT 100"
                )
                import json as _json
                for r in rows:
                    try:
                        meta = _json.loads(r.get("metadata_json") or "{}")
                        slip = meta.get("slippage_pct", 0)
                        if slip:
                            slippage_data.append({
                                "slippage_pct": slip,
                                "signal_price": meta.get("signal_price", 0),
                                "fill_price": meta.get("fill_price", 0),
                            })
                    except Exception:
                        pass
            except Exception:
                pass

            avg_slippage = round(sum(s["slippage_pct"] for s in slippage_data) / len(slippage_data), 4) if slippage_data else 0
            alert_count = sum(1 for s in slippage_data if abs(s["slippage_pct"]) > 0.5)
            total_amount = sum(d["amount"] for d in deals)

            result = {
                "deals": deals[-50:],
                "deal_count": len(deals),
                "total_amount": round(total_amount, 2),
                "avg_amount_per_deal": round(total_amount / len(deals), 2) if deals else 0,
                "slippage_records": len(slippage_data),
                "avg_slippage_pct": avg_slippage,
                "slippage_alerts": alert_count,
            }
            ds.cache.set("analytics_execution", result, ttl=60)
            return result
        except Exception as e:
            logger.error(f"[analytics/execution] {e}")
            return {"error": str(e), "deals": []}

    @router.get("/api/analytics/signal-quality")
    async def analytics_signals():
        ds = get_data_service()
        cached = ds.cache.get("analytics_signals")
        if cached is not None:
            return cached
        try:
            from app.analysis.signal_ic_tracker import get_ic_summary, get_stats, _get_db_path
            ic_summary = get_ic_summary(min_signals=10, days_back=30)
            stats = get_stats()

            snap_db = str(_get_db_path())
            rows = await ds.db_query(
                "SELECT symbol, direction, strength, confidence, fwd_ret_t1 "
                "FROM signal_snapshots WHERE fwd_ret_t1 IS NOT NULL "
                "AND COALESCE(trade_mode, 'simulate') = 'live' "
                "ORDER BY created_at DESC LIMIT 100",
                db_path=snap_db,
            )
            recent = [{
                "symbol": r["symbol"], "direction": r["direction"],
                "strength": r["strength"], "confidence": r["confidence"],
                "fwd_return": round(float(r["fwd_ret_t1"]) * 100, 2) if r.get("fwd_ret_t1") else 0,
            } for r in rows]

            result = {
                "ic_summary": ic_summary,
                "stats": stats,
                "recent_signals": recent,
                "signal_count": len(recent),
            }
            ds.cache.set("analytics_signals", result, ttl=120)
            return result
        except Exception as e:
            logger.error(f"[analytics/signals] {e}")
            return {"error": str(e), "ic_summary": {}, "recent_signals": []}

    @router.get("/api/analytics/sector-heatmap")
    async def analytics_sectors():
        ds = get_data_service()
        cached = ds.cache.get("analytics_sectors")
        if cached is not None:
            return cached
        try:
            svc = ds.svc()
            if not svc.connect():
                return {"error": "broker_not_connected", "sectors": []}
            from futu import RET_OK

            _SECTOR_ETFS = {
                "US.XLK": "科技", "US.XLF": "金融", "US.XLE": "能源",
                "US.XLV": "医疗", "US.XLI": "工业", "US.XLP": "必需消费",
                "US.XLU": "公用事业", "US.XLB": "材料", "US.XLY": "可选消费",
                "US.XLRE": "房地产", "US.XLC": "通信", "US.IWM": "小盘股",
                "US.SPY": "标普500", "US.QQQ": "纳斯达克100", "US.DIA": "道琼斯",
                "US.ARKW": "下一代互联网", "US.ARKK": "颠覆创新", "US.ARKG": "基因组革命",
                "US.HACK": "网络安全", "US.TAN": "太阳能", "US.LIT": "锂电池",
                "US.KWEB": "中概互联", "US.SMHH": "半导体",
            }

            # 合并为一次 snapshot 调用 (原来分 2 次)
            all_codes = list(_SECTOR_ETFS.keys())
            snap = await ds.get_market_snapshot(all_codes, ttl=120)

            _CONCEPT_CODES = {"US.ARKW", "US.ARKK", "US.ARKG", "US.HACK", "US.TAN", "US.LIT", "US.KWEB", "US.SMHH"}

            sectors = []
            hot_concepts = []

            for code, row in snap.items():
                name = _SECTOR_ETFS.get(code, code)
                cur = safe_float(row.get("cur_price"))
                if cur <= 0:
                    cur = safe_float(row.get("last_price"))
                if cur <= 0:
                    cur = safe_float(row.get("update_price"))
                prev = safe_float(row.get("prev_close_price"))
                change_pct = ((cur - prev) / prev * 100) if prev > 0 else 0
                vol = int(safe_float(row.get("volume")))

                entry = {"name": name, "code": code, "change_pct": round(change_pct, 2), "price": round(cur, 2), "volume": vol}

                if code in _CONCEPT_CODES:
                    hot_concepts.append({"name": name, "change_pct": round(change_pct, 2)})
                if code not in _CONCEPT_CODES:
                    sectors.append(entry)

            sectors.sort(key=lambda x: x["change_pct"], reverse=True)
            hot_concepts.sort(key=lambda x: x["change_pct"], reverse=True)

            result = {"sectors": sectors, "hot_concepts": hot_concepts, "sector_count": len(sectors)}
            ds.cache.set("analytics_sectors", result, ttl=120)
            return result
        except Exception as e:
            logger.error(f"[analytics/sectors] {e}")
            return {"error": str(e), "sectors": []}

    @router.get("/api/analytics/signal-dashboard")
    async def signal_dashboard():
        ds = get_data_service()
        cached = ds.cache.get("signal_dashboard")
        if cached is not None:
            return cached
        try:
            today = et_today()

            # 信号数
            signal_count = passed_count = 0
            try:
                from app.analysis.signal_ic_tracker import _get_db_path
                snap_db = str(_get_db_path())
                row = await ds.db_query_one(
                    "SELECT COUNT(*) as cnt FROM signal_snapshots WHERE trade_date = ? AND COALESCE(trade_mode, 'simulate') = 'live'",
                    (today,), db_path=snap_db,
                )
                signal_count = row["cnt"] if row else 0
                row2 = await ds.db_query_one(
                    "SELECT COUNT(*) as cnt FROM signal_snapshots WHERE trade_date = ? AND strength >= 30 AND confidence > 0 AND COALESCE(trade_mode, 'simulate') = 'live'",
                    (today,), db_path=snap_db,
                )
                passed_count = row2["cnt"] if row2 else 0
            except Exception:
                pass

            # 执行数
            executed_count = 0
            today_trades = []
            try:
                row3 = await ds.db_query_one(
                    "SELECT COUNT(*) as cnt FROM trade_records WHERE trade_date = ?", (today,),
                )
                executed_count = row3["cnt"] if row3 else 0
                recent_rows = await ds.db_query(
                    "SELECT symbol, direction, realized_pnl, exit_reason, trade_date "
                    "FROM trade_records ORDER BY trade_date DESC, id DESC LIMIT 5"
                )
                today_trades = [{
                    "symbol": r["symbol"], "direction": r["direction"],
                    "pnl": round(float(r.get("realized_pnl") or 0), 2),
                    "exit_reason": r.get("exit_reason", ""),
                    "date": r["trade_date"],
                } for r in recent_rows]
            except Exception:
                pass

            # Watchlist
            watchlist = []
            pos_raw = await ds.get_positions_raw()
            for p in pos_raw:
                code = str(p.get("code", ""))
                qty = float(p.get("qty", 0))
                if qty == 0 or not code.startswith("US."):
                    continue
                cost = safe_float(p.get("cost_price", 0))
                cur = safe_float(p.get("cur_price", 0))
                side = str(p.get("position_side", "")).upper()
                is_short = (qty < 0) or (side == "SHORT")
                if cost > 0 and cur > 0:
                    pnl_pct = ((cur - cost) / cost * 100) if not is_short else ((cost - cur) / cost * 100)
                else:
                    pnl_pct = 0
                light = "green" if pnl_pct > 5 else "yellow" if pnl_pct > -3 else "red"
                watchlist.append({
                    "symbol": code,
                    "direction": "SHORT" if is_short else "LONG",
                    "qty": abs(qty),
                    "pnl_pct": round(pnl_pct, 2),
                    "light": light,
                })
            watchlist.sort(key=lambda x: x["pnl_pct"])

            result = {
                "today": today,
                "signal_count": signal_count,
                "passed_count": passed_count,
                "executed_count": executed_count,
                "pass_rate": round(passed_count / signal_count * 100, 1) if signal_count > 0 else 0,
                "recent_trades": today_trades,
                "watchlist": watchlist,
            }
            ds.cache.set("signal_dashboard", result, ttl=15)
            return result
        except Exception as e:
            logger.error(f"[signal-dashboard] {e}")
            return {"error": str(e)}

    @router.get("/api/analytics/manual-performance")
    async def manual_performance():
        ds = get_data_service()
        cached = ds.cache.get("manual_perf")
        if cached is not None:
            return cached
        try:
            manual = await ds.db_query_one("""
                SELECT COUNT(*) as cnt,
                       SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                       COALESCE(SUM(realized_pnl), 0) as total_pnl,
                       COALESCE(AVG(CASE WHEN realized_pnl > 0 THEN realized_pnl END), 0) as avg_win,
                       COALESCE(AVG(CASE WHEN realized_pnl < 0 THEN realized_pnl END), 0) as avg_loss
                FROM trade_records
                WHERE (strategy_id = 'manual' OR source_type = 'MANUAL')
                  AND trade_mode != 'simulate'
            """)

            auto = await ds.db_query_one("""
                SELECT COUNT(*) as cnt,
                       SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                       COALESCE(SUM(realized_pnl), 0) as total_pnl,
                       COALESCE(AVG(CASE WHEN realized_pnl > 0 THEN realized_pnl END), 0) as avg_win,
                       COALESCE(AVG(CASE WHEN realized_pnl < 0 THEN realized_pnl END), 0) as avg_loss
                FROM trade_records
                WHERE (strategy_id != 'manual' OR strategy_id IS NULL)
                  AND source_type != 'MANUAL'
                  AND source_type NOT IN ('manual_sync', 'reconciler')
                  AND trade_mode != 'simulate'
            """)

            recent = await ds.db_query("""
                SELECT symbol, direction, realized_pnl, exit_reason, trade_date,
                       entry_price, exit_price, quantity
                FROM trade_records
                WHERE (strategy_id = 'manual' OR source_type = 'MANUAL')
                  AND trade_mode != 'simulate'
                ORDER BY trade_date DESC, id DESC LIMIT 20
            """)

            def _calc(row):
                if not row:
                    return {"count": 0, "wins": 0, "losses": 0, "win_rate": 0, "total_pnl": 0, "avg_win": 0, "avg_loss": 0, "profit_factor": 0, "avg_pnl": 0}
                cnt = row.get("cnt", 0) or 0
                wins = row.get("wins", 0) or 0
                total = row.get("total_pnl", 0) or 0
                avg_w = row.get("avg_win", 0) or 0
                avg_l = row.get("avg_loss", 0) or 0
                pf = abs(avg_w * wins / (avg_l * (cnt - wins))) if avg_l != 0 and (cnt - wins) > 0 else 0
                return {
                    "count": cnt, "wins": wins, "losses": cnt - wins,
                    "win_rate": round(wins / cnt * 100, 1) if cnt > 0 else 0,
                    "total_pnl": round(total, 2),
                    "avg_win": round(avg_w, 2), "avg_loss": round(avg_l, 2),
                    "profit_factor": round(pf, 2),
                    "avg_pnl": round(total / cnt, 2) if cnt > 0 else 0,
                }

            result = {
                "manual": _calc(manual),
                "auto": _calc(auto),
                "recent_trades": [{
                    "symbol": r["symbol"], "direction": r["direction"],
                    "pnl": round(float(r.get("realized_pnl") or 0), 2),
                    "exit_reason": r.get("exit_reason", ""),
                    "date": r["trade_date"],
                    "entry_price": round(float(r.get("entry_price") or 0), 2),
                    "exit_price": round(float(r.get("exit_price") or 0), 2),
                    "quantity": r.get("quantity", 0),
                } for r in recent],
            }
            ds.cache.set("manual_perf", result, ttl=60)
            return result
        except Exception as e:
            logger.error(f"[manual-performance] {e}")
            return {"error": str(e), "manual": {}, "auto": {}, "recent_trades": []}

    app.include_router(router)
