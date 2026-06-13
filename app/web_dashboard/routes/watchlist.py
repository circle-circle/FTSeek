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


def _get_latest_signals(symbols: list) -> dict:
    """从 signal_snapshots.db 获取每只股票最新信号"""
    import os
    result = {}
    db_path = os.path.join(os.path.dirname(_get_db()), "signal_snapshots.db")
    if not os.path.exists(db_path):
        return result
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        placeholders = ",".join(["?"] * len(symbols))
        rows = conn.execute(f"""
            SELECT s.symbol, s.direction, s.strength, s.confidence, s.created_at
            FROM signal_snapshots s
            INNER JOIN (
                SELECT symbol, MAX(created_at) as max_ts
                FROM signal_snapshots
                WHERE symbol IN ({placeholders})
                GROUP BY symbol
            ) latest ON s.symbol = latest.symbol AND s.created_at = latest.max_ts
        """, symbols).fetchall()
        for r in rows:
            result[r["symbol"]] = {
                "direction": r["direction"],
                "strength": r["strength"],
                "confidence": round(r["confidence"], 2) if r["confidence"] else 0,
                "created_at": r["created_at"],
            }
        conn.close()
    except Exception as e:
        logger.debug(f"[watchlist] signal_snapshots query failed: {e}")
    return result


def _get_trade_stats(symbols: list) -> dict:
    """从 trade_records 获取每只股票历史交易统计"""
    result = {}
    for db_name in ["ftseek_us_live.db", "ftseek_us.db"]:
        import os
        db_path = os.path.join(os.path.dirname(_get_db()), db_name)
        if not os.path.exists(db_path):
            continue
        try:
            conn = sqlite3.connect(db_path, timeout=5)
            conn.row_factory = sqlite3.Row
            placeholders = ",".join(["?"] * len(symbols))
            rows = conn.execute(f"""
                SELECT symbol,
                       COUNT(*) as cnt,
                       SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                       ROUND(AVG(realized_pnl), 2) as avg_pnl,
                       ROUND(SUM(realized_pnl), 2) as total_pnl
                FROM trade_records
                WHERE symbol IN ({placeholders})
                  AND realized_pnl IS NOT NULL
                GROUP BY symbol
            """, symbols).fetchall()
            for r in rows:
                sym = r["symbol"]
                cnt = r["cnt"]
                wins = r["wins"]
                # 合并多个 DB 的统计
                if sym in result:
                    old = result[sym]
                    old_total = old["total_pnl"] + (r["total_pnl"] or 0)
                    old_cnt = old["count"] + cnt
                    old_wins = old["_wins"] + wins
                    result[sym] = {
                        "count": old_cnt,
                        "win_rate": round(old_wins / old_cnt * 100, 1) if old_cnt > 0 else 0,
                        "avg_pnl": round(old_total / old_cnt, 2) if old_cnt > 0 else 0,
                        "total_pnl": round(old_total, 2),
                        "_wins": old_wins,
                    }
                else:
                    result[sym] = {
                        "count": cnt,
                        "win_rate": round(wins / cnt * 100, 1) if cnt > 0 else 0,
                        "avg_pnl": r["avg_pnl"] or 0,
                        "total_pnl": r["total_pnl"] or 0,
                        "_wins": wins,
                    }
            conn.close()
        except Exception as e:
            logger.debug(f"[watchlist] trade_records query failed ({db_name}): {e}")
    # 清理内部字段
    for v in result.values():
        v.pop("_wins", None)
    return result


