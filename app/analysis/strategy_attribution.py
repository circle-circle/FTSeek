"""
跨策略归因层 (Strategy Attribution Layer)

C5 实现 (2026-05-13): 追踪主线策略协同效果

核心指标:
  - per_strategy_pnl: 各策略独立 PnL
  - signal_disagreement 日志: 盘后归因用

输出:
  - ~/.hermes/ftseek/data/attribution/attribution_daily.jsonl  日报
  - ~/.hermes/ftseek/data/attribution/signal_disagreement.jsonl 信号冲突日志
"""

import json
import os
import threading
import logging
from datetime import datetime, date
from typing import Dict, List, Optional, Any
from pathlib import Path

logger = logging.getLogger(__name__)

HERMES_DIR = os.path.expanduser("~/.hermes/ftseek/data/attribution")


class StrategyAttribution:
    """
    跨策略归因追踪器

    职责:
      1. 累计各策略独立 PnL
      2. 输出归因日报
    """

    def __init__(self, data_dir: str = HERMES_DIR):
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

        # 当日归因状态
        # P1 修复 (2026-05-31): 使用 ET 日期, 避免 CST 跨日重置
        from app.utils.timezone import get_trade_date
        self._today = get_trade_date()
        self._reset_daily()

        # 文件路径
        self._disagreement_path = self._data_dir / "signal_disagreement.jsonl"
        self._attribution_path = self._data_dir / "attribution_daily.jsonl"

        logger.info(f"[归因层] 已初始化, 数据目录: {self._data_dir}")

    def _reset_daily(self):
        """重置当日统计"""
        # P1 修复 (2026-05-31): 使用 ET 日期
        from app.utils.timezone import get_trade_date
        self._today = get_trade_date()
        self._main_trades: List[Dict] = []         # 主线交易记录

    def _check_day_rollover(self):
        """跨日重置"""
        # P1 修复 (2026-05-31): 使用 ET 日期
        from app.utils.timezone import get_trade_date
        today = get_trade_date()
        if today != self._today:
            # 写出昨日归因
            self._flush_daily_attribution()
            self._reset_daily()

    # ── 交易盈利记录 ──

    def record_trade_pnl(
        self,
        strategy: str,
        symbol: str,
        direction: str,
        pnl_pct: float,
        pnl_amount: float = 0.0,
        exit_reason: str = "",
        entry_time: Optional[datetime] = None,
        exit_time: Optional[datetime] = None,
    ):
        """记录各策略交易盈亏"""
        with self._lock:
            self._check_day_rollover()

            trade = {
                "strategy": strategy,
                "symbol": symbol,
                "direction": direction,
                "pnl_pct": round(pnl_pct, 6),
                "pnl_amount": round(pnl_amount, 4),
                "exit_reason": exit_reason,
                "entry_time": entry_time.isoformat() if entry_time else "",
                "exit_time": exit_time.isoformat() if exit_time else "",
            }

            self._main_trades.append(trade)

    # ── 盘后归因输出 ──

    def _flush_daily_attribution(self):
        """写出当日归因日报到 JSONL"""
        try:
            summary = self.get_attribution_summary()
            summary["date"] = self._today

            with open(self._attribution_path, "a") as f:
                f.write(json.dumps(summary, ensure_ascii=False) + "\n")

            logger.info(
                f"[归因层] 日报已写出: trades={summary['main_total_trades']}, "
                f"main_pnl={summary['main_total_pnl_pct']:.4f}"
            )
        except Exception as e:
            logger.error(f"[归因层] 日报写出失败: {e}")

    def get_attribution_summary(self) -> Dict:
        """获取当日归因摘要"""
        with self._lock:
            # 主线统计
            main_pnl = sum(t.get("pnl_pct", 0) for t in self._main_trades)
            main_wins = [t for t in self._main_trades if t.get("pnl_pct", 0) > 0]
            main_losses = [t for t in self._main_trades if t.get("pnl_pct", 0) <= 0]
            main_wins_amount = sum(t.get("pnl_amount", 0) for t in main_wins)
            main_losses_amount = sum(t.get("pnl_amount", 0) for t in main_losses)

            return {
                "date": self._today,
                "main_total_trades": len(self._main_trades),
                "main_wins": len(main_wins),
                "main_losses": len(main_losses),
                "main_win_rate": len(main_wins) / len(self._main_trades) if self._main_trades else 0,
                "main_total_pnl_pct": round(main_pnl, 6),
                "main_total_pnl_amount": round(main_wins_amount + main_losses_amount, 4),
            }

    def get_formatted_report(self) -> str:
        """生成可读的归因报告文本"""
        s = self.get_attribution_summary()

        lines = [
            "── 策略归因 ──",
            f"主线: {s['main_total_trades']}笔 | "
            f"胜率 {s['main_win_rate']:.1%} | "
            f"PnL {s['main_total_pnl_pct']:+.4f}",
        ]

        return "\n".join(lines)


# 全局单例
_attribution_instance: Optional[StrategyAttribution] = None
_attribution_lock = threading.Lock()


def get_attribution() -> StrategyAttribution:
    """获取归因层全局单例"""
    global _attribution_instance
    if _attribution_instance is None:
        with _attribution_lock:
            if _attribution_instance is None:
                _attribution_instance = StrategyAttribution()
    return _attribution_instance