"""
资金隔离 — Scalping 独立预算管理

总资金分配:
  主管线: 70%
  Scalping: 30%

Scalping 内部风控:
  单笔最大仓位: 10% Scalping 预算
  日亏损上限: 2% 总资金 (= 6.67% Scalping 预算)
  最大同时持仓: 5 只

线程安全: 所有状态变更使用 threading.Lock
"""

import threading
import logging
from typing import Dict, Optional
from datetime import datetime, date

logger = logging.getLogger(__name__)


class ScalpingBudget:
    """
    Scalping 独立预算追踪 (线程安全单例)
    
    核心职责:
      1. 跟踪 Scalping 专属资金池
      2. 控制单笔最大仓位
      3. 跟踪日亏损并触发熔断
      4. 跟踪同时持仓数
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(ScalpingBudget, cls).__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(
        self,
        total_equity: float = None,
        scalping_budget_pct: float = 0.30,
        max_position_pct: float = 0.10,
        max_daily_loss_pct: float = 0.02,
        max_concurrent: int = 5,
    ):
        if self._initialized:
            return

        # SSOT: Capital Adaptivity - 优先从 unified_config.yaml 获取基准资金
        if total_equity is None:
            try:
                from app.core.config_container import get_config
                cfg = get_config()
                # 优先使用 capital.fixed_amount，其次 fallback 到 scalping 配置
                total_equity = float(cfg.get_dotted("capital.fixed_amount", 500_000))
            except Exception:
                total_equity = 500_000  # Fallback 值

        self._total_equity = total_equity
        self._scalping_budget_pct = scalping_budget_pct
        self._max_position_pct = max_position_pct
        self._max_daily_loss_pct = max_daily_loss_pct
        self._max_concurrent = max_concurrent

        # 内部状态
        self._budget = total_equity * scalping_budget_pct
        self._used = 0.0
        self._daily_loss = 0.0
        self._daily_loss_limit = total_equity * max_daily_loss_pct
        self._concurrent_positions = 0
        self._current_date = date.today()

        # 锁 — 使用 RLock 允许嵌套获取 (allocate → can_allocate)
        self._state_lock = threading.RLock()

        self._initialized = True

        # P1 修复 (2026-05-07): 持久化路径
        self._db_path = None
        try:
            from pathlib import Path
            self._db_path = str(Path(__file__).resolve().parent.parent.parent / "data" / "ftseek_us.db")
            self._init_persistence_db()
            self._load_persistent_state()
        except Exception as e:
            logger.warning(f"[ScalpingBudget] 持久化初始化失败 (降级到内存): {e}")

        logger.info(
            f"[ScalpingBudget] 初始化 | "
            f"总资金: ${total_equity:,.0f} | "
            f"Scalping 预算: ${self._budget:,.0f} ({scalping_budget_pct:.0%}) | "
            f"单笔最大: ${self._budget * max_position_pct:,.0f} | "
            f"日亏损上限: ${self._daily_loss_limit:,.0f}"
        )

    def _reset_daily_if_new_day(self):
        """如果是新的一天，重置日亏损"""
        today = date.today()
        if today != self._current_date:
            self._daily_loss = 0.0
            self._current_date = today
            logger.info("[ScalpingBudget] 新交易日，日亏损已重置")

    def get_available_budget(self) -> float:
        """获取可用预算"""
        with self._state_lock:
            self._reset_daily_if_new_day()
            return self._budget - self._used

    def get_total_budget(self) -> float:
        """获取总预算"""
        return self._budget

    def get_daily_loss(self) -> float:
        """获取日亏损"""
        with self._state_lock:
            return self._daily_loss

    def get_daily_loss_limit(self) -> float:
        """获取日亏损上限"""
        return self._daily_loss_limit

    def is_daily_loss_exceeded(self) -> bool:
        """检查是否触发日亏损熔断"""
        with self._state_lock:
            return self._daily_loss >= self._daily_loss_limit

    def update_total_equity(self, new_total_equity: float):
        """P0 修复 (2026-05-09): 线程安全地更新总资金

        当 Futu API 返回新的账户净值时调用，自动重新计算
        总预算和日亏损上限。不重置 _used（已用资金）。
        """
        with self._state_lock:
            old_budget = self._budget
            self._total_equity = new_total_equity
            self._budget = new_total_equity * self._scalping_budget_pct
            self._daily_loss_limit = new_total_equity * self._max_daily_loss_pct
            logger.debug(
                f"[ScalpingBudget] 资金更新: ${old_budget:,.0f} → ${self._budget:,.0f} "
                f"(净值=${new_total_equity:,.0f})"
            )

    def can_allocate(self, amount: float) -> bool:
        """
        检查是否可以分配资金
        
        条件:
          1. 可用预算足够
          2. 不超过单笔最大仓位
          3. 未触发日亏损熔断
        """
        with self._state_lock:
            self._reset_daily_if_new_day()

            if self._daily_loss >= self._daily_loss_limit:
                return False

            if (self._used + amount) > self._budget:
                return False

            max_position_size = self._budget * self._max_position_pct
            if amount > max_position_size:
                return False

            return True

    def allocate(self, amount: float) -> bool:
        """
        分配资金 (开仓时调用)
        
        Returns:
            True 如果分配成功
        """
        with self._state_lock:
            self._reset_daily_if_new_day()

            if not self.can_allocate(amount):
                logger.warning(
                    f"[ScalpingBudget] 分配失败: ${amount:,.0f} "
                    f"(可用: ${self._budget - self._used:,.0f}, "
                    f"最大单笔: ${self._budget * self._max_position_pct:,.0f})"
                )
                return False

            if self._concurrent_positions >= self._max_concurrent:
                logger.warning(
                    f"[ScalpingBudget] 分配失败: "
                    f"同时持仓已达上限 ({self._concurrent_positions}/{self._max_concurrent})"
                )
                return False

            self._used += amount
            self._concurrent_positions += 1
            self._save_persistent_state()  # P1 修复: 持久化

            logger.info(
                f"[ScalpingBudget] 分配 ${amount:,.0f} | "
                f"已用: ${self._used:,.0f}/{self._budget:,.0f} "
                f"({self._used / self._budget:.1%}) | "
                f"持仓: {self._concurrent_positions}/{self._max_concurrent}"
            )
            return True

    def release(self, amount: float):
        """释放资金 (平仓时调用)"""
        with self._state_lock:
            self._used = max(0, self._used - amount)
            self._concurrent_positions = max(0, self._concurrent_positions - 1)
            self._save_persistent_state()  # P1 修复: 持久化

            logger.debug(
                f"[ScalpingBudget] 释放 ${amount:,.0f} | "
                f"已用: ${self._used:,.0f}/{self._budget:,.0f} | "
                f"持仓: {self._concurrent_positions}"
            )

    def record_loss(self, loss_amount: float):
        """
        记录亏损 (平仓亏损时调用)
        
        Args:
            loss_amount: 亏损金额 (正数)
        """
        with self._state_lock:
            self._daily_loss += loss_amount
            self._save_persistent_state()  # P1 修复: 持久化

            if self._daily_loss >= self._daily_loss_limit:
                logger.critical(
                    f"[ScalpingBudget] 🚨 日亏损熔断触发! "
                    f"亏损: ${self._daily_loss:,.0f} / "
                    f"上限: ${self._daily_loss_limit:,.0f}"
                )

    def _init_persistence_db(self):
        """创建持久化表"""
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS scalping_budget_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                trade_date TEXT NOT NULL,
                daily_loss REAL DEFAULT 0,
                used_budget REAL DEFAULT 0,
                concurrent_positions INTEGER DEFAULT 0,
                last_update TEXT
            )
        """)
        cursor.execute("SELECT id FROM scalping_budget_state WHERE id = 1")
        if not cursor.fetchone():
            cursor.execute("""
                INSERT INTO scalping_budget_state (id, trade_date, last_update)
                VALUES (1, ?, ?)
            """, (date.today().isoformat(), datetime.now().isoformat()))
        conn.commit()
        conn.close()

    def _load_persistent_state(self):
        """从 DB 加载持久化状态 (日亏损、已用资金)"""
        import sqlite3
        try:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT trade_date, daily_loss, used_budget, concurrent_positions FROM scalping_budget_state WHERE id = 1"
            )
            row = cursor.fetchone()
            conn.close()
            if row:
                stored_date = row[0]
                today = date.today().isoformat()
                if stored_date == today:
                    self._daily_loss = float(row[1] or 0)
                    self._used = float(row[2] or 0)
                    self._concurrent_positions = int(row[3] or 0)
                    logger.info(
                        f"[ScalpingBudget] 持久化恢复 | 日亏损: ${self._daily_loss:,.0f} | "
                        f"已用: ${self._used:,.0f} | 持仓: {self._concurrent_positions}"
                    )
                else:
                    logger.info("[ScalpingBudget] 新交易日，持久化状态已过期，使用默认值")
        except Exception as e:
            logger.warning(f"[ScalpingBudget] 加载持久化状态失败: {e}")

    def _save_persistent_state(self):
        """保存当前状态到 DB"""
        if not self._db_path:
            return
        import sqlite3
        try:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE scalping_budget_state
                SET trade_date = ?, daily_loss = ?, used_budget = ?,
                    concurrent_positions = ?, last_update = ?
                WHERE id = 1
            """, (
                date.today().isoformat(),
                self._daily_loss,
                self._used,
                self._concurrent_positions,
                datetime.now().isoformat(),
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug(f"[ScalpingBudget] 保存持久化状态失败: {e}")

    def get_state(self) -> Dict:
        """获取当前状态 (用于日志和监控)"""
        with self._state_lock:
            self._reset_daily_if_new_day()
            return {
                "total_budget": self._budget,
                "used": self._used,
                "available": self._budget - self._used,
                "used_pct": self._used / self._budget if self._budget > 0 else 0,
                "daily_loss": self._daily_loss,
                "daily_loss_limit": self._daily_loss_limit,
                "daily_loss_pct": (
                    self._daily_loss / self._daily_loss_limit
                    if self._daily_loss_limit > 0
                    else 0
                ),
                "concurrent_positions": self._concurrent_positions,
                "max_concurrent": self._max_concurrent,
                "daily_loss_exceeded": self._daily_loss >= self._daily_loss_limit,
            }

    def update_config(self, config: Dict):
        """更新配置 (热重载)"""
        with self._state_lock:
            if "total_equity" in config:
                self._total_equity = config["total_equity"]
            if "scalping_budget_pct" in config:
                self._scalping_budget_pct = config["scalping_budget_pct"]
                self._budget = self._total_equity * self._scalping_budget_pct
            if "max_position_pct" in config:
                self._max_position_pct = config["max_position_pct"]
            if "max_daily_loss_pct" in config:
                self._max_daily_loss_pct = config["max_daily_loss_pct"]
                self._daily_loss_limit = self._total_equity * self._max_daily_loss_pct
            if "max_concurrent" in config:
                self._max_concurrent = config["max_concurrent"]

            logger.info("[ScalpingBudget] 配置已更新")


def get_scalping_budget(**kwargs) -> ScalpingBudget:
    """获取 ScalpingBudget 单例 (支持参数覆盖)"""
    instance = ScalpingBudget(**kwargs)
    return instance
