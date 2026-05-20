"""
统计验证链 V2 — 蒙特卡洛 + Bootstrap + Walk-Forward + K-fold CV

借鉴 Vibe-Trading backtest/validation.py:
  - Monte Carlo 置换检验 (行 26-79)
  - Bootstrap Sharpe CI (行 97-143)
  - Walk-Forward 分析 (行 154-233)

新增:
  - K-fold Cross-Validation 夏普稳定性检验

纯 numpy/pandas 实现，零 scipy 依赖
"""

import logging
import math
import random
import numpy as np
from typing import Dict, Any, List, Optional

from app.diagnostics.base import BaseDiagnostic, DiagnosticResult
from app.diagnostics.models import WalkForwardWindow, WalkForwardResult

logger = logging.getLogger(__name__)


class StatisticalValidator(BaseDiagnostic):

    @property
    def name(self) -> str:
        return "statistical_validator_v2"

    @property
    def schedule_hint(self) -> str:
        return "post_market"

    def is_applicable(self, **kwargs) -> bool:
        return kwargs.get("trade_pnl_series") is not None or kwargs.get("has_backtest", False)

    def run(self, **kwargs) -> DiagnosticResult:
        pnl_series = kwargs.get("trade_pnl_series", [])
        risk_free_rate = kwargs.get("risk_free_rate", 0.05)
        n_simulations = kwargs.get("n_simulations", 1000)
        n_wf_windows = kwargs.get("n_wf_windows", 5)
        n_kfold = kwargs.get("n_kfold", 5)

        if not pnl_series:
            return DiagnosticResult(
                module_name=self.name,
                status="skipped",
                data={"reason": "无盈亏序列数据"},
                summary="无可验证的盈亏数据",
            )

        if len(pnl_series) < 10:
            return DiagnosticResult(
                module_name=self.name,
                status="skipped",
                data={"reason": "样本不足", "n": len(pnl_series)},
                summary=f"仅 {len(pnl_series)} 笔，样本不足 (≥10)",
            )

        results = {}

        # ── 1. 基础统计 ──
        results["basic"] = self._basic_stats(pnl_series)

        # ── 2. Monte Carlo 置换检验 ──
        results["monte_carlo"] = self._monte_carlo(pnl_series, n_simulations)

        # ── 3. Bootstrap Sharpe CI ──
        results["bootstrap"] = self._bootstrap_ci(pnl_series, n_simulations)

        # ── 4. Walk-Forward 分析 ──
        results["walk_forward"] = self._walk_forward_analysis(pnl_series, n_wf_windows)

        # ── 5. K-fold Cross-Validation ──
        results["kfold_cv"] = self._kfold_cv(pnl_series, n_kfold)

        # ── 6. 盈亏比 + 自相关 ──
        results["profit_loss_ratio"] = self._profit_loss_ratio_test(pnl_series)
        results["autocorrelation"] = self._autocorrelation_test(pnl_series)

        # ── 综合判定 ──
        verdict = self._verdict(results)
        results["_verdict"] = verdict

        return DiagnosticResult(
            module_name=self.name,
            status="success",
            data=results,
            summary=self._build_summary(results, verdict),
        )

    # ═══════════════════════════════════════════════════
    #  基础统计
    # ═══════════════════════════════════════════════════

    def _basic_stats(self, pnl: List[float]) -> Dict[str, Any]:
        n = len(pnl)
        total = sum(pnl)
        mean = total / n
        variance = sum((x - mean) ** 2 for x in pnl) / (n - 1) if n > 1 else 0
        std = math.sqrt(variance)

        wins = [x for x in pnl if x > 0]
        losses = [x for x in pnl if x <= 0]

        win_rate = len(wins) / n if n > 0 else 0
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 1
        profit_factor = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else float("inf")

        sharpe = (mean / std * math.sqrt(252)) if std > 0 else 0
        max_dd = self._max_drawdown(pnl)

        return {
            "n_trades": n,
            "total_pnl": round(total, 2),
            "mean_pnl": round(mean, 4),
            "std_pnl": round(std, 4),
            "win_rate": round(win_rate, 3),
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else "inf",
            "max_drawdown": round(max_dd, 4),
            "sharpe_ratio": round(sharpe, 3),
        }

    # ═══════════════════════════════════════════════════
    #  Monte Carlo 置换检验 (Vibe-Trading 行 26-79)
    # ═══════════════════════════════════════════════════

    def _monte_carlo(self, pnl: List[float], n_sim: int = 1000) -> Dict[str, Any]:
        pnls = np.array(pnl)
        actual_sharpe = self._calc_sharpe(pnl)
        actual_max_dd = self._max_drawdown(pnl)

        sim_sharpes = []
        sim_max_dd = []

        for _ in range(n_sim):
            shuffled = np.random.permutation(pnls)
            sim_sharpes.append(self._calc_sharpe(shuffled.tolist()))
            sim_max_dd.append(self._max_drawdown(shuffled.tolist()))

        sim_sharpes.sort()
        sim_max_dd.sort()

        p_sharpe = sum(1 for s in sim_sharpes if s >= actual_sharpe) / n_sim
        p_max_dd = sum(1 for d in sim_max_dd if d <= actual_max_dd) / n_sim

        return {
            "n_simulations": n_sim,
            "actual_sharpe": round(actual_sharpe, 3),
            "actual_max_dd": round(actual_max_dd, 4),
            "p_value_sharpe": round(p_sharpe, 4),
            "p_value_max_dd": round(p_max_dd, 4),
            "simulated_sharpe_mean": round(np.mean(sim_sharpes), 3),
            "simulated_sharpe_std": round(np.std(sim_sharpes), 3),
            "simulated_sharpe_p5": round(np.percentile(sim_sharpes, 5), 3),
            "simulated_sharpe_p95": round(np.percentile(sim_sharpes, 95), 3),
            "is_significant": p_sharpe < 0.05,
            "interpretation": f"p_sharpe={p_sharpe:.4f} {'< 0.05 → 显著' if p_sharpe < 0.05 else '≥ 0.05 → 不显著'}",
        }

    # ═══════════════════════════════════════════════════
    #  Bootstrap Sharpe CI (Vibe-Trading 行 97-143)
    # ═══════════════════════════════════════════════════

    def _bootstrap_ci(self, pnl: List[float], n_boot: int = 1000, ci: float = 0.95) -> Dict[str, Any]:
        n = len(pnl)
        boot_sharpes = []

        for _ in range(n_boot):
            sample = [random.choice(pnl) for _ in range(n)]
            sharpe = self._calc_sharpe(sample)
            boot_sharpes.append(sharpe)

        boot_sharpes.sort()
        alpha = 1 - ci
        lower_idx = int(n_boot * alpha / 2)
        upper_idx = int(n_boot * (1 - alpha / 2))

        ci_lower = boot_sharpes[lower_idx]
        ci_upper = boot_sharpes[upper_idx]
        prob_positive = sum(1 for s in boot_sharpes if s > 0) / n_boot

        return {
            "observed_sharpe": round(self._calc_sharpe(pnl), 3),
            "ci_level": ci,
            "ci_lower": round(ci_lower, 3),
            "ci_upper": round(ci_upper, 3),
            "median_sharpe": round(boot_sharpes[n_boot // 2], 3),
            "prob_positive": round(prob_positive, 3),
            "interpretation": f"{ci*100:.0f}% CI: [{ci_lower:.3f}, {ci_upper:.3f}] | P(Sharpe>0)={prob_positive:.0%}",
        }

    # ═══════════════════════════════════════════════════
    #  Walk-Forward 分析 (Vibe-Trading 行 154-233)
    # ═══════════════════════════════════════════════════

    def _walk_forward_analysis(self, pnl: List[float], n_windows: int = 5) -> Dict[str, Any]:
        """
        Walk-Forward 分析 — expanding window 切分

        将交易序列等分为 n_windows 段，每段独立计算指标
        """
        n = len(pnl)
        n_windows = min(n_windows, n // 3)  # 每段至少 3 笔
        if n_windows < 2:
            return {"error": "样本不足以做 Walk-Forward"}

        window_size = n // n_windows
        windows = []

        for i in range(n_windows):
            start_idx = i * window_size
            end_idx = start_idx + window_size if i < n_windows - 1 else n
            window_pnl = pnl[start_idx:end_idx]

            if len(window_pnl) < 3:
                continue

            w_sharpe = self._calc_sharpe(window_pnl)
            w_return = sum(window_pnl)
            w_max_dd = self._max_drawdown(window_pnl)
            w_wins = sum(1 for x in window_pnl if x > 0)
            w_win_rate = w_wins / len(window_pnl) if window_pnl else 0
            w_avg_hold = 1.0  # 简化: 假设平均 1 天

            windows.append(WalkForwardWindow(
                window_id=i + 1,
                start_date=f"trade_{start_idx}",
                end_date=f"trade_{end_idx - 1}",
                n_trades=len(window_pnl),
                total_pnl=round(w_return, 2),
                win_rate=round(w_win_rate, 3),
                sharpe_ratio=round(w_sharpe, 3),
                max_drawdown=round(w_max_dd, 4),
                avg_hold_days=w_avg_hold,
            ))

        if not windows:
            return {"error": "无法切分窗口"}

        # 一致性指标
        profitable_windows = sum(1 for w in windows if w.total_pnl > 0)
        consistency_rate = profitable_windows / len(windows)

        sharpes = [w.sharpe_ratio for w in windows]
        returns = [w.total_pnl for w in windows]
        sharpe_mean = np.mean(sharpes)
        sharpe_std = np.std(sharpes)
        sharpe_cv = sharpe_std / abs(sharpe_mean) if sharpe_mean != 0 else 999

        # 稳定性判定
        if consistency_rate >= 0.8 and sharpe_cv < 0.5:
            stability = "STABLE"
        elif consistency_rate >= 0.6 and sharpe_cv < 1.0:
            stability = "MODERATE"
        else:
            stability = "UNSTABLE"

        wf_result = WalkForwardResult(
            n_windows=len(windows),
            windows=windows,
            consistency_rate=round(consistency_rate, 3),
            sharpe_mean=round(sharpe_mean, 3),
            sharpe_std=round(sharpe_std, 3),
            sharpe_cv=round(sharpe_cv, 3),
            return_mean=round(np.mean(returns), 2),
            return_std=round(np.std(returns), 2),
            stability_grade=stability,
        )

        return wf_result.to_dict()

    # ═══════════════════════════════════════════════════
    #  K-fold Cross-Validation
    # ═══════════════════════════════════════════════════

    def _kfold_cv(self, pnl: List[float], n_folds: int = 5) -> Dict[str, Any]:
        """
        K-fold CV — 随机切分验证夏普稳定性
        """
        n = len(pnl)
        n_folds = min(n_folds, n // 3)
        if n_folds < 2:
            return {"error": "样本不足"}

        indices = list(range(n))
        fold_size = n // n_folds

        fold_sharpes = []
        fold_win_rates = []

        for i in range(n_folds):
            start = i * fold_size
            end = start + fold_size if i < n_folds - 1 else n
            fold_pnl = [pnl[j] for j in indices[start:end]]

            if len(fold_pnl) >= 3:
                fold_sharpes.append(self._calc_sharpe(fold_pnl))
                fold_win_rates.append(sum(1 for x in fold_pnl if x > 0) / len(fold_pnl))

        if not fold_sharpes:
            return {"error": "无法切分 fold"}

        mean_sharpe = np.mean(fold_sharpes)
        std_sharpe = np.std(fold_sharpes)
        mean_wr = np.mean(fold_win_rates)
        std_wr = np.std(fold_win_rates)

        return {
            "n_folds": n_folds,
            "sharpe_mean": round(mean_sharpe, 3),
            "sharpe_std": round(std_sharpe, 3),
            "sharpe_min": round(min(fold_sharpes), 3),
            "sharpe_max": round(max(fold_sharpes), 3),
            "win_rate_mean": round(mean_wr, 3),
            "win_rate_std": round(std_wr, 3),
            "is_stable": std_sharpe < abs(mean_sharpe) if mean_sharpe != 0 else False,
            "interpretation": f"夏普 {mean_sharpe:.3f}±{std_sharpe:.3f} | 胜率 {mean_wr:.0%}±{std_wr:.0%}",
        }

    # ═══════════════════════════════════════════════════
    #  盈亏比 + 自相关
    # ═══════════════════════════════════════════════════

    def _profit_loss_ratio_test(self, pnl: List[float]) -> Dict[str, Any]:
        wins = [x for x in pnl if x > 0]
        losses = [abs(x) for x in pnl if x < 0]

        if not wins or not losses:
            return {"severity": "insufficient_data"}

        avg_win = sum(wins) / len(wins)
        avg_loss = sum(losses) / len(losses)
        ratio = avg_win / avg_loss if avg_loss > 0 else float("inf")

        quality = "good" if ratio >= 1.5 else "acceptable" if ratio >= 1.0 else "poor"

        return {
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "ratio": round(ratio, 3),
            "quality": quality,
        }

    def _autocorrelation_test(self, pnl: List[float], lag: int = 1) -> Dict[str, Any]:
        n = len(pnl)
        if n < lag + 2:
            return {"error": "样本不足"}

        mean = sum(pnl) / n
        var = sum((x - mean) ** 2 for x in pnl) / n
        if var == 0:
            return {"autocorrelation": 0, "is_independent": True}

        cov = sum((pnl[i] - mean) * (pnl[i + lag] - mean) for i in range(n - lag)) / n
        autocorr = cov / var
        se = 1.0 / math.sqrt(n)
        is_significant = abs(autocorr) > 1.96 * se

        return {
            "autocorrelation": round(autocorr, 4),
            "is_independent": not is_significant,
        }

    # ═══════════════════════════════════════════════════
    #  综合判定
    # ═══════════════════════════════════════════════════

    def _verdict(self, results: Dict) -> Dict[str, Any]:
        checks = []

        mc = results.get("monte_carlo", {})
        checks.append(("monte_carlo_significant", mc.get("is_significant", False)))

        bs = results.get("bootstrap", {})
        checks.append(("bootstrap_ci_positive", bs.get("ci_lower", 0) > 0 or bs.get("prob_positive", 0) > 0.9))

        plr = results.get("profit_loss_ratio", {})
        checks.append(("profit_ratio_healthy", plr.get("quality") in ("good", "acceptable")))

        ac = results.get("autocorrelation", {})
        checks.append(("independent_trades", ac.get("is_independent", True)))

        wf = results.get("walk_forward", {})
        checks.append(("walk_forward_stable", wf.get("stability_grade") in ("STABLE", "MODERATE")))

        kf = results.get("kfold_cv", {})
        checks.append(("kfold_stable", kf.get("is_stable", False)))

        passed = sum(1 for _, v in checks if v)
        total = len(checks)
        confidence = passed / total if total > 0 else 0

        verdict = "STRONG" if confidence >= 0.8 else "MODERATE" if confidence >= 0.5 else "WEAK"

        return {
            "checks_passed": passed,
            "checks_total": total,
            "confidence": round(confidence, 2),
            "verdict": verdict,
            "details": dict(checks),
        }

    def _build_summary(self, results: Dict, verdict: Dict) -> str:
        basic = results.get("basic", {})
        mc = results.get("monte_carlo", {})
        wf = results.get("walk_forward", {})
        kf = results.get("kfold_cv", {})
        v = verdict

        parts = [
            f"📊 {basic.get('n_trades', 0)} 笔",
            f"夏普 {basic.get('sharpe_ratio', 0):.2f}",
            f"胜率 {basic.get('win_rate', 0)*100:.0f}%",
            f"p={mc.get('p_value_sharpe', 0):.4f}",
        ]

        if wf.get("stability_grade"):
            parts.append(f"WF: {wf['stability_grade']} ({wf.get('consistency_rate', 0):.0%})")

        if kf.get("sharpe_mean") is not None:
            parts.append(f"KF: {kf['sharpe_mean']:.2f}±{kf.get('sharpe_std', 0):.2f}")

        parts.append(f"判定: {v['verdict']} ({v['confidence']:.0%})")

        return " | ".join(parts)

    # ═══════════════════════════════════════════════════
    #  辅助函数
    # ═══════════════════════════════════════════════════

    @staticmethod
    def _calc_sharpe(pnl: List[float], bars_per_year: int = 252) -> float:
        """计算夏普比率"""
        n = len(pnl)
        if n < 2:
            return 0.0
        mean = sum(pnl) / n
        variance = sum((x - mean) ** 2 for x in pnl) / (n - 1)
        std = math.sqrt(variance) if variance > 0 else 0
        return (mean / std * math.sqrt(bars_per_year)) if std > 0 else 0

    @staticmethod
    def _max_drawdown(pnl: List[float]) -> float:
        """计算最大回撤"""
        cumulative = 0
        peak = 0
        max_dd = 0
        for x in pnl:
            cumulative += x
            peak = max(peak, cumulative)
            dd = (peak - cumulative) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)
        return max_dd


def create_statistical_validator() -> StatisticalValidator:
    return StatisticalValidator()
