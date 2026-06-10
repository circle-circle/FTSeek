"""
简化版 Dashboard Web 服务
"""
import logging
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from datetime import datetime, timezone, timedelta

# 美东时间 (ET) 日期辅助函数 — 美股交易日以 ET 为准
_ET_OFFSET = timedelta(hours=-4)  # EDT (夏令时), EST 为 -5

def _et_now() -> datetime:
    """当前美东时间"""
    return datetime.now(timezone.utc) + _ET_OFFSET

def _et_today() -> str:
    """美东今日日期 (YYYY-MM-DD)，用于查询 trade_date"""
    return _et_now().strftime("%Y-%m-%d")

def _et_days_ago(n: int) -> str:
    """美东 N 天前日期"""
    return (_et_now() - timedelta(days=n)).strftime("%Y-%m-%d")
from typing import Dict, Any

from app.simple_dashboard import SimpleDashboard
from app.storage.metrics_tables import MetricsTables
from app.observability.metrics_api import MetricsAPI
from app.observability.dingtalk_alert import DingTalkAlerter

logger = logging.getLogger("ftseek_us.dashboard")

dashboard = SimpleDashboard()

def dashboard_is_live() -> bool:
    """从 .env.us 实时读取模式 (不依赖进程环境变量, 无需重启 Dashboard)"""
    env_file = Path(__file__).parent.parent.parent / ".env.us"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                if line.strip().startswith("TRADE_ENV="):
                    return line.strip().split("=", 1)[1].strip() == "0"
    return False

# 初始化指标 API
from app.core.paths import get_trade_records_db
db_path = str(get_trade_records_db())
metrics_tables = MetricsTables(db_path)
metrics_api = MetricsAPI(metrics_tables)

# 钉钉告警配置 (从环境变量或配置读取)
DINGTALK_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN"
DINGTALK_SECRET = ""
dingtalk_alerter = DingTalkAlerter(DINGTALK_WEBHOOK, DINGTALK_SECRET) if DINGTALK_WEBHOOK else None

