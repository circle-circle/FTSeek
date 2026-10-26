"""
Dashboard 路由 — 指挥中心首页 + 统一 Snapshot 端点

核心改进:
  /api/dashboard/snapshot 一次返回所有首页数据,
  替代原来 8+ 并行 API 调用 (risk_status + live-account + signal-dashboard + market-temp + positions-attribution)
  Futu RPC 从 10+ 次降到 3 次 (accinfo + positions + snapshot_batch)
"""
import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.web_dashboard.data_service import (
    get_data_service, safe_float, et_today,
)
from app.execution.futu_accounting import normalize_futu_account
from app.metrics import SourceTaxonomy

logger = logging.getLogger("ftseek_us.routes.dashboard")
router = APIRouter()

# 统一口径: 以下 source_type 为已废弃/禁用, 不纳入 Dashboard 统计
EXCLUDED_SOURCE_TYPES = SourceTaxonomy.EXCLUDED


def register(app, templates, dashboard):
    """注册 Dashboard 路由"""

    @router.get("/", response_class=HTMLResponse)
    async def dashboard_home(request: Request):
        is_live = _is_live()
        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "title": "FTSeek-US Dashboard",
            "is_live": is_live,
            "current_time": datetime.now().isoformat(),
        })

    @router.get("/api/account")
    async def get_account():
        return dashboard.get_account_summary()

    @router.get("/health")
    async def health_check_root():
        # 监控/负载均衡探针 (公开, 无需鉴权) — 2026-06-18 修复监控 400
        return {"status": "ok", "timestamp": datetime.now().isoformat()}

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
        acc_raw, pos_raw, orders_raw, spy_snap, vix = await asyncio.gather(
            ds.get_account_raw(),
            ds.get_positions_raw(),
            ds.get_today_orders_raw(),
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

        # ── 2b. Today's orders ──
        today_orders = _parse_today_orders(orders_raw)

        # ── 3. Market (SPY + VIX) ─
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

        # ── 10. Earnings calendar for positions ──
        position_symbols = [p["symbol"].replace("US.", "") for p in positions]
        earnings = _get_position_earnings(position_symbols)

        # ── 11. Lifecycle (thesis + recommendations) ──
        lifecycle = await _calc_lifecycle(ds, positions, stop_distances, earnings)

        # ── 12. Enrich lifecycle with equity_pct, pnl_usd, take_profit ──
        equity = float(account.get("total_assets", 0) or 0)
        sd_map = {s["symbol"]: s for s in stop_distances}
        for lc in lifecycle:
            sym = lc.get("symbol", "")
            sd = sd_map.get(sym, {})
            qty = float(lc.get("quantity", 0) or 0)
            entry = float(lc.get("entry_price", 0) or 0)
            cur = float(lc.get("current_price", 0) or 0)
            direction = lc.get("direction", "LONG")
            # PnL USD
            if entry > 0 and cur > 0 and qty > 0:
                if direction == "LONG":
                    lc["pnl_usd"] = round((cur - entry) * qty, 2)
                else:
                    lc["pnl_usd"] = round((entry - cur) * qty, 2)
            else:
                lc["pnl_usd"] = float(lc.get("unrealized_pnl", 0) or 0)
            # Equity % (position value / total equity)
            mv = cur * qty if cur > 0 and qty > 0 else 0
            lc["equity_pct"] = round(mv / equity * 100, 1) if equity > 0 else 0
            # Take profit from stop_distances
            lc["take_profit"] = sd.get("take_profit", 0)
            lc["tp_distance_pct"] = sd.get("tp_distance_pct", 0)
            lc["atr_estimated"] = sd.get("atr_estimated", False)

        # ── 13. Exposure ratio (gross / equity) ──
        gross = float(exposure.get("gross", 0) or 0)
        exposure["equity_ratio"] = round(gross / equity * 100, 1) if equity > 0 else 0

        # ── 14. Today's closed trades ──
        # 2026-08-28 出场日记账 + 净额: 原按 trade_date (入场日) 归集而本段语义是
        # "closed today" (隔夜仓今日了结漏列), 且 pnl_usd 取毛额 — 该字段同时喂
        # 下方 OKR today 胜/负/合计 (_load_okr_summary)。
        today_closed = []
        today_closed_total = 0
        try:
            _today_et = et_today()
            _excluded = ", ".join(f"'{s}'" for s in EXCLUDED_SOURCE_TYPES)
            _cnt_row = await ds.db_query(
                f"SELECT COUNT(*) AS c FROM trade_records "
                f"WHERE exit_time IS NOT NULL AND exit_price > 0 AND substr(exit_time, 1, 10) = ? "
                f"AND source_type NOT IN ({_excluded})",
                (_today_et,),
            )
            today_closed_total = (_cnt_row[0].get("c", 0) if _cnt_row else 0)
            rows = await ds.db_query(
                f"SELECT symbol, direction, entry_price, exit_price, pnl_pct, "
                f"COALESCE(pnl_net, realized_pnl) AS pnl_net_val, "
                f"exit_reason, exit_time, holding_hours, source_type FROM trade_records "
                f"WHERE exit_time IS NOT NULL AND exit_price > 0 AND substr(exit_time, 1, 10) = ? "
                f"AND source_type NOT IN ({_excluded}) "
                f"ORDER BY exit_time DESC LIMIT 200",
                (_today_et,),
            )
            for r in rows:
                _et = r.get("exit_time") or ""
                # exit_time 形如 "2026-06-23T22:31:49.944242" → 取 HH:MM (旧 [-8:-3] 切到微秒导致乱码)
                _hhmm = _et[11:16] if len(_et) >= 16 else _et[-5:]
                today_closed.append({
                    "symbol": (r.get("symbol") or "").replace("US.", ""),
                    "direction": r.get("direction", ""),
                    "pnl_pct": round(float(r.get("pnl_pct", 0) or 0) * 100, 1),
                    "pnl_usd": round(float(r.get("pnl_net_val", 0) or 0), 2),
                    "exit_reason": r.get("exit_reason", ""),
                    "exit_time": _hhmm,
                    "holding_hours": round(float(r.get("holding_hours") or 0), 2),
                    "entry_price": round(float(r.get("entry_price") or 0), 2),
                    "exit_price": round(float(r.get("exit_price") or 0), 2),
                    "source_type": r.get("source_type", ""),
                })
        except Exception:
            pass

        # ── 15. Weekly PnL (本周已实现盈亏) ──
        weekly_pnl = await _calc_weekly_pnl(ds)

        # ── 16. Decision summary (决策摘要) ──
        decision = _calc_decision_summary(market, margin, pnl, weekly_pnl)

        # ── 17. Concentration (行业集中度) ──
        concentration = _calc_concentration(positions, equity)

        # ── 18. OKR 进度摘要 (从 okr_latest.json, okr_baseline 每次跑更新) ──
        okr_summary = _load_okr_summary(today_closed)

        # ── 19. Graduation 门禁 + 因子性能 + auto_account (Phase 3 打磨循环眼睛) ──
        graduation = None
        factor_stats = []
        auto_account = "sim"
        try:
            from app.playbook.pipeline import _check_graduation_gate
            graduation = _check_graduation_gate()
        except Exception as _ge:
            logger.debug(f"[dashboard] graduation 查询失败: {_ge}")
        try:
            from app.playbook.playbook_service import get_playbook_service
            _fs = get_playbook_service().get_factor_stats() or {}
            factor_stats = _fs.get("single", []) if isinstance(_fs, dict) else (_fs or [])
        except Exception as _fe:
            logger.debug(f"[dashboard] factor_stats 查询失败: {_fe}")
        try:
            from app.web_dashboard.mode_switch_service import get_mode_switch_service
            auto_account = get_mode_switch_service().get_mode_status().get("auto_account", "sim")
        except Exception:
            pass

        return {
            "account": account,
            "positions": positions,
            "today_orders": today_orders,
            "today_closed": today_closed,
            "today_closed_total": today_closed_total,
            "market": market,
            "margin": margin,
            "exposure": exposure,
            "pnl": pnl,
            "stop_distances": stop_distances,
            "signals": signals,
            "futu_risk": futu_risk,
            "earnings": earnings,
            "lifecycle": lifecycle,
            "decision": decision,
            "weekly_pnl": weekly_pnl,
            "concentration": concentration,
            "okr_summary": okr_summary,
            "graduation": graduation,
            "factor_stats": factor_stats,
            "auto_account": auto_account,
            "mode": mode,
            "is_live": is_live,
            "timestamp": datetime.now().isoformat(),
        }

    # ── Thesis CRUD (持仓生命周期) ──

    @router.post("/api/positions/{symbol}/thesis")
    async def update_position_thesis(symbol: str, request: Request):
        """更新持仓thesis/失效价/预期天数"""
        ds = get_data_service()
        body = await request.json()
        from app.lifecycle.engine import update_thesis
        result = await update_thesis(
            ds,
            symbol=symbol,
            thesis=body.get("thesis"),
            invalidation_price=body.get("invalidation_price"),
            expected_hold_days=body.get("expected_hold_days"),
        )
        return result

    @router.get("/api/positions/{symbol}/thesis")
    async def get_position_thesis(symbol: str):
        """获取持仓thesis数据"""
        ds = get_data_service()
        from app.lifecycle.engine import get_lifecycle_map
        lc_map = await get_lifecycle_map(ds)
        data = lc_map.get(symbol, {})
        return {"symbol": symbol, **data}

    @router.post("/api/positions/{symbol}/challenge")
    async def challenge_thesis_api(symbol: str, request: Request):
        """LLM审视thesis — 结构化输出，4h缓存"""
        ds = get_data_service()
        body = await request.json() if request.headers.get("content-type") == "application/json" else {}
        thesis = body.get("thesis", "")
        force = body.get("force", False)

        if not thesis:
            # Read from DB
            from app.lifecycle.engine import get_lifecycle_map
            lc_map = await get_lifecycle_map(ds)
            thesis = lc_map.get(symbol, {}).get("thesis", "")

        if not thesis or thesis.strip() == "":
            return {"error": "无thesis可审视，请先填写进场理由"}

        from app.llm.thesis_challenge import challenge_thesis, get_cached_challenge

        # Check cache (unless force refresh)
        if not force:
            cached = get_cached_challenge(symbol)
            if cached:
                return cached

        # Get position context
        from app.lifecycle.engine import get_lifecycle_map
        lc_map = await get_lifecycle_map(ds)
        lc = lc_map.get(symbol, {})

        # Get bear cases from battle plan if available
        # 2026-08-01 断线: battle plan 07-11 冻结, 陈旧 rationale 无 stale 检查直接当 bear_cases 喂 LLM 系误导。
        bear_cases = ""

        # Call LLM (in thread pool to not block event loop)
        import asyncio
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: challenge_thesis(
                symbol=symbol,
                thesis=thesis,
                direction=lc.get("direction", ""),
                cur_price=lc.get("cur_price", 0),
                cost_price=lc.get("cost_price", 0),
                pnl_pct=lc.get("pnl_pct", 0),
                hold_days=lc.get("hold_days") or 0,
                bear_cases=bear_cases,
            )
        )
        return result

    @router.get("/api/positions/{symbol}/challenge")
    async def get_challenge_api(symbol: str):
        """获取已缓存的thesis挑战结果"""
        from app.llm.thesis_challenge import get_cached_challenge
        cached = get_cached_challenge(symbol)
        return cached or {"cached": False}

    # ── Protection API (止损/追踪止盈 — 人工确认模式) ──

    @router.post("/api/protection/stoploss/{symbol}")
    async def set_stop_loss(symbol: str, request: Request):
        """设置/更新止损价"""
        ds = get_data_service()
        body = await request.json()
        price = body.get("price")
        if price is None:
            return {"success": False, "error": "price required"}
        # SIM 隔离 (2026-09-05, DEB-20260905 §13.15): 看板是**实盘**手动台, 而这个 DB 由
        # TRADE_ENV 路由到 sim 库。不拦的后果: 给实盘仓设止损会**改写 sim 数据收集仓的 SL**
        # (污染数据集, 还可能让 sim 仓被提前止损), 而真正的实盘仓依旧裸奔、用户毫不知情。
        from app.web_dashboard.env_scope import is_simulate_row, real_only
        db_path = ds._get_db()
        if is_simulate_row(db_path, symbol):
            logger.critical(
                f"[EnvIsolation] 设置止损被拦截: {symbol} 是 SIM 数据收集仓 "
                f"(看板=实盘台, 不改写 sim 行) — DEB-20260905 §13.15"
            )
            return {
                "success": False, "symbol": symbol,
                "error": f"{symbol} 是 SIM 数据收集仓, 看板(实盘台)不改写其止损 — DEB-20260905 §13.15",
            }
        rows = await ds.db_execute(
            "UPDATE open_positions SET stop_loss_price = ?, stop_loss_triggered = 0 "
            f"WHERE symbol = ? AND {real_only(db_path)}",
            (float(price), symbol)
        )
        return {"success": True, "symbol": symbol, "stop_loss_price": float(price),
                "rows_affected": rows}

    @router.post("/api/protection/confirm/{symbol}")
    async def confirm_protection(symbol: str, request: Request):
        """确认止损/追踪止盈 → 执行平仓"""
        from app.web_dashboard.manual_order_service import get_manual_order_service
        svc = get_manual_order_service()
        # 判断触发类型
        body = await request.json() if request.headers.get("content-type") == "application/json" else {}
        reason = body.get("reason", "stop_loss")
        result = svc.close_position(symbol, reason=reason)
        if result.get("success"):
            # 发飞书通知已执行
            try:
                from scripts.protection_monitor import send_feishu
                bare = symbol.replace("US.", "")
                send_feishu(f"✅ {bare} 已确认平仓\n理由: {reason}\n订单: {result.get('order_id', 'N/A')}")
            except Exception:
                pass
        return result

    @router.post("/api/protection/cancel/{symbol}")
    async def cancel_protection(symbol: str):
        """取消告警（清除triggered标记，继续监控）"""
        ds = get_data_service()
        # SIM 隔离 (DEB-20260905 §13.15): 同上 — 清 triggered 标记不得落到 sim 行
        from app.web_dashboard.env_scope import is_simulate_row, real_only
        db_path = ds._get_db()
        if is_simulate_row(db_path, symbol):
            logger.critical(
                f"[EnvIsolation] 取消告警被拦截: {symbol} 是 SIM 数据收集仓 — DEB-20260905 §13.15"
            )
            return {
                "success": False, "symbol": symbol,
                "error": f"{symbol} 是 SIM 数据收集仓, 看板(实盘台)不改写其告警标记 — DEB-20260905 §13.15",
            }
        rows = await ds.db_execute(
            "UPDATE open_positions SET stop_loss_triggered = 0, trailing_stop_triggered = 0 "
            f"WHERE symbol = ? AND {real_only(db_path)}",
            (symbol,)
        )
        return {"success": True, "symbol": symbol, "rows_affected": rows}

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

    try:
        converted = normalize_futu_account(raw)
    except ValueError as e:
        logger.warning(f"[Dashboard] Futu 账户币种转换失败: {e}", exc_info=True)
        return {}

    market_val = converted["market_val"]
    if market_val == 0:
        market_val = converted["long_mv"] + converted["short_mv"]
    margin_usage = (
        round(converted["initial_margin"] / converted["total_assets"] * 100, 1)
        if converted["total_assets"] > 0 else 0
    )

    return {
        "total_assets": converted["total_assets"],
        "cash": converted["cash"],
        "avl_cash": converted["avl_withdrawal_cash"],
        "market_val": round(market_val, 2),
        "power": converted["power"],
        "long_mv": converted["long_mv"],
        "short_mv": converted["short_mv"],
        "initial_margin": converted["initial_margin"],
        "maintenance_margin": converted["maintenance_margin"],
        "margin_call_margin": converted["margin_call_margin"],
        "frozen_cash": converted["frozen_cash"],
        "margin_usage": margin_usage,
        "risk_status": str(raw.get("risk_status", "N/A")),
        # P1 (DEB-20261025): broker-native risk fields, read-only passthrough.
        # Amount fields are not re-FX'd here; FUTU already reports them in its
        # account risk currency (REAL often returns N/A for some of them).
        "risk_level": converted.get("risk_level", "N/A"),
        "exposure_level": converted.get("exposure_level", "N/A"),
        "exposure_limit": converted.get("exposure_limit", "N/A"),
        "used_limit": converted.get("used_limit", "N/A"),
        "remaining_limit": converted.get("remaining_limit", "N/A"),
        "is_pdt": converted.get("is_pdt", "N/A"),
        "pdt_seq": converted.get("pdt_seq", "N/A"),
        "beginning_dtbp": converted.get("beginning_dtbp", "N/A"),
        "dt_status": converted.get("dt_status", "N/A"),
        "remaining_dtbp": converted.get("remaining_dtbp", "N/A"),
        "dt_call_amount": converted.get("dt_call_amount", "N/A"),
        "risk_fields_currency": "broker_native",
        "interest_charged": converted["interest_charged_amount"],
        "currency": "USD",
        "account_scope": converted["account_scope"],
        "raw_currency": converted["raw_currency"],
        "fx": converted["fx"],
        "fx_estimated": converted["_fx_source"] == "fallback",
    }


