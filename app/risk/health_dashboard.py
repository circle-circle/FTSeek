"""
交易健康度看板数据采集器 (Health Dashboard Collector)

负责采集4个维度的指标：
  - 策略层：胜率/盈亏比/期望收益/最大回撤/Sharpe/Profit Factor
  - 执行层：成交率/平均滑点/下单延迟/撤单成功率/部分成交比例
  - 风控层：风控拦截率/熔断次数/单品种敞口/总敞口/杠杆使用率
  - 系统层：API错误率/行情延迟/DB写入延迟/任务堆积/服务存活状态
"""
import os
import time
import sqlite3
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Tuple, Any

logger = logging.getLogger("ftseek.health_dashboard")
logger.propagate = False  # 防止日志重复

# 暂时不可采集的指标统一使用此占位值
_NA = None


@dataclass
class HealthDashboard:
    """健康度看板 - 完整指标集"""

    # ── 策略层 ──────────────────────────────────────────────
    win_rate: Optional[float] = None          # 胜率 0-1
    profit_loss_ratio: Optional[float] = None  # 盈亏比 avg_win/avg_loss
    expectancy: Optional[float] = None         # 期望收益 (HKD)
    max_drawdown: Optional[float] = None        # 最大回撤 0-1
    sharpe_ratio: Optional[float] = None        # 夏普比率
    profit_factor: Optional[float] = None       # Profit Factor = 总盈 / |总亏|
    total_trades: int = 0
    winning_trades: int = 0
    today_pnl: float = 0.0

    # ── 执行层 ──────────────────────────────────────────────
    fill_rate: Optional[float] = None           # 成交率 = 成功下单 / 总信号
    avg_slippage_pct: Optional[float] = None    # 平均滑点 %（signal价 vs 成交价）
    avg_order_latency_ms: Optional[float] = None  # 下单延迟 ms（待接入）
    cancel_success_rate: Optional[float] = None   # 撤单成功率（待接入）
    partial_fill_ratio: Optional[float] = None    # 部分成交比例（待接入）

    # ── 风控层 ──────────────────────────────────────────────
    risk_intercept_rate: Optional[float] = None  # 拦截率 = 拒绝 / 总信号
    fuse_count: int = 0                           # 今日熔断触发次数
    total_rejects_today: int = 0                  # 今日总拒绝次数
    max_single_exposure: Optional[float] = None   # 最大单品种敞口 %
    max_single_code: str = ""                      # 最大单品种代码
    total_exposure: Optional[float] = None         # 总敞口 %
    leverage_ratio: Optional[float] = None         # 杠杆使用率（待接入）

    # ── 系统层 ──────────────────────────────────────────────
    api_error_rate: Optional[float] = None      # API错误率（待接入）
    quote_delay_sec: Optional[float] = None     # 行情延迟 (秒)
    db_write_latency_ms: Optional[float] = None  # DB写入延迟 (ms)
    task_queue_size: int = 0                     # 执行器信号队列堆积
    service_alive: bool = True

    timestamp: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))


