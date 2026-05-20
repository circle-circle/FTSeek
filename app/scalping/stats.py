"""
交易统计追踪 — Scalping 可观测性

记录每笔交易的:
  - 标的、方向、入场价、TP/SL
  - 出场原因 (止盈/止损/时间止损)
  - 盈亏、持仓时间
  - 信号来源

输出:
  - CSV 文件: ~/.hermes/ftseek/data/scalping_trades.csv
  - 实时统计: 胜率、期望值、最大回撤
"""

import csv
import os
import threading
import logging
from datetime import datetime
from typing import Dict, List, Optional
from pathlib import Path

logger = logging.getLogger(__name__)

# 输出目录
HERMES_DATA_DIR = os.path.expanduser("~/.hermes/ftseek/data")
TRADES_CSV = os.path.join(HERMES_DATA_DIR, "scalping_trades.csv")


class TradeRecord:
    """单笔交易记录"""

    def __init__(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        take_profit: float,
        stop_loss: float,
        signals: str,
        phase: str,
        quantity: int = 0,
    ):
        self.symbol = symbol
        self.direction = direction
        self.entry_price = entry_price
        self.take_profit = take_profit
        self.stop_loss = stop_loss
        self.signals = signals
        self.phase = phase
        self.quantity = quantity

        self.exit_price: Optional[float] = None
        self.exit_reason: Optional[str] = None
        self.pnl_pct: Optional[float] = None
        self.pnl_amount: Optional[float] = None
        self.entry_time = datetime.now()
        self.exit_time: Optional[datetime] = None
        self.hold_minutes: Optional[float] = None

    def close(
        self,
        exit_price: float,
        exit_reason: str,
    ):
        """平仓"""
        self.exit_price = exit_price
        self.exit_reason = exit_reason
        self.exit_time = datetime.now()
        self.hold_minutes = (
            (self.exit_time - self.entry_time).total_seconds() / 60
        )

        if self.direction == "LONG":
            self.pnl_pct = (exit_price - self.entry_price) / self.entry_price
        else:  # SHORT
            self.pnl_pct = (self.entry_price - exit_price) / self.entry_price

        self.pnl_amount = self.pnl_pct * self.entry_price * self.quantity

    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "take_profit": self.take_profit,
            "stop_loss": self.stop_loss,
            "exit_price": self.exit_price or "",
            "exit_reason": self.exit_reason or "",
            "pnl_pct": f"{self.pnl_pct:.4f}" if self.pnl_pct is not None else "",
            "pnl_amount": f"{self.pnl_amount:.2f}" if self.pnl_amount is not None else "",
            "signals": self.signals,
            "phase": self.phase,
            "quantity": self.quantity,
            "entry_time": self.entry_time.isoformat(),
            "exit_time": self.exit_time.isoformat() if self.exit_time else "",
            "hold_minutes": f"{self.hold_minutes:.1f}" if self.hold_minutes is not None else "",
        }


