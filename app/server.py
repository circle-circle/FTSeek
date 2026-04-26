"""
FastAPI 确认回调服务
提供 HTTP 接口接收钉钉按钮回调，确认后触发交易
同时提供管理 API 查看信号、账户信息等
"""
import logging
import time
import hashlib
import secrets
from collections import defaultdict
from dataclasses import asdict
from typing import Optional

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import config
from app.signal_store import signal_store
from app.storage.repository import get_db
from app.trader.executor import TradeExecutor
from app.notifier.dingtalk import DingTalkNotifier

logger = logging.getLogger("ftseek.server")

# 关闭 /docs 和 /redoc，防止接口信息泄露
# 生产环境建议通过 Nginx 反向代理 + HTTPS 保护 API
app = FastAPI(
    title="FTSeek",
    version="0.3.0",  # 升级到 v0.3.0 (新增实时风险监控和可视化大屏)
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    description="量化交易系统 API - 生产环境请配置 HTTPS 和防火墙"
)

# 挂载静态文件目录（可视化大屏）
from fastapi.staticfiles import StaticFiles
import os
static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# 挂载模拟盘 Dashboard 目录
dashboard_dir = os.path.join(os.path.dirname(__file__), "dashboard")
os.makedirs(dashboard_dir, exist_ok=True)

