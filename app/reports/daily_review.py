"""
FTSeek-US 美股交易日报复盘生成器

从 TradeStore 读取当日交易记录，生成结构化文本报告，
通过钉钉推送。每个交易日 16:05 ET 收盘后自动触发。
"""
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def _get_account_assets() -> float:
    """尝试从富途 OpenD 查询美股模拟账户净值，失败时返回 0"""
    try:
        import os as _os
        from futu import OpenSecTradeContext, TrdMarket, TrdEnv, RET_OK
        acc_id = int(_os.getenv("SIMULATE_ACCOUNT_ID", "8246816"))
        ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host=_os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
            port=int(_os.getenv("FUTU_OPEND_PORT", "11111")),
            is_encrypt=False,
        )
        ret, info = ctx.accinfo_query(trd_env=TrdEnv.SIMULATE, acc_id=acc_id)
        ctx.close()
        if ret == RET_OK and len(info) > 0:
            return float(info.iloc[0]["total_assets"])
    except Exception as e:
        logger.debug(f"[日报] 查询账户净值失败: {e}")
    return 0.0


def _get_open_positions_count() -> int:
    """尝试从富途 OpenD 查询当前持仓数"""
    try:
        import os as _os
        from futu import OpenSecTradeContext, TrdMarket, TrdEnv, RET_OK
        acc_id = int(_os.getenv("SIMULATE_ACCOUNT_ID", "8246816"))
        ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host=_os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
            port=int(_os.getenv("FUTU_OPEND_PORT", "11111")),
            is_encrypt=False,
        )
        ret, pos = ctx.position_list_query(trd_env=TrdEnv.SIMULATE, acc_id=acc_id)
        ctx.close()
        if ret == RET_OK:
            return len(pos)
    except Exception as e:
        logger.debug(f"[日报] 查询持仓数失败: {e}")
    return 0


def _get_new_open_positions(db_path: str, date_str: str) -> List[Dict[str, Any]]:
    """
    P4 修复: 查询当日新开仓但未平仓的持仓。
    
    从 open_positions 表中查找 entry_time 在 date_str 当天的记录，
    这些是策略开了但还没平掉的仓位。复盘报 0 交易的根因就是这些仓位
    不在 trade_records 中（trade_records 只记已平仓的）。
    """
    import sqlite3
    result = []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT symbol, direction, size, avg_entry_price, current_sl, "
            "entry_time, trader_type, strategy_id, source "
            "FROM open_positions "
            "WHERE entry_time LIKE ? || '%' "
            "AND (sync_status IS NULL OR sync_status NOT IN "
            "('closed_by_daemon','closed_by_scheduler','closed_by_sync','closed_emergency','stale_not_in_broker')) "
            "ORDER BY entry_time",
            (date_str,)
        )
        for row in cursor.fetchall():
            result.append(dict(row))
        conn.close()
    except Exception as e:
        logger.debug(f"[日报·P4] 查询新开仓失败: {e}")
    return result


def _format_option_positions(opt_positions: List[Dict[str, Any]]) -> List[str]:
    """格式化期权持仓段落"""
    if not opt_positions:
        return []

    from app.execution.options_selector import days_to_expiry

    lines = ["", "[ Put期权持仓（空头代理） ]"]
    for op in opt_positions:
        symbol       = op["symbol"].replace("US.", "")
        option_code  = op.get("option_code", "")
        qty          = op.get("qty", 0)
        entry_price  = op.get("entry_price", 0)
        strike       = op.get("strike_price", 0)
        expiry       = op.get("expiry_date", "")
        cost         = op.get("cost_total", entry_price * qty * 100)
        is_sim       = op.get("is_simulated", False)
        sim_tag      = "[模拟]" if is_sim else "[真实]"

        dte = days_to_expiry(expiry) if expiry else "?"
        lines.append(
            f"  {symbol:<6} Put {sim_tag}  "
            f"K={strike:.0f}  到期:{expiry}(DTE={dte})\n"
            f"         {option_code}\n"
            f"         {qty}手 @ 入${entry_price:.2f}  总成本≈${cost:.0f}"
        )
    return lines


