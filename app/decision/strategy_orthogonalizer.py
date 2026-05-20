"""
策略正交化模块 (Strategy Orthogonalizer)

解决问题
--------
原共识引擎 7 策略权重线性相加, 但 ma_cross / macd / supertrend / price_break
在趋势市高度相关 (ρ 常 > 0.8), 等于"同一个 β 被下了多倍注"。本模块提供两级去重:

1. 分组去重 (Group-level)
   策略分组 (trend/reversion/sentiment), 同组权重合计硬上限 group_weight_cap.

2. 相关性去重 (Correlation-level)
   基于历史信号方向流动态统计两两 ρ, 用 Effective Number of Bets:
       effective_n = n / (1 + ρ̄ × (n - 1))
   把每条信号权重按 effective_n / n 压缩.

对外接口
--------
OrthogonalizerConfig
StrategyOrthogonalizer:
    compute_effective_weights(signals, base_weights) -> Dict[sid, eff_w]
    update_from_signal_batch(signals, outcome=None)  # 在线学习
    pairwise_correlation(a, b) -> float
    effective_n(strategy_ids) -> float
    debug_snapshot() -> dict

作者: FTSeek Quant Team  创建: 2026-04-28
"""
from __future__ import annotations

import json
import logging
import math
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional

from app.core.models import Direction, SignalScore

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 默认策略分组
# --------------------------------------------------------------------------- #
DEFAULT_STRATEGY_GROUPS: Dict[str, str] = {
    "price_break":  "trend",
    "ma_cross":     "trend",
    "macd":         "trend",
    "supertrend":   "trend",
    "rsi_rebound":  "reversion",
    "rsi":          "reversion",
    "bollinger":    "reversion",
    "news":         "sentiment",
    "llm":          "sentiment",
    "sentiment":    "sentiment",
}

GROUP_PRIOR_CORR: Dict[str, float] = {
    # P0 优化 (2026-05-12): 降低相关性假设，增加交易机会
    # 原值: trend=0.75, reversion=0.60 → 新值: 降低 0.15
    "trend":     0.60,   # P0 优化: 0.75 → 0.60
    "reversion": 0.45,   # P0 优化: 0.60 → 0.45
    "sentiment": 0.50,
    "other":     0.30,
}


@dataclass
class OrthogonalizerConfig:
    strategy_groups: Dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_STRATEGY_GROUPS)
    )
    group_prior_corr: Dict[str, float] = field(
        default_factory=lambda: dict(GROUP_PRIOR_CORR)
    )
    default_group: str = "other"
    min_samples: int = 30
    window_size: int = 500
    group_weight_cap: float = 0.45
    min_effective_n: float = 1.0
    persist_path: Optional[str] = None


# --------------------------------------------------------------------------- #
# 相关性滚动估计器
# --------------------------------------------------------------------------- #
class _PairwiseCorrelation:
    """两两相关性滚动估计, 纯 python 实现 Pearson ρ."""

    def __init__(self, window: int = 500):
        self.window = window
        self._series: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=window)
        )
        self._lock = threading.RLock()

    def push(self, strategy_id: str, value: float, align_len: int) -> None:
        with self._lock:
            series = self._series[strategy_id]
            pad = align_len - len(series)
            if pad > 0:
                for _ in range(pad):
                    series.append(0.0)
            series.append(value)

    def sample_count(self, a: str, b: str) -> int:
        with self._lock:
            if a not in self._series or b not in self._series:
                return 0
            return min(len(self._series[a]), len(self._series[b]))

    def pearson(self, a: str, b: str) -> Optional[float]:
        with self._lock:
            if a == b:
                return 1.0
            if a not in self._series or b not in self._series:
                return None
            xs = list(self._series[a])
            ys = list(self._series[b])
            n = min(len(xs), len(ys))
            if n < 5:
                return None
            xs, ys = xs[-n:], ys[-n:]
            mx = sum(xs) / n
            my = sum(ys) / n
            num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
            dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
            dy = math.sqrt(sum((y - my) ** 2 for y in ys))
            if dx < 1e-9 or dy < 1e-9:
                return None
            return max(-1.0, min(1.0, num / (dx * dy)))

    def snapshot(self) -> Dict[str, List[float]]:
        with self._lock:
            return {k: list(v) for k, v in self._series.items()}

    def load(self, data: Dict[str, List[float]]) -> None:
        with self._lock:
            self._series.clear()
            for k, v in data.items():
                dq: Deque[float] = deque(v[-self.window:], maxlen=self.window)
                self._series[k] = dq


