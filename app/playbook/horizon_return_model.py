"""
Playbook Horizon Return Model — 历史收益估计

基于历史 playbook_instructions 的 outcome，
为每个 feature_key + horizon 组合估计期望收益和胜率，
供 HorizonSelector 做短线/长线决策。
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

from app.playbook.db import PlaybookDB

logger = logging.getLogger(__name__)


# 复用 factor_analyzer 的贝叶斯估计
from app.playbook.factor_analyzer import bayesian_binomial


@dataclass
class HorizonStats:
    """单个 feature_key + horizon 的统计"""
    feature_key: str
    horizon: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_return: float = 0.0
    avg_pnl: float = 0.0
    profit_factor: float = 0.0
    bayesian_win_rate: float = 0.5
    bayesian_lower: float = 0.0
    bayesian_upper: float = 1.0


class HorizonReturnModel:
    """
    Horizon 历史收益模型。

    职责:
      1. 从历史 outcome 学习不同 feature_key 在 short/long 下的表现
      2. 为 HorizonSelector 提供期望收益估计
      3. 每日/每周增量更新
    """

    def __init__(self, db: PlaybookDB = None):
        self.db = db or PlaybookDB()
        self.db.ensure_tables()

    def update(self, days_back: int = 90) -> Dict[str, int]:
        """
        基于最近 N 天的历史 outcome 更新 horizon 收益模型。

        Returns:
            {"updated": 更新组合数, "deleted": 0}
        """
        results = self.db.get_instructions_with_outcome(days_back=days_back)
        # 只保留真实交易结果，排除 missed / void / skipped 等伪结果
        results = [r for r in results if r.get("outcome") in ("win", "loss")]
        if not results:
            logger.info("[HorizonReturnModel] 无历史 outcome 数据")
            return {"updated": 0}

        # 按 (feature_key, horizon) 聚合
        grouped = self._group_by_feature_horizon(results)

        updated = 0
        for (feature_key, horizon), trades in grouped.items():
            if len(trades) < 3:
                # 样本太少，跳过（保留旧数据或等待积累）
                continue

            wins = sum(1 for t in trades if t["outcome"] == "win")
            losses = sum(1 for t in trades if t["outcome"] == "loss")
            total = len(trades)
            win_rate = wins / total if total > 0 else 0.0

            returns = [t["pnl_pct"] for t in trades if t.get("pnl_pct") is not None]
            avg_return = sum(returns) / len(returns) if returns else 0.0
            avg_pnl = sum(t["pnl"] for t in trades if t.get("pnl")) / len(trades) if trades else 0.0

            gross_profit = sum(t["pnl"] for t in trades if t.get("pnl", 0) > 0)
            gross_loss = abs(sum(t["pnl"] for t in trades if t.get("pnl", 0) < 0))
            profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0

            bayesian = bayesian_binomial(wins, total)

            self.db.save_horizon_stat({
                "feature_key": feature_key,
                "horizon": horizon,
                "total_trades": total,
                "wins": wins,
                "losses": losses,
                "win_rate": round(win_rate, 4),
                "avg_return": round(avg_return, 6),
                "avg_pnl": round(avg_pnl, 2),
                "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else 999.0,
                "bayesian_win_rate": bayesian.mean,
                "bayesian_lower": bayesian.lower_95,
                "bayesian_upper": bayesian.upper_95,
            })
            updated += 1

        logger.info(f"[HorizonReturnModel] 更新了 {updated} 个 feature+horizon 组合")
        return {"updated": updated}

    def _group_by_feature_horizon(self, results: List[Dict]) -> Dict[tuple, List[Dict]]:
        """
        把历史指令按 feature_key + horizon 分组。

        生成多层特征 key:
          - 单因子: "{factor}:short"
          - 因子对: "{factor1}+{factor2}:long"
          - 市场环境: "{factor}:vix_{state}:regime_{regime}:long"
          - 标的状态: "{factor}:trend_{state}:vol_{state}:short"
        """
        grouped = defaultdict(list)

        for r in results:
            horizon = r.get("horizon") or "short"
            conditions = self._parse_conditions(r.get("conditions_json"))
            if not conditions:
                continue

            # 单因子 key
            for factor in conditions:
                key = f"{factor}:{horizon}"
                grouped[(key, horizon)].append(r)

            # 因子对 key
            if len(conditions) >= 2:
                sorted_conds = sorted(conditions)
                for i in range(len(sorted_conds)):
                    for j in range(i + 1, len(sorted_conds)):
                        key = f"{sorted_conds[i]}+{sorted_conds[j]}:{horizon}"
                        grouped[(key, horizon)].append(r)

            # 市场环境 key
            market_regime = r.get("market_regime", "UNKNOWN")
            vix = r.get("vix_at_generation", 20.0) or 20.0
            vix_state = self._vix_state(vix)
            for factor in conditions:
                key = f"{factor}:vix_{vix_state}:regime_{market_regime}:{horizon}"
                grouped[(key, horizon)].append(r)

            # 标的状态 key (简化，用已有字段推导)
            trend_state = self._infer_trend_state(r)
            vol_state = self._infer_vol_state(r)
            for factor in conditions:
                key = f"{factor}:trend_{trend_state}:vol_{vol_state}:{horizon}"
                grouped[(key, horizon)].append(r)

        return grouped

    @staticmethod
    def _vix_state(vix: float) -> str:
        if vix < 18:
            return "low"
        elif vix < 22:
            return "normal"
        elif vix < 28:
            return "high"
        return "extreme"

    @staticmethod
    def _infer_trend_state(result: Dict) -> str:
        """从历史记录推断趋势状态 (简化版)"""
        spy_trend = result.get("spy_trend_at_generation", "")
        if spy_trend == "above_ma20":
            return "up"
        elif spy_trend == "below_ma20":
            return "down"
        return "neutral"

    @staticmethod
    def _infer_vol_state(result: Dict) -> str:
        """从历史记录推断波动状态 (简化版)"""
        vix = result.get("vix_at_generation", 20.0) or 20.0
        if vix < 20:
            return "low"
        elif vix < 25:
            return "medium"
        return "high"

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

    def get(self, feature_key: str, horizon: str) -> Optional[HorizonStats]:
        """查询单个 feature_key + horizon 的统计"""
        rows = self.db.get_horizon_stats(feature_key=feature_key, horizon=horizon)
        if not rows:
            return None
        row = rows[0]
        return HorizonStats(
            feature_key=row["feature_key"],
            horizon=row["horizon"],
            total_trades=row["total_trades"],
            wins=row["wins"],
            losses=row["losses"],
            win_rate=row["win_rate"],
            avg_return=row["avg_return"],
            avg_pnl=row["avg_pnl"],
            profit_factor=row["profit_factor"],
            bayesian_win_rate=row["bayesian_win_rate"],
            bayesian_lower=row["bayesian_lower"],
            bayesian_upper=row["bayesian_upper"],
        )

    def batch_get(self, feature_keys: List[str], horizon: str) -> Dict[str, HorizonStats]:
        """批量查询多个 feature_key 在指定 horizon 下的统计"""
        result = {}
        for key in feature_keys:
            stat = self.get(key, horizon)
            if stat:
                result[key] = stat
        return result