async def _parse_positions(ds, pos_raw: list) -> list:
    """从 Futu position rows 解析持仓 + 补充实时价格"""
    if not pos_raw:
        return []

    # ── 中文名映射 (覆盖 30+ 常用美股, 未命中 fallback 到 ticker) ──
    _ZH_MAP = {
        "AAPL": "苹果", "BABA": "阿里巴巴", "NVDA": "英伟达", "TSLA": "特斯拉",
        "MSFT": "微软", "GOOG": "谷歌", "AMZN": "亚马逊", "META": "Meta",
        "AMD": "AMD", "INTC": "英特尔", "MU": "美光科技", "TSM": "台积电",
        "AVGO": "博通", "QCOM": "高通", "ORCL": "甲骨文", "DIS": "迪士尼",
        "JPM": "摩根大通", "GS": "高盛", "WMT": "沃尔玛", "JNJ": "强生",
        "PFE": "辉瑞", "MRK": "默沙东", "LLY": "礼来", "BA": "波音",
        "PDD": "拼多多", "JD": "京东", "NIO": "蔚来", "XPEV": "小鹏",
        "LI": "理想", "NFLX": "Netflix", "CRM": "Salesforce",
        "F": "福特", "GM": "通用汽车", "XOM": "埃克森美孚", "CVX": "雪佛龙",
        "BAC": "美国银行",
    }

    # 提取有持仓的 codes
    codes = []
    for p in pos_raw:
        code = str(p.get("code", ""))
        qty = float(p.get("qty", 0))
        if qty != 0 and code.startswith("US."):
            # Filter out options: codes with digits after ticker (e.g. US.SNDK260620C75000)
            bare = code[3:]
            if any(c.isdigit() for c in bare):
                continue
            codes.append(code)

    # 批量获取报价 (含基本面+资金流字段, 一次snapshot调用)
    detailed_map = await ds.get_batch_quotes_detailed(codes) if codes else {}
    price_map = {sym: d.get("cur_price", 0) for sym, d in detailed_map.items() if d.get("cur_price", 0) > 0}

    # 从 DB 补充 owner + protection 字段
    owner_map = {}
    db_symbols = set()
    protection_map = {}
    protection_known = False
    entry_time_map = {}
    try:
        rows = await ds.get_open_position_owners()
        owner_map = {r["symbol"]: r["owner"] for r in rows}
        db_symbols = set(owner_map.keys())
        entry_time_map = {r["symbol"]: r.get("entry_time") for r in rows}
    except Exception:
        pass
    try:
        # 先探列, 兼容旧库缺 sl_broker_order_id/current_sl 的形态; 探列成功才宣称
        # "保护状态已知", 否则前端显示 unknown, 不把查询失败伪装成"无保护"。
        col_rows = await ds.db_query("PRAGMA table_info(open_positions)")
        cols = {str(r.get("name") or "") for r in col_rows}
        wanted = [
            "symbol", "current_sl", "stop_loss_price", "initial_tp",
            "trailing_stop_price", "sl_broker_order_id", "tp_broker_order_id",
            "stop_loss_triggered", "trailing_stop_triggered",
        ]
        selected = [c for c in wanted if c in cols]
        # 至少要有一种止损来源列才允许宣称“保护状态已知”。只有
        # stop_loss_triggered 之类的旁证列不能证明“已查询止损位/券商单”。
        has_sl_schema = bool(cols & {"current_sl", "stop_loss_price", "sl_broker_order_id"})
        if "symbol" in cols and has_sl_schema:
            prot_rows = await ds.db_query(
                f"SELECT {', '.join(selected)} FROM open_positions"
            )
            protection_map = {r["symbol"]: r for r in prot_rows}
            protection_known = True
    except Exception:
        protection_known = False

    positions = []
    for p in pos_raw:
        code = str(p.get("code", ""))
        qty = float(p.get("qty", 0))
        if qty == 0 or not code.startswith("US."):
            continue
        # Filter out options: codes with digits after ticker (e.g. US.SNDK260620C75000)
        bare = code[3:]
        if any(c.isdigit() for c in bare):
            continue
        # Phase B (2026-07-12): 实盘页防御性过滤 — 绝不渲染模拟账户持仓 (DB 兜底若漏过 sim 仓)
        if str(p.get("account_type", "")).lower() == "simulate":
            continue

        cost = safe_float(p.get("cost_price", 0))
        # Phase A (2026-07-12): 追踪现价来源, 供前端如实标注 (实时/成本占位/延迟)
        _is_db_fallback = str(p.get("data_source", "")) == "db_fallback"
        cur = price_map.get(code, safe_float(p.get("cur_price", 0)))
        if code in price_map and price_map[code] > 0:
            price_basis = "live"          # 来自实时 snapshot
        elif _is_db_fallback:
            price_basis = "cost"          # 券商不可用, 用成本价占位
        elif cur > 0:
            price_basis = "delayed"       # Futu 行内价 (可能昨收/盘后)
        else:
            price_basis = "none"
        if cur <= 0:
            cur = safe_float(p.get("val_price", 0)) / abs(qty) if qty != 0 else 0
        # stale: 现价非实时 → 浮盈/今日盈亏不可信, 前端应显示 "--" 而非伪造 $0
        stale = _is_db_fallback or price_basis in ("cost", "none")

        side = str(p.get("position_side", "")).upper()
        is_short = (qty < 0) or (side == "SHORT")
        is_long = not is_short
        mv = abs(cur * qty)

        # 手动计算盈亏 (优先用 pl_val=总盈亏, Futu 盘后返回 0)
        # pl_val = unrealized_pl + realized_pl (包含分批平仓的已实现部分)
        pl_val = safe_float(p.get("pl_val", 0))
        unrealized = safe_float(p.get("unrealized_pl", 0))
        if pl_val != 0:
            # pl_val 是 Futu 计算的总盈亏 (含分批已实现), 与 App 显示一致
            unrealized = pl_val
        elif unrealized == 0 and cur > 0 and cost > 0:
            if is_long:
                unrealized = (cur - cost) * abs(qty)
            else:
                unrealized = (cost - cur) * abs(qty)

        pnl_pct = 0
        if cost > 0 and cur > 0:
            pnl_pct = ((cur - cost) / cost * 100) if is_long else ((cost - cur) / cost * 100)

        # 今日盈亏 (Futu today_pl_val, 盘后为昨日结算盈亏)
        today_pl = safe_float(p.get("today_pl_val", 0))

        # 中文名
        code_short = code.replace("US.", "")
        name_cn = _ZH_MAP.get(code_short, code_short)

        # 行业分类
        sector = _SECTOR_MAP.get(code_short, "其他")

        prot = protection_map.get(code, {})
        _stored_sl = safe_float(prot.get("stop_loss_price", 0))
        _current_sl = safe_float(prot.get("current_sl", 0))
        # 口径统一: 交易中移动止损写 current_sl, 初始/静态止损写 stop_loss_price。
        # 展示以 current_sl 优先, 避免"改过止损但看板仍显示旧值"。
        _effective_sl = _current_sl if _current_sl > 0 else _stored_sl
        _effective_tp = safe_float(prot.get("initial_tp", 0))
        _sl_oid = str(prot.get("sl_broker_order_id") or "").strip()
        _tp_oid = str(prot.get("tp_broker_order_id") or "").strip()
        if not protection_known:
            _protection_state = "unknown"
        elif _sl_oid:
            _protection_state = "broker_attached"
        elif _effective_sl > 0:
            _protection_state = "db_only"
        else:
            _protection_state = "none"
        # P2 (2026-06-21): 从detailed_map注入基本面+资金流字段
        dq = detailed_map.get(code, {})
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
            "today_pl": round(today_pl, 2),
            # Phase A (2026-07-12): 数据来源/新鲜度 — 前端据此显示 "--" 与时段徽标
            "price_basis": price_basis,
            "stale": stale,
            "name_cn": name_cn,
            "sector": sector,
            "owner": owner_map.get(code, "legacy"),
            "legacy": code not in db_symbols,
            "created_at": entry_time_map.get(code),
            "stop_loss_price": _effective_sl if _effective_sl > 0 else None,
            "take_profit_price": _effective_tp if _effective_tp > 0 else None,
            "current_sl": _current_sl if _current_sl > 0 else None,
            "initial_stop_loss_price": _stored_sl if _stored_sl > 0 else None,
            "sl_broker_order_id": _sl_oid or None,
            "tp_broker_order_id": _tp_oid or None,
            "broker_sl_attached": bool(_sl_oid),
            "broker_tp_attached": bool(_tp_oid),
            "protection_state": _protection_state,
            "trailing_stop_price": prot.get("trailing_stop_price"),
            "stop_loss_triggered": bool(prot.get("stop_loss_triggered")),
            "trailing_stop_triggered": bool(prot.get("trailing_stop_triggered")),
            # 富途snapshot扩展字段
            "pe": dq.get("pe_ttm") or dq.get("pe_ratio"),
            "pb": dq.get("pb_ratio"),
            "eps": dq.get("eps"),
            "market_cap": dq.get("market_cap"),
            "bid_ask_ratio": dq.get("bid_ask_ratio"),
            "volume_ratio": dq.get("volume_ratio"),
            "high_52w": dq.get("high_52w"),
            "low_52w": dq.get("low_52w"),
            "turnover_rate": dq.get("turnover_rate"),
        })

    # 按浮盈绝对值排序
    positions.sort(key=lambda x: abs(x["unrealized_pnl"]), reverse=True)
    return positions


