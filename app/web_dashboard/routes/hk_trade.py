"""港股手动交易路由 — 无状态直通 (与美股 trade.py 物理隔离).

端点:
  GET  /trade/hk            页面 (全局 auth 中间件保护)
  GET  /api/hk/quote        报价 + lot_size
  POST /api/hk/orders       下单 (idempotency_key + 偏离确认)
  GET  /api/hk/orders/today 今日订单 (实时查 broker)
  POST /api/hk/orders/cancel 撤单
  GET  /api/hk/positions    港股持仓 (实时查 broker, 只读)
  GET  /api/hk/account      港股账户资金 (实时查 broker, 只读)
  GET  /api/hk/ledger       港股实盘台账 (本地只读: 成交流水 + 仓位快照 + FIFO 毛已实现)
  POST /api/hk/ledger/sync  台账回补 (券商 → live 库; 综合账户故两市场一并回补)

**"不写任何 DB" 已于 2026-09-08 (RFC-078 批B) 作废**: 港股此前唯一留痕是
`data/audit/hk_manual_orders.jsonl` 的 submit/cancel (无成交价/量/盈亏), 而券商侧港股
是真钱真有仓 (实测总资产 HK$715,975.88, HK.02513 浮亏 −33,900) ⇒ 港股在所有绩效/复盘
口径里等于不存在。现由 `app/web_dashboard/live_ledger.py` 落两张表到 **live 库**
(`live_deal_records` / `live_position_snapshots`)。下单/撤单路径本身仍不写 DB。

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

    @router.get("/api/hk/ledger")
    async def hk_ledger(days: int = 30):
        """港股实盘台账 (纯本地只读, **不连券商** ⇒ broker 挂了也能看历史).

        三态如实下发: 台账为空时 `state=NO_DATA` 并提示先回补, 前端不得渲染成
        "本周零成交" (那会让人以为实盘停了)。金额一律**毛/成交额** —— 富途 deal 级
        不返回手续费, `fee` 全 NULL, 不伪造 0。
        """
        from app.web_dashboard import live_ledger
        days = min(max(int(days), 1), 365)

        def _collect():
            # 窗口按**港股本地日**取 (deal_date 是券商给的 HKT 日), 不用服务器 CST 墙钟:
            # 尺度由 live_ledger.window_for 统一给出, 路由层不再自己算日期。
            start, end = live_ledger.window_for("HK", days)
            deals = live_ledger.read_deals(start, end, market="HK")
            cov = live_ledger.ledger_coverage()
            return {
                "window": [start, end],
                "window_tz": live_ledger._TZ_LABEL["HK"],
                "state": "OK" if deals else ("NO_DATA" if cov["deals"]["HK"]["rows"] == 0
                                             else "NO_DEALS_IN_WINDOW"),
                "deals": deals,
                # 汇总口径由后端给 (与实盘周报同一个 summarize_deals): 方向语义只有一份,
                # 不让前端各自判 BUY/SELL_SHORT ⇒ 避免"两边错得一样"的净现金流符号反。
                "summary": live_ledger.summarize_deals(deals),
                "fifo": live_ledger.fifo_realized(deals),
                "latest_positions": live_ledger.latest_snapshot("HK", on_or_before=end),
                # 批 D (RFC-078 §8): 回合(券商真值派生) — 港股此前**没有任何回合级数据**
                "round_trips": live_ledger.round_trip_panel("HK", start, end),
                "coverage": cov,
                "currency": "HKD",
                "amount_caliber": "gross_deal_amount_no_fee",
            }

        data = await asyncio.to_thread(_collect)
        if data["state"] == "NO_DATA":
            # 不是错误, 但必须让前端知道"空"是哪一种空
            data["hint"] = ("台账为空 — 先 POST /api/hk/ledger/sync 回补, "
                            "或等每日 cron (10 17 * * 1-5)")
        return api_ok(data)

    @router.post("/api/hk/ledger/sync")
    async def hk_ledger_sync(days: int = 30):
        """台账回补 (券商 → live 库). 幂等, 可反复点.

        ⚠ 综合账户下券商一次返回**两市场**成交 (实测 99 笔 = HK 47 + US 52), 故本端点
        同时回补美股腿 —— 这是券商侧的事实, 不是本端点越权。两腿各自三态, 一腿失败
        返回 207-ish 的 PARTIAL 而不是整体 503 (否则好腿的数据会被坏腿掩盖)。
        """
        from app.web_dashboard import live_ledger
        days = min(max(int(days), 1), 365)
        result = await asyncio.to_thread(live_ledger.sync_all, days)
        result["coverage"] = live_ledger.ledger_coverage()
        if result.get("overall") == "UNAVAILABLE":
            return api_error("BROKER_UNAVAILABLE",
                             "两市场券商腿均不可达 — 台账未更新, 这**不是**零成交",
                             503, retryable=True)
        return api_ok(result)

    app.include_router(router)
