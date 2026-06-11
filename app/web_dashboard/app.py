"""
FTSeek-US Dashboard — 精简 App Factory (2026-06-11 重构)

原 2433 行单体 app.py 拆分为:
  - data_service.py: 统一数据层 (Futu RPC + DB + TTL cache)
  - routes/dashboard.py: 首页 + /api/dashboard/snapshot
  - routes/trade.py: 手动下单 + 订单管理
  - routes/risk.py: 风控状态 + PnL 历史
  - routes/analytics.py: 6 模块分析 (equity, signals, execution, etc.)
  - routes/stock_detail.py: 个股详情 + K线 + LLM 分析
  - routes/system.py: 系统状态 + 模式切换
"""
import logging
import time
from collections import defaultdict
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger("ftseek_us.dashboard")

# ── 登录频率限制 (C2) ──
_login_attempts: dict = defaultdict(list)  # ip -> [timestamps]
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 300  # 5 分钟


def _is_rate_limited(ip: str) -> bool:
    now = time.time()
    attempts = _login_attempts[ip]
    # 清除过期记录
    _login_attempts[ip] = [t for t in attempts if now - t < _LOGIN_WINDOW_SECONDS]
    return len(_login_attempts[ip]) >= _LOGIN_MAX_ATTEMPTS


def _record_attempt(ip: str):
    _login_attempts[ip].append(time.time())


def create_app() -> FastAPI:
    app = FastAPI(
        title="FTSeek-US Dashboard",
        description="美股交易 Dashboard",
        version="2.0.0",
    )

    # ── 路径 ──
    base_dir = Path(__file__).parent

    # ── 静态文件 (Chart.js 等本地库) ──
    from fastapi.staticfiles import StaticFiles
    static_dir = base_dir / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ── 模板 ──
    templates_dir = base_dir / "templates"
    templates_dir.mkdir(exist_ok=True)
    templates = Jinja2Templates(directory=str(templates_dir))

    # ── SimpleDashboard (stats 计算) ──
    from app.simple_dashboard import SimpleDashboard
    dashboard = SimpleDashboard()

    # ── 认证中间件 ──
    from app.web_dashboard.auth import (
        is_public_path, verify_password, create_session_token,
        verify_session_token, has_password_configured,
    )

    # ── 请求体大小限制 (M8): 1MB ──
    MAX_BODY_SIZE = 1 * 1024 * 1024

    @app.middleware("http")
    async def security_and_auth_middleware(request: Request, call_next):
        path = request.url.path

        # ── 安全响应头 (H1) ──
        # 先执行后续中间件，再添加 headers
        # (在 call_next 之后添加)

        # ── 请求体大小限制 (M8) ──
        if request.method in ("POST", "PUT", "PATCH"):
            content_length = request.headers.get("content-length")
            if content_length and int(content_length) > MAX_BODY_SIZE:
                return JSONResponse(
                    content={"error": "请求体过大"},
                    status_code=413,
                )

        # ── 认证 ──
        if not is_public_path(path):
            token = request.cookies.get("ftseek_session", "")
            if not verify_session_token(token):
                if path.startswith("/api/"):
                    resp = JSONResponse(content={"error": "未登录"}, status_code=401)
                else:
                    resp = RedirectResponse(url="/login", status_code=302)
                resp.headers["X-Frame-Options"] = "DENY"
                resp.headers["X-Content-Type-Options"] = "nosniff"
                return resp

        response = await call_next(request)

        # ── 安全响应头 (H1) ──
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self'"
        )

        return response

    # ── 登录/登出 ──

    @app.get("/login")
    async def login_page(request: Request):
        token = request.cookies.get("ftseek_session", "")
        if verify_session_token(token):
            return RedirectResponse(url="/", status_code=302)
        return templates.TemplateResponse("login.html", {"request": request})

    @app.post("/api/auth/login")
    async def auth_login(request: Request):
        # ── 频率限制 (C2) ──
        client_ip = request.client.host if request.client else "unknown"
        if _is_rate_limited(client_ip):
            logger.warning(f"[Auth] 登录频率限制触发: {client_ip}")
            return JSONResponse(
                content={"success": False, "message": "登录尝试过多，请 5 分钟后再试"},
                status_code=429,
            )

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(content={"success": False, "message": "请求无效"}, status_code=400)

        password = body.get("password", "")

        if not has_password_configured():
            token = create_session_token()
            response = JSONResponse(content={
                "success": True, "redirect": "/",
                "message": "未设置密码，请尽快在 .env.us 中配置 DASHBOARD_PASSWORD_MD5",
            })
            response.set_cookie(
                "ftseek_session", token, max_age=86400,
                httponly=True, samesite="lax", secure=True,
            )
            return response

        if verify_password(password):
            token = create_session_token()
            response = JSONResponse(content={"success": True, "redirect": "/"})
            response.set_cookie(
                "ftseek_session", token, max_age=86400,
                httponly=True, samesite="lax", secure=True,
            )
            logger.info("[Auth] 用户登录成功")
            # 登录成功后清除限制
            _login_attempts.pop(client_ip, None)
            return response
        else:
            _record_attempt(client_ip)
            logger.warning(f"[Auth] 登录失败: 密码错误 (IP: {client_ip})")
            remaining = _LOGIN_MAX_ATTEMPTS - len(_login_attempts[client_ip])
            msg = "密码错误" if remaining > 0 else f"密码错误，剩余 {_LOGIN_MAX_ATTEMPTS} 次尝试后锁定 5 分钟"
            return JSONResponse(content={"success": False, "message": msg}, status_code=401)

    @app.get("/api/auth/check")
    async def auth_check(request: Request):
        token = request.cookies.get("ftseek_session", "")
        return {"authenticated": verify_session_token(token)}

    @app.post("/api/auth/logout")
    async def auth_logout(request: Request):
        response = JSONResponse(content={"success": True, "redirect": "/login"})
        response.delete_cookie("ftseek_session")
        return response

    @app.get("/logout")
    async def logout_redirect(request: Request):
        response = RedirectResponse(url="/login", status_code=302)
        response.delete_cookie("ftseek_session")
        return response

    # ── 注册路由模块 ──
    from app.web_dashboard.routes import dashboard as r_dashboard
    from app.web_dashboard.routes import trade as r_trade
    from app.web_dashboard.routes import risk as r_risk
    from app.web_dashboard.routes import analytics as r_analytics
    from app.web_dashboard.routes import stock_detail as r_stock_detail
    from app.web_dashboard.routes import system as r_system
    from app.web_dashboard.routes import watchlist as r_watchlist

    r_dashboard.register(app, templates, dashboard)
    r_trade.register(app, templates)
    r_risk.register(app, templates)
    r_analytics.register(app, templates)
    r_stock_detail.register(app, templates)
    r_system.register(app, templates)
    r_watchlist.register(app, templates)

    logger.info("Dashboard v2.0 已创建 (路由模块化 + DataService 共享缓存)")
    return app