def _parse_today_orders(orders_raw: list) -> dict:
    """解析今日订单, 返回 {active: [...], filled: [...], stats: {...}}"""
    if not orders_raw:
        return {"active": [], "filled": [], "stats": {"active": 0, "filled": 0, "cancelled": 0}}

    active = []
    filled = []
    cancelled = 0

    _ACTIVE_STATUSES = {"SUBMITTED", "WAITING_TO_SUBMIT", "MODIFYING", "CANCELLING", "PART_FILLED"}
    _FILLED_STATUSES = {"FILLED"}
    _CANCELLED_STATUSES = {"CANCELLED", "REJECTED", "DELETED"}

    for o in orders_raw:
        status = str(o.get("order_status", "")).upper()
        side = str(o.get("trd_side", "")).upper()
        code = str(o.get("code", ""))
        if not code.startswith("US."):
            continue

        entry = {
            "symbol": code,
            "code_short": code.replace("US.", ""),
            "side": side,
            "status": status,
            "qty": float(o.get("qty", 0)),
            "price": safe_float(o.get("price", 0)),
            "dealt_qty": float(o.get("dealt_qty", 0)),
            "dealt_avg_price": safe_float(o.get("dealt_avg_price", 0)),
            "create_time": str(o.get("create_time", "")),
            "updated_time": str(o.get("updated_time", "")),
            "remark": str(o.get("remark", "")),
        }

        if status in _ACTIVE_STATUSES:
            active.append(entry)
        elif status in _FILLED_STATUSES:
            filled.append(entry)
        elif status in _CANCELLED_STATUSES:
            cancelled += 1

    return {
        "active": active,
        "filled": filled,
        "stats": {"active": len(active), "filled": len(filled), "cancelled": cancelled},
    }


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
        "realized_all": 0,
        "realized_sim": 0,
        "realized_live": 0,
        "unrealized": 0,
        "total": 0,
        "today_trades": 0,
        "today_realized_sim": 0,
        "today_realized_live": 0,
    }
    today_et = et_today()

    # 今日已实现盈亏 (按 trade_mode 拆分模拟/实盘)
    try:
        _excluded = ", ".join(f"'{s}'" for s in EXCLUDED_SOURCE_TYPES)
        # 2026-08-28 出场日记账 + 净额 (同 AccountCircuitBreaker 口径): 原
        # trade_date (入场日) + realized_pnl (毛额) — 隔夜仓今日了结不计入且费用
        # 不可见 (8/27 实证: 面板 10 笔/+381.15 vs 出场日净额 13 笔/+119.37)。
        rows = await ds.db_query(
            f"SELECT trade_mode, COALESCE(SUM(COALESCE(pnl_net, realized_pnl)),0) as pnl, COUNT(*) as cnt "
            f"FROM trade_records "
            f"WHERE substr(exit_time, 1, 10) = ? AND exit_price > 0 "
            f"AND source_type NOT IN ({_excluded}) "
            f"GROUP BY trade_mode",
            (today_et,),
        )
        for r in rows or []:
            mode = str(r.get("trade_mode") or "simulate").lower()
            pnl_val = float(r.get("pnl", 0) or 0)
            cnt = int(r.get("cnt", 0) or 0)
            pnl["realized"] += pnl_val
            pnl["today_trades"] += cnt
            if mode == "live":
                pnl["today_realized_live"] += pnl_val
            else:
                pnl["today_realized_sim"] += pnl_val
    except Exception:
        pass

    # 全部已实现盈亏 (all-time) 同样按 trade_mode 拆分
    try:
        _excluded = ", ".join(f"'{s}'" for s in EXCLUDED_SOURCE_TYPES)
        rows = await ds.db_query(
            f"SELECT trade_mode, COALESCE(SUM(realized_pnl),0) as pnl "
            f"FROM trade_records "
            f"WHERE exit_price > 0 "
            f"AND source_type NOT IN ({_excluded}) "
            f"GROUP BY trade_mode"
        )
        for r in rows or []:
            mode = str(r.get("trade_mode") or "simulate").lower()
            pnl_val = float(r.get("pnl", 0) or 0)
            pnl["realized_all"] += pnl_val
            if mode == "live":
                pnl["realized_live"] += pnl_val
            else:
                pnl["realized_sim"] += pnl_val
    except Exception:
        pass

    # 持仓浮盈
    pnl["unrealized"] = round(sum(p.get("unrealized_pnl", 0) for p in positions), 2)
    # Phase A (2026-07-12): 任一持仓现价非实时 → 浮盈/总盈亏聚合不可信, 前端应显示 "--"
    pnl["positions_stale"] = any(p.get("stale") for p in positions)
    # 总盈亏 = 全部已实现 + 浮盈
    pnl["total"] = round(pnl["realized_all"] + pnl["unrealized"], 2)

    # 今日盈亏 (Futu today_pl_val 汇总)
    # 注意: 盘后 Futu 返回的是 T-1 结算盈亏, 非当日实时盈亏
    pnl["today_pl"] = round(sum(p.get("today_pl", 0) for p in positions), 2)

    # 标记 today_pl 语义 (前端可用于提示用户)
    from app.web_dashboard.data_service import et_now
    _et = et_now()
    _et_mins = _et.hour * 60 + _et.minute
    if 570 <= _et_mins < 960:  # 09:30 ~ 16:00 ET
        pnl["today_pl_label"] = "今日浮动"
    elif 960 <= _et_mins < 1200:  # 16:00 ~ 20:00 ET
        pnl["today_pl_label"] = "盘后(T-1结算)"
    else:
        pnl["today_pl_label"] = "昨日结算"

    return pnl


