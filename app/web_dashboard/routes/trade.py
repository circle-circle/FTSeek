"""
交易路由 — 手动下单 + 平仓 + 订单管理 + 交易页面
"""
import asyncio
import logging
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.web_dashboard.data_service import get_data_service

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
            return JSONResponse(content={"success": False, "error": "请求体无效 (需要 JSON)"}, status_code=400)

        if not body or not isinstance(body, dict):
            return JSONResponse(content={"success": False, "error": "请求体为空"}, status_code=400)

        svc = get_manual_order_service()

        try:
            quantity = int(body.get("quantity", 0))
        except (ValueError, TypeError):
            return JSONResponse(content={"success": False, "error": "数量必须是整数"}, status_code=400)

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
        )

        result = svc.submit_order(order)
        ds = get_data_service()
        ds.invalidate_cache()
        return JSONResponse(content=result)

    @router.post("/api/orders/close")
    async def close_position(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(content={"success": False, "error": "请求无效"}, status_code=400)
        svc = get_manual_order_service()
        close_qty = int(body.get("quantity", 0))
        if close_qty < 0 or close_qty > 1_000_000:
            return JSONResponse(content={"success": False, "error": "数量无效"}, status_code=400)
        result = svc.close_position(
            symbol=body.get("symbol", ""),
            reason=body.get("reason", "手动平仓"),
            close_qty=close_qty,
        )
        ds = get_data_service()
        ds.invalidate_cache()
        return JSONResponse(content=result)

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
            return JSONResponse(content={"success": False, "error": "请求无效"}, status_code=400)
        order_id = str(body.get("order_id", "")).strip()
        if not order_id or not order_id.replace(".", "").replace("-", "").isalnum():
            return JSONResponse(content={"success": False, "error": "order_id 格式无效"}, status_code=400)
        svc = get_manual_order_service()
        result = svc.cancel_order(order_id=order_id)
        ds = get_data_service()
        ds.invalidate_cache()
        status_code = 200 if result.get("success") else 400
        return JSONResponse(content=result, status_code=status_code)

    @router.get("/api/orders/audit")
    async def get_order_audit(limit: int = 50):
        limit = min(max(limit, 1), 500)  # M1: cap at 500
        svc = get_manual_order_service()
        history = svc.get_audit_history(limit)
        return {"orders": history}

    @router.post("/api/orders/audit/archive")
    async def archive_audit(request: Request):
        """归档旧审计记录到独立表，保留最近 N 天"""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(content={"error": "请求无效"}, status_code=400)
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
            return {"success": True, "archived": moved, "cutoff_date": cutoff[:10]}
        except Exception as e:
            logger.error(f"[审计归档] 失败: {e}")
            return JSONResponse(content={"error": str(e)}, status_code=500)
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
            return JSONResponse(content={"error": "broker_not_connected"}, status_code=503)

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
                cash = safe_float(row.get("us_cash", 0))
                power = safe_float(row.get("usd_net_cash_power", 0))
                max_short_power = safe_float(row.get("max_power_short", 0)) / 7.8
                if power <= 0:
                    power = safe_float(row.get("power", 0)) / 7.8
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
            return {"symbol": symbol, "error": str(e)}

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

        return {
            "positions": positions,
            "orders": orders,
            "account": account,
            "stop_distances": stop_distances,
        }

    @router.get("/api/trade/slow-data")
    async def trade_slow_data():
        """
        交易页慢速数据端点 (5min)
        合并 audit + manual-performance, 低频变化数据
        """
        svc = get_manual_order_service()
        audit = svc.get_audit_history(50)

        ds = get_data_service()
        cached = ds.cache.get("manual_perf")
        manual_perf = None
        if cached is not None:
            manual_perf = cached
        else:
            # Fetch inline (reuse analytics logic)
            try:
                manual = await ds.db_query_one("""
                    SELECT COUNT(*) as cnt,
                           SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                           COALESCE(SUM(realized_pnl), 0) as total_pnl,
                           COALESCE(AVG(CASE WHEN realized_pnl > 0 THEN realized_pnl END), 0) as avg_win,
                           COALESCE(AVG(CASE WHEN realized_pnl < 0 THEN realized_pnl END), 0) as avg_loss
                    FROM trade_records
                    WHERE strategy_id = 'manual' OR source_type = 'MANUAL'
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
                """)
                recent = await ds.db_query("""
                    SELECT symbol, direction, realized_pnl, exit_reason, trade_date,
                           entry_price, exit_price, quantity
                    FROM trade_records
                    WHERE strategy_id = 'manual' OR source_type = 'MANUAL'
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

                manual_perf = {
                    "manual": _calc(manual), "auto": _calc(auto),
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
                ds.cache.set("manual_perf", manual_perf, ttl=60)
            except Exception as e:
                logger.error(f"[trade/slow-data] manual perf failed: {e}")
                manual_perf = {"manual": {}, "auto": {}, "recent_trades": []}

        return {
            "audit": {"orders": audit},
            "manual_perf": manual_perf,
        }

    app.include_router(router)
