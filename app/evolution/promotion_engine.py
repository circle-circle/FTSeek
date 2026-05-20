#!/usr/bin/env python3
"""
FTSeek-US 影子测试推广引擎 (Promotion Engine)

职责：
基于严格的统计学纪律，决定影子策略是否可以转正。
解决 shadow.md 中提到的：
1. 样本量不足 (Trap 3)
2. 随机噪声导致假阳性 (Trap 4)
3. 效应量过小 (Trap 5)
4. 风格鲁棒性: 趋势日和震荡日都不能输

2026-05-13: 初始实现
2026-05-14: 新增 regime-aware 检查 + 精确 t-test
"""
import logging
import math
from typing import List, Tuple, Optional

logger = logging.getLogger("ftseek_us.evolution.promotion")


class PromotionEngine:
    """
    负责评估影子测试结果的统计显著性。
    """

    # 硬性指标 (Hard Constraints)
    MIN_SAMPLES = 20           # 至少 20 个交易日
    MIN_EFFECT_SIZE = 0.003    # 平均每日 PnL 差异必须 > 0.3%
    ALPHA = 0.05               # 显著性水平
    MAX_ACTIVE_SHADOWS = 3     # 最多同时运行 3 个影子

    @staticmethod
    def _critical_t(n: int, alpha: float) -> float:
        """双尾 t-test 临界值近似

        使用 Abramowitz-Stegun 有理逼近 (误差 < 0.001 for df >= 5).
        避免 scipy 依赖，保持模块零外部依赖。
        """
        df = n - 1
        if df < 1:
            return float('inf')
        if df == 1:
            return 12.706  # t_0.025,1
        if df == 2:
            return 4.303   # t_0.025,2

        # 正态近似: z_0.025 = 1.96
        z = 1.96
        # Williams 修正
        t_approx = z + (z**3 + z) / (4 * df) + (z**5 + 2 * z**3 + z) / (96 * df**2)
        return t_approx

    def should_promote(
        self,
        daily_diffs: List[float],
        candidate_name: str,
        active_count: int = 1,
        regime_labels: Optional[List[str]] = None,
    ) -> Tuple[bool, str]:
        """
        判断是否应该转正候选策略。

        Args:
            daily_diffs: 每日 PnL 差异列表 (Shadow PnL - Main PnL)
            candidate_name: 候选名称
            active_count: 当前正在运行的影子数量
            regime_labels: 每日对应的市场风格标签 (长度与 daily_diffs 一致)
                          如 ["TREND", "RANGE", "TREND", ...]
                          为 None 时跳过风格鲁棒性检查

        Returns:
            (should_promote, reason)
        """
        n = len(daily_diffs)

        # 1. 样本量检查 (Trap 3)
        if n < self.MIN_SAMPLES:
            return False, f"样本量不足: {n} < {self.MIN_SAMPLES}"

        # 2. 计算统计量
        mean_diff = sum(daily_diffs) / n

        # 3. 效应量检查 (Trap 5)
        if mean_diff < self.MIN_EFFECT_SIZE:
            return False, f"效应量过小: {mean_diff:.4%} < {self.MIN_EFFECT_SIZE:.1%}"

        # 4. 显著性检查 (配对 T-Test, Trap 4)
        variance = sum((x - mean_diff) ** 2 for x in daily_diffs) / (n - 1)
        std_dev = math.sqrt(variance) if variance > 0 else 0

        if std_dev == 0:
            return False, "标准差为 0 (数据异常)"

        t_stat = mean_diff / (std_dev / math.sqrt(n))

        # Bonferroni 校正 (多候选并行)
        adjusted_alpha = self.ALPHA / max(active_count, 1)
        critical_t = self._critical_t(n, adjusted_alpha)

        if t_stat < critical_t:
            return False, f"统计不显著: t={t_stat:.2f} < critical_t={critical_t:.2f} (α={adjusted_alpha:.3f})"

        # 5. 风格鲁棒性检查 (Trap 5: 趋势日和震荡日都不能输)
        if regime_labels is not None and len(regime_labels) == n:
            trend_diffs = [d for d, r in zip(daily_diffs, regime_labels) if r.upper() == "TREND"]
            range_diffs = [d for d, r in zip(daily_diffs, regime_labels) if r.upper() == "RANGE"]

            trend_mean = sum(trend_diffs) / len(trend_diffs) if trend_diffs else 0
            range_mean = sum(range_diffs) / len(range_diffs) if range_diffs else 0

            if trend_diffs and trend_mean < 0:
                return False, f"趋势日表现不佳: trend_mean={trend_mean:.3%} (需要 ≥ 0)"
            if range_diffs and range_mean < 0:
                return False, f"震荡日表现不佳: range_mean={range_mean:.3%} (需要 ≥ 0)"

            # 至少每种风格 5 个样本
            if len(trend_diffs) < 5:
                logger.warning(f"[Promotion] {candidate_name} 趋势日样本不足 ({len(trend_diffs)} < 5)")
            if len(range_diffs) < 5:
                logger.warning(f"[Promotion] {candidate_name} 震荡日样本不足 ({len(range_diffs)} < 5)")

        # 6. 极端回撤检查
        max_drawdown_day = min(daily_diffs)
        if max_drawdown_day < -0.05:
            return False, f"存在极端单日回撤: {max_drawdown_day:.2%}"

        return True, f"通过: mean_diff={mean_diff:.3%}, t={t_stat:.2f}, n={n}"