async def _calc_stop_distances(ds, positions: list, is_live: bool) -> list:
    """计算止损距离"""
    stop_distances = []

    if is_live:
        # 实盘: 使用 ExitMonitor 的真实 ATR (与止损逻辑一致)
        try:
            from app.risk.position_exit_monitor import get_cached_atr as _get_atr
        except Exception:
            _get_atr = lambda _sym: 0.0

        for p in positions:
            cur = p.get("cur_price", 0)
            cost = p.get("cost_price", 0)
            if cur <= 0 or cost <= 0:
                continue
            direction = p["direction"]
            # 优先用 ExitMonitor 缓存的真实 ATR, fallback 到 2% 估算
            raw_atr = _get_atr(p["code"])
            atr = raw_atr if raw_atr > 0 else cur * 0.02
            # SL: entry ± 4.0×ATR (与 atr_stop 一致)
            # TP: entry ± 1.0×ATR (与 scale_out 一致)
            atr_sl = cost - atr * 4 if direction == "LONG" else cost + atr * 4
            atr_tp = cost + atr * 1 if direction == "LONG" else cost - atr * 1
            actual_sl = safe_float(p.get("stop_loss_price", 0))
            actual_tp = safe_float(p.get("take_profit_price", 0))
            if actual_sl > 0:
                sl = actual_sl
                sl_source = ("broker" if p.get("protection_state") == "broker_attached"
                             else "db")
                atr_estimated = False
            else:
                sl = atr_sl
                sl_source = "atr_estimated" if raw_atr <= 0 else "atr"
                atr_estimated = raw_atr <= 0
            if actual_tp > 0:
                tp = actual_tp
                tp_source = ("broker" if p.get("broker_tp_attached") else "db")
            else:
                tp = atr_tp
                tp_source = "atr_estimated" if raw_atr <= 0 else "atr"

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
                "atr_estimated": atr_estimated,
                "sl_source": sl_source,
                "tp_source": tp_source,
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
        counts = await ds.get_signal_counts(today)
        result["signal_count"] = counts["total"]
        result["passed_count"] = counts["qualified"]
    except Exception:
        pass

    # 执行数
    try:
        result["executed_count"] = await ds.get_executed_count(today)
    except Exception:
        pass

    if result["signal_count"] > 0:
        result["pass_rate"] = round(result["passed_count"] / result["signal_count"] * 100, 1)

    return result


