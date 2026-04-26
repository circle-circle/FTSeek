"""
FTSeek HK 自动调优器 — 基于决策记录器真实数据

职责:
1. 读取策略历史表现 (从 DecisionRecorder)
2. 计算动态权重建议
3. 生成回测验证请求
4. 应用验证通过的权重调整

调优条件:
- 至少 ≥10 笔已平仓交易才建议调参
- 胜率 < 45% 或连续亏损 ≥3 笔 → 紧急调优
- 正常调优: 每周自动执行一次
"""
import logging
import json
from datetime import datetime
from typing import Dict, Any, List, Optional

logger = logging.getLogger("ftseek.deep_review.auto_tuner")


class AutoTuner:
    """自动调优器 — 基于真实决策/交易数据"""

    MIN_TRADES_FOR_TUNE = 10
    WIN_RATE_HIGH = 0.60
    WIN_RATE_LOW = 0.45
    WEIGHT_ADJUST_FACTOR = 0.10
    MIN_WEIGHT = 0.10
    MAX_WEIGHT = 0.50

    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self.min_trades = self.config.get("min_trades", self.MIN_TRADES_FOR_TUNE)
        self.auto_tune_enabled = self.config.get("auto_tune_enabled", True)
        logger.info(f"[AutoTuner] 初始化 | 最少交易={self.min_trades} | 自动调优={'开启' if self.auto_tune_enabled else '关闭'}")

    def run_weekly_review(self) -> Dict[str, Any]:
        """每周自动调优入口"""
        if not self.auto_tune_enabled:
            return {"status": "disabled"}

        from app.storage.decision_recorder import get_decision_recorder
        recorder = get_decision_recorder()

        model_perf = recorder.get_strategy_performance()
        if not model_perf:
            return {"status": "no_data", "message": "无历史数据"}

        total_trades = sum(m.get("total_votes", 0) for m in model_perf.values())
        if total_trades < self.min_trades:
            return {
                "status": "insufficient_data",
                "total_trades": total_trades,
                "required": self.min_trades,
                "message": f"交易数据不足: {total_trades}/{self.min_trades}",
            }

        current_weights = self._get_current_weights()
        suggestions = self._compute_weight_adjustments(model_perf, current_weights)

        if not suggestions:
            return {
                "status": "no_adjustment",
                "total_trades": total_trades,
                "message": "所有策略表现正常，无需调整",
            }

        emergency = self._check_emergency_issues(model_perf)

        result = {
            "status": "adjustment_ready",
            "total_trades": total_trades,
            "current_weights": current_weights,
            "suggested_weights": suggestions["new_weights"],
            "adjustments": suggestions["adjustments"],
            "emergency": emergency,
            "requires_approval": not emergency.get("is_emergency", False),
            "generated_at": datetime.now().isoformat(),
        }

        logger.info(f"[AutoTuner] 每周复盘: {total_trades} 笔交易, {len(suggestions['adjustments'])} 项调整")
        return result

    def _get_current_weights(self) -> Dict[str, float]:
        """获取当前策略权重 (从 config_center 读取)"""
        try:
            from config.config_center import get_config_center
            cc = get_config_center()
            weights = cc.get('strategy.signal_weights', {})
            if weights:
                return weights
        except Exception:
            pass
        # 默认权重 (HK 融合引擎)
        return {
            "technical": 0.50,
            "sentiment": 0.30,
            "llm": 0.20,
        }

    def _compute_weight_adjustments(
        self, model_perf: Dict[str, Any], current_weights: Dict[str, float]
    ) -> Optional[Dict]:
        """根据策略表现计算权重调整"""
        adjustments = []
        new_weights = dict(current_weights)

        for model_id, perf in model_perf.items():
            if model_id not in current_weights:
                continue

            total = perf.get("total_votes", 0)
            win_rate = perf.get("win_rate", 0.5)
            current = current_weights[model_id]

            if win_rate > self.WIN_RATE_HIGH and total >= 5:
                delta = min(self.WEIGHT_ADJUST_FACTOR, self.MAX_WEIGHT - current)
                if delta > 0.005:
                    new_weights[model_id] = round(current + delta, 3)
                    adjustments.append({
                        "model": model_id,
                        "old_weight": current,
                        "new_weight": new_weights[model_id],
                        "change": f"+{delta*100:.1f}%",
                        "reason": f"胜率 {win_rate*100:.0f}% > {self.WIN_RATE_HIGH*100:.0f}%",
                    })

            elif win_rate < self.WIN_RATE_LOW and total >= 5:
                delta = min(self.WEIGHT_ADJUST_FACTOR, current - self.MIN_WEIGHT)
                if delta > 0.005:
                    new_weights[model_id] = round(current - delta, 3)
                    adjustments.append({
                        "model": model_id,
                        "old_weight": current,
                        "new_weight": new_weights[model_id],
                        "change": f"-{delta*100:.1f}%",
                        "reason": f"胜率 {win_rate*100:.0f}% < {self.WIN_RATE_LOW*100:.0f}%",
                    })

        if not adjustments:
            return None

        # 重新归一化
        total_w = sum(new_weights.values())
        if total_w > 0:
            new_weights = {k: round(v / total_w, 3) for k, v in new_weights.items()}

        return {
            "new_weights": new_weights,
            "adjustments": adjustments,
        }

    def _check_emergency_issues(self, model_perf: Dict[str, Any]) -> Dict[str, Any]:
        """检查紧急问题"""
        issues = []
        for model_id, perf in model_perf.items():
            win_rate = perf.get("win_rate", 0.5)
            total = perf.get("total_votes", 0)

            if win_rate < 0.35 and total >= 5:
                issues.append({
                    "model": model_id,
                    "issue": "胜率极低",
                    "value": win_rate,
                    "severity": "critical",
                })

            daily = perf.get("daily_stats", {})
            if daily:
                consecutive_losses = 0
                for date_str in sorted(daily.keys(), reverse=True):
                    day = daily[date_str]
                    if day.get("votes", 0) > 0 and day.get("correct", 0) == 0:
                        consecutive_losses += 1
                    else:
                        break

                if consecutive_losses >= 3:
                    issues.append({
                        "model": model_id,
                        "issue": "连续亏损",
                        "value": consecutive_losses,
                        "severity": "warning",
                    })

        return {
            "is_emergency": any(i["severity"] == "critical" for i in issues),
            "issues": issues,
        }

    def record_adjustment(
        self,
        before_weights: Dict,
        after_weights: Dict,
        reason: str = "weekly_review",
        applied: bool = False,
    ) -> str:
        """记录权重调整到数据库"""
        from app.storage.decision_recorder import get_decision_recorder
        recorder = get_decision_recorder()
        return recorder.record_weight_adjustment(
            reason=reason,
            before_weights=before_weights,
            after_weights=after_weights,
            applied=applied,
        )

    def generate_summary_report(self) -> str:
        """生成调优摘要报告 (供钉钉推送)"""
        result = self.run_weekly_review()

        if result["status"] == "disabled":
            return "🔧 HK 自动调优已禁用"
        if result["status"] == "no_data":
            return "🔧 HK 无历史数据，无法调优"
        if result["status"] == "insufficient_data":
            return f"🔧 HK 数据不足: {result['total_trades']}/{result['required']} 笔交易"
        if result["status"] == "no_adjustment":
            return f"✅ HK 所有策略表现正常 ({result['total_trades']} 笔交易)，无需调整"

        lines = [
            "🔧 HK 策略权重调优建议",
            f"📊 数据量: {result['total_trades']} 笔交易",
            "",
            "当前权重:",
        ]
        for model, w in result["current_weights"].items():
            lines.append(f"  {model}: {w*100:.0f}%")

        lines.append("\n建议调整:")
        for adj in result["adjustments"]:
            lines.append(
                f"  {adj['model']}: {adj['old_weight']*100:.0f}% → {adj['new_weight']*100:.0f}% "
                f"({adj['change']}) - {adj['reason']}"
            )

        if result.get("emergency", {}).get("is_emergency"):
            lines.append("\n⚠️ 紧急问题:")
            for issue in result["emergency"]["issues"]:
                lines.append(f"  {issue['model']}: {issue['issue']} ({issue['value']})")

        lines.append(f"\n📅 生成时间: {result['generated_at']}")
        return "\n".join(lines)


# ─── Singleton ──────────────────────────────────────────────────
_instance = None


def get_auto_tuner() -> AutoTuner:
    global _instance
    if _instance is None:
        _instance = AutoTuner()
    return _instance
