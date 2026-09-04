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
    # DEB-20260904 #204: None = 不可度量 (未做参数绑定); 不再硬编码假读数 1.0
    param_stability: Optional[float] = None  # 参数跨窗口一致性 [0, 1]
    combined_pnl_curve: List[float] = field(default_factory=list)
    window_results: List[WindowResult] = field(default_factory=list)
    passed_gate: bool = False           # 是否通过统计门禁

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n_windows": self.n_windows,
            "in_sample_stats": self.in_sample_stats,
            "out_sample_stats": self.out_sample_stats,
            "degradation_ratio": round(self.degradation_ratio, 4),
            "param_stability": round(self.param_stability, 4) if self.param_stability is not None else None,
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


def _compute_param_stability(all_params: List[Dict[str, Any]]) -> float:
    """DEB-20260904 #204: 跨窗口参数稳定性 [0,1] — 数值参数按 1-变异系数,
    非数值参数按跨窗口一致性; 全窗口全同 = 1.0"""
    keys = set()
    for p in all_params:
        keys.update(p.keys())
    if not keys:
        return 1.0
    scores = []
    for k in keys:
        vals = [p.get(k) for p in all_params]
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
            mean = sum(vals) / len(vals)
            if mean == 0:
                scores.append(1.0 if all(v == 0 for v in vals) else 0.0)
            else:
                var = sum((v - mean) ** 2 for v in vals) / len(vals)
                cv = (var ** 0.5) / abs(mean)
                scores.append(max(0.0, 1.0 - cv))
        else:
            scores.append(1.0 if all(v == vals[0] for v in vals) else 0.0)
    return round(sum(scores) / len(scores), 4)


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

    DEB-20260904 #204 参数绑定契约:
        - 传 param_fitter 时: 每窗口先 fitter(train_bars)->params, 训练/测试均用该
          params 跑 (signal_generator 须接受 (bars, params) 双参), OOS 才是真样本外,
          param_stability 按跨窗口参数真实计算。
        - 不传 param_fitter 时: 无参数可绑定, train/test 跑同一 signal_generator —
          若生成器内部按传入 bars 自适应调参, OOS 结果实为样本内(前视偏差);
          此时 param_stability=None 表示不可度量, 不再返回假读数 1.0。
    """

    def run(
        self,
        signal_generator,
        bars: List[Dict[str, Any]],
        train_pct: float = 0.7,
        n_windows: int = 3,
        param_fitter=None,
    ) -> WalkForwardResult:
        """
        执行 walk-forward 回测

        Args:
            signal_generator: callable(bars_slice) -> List[float] pnl_list
                接收 K 线切片，返回该窗口的逐笔 PnL;
                配合 param_fitter 时为 callable(bars_slice, params) -> pnl_list
            bars: 完整历史 K 线 (按时间排序)
            train_pct: 训练集占比 (默认 0.7)
            n_windows: 窗口数 (默认 3)
            param_fitter: 可选 callable(train_bars) -> Dict params,
                仅在训练窗拟合参数, 测试窗强制复用 (DEB-20260904 #204)

        Returns:
            WalkForwardResult
        """
        # DEB-20260904 #204: 内省 signal_generator 是否接受 params 双参, 决定能否透传
        import inspect
        try:
            _accepts_params = len(inspect.signature(signal_generator).parameters) >= 2
        except (TypeError, ValueError) as _e:
            logger.debug("signal_generator 签名内省失败, 按单参调用: %s", _e)
            _accepts_params = False
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

            # DEB-20260904 #204: 参数仅在训练窗拟合, 测试窗强制复用 (消除 OOS=IS 前视);
            # signal_generator 接受 (bars, params) 双参时透传, 否则维持单参 (参数不可绑定)
            params = param_fitter(train_bars) if param_fitter is not None else None
            if params is not None and _accepts_params:
                is_pnl = signal_generator(train_bars, params)
                # 测试集: 用训练集参数跑
                oos_pnl = signal_generator(test_bars, params)
            else:
                is_pnl = signal_generator(train_bars)
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
                optimal_params=params or {},
            )
            window_results.append(wr)
            all_is_pnl.extend(is_pnl)
            all_oos_pnl.extend(oos_pnl)
            if params is not None:
                all_params.append(params)

        # 汇总统计
        is_agg = _compute_stats(all_is_pnl)
        oos_agg = _compute_stats(all_oos_pnl)

        # 退化比率: OOS Sharpe / IS Sharpe
        is_sharpe = is_agg.get("sharpe_per_trade", 0)
        oos_sharpe = oos_agg.get("sharpe_per_trade", 0)
        degradation = oos_sharpe / is_sharpe if is_sharpe != 0 else 0.0

        # DEB-20260904 #204: 参数稳定性真实计算; 无参数绑定时 None = 不可度量 (原硬编码 1.0 为假读数)
        if all_params and all(isinstance(p, dict) for p in all_params):
            param_stability = _compute_param_stability(all_params)
        else:
            param_stability = None

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
