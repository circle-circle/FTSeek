"""
Dashboard 路由 — 指挥中心首页 + 统一 Snapshot 端点

核心改进:
  /api/dashboard/snapshot 一次返回所有首页数据,
  替代原来 8+ 并行 API 调用 (risk_status + live-account + signal-dashboard + market-temp + positions-attribution)
  Futu RPC 从 10+ 次降到 3 次 (accinfo + positions + snapshot_batch)
"""
import asyncio
import logging
import sqlite3
from datetime import datetime
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.web_dashboard.data_service import (
    get_data_service, safe_float, et_today,
)

logger = logging.getLogger("ftseek_us.routes.dashboard")
router = APIRouter()


def register(app, templates, dashboard):
    """注册 Dashboard 路由"""

    @router.get("/", response_class=HTMLResponse)
    async def dashboard_home(request: Request):
        is_live = _is_live()
        stats = dashboard.get_stats()
        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "title": "FTSeek-US Dashboard",
            "stats": stats,
            "is_live": is_live,
            "current_time": datetime.now().isoformat(),
        })

    @router.get("/api/stats")
    async def get_stats():
        ds = get_data_service()
        cached = ds.cache.get("stats")
        if cached is not None:
            return cached
        result = dashboard.get_stats()
        ds.cache.set("stats", result, ttl=10)
        return result

    @router.get("/api/positions")
    async def get_positions():
        """DB 优先, 实盘 fallback broker"""
        db_positions = dashboard.get_positions()
        if db_positions:
            return {"positions": db_positions, "source": "db"}

        # Fallback: broker
        ds = get_data_service()
        try:
            raw = await ds.get_positions_raw()
            if raw:
                positions = []
                for row in raw:
                    qty = float(row.get("qty", 0))
                    side = str(row.get("position_side", "")).upper()
                    if qty == 0 and side not in ("LONG", "SHORT"):
                        continue
                    is_short = (qty < 0) or (side == "SHORT")
                    positions.append({
                        "symbol": str(row.get("code", "")),
                        "direction": "SHORT" if is_short else "LONG",
                        "quantity": abs(qty),
                        "entry_price": float(row.get("cost_price", 0)),
                        "current_price": float(row.get("cur_price", 0)),
                        "market_value": float(row.get("market_val", 0)),
                        "unrealized_pnl": float(row.get("unrealized_pl", 0)),
                        "owner": "auto",
                    })
                if positions:
                    return {"positions": positions, "source": "broker"}
        except Exception as e:
            logger.debug(f"[positions] broker fallback failed: {e}")

        return {"positions": [], "source": "empty"}

    @router.get("/api/account")
    async def get_account():
        return dashboard.get_account_summary()

    @router.get("/api/health")
    async def health_check_api():
        return {"status": "ok", "timestamp": datetime.now().isoformat()}

    @router.get("/api/market/temperature")
    async def market_temperature():
        """市场温度: SPY + VIX (30s TTL)"""
        ds = get_data_service()
        spy_task = ds.get_market_snapshot(["US.SPY"], ttl=30)
        vix_task = ds.get_vix()
        snap, vix = await asyncio.gather(spy_task, vix_task)

        result = {"spy": None, "vix": vix}
        spy_row = snap.get("US.SPY")
        if spy_row:
            cur = safe_float(spy_row.get("cur_price"))
            if cur <= 0:
                cur = safe_float(spy_row.get("last_price"))
            prev = safe_float(spy_row.get("prev_close_price"))
            if cur > 0 and prev > 0:
                result["spy"] = {
                    "price": round(cur, 2),
                    "change": round(cur - prev, 2),
                    "change_pct": round((cur - prev) / prev * 100, 2),
                }
        return result

    # ═══════════════════════════════════════════════════════════
    #  统一 Snapshot — 首页一次拉取全部数据
    # ═══════════════════════════════════════════════════════════

    @router.get("/api/dashboard/snapshot")
    async def dashboard_snapshot():
        """
        替代 5 个并行 API 调用:
          /api/risk/status + /api/live-account + /api/analytics/signal-dashboard
          + /api/market/temperature + /api/analytics/positions-attribution

        内部复用 DataService 缓存, Futu RPC 总计 3-4 次
        """
        ds = get_data_service()

        # 并行获取所有原始数据 (DataService 内部有 TTL cache)
        acc_raw, pos_raw, spy_snap, vix = await asyncio.gather(
            ds.get_account_raw(),
            ds.get_positions_raw(),
            ds.get_market_snapshot(["US.SPY"], ttl=30),
            ds.get_vix(),
        )

        svc = ds.svc()
        is_live = svc._is_live() if svc._connected else False
        mode = svc._get_mode_label() if svc._connected else "simulate"

        # ── 1. Account ──
        account = _parse_account(acc_raw)

        # ── 2. Positions with prices ──
        positions = await _parse_positions(ds, pos_raw)

        # ── 3. Market (SPY + VIX) ──
        market = _parse_market(spy_snap, vix)

        # ── 4. Margin ──
        margin = _calc_margin(account)

        # ── 5. Exposure ──
        exposure = _calc_exposure(account)

        # ── 6. PnL ──
        pnl = await _calc_pnl(ds, positions)

        # ── 7. Stop distances ──
        stop_distances = await _calc_stop_distances(ds, positions, is_live)

        # ── 8. Signal summary ──
        signals = await _calc_signals(ds)

        # ── 9. Risk level ──
        futu_risk = _parse_risk_level(account)

        return {
            "account": account,
            "positions": positions,
            "market": market,
            "margin": margin,
            "exposure": exposure,
            "pnl": pnl,
            "stop_distances": stop_distances,
            "signals": signals,
            "futu_risk": futu_risk,
            "mode": mode,
            "is_live": is_live,
            "timestamp": datetime.now().isoformat(),
        }

    app.include_router(router)


