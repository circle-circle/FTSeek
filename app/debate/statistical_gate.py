"""
统计门禁 (Statistical Gate) — 策略回测结果的统计学验证

核心理念:
    在策略进入辩论环节之前，必须通过严格的统计检验。
    防止"多重比较陷阱"和"幸存者偏差"导致的假阳性。

检验项目:
    1. 最小样本量 (按时间框架)
    2. Bonferroni 校正后 p < 0.05
    3. 修正 Sharpe (按实际交易频率年化)
    4. 头部交易移除后仍显著 (Top-N Robustness)
    5. 蒙特卡洛置换检验 (1000 次)

版本: 1.0 | 2026-05-23
"""
from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class GateResult:
    """统计门禁结果"""
    passed: bool
    checks: Dict[str, bool] = field(default_factory=dict)
    metrics: Dict[str, float] = field(default_factory=dict)
    details: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": self.checks,
            "metrics": {k: round(v, 4) for k, v in self.metrics.items()},
            "details": self.details,
            "reason": self.reason,
        }


class StatisticalGate:
    """
    策略回测的统计学门禁

    Usage:
        gate = StatisticalGate()
        result = gate.evaluate(
            trades=[{"pnl": 5.0, "symbol": "US.AAPL"}, ...],
            n_total_tests=249,  # 你总共测试了多少策略
            timeframe="15min",
        )
        if result.passed:
            # 进入辩论环节
        else:
            print(f"未通过: {result.reason}")
    """

    # 按时间框架的最小样本量要求
    MIN_SAMPLES = {
        "tick": 500,
        "1min": 500,
        "5min": 200,
        "15min": 100,
        "30min": 80,
        "1h": 60,
        "daily": 50,
        "weekly": 30,
    }

    # 显著性水平
    ALPHA = 0.05

    def evaluate(
        self,
        trades: List[Dict[str, Any]],
        n_total_tests: int = 1,
        timeframe: str = "15min",
        trades_per_year: Optional[float] = None,
    ) -> GateResult:
        """
        完整统计门禁评估

        Args:
            trades: 交易列表, 每条必须包含 "pnl" (float)
            n_total_tests: 总共测试的策略数量 (用于 Bonferroni 校正)
            timeframe: 策略时间框架
            trades_per_year: 年均交易数 (None 则自动估算)

        Returns:
            GateResult
        """
        checks = {}
        metrics = {}
        details = {}
        fail_reasons = []

        # 提取 PnL 列表
        pnl_list = [float(t.get("pnl", 0)) for t in trades]
        n = len(pnl_list)

        if n == 0:
            return GateResult(
                passed=False, reason="No trades provided",
                checks={"has_trades": False},
            )

        # 基本统计
        mean_pnl = sum(pnl_list) / n
        std_pnl = self._std(pnl_list)
        total_pnl = sum(pnl_list)
        wins = sum(1 for p in pnl_list if p > 0)
        win_rate = wins / n

        metrics["n_trades"] = n
        metrics["mean_pnl"] = mean_pnl
        metrics["std_pnl"] = std_pnl
        metrics["total_pnl"] = total_pnl
        metrics["win_rate"] = win_rate

        # ── Check 1: 最小样本量 ──
        min_required = self.MIN_SAMPLES.get(timeframe, 100)
        checks["min_samples"] = n >= min_required
        if not checks["min_samples"]:
            fail_reasons.append(
                f"样本量不足: {n} < {min_required} ({timeframe})")

        # ── Check 2: 修正 Sharpe ──
        if trades_per_year is None:
            # 估算: 假设 5 年回测期
            years = max(1, n / max(min_required, 20))
            trades_per_year = n / years

        sharpe_per_trade = mean_pnl / std_pnl if std_pnl > 0 else 0
        corrected_sharpe = sharpe_per_trade * math.sqrt(trades_per_year)
        metrics["sharpe_per_trade"] = sharpe_per_trade
        metrics["corrected_sharpe"] = corrected_sharpe
        metrics["trades_per_year"] = trades_per_year

        checks["positive_sharpe"] = corrected_sharpe > 0
        if not checks["positive_sharpe"]:
            fail_reasons.append(f"修正Sharpe为负: {corrected_sharpe:.3f}")

        # ── Check 3: t-test + Bonferroni 校正 ──
        t_stat, p_raw = self._ttest_1sample(pnl_list)
        p_bonferroni = min(1.0, p_raw * n_total_tests)
        metrics["t_statistic"] = t_stat
        metrics["p_raw"] = p_raw
        metrics["p_bonferroni"] = p_bonferroni
        metrics["bonferroni_threshold"] = self.ALPHA / n_total_tests

        checks["bonferroni_significant"] = p_bonferroni < self.ALPHA
        if not checks["bonferroni_significant"]:
            fail_reasons.append(
                f"Bonferroni校正后不显著: p={p_bonferroni:.4f} >= {self.ALPHA}")

        # ── Check 4: Top-N 稳健性 ──
        top_n_robust = self._top_n_robustness(pnl_list, max_n=min(10, n // 5))
        metrics["top_n_robust"] = top_n_robust
        details["top_n_robustness"] = top_n_robust
        checks["top_n_robust"] = top_n_robust >= 3
        if not checks["top_n_robust"]:
            fail_reasons.append(
                f"Top-N稳健性不足: 移除{top_n_robust}笔后不显著 (需>=3)")

        # ── Check 5: 蒙特卡洛置换检验 ──
        mc_p = self._monte_carlo_permutation(pnl_list, n_permutations=1000)
        metrics["monte_carlo_p"] = mc_p
        checks["monte_carlo_significant"] = mc_p < self.ALPHA
        if not checks["monte_carlo_significant"]:
            fail_reasons.append(
                f"蒙特卡洛置换检验不显著: p={mc_p:.4f}")

        # ── Check 6: 利润集中度 ──
        sorted_pnl = sorted(pnl_list, reverse=True)
        top_3_pct = sum(sorted_pnl[:max(1, n // 10)]) / total_pnl if total_pnl > 0 else 1.0
        metrics["top_10pct_contribution"] = top_3_pct
        checks["concentration_acceptable"] = top_3_pct < 0.8
        if not checks["concentration_acceptable"]:
            fail_reasons.append(
                f"利润过度集中: Top 10%贡献 {top_3_pct:.1%}")

        # ── 综合判定 ──
        # 必须通过: min_samples, bonferroni OR monte_carlo, top_n_robust
        hard_pass = (
            checks["min_samples"]
            and (checks["bonferroni_significant"] or checks["monte_carlo_significant"])
            and checks["top_n_robust"]
        )

        reason = "; ".join(fail_reasons) if not hard_pass else "All critical checks passed"

        return GateResult(
            passed=hard_pass,
            checks=checks,
            metrics=metrics,
            details=details,
            reason=reason,
        )

    # ── 统计方法 ──

    @staticmethod
    def _std(values: List[float]) -> float:
        """样本标准差"""
        n = len(values)
        if n < 2:
            return 0.0
        mean = sum(values) / n
        variance = sum((x - mean) ** 2 for x in values) / (n - 1)
        return math.sqrt(variance)

    def _ttest_1sample(self, pnl_list: List[float]) -> Tuple[float, float]:
        """
        单样本 t 检验 (H0: mean <= 0, H1: mean > 0)

        Returns:
            (t_statistic, p_value_one_tail)
        """
        n = len(pnl_list)
        if n < 2:
            return 0.0, 1.0

        mean = sum(pnl_list) / n
        std = self._std(pnl_list)

        if std <= 0:
            return 0.0, 1.0 if mean <= 0 else 0.0

        t_stat = mean / (std / math.sqrt(n))

        # p-value 近似 (使用正态分布近似, n>=30 时足够准确)
        p_value = self._normal_cdf(-t_stat)  # 单尾: P(T > t) = P(Z < -t)

        return t_stat, p_value

    @staticmethod
    def _normal_cdf(x: float) -> float:
        """标准正态分布 CDF 近似 (Abramowitz-Stegun)"""
        # 精度: |error| < 7.5e-8
        a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
        p_const = 0.3275911

        sign = 1
        if x < 0:
            sign = -1
        x = abs(x) / math.sqrt(2)

        t = 1.0 / (1.0 + p_const * x)
        y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x * x)

        return 0.5 * (1.0 + sign * y)

    def _top_n_robustness(self, pnl_list: List[float], max_n: int = 10) -> int:
        """
        逐步移除最大盈利交易，找到仍显著的 N

        Returns:
            移除 N 笔后仍显著的最大 N 值
        """
        sorted_pnl = sorted(pnl_list, reverse=True)
        robust_n = 0

        for remove_count in range(1, max_n + 1):
            remaining = sorted_pnl[remove_count:]
            if len(remaining) < 5:
                break

            _, p = self._ttest_1sample(remaining)
            if p < self.ALPHA:
                robust_n = remove_count
            else:
                break

        return robust_n

    def _monte_carlo_permutation(
        self,
        pnl_list: List[float],
        n_permutations: int = 1000,
    ) -> float:
        """
        蒙特卡洛置换检验

        打乱 PnL 时序，计算真实均值在随机分布中的 p 值。
        H0: PnL 的时序无关 (即策略没有真正的 alpha)

        Returns:
            p_value: 随机排列中均值 >= 真实均值的比例
        """
        n = len(pnl_list)
        if n < 5:
            return 1.0

        true_mean = sum(pnl_list) / n
        count_extreme = 0

        rng = random.Random(42)  # 固定种子确保可复现

        for _ in range(n_permutations):
            shuffled = pnl_list.copy()
            rng.shuffle(shuffled)
            # 随机打乱后，"策略均值"应为 ~0
            # 我们用正负号随机翻转来模拟 H0
            # (等价于: 如果策略无 alpha, 每笔交易盈亏概率各半)
            flipped = [x * rng.choice([1, -1]) for x in shuffled]
            perm_mean = sum(flipped) / n
            if perm_mean >= true_mean:
                count_extreme += 1

        return count_extreme / n_permutations
