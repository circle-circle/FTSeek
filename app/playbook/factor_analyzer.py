"""
Playbook Factor Analyzer — 双层因子分析 + 贝叶斯估计

核心功能 (spec 6.3):
  1. Layer A: 单因子贡献分析 (每个 condition 的胜率/盈亏)
  2. Layer B: 条件对分析 (两个 condition 组合的胜率)
  3. Layer C: 精确组合 (≥5 次的完整条件组合)
  4. 贝叶斯二项分布估计 (Beta-Binomial, 小样本更可靠)
  5. 因子判定: positive / negative / observing

Usage:
  from app.playbook.factor_analyzer import FactorAnalyzer

  analyzer = FactorAnalyzer(playbook_db)
  analyzer.update_all_stats()
  report = analyzer.generate_factor_report()
"""
import logging
import json
import hashlib
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from collections import defaultdict

from app.playbook.db import PlaybookDB

logger = logging.getLogger(__name__)


@dataclass
class BayesianEstimate:
    """贝叶斯二项分布估计结果"""
    mean: float           # 后验均值
    lower_95: float       # 95% 可信区间下界
    upper_95: float       # 95% 可信区间上界


@dataclass
class TradeStats:
    """交易统计汇总"""
    total: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    max_win: float = 0.0
    max_loss: float = 0.0
    pnls: List[float] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.wins / self.total if self.total > 0 else 0

    @property
    def avg_pnl(self) -> float:
        return self.total_pnl / self.total if self.total > 0 else 0

    @property
    def avg_pnl_pct(self) -> float:
        """平均盈亏百分比 (需要 pnl_pct 数据)"""
        return 0.0  # Computed separately when data available

    @property
    def profit_factor(self) -> float:
        if self.gross_loss <= 0:
            return float('inf') if self.gross_profit > 0 else 0
        return self.gross_profit / abs(self.gross_loss)


def bayesian_binomial(
    wins: int,
    total: int,
    prior_alpha: float = 1.0,
    prior_beta: float = 1.0,
) -> BayesianEstimate:
    """
    贝叶斯二项分布估计 (Beta-Binomial)

    先验: Beta(prior_alpha, prior_beta) = 均匀分布 Beta(1,1) (无信息先验)
    后验: Beta(prior_alpha + wins, prior_beta + losses)

    小样本示例:
      20笔16胜 → 后验均值 = (1+16)/(2+20) = 77.3%
      CI = [55%, 92%] (宽区间反映不确定性)

    大样本示例:
      100笔72胜 → 后验均值 = (1+72)/(2+100) = 71.6%
      CI = [62%, 80%] (窄区间, 高确定性)
    """
    if total <= 0:
        return BayesianEstimate(mean=0.5, lower_95=0.0, upper_95=1.0)

    losses = total - wins
    alpha = prior_alpha + wins
    beta = prior_beta + losses

    mean = alpha / (alpha + beta)

    try:
        from scipy.stats import beta as beta_dist
        lower = beta_dist.ppf(0.025, alpha, beta)
        upper = beta_dist.ppf(0.975, alpha, beta)
    except ImportError:
        # Fallback: normal approximation for large samples
        import math
        std = math.sqrt(mean * (1 - mean) / (alpha + beta))
        lower = max(0, mean - 1.96 * std)
        upper = min(1, mean + 1.96 * std)

    return BayesianEstimate(
        mean=round(mean, 4),
        lower_95=round(lower, 4),
        upper_95=round(upper, 4),
    )


def classify_factor(bayesian: BayesianEstimate, stats: TradeStats) -> str:
    """
    因子判定规则 (使用贝叶斯区间而非 naive 胜率):

    | 条件 | 判定 |
    |------|------|
    | 贝叶斯下界 > 55% 且 profit_factor > 1.2 | positive |
    | 贝叶斯上界 < 45% 或 profit_factor < 0.8 | negative |
    | 其他 | observing |
    """
    pf = stats.profit_factor

    if bayesian.lower_95 > 0.55 and pf > 1.2:
        return "positive"
    elif bayesian.upper_95 < 0.45 or (pf < 0.8 and pf > 0):
        return "negative"
    else:
        return "observing"