# ═══════════════════════════════════════════════════════════
#  内部解析函数 (纯计算, 不直接调 Futu)
# ═══════════════════════════════════════════════════════════

def _is_live() -> bool:
    """检查是否实盘模式 (30s TTL 缓存, 避免频繁读文件)
    
    Bug fix (2026-06-11): 路径少一层 .parent，解析到 app/.env.us (不存在)，
    导致永远返回 False → 查询 SIMULATE 账户。
    dashboard.py 在 routes/ 子目录，需要 4 层 .parent 才能到项目根。
    改为复用 manual_order_service._is_live() 统一入口。
    """
    import time as _time
    now = _time.time()
    if hasattr(_is_live, "_cache") and now - _is_live._cache_time < 30:
        return _is_live._cache
    try:
        from app.web_dashboard.manual_order_service import get_manual_order_service
        result = get_manual_order_service()._is_live()
    except Exception:
        # fallback: 正确路径 (dashboard.py → routes → web_dashboard → app → root)
        from pathlib import Path
        env_file = Path(__file__).parent.parent.parent.parent / ".env.us"
        result = False
        if env_file.exists():
            with open(env_file) as f:
                for line in f:
                    if line.strip().startswith("TRADE_ENV="):
                        result = line.strip().split("=", 1)[1].strip() == "0"
                        break
    _is_live._cache = result
    _is_live._cache_time = now
    return result


