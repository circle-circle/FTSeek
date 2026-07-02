"""
交易路由 — 手动下单 + 平仓 + 订单管理 + 交易页面
"""
import asyncio
import logging
from datetime import datetime
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.web_dashboard.data_service import get_data_service
from app.web_dashboard.api_response import api_ok, api_error, err_broker_unavailable

logger = logging.getLogger("ftseek_us.routes.trade")
router = APIRouter()


def register(app, templates):
    from app.web_dashboard.manual_order_service import get_manual_order_service, ManualOrder

    @router.get("/manual-trade")
    async def manual_trade_redirect(request: Request):
        return RedirectResponse(url="/trade", status_code=301)

    @router.get("/trade", response_class=HTMLResponse)
    async def trade_page(request: Request):
        from app.web_dashboard.routes.dashboard import _is_live
        return templates.TemplateResponse("trade.html", {
            "request": request,
            "title": "交易 - FTSeek-US",
            "is_live": _is_live(),
        })

    @router.get("/live-status")
    async def live_status_redirect(request: Request):
        return RedirectResponse(url="/trade", status_code=301)

    @router.post("/api/orders")
    async def place_manual_order(request: Request):
        try:
            body = await request.json()
        except Exception:
            return api_error("BAD_REQUEST", "请求体无效 (需要 JSON)", 400)

        if not body or not isinstance(body, dict):
            return api_error("BAD_REQUEST", "请求体为空", 400)

        svc = get_manual_order_service()

        try:
            quantity = int(body.get("quantity", 0))
        except (ValueError, TypeError):
            return api_error("BAD_REQUEST", "数量必须是整数", 400)

        try:
            price = float(body["price"]) if body.get("price") else None
        except (ValueError, TypeError):
            price = None

        order = ManualOrder(
            symbol=body.get("symbol", ""),
            direction=body.get("direction", "LONG"),
            action=body.get("action", "BUY"),
            quantity=quantity,
            order_type=body.get("order_type", "MARKET"),
            price=price,
            reason=body.get("reason", ""),
            mode=svc._get_mode_label(),
            time_in_force=body.get("time_in_force", "DAY"),
            fill_outside_rth=bool(body.get("fill_outside_rth", False)),
            stop_loss=float(body.get("stop_loss", 0) or 0),
            take_profit=float(body.get("take_profit", 0) or 0),
            acknowledge_price_warning=bool(body.get("acknowledge_price_warning", False)),
        )

        result = svc.submit_order(order, idempotency_key=body.get("idempotency_key"))
        ds = get_data_service()
        ds.invalidate_cache()
        if result.get("success"):
            return api_ok(result)
        else:
            return api_error("ORDER_ERROR", result.get("error", "下单失败"), 400)

    @router.post("/api/orders/close")
    async def close_position(request: Request):
        try:
            body = await request.json()
        except Exception:
            return api_error("BAD_REQUEST", "请求无效", 400)
        svc = get_manual_order_service()
        close_qty = int(body.get("quantity", 0))
        # P0-2 修复 (2026-06-12): 100K 股已能覆盖任何合理美股持仓, broker 端会做最终校验
        if close_qty < 0 or close_qty > 100_000:
            return api_error("BAD_REQUEST", "数量无效或超限 (上限 100,000 股)", 400)
        # 限价平仓 (止损/止盈) — price 为空则市价平仓
        limit_price = body.get("price")
        if limit_price is not None:
            try:
                limit_price = float(limit_price)
                if limit_price <= 0:
                    limit_price = None
            except (ValueError, TypeError):
                limit_price = None
        result = svc.close_position(
            symbol=body.get("symbol", ""),
            reason=body.get("reason", "手动平仓"),
            close_qty=close_qty,
            price=limit_price,
            fill_outside_rth=bool(body.get("fill_outside_rth", False)),
        )
        ds = get_data_service()
        ds.invalidate_cache()
        if result.get("success"):
            return api_ok(result)
        else:
            return api_error("ORDER_ERROR", result.get("error", "平仓失败"), 400)

    @router.get("/api/orders/today")
    async def get_today_orders():
        ds = get_data_service()
        cached = ds.cache.get("orders_today")
        if cached is not None:
            return cached
        svc = get_manual_order_service()
        orders = svc.get_today_orders()
        result = {"orders": orders}
        ds.cache.set("orders_today", result, ttl=10)
        return result

    @router.post("/api/orders/cancel")
    async def cancel_order(request: Request):
        try:
            body = await request.json()
        except Exception:
            return api_error("BAD_REQUEST", "请求无效", 400)
        order_id = str(body.get("order_id", "")).strip()
        if not order_id or not order_id.replace(".", "").replace("-", "").isalnum():
            return api_error("BAD_REQUEST", "order_id 格式无效", 400)
        svc = get_manual_order_service()
        result = svc.cancel_order(order_id=order_id)
        ds = get_data_service()
        ds.invalidate_cache()
        if result.get("success"):
            return api_ok(result)
        else:
            return api_error("ORDER_ERROR", result.get("error", "撤单失败"), 400)

    @router.post("/api/positions/modify-sl")
    async def modify_stop_loss(request: Request):
        """P0-3 修复 (2026-06-12): 修改持仓止损 — 调用 service 撤旧 STOP + 下新 STOP + 更新 DB
        实盘止损现已真正落到 broker (Futu SIMULATE 不支持 STOP, 走 graceful degrade 仅 DB)。
        """
        try:
            body = await request.json()
        except Exception:
            return api_error("BAD_REQUEST", "请求无效", 400)
        symbol = str(body.get("symbol", "")).strip()
        try:
            new_sl = float(body.get("stop_loss", 0))
        except (ValueError, TypeError):
            new_sl = 0.0
        if not symbol or new_sl <= 0:
            return api_error("BAD_REQUEST", "参数无效", 400)

        svc = get_manual_order_service()
        result = svc.modify_stop_loss(symbol=symbol, new_sl=new_sl)
        ds = get_data_service()
        ds.invalidate_cache()
        if result.get("success"):
            return api_ok(result)
        else:
            return api_error("ORDER_ERROR", result.get("error", "修改止损失败"), 400)

    @router.post("/api/positions/takeover")
    async def takeover_position(request: Request):
        """P1-2 (2026-06-12): import broker LEGACY position into DB management"""
        try:
            body = await request.json()
        except Exception:
            return api_error("BAD_REQUEST", "invalid request", 400)
        symbol = str(body.get("symbol", "")).strip()
        owner = str(body.get("owner", "manual")).strip().lower()
        if not symbol or not symbol.startswith("US."):
            return api_error("BAD_REQUEST", "invalid symbol", 400)
        if owner not in ("manual", "auto"):
            owner = "manual"
        svc = get_manual_order_service()
        result = svc.takeover_position(symbol=symbol, owner=owner)
        ds = get_data_service()
        ds.invalidate_cache()
        if result.get("success"):
            return api_ok(result)
        return api_error("TAKEOVER_ERROR", result.get("error", "接管失败"), 400)

    @router.get("/api/orders/audit")
    async def get_order_audit(limit: int = 50):
        limit = min(max(limit, 1), 500)  # M1: cap at 500
        svc = get_manual_order_service()
        history = svc.get_audit_history(limit)
        return {"orders": history}

    @router.get("/api/trade/attribution")
    async def get_trade_attribution(symbol: str = "", limit: int = 10):
        """获取最近已平仓交易的归因分析"""
        import json
        ds = get_data_service()
        rows = await ds.get_recent_trades(symbol=symbol, limit=limit)

        results = []
        for row_dict in rows:
            # 尝试从 metadata_json 读取已缓存的归因
            meta = {}
            if row_dict.get("metadata_json"):
                try:
                    meta = json.loads(row_dict["metadata_json"])
                except Exception:
                    pass
            if "attribution" in meta:
                results.append(meta["attribution"])
            else:
                # 实时生成
                try:
                    from app.analysis.trade_attribution import attribute_from_db
                    attr = attribute_from_db(row_dict)
                    if attr:
                        results.append(attr.to_dict())
                except Exception:
                    pass
        return {"attributions": results}

    @router.post("/api/orders/audit/archive")
    async def archive_audit(request: Request):
        """归档旧审计记录到独立表，保留最近 N 天"""
        try:
            body = await request.json()
        except Exception:
            return api_error("BAD_REQUEST", "请求无效", 400)
        keep_days = int(body.get("keep_days", 30))
        keep_days = min(max(keep_days, 7), 365)

        import sqlite3
        from datetime import datetime, timedelta
        from app.core.db_utils import get_main_db
        db_path = str(get_main_db())
        cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat()

        conn = sqlite3.connect(db_path, timeout=10)
        try:
            # 确保归档表存在
            conn.execute("""
                CREATE TABLE IF NOT EXISTS manual_order_audit_archive (
                    id INTEGER PRIMARY KEY,
                    timestamp TEXT, symbol TEXT, direction TEXT, action TEXT,
                    quantity INTEGER, order_type TEXT, price REAL, reason TEXT,
                    mode TEXT, order_id TEXT, status TEXT, error_msg TEXT,
                    archived_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # 移动旧记录到归档表
            moved = conn.execute(
                "INSERT INTO manual_order_audit_archive "
                "(id, timestamp, symbol, direction, action, quantity, order_type, "
                "price, reason, mode, order_id, status, error_msg) "
                "SELECT id, timestamp, symbol, direction, action, quantity, order_type, "
                "price, reason, mode, order_id, status, error_msg "
                "FROM manual_order_audit WHERE timestamp < ?",
                (cutoff,)
            ).rowcount
            conn.execute("DELETE FROM manual_order_audit WHERE timestamp < ?", (cutoff,))
            conn.commit()
            logger.info(f"[审计归档] 归档 {moved} 条记录 (早于 {cutoff[:10]})")
            return api_ok({"archived": moved, "cutoff_date": cutoff[:10]})
        except Exception as e:
            logger.error(f"[审计归档] 失败: {e}")
            return api_error("ARCHIVE_ERROR", "操作失败，请稍后重试", 500, retryable=True)
        finally:
            conn.close()

    @router.get("/api/quote/{symbol}")
    async def get_quote(symbol: str):
        svc = get_manual_order_service()
        return svc.get_quote(symbol.upper())

    @router.get("/api/order-capacity/{symbol}")
    async def get_order_capacity(symbol: str):
        svc = get_manual_order_service()
        if not svc.connect():
            return err_broker_unavailable()

        try:
            from futu import TrdEnv, RET_OK
            from app.web_dashboard.data_service import safe_float
            import os as _os

            symbol = symbol.upper()
            quote = svc.get_quote(symbol)
            cur_price = quote.get("cur_price", 0)
            if cur_price <= 0:
                return {"symbol": symbol, "error": "无法获取报价", "cur_price": 0}

            is_live = svc._is_live()
            trd_env = TrdEnv.REAL if is_live else TrdEnv.SIMULATE
            acc_id = int(svc._read_env(
                "REAL_ACCOUNT_ID" if is_live else "SIMULATE_ACCOUNT_ID", "0"
            ))
            ret, funds = svc._broker_ctx.accinfo_query(trd_env=trd_env, acc_id=acc_id)
            if ret != RET_OK:
                return {"symbol": symbol, "cur_price": cur_price, "error": "账户查询失败"}

            row = funds.iloc[0]
            currency = str(row.get("currency", "")).upper()
            is_usd = currency in ("USD", "")

            if is_usd:
                cash = safe_float(row.get("cash", 0))
                power = safe_float(row.get("power", 0))
                max_short_power = safe_float(row.get("max_power_short", 0))
            else:
                # HKD 账户: 用 Futu 官方 USD 字段
                # 可用现金 = us_avl_withdrawal_cash (扣除保证金后的真实可用)
                cash = safe_float(row.get("us_avl_withdrawal_cash", 0))
                if cash <= 0:
                    cash = safe_float(row.get("us_cash", 0))  # fallback
                power = safe_float(row.get("usd_net_cash_power", 0))
                hkd_assets = safe_float(row.get("total_assets", 0))
                usd_assets = safe_float(row.get("usd_assets", 0))
                fx = hkd_assets / usd_assets if usd_assets > 0 else 7.8
                max_short_power = safe_float(row.get("max_power_short", 0)) / fx
                if power <= 0:
                    power = safe_float(row.get("power", 0)) / fx
                if max_short_power <= 0:
                    max_short_power = power

            ret2, positions = svc._broker_ctx.position_list_query(trd_env=trd_env, acc_id=acc_id)
            held_qty = 0
            held_side = ""
            if ret2 == RET_OK and not positions.empty:
                pos = positions[positions["code"] == symbol]
                if not pos.empty:
                    held_qty = int(abs(float(pos.iloc[0].get("qty", 0))))
                    held_side = str(pos.iloc[0].get("position_side", "")).upper()

            is_short_pos = held_side == "SHORT"
            cash_can_buy = int(cash / cur_price) if cur_price > 0 else 0
            max_can_buy = int(power / cur_price) if cur_price > 0 else 0
            can_sell = 0 if is_short_pos else held_qty   # LONG 才能卖
            can_short = int(max_short_power / cur_price) if cur_price > 0 else 0
            can_cover = held_qty if is_short_pos else 0  # SHORT 才能 cover

            return {
                "symbol": symbol,
                "cur_price": cur_price,
                "currency": "USD",
                "cash_can_buy": cash_can_buy,
                "max_can_buy": max_can_buy,
                "can_sell": can_sell,
                "can_short": can_short,
                "can_cover": can_cover,
                "held_qty": held_qty,
                "cash": round(cash, 2),
                "power": round(power, 2),
                "max_short_power": round(max_short_power, 2),
                "account_currency": currency,
            }
        except Exception as e:
            logger.error(f"[order-capacity] {symbol}: {e}")
            return {"symbol": symbol, "error": "数据查询异常"}

    @router.get("/api/live-account")
    async def get_live_account():
        """实盘账户状态 (向后兼容, 内部使用 DataService)"""
        ds = get_data_service()
        cached = ds.cache.get("live_account_compat")
        if cached is not None:
            return cached

        acc_raw, pos_raw = await asyncio.gather(
            ds.get_account_raw(),
            ds.get_positions_raw(),
        )

        from app.web_dashboard.routes.dashboard import _parse_account, _parse_positions
        account_info = _parse_account(acc_raw)
        pos_list = await _parse_positions(ds, pos_raw)

        svc = ds.svc()
        result = {
            "mode": svc._get_mode_label() if svc._connected else "simulate",
            "is_live": svc._is_live() if svc._connected else False,
            "account": account_info,
            "positions": pos_list,
            "timestamp": __import__("datetime").datetime.now().isoformat(),
        }
        ds.cache.set("live_account_compat", result, ttl=5)
        return result

    # ═══════════════════════════════════════════════════════════
    #  Trade Snapshot — 合并 positions + orders 为单次请求
    # ═══════════════════════════════════════════════════════════

    @router.get("/api/trade/snapshot")
    async def trade_snapshot():
        """
        交易页快速轮询端点 (15s)
        合并 positions + orders + stop_distances, 替代 2 个并行 API 调用
        """
        ds = get_data_service()

        # 并行获取 positions 和 orders (DataService 有 TTL cache)
        acc_raw, pos_raw = await asyncio.gather(
            ds.get_account_raw(),
            ds.get_positions_raw(),
        )

        from app.web_dashboard.routes.dashboard import _parse_account, _parse_positions
        account = _parse_account(acc_raw)
        positions = await _parse_positions(ds, pos_raw)

        # Today orders
        svc = ds.svc()
        orders = svc.get_today_orders() if svc._connected else []

        # Stop distances (from risk data)
        stop_distances = []
        try:
            from app.web_dashboard.routes.dashboard import _calc_stop_distances
            svc_obj = ds.svc()
            is_live = svc_obj._is_live() if svc_obj._connected else False
            stop_distances = await _calc_stop_distances(ds, positions, is_live)
        except Exception:
            pass

        # Today PnL (realized from DB)
        pnl_data = {"today_pl": 0, "unrealized": 0}
        try:
            from app.web_dashboard.routes.dashboard import _calc_pnl
            pnl_data = await _calc_pnl(ds, positions)
        except Exception:
            pass

        # Audit orders (for unified trade records)
        audit_orders = []
        try:
            audit_orders = svc.get_audit_history(50)
        except Exception:
            pass

        result = {
            "positions": positions,
            "orders": orders,
            "account": account,
            "stop_distances": stop_distances,
            "pnl": pnl_data,
            "audit": {"orders": audit_orders},
        }
        # Debug: 记录返回数据量
        import json
        logger.info(f"[trade/snapshot] positions={len(positions)}, orders={len(orders)}, account_keys={list(account.keys()) if account else 'empty'}")
        return result

    @router.get("/api/trade/slow-data")
    async def trade_slow_data():
        """
        交易决策质量 — 统一端点 (5min cache)
        返回: audit + decision_quality + behavioral + plan_adherence + profit_factor + trade_cards
        """
        import glob as _glob
        import json as _json
        import os as _os

        svc = get_manual_order_service()
        audit = svc.get_audit_history(50)

        ds = get_data_service()
        cached = ds.cache.get("decision_quality_v2")
        if cached is not None:
            return {**cached, "audit": {"orders": audit}}

        result = {
            "decision_quality": {"score": 0, "grade": "--", "sample_size": 0, "sample_sufficient": False},
            "behavioral": {"available": False, "score": 0, "grade": "--", "disposition": {}, "anchoring": {}, "chasing": {}},
            "plan_adherence": {"available": False, "traded_rate": 0, "hit_rate": 0, "planned_count": 0, "date": ""},
            "profit_factor": 0,
            "trade_cards": [],
        }

        try:
            # ── 1. Trade cards with attribution ──
            trades = await ds.db_query("""
                SELECT id, symbol, direction, realized_pnl, pnl_pct, trade_date,
                       entry_price, exit_price, quantity, exit_reason, entry_time, exit_time,
                       metadata_json, source_type
                FROM trade_records
                WHERE source_type NOT IN ('PLAYBOOK_SHADOW','SCALPING') AND COALESCE(exit_reason,'') NOT IN ('PHANTOM_RECONCILE_VOID','ORPHAN_CLEANUP') AND exit_reason NOT IN ('PHANTOM_RECONCILE_VOID','ORPHAN_CLEANUP') AND exit_price > 0
                ORDER BY id DESC LIMIT 20
            """)

            # Load latest plan effectiveness for planned matching
            plan_targets = set()
            plan_date = ""
            plan_traded_rate = 0.0
            plan_hit_rate = 0.0
            plan_count = 0
            plan_files = sorted(_glob.glob("/opt/ftseek/FTSeek-US/data/plan_effectiveness/daily_*.json"))
            if plan_files:
                try:
                    with open(plan_files[-1]) as f:
                        pe = _json.load(f)
                    plan_date = pe.get("date", "")
                    plan_traded_rate = pe.get("traded_rate", 0)
                    plan_hit_rate = pe.get("hit_rate", 0)
                    plan_count = pe.get("planned_count", 0)
                    for t in pe.get("targets", []):
                        if t.get("traded"):
                            plan_targets.add(t.get("symbol", ""))
                except Exception:
                    pass

            cards = []
            total_win_pnl = 0.0
            total_loss_pnl = 0.0
            attr_scores = []

            for r in trades:
                pnl = round(float(r.get("realized_pnl") or 0), 2)
                if pnl > 0:
                    total_win_pnl += pnl
                else:
                    total_loss_pnl += abs(pnl)

                # Parse attribution from metadata_json
                attr = None
                meta_raw = r.get("metadata_json")
                if meta_raw:
                    try:
                        meta = _json.loads(meta_raw)
                        a = meta.get("attribution")
                        if a:
                            attr = {
                                "dimensions": a.get("dimensions", []),
                                "summary": a.get("summary", ""),
                            }
                            # Collect dimension scores for decision quality
                            for dim in a.get("dimensions", []):
                                s = dim.get("score")
                                if s is not None:
                                    attr_scores.append(float(s))
                    except Exception:
                        pass

                # Also check trade_attribution table for overall_score
                sym = r.get("symbol", "")
                td = r.get("trade_date", "")

                # Calculate holding hours
                holding_h = None
                entry_t = r.get("entry_time")
                exit_t = r.get("exit_time")
                if entry_t and exit_t:
                    try:
                        from datetime import datetime as _dt
                        # Handle various formats
                        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                            try:
                                et = _dt.strptime(str(entry_t)[:26], fmt)
                                xt = _dt.strptime(str(exit_t)[:26], fmt)
                                holding_h = round((xt - et).total_seconds() / 3600, 1)
                                break
                            except ValueError:
                                continue
                    except Exception:
                        pass

                planned = sym in plan_targets

                cards.append({
                    "id": r.get("id"),
                    "symbol": sym,
                    "direction": r.get("direction", ""),
                    "pnl": pnl,
                    "pnl_pct": round(float(r.get("pnl_pct") or 0), 4),
                    "date": td,
                    "entry_price": round(float(r.get("entry_price") or 0), 2),
                    "exit_price": round(float(r.get("exit_price") or 0), 2),
                    "quantity": r.get("quantity", 0),
                    "exit_reason": r.get("exit_reason", ""),
                    "holding_hours": holding_h,
                    "attribution": attr,
                    "planned": planned,
                })

            result["trade_cards"] = cards

            # ── 2. Profit factor ──
            pf = round(total_win_pnl / total_loss_pnl, 2) if total_loss_pnl > 0 else (99.99 if total_win_pnl > 0 else 0)
            result["profit_factor"] = pf

            # ── 3. Decision quality from trade_attribution ──
            try:
                attr_rows = await ds.db_query("""
                    SELECT overall_score FROM trade_attribution
                    WHERE trade_date >= date('now', '-30 days')
                    ORDER BY created_at DESC LIMIT 20
                """)
                if attr_rows:
                    scores = [r["overall_score"] for r in attr_rows if r.get("overall_score")]
                    if scores:
                        avg_score = round(sum(scores) / len(scores), 1)
                        sample = len(scores)
                        if avg_score >= 90: grade = "A+"
                        elif avg_score >= 80: grade = "A"
                        elif avg_score >= 70: grade = "B+"
                        elif avg_score >= 60: grade = "B"
                        elif avg_score >= 50: grade = "C+"
                        elif avg_score >= 40: grade = "C"
                        else: grade = "D"
                        result["decision_quality"] = {
                            "score": avg_score, "grade": grade,
                            "sample_size": sample, "sample_sufficient": sample >= 5,
                        }
            except Exception:
                pass

            # ── 4. Behavioral from diagnostic_results ──
            try:
                diag = await ds.db_query_one("""
                    SELECT data FROM diagnostic_results
                    WHERE module_name = 'shadow_account_v2' AND status = 'success'
                    ORDER BY created_at DESC LIMIT 1
                """)
                if diag and diag.get("data"):
                    bdata = _json.loads(diag["data"])
                    bs = bdata.get("behavioral_score", {})
                    if bs:
                        result["behavioral"] = {
                            "available": True,
                            "score": bs.get("score", 0),
                            "grade": bs.get("grade", "--"),
                            "disposition": bdata.get("disposition_effect", {}),
                            "anchoring": bdata.get("anchoring", {}),
                            "chasing": bdata.get("chasing", {}),
                        }
            except Exception:
                pass

            # ── 5. Plan adherence ──
            if plan_files:
                result["plan_adherence"] = {
                    "available": True,
                    "traded_rate": plan_traded_rate,
                    "hit_rate": plan_hit_rate,
                    "planned_count": plan_count,
                    "date": plan_date,
                }

            ds.cache.set("decision_quality_v2", result, ttl=60)
        except Exception as e:
            logger.error(f"[trade/slow-data] error: {e}")

        return {**result, "audit": {"orders": audit}}


    # ═══════════════════════════════════════════════════════════
    #  Auto Trade — Dashboard 一键开关 + 激进程度 (Task #2)
    # ═══════════════════════════════════════════════════════════
    from app.web_dashboard.mode_switch_service import get_mode_switch_service

    _YAML_PATH = "/opt/ftseek/FTSeek-US/config/unified_config.yaml"

    def _write_yaml_auto_trading(enabled: bool) -> bool:
        """同步写入 YAML auto_trading_enabled (原子替换, 与 _submit_order 直读对齐)"""
        import re, os, tempfile
        try:
            with open(_YAML_PATH, "r") as f:
                content = f.read()
            pattern = r'(  auto_trading_enabled:\s*)(true|false)'
            replacement = f'\\g<1>{"true" if enabled else "false"}'
            new_content = re.sub(pattern, replacement, content)
            if new_content == content:
                return False
            dir_name = os.path.dirname(_YAML_PATH)
            fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as tmp_f:
                    tmp_f.write(new_content)
                os.replace(tmp_path, _YAML_PATH)
            except Exception:
                os.unlink(tmp_path)
                raise
            return True
        except Exception as e:
            logger.error(f"[auto-trade] YAML 写入失败: {e}")
            return False

    @router.post("/api/auto-trade/toggle")
    async def toggle_auto_trade(request: Request):
        """Dashboard 一键开关自动交易 (不影响手动链路).

        同时写入内存 (ModeSwitchService) + YAML 文件,
        确保信号扫描和订单提交两道门控同步切换.
        """
        try:
            body = await request.json()
        except Exception:
            return api_error("BAD_REQUEST", "请求体无效 (需要 JSON)", 400)
        enabled = bool(body.get("enabled", False))

        # 1. 内存开关 (控制信号扫描 + 订单提交 Gate)
        result = get_mode_switch_service().set_auto_enabled(enabled)

        # 2. 持久化到 YAML (重启后 ModeSwitchService 从此恢复)
        _write_yaml_auto_trading(enabled)

        # 模式状态变化后清缓存, 让 Dashboard 立即看到新状态
        try:
            get_data_service().invalidate_cache()
        except Exception:
            pass
        if result.get("success"):
            return api_ok(result)
        return api_error("AUTO_TRADE_ERROR", result.get("error", "操作失败"), 400)

    @router.post("/api/auto-trade/level")
    async def set_auto_level(request: Request):
        """设置自动交易激进程度: conservative / normal / aggressive."""
        try:
            body = await request.json()
        except Exception:
            return api_error("BAD_REQUEST", "请求体无效 (需要 JSON)", 400)
        level = str(body.get("level", "conservative")).strip().lower()
        result = get_mode_switch_service().set_auto_aggression(level)
        try:
            get_data_service().invalidate_cache()
        except Exception:
            pass
        if result.get("success"):
            return api_ok(result)
        return api_error("AUTO_TRADE_ERROR", result.get("error", "操作失败"), 400)

    @router.get("/api/auto-trade/status")
    async def get_auto_status():
        """获取自动交易当前状态 (开关 + 激进程度 + 账户 + graduation + 最近切换时间)."""
        return get_mode_switch_service().get_mode_status()

    @router.post("/api/auto-trade/auto-account")
    async def set_auto_account(request: Request):
        """设置自动交易目标账户 (sim|real). real 需 graduation + ALLOW_REAL_TRADING."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        account = (body.get("account") or "").strip().lower()
        result = get_mode_switch_service().set_auto_account(account)
        if result.get("success"):
            return api_ok(result)
        return api_error("AUTO_ACCOUNT_ERROR", result.get("error", "操作失败"), 400, data={"graduation": result.get("graduation")})

    app.include_router(router)
