"""单标的当日已了结净亏损 — 主线与 playbook 共用的一把尺 (2026-08-28 全链路复查).

背景: "$500/标的/日" 死亡螺旋防线原先只写在
app/pipeline/order_executor._check_symbol_daily_loss 里, 而该函数位于 2026-07-02 起
冻结的 MAIN_PIPELINE 开仓路径 (:3140 硬拦非 playbook 来源, playbook 经
dual_broker 直连下单不经 order_executor) → **实盘唯一执行链路上没有任何单标的
日亏损门** (8/27 UBER 隔夜跳空 -307.13 即属该防线本应覆盖的形态)。

本模块把"当日该标的已了结净额"抽成单一实现, 两条链共用同一阈值与同一口径,
不再各写一份 SQL。口径沿用全仓已确立约定:
  出场日归集 substr(exit_time,1,10) (ET 墙钟日前 10 位)
  + 净额 COALESCE(pnl_net, realized_pnl)
  + 只算已了结 exit_price > 0

回滚: 删本文件并还原两处调用点即可。
"""

import logging
import sqlite3
from typing import Optional, Tuple

logger = logging.getLogger("ftseek.risk.symbol_day_loss")

# 单标的当日累计亏损限额 (USD) — 主线与 playbook 共用此一处定义
DAILY_LOSS_LIMIT_USD = 500.0


def symbol_day_net_pnl(symbol: str, et_date: Optional[str] = None,
                       db_path: Optional[str] = None) -> float:
    """某标的在指定 ET 交易日已了结的净盈亏 (亏损为负).

    Args:
        symbol: 标的代码 (与 trade_records.symbol 同格式, 如 "US.MU")
        et_date: "YYYY-MM-DD" ET 交易日, 缺省取当前 ET 日
        db_path: 覆盖主库路径 (测试用), 缺省走 db_utils.get_main_db()
    """
    if et_date is None:
        from app.utils.timezone import get_trade_date
        et_date = get_trade_date()

    if db_path is None:
        from app.core.db_utils import get_main_db
        db_path = get_main_db()

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(COALESCE(pnl_net, realized_pnl)), 0) FROM trade_records "
            "WHERE symbol = ? AND substr(exit_time, 1, 10) = ? AND exit_price > 0",
            (symbol, et_date),
        ).fetchone()
    finally:
        conn.close()
    return float(row[0]) if row and row[0] is not None else 0.0


def is_symbol_day_loss_exceeded(symbol: str, limit_usd: float = DAILY_LOSS_LIMIT_USD,
                                et_date: Optional[str] = None,
                                db_path: Optional[str] = None) -> Tuple[bool, float]:
    """判据: 该标的当日已了结净额是否已跌破限额.

    Returns:
        (blocked, day_net_pnl) — 查询异常时 raised 由调用方决定 fail 语义,
        本函数不做静默兜底 (两处调用方各自记录 WARNING 并选择放行/拒绝)。
    """
    day_net = symbol_day_net_pnl(symbol, et_date=et_date, db_path=db_path)
    return day_net <= -abs(float(limit_usd)), day_net