def _parse_account(raw: dict) -> dict:
    """从 Futu accinfo row dict 解析账户信息 (统一 USD)"""
    if not raw:
        return {}

    currency = str(raw.get("currency", "")).upper()
    is_hkd = currency == "HKD"
    fx = 7.8 if is_hkd else 1.0

    usd_assets = safe_float(raw.get("usd_assets")) if is_hkd else safe_float(raw.get("total_assets"))
    usd_cash = safe_float(raw.get("us_cash")) if is_hkd else safe_float(raw.get("cash"))
    usd_power = safe_float(raw.get("usd_net_cash_power")) if is_hkd else safe_float(raw.get("power"))
    if usd_power <= 0 and is_hkd:
        usd_power = safe_float(raw.get("power")) / fx

    long_mv = safe_float(raw.get("long_mv"))
    short_mv = abs(safe_float(raw.get("short_mv")))
    if is_hkd:
        long_mv = long_mv / fx
        short_mv = short_mv / fx

    return {
        "total_assets": round(usd_assets, 2),
        "cash": round(usd_cash, 2),
        "market_val": round(long_mv + short_mv, 2),
        "power": round(usd_power, 2),
        "long_mv": round(long_mv, 2),
        "short_mv": round(short_mv, 2),
        "initial_margin": round(safe_float(raw.get("initial_margin")) / fx, 2),
        "maintenance_margin": round(safe_float(raw.get("maintenance_margin")) / fx, 2),
        "margin_call_margin": round(safe_float(raw.get("margin_call_margin")) / fx, 2),
        "frozen_cash": round(safe_float(raw.get("frozen_cash")) / fx, 2),
        "risk_status": str(raw.get("risk_status", "N/A")),
        "interest_charged": round(safe_float(raw.get("interest_charged_amount")) / fx, 2),
        "currency": "USD",
        "raw_currency": currency,
    }


async def _parse_positions(ds, pos_raw: list) -> list:
    """从 Futu position rows 解析持仓 + 补充实时价格"""
    if not pos_raw:
        return []

    # 提取有持仓的 codes
    codes = []
    for p in pos_raw:
        code = str(p.get("code", ""))
        qty = float(p.get("qty", 0))
        if qty != 0 and code.startswith("US."):
            codes.append(code)

    # 批量获取报价
    price_map = await ds.get_batch_quotes(codes) if codes else {}

    # 从 DB 补充 owner 字段
    owner_map = {}
    try:
        rows = await ds.db_query("SELECT symbol, COALESCE(owner,'auto') as owner FROM open_positions")
        owner_map = {r["symbol"]: r["owner"] for r in rows}
    except Exception:
        pass

    positions = []
    for p in pos_raw:
        code = str(p.get("code", ""))
        qty = float(p.get("qty", 0))
        if qty == 0 or not code.startswith("US."):
            continue

        cost = safe_float(p.get("cost_price", 0))
        cur = price_map.get(code, safe_float(p.get("cur_price", 0)))
        if cur <= 0:
            cur = safe_float(p.get("val_price", 0)) / abs(qty) if qty != 0 else 0

        side = str(p.get("position_side", "")).upper()
        is_short = (qty < 0) or (side == "SHORT")
        is_long = not is_short
        mv = abs(cur * qty)

        # 手动计算盈亏 (Futu 盘后返回 0)
        unrealized = safe_float(p.get("unrealized_pl", 0))
        if unrealized == 0 and cur > 0 and cost > 0:
            if is_long:
                unrealized = (cur - cost) * abs(qty)
            else:
                unrealized = (cost - cur) * abs(qty)

        pnl_pct = 0
        if cost > 0 and cur > 0:
            pnl_pct = ((cur - cost) / cost * 100) if is_long else ((cost - cur) / cost * 100)

        positions.append({
            "code": code,
            "symbol": code,
            "qty": abs(qty),
            "direction": "LONG" if is_long else "SHORT",
            "cost_price": round(cost, 2),
            "cur_price": round(cur, 2),
            "market_val": round(mv, 2),
            "market_value": round(mv, 2),
            "unrealized_pl": round(unrealized, 2),
            "unrealized_pnl": round(unrealized, 2),
            "pnl_pct": round(pnl_pct, 2),
            "owner": owner_map.get(code, "auto"),
        })

    # 按浮盈绝对值排序
    positions.sort(key=lambda x: abs(x["unrealized_pnl"]), reverse=True)
    return positions


