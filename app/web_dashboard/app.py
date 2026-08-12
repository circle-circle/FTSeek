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

# 全局 SQLite 安全钩子 — 在所有 DB 操作之前加载
import app.core.db_utils  # noqa: F401

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


# ── P2-2 (2026-06-12): per-(ip,path) 写接口限速 ──
_RATE_BUCKETS: dict = defaultdict(list)  # (ip, path) -> [timestamps]
_RATE_MAX_PER_MIN = 60
_RATE_WINDOW = 60.0


def _is_write_method(method: str) -> bool:
    return method in ("POST", "PUT", "DELETE", "PATCH")


def _rate_check_and_record(ip: str, path: str) -> bool:
    """返回 True 表示放行, False 表示触发限速."""
    now = time.time()
    key = (ip, path)
    bucket = _RATE_BUCKETS[key]
    bucket[:] = [t for t in bucket if now - t < _RATE_WINDOW]
    if len(bucket) >= _RATE_MAX_PER_MIN:
        return False
    bucket.append(now)
    return True


# ── P2-1 (2026-06-12): 写接口同源 (Origin/Referer) 校验 ──
def _origin_allowed(request) -> bool:
    """同源校验: Origin 或 Referer 的 hostname 必须与 Host header 的 hostname 一致.
    比较时去掉端口，避免 nginx $host (无端口) vs 浏览器 Origin (含端口) 不匹配."""
    raw_host = (request.headers.get("host") or "").lower().strip()
    if not raw_host:
        return False  # 无 Host 头 → 拒绝 (防止 CSRF 旁路)
    # 去掉端口: "ft.aitz.cc:8888" → "ft.aitz.cc"
    host = raw_host.split(":")[0]
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    candidates = []
    for src_url in (origin, referer):
        if not src_url:
            continue
        try:
            from urllib.parse import urlparse
            p = urlparse(src_url)
            if p.hostname:
                candidates.append(p.hostname.lower())
        except Exception:
            pass
    if not candidates:
        # 浏览器写请求一般会带 Origin/Referer, 缺失视为可疑
        return False
    return any(c == host for c in candidates)


