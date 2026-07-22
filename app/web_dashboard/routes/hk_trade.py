"""港股手动交易路由 — 无状态直通 (与美股 trade.py 物理隔离).

端点:
  GET  /trade/hk            页面 (全局 auth 中间件保护)
  GET  /api/hk/quote        报价 + lot_size
  POST /api/hk/orders       下单 (idempotency_key + 偏离确认)
  GET  /api/hk/orders/today 今日订单 (实时查 broker)
  POST /api/hk/orders/cancel 撤单
  GET  /api/hk/positions    港股持仓 (实时查 broker, 只读)

不写任何 DB; 成交/持仓管理以富途 App 为准。
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
        orders = await asyncio.to_thread(svc.get_today_orders)
        return api_ok({"orders": orders})

    @router.get("/api/hk/positions")
    async def hk_positions():
        svc = get_hk_order_service()
        positions = await asyncio.to_thread(svc.get_positions)
        return api_ok({"positions": positions})

    app.include_router(router)