def _parse_market(spy_snap: dict, vix) -> dict:
    result = {"spy": None, "vix": vix}
    spy_row = spy_snap.get("US.SPY")
    if spy_row:
        cur = safe_float(spy_row.get("cur_price"))
        if cur <= 0:
            cur = safe_float(spy_row.get("last_price"))
        prev = safe_float(spy_row.get("prev_close_price"))
        if cur > 0 and prev > 0:
            result["spy"] = {
                "price": round(cur, 2),
                "change": round(cur - prev, 2),
                "change_pct": round((cur - prev) / prev * 100, 2),
            }
    return result


def _calc_margin(account: dict) -> dict:
    if not account:
        return {}
    total = account.get("total_assets", 0)
    init_margin = account.get("initial_margin", 0)
    market_val = account.get("market_val", 0)

    if total > 0 and init_margin > 0:
        usage = round(init_margin / total * 100, 1)
    elif total > 0 and market_val > 0:
        usage = round(market_val / total * 100, 1)
    else:
        usage = 0

    return {
        "usage_pct": usage,
        "available_power": account.get("power", 0),
        "initial_margin": init_margin,
        "maintenance_margin": account.get("maintenance_margin", 0),
        "margin_call": account.get("margin_call_margin", 0),
        "frozen_cash": account.get("frozen_cash", 0),
    }


def _calc_exposure(account: dict) -> dict:
    if not account:
        return {}
    long_mv = account.get("long_mv", 0)
    short_mv = abs(account.get("short_mv", 0))
    net = long_mv - short_mv
    gross = long_mv + short_mv
    return {
        "long_mv": long_mv,
        "short_mv": short_mv,
        "net": net,
        "gross": gross,
        "net_ratio": round(net / gross * 100, 1) if gross > 0 else 0,
    }


async def _calc_pnl(ds, positions: list) -> dict:
    pnl = {
        "realized": 0,
        "unrealized": 0,
        "total": 0,
        "today_trades": 0,
    }
    # DB 已实现盈亏
    try:
        row = await ds.db_query_one(
            "SELECT COALESCE(SUM(realized_pnl),0) as pnl, COUNT(*) as cnt "
            "FROM trade_records "
            "WHERE trade_date = (SELECT MAX(trade_date) FROM trade_records)"
        )
        if row:
            pnl["realized"] = float(row.get("pnl", 0))
            pnl["today_trades"] = int(row.get("cnt", 0))
    except Exception:
        pass

    # 持仓浮盈
    pnl["unrealized"] = round(sum(p.get("unrealized_pnl", 0) for p in positions), 2)
    pnl["total"] = round(pnl["realized"] + pnl["unrealized"], 2)
    return pnl


