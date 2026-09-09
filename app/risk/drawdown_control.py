"""回撤控制器 (日内日亏限额)

P0 修复 (2026-04-17): 支持动态资产基准
P0-fix (2026-06-15): 日内回撤状态持久化到 SQLite, 防止重启归零绕过 3% 日限

历史注记 (DEB-20260823-system-health-check, 用户裁"不值得, 删除"):
- 原 8% 总回撤门 (max_total_drawdown / update_assets / TOTAL_DRAWDOWN 分支) 已删除:
  update_assets() 全仓零调用, 该门自诞生从未被评估过 = 死门; 总回撤防护由
  capital_safety 水位体系承担 (-20% RED 清仓 / -35% BLACKOUT 锁死, 已接线实测).
- 原 set_baseline/reset_daily_baseline/update_drawdown/get_status/
  get_baseline_history/DrawdownBaselineRecord 同批删除 (全仓零调用).
- 已知残留缺口 (未修, 见 DEB P1-M3): playbook 平仓 pnl 未喂入本控制器,
  日回撤池当前仅主线平仓在喂 (order_executor C4 点).
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
from datetime import datetime
from typing import Optional
import logging
import os
import sqlite3

from app.utils.timezone import _et_trade_date, now_et

logger = logging.getLogger(__name__)

# DB 路径: data/drawdown_state.db
_DB_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'drawdown_state.db')


class DrawdownController:
    """日内回撤控制器 (日亏限额)

    计算逻辑：
        current_daily_dd_abs 累加每笔已实现亏损 (record_trade_pnl)
        current_daily_dd = current_daily_dd_abs / daily_start_assets
    """

    def __init__(
        self,
        max_daily_drawdown: float = 0.03,
        initial_baseline: float = 0.0,
    ):
        self.max_daily = max_daily_drawdown

        # 资产基准 (baseline/current_assets 仅作持久化往返保留, 无总回撤语义)
        self.baseline = initial_baseline
        self.current_assets = initial_baseline
        self.daily_start_assets = initial_baseline

        # 日内回撤 (绝对金额 + 百分比)
        self.current_daily_dd_abs = 0.0
        self.current_daily_dd = 0.0

        self.last_reset_date: Optional[datetime.date] = None
        self.block_reason: Optional[str] = None

        # P0-fix (2026-06-15): 从 SQLite 恢复日内状态
        self._ensure_table()
        self._load_state()

    # ------------------------------------------------------------------
    # SQLite 持久化 (P0-fix 2026-06-15)
    # ------------------------------------------------------------------

    def _ensure_table(self):
        try:
            os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
            conn = sqlite3.connect(_DB_PATH, timeout=5)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS drawdown_state (
                    trade_date TEXT PRIMARY KEY,
                    daily_start_assets REAL,
                    current_daily_dd_abs REAL,
                    block_reason TEXT,
                    baseline REAL,
                    current_assets REAL,
                    updated_at TEXT
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug(f"[回撤持久化] 建表失败: {e}")

    def _load_state(self):
        """从 DB 恢复今日状态 (防止重启归零)"""
        try:
            conn = sqlite3.connect(_DB_PATH, timeout=5)
            today = _et_trade_date(now_et())
            row = conn.execute(
                "SELECT daily_start_assets, current_daily_dd_abs, block_reason, "
                "baseline, current_assets FROM drawdown_state WHERE trade_date=?",
                (today,),
            ).fetchone()
            conn.close()
            if row:
                self.daily_start_assets = float(row[0])
                self.current_daily_dd_abs = float(row[1])
                if float(row[3]) > 0:
                    self.baseline = float(row[3])
                if float(row[4]) > 0:
                    self.current_assets = float(row[4])
                # 2026-09-10 修 (陈旧block照抄): 恢复后按当前配置重算, 不照抄持久化的
                # block_reason 字符串。根因: tests test_daily_drawdown_limit 曾绕过 DB
                # 隔离, 每次 pytest 向生产库 INSERT OR REPLACE 测试指纹行 (100000/4000/
                # DAILY_DRAWDOWN, 实测 8/23-9/9 连续16行全同指纹) → _load_state 照抄
                # 后重启即加载假熔断 (main 通道 HardStop + dashboard 手动单被拦,
                # 22:11 重启日志实证)。重算=源头自愈: 污染行 4%<sim上限5% → block 清除;
                # 真实超限行重算后仍拦 (拦截方向不降级)。
                if self.daily_start_assets > 0:
                    self.current_daily_dd = (
                        self.current_daily_dd_abs / self.daily_start_assets
                    )
                self._check_limits()
                self._save_state()
                logger.info(
                    f"[回撤持久化] 恢复今日状态: daily_start=${self.daily_start_assets:,.0f} "
                    f"dd_abs=${self.current_daily_dd_abs:,.0f} block={self.block_reason}"
                )
        except Exception as e:
            logger.debug(f"[回撤持久化] 加载失败 (使用默认值): {e}")

    def _save_state(self):
        """持久化当前状态到 DB"""
        try:
            conn = sqlite3.connect(_DB_PATH, timeout=5)
            today = _et_trade_date(now_et())
            conn.execute(
                "INSERT OR REPLACE INTO drawdown_state "
                "(trade_date, daily_start_assets, current_daily_dd_abs, block_reason, "
                "baseline, current_assets, updated_at) VALUES (?,?,?,?,?,?,?)",
                (today, self.daily_start_assets, self.current_daily_dd_abs,
                 self.block_reason or "", self.baseline, self.current_assets,
                 datetime.now().isoformat()),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug(f"[回撤持久化] 保存失败: {e}")

    # ------------------------------------------------------------------
    # 日内回撤记账
    # ------------------------------------------------------------------

    def record_trade_pnl(self, pnl: float, source: str = "unknown"):
        """C4 fix (2026-05-31): 记录单笔交易 P&L, 累加到日内回撤

        供主线在平仓时调用, 确保所有策略的亏损都计入共享日回撤池。

        Args:
            pnl: 交易净盈亏 (正=盈利, 负=亏损), 单位: 美元
            source: 来源标识 ("main" / etc.)
        """
        if self.daily_start_assets <= 0:
            logger.debug(f"[回撤] record_trade_pnl 跳过: daily_start_assets={self.daily_start_assets}")
            return

        # 累加到日内回撤 (亏损增加回撤, 盈利减少回撤)
        if pnl < 0:
            self.current_daily_dd_abs += abs(pnl)
        else:
            self.current_daily_dd_abs = max(0, self.current_daily_dd_abs - pnl)

        self.current_daily_dd = self.current_daily_dd_abs / self.daily_start_assets

        if pnl < 0:
            logger.info(
                f"[回撤·{source}] 记录亏损 ${pnl:.2f} | "
                f"日内回撤: ${self.current_daily_dd_abs:.2f} "
                f"({self.current_daily_dd:.2%} / {self._get_effective_max_daily():.2%})"
            )

        self._check_limits()
        self._save_state()

    # ------------------------------------------------------------------
    # 限制检查
    # ------------------------------------------------------------------

    def _check_limits(self):
        """检查日内回撤限制 (动态应用模式感知限额)"""
        self.block_reason = None

        effective_daily = self._get_effective_max_daily()

        if self.current_daily_dd > effective_daily:
            self.block_reason = "DAILY_DRAWDOWN"

    def _get_effective_max_daily(self) -> float:
        """获取模式感知的日亏损限额 (热加载)"""
        try:
            from app.core.trading_mode import TradingMode
            return TradingMode().get_max_daily_loss_pct()
        except Exception:
            return self.max_daily

    def is_daily_limit_breached(self) -> bool:
        """检查日内回撤是否超限"""
        return self.block_reason == "DAILY_DRAWDOWN"

    def is_trading_allowed(self) -> bool:
        """检查是否允许交易"""
        return self.block_reason is None

    def get_block_reason(self) -> Optional[str]:
        """获取阻止原因"""
        return self.block_reason
