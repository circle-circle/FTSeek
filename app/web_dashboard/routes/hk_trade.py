"""港股手动交易路由 — 无状态直通 (与美股 trade.py 物理隔离).

端点:
  GET  /trade/hk            页面 (全局 auth 中间件保护)
  GET  /api/hk/quote        报价 + lot_size
  POST /api/hk/orders       下单 (idempotency_key + 偏离确认)
  GET  /api/hk/orders/today 今日订单 (实时查 broker)
  POST /api/hk/orders/cancel 撤单
  GET  /api/hk/positions    港股持仓 (实时查 broker, 只读)
  GET  /api/hk/account      港股账户资金 (实时查 broker, 只读)

不写任何 DB; 成交/持仓管理以富途 App 为准。

2026-09-08 (REVIEW-20260908 批1): 查询失败不再伪装成空列表 —— positions/orders
两端点在 broker 不可用时返回 503 BROKER_UNAVAILABLE (retryable), 前端据此渲染
"加载失败 + 重试"而非"无港股持仓"; /api/hk/orders/today 另附只读 limits 快照
(单笔金额上限 / 当日单数额度 / 偏离阈值), 供前端提交前预检条呈现。判据仍在
hk_order_service.validate(), 本层只呈现不裁决。

2026-09-08 (REVIEW-20260908 批3 A11): 新增 GET /api/hk/account —— 本页是实盘直通,
此前却看不到可用资金/购买力 (hk_order_service 全文无 accinfo_query)。同样三态:
查询失败返回 503 而非"资金为 0"。
"""
import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.web_dashboard.api_response import api_ok, api_error

logger = logging.getLogger("ftseek_us.routes.hk_trade")
router = APIRouter()


def register(app, templates):
    from app.web_dashboard.hk_order_service import get_hk_order_service, HKOrder

    @router.get("/trade/hk", response_class=HTMLResponse)
    async def hk_trade_page(request: Request):
        return templates.TemplateResponse("hk_trade.html", {
            "request": request,
            "title": "港股交易 - FTSeek-US",
        })

    @router.get("/api/hk/quote")
    async def hk_quote(symbol: str = ""):
        svc = get_hk_order_service()
        sym = svc.normalize_symbol(symbol)
        if not (sym.startswith("HK.") and sym[3:].isdigit() and len(sym) == 8):
            return api_error("BAD_REQUEST", f"标的格式无效: {symbol} (如 00700)", 400)
        quote = await asyncio.to_thread(svc.get_quote, sym)
        if quote.get("error"):
            return api_error("QUOTE_ERROR", f"报价获取失败: {quote['error']}", 502)
        return api_ok(quote)

    @router.post("/api/hk/orders")
    async def hk_place_order(request: Request):
        try:
            body = await request.json()
        except Exception:
            return api_error("BAD_REQUEST", "请求体无效 (需要 JSON)", 400)
        if not isinstance(body, dict) or not body:
            return api_error("BAD_REQUEST", "请求体为空", 400)

        svc = get_hk_order_service()

        # 2026-07-22 用户要求: 去掉 CONFIRM 确认关键词与理由必填, 降低手动下单摩擦
        try:
            quantity = int(body.get("quantity", 0))
        except (ValueError, TypeError):
            return api_error("BAD_REQUEST", "数量必须是整数", 400)
        try:
            price = float(body["price"]) if body.get("price") else None
        except (ValueError, TypeError):
            price = None

        order = HKOrder(
            symbol=svc.normalize_symbol(body.get("symbol", "")),
            action=str(body.get("action", "")).upper(),
            quantity=quantity,
            order_type=str(body.get("order_type", "LIMIT")).upper(),
            price=price,
            reason=str(body.get("reason", "")),
            acknowledge_price_warning=bool(body.get("acknowledge_price_warning", False)),
        )
        result = await asyncio.to_thread(
            svc.submit_order, order, body.get("idempotency_key"))
        if result.get("success"):
            return api_ok(result)
        return api_error("ORDER_ERROR", result.get("error", "下单失败"), 400)

    @router.post("/api/hk/orders/cancel")
    async def hk_cancel_order(request: Request):
        try:
            body = await request.json()
        except Exception:
            return api_error("BAD_REQUEST", "请求无效", 400)
        order_id = str(body.get("order_id", "")).strip()
        if not order_id:
            return api_error("BAD_REQUEST", "order_id 必填", 400)
        svc = get_hk_order_service()
        result = await asyncio.to_thread(svc.cancel_order, order_id)
        if result.get("success"):
            return api_ok(result)
        return api_error("CANCEL_ERROR", result.get("error", "撤单失败"), 400)

    @router.get("/api/hk/orders/today")
    async def hk_today_orders():
        svc = get_hk_order_service()

        def _collect():
            # 两处都是同步 IO (broker RPC + 审计 jsonl 计数), 一并卸载到线程
            return svc.get_today_orders_with_status(), svc.get_order_limits()

        result, limits = await asyncio.to_thread(_collect)
        if result.get("error"):
            return api_error("BROKER_UNAVAILABLE",
                             f"今日订单查询失败: {result['error']}", 503, retryable=True)
        return api_ok({"orders": result["rows"], "limits": limits})

    @router.get("/api/hk/positions")
    async def hk_positions():
        svc = get_hk_order_service()
        result = await asyncio.to_thread(svc.get_positions_with_status)
        if result.get("error"):
            return api_error("BROKER_UNAVAILABLE",
                             f"港股持仓查询失败: {result['error']}", 503, retryable=True)
        return api_ok({"positions": result["rows"]})

    @router.get("/api/hk/account")
    async def hk_account():
        """港股账户资金 (只读). A11: 实盘下单前必须能看到可用资金。"""
        svc = get_hk_order_service()
        result = await asyncio.to_thread(svc.get_account_info_with_status)
        if result.get("error"):
            return api_error("BROKER_UNAVAILABLE",
                             f"港股账户查询失败: {result['error']}", 503, retryable=True)
        return api_ok({"account": result["info"]})

    app.include_router(router)
