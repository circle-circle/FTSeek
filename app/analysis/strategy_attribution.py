"""
跨策略归因层 (Strategy Attribution Layer)

C5 实现 (2026-05-13): 追踪主线 + Scalping 双策略协同效果

核心指标:
  - cross_strategy_conflict_count: 同日内同标的反向信号次数
  - per_strategy_pnl: 各策略独立 PnL
  - interference_pnl: 因策略互斥损失的机会成本估算
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
      1. 记录信号冲突事件 (signal_disagreement)
      2. 累计各策略独立 PnL
      3. 计算交叉影响 (interference)
      4. 输出归因日报
    """

    def __init__(self, data_dir: str = HERMES_DIR):
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

        # 当日归因状态
        self._today = date.today().isoformat()
        self._reset_daily()

        # 文件路径
        self._disagreement_path = self._data_dir / "signal_disagreement.jsonl"
        self._attribution_path = self._data_dir / "attribution_daily.jsonl"

        logger.info(f"[归因层] 已初始化, 数据目录: {self._data_dir}")

    def _reset_daily(self):
        """重置当日统计"""
        self._today = date.today().isoformat()
        self._conflicts: List[Dict] = []           # 信号冲突事件
        self._main_trades: List[Dict] = []         # 主线交易记录
        self._scalping_trades: List[Dict] = []     # scalping 交易记录
        self._interference_events: List[Dict] = [] # 互斥/干扰事件

    def _check_day_rollover(self):
        """跨日重置"""
        today = date.today().isoformat()
        if today != self._today:
            # 写出昨日归因
            self._flush_daily_attribution()
            self._reset_daily()

    # ── 信号冲突记录 ──

    def record_signal_disagreement(
        self,
        symbol: str,
        main_direction: Optional[str],
        main_strength: Optional[float],
        scalping_direction: Optional[str],
        scalping_strength: Optional[float],
        context: Optional[Dict] = None,
    ):
        """
        记录跨策略信号冲突

        当主线想做 LONG、scalping 想做 SHORT (或反之) 时调用。
        不强制干预交易决策，仅用于盘后归因分析。
        """
        with self._lock:
            self._check_day_rollover()

            event = {
                "ts": datetime.now().isoformat(),
                "symbol": symbol,
                "main_direction": main_direction,
                "main_strength": main_strength,
                "scalping_direction": scalping_direction,
                "scalping_strength": scalping_strength,
                "context": context or {},
            }
            self._conflicts.append(event)

            # 持久化到 JSONL
            try:
                with open(self._disagreement_path, "a") as f:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.warning(f"[归因层] 信号冲突写入失败: {e}")

            logger.info(
                f"[归因层·冲突] {symbol} main={main_direction}({main_strength}) "
                f"vs scalping={scalping_direction}({scalping_strength})"
            )

    # ── 交易盈利记录 ──

    def record_trade_pnl(
        self,
        strategy: str,  # "main" or "scalping"
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

            if strategy == "main":
                self._main_trades.append(trade)
            elif strategy == "scalping":
                self._scalping_trades.append(trade)

    # ── 互斥/干扰记录 ──

    def record_interference(
        self,
        symbol: str,
        interference_type: str,  # "main_blocked_by_scalping" | "scalping_blocked_by_main" | "exit_conflict"
        blocked_strategy: str,
        blocked_direction: str,
        estimated_opportunity_pnl: float = 0.0,
        detail: Optional[Dict] = None,
    ):
        """
        记录策略间互斥/干扰事件

        interference_type:
          - main_blocked_by_scalping: 主线被 scalping 持仓阻止入场
          - scalping_blocked_by_main: scalping 被主线持仓阻止入场
          - exit_conflict: 退出引擎冲突
        """
        with self._lock:
            self._check_day_rollover()

            event = {
                "ts": datetime.now().isoformat(),
                "symbol": symbol,
                "type": interference_type,
                "blocked_strategy": blocked_strategy,
                "blocked_direction": blocked_direction,
                "estimated_opportunity_pnl": round(estimated_opportunity_pnl, 6),
                "detail": detail or {},
            }
            self._interference_events.append(event)

    # ── 盘后归因输出 ──

    def _flush_daily_attribution(self):
        """写出当日归因日报到 JSONL"""
        try:
            summary = self.get_attribution_summary()
            summary["date"] = self._today

            with open(self._attribution_path, "a") as f:
                f.write(json.dumps(summary, ensure_ascii=False) + "\n")

            logger.info(
                f"[归因层] 日报已写出: conflicts={summary['conflict_count']}, "
                f"main_pnl={summary['main_total_pnl_pct']:.4f}, "
                f"scalping_pnl={summary['scalping_total_pnl_pct']:.4f}"
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

            # Scalping 统计
            scalping_pnl = sum(t.get("pnl_pct", 0) for t in self._scalping_trades)
            scalping_wins = [t for t in self._scalping_trades if t.get("pnl_pct", 0) > 0]
            scalping_losses = [t for t in self._scalping_trades if t.get("pnl_pct", 0) <= 0]

            # 冲突统计
            conflict_symbols = set(c["symbol"] for c in self._conflicts)

            # 互斥统计
            main_blocked = [e for e in self._interference_events if e["type"] == "main_blocked_by_scalping"]
            scalping_blocked = [e for e in self._interference_events if e["type"] == "scalping_blocked_by_main"]

            # 估算干扰损失
            interference_pnl = sum(
                e.get("estimated_opportunity_pnl", 0) for e in self._interference_events
            )

            return {
                "date": self._today,
                # 主线
                "main_total_trades": len(self._main_trades),
                "main_wins": len(main_wins),
                "main_losses": len(main_losses),
                "main_win_rate": len(main_wins) / len(self._main_trades) if self._main_trades else 0,
                "main_total_pnl_pct": round(main_pnl, 6),
                # Scalping
                "scalping_total_trades": len(self._scalping_trades),
                "scalping_wins": len(scalping_wins),
                "scalping_losses": len(scalping_losses),
                "scalping_win_rate": len(scalping_wins) / len(self._scalping_trades) if self._scalping_trades else 0,
                "scalping_total_pnl_pct": round(scalping_pnl, 6),
                # 组合
                "combined_total_pnl_pct": round(main_pnl + scalping_pnl, 6),
                # 冲突
                "conflict_count": len(self._conflicts),
                "conflict_symbols": list(conflict_symbols),
                "conflict_unique_symbols": len(conflict_symbols),
                # 互斥
                "main_blocked_by_scalping_count": len(main_blocked),
                "scalping_blocked_by_main_count": len(scalping_blocked),
                "total_interference_events": len(self._interference_events),
                "estimated_interference_pnl": round(interference_pnl, 6),
                # PnL 相关性 (简化: 如果两策略都有交易)
                "cross_strategy_correlation": self._calc_correlation(),
            }

    def _calc_correlation(self) -> Optional[float]:
        """计算两策略 PnL 相关性 (简化版: 按交易顺序配对)"""
        main_pnls = [t.get("pnl_pct", 0) for t in self._main_trades]
        scalping_pnls = [t.get("pnl_pct", 0) for t in self._scalping_trades]

        n = min(len(main_pnls), len(scalping_pnls))
        if n < 3:
            return None

        # 截取前 n 笔
        main_pnls = main_pnls[:n]
        scalping_pnls = scalping_pnls[:n]

        mean_m = sum(main_pnls) / n
        mean_s = sum(scalping_pnls) / n

        cov = sum((m - mean_m) * (s - mean_s) for m, s in zip(main_pnls, scalping_pnls)) / n
        std_m = (sum((m - mean_m) ** 2 for m in main_pnls) / n) ** 0.5
        std_s = (sum((s - mean_s) ** 2 for s in scalping_pnls) / n) ** 0.5

        if std_m == 0 or std_s == 0:
            return None

        return round(cov / (std_m * std_s), 4)

    def get_formatted_report(self) -> str:
        """生成可读的归因报告文本"""
        s = self.get_attribution_summary()

        lines = [
            "── 策略归因 ──",
            f"主线: {s['main_total_trades']}笔 | "
            f"胜率 {s['main_win_rate']:.1%} | "
            f"PnL {s['main_total_pnl_pct']:+.4f}",
            f"Scalping: {s['scalping_total_trades']}笔 | "
            f"胜率 {s['scalping_win_rate']:.1%} | "
            f"PnL {s['scalping_total_pnl_pct']:+.4f}",
            f"组合 PnL: {s['combined_total_pnl_pct']:+.4f}",
            "",
            f"信号冲突: {s['conflict_count']}次 ({s['conflict_unique_symbols']}只标的)",
            f"主线被 Scalping 屏蔽: {s['main_blocked_by_scalping_count']}次",
            f"Scalping 被主线屏蔽: {s['scalping_blocked_by_main_count']}次",
            f"估算干扰损失: {s['estimated_interference_pnl']:+.4f}",
        ]

        corr = s.get("cross_strategy_correlation")
        if corr is not None:
            corr_status = "✅ 互补" if corr < 0.3 else ("⚠️ 弱相关" if corr < 0.7 else "🚨 高度同步")
            lines.append(f"PnL 相关性: {corr:.3f} ({corr_status})")
        else:
            lines.append("PnL 相关性: 数据不足 (需各≥3笔)")

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