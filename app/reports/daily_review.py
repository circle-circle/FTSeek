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
            host=_os.getenv("OPEND_HOST", "127.0.0.1"),
            port=int(_os.getenv("OPEND_PORT", "11111")),
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
            host=_os.getenv("OPEND_HOST", "127.0.0.1"),
            port=int(_os.getenv("OPEND_PORT", "11111")),
            is_encrypt=False,
        )
        ret, pos = ctx.position_list_query(trd_env=TrdEnv.SIMULATE, acc_id=acc_id)
        ctx.close()
        if ret == RET_OK:
            return len(pos)
    except Exception as e:
        logger.debug(f"[日报] 查询持仓数失败: {e}")
    return 0


def _format_option_positions(opt_positions: List[Dict[str, Any]]) -> List[str]:
    """格式化期权持仓段落"""
    if not opt_positions:
        return []

    from app.execution.options_selector import OptionSelector

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

        dte = OptionSelector.days_to_expiry(expiry) if expiry else "?"
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

    db_path = str(BASE_DIR / "data" / "ftseek_us.db")
    store = TradeStore(db_path)
    stats = store.stats_by_date(date_str)

    # 期权持仓（未平仓）
    opt_positions = store.load_option_positions()

    sep = "━" * 24

    # ── 无交易 ──
    if stats.get("total_trades", 0) == 0:
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

    # ── 有交易 ──
    total   = stats["total_trades"]
    wins    = stats["wins"]
    losses  = stats["losses"]
    win_rate = stats["win_rate"]
    total_pnl = stats["total_pnl"]
    avg_pnl   = stats["avg_pnl"]
    avg_hold  = stats["avg_holding_hours"]
    avg_str   = stats["avg_strength"]
    avg_conf  = stats["avg_confidence"]
    exit_dist = stats["exit_reason_dist"]
    records   = stats["records"]

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
        f"  成交笔数：{total} 笔  胜率：{win_rate:.0%}  ({wins}胜/{losses}负)",
        f"  总盈亏：{pnl_sign}${total_pnl:.2f}  均值：{pnl_sign}${avg_pnl:.2f}/笔",
    ]
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
