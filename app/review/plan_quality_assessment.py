"""Phase 3: 作战计划质量评估 (Plan Quality Assessment)

评估盘前作战计划的价值和准确性：
1. 辩论质量 → 计划命中率关联分析
2. 方向准确率（计划建议 vs 实际收盘）
3. 辩论Agent贡献度
4. 计划内 vs 随机交易 ROI 对比
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("ftseek.review.plan_quality")

PLAN_DIR = Path(__file__).resolve().parent.parent.parent / "data"
DB_PATH = PLAN_DIR / "ftseek.db"


class PlanQualityAssessment:
    """作战计划质量评估器"""

    def assess(self, date: str = None) -> Dict:
        """评估指定日期的作战计划质量"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        plan = self._load_plan(date)
        if not plan:
            return {"error": f"未找到 {date} 的作战计划"}

        trades = self._load_trades(date)
        market_data = self._load_market_data(date)
        debate_archive = self._load_debate_archive(date)

        return {
            "date": date,
            "direction_accuracy": self._calc_direction_accuracy(plan, trades, market_data),
            "execution_rate": self._calc_execution_rate(plan, trades),
            "plan_vs_random": self._compare_plan_vs_random(plan, trades, market_data),
            "debate_quality_correlation": self._correlate_debate_quality(plan, debate_archive),
            "auction_filter_effectiveness": self._auction_effectiveness(plan, trades),
            "overall_score": self._composite_score(plan, trades),
        }

    def _load_plan(self, date: str) -> Optional[Dict]:
        """加载指定日期的作战计划"""
        plan_path = PLAN_DIR / "daily_battle_plan.json"
        try:
            with open(plan_path, 'r') as f:
                data = json.load(f)
            if data.get("date") == date:
                return data
        except Exception:
            pass
        return None

    def _load_trades(self, date: str) -> List[Dict]:
        """加载当日交易记录"""
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT code, action, entry_price, entry_time, exit_price, "
                    "realized_pnl, status FROM positions WHERE entry_time LIKE ?",
                    (f"{date}%",)
                )
                trades = [dict(row) for row in cursor.fetchall()]
            return trades
        except Exception as e:
            logger.warning(f"加载交易记录失败: {e}")
            return []

    def _load_market_data(self, date: str) -> Dict:
        """加载当日市场数据（简化版）"""
        return {}

    def _load_debate_archive(self, date: str) -> Optional[Dict]:
        """加载当日辩论归档"""
        archive_path = PLAN_DIR / "debate_archive" / f"{date}.json"
        try:
            with open(archive_path, 'r') as f:
                return json.load(f)
        except Exception:
            return None

    def _calc_direction_accuracy(self, plan: Dict, trades: List[Dict], market_data: Dict) -> Dict:
        """计算方向准确率：计划建议 vs 实际收盘"""
        plan_targets = {t['symbol']: t for t in plan.get('targets', [])}
        correct = 0
        total = 0

        for trade in trades:
            sym = trade['code']
            if sym in plan_targets:
                total += 1
                plan_dir = plan_targets[sym].get('direction', 'ANY')
                actual_action = trade.get('action', '')
                pnl = trade.get('realized_pnl', 0) or 0

                # 简单判断：计划做多且盈利，或计划做空且盈利
                if (plan_dir == 'LONG' and pnl > 0) or (plan_dir == 'SHORT' and pnl > 0):
                    correct += 1

        return {
            "correct": correct,
            "total": total,
            "accuracy": correct / total if total > 0 else 0,
        }

    def _calc_execution_rate(self, plan: Dict, trades: List[Dict]) -> Dict:
        """计算计划执行率"""
        plan_symbols = {t['symbol'] for t in plan.get('targets', [])}
        trade_symbols = {t['code'] for t in trades}
        executed = plan_symbols & trade_symbols

        return {
            "planned": len(plan_symbols),
            "executed": len(executed),
            "rate": len(executed) / len(plan_symbols) if plan_symbols else 0,
            "missed": list(plan_symbols - trade_symbols),
        }

    def _compare_plan_vs_random(self, plan: Dict, trades: List[Dict], market_data: Dict) -> Dict:
        """对比计划内交易 vs 计划外交易的 ROI"""
        plan_symbols = {t['symbol'] for t in plan.get('targets', [])}

        plan_trades = [t for t in trades if t['code'] in plan_symbols]
        random_trades = [t for t in trades if t['code'] not in plan_symbols]

        plan_pnl = sum(t.get('realized_pnl', 0) or 0 for t in plan_trades)
        random_pnl = sum(t.get('realized_pnl', 0) or 0 for t in random_trades)

        return {
            "plan_trades": len(plan_trades),
            "plan_pnl": round(plan_pnl, 2),
            "random_trades": len(random_trades),
            "random_pnl": round(random_pnl, 2),
            "plan_advantage": round(plan_pnl - random_pnl, 2) if random_trades else "N/A",
        }

    def _correlate_debate_quality(self, plan: Dict, debate_archive: Optional[Dict]) -> Dict:
        """分析辩论质量与计划命中率的相关性"""
        if not debate_archive:
            return {"correlation": "N/A", "reason": "无辩论归档数据"}

        # 提取辩论质量分数
        quality_scores = []
        for key, data in debate_archive.items():
            synth = data.get("synthesis", {})
            quality_scores.append(synth.get("debate_quality_score", 0))

        avg_quality = sum(quality_scores) / len(quality_scores) if quality_scores else 0

        return {
            "avg_debate_quality": round(avg_quality, 3),
            "total_debates": len(quality_scores),
            "correlation": "pending_more_data",
        }

    def _auction_effectiveness(self, plan: Dict, trades: List[Dict]) -> Dict:
        """评估竞价过滤的有效性"""
        restricted = set(plan.get('restricted', []))
        if not restricted:
            return {"filtered": 0, "would_have_lost": 0, "effectiveness": "N/A"}

        # 检查被过滤的标的如果交易是否会亏损
        would_have_lost = 0
        for trade in trades:
            if trade['code'] in restricted:
                pnl = trade.get('realized_pnl', 0) or 0
                if pnl < 0:
                    would_have_lost += 1

        return {
            "filtered": len(restricted),
            "would_have_lost": would_have_lost,
            "effectiveness": f"{would_have_lost}/{len(restricted)}" if restricted else "N/A",
        }

    def _composite_score(self, plan: Dict, trades: List[Dict]) -> Dict:
        """计算作战计划综合评分"""
        direction = self._calc_direction_accuracy(plan, trades, {})
        execution = self._calc_execution_rate(plan, trades)
        roi = self._compare_plan_vs_random(plan, trades, {})

        # 综合评分 (0-100)
        score = 0
        # 方向准确率权重 40%
        score += direction['accuracy'] * 40
        # 执行率权重 30%
        score += execution['rate'] * 30
        # 计划优势权重 30%
        if roi['plan_advantage'] != "N/A":
            roi_score = min(1.0, max(0.0, (roi['plan_advantage'] + 100) / 200))
            score += roi_score * 30

        return {
            "overall_score": round(score, 1),
            "direction_accuracy": round(direction['accuracy'], 3),
            "execution_rate": round(execution['rate'], 3),
            "plan_advantage": roi['plan_advantage'],
        }