async def _calc_stop_distances(ds, positions: list, is_live: bool) -> list:
    """计算止损距离"""
    stop_distances = []

    if is_live:
        # 实盘: 用 ATR 估算 (DB 中无 SL/TP)
        for p in positions:
            cur = p.get("cur_price", 0)
            cost = p.get("cost_price", 0)
            if cur <= 0 or cost <= 0:
                continue
            direction = p["direction"]
            atr_est = cur * 0.02
            sl = cost - atr_est * 2 if direction == "LONG" else cost + atr_est * 2
            tp = cost + atr_est * 4 if direction == "LONG" else cost - atr_est * 4

            if direction == "LONG":
                sl_dist = (cur - sl) / cur * 100
                tp_dist = (tp - cur) / cur * 100
            else:
                sl_dist = (sl - cur) / cur * 100
                tp_dist = (cur - tp) / cur * 100

            stop_distances.append({
                "symbol": p["code"],
                "direction": direction,
                "size": p["qty"],
                "entry": cost,
                "current": cur,
                "stop_loss": round(sl, 2),
                "take_profit": round(tp, 2),
                "sl_distance_pct": round(sl_dist, 2),
                "tp_distance_pct": round(tp_dist, 2),
                "unrealized_pnl": p.get("unrealized_pnl", 0),
                "danger": sl_dist < 2,
            })
    else:
        # 模拟盘: 从 DB 读取 SL/TP
        try:
            rows = await ds.db_query("""
                SELECT symbol, direction, size, avg_entry_price, current_sl, initial_tp
                FROM open_positions
                WHERE sync_status IS NULL OR sync_status NOT IN (
                    'closed_by_sync','closed_by_scheduler','closed_by_daemon',
                    'closed_emergency','stale_not_in_broker'
                )
                ORDER BY size DESC
            """)
            price_map = {p["code"]: p["cur_price"] for p in positions if p.get("cur_price", 0) > 0}

            for r in rows:
                sym = r["symbol"]
                cur = price_map.get(sym, 0)
                sl = float(r.get("current_sl") or 0)
                tp = float(r.get("initial_tp") or 0)
                entry = float(r.get("avg_entry_price") or 0)
                direction = r["direction"]

                if cur <= 0 or sl <= 0:
                    continue

                if direction == "LONG":
                    sl_dist = (cur - sl) / cur * 100
                    tp_dist = (tp - cur) / cur * 100 if tp > 0 else 0
                else:
                    sl_dist = (sl - cur) / cur * 100
                    tp_dist = (cur - tp) / cur * 100 if tp > 0 else 0

                size = float(r.get("size") or 0)
                upnl = (cur - entry) * size if direction == "LONG" else (entry - cur) * size

                stop_distances.append({
                    "symbol": sym,
                    "direction": direction,
                    "size": size,
                    "entry": entry,
                    "current": cur,
                    "stop_loss": sl,
                    "take_profit": tp,
                    "sl_distance_pct": round(sl_dist, 2),
                    "tp_distance_pct": round(tp_dist, 2),
                    "unrealized_pnl": round(upnl, 2),
                    "danger": sl_dist < 2,
                })
        except Exception as e:
            logger.error(f"[snapshot] stop distance calc failed: {e}")

    stop_distances.sort(key=lambda x: (not x["danger"], x["sl_distance_pct"]))
    return stop_distances


async def _calc_signals(ds) -> dict:
    """今日信号摘要"""
    today = et_today()
    result = {"signal_count": 0, "passed_count": 0, "executed_count": 0, "pass_rate": 0, "watchlist": []}

    # 信号数 (from signal_snapshots)
    try:
        from app.analysis.signal_ic_tracker import _get_db_path
        snap_db = str(_get_db_path())
        row = await ds.db_query_one(
            "SELECT COUNT(*) as cnt FROM signal_snapshots WHERE trade_date = ?",
            (today,), db_path=snap_db,
        )
        result["signal_count"] = row["cnt"] if row else 0
        row2 = await ds.db_query_one(
            "SELECT COUNT(*) as cnt FROM signal_snapshots WHERE trade_date = ? AND strength >= 30 AND confidence > 0",
            (today,), db_path=snap_db,
        )
        result["passed_count"] = row2["cnt"] if row2 else 0
    except Exception:
        pass

    # 执行数
    try:
        row3 = await ds.db_query_one(
            "SELECT COUNT(*) as cnt FROM trade_records WHERE trade_date = ?", (today,),
        )
        result["executed_count"] = row3["cnt"] if row3 else 0
    except Exception:
        pass

    if result["signal_count"] > 0:
        result["pass_rate"] = round(result["passed_count"] / result["signal_count"] * 100, 1)

    return result


def _parse_risk_level(account: dict) -> dict:
    rl = account.get("risk_status", "N/A") if account else "N/A"
    risk_levels = {
        "LEVEL1": {"label": "安全", "color": "green", "desc": "账户资金充足"},
        "LEVEL2": {"label": "注意", "color": "yellow", "desc": "保证金使用较高"},
        "LEVEL3": {"label": "警告", "color": "orange", "desc": "接近维持保证金"},
        "LEVEL4": {"label": "危险", "color": "red", "desc": "低于维持保证金"},
        "LEVEL5": {"label": "强平", "color": "red", "desc": "面临强制平仓"},
    }
    return {
        "risk_level": rl,
        "interest_charged": account.get("interest_charged", 0) if account else 0,
        "detail": risk_levels.get(rl, {"label": rl, "color": "gray", "desc": "未知状态"}),
    }