def create_app() -> FastAPI:
    app = FastAPI(
        title="FTSeek-US Dashboard",
        description="美股交易 Dashboard",
        version="2.0.0",
    )

    # ── 按需激活: 闲置自动退出 (2026-07-31, 用户裁) ──
    # dashboard 使用频率低, 配合 systemd socket 激活: 无访问时进程整体退出,
    # 不占内存/OpenD 连接; 下次访问由 socket 瞬时拉起 (冷启动数秒)。
    # ASGI 层记录 http+websocket 任何入站报文为活动时间; watchdog 每 60s 检查,
    # 闲置超 DASHBOARD_IDLE_MINUTES (默认 15) 即 os._exit(0)。
    # 注意: service 必须 Restart=on-failure, 正常退出不被复活。
    import os as _os
    _idle_sec = float(_os.getenv("DASHBOARD_IDLE_MINUTES", "15")) * 60
    _act = {"last": time.monotonic()}

    class _ActivityMiddleware:
        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope["type"] in ("http", "websocket"):
                _act["last"] = time.monotonic()

                async def _receive_touch():
                    msg = await receive()
                    _act["last"] = time.monotonic()
                    return msg

                await self.inner(scope, _receive_touch, send)
            else:
                await self.inner(scope, receive, send)

    app.add_middleware(_ActivityMiddleware)

    @app.on_event("startup")
    async def _idle_watchdog():
        import asyncio

        async def _loop():
            while True:
                await asyncio.sleep(60)
                if time.monotonic() - _act["last"] > _idle_sec:
                    logger.info(f"[dashboard] 闲置 {_idle_sec / 60:.0f}min 无活动, 按需激活模式退出")
                    _os._exit(0)

        asyncio.create_task(_loop())

    # ── 关闭 Futu SDK 日志 (SSOT: app.data.futu_lv3_provider.silence_futu_sdk_logs) ──
    # dashboard 自建 OpenQuoteContext/OpenSecTradeContext, FTLog 为进程级单例;
    # 模块 import 时已执行一次, 此处显式再确认 (2026-08-12 收敛 inline 拷贝)
    try:
        from app.data.futu_lv3_provider import silence_futu_sdk_logs
        silence_futu_sdk_logs()
    except Exception as _e:
        logger.warning(f"调整 Futu SDK 日志级别失败: {_e}")

    # ── 路径 ──
    base_dir = Path(__file__).parent

    # ── 静态文件 (Chart.js 等本地库, 30天缓存) ──
    from fastapi.staticfiles import StaticFiles
    from starlette.middleware.base import BaseHTTPMiddleware
    static_dir = base_dir / "static"
    if static_dir.exists():
        class StaticCacheMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                response = await call_next(request)
                if request.url.path.startswith("/static/"):
                    response.headers["Cache-Control"] = "public, max-age=2592000, immutable"
                return response
        app.add_middleware(StaticCacheMiddleware)
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

        # ── P2-1 写接口同源校验 (CSRF 防护): /api/auth/login 豁免 ──
        if _is_write_method(request.method) and path != "/api/auth/login":
            if not _origin_allowed(request):
                logger.warning(
                    f"[CSRF] 拒绝跨域写请求 path={path} "
                    f"origin={request.headers.get('origin')} "
                    f"referer={request.headers.get('referer')} "
                    f"host={request.headers.get('host')}"
                )
                return JSONResponse(
                    content={"error": "跨域写请求被拒绝"},
                    status_code=403,
                )

        # ── P2-2 写接口限速 (per-IP per-path 60/min) ──
        if _is_write_method(request.method) and path.startswith("/api/"):
            client_ip = request.client.host if request.client else "unknown"
            if not _rate_check_and_record(client_ip, path):
                logger.warning(f"[RateLimit] {client_ip} {path} 触发 60/min")
                return JSONResponse(
                    content={"error": "请求过于频繁, 请稍后再试"},
                    status_code=429,
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
            logger.error("[Auth] ⚠️ 未配置 DASHBOARD_PASSWORD_MD5，拒绝登录")
            return JSONResponse(
                content={"success": False, "message": "系统未配置密码，请联系管理员设置 DASHBOARD_PASSWORD_MD5"},
                status_code=503,
            )

        if verify_password(password):
            token = create_session_token()
            response = JSONResponse(content={"success": True, "redirect": "/"})
            response.set_cookie(
                "ftseek_session", token, max_age=86400,
                httponly=True, samesite="strict", secure=True,
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
    from app.web_dashboard.routes import hk_trade as r_hk_trade
    from app.web_dashboard.routes import risk as r_risk
    from app.web_dashboard.routes import analytics as r_analytics
    from app.web_dashboard.routes import stock_detail as r_stock_detail
    from app.web_dashboard.routes import system as r_system
    from app.web_dashboard.routes import watchlist as r_watchlist
    from app.web_dashboard.routes import battle_plan as r_battle_plan
    from app.web_dashboard.routes import trade_intel as r_trade_intel
    from app.web_dashboard.routes import playbook as r_playbook
    from app.web_dashboard.routes import chain as r_chain
    from app.web_dashboard.routes import ws as r_ws

    r_dashboard.register(app, templates, dashboard)
    r_trade.register(app, templates)
    r_hk_trade.register(app, templates)
    r_risk.register(app, templates)
    r_analytics.register(app, templates)
    r_stock_detail.register(app, templates)
    r_system.register(app, templates)
    r_watchlist.register(app, templates)
    r_battle_plan.register(app, templates)
    r_trade_intel.register(app, templates)
    r_playbook.register(app, templates)
    r_chain.register(app, templates)
    r_ws.register(app)

    logger.info("Dashboard v2.0 已创建 (路由模块化 + DataService 共享缓存)")
    return app
