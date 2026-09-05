"""LLM/auto-thesis 入场上下文与出场快照构建.

公开函数:
    - auto_generate_thesis(service, order) -> dict
    - save_thesis_to_db(service, order, thesis_ctx)
    - build_exit_context(service, symbol, entry_time_str, thesis_ctx) -> dict
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from app.web_dashboard.manual_order_service import ManualOrder, ManualOrderService

logger = logging.getLogger("ftseek_us.manual_order_service.thesis")


def auto_generate_thesis(service: "ManualOrderService", order: "ManualOrder") -> dict:
    """自动组装入场 thesis — 从 battle plan + 信号 + VIX + 历史战绩拼出结构化决策上下文."""
    symbol = order.symbol
    bare = symbol.replace("US.", "")
    ctx = {
        "symbol": symbol,
        "direction": order.direction,
        "entry_price": float(order.price or 0),
        "quantity": order.quantity,
        "order_type": order.order_type,
        "reason": order.reason or "",
        "generated_at": datetime.now().isoformat(),
    }

    # 1. Battle plan 匹配
    try:
        _plan_path = Path(__file__).resolve().parent.parent.parent.parent / "data" / "daily_battle_plan_us.json"
        if _plan_path.exists():
            import json as _json
            plan = _json.loads(_plan_path.read_text())
            from app.web_dashboard.data_service import battle_plan_is_stale
            _pm_targets = [] if battle_plan_is_stale(plan) else plan.get("targets", [])
            for t in _pm_targets:
                t_sym = t.get("symbol", "").replace("US.", "")
                if t_sym == bare:
                    ctx["plan_match"] = True
                    ctx["plan"] = {
                        "direction": t.get("direction", ""),
                        "entry_zone": t.get("entry_zone", []),
                        "stop_loss": t.get("stop_loss"),
                        "take_profit": t.get("take_profit"),
                        "confidence": t.get("confidence", 0),
                        "rationale": t.get("rationale", ""),
                        "risk_reward": t.get("risk_reward"),
                        "factor_score": t.get("factor_score"),
                    }
                    break
            else:
                ctx["plan_match"] = False
            # 市场 regime
            ctx["market_regime"] = plan.get("market_regime", "UNKNOWN")
            # 是否在 avoid 列表
            restricted = plan.get("restricted", [])
            ctx["in_avoid_list"] = bare in restricted or symbol in restricted
    except Exception:
        ctx["plan_match"] = False

    # 2. VIX
    try:
        _vix_path = Path(__file__).resolve().parent.parent.parent.parent / "data" / "vix_cache" / "vix_data.json"
        if _vix_path.exists():
            import json as _json
            ctx["vix"] = float(_json.loads(_vix_path.read_text()).get("vix", 0))
    except Exception:
        pass

    # 3. 最近信号
    try:
        conn = service._db()
        row = conn.execute(
            "SELECT direction, strength, confidence, source FROM signal_snapshots "
            "WHERE symbol = ? ORDER BY created_at DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        conn.close()
        if row:
            ctx["signal"] = {
                "direction": row[0], "strength": row[1],
                "confidence": row[2], "source": row[3],
            }
    except Exception:
        pass

    # 4. 7 天战绩
    try:
        conn = service._db()
        from app.web_dashboard.data_service import et_days_ago as _eda
        row = conn.execute(
            "SELECT COUNT(*) as cnt, "
            "SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins, "
            "COALESCE(SUM(realized_pnl), 0) as net_pnl "
            "FROM trade_records WHERE symbol = ? "
            "AND trade_date >= ? AND source_type NOT IN ('PLAYBOOK_SHADOW','SCALPING') AND COALESCE(exit_reason,'') NOT IN ('PHANTOM_RECONCILE_VOID','ORPHAN_CLEANUP')",
            (symbol, _eda(7)),  # 2026-07-28: date('now') UTC→ET (时区家族)
        ).fetchone()
        conn.close()
        if row and row[0] > 0:
            cnt = row[0]
            wins = row[1] or 0
            ctx["history_7d"] = {
                "trades": cnt, "wins": wins,
                "win_rate": round(wins / cnt * 100, 1),
                "net_pnl": round(float(row[2]), 2),
            }
    except Exception:
        pass

    # 5. 交易时段 (ET)
    try:
        from app.utils.timezone import now_et
        et_now = now_et()
        et_h = et_now.hour
        et_m = et_now.minute
        et_minutes = et_h * 60 + et_m
        if et_minutes < 570:       # < 09:30
            ctx["session_phase"] = "pre_market"
        elif et_minutes < 600:     # 09:30-10:00
            ctx["session_phase"] = "opening_drive"
        elif et_minutes < 900:     # 10:00-15:00
            ctx["session_phase"] = "mid_day"
        elif et_minutes < 960:     # 15:00-16:00
            ctx["session_phase"] = "closing_drive"
        else:
            ctx["session_phase"] = "after_hours"
    except Exception:
        ctx["session_phase"] = "unknown"

    # 6. 组合状态 (持仓数 + 敞口 + 资金水位)
    try:
        if service._broker_ctx and service._connected:
            from futu import RET_OK as _RET_OK
            ret, acc = service._broker_ctx.accinfo_query(
                trd_env=service._trd_env(), acc_id=service._acc_id(),
            )
            if ret == _RET_OK and not acc.empty:
                _row = acc.iloc[0]
                _total = float(_row.get("total_assets", 0))
                _long = float(_row.get("long_mv", 0))
                _short = abs(float(_row.get("short_mv", 0)))
                ctx["portfolio_exposure_pct"] = round((_long + _short) / _total * 100, 1) if _total > 0 else 0

            ret2, pos_list = service._broker_ctx.position_list_query(
                trd_env=service._trd_env(), acc_id=service._acc_id(),
            )
            if ret2 == _RET_OK:
                ctx["open_position_count"] = len(pos_list) if pos_list is not None else 0
    except Exception:
        pass

    # 7. 资金水位
    try:
        from app.risk.capital_safety import get_capital_safety_system
        _cs = get_capital_safety_system()
        if _cs and _cs._state and _cs._state.water_level:
            ctx["capital_water_level"] = _cs._state.water_level.value
    except Exception:
        pass

    # 9. 组装 thesis 文本
    parts = []
    if ctx.get("plan_match"):
        p = ctx["plan"]
        parts.append(f"计划匹配({p.get('confidence', 0)}%)")
        if p.get("risk_reward"):
            parts.append(f"R:R={p['risk_reward']:.1f}:1")
    if ctx.get("signal"):
        s = ctx["signal"]
        parts.append(f"信号{s['direction']}({s.get('strength', 0)})")
    if ctx.get("history_7d"):
        h = ctx["history_7d"]
        parts.append(f"7天{h['wins']}W/{h['trades'] - h['wins']}L")
    if ctx.get("vix"):
        vix = ctx["vix"]
        vix_label = "低波" if vix < 15 else "正常" if vix < 25 else "偏高" if vix < 35 else "恐慌"
        parts.append(f"VIX={vix:.1f}({vix_label})")
    if ctx.get("in_avoid_list"):
        parts.append("⚠️在回避列表")
    # 组合状态
    if ctx.get("session_phase") not in (None, "unknown"):
        phase_cn = {"opening_drive": "开盘", "mid_day": "盘中", "closing_drive": "尾盘",
                    "pre_market": "盘前", "after_hours": "盘后"}.get(ctx["session_phase"], "")
        if phase_cn:
            parts.append(f"时段:{phase_cn}")
    if ctx.get("open_position_count") is not None:
        parts.append(f"{ctx['open_position_count']}仓")

    ctx["thesis_text"] = " | ".join(parts) if parts else f"手动{order.direction} — 无计划/信号匹配"
    return ctx


def save_thesis_to_db(service: "ManualOrderService", order: "ManualOrder", thesis_ctx: dict) -> None:
    """将 auto-thesis 写入 open_positions.thesis + entry_context_json."""
    try:
        import json as _json
        conn = service._db()
        thesis_text = thesis_ctx.get("thesis_text", "")
        entry_context = _json.dumps(thesis_ctx, ensure_ascii=False)
        plan = thesis_ctx.get("plan", {})
        invalidation = plan.get("stop_loss") if thesis_ctx.get("plan_match") else None

        # SIM 隔离 (2026-09-05, DEB-20260905 §13.15): 实盘单的 thesis 不得写进 sim 收集仓
        from app.web_dashboard.env_scope import scoped_update
        scoped_update(
            conn,
            "UPDATE open_positions SET thesis=?, entry_context_json=?, "
            "expected_hold_days=?, invalidation_price=? WHERE symbol=?",
            (thesis_text, entry_context, 7, invalidation, order.symbol),
            order.symbol, "auto-thesis 落库",
        )
        conn.commit()
        conn.close()
        logger.info(f"[auto-thesis] {order.symbol}: {thesis_text}")
    except Exception as e:
        logger.debug(f"[auto-thesis] DB 写入失败 (不影响下单): {e}")


def build_exit_context(service: "ManualOrderService", symbol: str,
                       entry_time_str: str, thesis_ctx: dict) -> dict:
    """平仓时记录出场快照 — 与 entry_context 对称."""
    exit_ctx = {"exit_time": datetime.now().isoformat()}

    # 交易时段
    try:
        from app.utils.timezone import now_et
        et_now = now_et()
        et_minutes = et_now.hour * 60 + et_now.minute
        if et_minutes < 570:
            exit_ctx["session_phase"] = "pre_market"
        elif et_minutes < 600:
            exit_ctx["session_phase"] = "opening_drive"
        elif et_minutes < 900:
            exit_ctx["session_phase"] = "mid_day"
        elif et_minutes < 960:
            exit_ctx["session_phase"] = "closing_drive"
        else:
            exit_ctx["session_phase"] = "after_hours"
    except Exception:
        pass

    # VIX
    try:
        _vix_path = Path(__file__).resolve().parent.parent.parent.parent / "data" / "vix_cache" / "vix_data.json"
        if _vix_path.exists():
            import json as _json
            exit_ctx["vix"] = float(_json.loads(_vix_path.read_text()).get("vix", 0))
    except Exception:
        pass

    # 持仓时长 (小时)
    try:
        if entry_time_str:
            _entry_dt = datetime.fromisoformat(entry_time_str.replace("Z", "+00:00").replace(" ", "T"))
            if _entry_dt.tzinfo is None:
                _entry_dt = _entry_dt.replace(tzinfo=timezone.utc)
            _now_utc = datetime.now(timezone.utc)
            _delta = _now_utc - _entry_dt
            exit_ctx["hold_hours"] = round(_delta.total_seconds() / 3600, 1)
    except Exception:
        pass

    # 从 entry thesis 继承的字段
    if thesis_ctx:
        exit_ctx["plan_match"] = thesis_ctx.get("plan_match", False)
        exit_ctx["expected_hold_days"] = thesis_ctx.get("expected_hold_days")
        exit_ctx["entry_session_phase"] = thesis_ctx.get("session_phase")

    # 市场 regime
    try:
        import json as _json
        _plan_path = Path(__file__).resolve().parent.parent.parent.parent / "data" / "daily_battle_plan_us.json"
        if _plan_path.exists():
            exit_ctx["market_regime"] = _json.loads(_plan_path.read_text()).get("market_regime", "UNKNOWN")
    except Exception:
        pass

    return exit_ctx
