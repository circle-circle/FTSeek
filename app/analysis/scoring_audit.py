"""
信号评分函数审计工具
分析 strength 与 T+1 前瞻收益的关系，定位反向因子

Task #4 (2026-06-17): 信号评分函数审计与修复
- 按 strength 分桶计算 T+1 胜率和平均收益
- 逐因子计算与 T+1 收益的 IC
- Forward Walk 验证框架
- 生成权重修复建议
"""
import logging
import math
from typing import Dict, List, Optional, Tuple
from scipy.stats import spearmanr

logger = logging.getLogger(__name__)


class ScoringAudit:
    """
    评分函数审计与诊断

    功能：
    1. 按 strength 分桶计算 T+1 胜率和平均收益
    2. 逐因子计算与 T+1 收益的 IC
    3. Forward Walk 验证
    4. 生成审计报告
    """

    def __init__(self, db_path: str = None):
        """
        Args:
            db_path: signal_snapshots 数据库路径，None 则使用默认
        """
        self._db_path = db_path or self._get_default_db_path()

    def _get_default_db_path(self) -> str:
        """获取默认数据库路径"""
        import os
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return os.path.join(base, "data", "signal_snapshots.db")

    def audit_strength_vs_return(self, days_back: int = 30) -> Dict:
        """
        按 strength 分桶计算 T+1 胜率

        分桶: 0-30, 30-50, 50-70, 70-100

        Returns:
            {
                "buckets": [
                    {"range": "0-30", "count": N, "win_rate": 0.xx, "avg_return": 0.xx},
                    ...
                ],
                "overall_ic": float,
                "overall_ic_pval": float,
                "is_monotonic": bool,  # 胜率是否单调递增
                "diagnosis": str,      # 诊断结论
            }
        """
        import sqlite3
        from datetime import datetime, timedelta

        cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row

        try:
            rows = conn.execute("""
                SELECT strength, fwd_ret_t1
                FROM signal_snapshots
                WHERE created_at >= ?
                  AND fwd_ret_t1 IS NOT NULL
                  AND COALESCE(trade_mode, 'live') = 'live'
            """, (cutoff,)).fetchall()
        finally:
            conn.close()

        if not rows:
            return {"status": "no_data", "diagnosis": "无有效数据"}

        # 分桶
        buckets_def = [(0, 30), (30, 50), (50, 70), (70, 100)]
        buckets_result = []

        for low, high in buckets_def:
            bucket_rows = [(r["strength"], r["fwd_ret_t1"]) for r in rows
                          if r["strength"] is not None and r["fwd_ret_t1"] is not None
                          and low <= r["strength"] < high]

            if bucket_rows:
                strengths_b, returns_b = zip(*bucket_rows)
                wins = sum(1 for r in returns_b if r > 0)
                win_rate = wins / len(returns_b)
                avg_ret = sum(returns_b) / len(returns_b)
            else:
                win_rate = 0
                avg_ret = 0

            buckets_result.append({
                "range": f"{low}-{high}",
                "count": len(bucket_rows),
                "win_rate": round(win_rate, 4),
                "avg_return": round(avg_ret, 6),
            })

        # 整体 IC
        valid_pairs = [(r["strength"], r["fwd_ret_t1"]) for r in rows
                      if r["strength"] is not None and r["fwd_ret_t1"] is not None]

        if len(valid_pairs) >= 10:
            all_strengths, all_returns = zip(*valid_pairs)
            ic, pval = spearmanr(all_strengths, all_returns)
        else:
            ic, pval = 0.0, 1.0

        # 单调性检查
        win_rates = [b["win_rate"] for b in buckets_result if b["count"] > 0]
        is_monotonic = (
            all(win_rates[i] <= win_rates[i + 1] for i in range(len(win_rates) - 1))
            if len(win_rates) > 1 else False
        )

        # 诊断
        if ic < -0.02 and not is_monotonic:
            diagnosis = "CRITICAL: 评分与收益负相关，高分信号表现更差，评分函数可能反向"
        elif ic < 0.02:
            diagnosis = "WARNING: 评分与收益无显著相关性，评分函数无效"
        elif is_monotonic:
            diagnosis = "OK: 评分与收益正相关且单调"
        else:
            diagnosis = "PARTIAL: IC为正但分桶胜率非单调，部分因子可能有问题"

        return {
            "buckets": buckets_result,
            "overall_ic": round(ic, 4) if not math.isnan(ic) else 0.0,
            "overall_ic_pval": round(pval, 4) if not math.isnan(pval) else 1.0,
            "is_monotonic": is_monotonic,
            "diagnosis": diagnosis,
            "total_signals": len(valid_pairs),
        }

    def audit_factor_ic(self, days_back: int = 30) -> List[Dict]:
        """
        逐因子计算与 T+1 收益的 IC

        需要从 score_breakdown 中提取各因子的贡献值

        Returns:
            [{"factor": "trend_confirm", "ic": 0.12, "pval": 0.03, "direction": "correct"}, ...]
        """
        import sqlite3
        import json
        from datetime import datetime, timedelta

        cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row

        try:
            rows = conn.execute("""
                SELECT score_breakdown, fwd_ret_t1
                FROM signal_snapshots
                WHERE created_at >= ?
                  AND fwd_ret_t1 IS NOT NULL
                  AND score_breakdown IS NOT NULL
                  AND COALESCE(trade_mode, 'live') = 'live'
            """, (cutoff,)).fetchall()
        finally:
            conn.close()

        if len(rows) < 20:
            return [{"status": "insufficient_data", "count": len(rows)}]

        # 收集各因子值
        factor_values: Dict[str, List[Tuple[float, float]]] = {}

        for row in rows:
            try:
                breakdown = (
                    json.loads(row["score_breakdown"])
                    if isinstance(row["score_breakdown"], str)
                    else row["score_breakdown"]
                )
                fwd_ret = float(row["fwd_ret_t1"])

                if isinstance(breakdown, dict):
                    for factor_name, factor_val in breakdown.items():
                        if factor_val is not None:
                            try:
                                fv = float(factor_val)
                                if not (math.isnan(fv) or math.isinf(fv)):
                                    if factor_name not in factor_values:
                                        factor_values[factor_name] = []
                                    factor_values[factor_name].append((fv, fwd_ret))
                            except (TypeError, ValueError):
                                continue
            except (json.JSONDecodeError, TypeError):
                continue

        # 计算各因子 IC
        results = []
        for factor_name, pairs in factor_values.items():
            if len(pairs) < 15:
                continue
            factor_scores, fwd_rets = zip(*pairs)
            ic, pval = spearmanr(factor_scores, fwd_rets)

            if math.isnan(ic):
                continue

            direction = "correct" if ic > 0 else "inverted" if ic < -0.05 else "neutral"

            results.append({
                "factor": factor_name,
                "ic": round(ic, 4),
                "pval": round(pval, 4),
                "count": len(pairs),
                "direction": direction,
                "recommendation": "keep" if ic > 0 else "invert" if ic < -0.05 else "zero_weight",
            })

        # 按 IC 绝对值排序
        results.sort(key=lambda x: abs(x.get("ic", 0)), reverse=True)
        return results

    def generate_weight_fix_recommendation(self, days_back: int = 30) -> Dict:
        """
        基于因子 IC 审计结果，生成权重修复建议

        Returns:
            {
                "factor_analysis": [...],
                "recommended_changes": [...],
                "total_factors_analyzed": int,
                "factors_to_fix": int,
            }
        """
        factor_results = self.audit_factor_ic(days_back)

        if not factor_results or (len(factor_results) == 1 and "status" in factor_results[0]):
            return {"status": "insufficient_data"}

        changes = []
        for factor in factor_results:
            if factor.get("recommendation") == "invert":
                changes.append({
                    "factor": factor["factor"],
                    "action": "invert_sign",
                    "ic": factor["ic"],
                    "pval": factor["pval"],
                    "reason": f"IC={factor['ic']:.3f} (负相关), p={factor['pval']:.3f}",
                })
            elif factor.get("recommendation") == "zero_weight":
                changes.append({
                    "factor": factor["factor"],
                    "action": "set_zero",
                    "ic": factor["ic"],
                    "pval": factor["pval"],
                    "reason": f"IC={factor['ic']:.3f} (无效), p={factor['pval']:.3f}",
                })

        return {
            "factor_analysis": factor_results,
            "recommended_changes": changes,
            "total_factors_analyzed": len(factor_results),
            "factors_to_fix": len(changes),
        }

    def forward_walk_validation(self, train_days: int = 30, test_days: int = 7) -> Dict:
        """
        Blind Forward Walk 验证
        用前 train_days 数据"训练"（分析），在后 test_days 数据上验证 IC

        Returns:
            {
                "train_ic": float,
                "test_ic": float,
                "test_ic_pval": float,
                "is_valid": bool,  # test_ic > 0
                "diagnosis": str,
            }
        """
        import sqlite3
        from datetime import datetime, timedelta

        now = datetime.now()
        test_start = (now - timedelta(days=test_days)).strftime("%Y-%m-%d")
        train_start = (now - timedelta(days=train_days + test_days)).strftime("%Y-%m-%d")

        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row

        try:
            # 训练集
            train_rows = conn.execute("""
                SELECT strength, fwd_ret_t1 FROM signal_snapshots
                WHERE created_at >= ? AND created_at < ?
                  AND fwd_ret_t1 IS NOT NULL
                  AND COALESCE(trade_mode, 'live') = 'live'
            """, (train_start, test_start)).fetchall()

            # 测试集
            test_rows = conn.execute("""
                SELECT strength, fwd_ret_t1 FROM signal_snapshots
                WHERE created_at >= ?
                  AND fwd_ret_t1 IS NOT NULL
                  AND COALESCE(trade_mode, 'live') = 'live'
            """, (test_start,)).fetchall()
        finally:
            conn.close()

        def calc_ic(rows):
            valid = [(r["strength"], r["fwd_ret_t1"]) for r in rows
                    if r["strength"] is not None and r["fwd_ret_t1"] is not None]
            if len(valid) < 10:
                return 0.0, 1.0, len(valid)
            s, r = zip(*valid)
            ic, pval = spearmanr(s, r)
            return (
                ic if not math.isnan(ic) else 0.0,
                pval if not math.isnan(pval) else 1.0,
                len(valid),
            )

        train_ic, train_pval, train_n = calc_ic(train_rows)
        test_ic, test_pval, test_n = calc_ic(test_rows)

        is_valid = test_ic > 0

        if test_n < 10:
            diagnosis = f"测试集样本不足({test_n}条)，无法验证"
        elif is_valid and test_pval < 0.10:
            diagnosis = f"PASS: 前向IC={test_ic:.3f}为正且显著(p={test_pval:.3f})"
        elif is_valid:
            diagnosis = f"MARGINAL: 前向IC={test_ic:.3f}为正但不显著(p={test_pval:.3f})"
        else:
            diagnosis = f"FAIL: 前向IC={test_ic:.3f}为负，评分修复无效"

        return {
            "train_ic": round(train_ic, 4),
            "train_pval": round(train_pval, 4),
            "train_n": train_n,
            "test_ic": round(test_ic, 4),
            "test_pval": round(test_pval, 4),
            "test_n": test_n,
            "is_valid": is_valid,
            "diagnosis": diagnosis,
        }

    def full_audit_report(self, days_back: int = 30) -> Dict:
        """
        生成完整审计报告（聚合所有审计结果）

        Returns:
            {
                "strength_vs_return": {...},
                "factor_ic": [...],
                "weight_recommendation": {...},
                "forward_walk": {...},
                "summary": str,
            }
        """
        strength_report = self.audit_strength_vs_return(days_back)
        factor_report = self.audit_factor_ic(days_back)
        weight_report = self.generate_weight_fix_recommendation(days_back)
        forward_walk = self.forward_walk_validation(train_days=days_back, test_days=7)

        # 汇总诊断
        issues = []
        if strength_report.get("overall_ic", 0) < -0.02:
            issues.append("整体IC为负，评分函数反向")
        if not strength_report.get("is_monotonic", False):
            issues.append("分桶胜率非单调")
        if weight_report.get("factors_to_fix", 0) > 0:
            issues.append(f"{weight_report['factors_to_fix']}个因子需要修复")
        if not forward_walk.get("is_valid", False):
            issues.append("Forward Walk验证失败")

        if issues:
            summary = f"发现 {len(issues)} 个问题: " + "; ".join(issues)
        else:
            summary = "评分函数运行正常，无需修复"

        return {
            "strength_vs_return": strength_report,
            "factor_ic": factor_report,
            "weight_recommendation": weight_report,
            "forward_walk": forward_walk,
            "summary": summary,
        }