class HealthDashboardCollector:
    """
    健康度看板数据采集器

    依赖注入：
      risk_manager    - 账户信息/持仓敞口
      circuit_breaker - 熔断/拒绝次数
      subscriber_ref  - 行情心跳时间 (_last_quote_time)
      executor_ref    - 信号队列/滑点记录
      db_path         - SQLite 数据库路径（测量写入延迟）
    """

    def __init__(
        self,
        risk_manager=None,
        circuit_breaker=None,
        subscriber_ref=None,
        executor_ref=None,
        db_path: str = None,
    ):
        self._rm = risk_manager
        self._cb = circuit_breaker
        self._sub = subscriber_ref
        self._exec = executor_ref
        self._db_path = db_path or "/opt/ftseek/FTSeek/data/ftseek.db"

    # ------------------------------------------------------------------ #
    #  主采集入口
    # ------------------------------------------------------------------ #
    def collect(self) -> HealthDashboard:
        dash = HealthDashboard()
        try:
            self._collect_strategy_layer(dash)
        except Exception as e:
            logger.warning(f"[健康看板] 策略层采集异常: {e}")
        try:
            self._collect_execution_layer(dash)
        except Exception as e:
            logger.warning(f"[健康看板] 执行层采集异常: {e}")
        try:
            self._collect_risk_layer(dash)
        except Exception as e:
            logger.warning(f"[健康看板] 风控层采集异常: {e}")
        try:
            self._collect_system_layer(dash)
        except Exception as e:
            logger.warning(f"[健康看板] 系统层采集异常: {e}")
        return dash

    # ------------------------------------------------------------------ #
    #  策略层
    # ------------------------------------------------------------------ #
    def _collect_strategy_layer(self, dash: HealthDashboard):
        from app.storage.signal_tracker import signal_tracker

        stats = signal_tracker.get_statistics()
        if not stats:
            return

        today_signals = signal_tracker.get_today_signals()
        traded = [r for r in today_signals if r.pnl is not None]
        wins = [r for r in traded if r.pnl > 0]
        losses = [r for r in traded if r.pnl <= 0]

        dash.total_trades = len(traded)
        dash.winning_trades = len(wins)
        dash.today_pnl = sum(r.pnl for r in traded)

        # 若 signal_tracker 无平仓记录，尝试从 risk_manager 获取今日盈亏
        if dash.today_pnl == 0.0 and self._rm:
            try:
                rm_status = self._rm.get_status()
                today_info = rm_status.get("today", {})
                pnl_str = today_info.get("pnl", None)
                if pnl_str not in (None, "", "0.00"):
                    dash.today_pnl = float(pnl_str)
            except Exception:
                pass

        if traded:
            dash.win_rate = len(wins) / len(traded)

        avg_win = sum(r.pnl for r in wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(r.pnl for r in losses) / len(losses)) if losses else 0.0

        if avg_loss > 0:
            dash.profit_loss_ratio = round(avg_win / avg_loss, 2)
        if dash.win_rate is not None and avg_loss > 0:
            dash.expectancy = round(
                dash.win_rate * avg_win - (1 - dash.win_rate) * avg_loss, 2
            )

        total_profit = sum(r.pnl for r in wins)
        total_loss = abs(sum(r.pnl for r in losses))
        if total_loss > 0:
            dash.profit_factor = round(total_profit / total_loss, 2)

        # Sharpe & 最大回撤：用每笔盈亏序列近似
        pnl_series = [r.pnl for r in traded]
        if len(pnl_series) >= 3:
            try:
                import statistics
                mean_r = statistics.mean(pnl_series)
                std_r = statistics.stdev(pnl_series)
                if std_r > 0:
                    dash.sharpe_ratio = round(mean_r / std_r, 2)
            except Exception:
                pass

        # 最大回撤（资金曲线累计盈亏）
        if pnl_series:
            peak = 0.0
            cum = 0.0
            max_dd = 0.0
            for p in pnl_series:
                cum += p
                if cum > peak:
                    peak = cum
                dd = (peak - cum) / peak if peak > 0 else 0
                if dd > max_dd:
                    max_dd = dd
            dash.max_drawdown = round(max_dd, 4)

    # ------------------------------------------------------------------ #
    #  执行层
    # ------------------------------------------------------------------ #
    def _collect_execution_layer(self, dash: HealthDashboard):
        from app.storage.signal_tracker import signal_tracker

        stats = signal_tracker.get_statistics()
        if stats and stats.get("total_signals", 0) > 0:
            dash.fill_rate = stats.get("execution_rate")

        # 滑点：从 executor 的内存记录读取
        if self._exec and hasattr(self._exec, "_slippage_records"):
            records: List[Tuple[float, float, str]] = self._exec._slippage_records
            if records:
                slippages = []
                for signal_px, dealt_px, _ in records:
                    if signal_px > 0 and dealt_px > 0:
                        slip = abs(dealt_px - signal_px) / signal_px
                        slippages.append(slip)
                if slippages:
                    dash.avg_slippage_pct = round(
                        sum(slippages) / len(slippages) * 100, 4
                    )

        # 任务堆积（当前 executor 中仍在 30s TTL 内的 pending signals）
        if self._exec and hasattr(self._exec, "_pending_signals"):
            dash.task_queue_size = len(self._exec._pending_signals)

        # 下单延迟/撤单/部分成交 → 待接入
        dash.avg_order_latency_ms = _NA
        dash.cancel_success_rate = _NA
        dash.partial_fill_ratio = _NA

    # ------------------------------------------------------------------ #
    #  风控层
    # ------------------------------------------------------------------ #
    def _collect_risk_layer(self, dash: HealthDashboard):
        # 熔断/拒绝信息
        if self._cb:
            cb_status = self._cb.get_status()
            # 是否当前处于熔断暂停中（paused=True 计为1次）
            dash.fuse_count = 1 if cb_status.get("paused") else 0
            dash.total_rejects_today = cb_status.get("total_rejects_today", 0)

        # 拦截率：拒绝数 / 总信号数
        from app.storage.signal_tracker import signal_tracker
        stats = signal_tracker.get_statistics()
        if stats and stats.get("total_signals", 0) > 0:
            dash.risk_intercept_rate = round(
                stats["rejected"] / stats["total_signals"], 4
            )

        # 杠杆：模拟账户无杠杆
        dash.leverage_ratio = _NA

        # 单品种 / 总敞口（从 risk_manager 账户读取）
        if self._rm:
            try:
                status = self._rm.get_status()
                account = self._rm.account
                total_value = account.get("total_value", 0) or account.get("net_assets", 0)
                positions = account.get("positions", {})

                if total_value > 0 and positions:
                    exposures: List[Tuple[str, float]] = []
                    for code, pos in positions.items():
                        qty = pos.get("quantity", 0)
                        price = pos.get("current_price", pos.get("entry_price", 0))
                        mkt_val = qty * price
                        exposures.append((code, mkt_val))

                    total_exp = sum(v for _, v in exposures)
                    dash.total_exposure = round(total_exp / total_value * 100, 1)

                    if exposures:
                        max_code, max_val = max(exposures, key=lambda x: x[1])
                        dash.max_single_exposure = round(max_val / total_value * 100, 1)
                        dash.max_single_code = max_code
            except Exception as e:
                logger.debug(f"[健康看板] 敞口采集失败: {e}")

        # 若 risk_manager 不可用，尝试从 positions DB 直接读
        if dash.total_exposure is None:
            try:
                self._collect_exposure_from_db(dash)
            except Exception as e:
                logger.debug(f"[健康看板] DB敞口采集失败: {e}")

    def _collect_exposure_from_db(self, dash: HealthDashboard):
        """从 positions 表采集当前持仓敞口（回退方案）"""
        conn = sqlite3.connect(self._db_path, timeout=3)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT code, quantity, entry_price FROM positions WHERE status='OPEN'"
            )
            rows = cur.fetchall()
        finally:
            conn.close()

        if not rows:
            return

        # 无法获取总净值时，用持仓成本估算
        total_cost = sum(q * p for _, q, p in rows)
        if total_cost <= 0:
            return

        code_costs: List[Tuple[str, float]] = [
            (c, q * p) for c, q, p in rows
        ]
        dash.total_exposure = round(100.0, 1)  # 全部持仓
        if code_costs:
            max_code, max_cost = max(code_costs, key=lambda x: x[1])
            dash.max_single_code = max_code
            dash.max_single_exposure = round(max_cost / total_cost * 100, 1)

    # ------------------------------------------------------------------ #
    #  系统层
    # ------------------------------------------------------------------ #
    def _collect_system_layer(self, dash: HealthDashboard):
        dash.service_alive = True  # 自身运行中，必为 True

        # 行情延迟
        if self._sub and hasattr(self._sub, "_last_quote_time"):
            last_t = self._sub._last_quote_time
            if last_t:
                dash.quote_delay_sec = round(time.time() - last_t, 1)

        # DB 写入延迟：测量一次简单写操作耗时
        try:
            conn = sqlite3.connect(self._db_path, timeout=3)
            t0 = time.perf_counter()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS _health_ping (ts INTEGER)"
            )
            conn.execute("INSERT INTO _health_ping VALUES (?)", (int(time.time()),))
            conn.commit()
            conn.execute("DELETE FROM _health_ping WHERE ts < ?", (int(time.time()) - 60,))
            conn.commit()
            conn.close()
            dash.db_write_latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        except Exception as e:
            logger.debug(f"[健康看板] DB延迟测量失败: {e}")

        # 任务堆积已在执行层收集，这里不再重复
        # API 错误率 → 待接入
        dash.api_error_rate = _NA