class ScalpingStatsTracker:
    """
    Scalping 统计追踪器
    
    职责:
      1. 记录每笔交易
      2. 计算实时统计 (胜率、EV、最大回撤)
      3. 持久化到 CSV
    """

    def __init__(self, csv_path: str = TRADES_CSV):
        self._csv_path = csv_path
        self._trades: List[TradeRecord] = []
        self._lock = threading.Lock()

        # 确保输出目录存在
        os.makedirs(os.path.dirname(self._csv_path), exist_ok=True)

        # 创建 CSV header (如果不存在)
        if not os.path.exists(self._csv_path):
            self._write_header()
            logger.info(f"[StatsTracker] CSV 文件已创建: {self._csv_path}")

        logger.info(f"[StatsTracker] 已初始化 (CSV: {self._csv_path})")

    def _write_header(self):
        """写入 CSV header"""
        headers = [
            "symbol", "direction", "entry_price", "take_profit", "stop_loss",
            "exit_price", "exit_reason", "pnl_pct", "pnl_amount",
            "signals", "phase", "quantity",
            "entry_time", "exit_time", "hold_minutes",
        ]
        with open(self._csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()

    def record_entry(self, trade: TradeRecord):
        """记录开仓"""
        with self._lock:
            self._trades.append(trade)
            logger.info(
                f"[StatsTracker] 📊 开仓记录 | "
                f"{trade.symbol} {trade.direction} @ ${trade.entry_price:.2f} | "
                f"TP: ${trade.take_profit:.2f} | SL: ${trade.stop_loss:.2f}"
            )

    def record_exit(self, symbol: str, exit_price: float, exit_reason: str):
        """
        记录平仓
        
        Args:
            symbol: 标的代码
            exit_price: 出场价
            exit_reason: 出场原因
        """
        with self._lock:
            # 找到最近未平仓的记录
            for trade in reversed(self._trades):
                if trade.symbol == symbol and trade.exit_price is None:
                    trade.close(exit_price, exit_reason)
                    self._write_trade_to_csv(trade)

                    pnl_str = f"+{trade.pnl_pct:.2%}" if trade.pnl_pct > 0 else f"{trade.pnl_pct:.2%}"
                    logger.info(
                        f"[StatsTracker] 📊 平仓记录 | "
                        f"{symbol} {trade.direction} | "
                        f"盈亏: {pnl_str} | "
                        f"持仓: {trade.hold_minutes:.0f}min | "
                        f"原因: {exit_reason}"
                    )
                    return

            logger.warning(
                f"[StatsTracker] 未找到 {symbol} 的开仓记录"
            )

    def _write_trade_to_csv(self, trade: TradeRecord):
        """将交易写入 CSV"""
        try:
            with open(self._csv_path, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=[
                    "symbol", "direction", "entry_price", "take_profit", "stop_loss",
                    "exit_price", "exit_reason", "pnl_pct", "pnl_amount",
                    "signals", "phase", "quantity",
                    "entry_time", "exit_time", "hold_minutes",
                ])
                writer.writerow(trade.to_dict())
        except Exception as e:
            logger.error(f"[StatsTracker] CSV 写入失败: {e}")

    def get_stats(self) -> Dict:
        """
        获取实时统计
        
        Returns:
            {
                "total_trades": int,
                "wins": int,
                "losses": int,
                "win_rate": float,
                "avg_win_pct": float,
                "avg_loss_pct": float,
                "expectancy": float,       # EV
                "max_drawdown_pct": float,
                "total_pnl_pct": float,
            }
        """
        with self._lock:
            completed = [t for t in self._trades if t.pnl_pct is not None]

            if not completed:
                return {
                    "total_trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "win_rate": 0.0,
                    "avg_win_pct": 0.0,
                    "avg_loss_pct": 0.0,
                    "expectancy": 0.0,
                    "max_drawdown_pct": 0.0,
                    "total_pnl_pct": 0.0,
                }

            wins = [t for t in completed if t.pnl_pct > 0]
            losses = [t for t in completed if t.pnl_pct <= 0]

            win_rate = len(wins) / len(completed) if completed else 0
            avg_win = (
                sum(t.pnl_pct for t in wins) / len(wins) if wins else 0
            )
            avg_loss = (
                sum(t.pnl_pct for t in losses) / len(losses) if losses else 0
            )

            # 期望值: EV = win_rate * avg_win - loss_rate * |avg_loss|
            expectancy = (
                win_rate * avg_win - (1 - win_rate) * abs(avg_loss)
            )

            # 最大回撤
            cumulative = 0
            peak = 0
            max_dd = 0
            for t in completed:
                cumulative += t.pnl_pct
                peak = max(peak, cumulative)
                max_dd = max(max_dd, peak - cumulative)

            total_pnl = sum(t.pnl_pct for t in completed)

            return {
                "total_trades": len(completed),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": win_rate,
                "avg_win_pct": avg_win,
                "avg_loss_pct": avg_loss,
                "expectancy": expectancy,
                "max_drawdown_pct": max_dd,
                "total_pnl_pct": total_pnl,
            }

    def get_today_stats(self) -> Dict:
        """获取今日统计"""
        with self._lock:
            today = datetime.now().date()
            completed = [
                t for t in self._trades
                if t.pnl_pct is not None
                and t.exit_time is not None
                and t.exit_time.date() == today
            ]

            wins = [t for t in completed if t.pnl_pct > 0]
            losses = [t for t in completed if t.pnl_pct <= 0]

            win_rate = len(wins) / len(completed) if completed else 0
            total_pnl = sum(t.pnl_pct for t in completed)

            return {
                "total_trades": len(completed),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": win_rate,
                "total_pnl_pct": total_pnl,
            }

    def get_active_trades(self) -> List[TradeRecord]:
        """获取当前活跃交易 (未平仓)"""
        with self._lock:
            return [t for t in self._trades if t.exit_price is None]


def get_stats_tracker(**kwargs) -> ScalpingStatsTracker:
    """获取统计追踪器实例"""
    return ScalpingStatsTracker(**kwargs)
