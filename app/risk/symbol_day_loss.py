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
# ── 日亏损防线登记 (2026-08-28 冲突显性化): "当日亏损"事实六套权威并存 ──
#   1. AccountCircuitBreaker  app/risk/account_circuit_breaker.py
#      净值 -1%/-2%/-3% 三档 (减半/停新仓/停新仓 — 全平从未接线)
#   2. AbsoluteCircuitBreaker app/risk/absolute_circuit_breaker.py
#      -$2000/日 + -$1000/时 + 连亏5笔 (类常量, 不受开关控制)
#   3. symbol_day_loss        app/risk/symbol_day_loss.py
#      -$500/标的/日 (出场日+净额, 主线/playbook 共用)
#   4. SymbolDailyLossGuard   app/risk/symbol_daily_loss_guard.py
#      净值0.5%/标的/日 + 连亏N笔 + 低胜率 (yaml risk.symbol_daily_loss_guard.*)
#   5. RiskCircuitBreaker     app/playbook/risk_circuit_breaker.py
#      总资金1% + 连亏3减半/连亏5停1日 (playbook 域; 周报告警组件 — "禁执行按钮+飞书"
#      从未接线, 全仓唯一调用方=周五周报 logger.warning, 仅记录不阻断; 2026-08-30 DEB-timezone 第4轮)
#   6. DrawdownController     app/risk/drawdown_control.py
#      日3% (risk.account.max_daily_drawdown, 持久化防重启归零; 仅主线平仓在喂)
# 六套独立查询/独立状态互不感知; 口径 % 与 $ 混杂, 触发先后交错, 合并/裁剪属
# 人工风险裁决 (每套有各自历史语义与恢复机制)。本登记保证读任一防线时看得见
# 全景; 已统一项: 1/3 出场日+净额 (2026-08-27/28), 其余各口径见各自头注。

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