# --------------------------------------------------------------------------- #
# 主类
# --------------------------------------------------------------------------- #
class StrategyOrthogonalizer:
    """策略正交化器."""

    def __init__(self, cfg: Optional[OrthogonalizerConfig] = None):
        self.cfg = cfg or OrthogonalizerConfig()
        self._corr = _PairwiseCorrelation(window=self.cfg.window_size)
        self._tick = 0
        if self.cfg.persist_path:
            self._try_load()

    # ----- 工具 ----- #
    def group_of(self, strategy_id: str) -> str:
        return self.cfg.strategy_groups.get(strategy_id, self.cfg.default_group)

    def pairwise_correlation(self, a: str, b: str) -> float:
        rho = self._corr.pearson(a, b)
        if rho is None or self._corr.sample_count(a, b) < self.cfg.min_samples:
            ga, gb = self.group_of(a), self.group_of(b)
            if ga == gb:
                return self.cfg.group_prior_corr.get(ga, 0.3)
            return 0.0
        return rho
    def effective_n(self, strategy_ids: Iterable[str]) -> float:
        """有效独立信号数（考虑相关性）"""
        unique_ids = list(dict.fromkeys(strategy_ids))
        n = len(unique_ids)
        if n <= 1:
            return float(n)

        # 冷启动保护：样本不足时使用先验 ρ 保守估计
        if self._tick < self.cfg.min_samples:
            groups = set(self.group_of(sid) for sid in unique_ids)
            prior_rho = max(
                self.cfg.group_prior_corr.get(g, 0.3) for g in groups
            )
            enb = n / (1.0 + prior_rho * (n - 1))
            return max(self.cfg.min_effective_n, enb)

        rho_sum = 0.0
        pair_count = 0
        for i in range(n):
            for j in range(i + 1, n):
                rho_sum += self.pairwise_correlation(unique_ids[i], unique_ids[j])
                pair_count += 1
        mean_rho = rho_sum / pair_count if pair_count else 0.0
        mean_rho = max(0.0, min(0.99, mean_rho))
        enb = n / (1.0 + mean_rho * (n - 1))
        return max(self.cfg.min_effective_n, enb)

    # ----- 关键: 计算有效权重 ----- #
    def compute_effective_weights(
        self,
        signals: List[SignalScore],
        base_weights: Dict[str, float],
    ) -> Dict[str, float]:
        if not signals:
            return {}

        sids = [s.strategy_id for s in signals]

        # 分组
        group_to_sids: Dict[str, List[str]] = defaultdict(list)
        for sid in sids:
            group_to_sids[self.group_of(sid)].append(sid)

        per_strategy_weight: Dict[str, float] = {}
        for group, group_sids in group_to_sids.items():
            unique = list(dict.fromkeys(group_sids))
            group_enb = self.effective_n(unique)
            shrink = group_enb / len(unique) if unique else 1.0
            for sid in unique:
                per_strategy_weight[sid] = base_weights.get(sid, 0.1) * shrink
            # 组上限
            group_total = sum(per_strategy_weight[s] for s in unique)
            if group_total > self.cfg.group_weight_cap and group_total > 0:
                scale = self.cfg.group_weight_cap / group_total
                for sid in unique:
                    per_strategy_weight[sid] *= scale
                logger.debug(
                    "[ortho] group %s capped: %.3f -> %.3f",
                    group, group_total, self.cfg.group_weight_cap,
                )

        # 全局二次压缩
        global_enb = self.effective_n(sids)
        global_shrink = global_enb / len(set(sids)) if sids else 1.0
        for sid in per_strategy_weight:
            per_strategy_weight[sid] *= global_shrink

        logger.debug(
            "[ortho] eff_w=%s global_enb=%.2f/n=%d",
            {k: round(v, 4) for k, v in per_strategy_weight.items()},
            global_enb, len(set(sids)),
        )
        return per_strategy_weight

    # ----- 在线更新 ----- #
    def update_from_signal_batch(
        self,
        signals: List[SignalScore],
        outcome: Optional[Dict[str, float]] = None,
    ) -> None:
        if not signals:
            return
        per_sid: Dict[str, float] = {}
        for s in signals:
            sign = 1.0 if s.direction == Direction.LONG else -1.0
            val = sign * max(0.1, min(1.0, s.confidence or 0.5))
            if s.strategy_id in per_sid and abs(per_sid[s.strategy_id]) >= abs(val):
                continue
            per_sid[s.strategy_id] = val

        all_known = set(self._corr._series.keys()) | set(per_sid.keys())
        align_len = self._tick
        self._tick += 1
        for sid in all_known:
            self._corr.push(sid, per_sid.get(sid, 0.0), align_len)

        if outcome:
            pass  # 预留 PnL 加权扩展

        if self.cfg.persist_path and self._tick % 20 == 0:
            self._try_persist()

    # ----- 持久化 ----- #
    def _try_persist(self) -> None:
        try:
            p = Path(self.cfg.persist_path)  # type: ignore[arg-type]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({
                "tick": self._tick,
                "series": self._corr.snapshot(),
            }), encoding="utf-8")
        except Exception as e:
            logger.warning("[ortho] persist failed: %s", e)

    def _try_load(self) -> None:
        try:
            p = Path(self.cfg.persist_path)  # type: ignore[arg-type]
            if not p.exists():
                return
            blob = json.loads(p.read_text(encoding="utf-8"))
            self._tick = int(blob.get("tick", 0))
            self._corr.load(blob.get("series", {}))
            logger.info("[ortho] loaded %d series from %s",
                        len(blob.get("series", {})), p)
        except Exception as e:
            logger.warning("[ortho] load failed: %s", e)

    # ----- 可观测性 ----- #
    def debug_snapshot(self) -> Dict[str, Any]:
        known = list(self._corr.snapshot().keys())
        matrix: Dict[str, Dict[str, float]] = {}
        for a in known:
            matrix[a] = {}
            for b in known:
                matrix[a][b] = round(self.pairwise_correlation(a, b), 3)
        return {
            "tick": self._tick,
            "strategies": known,
            "pairwise_rho": matrix,
        }


