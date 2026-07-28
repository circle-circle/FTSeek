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
from app.web_dashboard.api_response import api_ok, api_error, err_broker_unavailable

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
            return api_ok(cached)
        try:
            # 1. Read historical data, filter out garbage records
            #    (early records may contain HK-account or sim-mode values >200K)
            rows = await ds.db_query(
                "SELECT id, current_equity, peak_equity, drawdown_pct, max_drawdown_pct, "
                "water_level, buying_power, last_update "
                "FROM capital_state WHERE current_equity > 0 AND current_equity < 200000 "
                "ORDER BY id ASC LIMIT 1000"
            )
            history = [{
                "ts": r.get("last_update", ""),
                "date": (r.get("last_update", "") or "")[:10],
                "equity": round(float(r["current_equity"]), 2),
                "peak": round(float(r["peak_equity"]), 2),
                "drawdown_pct": round(float(r["drawdown_pct"]) * 100, 2),
                "water_level": r.get("water_level", "unknown"),
                "buying_power": round(float(r["buying_power"]), 2),
            } for r in rows]

            # 2. Deduplicate by date — keep last record per day for cleaner chart
            seen_dates = {}
            for h in history:
                seen_dates[h["date"]] = h
            daily_history = sorted(seen_dates.values(), key=lambda x: x["ts"])

            # 3. Get real-time current equity from Futu broker
            try:
                svc = ds.svc()
                if svc.connect():
                    acc_raw = await ds.get_account_raw()
                    if acc_raw is not None and not (hasattr(acc_raw, 'empty') and acc_raw.empty):
                        from app.web_dashboard.routes.dashboard import _parse_account
                        live_acc = _parse_account(acc_raw)
                        live_equity = live_acc.get("total_assets", 0)
                        if live_equity > 0:
                            now_ts = __import__('datetime').datetime.now().isoformat()
                            today_str = now_ts[:10]
                            live_point = {
                                "ts": now_ts,
                                "date": today_str,
                                "equity": round(live_equity, 2),
                                "peak": max((h["peak"] for h in daily_history), default=live_equity),
                                "drawdown_pct": 0,
                                "water_level": "green",
                                "buying_power": round(live_acc.get("buying_power", 0), 2),
                            }
                            # Update or append today's live point
                            if daily_history and daily_history[-1]["date"] == today_str:
                                daily_history[-1] = live_point
                            else:
                                daily_history.append(live_point)
            except Exception:
                pass  # Broker unavailable — use DB data only

            # 4. Recalculate peak + drawdown from clean history
            peak = 0
            for h in daily_history:
                if h["equity"] > peak:
                    peak = h["equity"]
                h["peak"] = round(peak, 2)
                if peak > 0:
                    h["drawdown_pct"] = round((h["equity"] - peak) / peak * 100, 2)
                else:
                    h["drawdown_pct"] = 0

            current = daily_history[-1] if daily_history else {}
            max_dd = min(h["drawdown_pct"] for h in daily_history) if daily_history else 0
            result = {
                "history": daily_history,
                "current_equity": current.get("equity", 0),
                "peak_equity": current.get("peak", 0),
                "current_drawdown_pct": current.get("drawdown_pct", 0),
                "max_drawdown_pct": round(max_dd, 2),
                "water_level": current.get("water_level", "unknown"),
                "buying_power": current.get("buying_power", 0),
                "currency": "USD",
            }
            ds.cache.set("analytics_equity", result, ttl=30)
            return api_ok(result)
        except Exception as e:
            logger.error(f"[analytics/equity] {e}")
            return api_error("ANALYTICS_ERROR", "数据查询异常", retryable=True,
                             data={"history": []})

    @router.get("/api/analytics/positions-attribution")
    async def analytics_positions():
        ds = get_data_service()
        cached = ds.cache.get("analytics_positions")
        if cached is not None:
            return api_ok(cached)
        try:
            svc = ds.svc()
            if not svc.connect():
                return err_broker_unavailable()

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
            return api_ok(result)
        except Exception as e:
            logger.error(f"[analytics/positions] {e}")
            return api_error("ANALYTICS_ERROR", "数据查询异常", retryable=True,
                             data={"positions": []})

    @router.get("/api/analytics/capital-flow")
    async def analytics_capital_flow():
        ds = get_data_service()
        cached = ds.cache.get("analytics_capital_flow")
        if cached is not None:
            return api_ok(cached)
        try:
            svc = ds.svc()
            if not svc.connect():
                return err_broker_unavailable()

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
            return api_ok(result)
        except Exception as e:
            logger.error(f"[analytics/capital-flow] {e}")
            return api_error("ANALYTICS_ERROR", "数据查询异常", retryable=True,
                             data={"flows": []})

    @router.get("/api/analytics/execution-quality")
    async def analytics_execution():
        ds = get_data_service()
        cached = ds.cache.get("analytics_execution")
        if cached is not None:
            return api_ok(cached)
        try:
            svc = ds.svc()
            if not svc.connect():
                return err_broker_unavailable()
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
            # 2026-07-28 修复 (看板审计 P0): 原读 meta["slippage_pct"] — 该键从未存在
            #   (落库真键=metadata.execution.slippage_bps, bps 非 pct) → 面板恒 0 假完美。
            #   常量退化家族看板侧首例。阈值同步 bps 口径 (50bps 超标, 与落库告警一致)。
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
                        _exec = meta.get("execution") or {}
                        slip_bps = _exec.get("slippage_bps")
                        if slip_bps is not None:
                            slippage_data.append({
                                "slippage_bps": safe_float(slip_bps),
                                "signal_price": _exec.get("arrival_price", 0),
                                "fill_price": _exec.get("fill_price", 0),
                            })
                    except Exception:
                        pass
            except Exception:
                pass

            avg_slippage_bps = round(sum(s["slippage_bps"] for s in slippage_data) / len(slippage_data), 2) if slippage_data else 0
            alert_count = sum(1 for s in slippage_data if abs(s["slippage_bps"]) > 50.0)
            total_amount = sum(d["amount"] for d in deals)

            result = {
                "deals": deals[-50:],
                "deal_count": len(deals),
                "total_amount": round(total_amount, 2),
                "avg_amount_per_deal": round(total_amount / len(deals), 2) if deals else 0,
                "slippage_records": len(slippage_data),
                "avg_slippage_bps": avg_slippage_bps,
                # 兼容旧前端键名 (pct 值≈bps/100, 前端迁移后可删)
                "avg_slippage_pct": round(avg_slippage_bps / 100.0, 4),
                "slippage_alerts": alert_count,
            }
            ds.cache.set("analytics_execution", result, ttl=60)
            return api_ok(result)
        except Exception as e:
            logger.error(f"[analytics/execution] {e}")
            return api_error("ANALYTICS_ERROR", "数据查询异常", retryable=True,
                             data={"deals": []})

    @router.get("/api/analytics/signal-quality")
    async def analytics_signals():
        ds = get_data_service()
        cached = ds.cache.get("analytics_signals")
        if cached is not None:
            return api_ok(cached)
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
            return api_ok(result)
        except Exception as e:
            logger.error(f"[analytics/signals] {e}")
            return api_error("ANALYTICS_ERROR", "数据查询异常", retryable=True,
                             data={"ic_summary": {}, "recent_signals": []})

    @router.get("/api/analytics/sector-heatmap")
    async def analytics_sectors():
        ds = get_data_service()
        cached = ds.cache.get("analytics_sectors")
        if cached is not None:
            return api_ok(cached)
        try:
            svc = ds.svc()
            if not svc.connect():
                return err_broker_unavailable()
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
            return api_ok(result)
        except Exception as e:
            logger.error(f"[analytics/sectors] {e}")
            return api_error("ANALYTICS_ERROR", "数据查询异常", retryable=True,
                             data={"sectors": []})

    @router.get("/api/analytics/signal-dashboard")
    async def signal_dashboard():
        ds = get_data_service()
        cached = ds.cache.get("signal_dashboard")
        if cached is not None:
            return api_ok(cached)
        try:
            today = et_today()

            # 信号数
            signal_count = passed_count = 0
            try:
                counts = await ds.get_signal_counts(today)
                signal_count = counts["total"]
                passed_count = counts["qualified"]
            except Exception:
                pass

            # 执行数
            executed_count = 0
            today_trades = []
            try:
                executed_count = await ds.get_executed_count(today)
                recent_rows = await ds.db_query(
                    "SELECT symbol, direction, realized_pnl, exit_reason, trade_date "
                    "FROM trade_records "
                    "WHERE source_type NOT IN ('PLAYBOOK_SHADOW','SCALPING') AND COALESCE(exit_reason,'') NOT IN ('PHANTOM_RECONCILE_VOID','ORPHAN_CLEANUP') "
                    "ORDER BY trade_date DESC, id DESC LIMIT 5"
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

            # RFC-060 决策辅助: 按策略×方向的历史胜率给信号打信任度 (人工执行参考)
            # 诊断依据: TECHNICAL_MULTI_FACTOR SHORT 58.7%WR (真edge); voting_llm 40.8%WR (出血源)
            strategy_quality = {}
            try:
                _snap_db = ds._snap_db()
                q_rows = await ds.db_query(
                    "SELECT COALESCE(strategy_id,'(无标签)') sid, direction, "
                    "COUNT(*) n, ROUND(AVG(fwd_ret_t1)*100,2) fwd_pct, "
                    "ROUND(100.0*SUM(CASE WHEN fwd_ret_t1>0 THEN 1 ELSE 0 END)/COUNT(*),1) winrate "
                    "FROM signal_snapshots "
                    "WHERE fwd_ret_t1 IS NOT NULL AND ABS(fwd_ret_t1)<0.5 "
                    "GROUP BY strategy_id, direction",
                    db_path=_snap_db,
                )
                for r in q_rows:
                    key = f"{r['sid']}|{r['direction']}"
                    wr = float(r.get("winrate") or 0)  # 百分制 (0-100)
                    # 信任度阈值用百分制: ≥55% trust / 45-55% neutral / <45% caution
                    badge = "trust" if wr >= 55 else ("neutral" if wr >= 45 else "caution")
                    strategy_quality[key] = {
                        "n": r["n"], "winrate": wr, "fwd_pct": r.get("fwd_pct"),
                        "badge": badge,
                    }
            except Exception:
                pass

            # 近期候选信号 (带策略信任度, 排序: trust>neutral>caution)
            decision_candidates = []
            try:
                _snap_db2 = ds._snap_db()
                cand_rows = await ds.db_query(
                    "SELECT decision_id, symbol, direction, strength, "
                    "COALESCE(strategy_id,'(无标签)') sid, created_at "
                    "FROM signal_snapshots "
                    f"WHERE trade_date >= '{et_days_ago(3)}' "  # 2026-07-28: UTC→ET (时区家族)
                    "ORDER BY created_at DESC LIMIT 25",
                    db_path=_snap_db2,
                )
                _rank = {"trust": 0, "neutral": 1, "caution": 2}
                for c in cand_rows:
                    q = strategy_quality.get(f"{c['sid']}|{c['direction']}", {})
                    decision_candidates.append({
                        "symbol": c["symbol"], "direction": c["direction"],
                        "strength": c["strength"], "strategy": c["sid"],
                        "created_at": c["created_at"],
                        "winrate": q.get("winrate"), "n": q.get("n"),
                        "badge": q.get("badge", "unknown"),
                        "note": ("已验证edge" if q.get("badge") == "trust"
                                 else "历史低胜率,慎入" if q.get("badge") == "caution"
                                 else "样本不足/中性"),
                    })
                decision_candidates.sort(key=lambda x: (_rank.get(x["badge"], 3), -(x.get("strength") or 0)))
            except Exception:
                pass

            result = {
                "today": today,
                "signal_count": signal_count,
                "passed_count": passed_count,
                "executed_count": executed_count,
                "pass_rate": round(passed_count / signal_count * 100, 1) if signal_count > 0 else 0,
                "recent_trades": today_trades,
                "watchlist": watchlist,
                # RFC-060 决策辅助: 让人工一眼分清信任信号 vs 出血源
                "strategy_quality": strategy_quality,
                "decision_candidates": decision_candidates,
            }
            ds.cache.set("signal_dashboard", result, ttl=15)
            return api_ok(result)
        except Exception as e:
            logger.error(f"[signal-dashboard] {e}")
            return api_error("ANALYTICS_ERROR", "数据查询异常", retryable=True)

    @router.get("/api/analytics/manual-performance")
    async def manual_performance():
        ds = get_data_service()
        cached = ds.cache.get("manual_perf")
        if cached is not None:
            return api_ok(cached)
        try:
            manual = await ds.db_query_one("""
                SELECT COUNT(*) as cnt,
                       SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                       COALESCE(SUM(realized_pnl), 0) as total_pnl,
                       COALESCE(AVG(CASE WHEN realized_pnl > 0 THEN realized_pnl END), 0) as avg_win,
                       COALESCE(AVG(CASE WHEN realized_pnl < 0 THEN realized_pnl END), 0) as avg_loss
                FROM trade_records
                WHERE (strategy_id = 'manual' OR source_type = 'MANUAL')
                  AND source_type NOT IN ('PLAYBOOK_SHADOW','SCALPING') AND COALESCE(exit_reason,'') NOT IN ('PHANTOM_RECONCILE_VOID','ORPHAN_CLEANUP')
                  AND exit_price > 0
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
                  AND source_type NOT IN ('PLAYBOOK_SHADOW','SCALPING') AND COALESCE(exit_reason,'') NOT IN ('PHANTOM_RECONCILE_VOID','ORPHAN_CLEANUP')
                  AND exit_price > 0
            """)

            # 最近交易: 所有来源 (手动+自动+外部平仓), 按时间倒序
            recent = await ds.db_query("""
                SELECT symbol, direction, realized_pnl, exit_reason, trade_date,
                       entry_price, exit_price, quantity, source_type, strategy_id,
                       COALESCE(signal_id, '') as signal_id,
                       COALESCE(trace_id, '') as trace_id
                FROM trade_records
                WHERE source_type NOT IN ('PLAYBOOK_SHADOW','SCALPING') AND COALESCE(exit_reason,'') NOT IN ('PHANTOM_RECONCILE_VOID','ORPHAN_CLEANUP')
                  AND exit_price > 0
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
                    "source_type": r.get("source_type", ""),
                    "signal_id": r.get("signal_id", ""),
                } for r in recent],
            }
            ds.cache.set("manual_perf", result, ttl=15)
            return api_ok(result)
        except Exception as e:
            logger.error(f"[manual-performance] {e}")
            return api_error("ANALYTICS_ERROR", "数据查询异常", retryable=True,
                             data={"manual": {}, "auto": {}, "recent_trades": []})

    # ── Trade Analysis (多维交易分析) ──

    @router.get("/api/analytics/trade-analysis")
    async def trade_analysis(days: int = 30):
        """按 entry/exit context 多维度切分交易绩效"""
        ds = get_data_service()
        try:
            rows = await ds.db_query(
                "SELECT symbol, direction, entry_price, exit_price, quantity, "
                "realized_pnl, pnl_pct, entry_time, exit_time, trade_date, "
                "metadata_json FROM trade_records "
                "WHERE trade_date >= ? "  # 2026-07-28: UTC→ET, 参数改传 et_days_ago(days)
                "AND source_type NOT IN ('PLAYBOOK_SHADOW','SCALPING') AND COALESCE(exit_reason,'') NOT IN ('PHANTOM_RECONCILE_VOID','ORPHAN_CLEANUP') "
                "AND exit_time IS NOT NULL "
                "ORDER BY trade_date DESC",
                (et_days_ago(abs(int(days))),),
            )

            import json as _json

            trades = []
            for r in rows:
                meta = {}
                try:
                    meta = _json.loads(r.get("metadata_json") or "{}")
                except Exception:
                    pass

                entry_ctx = (meta.get("thesis") or {}).get("entry_context") or {}
                exit_ctx = meta.get("exit_context") or {}

                trades.append({
                    "symbol": r.get("symbol", ""),
                    "direction": r.get("direction", ""),
                    "pnl": round(float(r.get("realized_pnl") or 0), 2),
                    "pnl_pct": round(float(r.get("pnl_pct") or 0) * 100, 1),
                    "hold_hours": exit_ctx.get("hold_hours"),
                    "entry_session": entry_ctx.get("session_phase", "unknown"),
                    "exit_session": exit_ctx.get("session_phase", "unknown"),
                    "plan_match": entry_ctx.get("plan_match", False),
                    "vix_entry": entry_ctx.get("vix"),
                    "vix_exit": exit_ctx.get("vix"),
                    "trade_date": r.get("trade_date", ""),
                })

            def _stats(subset):
                if not subset:
                    return {"count": 0, "win_rate": 0, "avg_pnl": 0, "total_pnl": 0, "avg_hold_hours": 0}
                wins = sum(1 for t in subset if t["pnl"] > 0)
                cnt = len(subset)
                holds = [t["hold_hours"] for t in subset if t.get("hold_hours")]
                return {
                    "count": cnt,
                    "win_rate": round(wins / cnt * 100, 1) if cnt else 0,
                    "avg_pnl": round(sum(t["pnl"] for t in subset) / cnt, 2),
                    "total_pnl": round(sum(t["pnl"] for t in subset), 2),
                    "avg_hold_hours": round(sum(holds) / len(holds), 1) if holds else 0,
                }

            # 按维度切分
            by_session = {}
            for t in trades:
                s = t["entry_session"]
                by_session.setdefault(s, []).append(t)

            by_plan = {
                "plan": _stats([t for t in trades if t["plan_match"]]),
                "non_plan": _stats([t for t in trades if not t["plan_match"]]),
            }

            by_direction = {}
            for t in trades:
                d = t["direction"]
                by_direction.setdefault(d, []).append(t)

            by_vix = {"low": [], "normal": [], "high": [], "panic": []}
            for t in trades:
                v = t.get("vix_entry")
                if v is None:
                    continue
                if v < 15:
                    by_vix["low"].append(t)
                elif v < 25:
                    by_vix["normal"].append(t)
                elif v < 35:
                    by_vix["high"].append(t)
                else:
                    by_vix["panic"].append(t)

            result = {
                "days": days,
                "total_trades": len(trades),
                "overall": _stats(trades),
                "by_session": {k: _stats(v) for k, v in by_session.items()},
                "by_plan": by_plan,
                "by_direction": {k: _stats(v) for k, v in by_direction.items()},
                "by_vix": {k: _stats(v) for k, v in by_vix.items()},
                "recent": trades[:10],
            }
            return api_ok(result)
        except Exception as e:
            logger.error(f"[trade-analysis] {e}")
            return api_error("ANALYTICS_ERROR", "数据查询异常", retryable=True)

    # ── Trade Review (深度复盘) ──

    @router.get("/api/trade/review")
    async def trade_review(days: int = 0):
        """深度复盘：维度热力图 + 计划vs非计划 + 趋势 + 最弱维度"""
        import glob as _glob
        import json as _json
        import os as _os

        ds = get_data_service()
        try:
            # Build date filter
            if days > 0:
                date_filter = f"AND trade_date >= '{et_days_ago(int(days))}'"  # 2026-07-28: UTC→ET
            else:
                date_filter = ""

            rows = await ds.db_query(
                f"SELECT id, symbol, direction, realized_pnl, pnl_pct, trade_date, "
                f"entry_price, exit_price, quantity, exit_reason, metadata_json, source_type "
                f"FROM trade_records "
                f"WHERE source_type NOT IN ('PLAYBOOK_SHADOW','SCALPING') AND COALESCE(exit_reason,'') NOT IN ('PHANTOM_RECONCILE_VOID','ORPHAN_CLEANUP') AND exit_price > 0 {date_filter} "
                f"ORDER BY trade_date DESC, id DESC"
            )

            trades = []
            for r in rows:
                meta = {}
                try:
                    meta = _json.loads(r.get("metadata_json") or "{}")
                except Exception:
                    pass
                trades.append({
                    "id": r.get("id"),
                    "symbol": r.get("symbol", ""),
                    "direction": r.get("direction", ""),
                    "pnl": round(float(r.get("realized_pnl") or 0), 2),
                    "pnl_pct": round(float(r.get("pnl_pct") or 0), 4),
                    "trade_date": r.get("trade_date", ""),
                    "meta": meta,
                    "source_type": r.get("source_type", ""),
                })

            trade_count = len(trades)

            # ── Load plan effectiveness data ──
            plan_symbols = set()
            plan_files = sorted(_glob.glob("/opt/ftseek/FTSeek-US/data/plan_effectiveness/daily_*.json"))
            if plan_files:
                try:
                    with open(plan_files[-1]) as f:
                        pe = _json.load(f)
                    for t in pe.get("targets", []):
                        if t.get("traded"):
                            plan_symbols.add(t.get("symbol", ""))
                except Exception:
                    pass

            # ── Dimension stats from metadata_json attribution ──
            dimensions = ["direction", "entry", "exit", "duration", "efficiency"]
            dim_stats = {}
            for dim in dimensions:
                dim_stats[dim] = {"pass": 0, "warn": 0, "fail": 0, "accuracy": 0.0, "avg_score": 0.0}

            for t in trades:
                attr = t["meta"].get("attribution", {})
                dims = attr.get("dimensions", [])
                for d in dims:
                    name = d.get("name", d.get("dimension", "")).lower()
                    if name in dim_stats:
                        verdict = d.get("verdict", "")
                        score = float(d.get("score", 0) or 0)
                        if verdict == "✅":
                            dim_stats[name]["pass"] += 1
                        elif verdict == "⚠️":
                            dim_stats[name]["warn"] += 1
                        elif verdict == "❌":
                            dim_stats[name]["fail"] += 1
                        dim_stats[name]["avg_score"] += score

            # Calculate accuracy per dimension
            weakest_dim = "entry"
            weakest_acc = 100.0
            for dim in dimensions:
                ds_stats = dim_stats[dim]
                total = ds_stats["pass"] + ds_stats["warn"] + ds_stats["fail"]
                if total > 0:
                    ds_stats["accuracy"] = round(ds_stats["pass"] / total * 100, 1)
                    ds_stats["avg_score"] = round(ds_stats["avg_score"] / total, 2)
                if ds_stats["accuracy"] < weakest_acc:
                    weakest_acc = ds_stats["accuracy"]
                    weakest_dim = dim

            # ── Plan vs Unplanned ──
            planned_trades = []
            unplanned_trades = []
            for t in trades:
                sym = t.get("symbol", "")
                if sym in plan_symbols:
                    planned_trades.append(t)
                else:
                    unplanned_trades.append(t)

            def _calc_group(pts):
                if not pts:
                    return {"count": 0, "win_rate": 0.0, "avg_pnl": 0.0, "profit_factor": 0.0}
                cnt = len(pts)
                wins = sum(1 for t in pts if t["pnl"] > 0)
                total_pnl = sum(t["pnl"] for t in pts)
                win_pnl = sum(t["pnl"] for t in pts if t["pnl"] > 0)
                loss_pnl = abs(sum(t["pnl"] for t in pts if t["pnl"] < 0))
                pf = round(win_pnl / loss_pnl, 2) if loss_pnl > 0 else (99.99 if win_pnl > 0 else 0.0)
                return {
                    "count": cnt,
                    "win_rate": round(wins / cnt * 100, 1) if cnt > 0 else 0.0,
                    "avg_pnl": round(total_pnl / cnt, 2),
                    "profit_factor": pf,
                }

            plan_vs_unplanned = {
                "planned": _calc_group(planned_trades),
                "unplanned": _calc_group(unplanned_trades),
            }

            # ── Trend: Compare current period vs previous period ──
            # For trend, we need to calculate metrics for current and previous period
            trend = {
                "decision_quality": {"current": 0, "previous": 0, "delta": 0, "direction": "neutral"},
                "profit_factor": {"current": 0.0, "previous": 0.0, "delta": 0.0, "direction": "neutral"},
                "total_trades": {"current": trade_count, "previous": 0},
            }

            # Calculate profit factor for current period
            if trades:
                win_pnl = sum(t["pnl"] for t in trades if t["pnl"] > 0)
                loss_pnl = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
                pf = round(win_pnl / loss_pnl, 2) if loss_pnl > 0 else (99.99 if win_pnl > 0 else 0.0)
                trend["profit_factor"]["current"] = pf

            # Get decision quality from trade_attribution
            prev_days = 60
            try:
                if days > 0:
                    prev_days = days * 2

                # Current period
                attr_rows = await ds.db_query(
                    f"SELECT overall_score FROM trade_attribution "
                    f"WHERE trade_date >= '{et_days_ago(days if days > 0 else 30)}' "  # 2026-07-28: UTC→ET
                    f"ORDER BY created_at DESC LIMIT 50"
                )
                if attr_rows:
                    scores = [r["overall_score"] for r in attr_rows if r.get("overall_score")]
                    if scores:
                        trend["decision_quality"]["current"] = round(sum(scores) / len(scores), 1)

                # Previous period
                prev_rows = await ds.db_query(
                    f"SELECT overall_score FROM trade_attribution "
                    f"WHERE trade_date >= '{et_days_ago(prev_days)}' "
                    f"AND trade_date < '{et_days_ago(days if days > 0 else 30)}' "
                    f"ORDER BY created_at DESC LIMIT 50"
                )
                if prev_rows:
                    prev_scores = [r["overall_score"] for r in prev_rows if r.get("overall_score")]
                    if prev_scores:
                        trend["decision_quality"]["previous"] = round(sum(prev_scores) / len(prev_scores), 1)
            except Exception:
                pass

            # Calculate previous period profit factor
            if days > 0:
                prev_rows = await ds.db_query(
                    f"SELECT id, realized_pnl FROM trade_records "
                    f"WHERE source_type NOT IN ('PLAYBOOK_SHADOW','SCALPING') AND COALESCE(exit_reason,'') NOT IN ('PHANTOM_RECONCILE_VOID','ORPHAN_CLEANUP') AND exit_price > 0 "
                    f"AND trade_date >= '{et_days_ago(prev_days)}' "
                    f"AND trade_date < '{et_days_ago(days)}' "
                    f"ORDER BY trade_date DESC"
                )
                if prev_rows:
                    prev_trades = [{"pnl": float(r.get("realized_pnl") or 0)} for r in prev_rows]
                    trend["total_trades"]["previous"] = len(prev_trades)
                    win_pnl_prev = sum(t["pnl"] for t in prev_trades if t["pnl"] > 0)
                    loss_pnl_prev = abs(sum(t["pnl"] for t in prev_trades if t["pnl"] < 0))
                    pf_prev = round(win_pnl_prev / loss_pnl_prev, 2) if loss_pnl_prev > 0 else (99.99 if win_pnl_prev > 0 else 0.0)
                    trend["profit_factor"]["previous"] = pf_prev

            # Calculate deltas
            for key in ("decision_quality", "profit_factor"):
                cur = trend[key]["current"]
                prev = trend[key]["previous"]
                delta = round(cur - prev, 2)
                trend[key]["delta"] = delta
                if delta > 0:
                    trend[key]["direction"] = "up"
                elif delta < 0:
                    trend[key]["direction"] = "down"
                else:
                    trend[key]["direction"] = "neutral"

            period_label = "all" if days == 0 else f"{days}d"

            # ── Behavioral diagnostics (shadow_account_v2) ──
            behavioral = {"available": False}
            try:
                beh_rows = await ds.db_query(
                    "SELECT data FROM diagnostic_results "
                    "WHERE module_name = 'shadow_account_v2' "
                    "ORDER BY created_at DESC LIMIT 1"
                )
                if beh_rows:
                    import json as _json2
                    beh_data = _json2.loads(beh_rows[0].get("data") or "{}")
                    behavioral = {
                        "available": True,
                        "disposition": beh_data.get("disposition", {}),
                        "anchoring": beh_data.get("anchoring", {}),
                        "chasing": beh_data.get("chasing", {}),
                        "five_factor": beh_data.get("five_factor_attribution", {}),
                    }
            except Exception:
                pass

            # ── Attribution cards (recent 5 closed trades with attribution) ──
            attribution_cards = []
            try:
                card_rows = await ds.db_query(
                    "SELECT tr.symbol, tr.direction, tr.realized_pnl, tr.pnl_pct, "
                    "tr.entry_price, tr.exit_price, tr.quantity, tr.trade_date, "
                    "ta.overall_score, ta.direction_score, ta.signal_score, "
                    "ta.execution_score, ta.timing_score, ta.risk_score "
                    "FROM trade_records tr "
                    "LEFT JOIN trade_attribution ta ON tr.id = ta.trade_id "
                    "WHERE tr.source_type NOT IN ('PLAYBOOK_SHADOW','SCALPING') AND COALESCE(tr.exit_reason,'') NOT IN ('PHANTOM_RECONCILE_VOID','ORPHAN_CLEANUP') AND tr.exit_price > 0 "
                    "ORDER BY tr.trade_date DESC, tr.id DESC LIMIT 5"
                )
                _dim_labels = {"direction": "方向", "signal": "信号",
                               "execution": "执行", "timing": "时机", "risk": "风控"}
                _dim_col_map = {"direction": "direction_score", "signal": "signal_score",
                                "execution": "execution_score", "timing": "timing_score",
                                "risk": "risk_score"}
                for cr in card_rows:
                    dims = []
                    for dk, dl in _dim_labels.items():
                        col = _dim_col_map[dk]
                        score_val = cr.get(col)
                        if score_val is not None:
                            try:
                                s = float(score_val)
                                verdict = "✅" if s >= 60 else ("⚠️" if s >= 40 else "❌")
                            except (ValueError, TypeError):
                                verdict = "—"
                        else:
                            verdict = "—"
                        dims.append({"label": dl, "verdict": verdict})
                    # Derive grade from overall_score
                    _score = float(cr.get("overall_score") or 0)
                    if _score >= 80: _grade = "A"
                    elif _score >= 60: _grade = "B"
                    elif _score >= 40: _grade = "C"
                    else: _grade = "D"
                    attribution_cards.append({
                        "symbol": (cr.get("symbol") or "").replace("US.", ""),
                        "direction": cr.get("direction", ""),
                        "pnl": round(float(cr.get("realized_pnl") or 0), 2),
                        "pnl_pct": round(float(cr.get("pnl_pct") or 0), 4),
                        "entry_price": round(float(cr.get("entry_price") or 0), 2),
                        "exit_price": round(float(cr.get("exit_price") or 0), 2),
                        "quantity": int(cr.get("quantity") or 0),
                        "date": cr.get("trade_date", ""),
                        "grade": _grade,
                        "score": round(_score, 1),
                        "dimensions": dims,
                    })
            except Exception:
                pass

            result = {
                "dimension_stats": dim_stats,
                "weakest_dimension": weakest_dim,
                "plan_vs_unplanned": plan_vs_unplanned,
                "trend": trend,
                "period": period_label,
                "trade_count": trade_count,
                "behavioral": behavioral,
                "attribution_cards": attribution_cards,
            }
            return api_ok(result)
        except Exception as e:
            logger.error(f"[trade/review] {e}")
            return api_error("ANALYTICS_ERROR", "数据查询异常", retryable=True,
                             data={"dimension_stats": {}, "trade_count": 0})

    # ═══════════════════════════════════════════════════════════
    #  新机制观测面板 (2026-07-28 看板审计 P1): 本周上线机制此前看板零展示位 —
    #  高ATR/保本谷 shadow、板块脉搏、topup、entry_price_source 修复爬坡。
    #  只读聚合, 全部 best-effort (单项失败不拖垮整卡)。
    # ═══════════════════════════════════════════════════════════
    @router.get("/api/analytics/new-mechanisms")
    async def new_mechanisms():
        import json as _json
        from pathlib import Path as _P
        _root = _P("/opt/ftseek/FTSeek-US")
        out: dict = {"asof_et": et_today()}
        ds = get_data_service()
        # 1) shadow 计数 (高 ATR 停滞 / 保本死亡谷 A/B)
        for key, fn in (("high_atr_stall_shadow", "data/high_atr_stall_shadow.jsonl"),
                        ("breakeven_shadow", "data/breakeven_shadow.jsonl")):
            try:
                _f = _root / fn
                lines = [_json.loads(x) for x in _f.read_text().splitlines() if x.strip()] if _f.exists() else []
                item = {"count": len(lines), "latest": lines[-1] if lines else None}
                if key == "breakeven_shadow" and lines:
                    item["by_variant"] = {
                        "A": sum(1 for r in lines if str(r.get("variant", "")).startswith("A")),
                        "B": sum(1 for r in lines if str(r.get("variant", "")).startswith("B")),
                    }
                out[key] = item
            except Exception:
                out[key] = {"count": -1}
        # 2) 板块脉搏 (盘中 30min 刷新)
        try:
            _pf = _root / "data" / "sector_pulse.json"
            if _pf.exists():
                _pu = _json.loads(_pf.read_text())
                out["sector_pulse"] = {"date": _pu.get("date"), "asof_et": _pu.get("asof_et"),
                                       "weakest3": _pu.get("weakest3"), "strongest3": _pu.get("strongest3")}
        except Exception:
            pass
        # 3) topup 今日产出 (全史零 → 修复观察点)
        try:
            rows = await ds.db_query(
                "SELECT COUNT(*) n FROM playbook_instructions "
                "WHERE date = ? AND raw_instruction_json LIKE '%topup_pool%'", (et_today(),))
            out["topup_today"] = int(rows[0]["n"]) if rows else 0
        except Exception:
            out["topup_today"] = -1
        # 4) entry_price_source 近 7 日分布 (broker_deal_confirm 爬坡 = 07-27 假价修复验收)
        try:
            rows = await ds.db_query(
                "SELECT metadata_json FROM trade_records WHERE trade_date >= ? "
                "AND source_type LIKE 'PLAYBOOK%'", (et_days_ago(7),))
            _dist: dict = {}
            for r in rows:
                try:
                    _src = (_json.loads(r.get("metadata_json") or "{}")).get("entry_price_source") or "unknown"
                    _dist[_src] = _dist.get(_src, 0) + 1
                except Exception:
                    pass
            out["entry_price_source_7d"] = _dist
        except Exception:
            out["entry_price_source_7d"] = {}
        return api_ok(out)

    app.include_router(router)