def create_app() -> FastAPI:
    """创建 FastAPI 应用"""
    app = FastAPI(
        title="FTSeek-US Dashboard",
        description="美股交易 Dashboard",
        version="1.0.0",
    )
    
    # 配置模板
    base_dir = Path(__file__).parent
    templates_dir = base_dir / "templates"
    templates_dir.mkdir(exist_ok=True)
    
    templates = Jinja2Templates(directory=str(templates_dir))

    # ─────────────────────────────────────────────────────────────
    # 认证中间件
    # ─────────────────────────────────────────────────────────────
    from app.web_dashboard.auth import (
        is_public_path, verify_password, create_session_token,
        verify_session_token, has_password_configured,
    )

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path

        # 公开路径无需认证
        if is_public_path(path):
            return await call_next(request)

        # 检查 session cookie
        token = request.cookies.get("ftseek_session", "")
        if not verify_session_token(token):
            # API 请求返回 401，页面请求重定向
            if path.startswith("/api/"):
                return JSONResponse(content={"error": "未登录"}, status_code=401)
            return RedirectResponse(url="/login", status_code=302)

        return await call_next(request)

    # ─────────────────────────────────────────────────────────────
    # 登录/登出 API
    # ─────────────────────────────────────────────────────────────

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        """登录页面"""
        # 已登录则跳转首页
        token = request.cookies.get("ftseek_session", "")
        if verify_session_token(token):
            return RedirectResponse(url="/", status_code=302)
        return templates.TemplateResponse("login.html", {"request": request})

    @app.post("/api/auth/login")
    async def auth_login(request: Request):
        """登录验证"""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(content={"success": False, "message": "请求无效"}, status_code=400)

        password = body.get("password", "")

        # 未设置密码时，警告但仍允许登录
        if not has_password_configured():
            token = create_session_token()
            response = JSONResponse(content={"success": True, "redirect": "/", "message": "未设置密码，请尽快在 .env.us 中配置 DASHBOARD_PASSWORD_MD5"})
            response.set_cookie("ftseek_session", token, max_age=86400, httponly=True, samesite="lax")
            return response

        if verify_password(password):
            token = create_session_token()
            response = JSONResponse(content={"success": True, "redirect": "/"})
            response.set_cookie("ftseek_session", token, max_age=86400, httponly=True, samesite="lax")
            logger.info("[Auth] 用户登录成功")
            return response
        else:
            logger.warning("[Auth] 登录失败: 密码错误")
            return JSONResponse(content={"success": False, "message": "密码错误"}, status_code=401)

    @app.get("/api/auth/check")
    async def auth_check(request: Request):
        """检查登录状态"""
        token = request.cookies.get("ftseek_session", "")
        return {"authenticated": verify_session_token(token)}

    @app.post("/api/auth/logout")
    async def auth_logout(request: Request):
        """登出"""
        response = JSONResponse(content={"success": True, "redirect": "/login"})
        response.delete_cookie("ftseek_session")
        return response

    @app.get("/logout")
    async def logout_redirect(request: Request):
        """登出并重定向到登录页"""
        response = RedirectResponse(url="/login", status_code=302)
        response.delete_cookie("ftseek_session")
        return response

    # ─────────────────────────────────────────────────────────────
    # 业务路由
    # ─────────────────────────────────────────────────────────────

    # 路由
    @app.get("/review", response_class=HTMLResponse)
    async def review_page(request: Request):
        """分析页面"""
        return templates.TemplateResponse("review.html", {
            "request": request,
            "title": "分析 - FTSeek US",
        })

    @app.get("/", response_class=HTMLResponse)
    async def dashboard_home(request: Request):
        """仪表盘首页"""
        stats = dashboard.get_stats()
        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "title": "FTSeek-US Dashboard",
            "stats": stats,
            "is_live": dashboard_is_live(),
            "current_time": datetime.now().isoformat(),
        })
    
    @app.get("/api/stats")
    async def get_stats():
        """获取统计信息"""
        return dashboard.get_stats()
    
    @app.get("/api/positions")
    async def get_positions():
        """获取持仓列表 — DB优先，实盘模式下DB空则回退到broker"""
        db_positions = dashboard.get_positions()
        if db_positions:
            return {"positions": db_positions, "source": "db"}
        
        # Fallback: live mode + empty DB → pull from broker
        try:
            svc = get_manual_order_service()
            if svc.connect():
                from futu import TrdEnv, RET_OK
                import os as _os
                is_live = svc._is_live()
                trd_env = TrdEnv.REAL if is_live else TrdEnv.SIMULATE
                acc_id = int(_os.getenv("REAL_ACCOUNT_ID" if is_live else "SIMULATE_ACCOUNT_ID", "0"))
                ret, data = svc._broker_ctx.position_list_query(trd_env=trd_env, acc_id=acc_id, refresh_cache=True)
                if ret == RET_OK and len(data) > 0:
                    positions = []
                    for _, row in data.iterrows():
                        qty = float(row.get("qty", 0))
                        if qty <= 0:
                            continue
                        side = str(row.get("position_side", ""))
                        positions.append({
                            "symbol": str(row.get("code", "")),
                            "direction": "LONG" if "LONG" in side else "SHORT",
                            "quantity": qty,
                            "entry_price": float(row.get("cost_price", 0)),
                            "current_price": float(row.get("cur_price", 0)),
                            "market_value": float(row.get("market_val", 0)),
                            "unrealized_pnl": float(row.get("unrealized_pl", 0)),
                            "owner": "auto",
                        })
                    if positions:
                        return {"positions": positions, "source": "broker"}
        except Exception as _e:
            import logging
            logging.getLogger("ftseek.dashboard").debug(f"[positions] broker fallback failed: {_e}")
        
        return {"positions": [], "source": "empty"}
    
    @app.get("/api/trades")
    async def get_trades(limit: int = 50):
        """获取交易记录"""
        return {"trades": dashboard.get_trades(limit)}
    
    @app.get("/api/account")
    async def get_account():
        """获取账户信息"""
        return dashboard.get_account_summary()
    
    @app.get("/health", response_class=HTMLResponse)
    async def health_page(request: Request):
        """健康监控页面 → 重定向到系统页"""
        return RedirectResponse(url="/system", status_code=301)
    
    @app.get("/api/metrics/system")
    async def get_system_metrics():
        """获取系统指标"""
        try:
            status = metrics_api.get_latest_system_status()
            return {
                "cpu_percent": status.get("cpu_percent", {}).get("value", 0),
                "memory_percent": status.get("memory_percent", {}).get("value", 0),
                "disk_percent": status.get("disk_percent", {}).get("value", 0),
            }
        except Exception as e:
            logger.error(f"获取系统指标失败：{e}")
            return {"cpu_percent": 0, "memory_percent": 0, "disk_percent": 0}
    
    @app.get("/api/metrics/trading")
    async def get_trading_metrics():
        """获取交易指标"""
        try:
            status = metrics_api.get_latest_trading_status()
            return {
                "signals_count": int(status.get("signals_count", {}).get("value", 0)),
                "orders_count": int(status.get("orders_count", {}).get("value", 0)),
                "fills_count": int(status.get("fills_count", {}).get("value", 0)),
                "pending_orders": int(status.get("pending_orders", {}).get("value", 0)),
            }
        except Exception as e:
            logger.error(f"获取交易指标失败：{e}")
            return {"signals_count": 0, "orders_count": 0, "fills_count": 0, "pending_orders": 0}
    
    @app.get("/api/metrics/api")
    async def get_api_metrics():
        """获取 API 调用指标"""
        try:
            # 从数据源指标中获取
            status = metrics_api.get_latest_system_status()
            # 默认返回模拟数据 (实际应从 metrics 表读取)
            return {
                "total_requests": int(status.get("opendata_total", {}).get("value", 0)),
                "success_requests": int(status.get("opendata_success", {}).get("value", 0)),
                "failed_requests": int(status.get("opendata_failed", {}).get("value", 0)),
                "avg_latency_ms": status.get("opendata_latency", {}).get("value", 0),
            }
        except Exception as e:
            logger.error(f"获取 API 指标失败：{e}")
            return {"total_requests": 0, "success_requests": 0, "failed_requests": 0, "avg_latency_ms": 0}
    
    @app.get("/api/metrics/alert")
    async def check_and_send_alerts():
        """检查指标并发送告警"""
        try:
            alerts_sent = []
            
            # 获取系统指标
            system_status = metrics_api.get_latest_system_status()
            cpu = system_status.get("cpu_percent", {}).get("value", 0)
            memory = system_status.get("memory_percent", {}).get("value", 0)
            
            # 检查 CPU
            if cpu > 90:
                if dingtalk_alerter:
                    dingtalk_alerter.send_alert(
                        level="CRITICAL",
                        title="⚠️ CPU 使用率告警",
                        content=f"CPU 使用率达到 {cpu:.1f}%，超过阈值 90%"
                    )
                alerts_sent.append("CPU_CRITICAL")
            elif cpu > 70:
                if dingtalk_alerter:
                    dingtalk_alerter.send_alert(
                        level="WARN",
                        title="⚡ CPU 使用率警告",
                        content=f"CPU 使用率达到 {cpu:.1f}%，超过阈值 70%"
                    )
                alerts_sent.append("CPU_WARN")
            
            # 检查内存
            if memory > 90:
                if dingtalk_alerter:
                    dingtalk_alerter.send_alert(
                        level="CRITICAL",
                        title="⚠️ 内存使用率告警",
                        content=f"内存使用率达到 {memory:.1f}%，超过阈值 90%"
                    )
                alerts_sent.append("MEMORY_CRITICAL")
            elif memory > 70:
                if dingtalk_alerter:
                    dingtalk_alerter.send_alert(
                        level="WARN",
                        title="⚡ 内存使用率警告",
                        content=f"内存使用率达到 {memory:.1f}%，超过阈值 70%"
                    )
                alerts_sent.append("MEMORY_WARN")
            
            return {
                "status": "checked",
                "alerts_sent": alerts_sent,
                "timestamp": datetime.now().isoformat(),
            }
        except Exception as e:
            logger.error(f"告警检查失败：{e}")
            return {"status": "error", "error": str(e)}
    
    @app.get("/api/health")
    async def health_check_api():
        """健康检查 API"""
        return {"status": "ok", "timestamp": datetime.now().isoformat()}

    # ─────────────────────────────────────────────────────────────
    # 手动下单 API (2026-06-09)
    # ─────────────────────────────────────────────────────────────

    from app.web_dashboard.manual_order_service import get_manual_order_service, ManualOrder

    @app.get("/manual-trade")
    async def manual_trade_redirect(request: Request):
        """重定向到合并后的交易页"""
        return RedirectResponse(url="/trade", status_code=301)

    @app.get("/trade", response_class=HTMLResponse)
    async def trade_page(request: Request):
        """交易页面 (合并下单+持仓+订单)"""
        return templates.TemplateResponse("trade.html", {
            "request": request,
            "title": "交易 - FTSeek-US",
            "is_live": dashboard_is_live(),
        })

    @app.get("/live-status")
    async def live_status_redirect(request: Request):
        """重定向到合并后的交易页"""
        return RedirectResponse(url="/trade", status_code=301)

    @app.post("/api/orders")
    async def place_manual_order(request: Request):
        """手动下单"""
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
        )

        result = svc.submit_order(order)
        return JSONResponse(content=result)

    @app.post("/api/orders/close")
    async def close_position(request: Request):
        """手动平仓 (支持部分平仓)"""
        body = await request.json()
        svc = get_manual_order_service()
        close_qty = int(body.get("quantity", 0))  # 0=全平
        result = svc.close_position(
            symbol=body.get("symbol", ""),
            reason=body.get("reason", "手动平仓"),
            close_qty=close_qty,
        )
        return JSONResponse(content=result)

    @app.get("/api/orders/today")
    async def get_today_orders():
        """查询今日所有订单状态"""
        svc = get_manual_order_service()
        orders = svc.get_today_orders()
        return {"orders": orders}

    @app.post("/api/orders/cancel")
    async def cancel_order(request: Request):
        """撤销指定订单"""
        body = await request.json()
        svc = get_manual_order_service()
        result = svc.cancel_order(order_id=body.get("order_id", ""))
        status_code = 200 if result.get("success") else 400
        return JSONResponse(content=result, status_code=status_code)

    @app.get("/api/orders/audit")
    async def get_order_audit(limit: int = 50):
        """获取手动下单审计历史"""
        svc = get_manual_order_service()
        history = svc.get_audit_history(limit)
        return {"orders": history}

    @app.get("/api/quote/{symbol}")
    async def get_quote(symbol: str):
        """获取实时报价"""
        svc = get_manual_order_service()
        return svc.get_quote(symbol.upper())

    @app.get("/api/order-capacity/{symbol}")
    async def get_order_capacity(symbol: str):
        """获取标的的可买卖数量"""
        svc = get_manual_order_service()
        if not svc.connect():
            return JSONResponse(content={"error": "broker_not_connected"}, status_code=503)

        try:
            from futu import TrdEnv, RET_OK
            symbol = symbol.upper()

            # 1. 获取报价
            quote = svc.get_quote(symbol)
            cur_price = quote.get("cur_price", 0)
            if cur_price <= 0:
                return {"symbol": symbol, "error": "无法获取报价", "cur_price": 0}

            # 2. 获取账户信息
            trd_env = TrdEnv.REAL if svc._is_live() else TrdEnv.SIMULATE
            acc_id = int(svc._read_env(
                "REAL_ACCOUNT_ID" if svc._is_live() else "SIMULATE_ACCOUNT_ID", "0"
            ))
            ret, funds = svc._broker_ctx.accinfo_query(trd_env=trd_env, acc_id=acc_id)
            if ret != RET_OK:
                return {"symbol": symbol, "cur_price": cur_price, "error": "账户查询失败"}

            row = funds.iloc[0]
            def _f(v):
                try:
                    return float(v) if v is not None and v != "N/A" else 0.0
                except (ValueError, TypeError):
                    return 0.0

            currency = str(row.get("currency", "")).upper()
            is_usd_account = currency in ("USD", "")

            if is_usd_account:
                # USD 账户 (模拟盘通常是)
                cash = _f(row.get("cash", 0))
                power = _f(row.get("power", 0))
                max_short_power = _f(row.get("max_power_short", 0))
            else:
                # HKD 账户 — 美股交易用 USD 字段
                cash = _f(row.get("us_cash", 0))
                power = _f(row.get("usd_net_cash_power", 0))
                max_short_power = _f(row.get("max_power_short", 0)) / 7.8  # HKD→USD 估算
                if power <= 0:
                    # fallback: 用 HKD power 换算
                    power = _f(row.get("power", 0)) / 7.8
                if max_short_power <= 0:
                    max_short_power = power  # 做空购买力 ≈ 做多购买力

            # 标记货币单位
            display_currency = "USD"

            # 3. 获取当前持仓
            ret2, positions = svc._broker_ctx.position_list_query(trd_env=trd_env, acc_id=acc_id)
            held_qty = 0
            if ret2 == RET_OK and not positions.empty:
                pos = positions[positions["code"] == symbol]
                if not pos.empty:
                    raw_qty = float(pos.iloc[0].get("qty", 0))
                    held_qty = int(raw_qty)  # 正=LONG, 负=SHORT

            # 4. 计算可买卖数量
            cash_can_buy = int(cash / cur_price) if cur_price > 0 else 0
            max_can_buy = int(power / cur_price) if cur_price > 0 else 0
            can_sell = max(0, held_qty)  # 持有正数才能卖
            can_short = int(max_short_power / cur_price) if cur_price > 0 else 0
            can_cover = max(0, -held_qty)  # 持有负数(做空)才能回补买入

            return {
                "symbol": symbol,
                "cur_price": cur_price,
                "currency": display_currency,
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

    @app.get("/api/live-account")
    async def get_live_account():
        """获取实盘账户状态"""
        svc = get_manual_order_service()
        if not svc.connect():
            return JSONResponse(content={"error": "broker_not_connected"}, status_code=503)

        try:
            from futu import TrdEnv, RET_OK
            import os as _os
            trd_env = TrdEnv.REAL if svc._is_live() else TrdEnv.SIMULATE
            if svc._is_live():
                acc_id = int(_os.getenv("REAL_ACCOUNT_ID", "0"))
            else:
                acc_id = int(_os.getenv("SIMULATE_ACCOUNT_ID", "0"))

            ret, funds = svc._broker_ctx.accinfo_query(trd_env=trd_env, acc_id=acc_id)
            account_info = {}
            if ret == RET_OK:
                row = funds.iloc[0]
                def _af(v):
                    try:
                        return float(v) if v is not None and v != "N/A" else 0.0
                    except (ValueError, TypeError):
                        return 0.0

                raw_cur = str(row.get("currency", "")).upper()
                is_hkd = raw_cur == "HKD"
                fx = 7.8 if is_hkd else 1.0

                # 统一 USD
                account_info = {
                    "cash": round(_af(row.get("us_cash")) / 1 if is_hkd else _af(row.get("cash")), 2),
                    "total_assets": round(_af(row.get("usd_assets")) if is_hkd else _af(row.get("total_assets")), 2),
                    "power": round(_af(row.get("usd_net_cash_power")) if is_hkd and _af(row.get("usd_net_cash_power")) > 0 else _af(row.get("power")) / fx, 2),
                    "market_val": round(_af(row.get("market_val")) / fx, 2),
                    "currency": "USD",
                }

            ret2, positions = svc._broker_ctx.position_list_query(trd_env=trd_env, acc_id=acc_id)
            pos_list = []
            if ret2 == RET_OK and not positions.empty:
                def _safe_float(v):
                    try:
                        if v is None or v == "N/A":
                            return 0.0
                        return float(v)
                    except (ValueError, TypeError):
                        return 0.0

                # 批量获取报价 (盘后 cur_price 为 0，需要补充)
                codes = [p.get("code", "") for _, p in positions.iterrows() if p.get("code")]
                price_map = {}
                if codes:
                    try:
                        ret_q, quotes = svc._quote_ctx.get_market_snapshot(codes)
                        if ret_q == RET_OK and not quotes.empty:
                            for _, q in quotes.iterrows():
                                code = q.get("code", "")
                                price = _safe_float(q.get("cur_price", 0))
                                if price <= 0:
                                    price = _safe_float(q.get("last_price", 0))
                                if price <= 0:
                                    price = _safe_float(q.get("prev_close_price", 0))
                                price_map[code] = price
                    except Exception:
                        pass

                for _, p in positions.iterrows():
                    code = p.get("code", "")
                    qty = _safe_float(p.get("qty", 0))
                    cost = _safe_float(p.get("cost_price", 0))
                    cur = _safe_float(p.get("cur_price", 0))

                    # 盘后补充价格
                    if cur <= 0 and code in price_map:
                        cur = price_map[code]

                    # 手动计算盈亏 (Futu 盘后返回 0)
                    unrealized = _safe_float(p.get("unrealized_pl", 0))
                    if unrealized == 0 and cur > 0 and cost > 0:
                        if qty > 0:  # LONG
                            unrealized = (cur - cost) * abs(qty)
                        else:  # SHORT
                            unrealized = (cost - cur) * abs(qty)

                    pos_list.append({
                        "code": code,
                        "qty": qty,
                        "cost_price": cost,
                        "cur_price": cur,
                        "market_val": _safe_float(p.get("market_val", 0)) or (cur * abs(qty)),
                        "unrealized_pl": round(unrealized, 2),
                    })

            # 从 open_positions 补充 owner 字段
            try:
                owner_conn = sqlite3.connect(svc._get_db_path())
                owner_rows = owner_conn.execute(
                    "SELECT symbol, COALESCE(owner,'auto') FROM open_positions"
                ).fetchall()
                owner_conn.close()
                owner_map = {row[0]: row[1] for row in owner_rows}
                for pos in pos_list:
                    pos["owner"] = owner_map.get(pos["code"], "auto")
            except Exception:
                for pos in pos_list:
                    pos.setdefault("owner", "auto")

            return {
                "mode": svc._get_mode_label(),
                "is_live": svc._is_live(),
                "account": account_info,
                "positions": pos_list,
                "timestamp": datetime.now().isoformat(),
            }
        except Exception as e:
            return JSONResponse(content={"error": str(e)}, status_code=500)

    # ─────────────────────────────────────────────────────────────
    # 模式切换 API (2026-06-09)
    # ─────────────────────────────────────────────────────────────

    from app.web_dashboard.mode_switch_service import (
        get_current_mode, switch_mode as do_switch_mode,
    )

    @app.get("/mode-switch", response_class=HTMLResponse)
    async def mode_switch_page(request: Request):
        """模式切换页面 → 重定向到系统页"""
        return RedirectResponse(url="/system", status_code=301)

    @app.get("/system", response_class=HTMLResponse)
    async def system_page(request: Request):
        """统一系统页面（健康监控 + 模式切换 + 日志）"""
        return templates.TemplateResponse("system.html", {
            "request": request,
            "title": "系统 - FTSeek-US",
        })

    @app.get("/api/system/logs")
    async def get_system_logs():
        """读取最近 20 行日志"""
        import subprocess as _sp
        log_path = base_dir.parent.parent / "logs" / "ftseek_us.log"
        try:
            if log_path.exists():
                proc = _sp.run(
                    ["tail", "-n", "20", str(log_path)],
                    capture_output=True, text=True, timeout=5,
                )
                lines = proc.stdout.strip().split("\n") if proc.stdout.strip() else []
                return {"lines": lines, "count": len(lines)}
            else:
                return {"lines": [], "count": 0}
        except Exception as e:
            logger.error(f"读取日志失败: {e}")
            return {"lines": [], "count": 0, "error": str(e)}

    @app.get("/api/system/status")
    async def get_system_status():
        """获取系统服务状态"""
        import subprocess as _sp
        services = {}

        for svc in ["ftseek-us", "ftseek-us-dashboard"]:
            try:
                proc = _sp.run(
                    ["systemctl", "is-active", svc],
                    capture_output=True, text=True, timeout=5,
                )
                key = "dashboard" if svc == "ftseek-us-dashboard" else "ftseek_us"
                services[key] = proc.stdout.strip()
            except Exception:
                key = "dashboard" if svc == "ftseek-us-dashboard" else "ftseek_us"
                services[key] = "unknown"

        # Broker connection check
        broker_connected = False
        account_id = ""
        try:
            svc = get_manual_order_service()
            broker_connected = svc.connect()
            if broker_connected:
                import os as _os
                is_live_env = dashboard_is_live()
                account_id = _os.getenv(
                    "REAL_ACCOUNT_ID" if is_live_env else "SIMULATE_ACCOUNT_ID", ""
                )
        except Exception:
            pass

        return {
            "services": services,
            "broker_connected": broker_connected,
            "account_id": account_id,
        }

    @app.get("/api/mode")
    async def get_mode():
        """获取当前模式"""
        return get_current_mode()

    @app.post("/api/mode/switch")
    async def switch_mode_api(request: Request):
        """切换模式"""
        try:
            body = await request.json()
        except Exception as e:
            logger.error(f"[模式切换] JSON 解析失败: {e}")
            return JSONResponse(content={"success": False, "message": f"请求体无效: {e}"}, status_code=400)

        target = body.get("target_mode", "") if isinstance(body, dict) else ""
        confirm = body.get("confirm", "") if isinstance(body, dict) else ""
        logger.info(f"[模式切换] 收到请求: target={target}, confirm={'***' if confirm else '(空)'}")
        result = do_switch_mode(target, confirm)
        status_code = 200 if result["success"] else 400
        return JSONResponse(content=result, status_code=status_code)

    @app.post("/api/mode/restart")
    async def restart_service():
        """重启 ftseek-us 服务 (需要 systemd)"""
        import subprocess
        try:
            proc = subprocess.run(
                ["systemctl", "restart", "ftseek-us-dashboard"],
                capture_output=True, text=True, timeout=15,
            )
            if proc.returncode == 0:
                return {"success": True, "message": "服务重启中，页面将在几秒后恢复"}
            # 尝试用 fuser + 重启
            return {"success": False, "message": f"systemctl 失败: {proc.stderr}。请手动重启 Dashboard 进程。"}
        except Exception as e:
            return {"success": False, "message": f"重启失败: {e}。请手动重启 Dashboard。"}

    # ─────────────────────────────────────────────────────────────
    # 风控 API (2026-06-10)
    # ─────────────────────────────────────────────────────────────

    @app.get("/risk")
    async def risk_page():
        """风控页面已合并到指挥中心"""
        return RedirectResponse(url="/", status_code=301)

    @app.get("/api/risk/status")
    async def risk_status():
        """风控状态 — 真实数据驱动"""
        import sqlite3 as _sqlite3

        svc = get_manual_order_service()
        is_live = svc._is_live()
        mode = svc._get_mode_label()

        # ── 1. Futu 账户真实数据 (统一 USD 显示) ──
        account = {}
        try:
            from futu import TrdEnv, RET_OK
            if not svc.connect():
                raise Exception("broker_not_connected")
            trd_env = TrdEnv.REAL if is_live else TrdEnv.SIMULATE
            acc_id = int(svc._read_env(
                "REAL_ACCOUNT_ID" if is_live else "SIMULATE_ACCOUNT_ID", "0"
            ))
            ret, funds = svc._broker_ctx.accinfo_query(trd_env=trd_env, acc_id=acc_id)
            if ret == RET_OK:
                row = funds.iloc[0]
                def _f(v):
                    try:
                        return float(v) if v is not None and v != "N/A" else 0.0
                    except (ValueError, TypeError):
                        return 0.0

                currency = str(row.get("currency", "")).upper()
                is_hkd = currency == "HKD"
                fx = 7.8 if is_hkd else 1.0  # HKD→USD 汇率

                # USD 字段 (HKD 账户用 us_* 字段, USD 账户直接用)
                usd_assets = _f(row.get("usd_assets")) if is_hkd else _f(row.get("total_assets"))
                usd_cash = _f(row.get("us_cash")) if is_hkd else _f(row.get("cash"))
                usd_power = _f(row.get("usd_net_cash_power")) if is_hkd else _f(row.get("power"))
                if usd_power <= 0 and is_hkd:
                    usd_power = _f(row.get("power")) / fx

                # long_mv / short_mv: Futu 返回的是各币种混合, HKD 账户需换算
                long_mv = _f(row.get("long_mv"))
                short_mv = abs(_f(row.get("short_mv")))
                if is_hkd and long_mv > 0:
                    # 用 USD assets 估算 USD 市值
                    usd_securities = _f(row.get("securities_assets")) if not is_hkd else usd_assets
                    if usd_securities > 0:
                        long_mv = long_mv / fx  # 粗略 HKD→USD
                        short_mv = short_mv / fx

                account = {
                    "total_assets": round(usd_assets, 2),
                    "cash": round(usd_cash, 2),
                    "market_val": round(long_mv + short_mv, 2),
                    "power": round(usd_power, 2),
                    "long_mv": round(long_mv, 2),
                    "short_mv": round(short_mv, 2),
                    "initial_margin": round(_f(row.get("initial_margin")) / fx, 2),
                    "maintenance_margin": round(_f(row.get("maintenance_margin")) / fx, 2),
                    "margin_call_margin": round(_f(row.get("margin_call_margin")) / fx, 2),
                    "frozen_cash": round(_f(row.get("frozen_cash")) / fx, 2),
                    "risk_status": str(row.get("risk_status", "N/A")),
                    "interest_charged": round(_f(row.get("interest_charged_amount")) / fx, 2),
                    "currency": "USD",
                    "raw_currency": currency,
                }
        except Exception as e:
            logger.error(f"[Risk] Futu accinfo failed: {e}")

        # ── 2. 保证金使用率 ──
        margin = {}
        if account:
            total = account.get("total_assets", 0)
            init_margin = account.get("initial_margin", 0)
            maint_margin = account.get("maintenance_margin", 0)
            power = account.get("power", 0)
            market_val = account.get("market_val", 0)

            if total > 0 and init_margin > 0:
                margin["usage_pct"] = round(init_margin / total * 100, 1)
            elif total > 0 and market_val > 0:
                margin["usage_pct"] = round(market_val / total * 100, 1)
            else:
                margin["usage_pct"] = 0

            margin["available_power"] = power
            margin["initial_margin"] = init_margin
            margin["maintenance_margin"] = maint_margin
            margin["margin_call"] = account.get("margin_call_margin", 0)
            margin["frozen_cash"] = account.get("frozen_cash", 0)

        # ── 3. 净敞口 ──
        exposure = {}
        if account:
            long_mv = account.get("long_mv", 0)
            short_mv = abs(account.get("short_mv", 0))
            net = long_mv - short_mv
            gross = long_mv + short_mv
            exposure["long_mv"] = long_mv
            exposure["short_mv"] = short_mv
            exposure["net"] = net
            exposure["gross"] = gross
            exposure["net_ratio"] = round(net / gross * 100, 1) if gross > 0 else 0

        # ── 4. 真实 PnL (Futu 账户级 + DB 策略级) ──
        pnl = {
            "realized": 0,          # DB 已实现 (策略级)
            "unrealized": 0,        # Futu 持仓浮盈 (账户级, snapshot 价格)
            "total": 0,
            "today_trades": 0,      # DB 策略交易数
            "futu_today_trades": 0, # Futu 实际成交数
        }
        today = _et_today()

        # 4a. DB 策略级已实现盈亏 — 使用最新交易日而非日历日期
        #     日历日期可能尚无交易记录 (非交易日、盘前等场景)
        try:
            from app.core.db_utils import get_main_db
            conn = _sqlite3.connect(str(get_main_db()))
            row = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl),0), COUNT(*) "
                "FROM trade_records "
                "WHERE trade_date = (SELECT MAX(trade_date) FROM trade_records)"
            ).fetchone()
            pnl["realized"] = float(row[0]) if row else 0
            pnl["today_trades"] = int(row[1]) if row else 0
            conn.close()
        except Exception:
            pass

        # 4b. Futu 实际成交数
        try:
            from futu import TrdEnv as _TE, RET_OK as _ROK
            _trd_env = _TE.REAL if is_live else _TE.SIMULATE
            _acc = int(svc._read_env("REAL_ACCOUNT_ID" if is_live else "SIMULATE_ACCOUNT_ID", "0"))
            _ret, _orders = svc._broker_ctx.order_list_query(trd_env=_trd_env, acc_id=_acc)
            if _ret == _ROK and not _orders.empty:
                futu_today = _orders[_orders["create_time"].astype(str).str.startswith(today)]
                filled = futu_today[futu_today["order_status"].astype(str).str.contains("FILLED")]
                pnl["futu_today_trades"] = len(filled)
        except Exception:
            pass

        # 4c. 持仓浮盈: 统一用 Futu 持仓 + snapshot 价格 (两个模式都适用)
        try:
            from futu import TrdEnv as _TE2, RET_OK as _ROK2
            _trd_env2 = _TE2.REAL if is_live else _TE2.SIMULATE
            _acc2 = int(svc._read_env("REAL_ACCOUNT_ID" if is_live else "SIMULATE_ACCOUNT_ID", "0"))

            _ret2, _pos = svc._broker_ctx.position_list_query(trd_env=_trd_env2, acc_id=_acc2)
            if _ret2 == _ROK2 and not _pos.empty:
                # 获取所有持仓代码的实时报价
                _codes = [str(r.get("code", "")) for _, r in _pos.iterrows() if abs(float(r.get("qty", 0))) > 0]
                _price_map = svc._batch_quotes(_codes) if _codes else {}

                total_unreal = 0
                positions = []
                for _, _p in _pos.iterrows():
                    _qty = float(_p.get("qty", 0))
                    if _qty == 0:
                        continue
                    _sym = str(_p.get("code", ""))
                    _cost = float(_p.get("cost_price", 0))
                    _cur = _price_map.get(_sym, 0)
                    _is_long = _qty > 0

                    if _cur > 0 and _cost > 0:
                        if _is_long:
                            _unreal = (_cur - _cost) * abs(_qty)
                        else:
                            _unreal = (_cost - _cur) * abs(_qty)
                    else:
                        _unreal = 0

                    total_unreal += _unreal
                    positions.append({
                        "symbol": _sym,
                        "direction": "LONG" if _is_long else "SHORT",
                        "quantity": abs(_qty),
                        "avg_entry_price": _cost,
                        "current_price": _cur,
                        "unrealized_pnl": round(_unreal, 2),
                        "unrealized_pnl_percent": round((_cur - _cost) / _cost * 100, 2) if _cost > 0 and _is_long else round((_cost - _cur) / _cost * 100, 2) if _cost > 0 else 0,
                    })

                pnl["unrealized"] = round(total_unreal, 2)
        except Exception as e:
            logger.error(f"[Risk] Futu unrealized PnL calc failed: {e}")
            # Fallback to DB positions for simulate mode
            if not is_live:
                try:
                    positions = dashboard.get_positions()
                    pnl["unrealized"] = round(sum(p.get("unrealized_pnl", 0) for p in positions), 2)
                except Exception:
                    pass

        pnl["total"] = round(pnl["realized"] + pnl["unrealized"], 2)

        # ── 5. 止损距离 (实盘直接从 Futu 拉, 不依赖 DB 同步) ──
        stop_distances = []
        try:
            if is_live:
                # 实盘: 直接从 Futu API 获取持仓 + 报价
                from futu import TrdEnv as _TrdEnv, RET_OK as _RET_OK
                _trd_env = _TrdEnv.REAL
                _acc_id = int(svc._read_env("REAL_ACCOUNT_ID", "0"))

                _ret, _pos_df = svc._broker_ctx.position_list_query(
                    trd_env=_trd_env, acc_id=_acc_id,
                )
                if _ret == _RET_OK and not _pos_df.empty:
                    # 批量获取报价
                    _codes = [str(r.get("code", "")) for _, r in _pos_df.iterrows() if abs(float(r.get("qty", 0))) > 0]
                    _price_map = svc._batch_quotes(_codes) if _codes else {}

                    for _, _row in _pos_df.iterrows():
                        _qty = float(_row.get("qty", 0))
                        if _qty == 0:
                            continue
                        _sym = str(_row.get("code", ""))
                        _cost = float(_row.get("cost_price", 0))
                        _cur = _price_map.get(_sym, 0)
                        _direction = "LONG" if _qty > 0 else "SHORT"
                        _size = abs(_qty)

                        # 实盘没有 DB 中的 SL/TP，用 ATR 估算
                        _atr_est = _cur * 0.02 if _cur > 0 else 0  # ~2% ATR 估算
                        _sl = _cost - _atr_est * 2 if _direction == "LONG" else _cost + _atr_est * 2
                        _tp = _cost + _atr_est * 4 if _direction == "LONG" else _cost - _atr_est * 4

                        if _cur <= 0:
                            continue

                        if _direction == "LONG":
                            _sl_dist = (_cur - _sl) / _cur * 100
                            _tp_dist = (_tp - _cur) / _cur * 100
                            _upnl = (_cur - _cost) * _size
                        else:
                            _sl_dist = (_sl - _cur) / _cur * 100
                            _tp_dist = (_cur - _tp) / _cur * 100
                            _upnl = (_cost - _cur) * _size

                        stop_distances.append({
                            "symbol": _sym,
                            "direction": _direction,
                            "size": _size,
                            "entry": _cost,
                            "current": _cur,
                            "stop_loss": round(_sl, 2),
                            "take_profit": round(_tp, 2),
                            "sl_distance_pct": round(_sl_dist, 2),
                            "tp_distance_pct": round(_tp_dist, 2),
                            "unrealized_pnl": round(_upnl, 2),
                            "danger": _sl_dist < 2,
                        })
            else:
                # 模拟盘: 从 DB 读取 (含 SL/TP 数据)
                from app.core.db_utils import get_main_db
                conn = _sqlite3.connect(str(get_main_db()))
                conn.row_factory = _sqlite3.Row
                rows = conn.execute("""
                    SELECT symbol, direction, size, avg_entry_price, current_sl, initial_tp
                    FROM open_positions
                    WHERE sync_status IS NULL OR sync_status NOT IN (
                        'closed_by_sync','closed_by_scheduler','closed_by_daemon'
                    )
                    ORDER BY size DESC
                """).fetchall()
                conn.close()

                symbols = [r["symbol"] for r in rows if r["symbol"]]
                price_map = svc._batch_quotes(symbols) if symbols else {}
                pos_price_map = {p["symbol"]: p.get("current_price", 0) for p in positions}
                price_map = price_map or pos_price_map

                for r in rows:
                    sym = r["symbol"]
                    cur = price_map.get(sym, 0)
                    sl = float(r["current_sl"] or 0)
                    tp = float(r["initial_tp"] or 0)
                    entry = float(r["avg_entry_price"] or 0)
                    direction = r["direction"]

                    if cur <= 0 or sl <= 0:
                        continue

                    if direction == "LONG":
                        sl_dist_pct = (cur - sl) / cur * 100
                        tp_dist_pct = (tp - cur) / cur * 100 if tp > 0 else 0
                    else:
                        sl_dist_pct = (sl - cur) / cur * 100
                        tp_dist_pct = (cur - tp) / cur * 100 if tp > 0 else 0

                    size = float(r["size"] or 0)
                    upnl = (cur - entry) * size if direction == "LONG" else (entry - cur) * size

                    stop_distances.append({
                        "symbol": sym,
                        "direction": direction,
                        "size": size,
                        "entry": entry,
                        "current": cur,
                        "stop_loss": sl,
                        "take_profit": tp,
                        "sl_distance_pct": round(sl_dist_pct, 2),
                        "tp_distance_pct": round(tp_dist_pct, 2),
                        "unrealized_pnl": round(upnl, 2),
                        "danger": sl_dist_pct < 2,
                    })

            stop_distances.sort(key=lambda x: (not x["danger"], x["sl_distance_pct"]))

            # 实盘模式: 用 Futu 持仓的浮盈更新 PnL
            if is_live and stop_distances:
                pnl["unrealized"] = round(sum(s["unrealized_pnl"] for s in stop_distances), 2)
                pnl["total"] = round(pnl["realized"] + pnl["unrealized"], 2)
        except Exception as e:
            logger.error(f"[Risk] Stop distance calc failed: {e}")

        # ── 6. Futu 风控状态 ──
        futu_risk = {
            "risk_level": account.get("risk_status", "N/A"),
            "interest_charged": account.get("interest_charged", 0),
        }
        # Interpret risk level
        risk_levels = {
            "LEVEL1": {"label": "安全", "color": "green", "desc": "账户资金充足"},
            "LEVEL2": {"label": "注意", "color": "yellow", "desc": "保证金使用较高"},
            "LEVEL3": {"label": "警告", "color": "orange", "desc": "接近维持保证金"},
            "LEVEL4": {"label": "危险", "color": "red", "desc": "低于维持保证金"},
            "LEVEL5": {"label": "强平", "color": "red", "desc": "面临强制平仓"},
        }
        rl = futu_risk["risk_level"]
        futu_risk["detail"] = risk_levels.get(rl, {"label": rl, "color": "gray", "desc": "未知状态"})

        return {
            "mode": mode,
            "is_live": is_live,
            "account": account,
            "margin": margin,
            "exposure": exposure,
            "pnl": pnl,
            "stop_distances": stop_distances,
            "futu_risk": futu_risk,
        }

    @app.get("/api/strategy/status")
    async def strategy_status():
        """策略运行状态"""
        import sqlite3 as _sqlite3
        import subprocess as _subprocess

        today = _et_today()
        result = {"strategies": [], "orb": {}, "signals": {}, "service": {}}

        # 1. 服务状态
        try:
            proc = _subprocess.run(
                ["systemctl", "is-active", "ftseek-us"],
                capture_output=True, text=True, timeout=5,
            )
            result["service"]["ftseek_us"] = proc.stdout.strip()
        except Exception:
            result["service"]["ftseek_us"] = "unknown"

        # 2. 按策略统计最新交易日交易 (使用 DB 最新 trade_date 而非日历日期)
        try:
            from app.core.db_utils import get_main_db
            conn = _sqlite3.connect(str(get_main_db()))
            rows = conn.execute("""
                SELECT
                    COALESCE(strategy_id, 'unknown') as strategy,
                    COUNT(*) as trades,
                    SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                    ROUND(SUM(realized_pnl), 2) as pnl,
                    ROUND(AVG(realized_pnl), 2) as avg_pnl
                FROM trade_records
                WHERE trade_date = (SELECT MAX(trade_date) FROM trade_records)
                GROUP BY strategy_id
                ORDER BY pnl DESC
            """).fetchall()
            conn.close()

            for r in rows:
                strat = r[0] or "unknown"
                trades = r[1]
                wins = r[2] or 0
                wr = round(wins / trades * 100, 1) if trades > 0 else 0
                result["strategies"].append({
                    "name": strat,
                    "trades": trades,
                    "wins": wins,
                    "win_rate": wr,
                    "pnl": float(r[3] or 0),
                    "avg_pnl": float(r[4] or 0),
                })
        except Exception as e:
            logger.error(f"[Strategy] Trade stats failed: {e}")

        # 3. 最近7天策略表现
        try:
            from app.core.db_utils import get_main_db
            conn = _sqlite3.connect(str(get_main_db()))
            rows = conn.execute("""
                SELECT
                    COALESCE(strategy_id, 'unknown') as strategy,
                    COUNT(*) as trades,
                    SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                    ROUND(SUM(realized_pnl), 2) as pnl
                FROM trade_records
                WHERE trade_date >= ?
                GROUP BY strategy_id
                ORDER BY pnl DESC
            """, (_et_days_ago(7),)).fetchall()
            conn.close()
            result["weekly"] = [{
                "name": r[0] or "unknown",
                "trades": r[1],
                "wins": r[2] or 0,
                "win_rate": round((r[2] or 0) / r[1] * 100, 1) if r[1] > 0 else 0,
                "pnl": float(r[3] or 0),
            } for r in rows]
        except Exception:
            result["weekly"] = []

        # 4. 今日信号统计
        try:
            from app.core.db_utils import get_main_db
            conn = _sqlite3.connect(str(get_main_db()))
            rows = conn.execute("""
                SELECT COUNT(*) as total,
                    SUM(CASE WHEN status = 'ACCEPTED' THEN 1 ELSE 0 END) as accepted,
                    SUM(CASE WHEN status = 'REJECTED' THEN 1 ELSE 0 END) as rejected
                FROM signal_outcomes
                WHERE date(timestamp) = ?
            """, (today,)).fetchone()
            if rows:
                result["signals"] = {
                    "total": rows[0] or 0,
                    "accepted": rows[1] or 0,
                    "rejected": rows[2] or 0,
                }
            conn.close()
        except Exception:
            pass

        # 5. ORB 状态 (从日志读取最近活动)
        try:
            proc = _subprocess.run(
                ["grep", "-c", "[ORB].*tick", "logs/ftseek_us.log"],
                capture_output=True, text=True, timeout=5,
            )
            orb_ticks = int(proc.stdout.strip() or "0")

            proc2 = _subprocess.run(
                ["grep", "[ORB].*滚动区间构建完成", "logs/ftseek_us.log"],
                capture_output=True, text=True, timeout=5,
            )
            last_build = ""
            if proc2.stdout.strip():
                last_line = proc2.stdout.strip().split("\n")[-1]
                last_build = last_line[:23] if len(last_line) > 23 else ""

            proc3 = _subprocess.run(
                ["grep", "[ORB].*trade\\|order\\|开仓", "logs/ftseek_us.log"],
                capture_output=True, text=True, timeout=5,
            )
            orb_trades = len([l for l in proc3.stdout.strip().split("\n") if l.strip()]) if proc3.stdout.strip() else 0

            result["orb"] = {
                "status": "active" if orb_ticks > 0 else "inactive",
                "ticks_today": orb_ticks,
                "last_range_build": last_build,
                "trades_today": orb_trades,
            }
        except Exception:
            result["orb"] = {"status": "unknown"}

        # 6. 最近信号明细 (最近10条)
        try:
            from app.core.db_utils import get_main_db
            conn = _sqlite3.connect(str(get_main_db()))
            conn.row_factory = _sqlite3.Row
            rows = conn.execute("""
                SELECT symbol, direction, confidence, strength, status,
                       substr(timestamp, 12, 8) as time
                FROM signal_outcomes
                WHERE date(timestamp) = ?
                ORDER BY timestamp DESC
                LIMIT 10
            """, (today,)).fetchall()
            conn.close()
            result["recent_signals"] = [{
                "symbol": r["symbol"],
                "direction": r["direction"],
                "confidence": round(float(r["confidence"] or 0), 3),
                "strength": int(r["strength"] or 0),
                "status": r["status"],
                "time": r["time"],
            } for r in rows]
        except Exception:
            result["recent_signals"] = []

        return result

    @app.get("/api/risk/pnl-history")
    async def risk_pnl_history(days: int = 30):
        """获取近N日盈亏历史"""
        import sqlite3 as _sqlite3

        history = []
        try:
            from app.core.db_utils import get_main_db
            conn = _sqlite3.connect(str(get_main_db()))
            start_date = _et_days_ago(days)
            rows = conn.execute("""
                SELECT trade_date,
                       COALESCE(SUM(realized_pnl), 0) as daily_pnl,
                       COUNT(*) as trades,
                       SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins
                FROM trade_records
                WHERE trade_date >= ?
                GROUP BY trade_date
                ORDER BY trade_date
            """, (start_date,)).fetchall()

            cumulative = 100000  # starting capital estimate
            for row in rows:
                daily_pnl = float(row[1] or 0)
                cumulative += daily_pnl
                trades = int(row[2] or 0)
                wins = int(row[3] or 0)
                history.append({
                    "date": row[0],
                    "daily_pnl": round(daily_pnl, 2),
                    "total_assets": round(cumulative, 2),
                    "trades": trades,
                    "win_rate": round(wins / trades * 100, 1) if trades > 0 else 0,
                })
            conn.close()
        except Exception as e:
            logger.error(f"[Risk] PnL history query failed: {e}")

        return {"equity_history": history}

    logger.info("简化版 Dashboard Web 服务已创建")

    return app
