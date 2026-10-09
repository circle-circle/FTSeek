"""auto_execute 装配域 (阶段 9 迁自 app/playbook/pipeline.py)。"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple


def load_budget_and_guardrails(
    today: str,
    *,
    resolve_sizing_base: Callable,
    logger,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """返回 (执行上下文, 终止结果); DB 日帽查询失败保持 fail-close。"""
    max_deploy = 0.0
    try:
        from app.core.db_utils import safe_sqlite_connect
        from app.core.paths import get_trade_records_db_for_arm
        connection = safe_sqlite_connect(
            get_trade_records_db_for_arm("sim"), timeout=5
        )
        row = connection.execute(
            "SELECT COALESCE(SUM(entry_price * quantity), 0) FROM trade_records "
            "WHERE source_type='PLAYBOOK_SIM' AND (exit_time IS NULL OR exit_time = '')"
        ).fetchone()
        connection.close()
        max_deploy = row[0] if row else 0.0
    except Exception as error:
        max_deploy = float("inf")
        logger.error(
            f"[playbook] R1b 预算查询失败 → fail-close 拦后续开仓: {error}"
        )

    from app.playbook.config import get_playbook_config
    from app.risk.capital_safety import get_capital_safety_system
    playbook_config = get_playbook_config()

    budget_limit = 0.0
    budget_source = "unavailable"
    try:
        capital_safety = get_capital_safety_system()
        budget_limit = capital_safety.get_available_budget_for_auto()
        budget_source = "capital_safety_auto_budget"
    except Exception as error:
        logger.warning(
            f"[playbook] capital_safety auto_budget 读取失败: {error}"
        )
    if budget_limit <= 0:
        risk_base = resolve_sizing_base(playbook_config)
        budget_limit = risk_base * 0.8
        budget_source = f"fallback_{risk_base:.0f}×80%"
    logger.info(
        f"[playbook] 预算上限: ${budget_limit:,.0f} "
        f"(来源={budget_source}), 已部署=${max_deploy:,.0f}"
    )

    risk_base_capital = resolve_sizing_base(playbook_config)
    daily_notional_cap_pct = float(
        playbook_config.get("daily_notional_cap_pct", 0.80)
    )
    daily_notional_cap = risk_base_capital * daily_notional_cap_pct
    max_daily_positions = int(
        playbook_config.get("max_daily_positions", 16)
    )
    max_new_per_cycle = int(
        playbook_config.get("max_new_positions_per_cycle", 3)
    )
    max_open_positions = int(playbook_config.get("max_open_positions", 999))

    today_opened_notional = 0.0
    today_opened_count = 0
    open_positions_count = 0
    try:
        from app.core.db_utils import safe_sqlite_connect
        from app.core.paths import get_trade_records_db
        connection = safe_sqlite_connect(get_trade_records_db(), timeout=5)
        try:
            row = connection.execute(
                "SELECT COALESCE(SUM(entry_price * quantity), 0), COUNT(*) "
                "FROM trade_records WHERE substr(entry_time,1,10) = ? "
                "AND source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')",
                (today,)
            ).fetchone()
            today_opened_notional = float(row[0] or 0)
            today_opened_count = int(row[1] or 0)
            open_row = connection.execute(
                "SELECT COUNT(*) FROM trade_records "
                "WHERE source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') "
                "AND (exit_time IS NULL OR exit_time = '')"
            ).fetchone()
            open_positions_count = int(open_row[0] or 0)
        finally:
            connection.close()
    except Exception as error:
        logger.error(
            f"[playbook] 查询当日已开仓失败, fail-close 拦截本轮: {error}"
        )
        return None, {
            "date": today,
            "executed": 0,
            "skipped": 0,
            "reason": "daily_guardrail_query_failed",
            "detail": str(error),
        }

    logger.info(
        f"[playbook] 单日 guardrail: notional_cap=${daily_notional_cap:,.0f} "
        f"({daily_notional_cap_pct*100:.0f}% of ${risk_base_capital:,.0f}), "
        f"max_daily_positions={max_daily_positions}, "
        f"已开仓: ${today_opened_notional:,.0f}/{today_opened_count}笔, "
        f"在仓标的={open_positions_count}/{max_open_positions}"
    )

    return {
        "max_deploy": max_deploy,
        "playbook_config": playbook_config,
        "budget_limit": budget_limit,
        "risk_base_capital": risk_base_capital,
        "daily_notional_cap": daily_notional_cap,
        "daily_notional_cap_pct": daily_notional_cap_pct,
        "max_daily_positions": max_daily_positions,
        "max_new_per_cycle": max_new_per_cycle,
        "max_open_positions": max_open_positions,
        "today_opened_notional": today_opened_notional,
        "today_opened_count": today_opened_count,
        "open_positions_count": open_positions_count,
    }, None