def compute_trade_stats(trades: List[Dict]) -> TradeStats:
    """从交易记录列表计算统计数据"""
    stats = TradeStats()

    for t in trades:
        pnl = t.get("pnl", 0) or 0
        outcome = t.get("outcome", "")

        stats.total += 1
        stats.total_pnl += pnl
        stats.pnls.append(pnl)

        if outcome == "win" or pnl > 0:
            stats.wins += 1
            stats.gross_profit += pnl
            stats.max_win = max(stats.max_win, pnl)
        elif outcome == "loss" or pnl < 0:
            stats.losses += 1
            stats.gross_loss += abs(pnl)
            stats.max_loss = min(stats.max_loss, pnl)

    return stats


def hash_conditions(conditions: List[str]) -> str:
    """对条件列表生成确定性 hash"""
    sorted_conds = sorted(conditions)
    key = "|".join(sorted_conds)
    return hashlib.md5(key.encode()).hexdigest()[:12]


class FactorAnalyzer:
    """双层因子分析器"""

    def __init__(self, playbook_db: PlaybookDB = None):
        self.db = playbook_db or PlaybookDB()

    def update_all_stats(self, days_back: int = 90) -> Dict[str, int]:
        """
        更新所有因子统计 (主入口)

        流程:
        1. 获取有结果的指令 (非 SKIP, 非 holding)
        2. Layer A: 单因子分析
        3. Layer B: 条件对分析
        4. Layer C: 精确组合 (≥5 次)

        Returns:
            {"single": N, "pair": N, "combo": N} 更新数量
        """
        results = self.db.get_instructions_with_outcome(days_back=days_back)
        if not results:
            logger.info("No results to analyze")
            return {"single": 0, "pair": 0, "combo": 0}

        logger.info(f"Analyzing {len(results)} instructions")

        single_count = self._update_single_factors(results)
        pair_count = self._update_pair_factors(results)
        combo_count = self._update_exact_combos(results)

        logger.info(f"Factor stats updated: single={single_count}, pair={pair_count}, combo={combo_count}")
        return {"single": single_count, "pair": pair_count, "combo": combo_count}

    def _update_single_factors(self, results: List[Dict]) -> int:
        """Layer A: 单因子贡献分析"""
        factor_trades = defaultdict(list)

        for r in results:
            conditions = self._parse_conditions(r.get("conditions_json"))
            for condition in conditions:
                factor_trades[condition].append(r)

        count = 0
        for factor_key, trades in factor_trades.items():
            stats = compute_trade_stats(trades)
            bayesian = bayesian_binomial(stats.wins, stats.total)
            verdict = classify_factor(bayesian, stats)

            self.db.upsert_factor_stat({
                "factor_key": factor_key,
                "analysis_level": "single",
                "total_trades": stats.total,
                "wins": stats.wins,
                "losses": stats.losses,
                "win_rate": round(stats.win_rate, 4),
                "avg_pnl": round(stats.avg_pnl, 2),
                "total_pnl": round(stats.total_pnl, 2),
                "profit_factor": round(stats.profit_factor, 2) if stats.profit_factor != float('inf') else 999.0,
                "max_win": round(stats.max_win, 2),
                "max_loss": round(stats.max_loss, 2),
                "bayesian_win_rate": bayesian.mean,
                "bayesian_lower": bayesian.lower_95,
                "bayesian_upper": bayesian.upper_95,
                "effective_sample_size": stats.total,
                "verdict": verdict,
            })
            count += 1

        return count

    def _update_pair_factors(self, results: List[Dict]) -> int:
        """Layer B: 条件对分析"""
        pair_trades = defaultdict(list)

        for r in results:
            conditions = sorted(self._parse_conditions(r.get("conditions_json")))
            # 生成所有两两组合
            for i in range(len(conditions)):
                for j in range(i + 1, len(conditions)):
                    pair_key = f"{conditions[i]}|{conditions[j]}"
                    pair_trades[pair_key].append(r)

        count = 0
        for pair_key, trades in pair_trades.items():
            if len(trades) < 3:
                continue  # 样本太少, 跳过

            stats = compute_trade_stats(trades)
            bayesian = bayesian_binomial(stats.wins, stats.total)
            verdict = classify_factor(bayesian, stats)

            co_factors = pair_key.split("|")

            self.db.upsert_factor_stat({
                "factor_key": pair_key,
                "analysis_level": "pair",
                "co_factors": co_factors,
                "total_trades": stats.total,
                "wins": stats.wins,
                "losses": stats.losses,
                "win_rate": round(stats.win_rate, 4),
                "avg_pnl": round(stats.avg_pnl, 2),
                "total_pnl": round(stats.total_pnl, 2),
                "profit_factor": round(stats.profit_factor, 2) if stats.profit_factor != float('inf') else 999.0,
                "max_win": round(stats.max_win, 2),
                "max_loss": round(stats.max_loss, 2),
                "bayesian_win_rate": bayesian.mean,
                "bayesian_lower": bayesian.lower_95,
                "bayesian_upper": bayesian.upper_95,
                "effective_sample_size": stats.total,
                "verdict": verdict,
            })
            count += 1

        return count

    def _update_exact_combos(self, results: List[Dict]) -> int:
        """Layer C: 精确组合 (≥5 次)"""
        combo_trades = defaultdict(list)

        for r in results:
            conditions = sorted(self._parse_conditions(r.get("conditions_json")))
            if len(conditions) < 2:
                continue
            combo_key = hash_conditions(conditions)
            combo_trades[combo_key].append({
                "conditions": conditions,
                "trades": [r],
            })

        count = 0
        for combo_key, entries in combo_trades.items():
            # 收集所有交易
            all_trades = []
            conditions = entries[0]["conditions"]
            for entry in entries:
                all_trades.extend(entry["trades"])

            if len(all_trades) < 5:
                continue  # 样本不足

            stats = compute_trade_stats(all_trades)
            bayesian = bayesian_binomial(stats.wins, stats.total)

            # 判定状态
            pf = stats.profit_factor
            if bayesian.lower_95 > 0.55 and pf > 1.2:
                status = "active"
            elif bayesian.upper_95 < 0.45 or (pf < 0.8 and pf > 0):
                status = "retired"
            else:
                status = "observing"

            from app.utils.timezone import now_utc
            now = now_utc().isoformat()

            self.db.upsert_pattern({
                "pattern_key": combo_key,
                "conditions": conditions,
                "total_trades": stats.total,
                "wins": stats.wins,
                "losses": stats.losses,
                "win_rate": round(stats.win_rate, 4),
                "avg_pnl": round(stats.avg_pnl, 2),
                "max_win": round(stats.max_win, 2),
                "max_loss": round(stats.max_loss, 2),
                "profit_factor": round(pf, 2) if pf != float('inf') else 999.0,
                "bayesian_win_rate": bayesian.mean,
                "bayesian_lower": bayesian.lower_95,
                "bayesian_upper": bayesian.upper_95,
                "status": status,
                "last_seen": now,
            })
            count += 1

        return count

    def update_horizon_stats(self, days_back: int = 90) -> Dict[str, int]:
        """
        更新 horizon 收益统计 (供 HorizonSelector 使用)

        Args:
            days_back: 回溯天数

        Returns:
            {\"updated\": int}
        """
        from app.playbook.horizon_return_model import HorizonReturnModel
        model = HorizonReturnModel(self.db)
        return model.update(days_back=days_back)

    def generate_factor_report(self) -> Dict[str, Any]:
        """
        生成因子分析报告 (用于周报 + LLM prompt)

        Returns:
            {
                \"positive_singles\": [...],
                \"negative_singles\": [...],
                \"positive_pairs\": [...],
                \"negative_pairs\": [...],
                \"observing_singles\": [...],
                \"active_patterns\": [...],
                \"retired_patterns\": [...],
                \"summary\": {...},
            }
        """
        single_stats = self.db.get_factor_stats(level="single")
        pair_stats = self.db.get_factor_stats(level="pair")

        positive_singles = [s for s in single_stats if s.get("verdict") == "positive"]
        negative_singles = [s for s in single_stats if s.get("verdict") == "negative"]
        observing_singles = [s for s in single_stats if s.get("verdict") == "observing"]

        positive_pairs = [s for s in pair_stats if s.get("verdict") == "positive"]
        negative_pairs = [s for s in pair_stats if s.get("verdict") == "negative"]

        active_patterns = self.db.get_patterns(status="active")
        retired_patterns = self.db.get_patterns(status="retired")

        # 排序: 按 total_trades 降序
        for lst in [positive_singles, negative_singles, positive_pairs, negative_pairs]:
            lst.sort(key=lambda x: x.get("total_trades", 0), reverse=True)

        # 汇总
        total_single_trades = sum(s.get("total_trades", 0) for s in single_stats)
        total_pair_trades = sum(s.get("total_trades", 0) for s in pair_stats)

        return {
            "positive_singles": positive_singles,
            "negative_singles": negative_singles,
            "observing_singles": observing_singles,
            "positive_pairs": positive_pairs,
            "negative_pairs": negative_pairs,
            "active_patterns": active_patterns,
            "retired_patterns": retired_patterns,
            "summary": {
                "total_factors_analyzed": len(single_stats) + len(pair_stats),
                "total_single_trades": total_single_trades,
                "total_pair_trades": total_pair_trades,
                "positive_count": len(positive_singles) + len(positive_pairs),
                "negative_count": len(negative_singles) + len(negative_pairs),
                "observing_count": len(observing_singles),
            },
        }

    def get_patterns_for_prompt(self) -> Tuple[str, str]:
        """
        获取用于 LLM prompt 的 pattern 文本

        Returns:
            (active_patterns_text, retired_patterns_text)
        """
        report = self.generate_factor_report()

        active_lines = []
        for f in report["positive_singles"][:5]:
            active_lines.append(
                f"  - [single] {f['factor_key']}: "
                f"贝叶斯胜率{f.get('bayesian_win_rate', 0):.0%} "
                f"(CI {f.get('bayesian_lower', 0):.0%}-{f.get('bayesian_upper', 0):.0%}), "
                f"平均${f.get('avg_pnl', 0):+.0f}, {f.get('total_trades', 0)}笔"
            )
        for f in report["positive_pairs"][:5]:
            active_lines.append(
                f"  - [pair] {f['factor_key']}: "
                f"贝叶斯胜率{f.get('bayesian_win_rate', 0):.0%}, "
                f"平均${f.get('avg_pnl', 0):+.0f}, {f.get('total_trades', 0)}笔"
            )

        retired_lines = []
        for f in report["negative_singles"][:5]:
            retired_lines.append(
                f"  - [single] {f['factor_key']}: "
                f"胜率{f.get('bayesian_win_rate', 0):.0%}, "
                f"平均${f.get('avg_pnl', 0):+.0f}"
            )
        for f in report["negative_pairs"][:5]:
            retired_lines.append(
                f"  - [pair] {f['factor_key']}: "
                f"胜率{f.get('bayesian_win_rate', 0):.0%}, "
                f"平均${f.get('avg_pnl', 0):+.0f}"
            )

        active_text = "\n".join(active_lines) if active_lines else "  (暂无足够数据)"
        retired_text = "\n".join(retired_lines) if retired_lines else "  (暂无)"

        return active_text, retired_text

    @staticmethod
    def _parse_conditions(val) -> List[str]:
        """解析 conditions_json"""
        if not val:
            return []
        if isinstance(val, list):
            return val
        try:
            return json.loads(val)
        except (ValueError, TypeError):
            return []
