"""
股票池路由 — 自选股管理 + 实时行情
"""
import logging
import sqlite3
from datetime import datetime
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.web_dashboard.data_service import get_data_service

logger = logging.getLogger("ftseek_us.routes.watchlist")
router = APIRouter()


def _get_db():
    from app.core.db_utils import get_main_db
    return str(get_main_db())


def _ensure_table():
    """确保 user_watchlist 表存在"""
    db_path = _get_db()
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL UNIQUE,
                name TEXT DEFAULT '',
                note TEXT DEFAULT '',
                added_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                sort_order INTEGER DEFAULT 0
            )
        """)
        conn.commit()
    finally:
        conn.close()


def register(app, templates):

    _ensure_table()

    @router.get("/watchlist", response_class=HTMLResponse)
    async def watchlist_page(request: Request):
        from app.web_dashboard.routes.dashboard import _is_live
        return templates.TemplateResponse("watchlist.html", {
            "request": request,
            "title": "股票池 - FTSeek-US",
            "is_live": _is_live(),
        })

    @router.get("/api/watchlist")
    async def get_watchlist():
        """返回自选股列表 + 实时行情"""
        db_path = _get_db()
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT symbol, name, note, added_at FROM user_watchlist ORDER BY sort_order DESC, added_at DESC"
            ).fetchall()
            items = [dict(r) for r in rows]
        finally:
            conn.close()

        if not items:
            return {"items": []}

        # 批量获取实时行情
        symbols = [it["symbol"] for it in items]
        ds = get_data_service()
        quotes = await ds.get_batch_quotes_detailed(symbols)

        for it in items:
            q = quotes.get(it["symbol"], {})
            it["cur_price"] = q.get("cur_price", 0)
            it["prev_close"] = q.get("prev_close", 0)
            it["high_price"] = q.get("high_price", 0)
            it["low_price"] = q.get("low_price", 0)
            it["volume"] = q.get("volume", 0)
            # 用 API 返回的名称补充 DB 中为空的名称
            if not it["name"] and q.get("name"):
                it["name"] = q["name"]
            # 计算涨跌百分比
            if it["prev_close"] > 0 and it["cur_price"] > 0:
                it["change_pct"] = round(
                    (it["cur_price"] - it["prev_close"]) / it["prev_close"] * 100, 2
                )
            else:
                it["change_pct"] = 0

        return {"items": items}

    @router.post("/api/watchlist")
    async def add_to_watchlist(request: Request):
        """添加自选股"""
        body = await request.json()
        symbol = str(body.get("symbol", "")).strip().upper()
        note = str(body.get("note", "")).strip()

        if not symbol:
            return JSONResponse({"error": "请输入股票代码"}, status_code=400)

        # 自动补全 US. 前缀
        if not symbol.startswith("US."):
            symbol = f"US.{symbol}"

        # 验证股票存在性 + 获取名称
        ds = get_data_service()
        quotes = await ds.get_batch_quotes_detailed([symbol])
        q = quotes.get(symbol)
        if not q:
            return JSONResponse({"error": f"未找到 {symbol}，请检查代码是否正确"}, status_code=400)

        name = q.get("name", "")

        db_path = _get_db()
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO user_watchlist (symbol, name, note) VALUES (?, ?, ?)",
                (symbol, name, note),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return JSONResponse({"error": f"{symbol} 已在股票池中"}, status_code=409)
        finally:
            conn.close()

        return {"success": True, "symbol": symbol, "name": name}

    @router.delete("/api/watchlist/{symbol}")
    async def remove_from_watchlist(symbol: str):
        """删除自选股"""
        symbol = symbol.upper()
        db_path = _get_db()
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            cursor = conn.execute(
                "DELETE FROM user_watchlist WHERE symbol = ?", (symbol,)
            )
            conn.commit()
            if cursor.rowcount == 0:
                return JSONResponse({"error": f"{symbol} 不在股票池中"}, status_code=404)
        finally:
            conn.close()

        return {"success": True}

    @router.patch("/api/watchlist/{symbol}")
    async def update_watchlist_item(symbol: str, request: Request):
        """更新自选股备注"""
        symbol = symbol.upper()
        body = await request.json()
        note = str(body.get("note", "")).strip()

        db_path = _get_db()
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            cursor = conn.execute(
                "UPDATE user_watchlist SET note = ? WHERE symbol = ?", (note, symbol)
            )
            conn.commit()
            if cursor.rowcount == 0:
                return JSONResponse({"error": f"{symbol} 不在股票池中"}, status_code=404)
        finally:
            conn.close()

        return {"success": True}

    app.include_router(router)