async def _calc_lifecycle(ds, positions: list, stop_distances: list, earnings: list) -> list:
    """生成持仓生命周期数据 (thesis + recommendations + sector peers + sentiment)"""
    try:
        from app.lifecycle.engine import get_lifecycle_map, generate_recommendations, collect_all_peer_symbols
        lifecycle_map = await get_lifecycle_map(ds)
        results = generate_recommendations(positions, stop_distances, earnings, lifecycle_map)

        # Batch-fetch peer quotes and attach to each lifecycle item
        peer_symbols = collect_all_peer_symbols(results, max_per_position=4)
        if peer_symbols:
            peer_codes = [f"US.{s}" for s in peer_symbols]
            peer_quotes = await ds.get_batch_quotes_detailed(peer_codes)
        else:
            peer_quotes = {}

        # Attach peer price data to each result
        for r in results:
            for group in r.get("sector_peers", []):
                enriched_peers = []
                for peer_ticker in group["peers"][:4]:
                    code = f"US.{peer_ticker}"
                    q = peer_quotes.get(code, {})
                    cur_price = q.get("cur_price", 0)
                    prev_close = q.get("prev_close", 0)
                    change_pct = 0
                    if cur_price and prev_close:
                        change_pct = round((cur_price - prev_close) / prev_close * 100, 2)
                    enriched_peers.append({
                        "ticker": peer_ticker,
                        "price": round(cur_price, 2) if cur_price else None,
                        "change_pct": change_pct,
                    })
                group["peers"] = enriched_peers

        # Attach social sentiment from cache (populated by cron job)
        sentiment_map = _load_sentiment_cache([r["symbol"] for r in results])
        for r in results:
            short = r["symbol"].replace("US.", "")
            r["sentiment"] = sentiment_map.get(short, {})

        # Attach protection data from positions
        pos_map = {p["symbol"]: p for p in positions}
        for r in results:
            p = pos_map.get(r["symbol"], {})
            r["stop_loss_price"] = p.get("stop_loss_price")
            r["trailing_stop_price"] = p.get("trailing_stop_price")
            r["stop_loss_triggered"] = p.get("stop_loss_triggered", False)
            r["trailing_stop_triggered"] = p.get("trailing_stop_triggered", False)

        return results
    except Exception as e:
        logger.error(f"[snapshot] lifecycle calc failed: {e}")
        return []