# --------------------------------------------------------------------------- #
# Self test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import random
    logging.basicConfig(level=logging.INFO)
    from app.core.models import Direction as _D, SignalScore as _S, MarketRegime as _M

    ortho = StrategyOrthogonalizer()
    # 喂 50 个 tick: 趋势族高度同步, 反转族独立
    for _ in range(50):
        trend_dir = _D.LONG if random.random() > 0.3 else _D.SHORT
        batch = [
            _S(strategy_id=sid, symbol="AAPL", direction=trend_dir,
               strength=70, confidence=0.7, market_regime=_M.TREND_UP)
            for sid in ["price_break", "ma_cross", "macd", "supertrend"]
        ]
        batch.append(_S(
            strategy_id="rsi_rebound", symbol="AAPL",
            direction=_D.LONG if random.random() > 0.5 else _D.SHORT,
            strength=60, confidence=0.6, market_regime=_M.TREND_UP,
        ))
        ortho.update_from_signal_batch(batch)

    print("ρ(ma_cross, macd)       =", round(ortho.pairwise_correlation("ma_cross", "macd"), 3))
    print("ρ(ma_cross, rsi_rebound)=", round(ortho.pairwise_correlation("ma_cross", "rsi_rebound"), 3))

    sigs = [
        _S(strategy_id=s, symbol="AAPL", direction=_D.LONG,
           strength=75, confidence=0.7, market_regime=_M.TREND_UP)
        for s in ["price_break", "ma_cross", "macd", "supertrend", "rsi_rebound"]
    ]
    base_w = {"price_break": 0.25, "ma_cross": 0.20, "macd": 0.15,
              "supertrend": 0.05, "rsi_rebound": 0.25}
    eff = ortho.compute_effective_weights(sigs, base_w)
    print("effective_n =", round(ortho.effective_n([s.strategy_id for s in sigs]), 3))
    for k in base_w:
        print(f"  {k:14s} base={base_w[k]:.3f}  eff={eff.get(k,0):.3f}")
    print("sum(base)=", sum(base_w.values()), " sum(eff)=", round(sum(eff.values()), 3))
