"""
反馈学习模块 — 从用户反馈中提取信号并调整 Screener/Generator 参数

设计原则:
  1. 被动学习: 只在周度复盘中分析历史反馈
  2. 信号提取: 识别"哪些条件组合导致用户跳过"和"用户倾向于修改哪些参数"
  3. 参数调整: 输出建议的 Screener 权重调整和 Generator 参数调整

Usage:
  learner = FeedbackLearner(db)
  report = learner.learn_from_feedback(days=7)
  # report = {
  #   "skip_patterns": [{"conditions": [...], "count": 5, "suggestion": "降权"}],
  #   "modify_patterns": [{"field": "stop_loss", "direction": "widen", "count": 3}],
  #   "screener_adjustments": {"insider_cluster_buy": +0.5, "rsi_oversold": -0.3},
  #   "generator_adjustments": {"default_rr_ratio": 2.5},
  # }
"""
from __future__ import annotations

import logging
from typing import Dict, List, Any
from collections import defaultdict

logger = logging.getLogger(__name__)


class FeedbackLearner:
    """从用户反馈中学习"""

    def __init__(self, playbook_db):
        self.db = playbook_db

    def learn_from_feedback(self, days: int = 7) -> Dict[str, Any]:
        """
        分析最近 N 天的用户反馈，提取学习信号

        Args:
            days: 回溯天数

        Returns:
            学习报告 dict
        """
        try:
            feedbacks = self.db.get_recent_feedback(days=days)
        except Exception as e:
            logger.warning(f"获取反馈失败: {e}")
            return {"error": str(e), "feedback_count": 0}

        if not feedbacks:
            return {
                "feedback_count": 0,
                "skip_patterns": [],
                "modify_patterns": [],
                "screener_adjustments": {},
                "generator_adjustments": {},
            }

        # 1. 分析跳过模式
        skip_patterns = self._analyze_skip_patterns(feedbacks)

        # 2. 分析修改模式
        modify_patterns = self._analyze_modify_patterns(feedbacks)

        # 3. 生成 Screener 权重调整建议
        screener_adjustments = self._suggest_screener_adjustments(skip_patterns)

        # 4. 生成 Generator 参数调整建议
        generator_adjustments = self._suggest_generator_adjustments(modify_patterns)

        return {
            "feedback_count": len(feedbacks),
            "skip_patterns": skip_patterns[:10],  # Top 10
            "modify_patterns": modify_patterns[:10],
            "screener_adjustments": screener_adjustments,
            "generator_adjustments": generator_adjustments,
        }

    def _analyze_skip_patterns(self, feedbacks: List[Dict]) -> List[Dict]:
        """分析哪些条件组合导致用户跳过"""
        skip_count = defaultdict(lambda: {"count": 0, "reasons": defaultdict(int)})

        for fb in feedbacks:
            if fb.get("action") != "skipped":
                continue

            inst = self.db.get_instruction_by_id(fb.get("instruction_id"))
            if not inst:
                continue

            # 提取触发条件
            conditions = inst.get("triggered_factors", [])
            if not conditions:
                continue

            # 按条件组合聚合
            cond_key = tuple(sorted(conditions))
            skip_count[cond_key]["count"] += 1
            skip_count[cond_key]["reasons"][fb.get("skip_reason", "unknown")] += 1

        # 排序并生成建议
        patterns = []
        for cond_key, data in skip_count.items():
            if data["count"] < 2:  # 至少 2 次才纳入
                continue

            # 找出最常见的跳过原因
            top_reason = max(data["reasons"].items(), key=lambda x: x[1])[0]

            patterns.append({
                "conditions": list(cond_key),
                "count": data["count"],
                "top_reason": top_reason,
                "suggestion": "降权" if data["count"] >= 3 else "观察",
            })

        # 按跳过次数排序
        patterns.sort(key=lambda x: x["count"], reverse=True)
        return patterns

    def _analyze_modify_patterns(self, feedbacks: List[Dict]) -> List[Dict]:
        """分析用户倾向于修改哪些参数"""
        modify_count = defaultdict(lambda: {"count": 0, "directions": defaultdict(int)})

        for fb in feedbacks:
            if fb.get("action") != "modified":
                continue

            field = fb.get("modified_field")
            if not field:
                continue

            modify_count[field]["count"] += 1

            # 判断修改方向 (仅对数值字段)
            if field in ("stop_loss", "take_profit", "position_size"):
                original = fb.get("original_value")
                modified = fb.get("modified_value")
                if original and modified:
                    try:
                        orig_val = float(original)
                        mod_val = float(modified)
                        if field == "stop_loss":
                            # stop_loss 是负数，绝对值变大 = 放宽
                            direction = "widen" if abs(mod_val) > abs(orig_val) else "tighten"
                        elif field == "take_profit":
                            direction = "widen" if mod_val > orig_val else "tighten"
                        else:  # position_size
                            direction = "increase" if mod_val > orig_val else "decrease"
                        modify_count[field]["directions"][direction] += 1
                    except (ValueError, TypeError) as _me:
                        # R5 同款纪律 (2026-08-31 DEB-20260831 P1③): 反馈学习链异常留痕 —
                        # 吞错=用户修改样本无声消失, 学习环效力测量 (L3) 永远攒不起料。
                        logger.warning(f"[feedback_learner] 修改样本解析失败: field={field} "
                                       f"original={original!r} modified={modified!r} err={_me}")

        patterns = []
        for field, data in modify_count.items():
            if data["count"] < 2:
                continue

            # 找出最常见的修改方向
            top_direction = None
            if data["directions"]:
                top_direction = max(data["directions"].items(), key=lambda x: x[1])[0]

            patterns.append({
                "field": field,
                "count": data["count"],
                "direction": top_direction,
                "suggestion": f"默认{field}可能需要{'放宽' if top_direction == 'widen' else '收紧' if top_direction == 'tighten' else '调整'}"
            })

        patterns.sort(key=lambda x: x["count"], reverse=True)
        return patterns

    def _suggest_screener_adjustments(self, skip_patterns: List[Dict]) -> Dict[str, float]:
        """
        根据跳过模式建议 Screener 权重调整

        逻辑:
          - 某个条件被跳过 >= 3 次 → 建议降权 -0.3
          - 某个条件被跳过 >= 5 次 → 建议降权 -0.5
        """
        adjustments = {}
        factor_skip_count = defaultdict(int)

        for pattern in skip_patterns:
            for cond in pattern["conditions"]:
                factor_skip_count[cond] += pattern["count"]

        for factor, count in factor_skip_count.items():
            if count >= 5:
                adjustments[factor] = -0.5
            elif count >= 3:
                adjustments[factor] = -0.3

        return adjustments

    def _suggest_generator_adjustments(self, modify_patterns: List[Dict]) -> Dict[str, Any]:
        """
        根据修改模式建议 Generator 参数调整

        逻辑:
          - stop_loss 被放宽 >= 3 次 → 建议 default_atr_multiplier 从 1.5 增加到 1.8
          - take_profit 被放宽 >= 3 次 → 建议 default_rr_ratio 从 2.0 增加到 2.5
          - position_size 被减小 >= 3 次 → 建议 risk_pct 从 1.0% 减小到 0.8%
        """
        adjustments = {}

        for pattern in modify_patterns:
            field = pattern["field"]
            direction = pattern["direction"]
            count = pattern["count"]

            if field == "stop_loss" and direction == "widen" and count >= 3:
                adjustments["default_atr_multiplier"] = 1.8
            elif field == "take_profit" and direction == "widen" and count >= 3:
                adjustments["default_rr_ratio"] = 2.5
            elif field == "position_size" and direction == "decrease" and count >= 3:
                adjustments["risk_pct"] = 0.8

        return adjustments

    def format_learning_report(self, report: Dict[str, Any]) -> str:
        """格式化学习报告为 Markdown"""
        lines = ["## 🧠 反馈学习报告", ""]

        # 反馈统计
        lines.append(f"**反馈总数**: {report.get('feedback_count', 0)} 条")
        lines.append("")

        # 跳过模式
        skip_patterns = report.get("skip_patterns", [])
        if skip_patterns:
            lines.append("### ⛔ 跳过模式 (Top 5)")
            for p in skip_patterns[:5]:
                conds = ", ".join(p["conditions"])
                lines.append(f"- **{conds}** — 跳过 {p['count']} 次")
                lines.append(f"  - 主要原因: {p['top_reason']}")
                lines.append(f"  - 建议: {p['suggestion']}")
            lines.append("")

        # 修改模式
        modify_patterns = report.get("modify_patterns", [])
        if modify_patterns:
            lines.append("### ✏️ 修改模式 (Top 5)")
            for p in modify_patterns[:5]:
                lines.append(f"- **{p['field']}** — 修改 {p['count']} 次")
                if p.get("direction"):
                    lines.append(f"  - 方向: {p['direction']}")
                lines.append(f"  - 建议: {p['suggestion']}")
            lines.append("")

        # Screener 调整建议
        screener_adj = report.get("screener_adjustments", {})
        if screener_adj:
            lines.append("### 🎯 Screener 权重调整建议")
            for factor, delta in screener_adj.items():
                emoji = "📉" if delta < 0 else "📈"
                lines.append(f"- {emoji} **{factor}**: {delta:+.1f}")
            lines.append("")

        # Generator 调整建议
        gen_adj = report.get("generator_adjustments", {})
        if gen_adj:
            lines.append("### ⚙️ Generator 参数调整建议")
            for param, value in gen_adj.items():
                lines.append(f"- **{param}**: {value}")
            lines.append("")

        # 总结
        if not skip_patterns and not modify_patterns:
            lines.append("💤 **暂无足够反馈数据**")
        else:
            lines.append("---")
            lines.append("💡 **建议**: 在周度复盘中审查上述建议，手动确认后更新配置")

        return "\n".join(lines)
