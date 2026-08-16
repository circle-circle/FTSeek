"""回撤控制器

P0 修复 (2026-04-17):
- 支持动态资产基准（启动时查询真实资产）
- 支持入金/出金后的基准更新
- 保存基准变更历史，用于复盘审计

P0-fix (2026-06-15):
- 日内回撤状态持久化到 SQLite, 防止重启归零绕过 3% 日限
"""
from datetime import datetime, date
from typing import Optional, List, Dict, Any
import logging
import os
import sqlite3

from app.utils.timezone import _et_trade_date, now_et

logger = logging.getLogger(__name__)

# DB 路径: data/drawdown_state.db
_DB_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'drawdown_state.db')


class DrawdownBaselineRecord:
    """基准变更历史记录"""
    def __init__(
        self,
        old_baseline: float,
        new_baseline: float,
        reason: str,
        timestamp: Optional[datetime] = None,
    ):
        self.old_baseline = old_baseline
        self.new_baseline = new_baseline
        self.reason = reason
        self.timestamp = timestamp or datetime.now()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "old_baseline": self.old_baseline,
            "new_baseline": self.new_baseline,
            "change_pct": (self.new_baseline - self.old_baseline) / max(0.01, self.old_baseline) * 100,
            "reason": self.reason,
            "timestamp": self.timestamp.isoformat(),
        }


