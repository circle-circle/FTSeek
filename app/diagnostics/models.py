"""
诊断模块数据契约 — Frozen Dataclass

借鉴 Vibe-Trading 的 models.py 设计:
  - 全部 frozen=True, 防止运行时意外修改
  - 实现 __post_init__ 验证
  - 实现 to_dict() 序列化

零侵入: 不修改任何现有模型, 独立命名空间
"""

from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple


@dataclass(frozen=True)
class ShadowRule:
    """
    从交易记录中提取的隐含策略规则

    借鉴 Vibe-Trading 的 ShadowRule:
      - entry_condition: KMeans 聚类的 entry_hour / entry_weekday 分位数范围
      - exit_condition: holding_days 分位数范围
      - human_text: 自然语言规则描述
    """
    rule_id: str                         # "R1", "R2"
    human_text: str                      # "持有 2-5 天，早盘 9:30-10:30 入场"
    entry_hour_range: Tuple[float, float]  # (min, max) 入场小时范围
    holding_days_range: Tuple[float, float]  # (min, max) 持仓天数范围
    support_count: int                   # 支持该规则的 profitable roundtrip 数量
    coverage_rate: float                 # 覆盖率 (0-1)
    avg_pnl_pct: float = 0.0            # 该规则下交易的平均盈亏%
    weight: float = 1.0                  # 规则权重

    def __post_init__(self):
        if not self.rule_id:
            raise ValueError("rule_id 不能为空")
        if self.coverage_rate < 0 or self.coverage_rate > 1:
            raise ValueError(f"coverage_rate 必须在 0-1 之间: {self.coverage_rate}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "human_text": self.human_text,
            "entry_hour_range": list(self.entry_hour_range),
            "holding_days_range": list(self.holding_days_range),
            "support_count": self.support_count,
            "coverage_rate": round(self.coverage_rate, 3),
            "avg_pnl_pct": round(self.avg_pnl_pct, 4),
            "weight": self.weight,
        }


@dataclass(frozen=True)
class ClusterResult:
    """KMeans 聚类结果"""
    n_clusters: int
    silhouette_score: float              # 轮廓系数 (-1 到 1)
    labels: List[int]                    # 每个交易的聚类标签
    cluster_sizes: Dict[int, int]        # 每个聚类的大小
    cluster_avg_pnl: Dict[int, float]    # 每个聚类的平均盈亏

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n_clusters": self.n_clusters,
            "silhouette_score": round(self.silhouette_score, 4),
            "cluster_sizes": self.cluster_sizes,
            "cluster_avg_pnl": {k: round(v, 4) for k, v in self.cluster_avg_pnl.items()},
        }


@dataclass(frozen=True)
class AttributionBreakdown:
    """
    五因子归因分解 (借鉴 Vibe-Trading backtester.py 行 394-475)

    分解公式:
      shadow_pnl - real_pnl = noise + early_exit + late_exit + overtrading + missed_signals

    每个因子的符号: 正号 = Shadow 策略能赚更多 (用户行为偏离规则导致亏损)
    """
    noise_trades_pnl: float              # 不满足规则的交易造成的亏损 (负值=用户亏了)
    early_exit_pnl: float                # 过早止盈的潜在收益损失 (正数=用户少赚了)
    late_exit_pnl: float                 # 延迟止损的超额亏损 (正数=用户多亏了)
    overtrading_pnl: float               # 过度交易的亏损 (负值=用户亏了)
    missed_signals_pnl: float            # 残差: 应该做但没做的交易

    @property
    def total_delta_pnl(self) -> float:
        """总偏差 PnL"""
        return (self.noise_trades_pnl + self.early_exit_pnl +
                self.late_exit_pnl + self.overtrading_pnl +
                self.missed_signals_pnl)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "noise_trades_pnl": round(self.noise_trades_pnl, 2),
            "early_exit_pnl": round(self.early_exit_pnl, 2),
            "late_exit_pnl": round(self.late_exit_pnl, 2),
            "overtrading_pnl": round(self.overtrading_pnl, 2),
            "missed_signals_pnl": round(self.missed_signals_pnl, 2),
            "total_delta_pnl": round(self.total_delta_pnl, 2),
        }

    def get_top_impact_factors(self, n: int = 3) -> List[Tuple[str, float]]:
        """获取影响最大的 N 个因子"""
        factors = [
            ("noise_trades", self.noise_trades_pnl),
            ("early_exit", self.early_exit_pnl),
            ("late_exit", self.late_exit_pnl),
            ("overtrading", self.overtrading_pnl),
            ("missed_signals", self.missed_signals_pnl),
        ]
        factors.sort(key=lambda x: abs(x[1]), reverse=True)
        return factors[:n]


@dataclass(frozen=True)
class WalkForwardWindow:
    """Walk-Forward 分析的单窗口结果"""
    window_id: int
    start_date: str
    end_date: str
    n_trades: int
    total_pnl: float
    win_rate: float
    sharpe_ratio: float
    max_drawdown: float
    avg_hold_days: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "window_id": self.window_id,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "n_trades": self.n_trades,
            "total_pnl": round(self.total_pnl, 2),
            "win_rate": round(self.win_rate, 3),
            "sharpe_ratio": round(self.sharpe_ratio, 3),
            "max_drawdown": round(self.max_drawdown, 4),
            "avg_hold_days": round(self.avg_hold_days, 1),
        }


@dataclass(frozen=True)
class WalkForwardResult:
    """Walk-Forward 分析完整结果"""
    n_windows: int
    windows: List[WalkForwardWindow]
    consistency_rate: float              # 盈利窗口比例
    sharpe_mean: float
    sharpe_std: float
    sharpe_cv: float                     # 变异系数 = std / mean
    return_mean: float
    return_std: float
    stability_grade: str                 # "STABLE" / "MODERATE" / "UNSTABLE"

    @property
    def is_stable(self) -> bool:
        return self.stability_grade == "STABLE"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n_windows": self.n_windows,
            "consistency_rate": round(self.consistency_rate, 3),
            "sharpe_mean": round(self.sharpe_mean, 3),
            "sharpe_std": round(self.sharpe_std, 3),
            "sharpe_cv": round(self.sharpe_cv, 3),
            "return_mean": round(self.return_mean, 4),
            "return_std": round(self.return_std, 4),
            "stability_grade": self.stability_grade,
            "windows": [w.to_dict() for w in self.windows],
        }


@dataclass(frozen=True)
class FactorExposure:
    """因子暴露分析结果"""
    factor_name: str
    coefficient: float                   # OLS 回归系数
    t_statistic: float                   # t 统计量
    p_value: float                       # p 值
    is_significant: bool                 # p < 0.05

    def to_dict(self) -> Dict[str, Any]:
        return {
            "factor_name": self.factor_name,
            "coefficient": round(self.coefficient, 6),
            "t_statistic": round(self.t_statistic, 3),
            "p_value": round(self.p_value, 4),
            "is_significant": self.is_significant,
        }