def _load_sentiment_cache(symbols: list) -> dict:
    """Read social sentiment from SQLite cache. Non-blocking, returns {} on any error."""
    try:
        import sqlite3, json
        from pathlib import Path
        db_path = Path(__file__).parent.parent.parent.parent / "data" / "social_sentiment.db"
        if not db_path.exists():
            return {}
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        result = {}
        for sym in symbols:
            short = sym.replace("US.", "")
            row = conn.execute(
                "SELECT data_json FROM sentiment_cache WHERE symbol = ?", (short,)
            ).fetchone()
            if row:
                data = json.loads(row["data_json"])
                # Only return summary + hot_topics (not full items)
                result[short] = {
                    "summary": data.get("summary", {}),
                    "hot_topics": data.get("summary", {}).get("hot_topics", []),
                    "reddit_count": data.get("summary", {}).get("reddit_count", 0),
                    "hn_count": data.get("summary", {}).get("hn_count", 0),
                    "fetched_at": data.get("fetched_at", 0),
                }
        conn.close()
        return result
    except Exception:
        return {}

def _parse_risk_level(account: dict) -> dict:
    """富途账户风险档位 → 标签/颜色/说明 (只读呈现, 不参与任何判据)。

    2026-09-08 (REVIEW-20260908 §H) **口径更正 —— 两头都错的假警报**: 原表把 LEVEL1-5
    当成 安全/注意/警告/危险/强平, 而富途官方 `CltRiskStatus` 是**九档且 LEVEL1 最安全 ⇒
    LEVEL9 最危险** (LEVEL3 = 较安全, LEVEL5 = 中等风险)。后果实测:
      ① **常态误报** — 线上 `/api/risk/status` 对健康账户 (risk_status=LEVEL3) 返回
         `{"label":"警告","color":"orange","desc":"接近维持保证金"}`;
      ② **真危险漏报** — LEVEL6-9 (偏高风险/预警/危险/危险) 全部落 default gray「未知状态」,
         即最该报警的四档被渲染成中性灰。
    新表按官方九档逐档给中文标签, 分档语义与港股页 `hkRiskBadge()` 一致
    (LEVEL1-3 安全 / 4-6 隐患 / 7-9 危险); 本页色板有 5 色, 故隐患带内再分 yellow/orange,
    **排序不变**。未知 (`N/A`、`NONE`、券商新增档位) 一律 gray + 原样透出 —— 未知 ≠ 危险。
    desc 只写档位含义与可执行动作, **不编造保证金阈值** (富途文档只给档位名与"LEVEL1 最安全
    / LEVEL9 最危险", 原表的"接近维持保证金"等描述无出处)。

    登记 (AGENT.md「字段消费方登记制」): `futu_risk` 目前**前端零消费** (templates/static
    全仓 grep 无命中) = write-only payload; 但 `/api/risk/status` 与 dashboard snapshot 是
    对外契约 (仓外 skill 文档/LLM 会读), 故**更正口径而非删键**, 消费方待补登记。
    """
    rl = str(account.get("risk_status", "N/A") if account else "N/A")
    risk_levels = {
        "LEVEL1": {"label": "非常安全", "color": "green", "desc": "九档中最安全档，账户资金充裕"},
        "LEVEL2": {"label": "安全", "color": "green", "desc": "账户资金充足"},
        "LEVEL3": {"label": "较安全", "color": "green", "desc": "有杠杆但流动性充裕，无需动作"},
        "LEVEL4": {"label": "较低风险", "color": "yellow", "desc": "保证金使用开始上升，留意即可"},
        "LEVEL5": {"label": "中等风险", "color": "yellow", "desc": "关注保证金使用率与流动性"},
        "LEVEL6": {"label": "偏高风险", "color": "orange", "desc": "流动性偏紧，考虑减仓或入金"},
        "LEVEL7": {"label": "预警", "color": "red", "desc": "券商预警档，尽快减仓或入金"},
        "LEVEL8": {"label": "危险", "color": "red", "desc": "贴近维持保证金，存在强平风险"},
        "LEVEL9": {"label": "危险", "color": "red", "desc": "九档中最危险档，随时可能被强平"},
    }
    return {
        "risk_level": rl,
        "interest_charged": account.get("interest_charged", 0) if account else 0,
        "detail": risk_levels.get(
            rl.strip().upper(),
            {"label": rl, "color": "gray", "desc": "未知状态（券商未给档位或非九档口径）"},
        ),
    }