def _get_historical_edge(symbols: list) -> dict:
    """从 signal_snapshots.db 计算每只股票的历史方向优势
    
    核心: 对每只股票分 LONG/SHORT 统计 T+1 胜率。
    输出 edge_direction (优势方向) + edge_confidence (可信度)。
    """
    import os
    result = {}
    db_path = os.path.join(os.path.dirname(_get_db()), "signal_snapshots.db")
    if not os.path.exists(db_path):
        return result
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        placeholders = ",".join(["?"] * len(symbols))
        rows = conn.execute(f"""
            SELECT 
                symbol, direction,
                COUNT(*) as total,
                SUM(CASE WHEN fwd_ret_t1 > 0 THEN 1 ELSE 0 END) as wins,
                ROUND(AVG(fwd_ret_t1) * 100, 3) as avg_ret_pct,
                ROUND(AVG(strength), 0) as avg_strength,
                MAX(trade_date) as last_date
            FROM signal_snapshots 
            WHERE realized > 0 AND fwd_ret_t1 IS NOT NULL
              AND symbol IN ({placeholders})
            GROUP BY symbol, direction
            HAVING COUNT(*) >= 2
        """, symbols).fetchall()
        conn.close()
        
        by_sym = {}
        for r in rows:
            sym = r["symbol"]
            if sym not in by_sym:
                by_sym[sym] = {}
            by_sym[sym][r["direction"].upper()] = {
                "total": r["total"],
                "wins": r["wins"],
                "win_rate": round(r["wins"] / r["total"] * 100, 1),
                "avg_ret": r["avg_ret_pct"],
                "avg_strength": int(r["avg_strength"]),
                "last_date": r["last_date"],
            }
        
        for sym, dirs in by_sym.items():
            long_d = dirs.get("LONG", {})
            short_d = dirs.get("SHORT", {})
            
            edge_dir = ""
            edge_conf = ""
            edge_detail = ""
            
            if long_d and short_d:
                lr = long_d["win_rate"]
                sr = short_d["win_rate"]
                diff = abs(lr - sr)
                if diff >= 30 and max(long_d["total"], short_d["total"]) >= 3:
                    edge_dir = "LONG" if lr > sr else "SHORT"
                    edge_conf = "strong"
                    winner = long_d if edge_dir == "LONG" else short_d
                    edge_detail = f"{edge_dir} {winner['win_rate']:.0f}% ({winner['total']}笔)"
                elif diff >= 15 and max(long_d["total"], short_d["total"]) >= 3:
                    edge_dir = "LONG" if lr > sr else "SHORT"
                    edge_conf = "moderate"
                    winner = long_d if edge_dir == "LONG" else short_d
                    edge_detail = f"{edge_dir} {winner['win_rate']:.0f}% ({winner['total']}笔)"
                else:
                    edge_conf = "none"
                    edge_detail = "无方向优势"
            elif long_d and long_d["total"] >= 3:
                if long_d["win_rate"] >= 60:
                    edge_dir = "LONG"
                    edge_conf = "moderate"
                    edge_detail = f"LONG {long_d['win_rate']:.0f}% ({long_d['total']}笔)"
                else:
                    edge_conf = "none"
                    edge_detail = "无方向优势"
            elif short_d and short_d["total"] >= 3:
                if short_d["win_rate"] >= 60:
                    edge_dir = "SHORT"
                    edge_conf = "moderate"
                    edge_detail = f"SHORT {short_d['win_rate']:.0f}% ({short_d['total']}笔)"
                else:
                    edge_conf = "none"
                    edge_detail = "无方向优势"
            else:
                edge_conf = "insufficient"
                edge_detail = "数据不足"
            
            result[sym] = {
                "long": long_d,
                "short": short_d,
                "edge_direction": edge_dir,
                "edge_confidence": edge_conf,
                "edge_detail": edge_detail,
            }
    except Exception as e:
        logger.debug(f"[watchlist] historical edge query failed: {e}")
    return result


def _get_day_state(quote: dict) -> dict:
    """从报价数据推导当日状态 (不需要 K 线, 纯日内数据)"""
    cur = quote.get("cur_price", 0) or 0
    prev = quote.get("prev_close", 0) or 0
    high = quote.get("high_price", 0) or 0
    low = quote.get("low_price", 0) or 0
    vol = quote.get("volume", 0) or 0
    
    if cur <= 0 or prev <= 0:
        return {"state": "no_data", "label": "无数据", "intraday_range": 0, "change_pct": 0}
    
    change_pct = round((cur - prev) / prev * 100, 2)
    intraday_range = round((high - low) / prev * 100, 2) if prev > 0 and high > low else 0
    
    # 状态分类
    if change_pct >= 3:
        state, label = "strong_up", "强势上涨"
    elif change_pct >= 1:
        state, label = "up", "上涨"
    elif change_pct <= -3:
        state, label = "strong_down", "强势下跌"
    elif change_pct <= -1:
        state, label = "down", "下跌"
    else:
        state, label = "flat", "震荡"
    
    # 日内位置 (在高低点之间的位置)
    if high > low:
        day_position = round((cur - low) / (high - low) * 100, 0)
    else:
        day_position = 50
    
    return {
        "state": state,
        "label": label,
        "intraday_range": intraday_range,
        "change_pct": change_pct,
        "day_position": day_position,
    }


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

        # 批量获取信号快照 + 交易统计 + 历史优势
        signal_data = _get_latest_signals(symbols)
        trade_stats = _get_trade_stats(symbols)
        edge_data = _get_historical_edge(symbols)

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

            # 信号数据 (latest signal from pipeline)
            sig = signal_data.get(it["symbol"], {})
            it["signal_direction"] = sig.get("direction", "")
            it["signal_strength"] = sig.get("strength", 0)
            it["signal_confidence"] = sig.get("confidence", 0)
            it["signal_created_at"] = sig.get("created_at", "")

            # 交易统计 (actual trades)
            ts = trade_stats.get(it["symbol"], {})
            it["trade_count"] = ts.get("count", 0)
            it["win_rate"] = ts.get("win_rate", 0)
            it["avg_pnl"] = ts.get("avg_pnl", 0)
            it["total_pnl"] = ts.get("total_pnl", 0)

            # 历史方向优势 (signal_snapshots forward returns)
            edge = edge_data.get(it["symbol"], {})
            it["edge_direction"] = edge.get("edge_direction", "")
            it["edge_confidence"] = edge.get("edge_confidence", "")
            it["edge_detail"] = edge.get("edge_detail", "")
            it["long_stats"] = edge.get("long", {})
            it["short_stats"] = edge.get("short", {})

            # 当日状态
            day = _get_day_state(q)
            it["day_state"] = day["state"]
            it["day_label"] = day["label"]
            it["day_position"] = day.get("day_position", 50)
            it["intraday_range"] = day.get("intraday_range", 0)

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
