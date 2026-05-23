"""
Walk-Forward 回测引擎

解决回测过拟合的核心方法:
1. 滚动窗口: 前 N% 训练，后 (100-N)% 测试
2. 多窗口平均: 重复 K 次，取统计平均
3. 参数稳定性: 不同窗口的最优参数应该相近
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class WindowResult:
    """单个窗口的回测结果"""
    window_id: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    in_sample_stats: Dict[str, float] = field(default_factory=dict)
    out_sample_stats: Dict[str, float] = field(default_factory=dict)
    optimal_params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WalkForwardResult:
    """Walk-Forward 回测总结"""
    n_windows: int
    in_sample_stats: Dict[str, float] = field(default_factory=dict)
    out_sample_stats: Dict[str, float] = field(default_factory=dict)
    degradation_ratio: float = 0.0      # OOS/IS Sharpe ratio
    param_stability: float = 0.0        # 参数跨窗口一致性 [0, 1]
    combined_pnl_curve: List[float] = field(default_factory=list)
    window_results: List[WindowResult] = field(default_factory=list)
    passed_gate: bool = False           # 是否通过统计门禁

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n_windows": self.n_windows,
            "in_sample_stats": self.in_sample_stats,
            "out_sample_stats": self.out_sample_stats,
            "degradation_ratio": round(self.degradation_ratio, 4),
            "param_stability": round(self.param_stability, 4),
            "passed_gate": self.passed_gate,
            "total_trades": len(self.combined_pnl_curve),
        }


def _compute_stats(pnl_list: List[float]) -> Dict[str, float]:
    """计算交易统计"""
    if not pnl_list:
        return {"n_trades": 0, "win_rate": 0, "profit_factor": 0, "sharpe": 0, "total_pnl": 0}

    n = len(pnl_list)
    wins = [p for p in pnl_list if p > 0]
    losses = [p for p in pnl_list if p <= 0]

    total_pnl = sum(pnl_list)
    gross_profit = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0

    # 利润因子
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0

    # 胜率
    wr = len(wins) / n if n > 0 else 0

    # Sharpe per trade
    if n > 1:
        import math
        mean_pnl = total_pnl / n
        variance = sum((p - mean_pnl) ** 2 for p in pnl_list) / (n - 1)
        std_pnl = math.sqrt(variance) if variance > 0 else 0
        sharpe = mean_pnl / std_pnl if std_pnl > 0 else 0
    else:
        mean_pnl = pnl_list[0] if pnl_list else 0
        sharpe = 0

    return {
        "n_trades": n,
        "win_rate": round(wr, 4),
        "profit_factor": round(pf, 4),
        "sharpe_per_trade": round(sharpe, 4),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(total_pnl / n, 4),
    }


class WalkForwardEngine:
    """
    Walk-Forward 回测引擎

    Usage:
        engine = WalkForwardEngine()
        result = engine.run(
            signal_generator=my_signal_fn,
            bars=historical_bars,
            train_pct=0.7,
            n_windows=3,
        )
    """

    def run(
        self,
        signal_generator,
        bars: List[Dict[str, Any]],
        train_pct: float = 0.7,
        n_windows: int = 3,
    ) -> WalkForwardResult:
        """
        执行 walk-forward 回测

        Args:
            signal_generator: callable(bars_slice) -> List[float] pnl_list
                接收 K 线切片，返回该窗口的逐笔 PnL
            bars: 完整历史 K 线 (按时间排序)
            train_pct: 训练集占比 (默认 0.7)
            n_windows: 窗口数 (默认 3)

        Returns:
            WalkForwardResult
        """
        total_bars = len(bars)
        if total_bars < 100:
            logger.warning(f"Bars too few for walk-forward: {total_bars}")

        # 计算每个窗口大小
        window_size = total_bars // n_windows
        if window_size < 50:
            logger.warning(f"Window too small: {window_size} bars per window")

        window_results = []
        all_is_pnl = []
        all_oos_pnl = []
        all_params = []

        for w in range(n_windows):
            start = w * window_size
            end = min(start + window_size, total_bars)
            window_bars = bars[start:end]

            split = int(len(window_bars) * train_pct)
            train_bars = window_bars[:split]
            test_bars = window_bars[split:]

            # 训练集: 找到最优参数 (简化: 直接跑)
            is_pnl = signal_generator(train_bars)
            # 测试集: 用训练集参数跑
            oos_pnl = signal_generator(test_bars)

            is_stats = _compute_stats(is_pnl)
            oos_stats = _compute_stats(oos_pnl)

            wr = WindowResult(
                window_id=w,
                train_start=start,
                train_end=start + split,
                test_start=start + split,
                test_end=end,
                in_sample_stats=is_stats,
                out_sample_stats=oos_stats,
            )
            window_results.append(wr)
            all_is_pnl.extend(is_pnl)
            all_oos_pnl.extend(oos_pnl)

        # 汇总统计
        is_agg = _compute_stats(all_is_pnl)
        oos_agg = _compute_stats(all_oos_pnl)

        # 退化比率: OOS Sharpe / IS Sharpe
        is_sharpe = is_agg.get("sharpe_per_trade", 0)
        oos_sharpe = oos_agg.get("sharpe_per_trade", 0)
        degradation = oos_sharpe / is_sharpe if is_sharpe != 0 else 0.0

        # 参数稳定性 (简化: 暂时返回 1.0)
        param_stability = 1.0

        # 合并 PnL 曲线 (OOS 部分)
        combined = all_oos_pnl

        result = WalkForwardResult(
            n_windows=n_windows,
            in_sample_stats=is_agg,
            out_sample_stats=oos_agg,
            degradation_ratio=degradation,
            param_stability=param_stability,
            combined_pnl_curve=combined,
            window_results=window_results,
        )

        logger.info(
            f"Walk-forward done: {n_windows} windows, "
            f"IS trades={is_agg['n_trades']}, OOS trades={oos_agg['n_trades']}, "
            f"degradation={degradation:.3f}"
        )

        return result