def _get_position_earnings(position_tickers: list) -> dict:
    """获取持仓标的未来 7 天财报日历, 返回 {today: [...], upcoming: [...]}"""
    result = {"today": [], "upcoming": [], "has_events": False}
    if not position_tickers:
        return result

    try:
        from app.data.earnings_calendar import EarningsCalendar
        ec = EarningsCalendar()
        events = ec.get_watchlist_earnings(
            watchlist_symbols=[f"US.{t}" for t in position_tickers],
            days_ahead=7,
            force_refresh=False,
        )
        for ev in events:
            days = ev.get("days_until", 999)
            entry = {
                "symbol": ev.get("symbol", ""),
                "ticker": ev.get("ticker", ""),
                "name": ev.get("name", ""),
                "report_date": ev.get("report_date", ""),
                "time_of_day": ev.get("time_of_day", ""),
                "sector": ev.get("sector", ""),
                "days_until": days,
            }
            if days == 0:
                result["today"].append(entry)
            else:
                result["upcoming"].append(entry)

        result["has_events"] = bool(result["today"] or result["upcoming"])
    except Exception as e:
        logger.debug(f"[snapshot] earnings calendar failed: {e}")

    return result


# ── 行业分类 (静态映射, 覆盖 50+ 常用美股) ──
_SECTOR_MAP = {
    "AAPL": "科技", "MSFT": "科技", "GOOGL": "科技", "META": "科技",
    "CRM": "科技", "ORCL": "科技", "ADBE": "科技", "CSCO": "科技", "IBM": "科技",
    "NOW": "科技", "INTU": "科技", "PANW": "科技", "SNOW": "科技", "PLTR": "科技",
    "NVDA": "半导体", "AMD": "半导体", "INTC": "半导体", "TSM": "半导体", "MU": "半导体",
    "AVGO": "半导体", "QCOM": "半导体", "MRVL": "半导体", "ARM": "半导体", "ASML": "半导体",
    "AMZN": "消费", "TSLA": "消费", "NKE": "消费", "MCD": "消费", "SBUX": "消费",
    "WMT": "消费", "COST": "消费", "HD": "消费", "LOW": "消费", "TGT": "消费",
    "JPM": "金融", "BAC": "金融", "GS": "金融", "MS": "金融", "WFC": "金融",
    "C": "金融", "BLK": "金融", "SCHW": "金融", "AXP": "金融", "V": "金融", "MA": "金融",
    "JNJ": "医疗", "UNH": "医疗", "PFE": "医疗", "MRK": "医疗", "ABBV": "医疗",
    "LLY": "医疗", "TMO": "医疗", "ABT": "医疗", "BMY": "医疗",
    "XOM": "能源", "CVX": "能源", "COP": "能源", "SLB": "能源",
    "CAT": "工业", "BA": "工业", "HON": "工业", "GE": "工业", "UNP": "工业", "RTX": "工业",
    "T": "通信", "VZ": "通信", "TMUS": "通信", "DIS": "通信", "NFLX": "通信", "CMCSA": "通信",
    "BABA": "中概", "JD": "中概", "PDD": "中概", "NIO": "中概", "XPEV": "中概",
    "LI": "中概", "BIDU": "中概", "TME": "中概", "BILI": "中概",
    "F": "汽车", "GM": "汽车", "RIVN": "汽车", "LCID": "汽车",
    "NEE": "公用事业", "DUK": "公用事业", "SO": "公用事业",
    "AMT": "地产", "PLD": "地产", "SPG": "地产",
    "NEM": "材料", "FCX": "材料", "DOW": "材料",
    "SPY": "指数ETF", "QQQ": "指数ETF", "IWM": "指数ETF", "DIA": "指数ETF",
    "XLE": "能源ETF", "XLF": "金融ETF", "XLK": "科技ETF", "XLV": "医疗ETF",
    "SOXX": "半导体ETF", "SMH": "半导体ETF", "ARKK": "创新ETF",
    "TLT": "债券ETF", "GLD": "黄金ETF", "USO": "原油ETF",
}


def _load_okr_summary(today_closed: list) -> dict:
    """读取 data/okr_latest.json 构造 OKR 进度摘要 (4 KR vs 目标).

    okr_baseline 每次跑更新此文件; 同时用今日实时已平数据补充当日胜率/PnL.
    """
    targets = {
        "win_rate": 55,        # KR1 胜率 > 55%
        "weekly_pnl": 1000,    # KR2 周收益 ≥ $1000
        "sample": 30,          # KR3 样本 ≥ 30
        "ic_t3": 0.05,         # KR3 IC(T+3) > 0.05
    }
    summary = {"targets": targets, "has_data": False, "today": {}}
    # 今日实时 (从已平列表算, 比磁盘 json 更新)
    if today_closed:
        wins = sum(1 for t in today_closed if t.get("pnl_usd", 0) > 0)
        losses = sum(1 for t in today_closed if t.get("pnl_usd", 0) < 0)
        total_pnl = round(sum(t.get("pnl_usd", 0) for t in today_closed), 2)
        summary["today"] = {
            "count": len(today_closed),
            "wins": wins, "losses": losses,
            "win_rate": round(wins / len(today_closed) * 100, 1) if today_closed else 0,
            "total_pnl": total_pnl,
        }
    # 累计 OKR (磁盘 json)
    try:
        _path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), "data", "okr_latest.json")
        with open(_path, "r", encoding="utf-8") as f:
            okr = json.load(f)
        kr = okr.get("kr_scores", {})
        ic = okr.get("ic", {})
        pipe = okr.get("pipeline", {})
        summary["has_data"] = True
        summary["overall_score"] = okr.get("overall_score", 0)
        summary["cumulative"] = {
            "win_rate": round(kr.get("kr1_win_rate", 0), 1),
            "weekly_pnl": round(kr.get("kr2_weekly_pnl", 0), 2),
            "sample": round(kr.get("kr3_sample", 0), 0),
            "ic_t3": ic.get("t3"),
            "ic_graduation": ic.get("graduation", "ACCUMULATING"),
            "pipeline_executed": pipe.get("executed", 0),
            "pipeline_win_rate": pipe.get("win_rate", 0),
        }
        summary["updated_at"] = okr.get("timestamp", "")
    except Exception:
        pass
    return summary


