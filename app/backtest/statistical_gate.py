"""
统计验证门禁 (Statistical Gate)

策略回测必须通过以下所有检验才能进入辩论环节:
1. 最小样本量 (基于策略时间框架)
2. Bonferroni 校正后 p < 0.05
3. 修正 Sharpe (按实际交易频率年化)
4. 头部交易移除后仍显著
5. 蒙特卡洛置换检验 (1000 次)
"""
from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class GateResult:
    """统计门禁结果"""
    passed: bool = False
    sharpe_corrected: float = 0.0       # 修正年化 Sharpe
    p_value_bonferroni: float = 1.0     # Bonferroni 校正后 p 值
    monte_carlo_p: float = 1.0          # 蒙特卡洛置换 p 值
    top_n_robustness: int = 0           # 移除前 N 笔仍显著
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "sharpe_corrected": round(self.sharpe_corrected, 4),
            "p_value_bonferroni": round(self.p_value_bonferroni, 6),
            "monte_carlo_p": round(self.monte_carlo_p, 4),
            "top_n_robustness": self.top_n_robustness,
            "details": self.details,
        }


# 按时间框架的最小样本量
MIN_SAMPLES = {"5min": 200, "15min": 100, "daily": 50}


class StatisticalGate:
    """
    策略回测的统计学门禁

    Usage:
        gate = StatisticalGate()
        result = gate.evaluate(pnl_list, n_total_tests=9)
        if result.passed:
            print("通过统计门禁，可以进入辩论")
    """

    def evaluate(
        self,
        pnl_list: List[float],
        n_total_tests: int = 1,
        timeframe: str = "15min",
        trades_per_year: float = 252,
    ) -> GateResult:
        """
        完整统计门禁评估

        Args:
            pnl_list: 逐笔 PnL 列表
            n_total_tests: 同时检验的策略总数 (Bonferroni 校正)
            timeframe: 策略时间框架
            trades_per_year: 年化交易频率

        Returns:
            GateResult
        """
        result = GateResult()
        n = len(pnl_list)

        # 1. 最小样本量检查
        min_n = MIN_SAMPLES.get(timeframe, 100)
        if n < min_n:
            result.details["sample_size"] = f"FAIL: {n} < {min_n}"
            return result
        result.details["sample_size"] = f"PASS: {n} >= {min_n}"

        # 2. 计算基础 Sharpe
        mean_pnl = sum(pnl_list) / n
        if n > 1:
            variance = sum((p - mean_pnl) ** 2 for p in pnl_list) / (n - 1)
            std_pnl = math.sqrt(variance) if variance > 0 else 0
        else:
            std_pnl = 0

        sharpe_per_trade = mean_pnl / std_pnl if std_pnl > 0 else 0
        sharpe_corrected = sharpe_per_trade * math.sqrt(trades_per_year)
        result.sharpe_corrected = sharpe_corrected

        # 3. t 检验 (H0: mean_pnl = 0)
        if std_pnl > 0 and n > 1:
            t_stat = mean_pnl / (std_pnl / math.sqrt(n))
            # 近似 p-value (双侧, 用正态近似 for large n)
            p_raw = self._approx_p_value(t_stat)
        else:
            p_raw = 1.0

        # Bonferroni 校正
        p_bonferroni = min(p_raw * n_total_tests, 1.0)
        result.p_value_bonferroni = p_bonferroni
        result.details["p_raw"] = round(p_raw, 6)
        result.details["bonferroni_correction"] = n_total_tests

        if p_bonferroni >= 0.05:
            result.details["bonferroni"] = f"FAIL: p={p_bonferroni:.4f} >= 0.05"
            return result
        result.details["bonferroni"] = f"PASS: p={p_bonferroni:.4f} < 0.05"

        # 4. 蒙特卡洛置换检验
        mc_p = self._monte_carlo_permutation(pnl_list, n_permutations=1000)
        result.monte_carlo_p = mc_p
        if mc_p >= 0.05:
            result.details["monte_carlo"] = f"FAIL: p={mc_p:.4f} >= 0.05"
            return result
        result.details["monte_carlo"] = f"PASS: p={mc_p:.4f} < 0.05"

        # 5. 头部交易移除稳健性
        top_n = self._top_n_robustness(pnl_list, trades_per_year)
        result.top_n_robustness = top_n
        result.details["top_n_robustness"] = f"Can remove top {top_n} trades"

        if top_n == 0:
            result.details["robustness"] = "FAIL: Not robust to top trade removal"
            return result
        result.details["robustness"] = f"PASS: Robust after removing top {top_n}"

        # 全部通过
        result.passed = True
        return result

    def _approx_p_value(self, t_stat: float) -> float:
        """近似双侧 p-value (正态近似)"""
        # 使用标准正态 CDF 近似
        z = abs(t_stat)
        # Abramowitz & Stegun approximation
        p = 0.5 * math.erfc(z / math.sqrt(2))
        return 2 * p  # 双侧

    def _monte_carlo_permutation(
        self, pnl_list: List[float], n_permutations: int = 1000
    ) -> float:
        """
        蒙特卡洛符号翻转检验 (Sign Permutation Test)

        H0: mean(PnL) = 0
        方法: 随机翻转每笔 PnL 的符号，计算翻转后均值 >= 真实均值的频率
        这等价于 bootstrap 检验均值是否显著不同于零
        """
        if not pnl_list:
            return 1.0

        n = len(pnl_list)
        real_sum = sum(pnl_list)
        count_extreme = 0

        rng = random.Random(42)  # 可复现

        for _ in range(n_permutations):
            # 随机翻转符号: 每笔 PnL 有 50% 概率取负
            perm_sum = sum(p if rng.random() > 0.5 else -p for p in pnl_list)
            if perm_sum >= real_sum:
                count_extreme += 1

        return count_extreme / n_permutations if n_permutations > 0 else 1.0

    def _top_n_robustness(self, pnl_list: List[float], trades_per_year: float) -> int:
        """
        逐步移除最大盈利，找到仍显著的 N

        返回: 移除前 N 笔最大盈利后 Sharpe 仍 > 0 的最大 N
        """
        if not pnl_list:
            return 0

        sorted_desc = sorted(pnl_list, reverse=True)
        max_remove = min(len(pnl_list) // 5, 20)  # 最多移除 20%

        for n_remove in range(1, max_remove + 1):
            remaining = sorted_desc[n_remove:]
            if len(remaining) < 10:
                return n_remove - 1

            mean_r = sum(remaining) / len(remaining)
            if mean_r <= 0:
                return n_remove - 1

            if len(remaining) > 1:
                var_r = sum((p - mean_r) ** 2 for p in remaining) / (len(remaining) - 1)
                std_r = math.sqrt(var_r) if var_r > 0 else 0
                sharpe_r = mean_r / std_r if std_r > 0 else 0
                if sharpe_r <= 0:
                    return n_remove - 1

        return max_remove