def generate_daily_review(review_date: Optional[date] = None) -> str:
    """
    生成指定日期的交易复盘报告文本

    Args:
        review_date: 要复盘的日期，默认今日

    Returns:
        格式化的复盘文本，可直接推送钉钉
    """
    from app.storage.trade_store import TradeStore

    if review_date is None:
        review_date = date.today()

    date_str = review_date.strftime("%Y-%m-%d")
    weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][review_date.weekday()]

    from app.core.db_utils import get_main_db
    db_path = str(get_main_db())
    store = TradeStore(db_path)
    stats = store.stats_by_date(date_str)

    # 期权持仓（未平仓）
    opt_positions = store.load_option_positions()

    sep = "━" * 24

    # ── P4 修复 (2026-06-09): 查询今日新开仓（含未平仓），避免报"0 交易"──
    new_open_positions = _get_new_open_positions(db_path, date_str)

    # ── 无平仓交易 ──
    if stats.get("total_trades", 0) == 0 and not new_open_positions:
        assets = _get_account_assets()
        open_pos = _get_open_positions_count()
        lines = [
            f"【FTSeek-US 美股日报】{date_str} {weekday_cn}",
            sep,
            "今日无平仓交易记录",
            "",
        ]
        if open_pos > 0:
            lines.append(f"持仓中：{open_pos} 只（未平仓）")
        if assets > 0:
            lines.append(f"账户净值：${assets:,.2f}")

        # 期权持仓段（即使当日无平仓，也展示未平仓期权）
        lines += _format_option_positions(opt_positions)

        lines += [sep, "FTSeek-US 模拟交易系统"]
        return "\n".join(lines)

    # ── 有交易或有新开仓 ──
    total   = stats.get("total_trades", 0)
    wins    = stats.get("wins", 0)
    losses  = stats.get("losses", 0)
    win_rate = stats.get("win_rate", 0.0)
    total_pnl = stats.get("total_pnl", 0.0)
    avg_pnl   = stats.get("avg_pnl", 0.0)
    avg_hold  = stats.get("avg_holding_hours", 0.0)
    avg_str   = stats.get("avg_strength", 0)
    avg_conf  = stats.get("avg_confidence", 0.0)
    exit_dist = stats.get("exit_reason_dist", {})
    records   = stats.get("records", [])

    # 区分普通交易和期权交易
    stock_records  = [r for r in records if "OPTION" not in r.get("direction", "")]
    option_records = [r for r in records if "OPTION" in r.get("direction", "")]

    # 盈亏比（仅普通股票）
    win_pnl  = sum(r["realized_pnl"] for r in records if r["realized_pnl"] > 0)
    loss_pnl = abs(sum(r["realized_pnl"] for r in records if r["realized_pnl"] < 0))
    rr_ratio = (win_pnl / wins) / (loss_pnl / losses) if wins > 0 and losses > 0 else 0.0

    pnl_sign = "+" if total_pnl >= 0 else ""
    assets = _get_account_assets()
    open_pos = _get_open_positions_count()

    lines = [
        f"【FTSeek-US 美股日报】{date_str} {weekday_cn}",
        sep,
        "[ 交易汇总 ]",
    ]
    # P4: 头部同时展示平仓和开仓
    _new_open_cnt = len(new_open_positions)
    if total > 0 and _new_open_cnt > 0:
        lines.append(f"  平仓：{total} 笔 ({wins}胜/{losses}负)  新开仓：{_new_open_cnt} 笔")
    elif total > 0:
        lines.append(f"  平仓：{total} 笔  胜率：{win_rate:.0%}  ({wins}胜/{losses}负)")
    elif _new_open_cnt > 0:
        lines.append(f"  今日无平仓  新开仓：{_new_open_cnt} 笔（持仓中）")
    if total > 0:
        lines.append(f"  总盈亏：{pnl_sign}${total_pnl:.2f}  均值：{pnl_sign}${avg_pnl:.2f}/笔")
    if rr_ratio > 0:
        lines.append(f"  盈亏比：{rr_ratio:.1f}:1  均持仓：{avg_hold:.1f}h")
    else:
        lines.append(f"  均持仓：{avg_hold:.1f}h")

    # 普通股票个股明细
    if stock_records:
        lines += ["", "[ 个股明细 ]"]
        for r in stock_records:
            symbol_short = r["symbol"].replace("US.", "")
            direction_cn = "做多" if r["direction"] == "LONG" else "做空"
            pnl = r["realized_pnl"]
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            reason = r.get("exit_reason", "")
            lines.append(
                f"  {symbol_short:<6} {direction_cn}  "
                f"入${r['entry_price']:.2f}→出${r['exit_price']:.2f}  "
                f"{pnl_str}  {r['holding_hours']:.1f}h  [{reason}]"
            )

    # 期权平仓记录
    if option_records:
        opt_pnl_total = sum(r["realized_pnl"] for r in option_records)
        opt_pnl_sign  = "+" if opt_pnl_total >= 0 else ""
        lines += ["", "[ Put期权平仓记录 ]",
                  f"  共 {len(option_records)} 笔  合计盈亏：{opt_pnl_sign}${opt_pnl_total:.2f}"]
        for r in option_records:
            symbol_short = r["symbol"].replace("US.", "")
            pnl = r["realized_pnl"]
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            reason = r.get("exit_reason", "")
            entry_p = r.get("entry_price", 0)
            exit_p  = r.get("exit_price", 0)
            qty     = r.get("quantity", 0)
            lines.append(
                f"  {symbol_short:<6} Put空  "
                f"入${entry_p:.2f}→出${exit_p:.2f}  "
                f"{qty}手  {pnl_str}  [{reason}]"
            )

    # 出场原因分布
    if exit_dist:
        dist_str = "  ".join(f"{k}×{v}" for k, v in sorted(exit_dist.items()))
        lines += ["", f"  出场：{dist_str}"]

    # 信号质量
    lines += [
        "",
        "[ 信号质量 ]",
        f"  均置信度：{avg_conf:.2f}  均信号强度：{avg_str:.0f}",
    ]

    # 未平仓期权持仓段
    lines += _format_option_positions(opt_positions)

    # P4 修复: 今日新开仓（含未平仓）
    if new_open_positions:
        lines += ["", "[ 今日新开仓（未平仓）]"]
        for p in new_open_positions:
            sym = p["symbol"].replace("US.", "")
            dir_cn = "做多" if p["direction"] == "LONG" else "做空"
            entry_t = p.get("entry_time", "")
            # 提取时间部分 HH:MM
            if "T" in entry_t:
                time_part = entry_t.split("T")[1][:5]
            else:
                time_part = "??"
            lines.append(
                f"  {sym:<6} {dir_cn}  {p['size']:.0f}股 @${p['avg_entry_price']:.2f}  "
                f"入场:{time_part}  SL=${p.get('current_sl', 0):.2f}  "
                f"[{p.get('trader_type', 'UNKNOWN')}]"
            )

    # 账户状态
    lines += ["", "[ 账户状态 ]"]
    if open_pos > 0:
        lines.append(f"  持仓中：{open_pos} 只（未平仓）")
    else:
        lines.append("  当前持仓：空仓")
    if assets > 0:
        lines.append(f"  账户净值：${assets:,.2f}")

    lines += [sep, "FTSeek-US 模拟交易系统"]
    return "\n".join(lines)


if __name__ == "__main__":
    # 手动测试：python -m app.reports.daily_review
    import sys
    target = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else None
    print(generate_daily_review(target))