def _calc_concentration(positions: list, equity: float) -> dict:
    """计算行业集中度 + 方向聚集"""
    if not positions:
        return {"sectors": [], "direction": {"LONG": 0, "SHORT": 0},
                "same_direction": False, "warnings": [], "has_data": False}

    sector_mv = {}
    direction_count = {"LONG": 0, "SHORT": 0}
    total_mv = 0

    for p in positions:
        code_short = p.get("symbol", "").replace("US.", "")
        sector = _SECTOR_MAP.get(code_short, "其他")
        mv = p.get("market_val", 0) or p.get("market_value", 0) or 0
        sector_mv[sector] = sector_mv.get(sector, 0) + mv
        total_mv += mv
        d = p.get("direction", "LONG")
        direction_count[d] = direction_count.get(d, 0) + 1

    # 行业占比
    sectors = []
    warnings = []
    denom = equity if equity > 0 else total_mv
    for name, mv in sorted(sector_mv.items(), key=lambda x: x[1], reverse=True):
        pct = round(mv / denom * 100, 1) if denom > 0 else 0
        sectors.append({"name": name, "market_val": round(mv, 2), "pct": pct})
        if pct > 40:
            warnings.append(f"{name} 占比 {pct}% > 40%")

    # 方向聚集
    long_n = direction_count.get("LONG", 0)
    short_n = direction_count.get("SHORT", 0)
    total = long_n + short_n
    same_direction = total > 1 and (long_n == total or short_n == total)
    if same_direction:
        dir_label = "LONG" if long_n == total else "SHORT"
        warnings.append(f"全部 {total} 仓同向 ({dir_label})")

    return {
        "sectors": sectors,
        "direction": direction_count,
        "same_direction": same_direction,
        "warnings": warnings,
        "has_data": True,
    }


async def _calc_weekly_pnl(ds) -> dict:
    """本周 (周一至今) 已实现盈亏 + 胜率"""
    result = {"realized": 0, "trades": 0, "wins": 0, "win_rate": 0}
    try:
        from datetime import timedelta, date as _date
        today_str = et_today()
        d = _date.fromisoformat(today_str)
        monday = d - timedelta(days=d.weekday())
        row = await ds.db_query_one(
            "SELECT COALESCE(SUM(realized_pnl),0) as pnl, COUNT(*) as cnt, "
            "SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins "
            "FROM trade_records WHERE trade_date >= ? AND source_type NOT IN ('PLAYBOOK_SHADOW','SCALPING') AND COALESCE(exit_reason,'') NOT IN ('PHANTOM_RECONCILE_VOID','ORPHAN_CLEANUP') "
            "AND exit_price > 0",
            (monday.isoformat(),),
        )
        if row:
            result["realized"] = round(float(row.get("pnl", 0) or 0), 2)
            result["trades"] = int(row.get("cnt", 0) or 0)
            result["wins"] = int(row.get("wins", 0) or 0)
            if result["trades"] > 0:
                result["win_rate"] = round(
                    result["wins"] / result["trades"] * 100, 1
                )
    except Exception:
        pass
    return result


def _calc_decision_summary(market: dict, margin: dict, pnl: dict, weekly_pnl: dict) -> dict:
    """构造 3 行决策摘要数据 (纯内存/文件, 无 broker 调用)"""
    # 读 battle plan 文件
    plan_actions = _empty_plan_actions()
    try:
        from pathlib import Path as _Path
        import json as _json

        _plan_path = (
            _Path(__file__).resolve().parent.parent.parent.parent
            / "data"
            / "daily_battle_plan_us.json"
        )
        if _plan_path.exists():
            with open(_plan_path) as f:
                plan = _json.load(f)
            plan_actions = _summarize_plan_actions(plan)
    except Exception:
        pass

    # VIX 等级
    vix = float(market.get("vix", 0) or 0)
    vix_label = "低波" if vix < 15 else "正常" if vix < 25 else "偏高" if vix < 35 else "恐慌"

    # 水位
    water_level = "unknown"
    try:
        from app.risk.capital_safety import get_capital_safety_system

        cs = get_capital_safety_system()
        if cs and cs._state and cs._state.water_level:
            water_level = cs._state.water_level.value
    except Exception:
        pass

    # 能否开仓
    margin_pct = float(margin.get("usage_pct", 0) or 0)
    can_trade = margin_pct < 70 and water_level not in ("red", "blackout")

    return {
        "actions": plan_actions,
        "risk": {
            "vix": round(vix, 1),
            "vix_label": vix_label,
            "margin_pct": round(margin_pct, 1),
            "water_level": water_level,
            "can_trade": can_trade,
        },
        "performance": {
            "today_pl": float(pnl.get("today_pl", 0) or 0),
            "today_realized": float(pnl.get("realized", 0) or 0),
            "week_realized": weekly_pnl.get("realized", 0),
            "week_trades": weekly_pnl.get("trades", 0),
            "week_win_rate": weekly_pnl.get("win_rate", 0),
        },
    }


def _empty_plan_actions() -> dict:
    return {"must_do": 0, "opportunities": 0, "regime": "UNKNOWN", "brief": ""}


def _summarize_plan_actions(plan: dict) -> dict:
    """把作战计划压成今日摘要；过期计划整体作废，不得只清 targets。

    2026-10-25 Dashboard UX 审计：旧计划文件冻结在 2026-07-31，targets 已按
    stale 清空，但 position_actions 仍参与 must_do 计数，导致首屏同时出现
    "1 项必须执行" 与 "当前无需操作"。计划日期不是今日 ET 时，所有摘要字段
    都必须回到空值，旧仓动作不能冒充今日待办。
    """
    if not isinstance(plan, dict):
        return _empty_plan_actions()
    from app.web_dashboard.data_service import battle_plan_is_stale
    if battle_plan_is_stale(plan):
        return _empty_plan_actions()

    targets = plan.get("targets", []) or []
    position_actions = plan.get("position_actions", []) or []
    restricted = set(plan.get("restricted", []) or [])
    must_do = sum(
        1
        for pa in position_actions
        if str(pa.get("action", "")).upper() in ("EXIT", "CLOSE", "REDUCE")
    )
    opps = sum(
        1
        for t in targets
        if t.get("symbol", "").replace("US.", "") not in restricted
        and t.get("symbol", "") not in restricted
        and float(t.get("confidence", 0) or 0) >= 60
    )
    return {
        "must_do": must_do,
        "opportunities": opps,
        "regime": plan.get("market_regime", "UNKNOWN"),
        "brief": plan.get("morning_brief", ""),
    }