# 修复 /simulation/ 根路径访问 (必须在 StaticFiles 挂载之前)
@app.get("/simulation", include_in_schema=False)
@app.get("/simulation/", include_in_schema=False)
async def simulation_dashboard_root():
    """模拟盘监控看板"""
    from pathlib import Path
    dashboard_file = Path(__file__).parent / "dashboard" / "simulation_dashboard.html"
    if dashboard_file.exists():
        return HTMLResponse(dashboard_file.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>模拟盘看板文件不存在</h1>", status_code=404)

app.mount("/simulation", StaticFiles(directory=dashboard_dir), name="simulation")

# 【新增 2026-04-02】挂载 Dashboard 优化监控面板
@app.get("/dashboard/optimization_monitor.html", include_in_schema=False)
async def optimization_monitor_page():
    """系统优化监控面板"""
    from pathlib import Path
    dashboard_file = Path(__file__).parent / "dashboard" / "optimization_monitor.html"
    if dashboard_file.exists():
        return HTMLResponse(dashboard_file.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>优化监控面板文件不存在</h1>", status_code=404)


# 【新增 2026-04-05】新版复盘大盘 v2
@app.get("/review-v2/", include_in_schema=False)
@app.get("/review-v2", include_in_schema=False)
async def review_v2_page():
    """新版复盘大盘 v2 (Vue 3 + ECharts)"""
    from pathlib import Path
    dashboard_file = Path(__file__).parent / "dashboard" / "v2" / "index.html"
    if dashboard_file.exists():
        return HTMLResponse(dashboard_file.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>复盘大盘 v2 页面文件不存在</h1>", status_code=404)

# 【重定向】/dashboard/v2/ -> /review-v2/
@app.get("/dashboard/v2/", include_in_schema=False)
@app.get("/dashboard/v2", include_in_schema=False)
async def dashboard_v2_redirect():
    """重定向到 v2 首页"""
    return RedirectResponse(url="/review-v2/")

# 【静态资源映射】/dashboard/v2/css -> /dashboard/static/v2/css
# 兼容 HTML 中引用的旧路径
app.mount("/dashboard/v2/css", StaticFiles(directory=os.path.join(dashboard_dir, "v2", "css")), name="dashboard_v2_css")
app.mount("/dashboard/v2/js", StaticFiles(directory=os.path.join(dashboard_dir, "v2", "js")), name="dashboard_v2_js")

# 挂载 /dashboard/static 目录（用于访问静态资源，如 CSS/JS）
# 注意：必须在页面路由之后注册，避免覆盖 HTML 路由
app.mount("/dashboard/static", StaticFiles(directory=dashboard_dir), name="dashboard_static")



# 注册新版复盘大盘 API v2 路由
try:
    from app.api.v2.review_dashboard import router as review_v2_router
    app.include_router(review_v2_router)
    logger.info("新版复盘大盘 API v2 路由已注册")
except Exception as e:
    logger.warning(f"新版复盘大盘 API v2 路由注册失败：{e}")

# 注册回测 API 路由
try:
    from app.backtest.api import router as backtest_router
    app.include_router(backtest_router)
    logger.info("回测 API 路由已注册")
except Exception as e:
    logger.warning(f"回测 API 路由注册失败：{e}")

# 注册参数优化 API 路由
try:
    from app.backtest.optimizer_api import router as optimizer_router
    app.include_router(optimizer_router, prefix="/api/optimize")
    logger.info("参数优化 API 路由已注册")
except Exception as e:
    logger.warning(f"参数优化 API 路由注册失败：{e}")

# 注册场景识别 API 路由 (Phase 1: 2026-04-09)
try:
    from app.api.v2.scenario import router as scenario_router
    app.include_router(scenario_router)
    logger.info("场景识别 API 路由已注册")
except Exception as e:
    logger.warning(f"场景识别 API 路由注册失败：{e}")

# 注册场景日志 API 路由 (Phase 2: 2026-04-09)
try:
    from app.api.v2.scenario_log import router as scenario_log_router
    app.include_router(scenario_log_router)
    logger.info("场景日志 API 路由已注册")
except Exception as e:
    logger.warning(f"场景日志 API 路由注册失败：{e}")

# 注册场景绩效 API 路由 (Phase 3: 2026-04-09)
try:
    from app.api.v2.scenario_performance import router as scenario_performance_router
    app.include_router(scenario_performance_router)
    logger.info("场景绩效 API 路由已注册")
except Exception as e:
    logger.warning(f"场景绩效 API 路由注册失败：{e}")

# 注册实时风险监控 API 路由
try:
    from app.risk.api import router as risk_router
    app.include_router(risk_router)
    logger.info("实时风险监控 API 路由已注册")
except Exception as e:
    logger.warning(f"实时风险监控 API 路由注册失败：{e}")

# 注册专业量化指标 API 路由 (已废弃/暂未实现)
# try:
#     from app.risk.metrics_api import router as metrics_router
#     app.include_router(metrics_router, prefix="/api/risk")
#     logger.info("专业量化指标 API 路由已注册")
# except Exception as e:
#     logger.warning(f"专业量化指标 API 路由注册失败：{e}")

# 注册认证 API 路由
try:
    from app.auth.api import router as auth_router
    app.include_router(auth_router)
    logger.info("认证 API 路由已注册")
except Exception as e:
    logger.warning(f"认证 API 路由注册失败：{e}")

# 全局实例 (在 main.py 启动时注入)
_executor: Optional[TradeExecutor] = None
_notifier: Optional[DingTalkNotifier] = None
_subscriber = None  # P0 (2026-04-16): DataSubscriber 引用，供新闻事件注入使用


def set_executor(executor: TradeExecutor):
    global _executor
    _executor = executor


def set_notifier(notifier: DingTalkNotifier):
    global _notifier
    _notifier = notifier


def set_subscriber(subscriber):
    """设置全局 DataSubscriber 实例"""
    global _subscriber
    _subscriber = subscriber

def get_subscriber_instance():
    """获取全局 DataSubscriber 实例"""
    return _subscriber


# ============================================================
#  安全: Token 校验 (使用 constant-time 比较防止时序攻击)
# ============================================================
def _check_token(token: str):
    if not secrets.compare_digest(token, config.API_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid token")


# ============================================================
#  安全: 速率限制 (内存级简易实现)
# ============================================================
class RateLimiter:
    """简易速率限制器: 每个 IP 每分钟最多 max_requests 次"""
    def __init__(self, max_requests: int = 30, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._hits = defaultdict(list)  # ip -> [timestamp, ...]

    def check(self, ip: str) -> bool:
        now = time.time()
        hits = self._hits[ip]
        # 清理过期记录
        self._hits[ip] = [t for t in hits if now - t < self.window]
        if len(self._hits[ip]) >= self.max_requests:
            return False
        self._hits[ip].append(now)
        return True

_rate_limiter = RateLimiter(max_requests=30, window_seconds=60)
_confirm_rate_limiter = RateLimiter(max_requests=5, window_seconds=60)  # 确认接口更严格


# ============================================================
#  安全: 速率限制中间件
# ============================================================
@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    client_ip = request.client.host if request.client else "unknown"
    path = request.url.path

    # 确认/拒绝接口用更严格的限制
    if path in ("/api/confirm", "/api/reject"):
        if not _confirm_rate_limiter.check(client_ip):
            logger.warning(f"速率限制触发(confirm): {client_ip}")
            return JSONResponse(status_code=429, content={"detail": "Too many requests"})
    elif path.startswith("/api/"):
        if not _rate_limiter.check(client_ip):
            logger.warning(f"速率限制触发: {client_ip}")
            return JSONResponse(status_code=429, content={"detail": "Too many requests"})

    response = await call_next(request)
    # 安全响应头
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "no-store"
    return response


# ============================================================
#  确认回调 - 钉钉按钮点击后触发
# ============================================================

CONFIRM_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>FTSeek - {title}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, sans-serif; background: #f5f5f5; display: flex; 
                justify-content: center; align-items: center; min-height: 100vh; }}
        .card {{ background: white; border-radius: 16px; padding: 40px; max-width: 400px;
                 text-align: center; box-shadow: 0 4px 24px rgba(0,0,0,0.1); }}
        .icon {{ font-size: 64px; margin-bottom: 16px; }}
        .title {{ font-size: 20px; font-weight: 600; color: #333; margin-bottom: 8px; }}
        .detail {{ font-size: 14px; color: #666; line-height: 1.6; margin-top: 12px; }}
        .code {{ font-weight: 600; color: #1a73e8; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">{icon}</div>
        <div class="title">{title}</div>
        <div class="detail">{detail}</div>
    </div>
</body>
</html>
"""


@app.get("/api/confirm")
async def confirm_signal(request: Request, signal_id: str = Query(...), token: str = Query(...)):
    """确认交易信号 - 钉钉按钮回调入口"""
    _check_token(token)

    client_ip = request.client.host if request.client else "unknown"
    logger.info(f"确认请求: signal_id={signal_id} from={client_ip}")

    signal = signal_store.confirm(signal_id)
    if not signal:
        return HTMLResponse(CONFIRM_HTML.format(
            icon="⚠️", title="信号无效或已过期",
            detail="该信号可能已过期、已处理或不存在。"
        ))

    # 执行交易
    currency = "$" if signal.code.startswith("US.") else "HK$"

    if _executor and signal.action.value in ("BUY", "SELL"):
        result = _executor.execute(signal)
        signal_store.mark_executed(signal_id)

        if _notifier:
            _notifier.send_execution_result(signal, result["success"], result["message"])

        if result["success"]:
            return HTMLResponse(CONFIRM_HTML.format(
                icon="✅", title="交易已执行",
                detail=(
                    f"<span class='code'>{signal.code}</span> {signal.action.value}<br>"
                    f"价格: {currency}{signal.price:.2f} | 数量: {signal.quantity}<br>"
                    f"订单号: {result.get('order_id', 'N/A')}"
                )
            ))
        else:
            return HTMLResponse(CONFIRM_HTML.format(
                icon="❌", title="交易执行失败",
                detail=f"{result['message']}"
            ))
    else:
        signal_store.mark_executed(signal_id)
        return HTMLResponse(CONFIRM_HTML.format(
            icon="👁️", title="已确认查看",
            detail=f"<span class='code'>{signal.code}</span><br>{signal.reason}"
        ))


@app.get("/api/reject")
async def reject_signal(signal_id: str = Query(...), token: str = Query(...)):
    """忽略/拒绝信号"""
    _check_token(token)
    signal_store.reject(signal_id)
    return HTMLResponse(CONFIRM_HTML.format(
        icon="🚫", title="信号已忽略",
        detail="该交易信号已被忽略，不会执行任何操作。"
    ))


@app.get("/api/defer")
async def defer_signal(request: Request, signal_id: str = Query(...), 
                       token: str = Query(...), minutes: int = Query(default=15)):
    """延期信号 - 钉钉按钮回调"""
    _check_token(token)
    
    client_ip = request.client.host if request.client else "unknown"
    logger.info(f"延期请求：signal_id={signal_id} minutes={minutes} from={client_ip}")
    
    signal = signal_store.get(signal_id)
    if not signal:
        return HTMLResponse(CONFIRM_HTML.format(
            icon="⚠️", title="信号不存在",
            detail="该信号可能已过期或不存在。"
        ))
    
    if signal.confirmed or signal.executed:
        return HTMLResponse(CONFIRM_HTML.format(
            icon="⚠️", title="信号已处理",
            detail="该信号已被确认或执行，无法延期。"
        ))
    
    # 延长有效期
    import time
    signal.expire_at = time.time() + (minutes * 60)
    
    # 重新推送通知
    if _notifier:
        _notifier.send_text(
            f"⏰ 信号延期通知\n"
            f"标的：{signal.code}\n"
            f"方向：{signal.action.value}\n"
            f"延期：{minutes} 分钟\n"
            f"新有效期：{time.strftime('%H:%M:%S', time.localtime(signal.expire_at))}"
        )
    
    logger.info(f"信号已延期：{signal_id} +{minutes}分钟")
    
    return HTMLResponse(CONFIRM_HTML.format(
        icon="⏰", title="信号已延期",
        detail=f"该信号有效期已延长 {minutes} 分钟。\n请在新的有效期内确认或忽略。"
    ))


# ============================================================
#  管理 API
# ============================================================

@app.get("/api/status")
async def status(token: str = Query(...)):
    """系统状态"""
    _check_token(token)
    pending = signal_store.get_pending()
    return {
        "status": "running",
        "pending_signals": len(pending),
        "trade_env": "模拟" if config.TRADE_ENV == 1 else "真实",
        "watch_list": config.WATCH_LIST_HK,
        "timestamp": time.time(),
    }


@app.get("/api/signals")
async def list_signals(token: str = Query(...), history: bool = False):
    """查看信号列表"""
    _check_token(token)
    if history:
        signals = signal_store.get_history()
    else:
        signals = signal_store.get_pending()
    return [asdict(s) for s in signals]


@app.post("/api/signal/create")
async def create_signal_api(request: Request):
    """
    创建测试信号并推送到钉钉
    用于测试联动流程
    """
    from app.strategy.base import Signal, SignalAction
    
    body = await request.json()
    token = body.get('token', '')
    _check_token(token)
    
    # 创建信号
    signal = Signal(
        id=body.get('id', f'test_{int(time.time())}'),
        code=body.get('code', 'HK.00700'),
        name=body.get('name', '腾讯控股'),
        action=SignalAction.BUY if body.get('action', 'BUY') == 'BUY' else SignalAction.SELL,
        price=body.get('price', 520.0),
        quantity=body.get('quantity', 100),
        strategy=body.get('strategy', 'test'),
        reason=body.get('reason', '测试信号'),
        extra=body.get('extra', {})
    )
    
    # 添加到 SignalStore
    signal_store.add(signal)
    logger.info(f"API 创建信号：{signal.id} -> {signal.code} {signal.action.value}")
    
    # 推送到钉钉
    if _notifier:
        _notifier.send_signal(signal)
        logger.info(f"信号已推送：{signal.id}")
    
    return {
        'status': 'ok',
        'signal_id': signal.id,
        'code': signal.code,
        'action': signal.action.value,
        'expire_seconds': config.SIGNAL_EXPIRE_SECONDS,
        'confirm_url': f"{config.CALLBACK_EXTERNAL_URL}/api/confirm?signal_id={signal.id}&token={token}"
    }


@app.get("/api/account")
async def account_info(token: str = Query(...)):
    """查看账户信息"""
    _check_token(token)
    if not _executor:
        return {"error": "交易执行器未初始化"}
    info = _executor.get_account_info()
    return info or {"error": "获取账户信息失败"}


@app.get("/health")
async def health(token: str = Query(default=None)):
    """健康检查 (不泄露内部版本号，可选 Token 鉴权)"""
    # 如果提供了 token 则校验，允许无 token 的简单健康检查
    # 监控系统可以选择配置 token 来增强安全性
    if token:
        try:
            _check_token(token)
        except HTTPException:
            return {"status": "unauthorized"}
    return {"status": "ok"}


@app.post("/api/news/event")
async def news_event(news_data: dict):
    """
    P0 (2026-04-16): 新闻事件注入接口
    供 futu_flash_cron 等外部采集器调用，将新闻注入事件驱动管线
    """
    news_list = news_data.get('news_list', [])
    if not news_list:
        return {"status": "error", "message": "no news"}
    
    # 注入到 DataSubscriber 的事件驱动入口
    try:
        if _subscriber and hasattr(_subscriber, 'on_news_event'):
            _subscriber.on_news_event(news_list)
            return {"status": "ok", "injected": len(news_list)}
        else:
            return {"status": "error", "message": "subscriber not available"}
    except Exception as e:
        return {"status": "error", "message": str(e)[:100]}


@app.get("/")
async def root(request: Request):
    """主页 - 重定向到功能页面"""
    # 检查是否已登录
    session_id = request.cookies.get("session_id")
    if session_id:
        from app.auth.users import user_manager
        username = user_manager.validate_session(session_id)
        if username:
            # 已登录，返回主功能页面
            from pathlib import Path
            index_file = Path(__file__).parent / "static" / "index.html"
            if index_file.exists():
                return HTMLResponse(index_file.read_text(encoding="utf-8"))
    
    # 未登录，重定向到登录页
    return RedirectResponse(url="/auth/login")

@app.get("/risk/dashboard")
async def risk_dashboard_page():
    """可视化风险大屏页面（专业版）"""
    from pathlib import Path
    # 优先使用 v2 版本
    dashboard_file = Path(__file__).parent / "static" / "risk_dashboard_v2.html"
    if not dashboard_file.exists():
        dashboard_file = Path(__file__).parent / "static" / "risk_dashboard.html"
    if dashboard_file.exists():
        return HTMLResponse(dashboard_file.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>页面不存在</h1>", status_code=404)


# ============================================================
#  TradingView Webhook 接收端点
# ============================================================
from app.strategy.tradingview_webhook import parse_tradingview_webhook

@app.post("/api/tradingview/webhook")
async def tradingview_webhook(request: Request):
    """
    接收 TradingView Alert Webhook
    TradingView Alert → JSON POST → 此端点 → 转为内部 Signal → 推送通知
    """
    token = request.headers.get("X-Token") or request.query_params.get("token", "")
    if not secrets.compare_digest(token, config.TRADINGVIEW_WEBHOOK_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid token")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.info(f"TradingView Webhook: {payload}")

    signal = parse_tradingview_webhook(payload)
    if signal is None:
        return {"status": "ignored", "reason": "unable to parse signal"}

    signal_store.add(signal)
    if _notifier:
        _notifier.send_signal(signal)

    return {
        "status": "ok",
        "signal_id": signal.id,
        "code": signal.code,
        "action": signal.action.value,
        "price": signal.price,
    }


# ============================================================
#  策略管理 API
# ============================================================
_strategy_manager = None

def set_strategy_manager(manager):
    global _strategy_manager
    _strategy_manager = manager


@app.get("/api/strategies")
async def list_strategies_api(token: str = Query(...)):
    """列出所有策略及参数"""
    _check_token(token)
    if not _strategy_manager:
        return {"error": "策略管理器未初始化"}
    return _strategy_manager.list_status()


@app.post("/api/strategy/update")
async def update_strategy_api(request: Request):
    """动态更新策略参数"""
    body = await request.json()
    token = body.get("token", "")
    _check_token(token)

    name = body.get("strategy", "")
    params = body.get("params", {})
    if not name or not params:
        raise HTTPException(status_code=400, detail="strategy and params required")

    if not _strategy_manager:
        return {"error": "策略管理器未初始化"}

    ok = _strategy_manager.update_strategy(name, params)
    if ok:
        s = _strategy_manager.get(name)
        return {"status": "ok", "params": s.get_params() if hasattr(s, "get_params") else {}}
    return {"status": "failed", "message": f"策略 {name} 不存在"}


# ============================================================
#  苏格拉底复盘 Dashboard (恢复)
# ============================================================
# 虽然之前分离为独立服务，但为了方便访问，重新添加路由
# WebSocket 错误已修复，不会影响交易日志
# ============================================================
@app.get("/socratic-review/", include_in_schema=False)
async def socratic_review_root():
    """苏格拉底深度复盘 Dashboard"""
    from pathlib import Path
    from fastapi.responses import HTMLResponse
    review_file = Path(__file__).parent / "simulation" / "socratic_review_dashboard.html"
    if review_file.exists():
        html_content = review_file.read_text(encoding="utf-8")
        return HTMLResponse(html_content)
    return HTMLResponse("<h1>苏格拉底复盘 Dashboard 加载中...</h1>", status_code=404)

print("✅ 苏格拉底复盘路由已恢复 (/socratic-review/)")

# 注册苏格拉底复盘 API 路由
try:
    from app.simulation.socratic_review_dashboard import router as socratic_router
    app.include_router(socratic_router)
    print("✅ 苏格拉底复盘 API 路由已注册")
except Exception as e:
    print(f"⚠️ 苏格拉底复盘 API 路由注册失败：{e}")


# ============================================================
#  Legacy Deep Review APIs Removed
#  Migrated to: app/simulation/socratic_review_dashboard.py
# ============================================================

# ============================================================
# Phase 1: 归因链路回溯查询 API
# ============================================================

@app.get("/api/attribution/chain/{trace_id}")
async def get_attribution_chain(trace_id: str):
    """
    获取指定 trace_id 的完整证据链。
    返回: position + trade_chain + attribution + feedback
    """
    from app.evidence.attribution_chain_fix import AttributionQueryEngine
    engine = AttributionQueryEngine()
    chain = engine.get_full_chain(trace_id)
    if not chain:
        raise HTTPException(status_code=404, detail=f"Trace ID {trace_id} 未找到")
    return chain


@app.get("/api/attribution/trades")
async def query_attribution_trades(
    code: str = Query(None, description="股票代码"),
    strategy: str = Query(None, description="策略名称"),
    start_time: str = Query(None, description="开始时间 (ISO)"),
    end_time: str = Query(None, description="结束时间 (ISO)"),
    pnl_min: float = Query(None, description="最小盈亏"),
    pnl_max: float = Query(None, description="最大盈亏"),
    limit: int = Query(50, description="返回数量"),
):
    """
    查询交易列表，支持多维度过滤。
    """
    from app.evidence.attribution_chain_fix import AttributionQueryEngine
    engine = AttributionQueryEngine()
    trades = engine.query_trades(
        start_time=start_time,
        end_time=end_time,
        code=code,
        strategy=strategy,
        pnl_min=pnl_min,
        pnl_max=pnl_max,
        limit=limit,
    )
    return {"count": len(trades), "trades": trades}


@app.get("/api/attribution/summary")
async def get_attribution_summary(days: int = Query(30, description="统计天数")):
    """
    获取归因统计摘要。
    """
    from app.evidence.attribution_chain_fix import AttributionQueryEngine
    engine = AttributionQueryEngine()
    summary = engine.get_attribution_summary(days=days)
    return summary


print("✅ 归因链路回溯查询 API 已注册 (/api/attribution/*)")
