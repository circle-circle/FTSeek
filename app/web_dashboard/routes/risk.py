"""
风控路由 — 风控状态 + PnL 历史 + 策略状态

/api/risk/status 保留向后兼容, 内部使用 DataService 共享缓存
"""
import asyncio
import logging
from datetime import datetime
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.web_dashboard.data_service import get_data_service, safe_float, et_today, et_days_ago
from app.web_dashboard.api_response import api_ok, api_error, err_broker_unavailable

logger = logging.getLogger("ftseek_us.routes.risk")
router = APIRouter()


def register(app, templates):
    @router.get("/risk")
    async def risk_page():
        return RedirectResponse(url="/", status_code=301)

    @router.get("/api/risk/status")
    async def risk_status():
        """风控状态 — 使用 DataService 共享缓存 (向后兼容)"""
        ds = get_data_service()
        cached = ds.cache.get("risk_status_v2")
        if cached is not None:
            return cached

        svc = ds.svc()
        is_live = svc._is_live() if svc._connected else False
        mode = svc._get_mode_label() if svc._connected else "simulate"

        # 并行获取数据 (DataService 有 TTL cache, 与 snapshot 共享)
        acc_raw, pos_raw = await asyncio.gather(
            ds.get_account_raw(),
            ds.get_positions_raw(),
        )

        from app.web_dashboard.routes.dashboard import (
            _parse_account, _parse_positions, _calc_margin,
            _calc_exposure, _calc_pnl, _calc_stop_distances, _parse_risk_level,
        )

        account = _parse_account(acc_raw)
        positions = await _parse_positions(ds, pos_raw)
        margin = _calc_margin(account)
        exposure = _calc_exposure(account)
        pnl = await _calc_pnl(ds, positions)
        stop_distances = await _calc_stop_distances(ds, positions, is_live)
        futu_risk = _parse_risk_level(account)

        result = {
            "mode": mode,
            "is_live": is_live,
            "account": account,
            "margin": margin,
            "exposure": exposure,
            "pnl": pnl,
            "stop_distances": stop_distances,
            "futu_risk": futu_risk,
        }
        ds.cache.set("risk_status_v2", result, ttl=10)
        return result

    @router.get("/api/risk/pnl-history")
    async def risk_pnl_history(days: int = 30):
        ds = get_data_service()
        start_date = et_days_ago(days)
        rows = await ds.db_query(
            "SELECT trade_date, COALESCE(SUM(realized_pnl), 0) as daily_pnl, "
            "COUNT(*) as trades, "
            "SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins "
            "FROM trade_records WHERE trade_date >= ? "
            "AND source_type NOT IN ('PLAYBOOK_SHADOW','SCALPING') "
            "GROUP BY trade_date ORDER BY trade_date",
            (start_date,),
        )

        history = []
        # M2: 从 capital_state 获取实际起始资金，而非硬编码 $100K
        starting_equity = 100000  # 默认值
        try:
            first_cap = await ds.db_query_one(
                "SELECT current_equity FROM capital_state ORDER BY rowid LIMIT 1"
            )
            if first_cap:
                starting_equity = float(first_cap["current_equity"])
        except Exception:
            pass
        cumulative = starting_equity
        for row in rows:
            daily_pnl = float(row.get("daily_pnl", 0))
            cumulative += daily_pnl
            trades = int(row.get("trades", 0))
            wins = int(row.get("wins", 0))
            history.append({
                "date": row["trade_date"],
                "daily_pnl": round(daily_pnl, 2),
                "total_assets": round(cumulative, 2),
                "trades": trades,
                "win_rate": round(wins / trades * 100, 1) if trades > 0 else 0,
            })

        return {"equity_history": history}

    @router.get("/api/strategy/status")
    async def strategy_status():
        ds = get_data_service()
        today = et_today()
        result = {"strategies": [], "orb": {}, "signals": {}, "service": {}}

        # 1. 服务状态 (H2: non-blocking)
        try:
            import asyncio as _aio
            _proc = await _aio.create_subprocess_exec(
                "systemctl", "is-active", "ftseek-us",
                stdout=_aio.subprocess.PIPE, stderr=_aio.subprocess.PIPE,
            )
            _out, _ = await _aio.wait_for(_proc.communicate(), timeout=5)
            result["service"]["ftseek_us"] = _out.decode().strip()
        except Exception:
            result["service"]["ftseek_us"] = "unknown"

        # 2. 按策略统计
        strat_rows = await ds.db_query("""
            SELECT COALESCE(strategy_id, 'unknown') as strategy,
                   COUNT(*) as trades,
                   SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                   ROUND(SUM(realized_pnl), 2) as pnl,
                   ROUND(AVG(realized_pnl), 2) as avg_pnl
            FROM trade_records
            WHERE trade_date = (SELECT MAX(trade_date) FROM trade_records)
              AND source_type NOT IN ('PLAYBOOK_SHADOW','SCALPING')
            GROUP BY strategy_id ORDER BY pnl DESC
        """)
        for r in strat_rows:
            trades = r["trades"]
            wins = r.get("wins", 0) or 0
            result["strategies"].append({
                "name": r["strategy"],
                "trades": trades,
                "wins": wins,
                "win_rate": round(wins / trades * 100, 1) if trades > 0 else 0,
                "pnl": float(r.get("pnl") or 0),
                "avg_pnl": float(r.get("avg_pnl") or 0),
            })

        # 3. 最近7天
        weekly = await ds.db_query("""
            SELECT COALESCE(strategy_id, 'unknown') as strategy,
                   COUNT(*) as trades,
                   SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                   ROUND(SUM(realized_pnl), 2) as pnl
            FROM trade_records WHERE trade_date >= ?
              AND source_type NOT IN ('PLAYBOOK_SHADOW','SCALPING')
            GROUP BY strategy_id ORDER BY pnl DESC
        """, (et_days_ago(7),))
        result["weekly"] = [{
            "name": r["strategy"],
            "trades": r["trades"],
            "wins": r.get("wins", 0) or 0,
            "win_rate": round((r.get("wins", 0) or 0) / r["trades"] * 100, 1) if r["trades"] > 0 else 0,
            "pnl": float(r.get("pnl") or 0),
        } for r in weekly]

        # 4. 今日信号统计 (signal_lifecycle + rejections)
        try:
            sig_row = await ds.db_query_one(
                "SELECT COUNT(*) as executed FROM signal_lifecycle WHERE trade_date = ?",
                (today,),
            )
            from app.core.paths import get_rejections_db
            _rej_db = str(get_rejections_db())
            rej_row = await ds.db_query_one(
                "SELECT COUNT(*) as rejected FROM rejections WHERE date(timestamp) = ?",
                (today,),
                db_path=_rej_db,
            )
            executed = (sig_row.get("executed", 0) or 0) if sig_row else 0
            rejected = (rej_row.get("rejected", 0) or 0) if rej_row else 0
            result["signals"] = {
                "total": executed + rejected,
                "accepted": executed,
                "rejected": rejected,
            }
        except Exception:
            pass

        # 5. 最近信号明细 (signal_lifecycle 表)
        recent = await ds.db_query(
            "SELECT symbol, signal_direction, signal_confidence, signal_strength, "
            "source_type, substr(created_at, 12, 8) as time "
            "FROM signal_lifecycle WHERE trade_date = ? "
            "ORDER BY created_at DESC LIMIT 10",
            (today,),
        )
        result["recent_signals"] = [{
            "symbol": r["symbol"],
            "direction": r.get("signal_direction", ""),
            "confidence": round(float(r.get("signal_confidence") or 0), 3),
            "strength": int(r.get("signal_strength") or 0),
            "status": r.get("source_type", ""),
            "time": r.get("time", ""),
        } for r in recent]

        return result

    # ═══════════════════════════════════════════════════════
    #  Risk Command Panel (风控中心)
    # ═══════════════════════════════════════════════════════

    @router.get("/api/risk/command")
    async def risk_command():
        """聚合风控数据: 保证金 + VIX + Kill Switch + 财报 + 建议"""
        ds = get_data_service()
        cached = ds.cache.get("risk_command")
        if cached is not None:
            return cached

        # 并行获取数据
        acc_raw, pos_raw, vix = await asyncio.gather(
            ds.get_account_raw(),
            ds.get_positions_raw(),
            ds.get_vix(),
        )

        from app.web_dashboard.routes.dashboard import _parse_account, _parse_positions, _calc_margin

        account = _parse_account(acc_raw)
        margin = _calc_margin(account)
        margin_pct = margin.get("usage_pct", 0)

        # Kill switch 状态
        kill_switch_active = False
        try:
            from app.us_pipeline.emergency_interceptor import check_kill_switch
            kill_switch_active = check_kill_switch()
        except Exception:
            # Fallback: 直接检查 flag 文件
            from pathlib import Path
            flag = Path(__file__).parent.parent.parent.parent / "data" / "KILL_SWITCH.flag"
            kill_switch_active = flag.exists()

        # 财报预警 (持仓中有财报的)
        earnings_count = 0
        try:
            from app.web_dashboard.routes.dashboard import _get_position_earnings
            positions = await _parse_positions(ds, pos_raw)
            earnings = _get_position_earnings(positions)
            earnings_count = len(earnings) if earnings else 0
        except Exception:
            pass

        # 生成建议
        suggestions = []
        if margin_pct > 50:
            suggestions.append(f"保证金使用率 {margin_pct:.1f}% 偏高，建议减仓降压")
        if vix and vix >= 25:
            suggestions.append(f"VIX {vix:.1f} 处于高波动区，建议收紧止损或减少新开仓")
        if kill_switch_active:
            suggestions.append("紧急制动已激活，一切扫描和下单已停止")
        if earnings_count > 0:
            suggestions.append(f"{earnings_count} 只持仓近期有财报，注意事件风险")

        result = {
            "margin_pct": round(margin_pct, 1),
            "vix": round(vix, 2) if vix else 0,
            "kill_switch_active": kill_switch_active,
            "earnings_count": earnings_count,
            "suggestions": suggestions,
        }
        ds.cache.set("risk_command", result, ttl=15)
        return result

    @router.post("/api/risk/pause-trading")
    async def pause_trading():
        """激活 Kill Switch，暂停自动交易"""
        try:
            from app.us_pipeline.emergency_interceptor import activate_kill_switch
            activate_kill_switch("Dashboard manual pause")
            ds = get_data_service()
            ds.invalidate_cache("risk_command")
            logger.warning("[RiskCmd] Kill Switch 已激活 (Dashboard)")
            return api_ok({"message": "自动交易已暂停"})
        except Exception as e:
            logger.error(f"[RiskCmd] 暂停失败: {e}")
            return api_error("INTERNAL_ERROR", "操作失败，请稍后重试", 500)

    @router.post("/api/risk/resume-trading")
    async def resume_trading():
        """解除 Kill Switch，恢复自动交易"""
        try:
            from app.us_pipeline.emergency_interceptor import deactivate_kill_switch
            deactivate_kill_switch()
            ds = get_data_service()
            ds.invalidate_cache("risk_command")
            logger.info("[RiskCmd] Kill Switch 已解除 (Dashboard)")
            return api_ok({"message": "自动交易已恢复"})
        except Exception as e:
            logger.error(f"[RiskCmd] 恢复失败: {e}")
            return api_error("INTERNAL_ERROR", "操作失败，请稍后重试", 500)

    @router.post("/api/risk/emergency-close")
    async def emergency_close(request: Request):
        """全部平仓 — 需要 CONFIRM 确认"""
        try:
            body = await request.json()
        except Exception:
            return api_error("BAD_REQUEST", "请求无效", 400)

        if body.get("confirm") != "CONFIRM":
            return api_error("BAD_REQUEST", "需要 CONFIRM 确认", 400)

        # P0-2 修复 (2026-06-12): 二次密钥校验, 防止 CSRF / 误点导致一键全平
        # 在 .env.us 配置 EMERGENCY_CONFIRM_TOKEN=<随机字符串>, 调用方需在 body 中提供 token 字段
        try:
            from app.web_dashboard.auth import _read_env
            expected_token = _read_env("EMERGENCY_CONFIRM_TOKEN", "")
        except Exception:
            expected_token = ""
        if expected_token:
            user_token = str(body.get("token", ""))
            import hmac as _hmac
            if not user_token or not _hmac.compare_digest(user_token, expected_token):
                logger.warning("[RiskCmd] 紧急平仓拒绝: EMERGENCY_CONFIRM_TOKEN 校验失败")
                return api_error("FORBIDDEN", "EMERGENCY_CONFIRM_TOKEN 校验失败", 403)
        else:
            # P0 2026-06-15: 未配置 token 时拒绝执行, 防止误触/CSRF 一键全平
            logger.error("[RiskCmd] 紧急平仓被拒绝: 未配置 EMERGENCY_CONFIRM_TOKEN")
            return api_error("CONFIG_ERROR", "紧急平仓需要先在 .env.us 配置 EMERGENCY_CONFIRM_TOKEN", 503)

        ds = get_data_service()
        pos_raw = await ds.get_positions_raw()
        if not pos_raw:
            return api_ok({"message": "无持仓需要平仓", "closed": 0})

        closed = 0
        errors = []
        from app.web_dashboard.manual_order_service import get_manual_order_service
        svc = get_manual_order_service()

        for pos in pos_raw:
            symbol = str(pos.get("code", ""))
            qty = int(abs(float(pos.get("qty", 0))))
            if qty <= 0:
                continue
            try:
                result = svc.close_position(symbol=symbol, reason="紧急平仓(Dashboard)", close_qty=qty)
                if result.get("success"):
                    closed += 1
                else:
                    errors.append(f"{symbol}: {result.get('error', '未知错误')}")
            except Exception as e:
                errors.append(f"{symbol}: {e}")

        ds.invalidate_cache()
        msg = f"平仓 {closed} 只" + (f", {len(errors)} 只失败" if errors else "")
        logger.warning(f"[RiskCmd] 紧急平仓: {msg}")
        return api_ok({"message": msg, "closed": closed, "errors": errors})

    app.include_router(router)