def generate_plan_quality_report(date: str = None) -> str:
    """生成 Markdown 格式的计划质量报告"""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    assessor = PlanQualityAssessment()
    result = assessor.assess(date)

    if "error" in result:
        return f"❌ {result['error']}"

    lines = [
        f"## 📊 FTSeek 作战计划质量评估 | {date}",
        "",
        "### 🎯 综合评分",
        f"- **总分**: {result.get('overall_score', {}).get('overall_score', 'N/A')}/100",
        "",
        "### 📈 方向准确率",
        f"- 正确: {result['direction_accuracy']['correct']}/{result['direction_accuracy']['total']}",
        f"- 准确率: {result['direction_accuracy']['accuracy']:.1%}",
        "",
        "### ⚡ 执行率",
        f"- 计划: {result['execution_rate']['planned']} 只",
        f"- 执行: {result['execution_rate']['executed']} 只",
        f"- 执行率: {result['execution_rate']['rate']:.1%}",
        "",
        "### 💰 计划 vs 随机",
        f"- 计划内交易: {result['plan_vs_random']['plan_trades']} 笔, 盈亏 {result['plan_vs_random']['plan_pnl']:+.2f}",
        f"- 计划外交易: {result['plan_vs_random']['random_trades']} 笔, 盈亏 {result['plan_vs_random']['random_pnl']:+.2f}",
        f"- 计划优势: {result['plan_vs_random']['plan_advantage']}",
        "",
        "### 🛡️ 竞价过滤效果",
        f"- 过滤标的: {result['auction_filter_effectiveness']['filtered']} 只",
        f"- 避免亏损: {result['auction_filter_effectiveness']['would_have_lost']} 只",
        "",
        "---",
        "*报告由 FTSeek 计划质量评估引擎自动生成*",
    ]

    return "\n".join(lines)


if __name__ == "__main__":
    print(generate_plan_quality_report())