class DrawdownController:
    """回撤控制器

    计算逻辑：
        current_total_dd = (baseline - current_assets) / baseline

    baseline 来源（优先级从高到低）：
        1. 用户/系统手动更新（入金/出金后）
        2. 每日开盘前自动更新（取前一日收盘总资产）
        3. 启动时通过 broker.get_account_info() 查询
        4. config 中的初始值（兜底）
    """

    def __init__(
        self,
        max_daily_drawdown: float = 0.03,
        max_total_drawdown: float = 0.08,
        initial_baseline: float = 0.0,
    ):
        self.max_daily = max_daily_drawdown
        self.max_total = max_total_drawdown

        # 资产基准
        self.baseline = initial_baseline
        self.current_assets = initial_baseline
        self.daily_start_assets = initial_baseline

        # 回撤值（绝对金额）
        self.current_daily_dd_abs = 0.0
        self.current_total_dd_abs = 0.0

        # 回撤百分比（由 update_assets 自动计算）
        self.current_daily_dd = 0.0
        self.current_total_dd = 0.0

        self.last_reset_date: Optional[date] = None
        self.block_reason: Optional[str] = None

        # 基准变更历史
        self.baseline_history: List[DrawdownBaselineRecord] = []

        # P2 修复 (2026-05-11): 入金/出金自动检测阈值 (净值突变倍数)
        self.auto_reset_threshold = 5.0  # 当前资产 vs 基准差异 >5x 时自动重置

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
                self.block_reason = row[2] if row[2] else None
                if float(row[3]) > 0:
                    self.baseline = float(row[3])
                if float(row[4]) > 0:
                    self.current_assets = float(row[4])
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
    # 基准管理
    # ------------------------------------------------------------------

    def set_baseline(self, new_baseline: float, reason: str = "manual"):
        """设置/更新资产基准（如入金/出金后）"""
        old = self.baseline
        if old > 0:
            record = DrawdownBaselineRecord(old, new_baseline, reason)
            self.baseline_history.append(record)
            logger.info(
                f"[回撤基准] 更新: {old:.2f} -> {new_baseline:.2f} "
                f"({reason}, 变更 {record.to_dict()['change_pct']:.2f}%)"
            )
        else:
            logger.info(f"[回撤基准] 初始设置: {new_baseline:.2f} ({reason})")

        self.baseline = new_baseline
        self.current_assets = new_baseline
        self.daily_start_assets = new_baseline
        self.current_daily_dd = 0.0
        self.current_total_dd = 0.0
        self._check_limits()
        self._save_state()

    def reset_daily_baseline(self, reason: str = "daily_open"):
        """每日开盘前重置日内基准"""
        old = self.daily_start_assets
        self.daily_start_assets = self.current_assets
        self.current_daily_dd_abs = 0.0
        self.current_daily_dd = 0.0
        self.last_reset_date = now_et().date()
        logger.info(
            f"[回撤基准] 日内基准重置: {old:.2f} -> {self.daily_start_assets:.2f} ({reason})"
        )
        self._save_state()

    # ------------------------------------------------------------------
    # 资产更新
    # ------------------------------------------------------------------

    def update_assets(self, current_assets: float):
        """
        更新当前总资产，自动计算回撤百分比。
        P2 修复 (2026-05-11): 检测入金/出金导致的净值突变，自动重置基准

        调用方通常通过 broker.get_account_info()["total_assets"] 获取后传入。
        """
        if self.baseline <= 0:
            logger.warning("[回撤] 基准未设置，无法计算回撤")
            return

        # P2 修复 (2026-05-11): 入金/出金检测 — 净值突变时自动重置基准
        # [DEB-fullchain P1-12 2026-08-16] 修复方向性背门: 原 max(cur/base, base/cur)
        #   对称判定 → 大亏损 (基准/当前 >5x, 即回撤 >80%) 同样触发 set_baseline →
        #   total/daily dd 双清零 + 熔断复位, 恰在最需要熔断时拆熔断 (fail-open)。
        #   现仅入金方向 (current > baseline×threshold) 自动重置; 出金/亏损方向
        #   维持基准 → 回撤偏严 → 需人工 set_baseline (fail-safe 方向)。
        #   阈值 5.0 不变 (红线①: 不动参数)。
        if current_assets > 0 and self.baseline > 0:
            if current_assets > self.baseline * self.auto_reset_threshold:
                logger.warning(
                    f"[回撤] 入金检测: 当前=${current_assets:,.0f} vs 基准=${self.baseline:,.0f} "
                    f"(>{self.auto_reset_threshold}x), 自动重置基准"
                )
                self.set_baseline(current_assets, reason="auto_reset_capital_change")
                return

        self.current_assets = current_assets

        # 计算总回撤
        self.current_total_dd_abs = max(0, self.baseline - current_assets)
        self.current_total_dd = self.current_total_dd_abs / self.baseline

        # 计算日内回撤
        self.current_daily_dd_abs = max(0, self.daily_start_assets - current_assets)
        self.current_daily_dd = self.current_daily_dd_abs / self.daily_start_assets

        self._check_limits()
        self._save_state()

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

    def update_drawdown(self, daily_dd: float, total_dd: float):
        """兼容旧接口：直接传入回撤百分比"""
        self.current_daily_dd = daily_dd
        self.current_total_dd = total_dd
        self._check_limits()

    # ------------------------------------------------------------------
    # 限制检查
    # ------------------------------------------------------------------

    def _check_limits(self):
        """检查回撤限制 (动态应用模式感知限额)"""
        self.block_reason = None

        # 模式感知日亏损限额: 从 TradingMode 读取 YAML mode_adaptation 配置
        effective_daily = self._get_effective_max_daily()

        if self.current_daily_dd > effective_daily:
            self.block_reason = "DAILY_DRAWDOWN"
        elif self.current_total_dd > self.max_total:
            self.block_reason = "TOTAL_DRAWDOWN"

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

    def reset(self):
        """重置控制器"""
        self.current_daily_dd = 0.0
        self.current_total_dd = 0.0
        self.current_daily_dd_abs = 0.0
        self.current_total_dd_abs = 0.0
        self.block_reason = None

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """获取完整回撤状态"""
        return {
            "baseline": self.baseline,
            "current_assets": self.current_assets,
            "daily_start_assets": self.daily_start_assets,
            "daily_drawdown_abs": round(self.current_daily_dd_abs, 2),
            "daily_drawdown_pct": round(self.current_daily_dd * 100, 2),
            "total_drawdown_abs": round(self.current_total_dd_abs, 2),
            "total_drawdown_pct": round(self.current_total_dd * 100, 2),
            "max_daily_allowed": round(self._get_effective_max_daily() * 100, 2),
            "max_daily_configured": round(self.max_daily * 100, 2),
            "max_total_allowed": round(self.max_total * 100, 2),
            "blocked": self.block_reason is not None,
            "block_reason": self.block_reason,
            "baseline_changes": len(self.baseline_history),
        }

    def get_baseline_history(self) -> List[Dict[str, Any]]:
        """获取基准变更历史"""
        return [r.to_dict() for r in self.baseline_history]
