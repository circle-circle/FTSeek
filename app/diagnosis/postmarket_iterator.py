#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek 盘后持续迭代器 (Layer 3)

功能:
1. 每日16:30收盘后自动执行
2. 分析当日漏斗 + 对比历史趋势
3. 生成优化建议（参数/配置/代码）
4. 保存到 data/diagnosis_knowledge/ 知识库
5. 推送盘后报告到钉钉/飞书

数据源:
- /opt/ftseek/FTSeek/data/signal_records/signals_YYYY-MM-DD.json
- /opt/ftseek/FTSeek/data/decisions_hk.db
- /opt/ftseek/FTSeek/data/diagnosis_knowledge/*.json (历史诊断)
"""

import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from collections import defaultdict

logger = logging.getLogger("ftseek.diagnosis.postmarket")

# 知识库存储目录
DIAGNOSIS_KNOWLEDGE_DIR = Path('/opt/ftseek/FTSeek/data/diagnosis_knowledge')
DIAGNOSIS_KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)

# 拒绝原因分类（复用 intraday_diagnose.py 的分类体系）
REJECT_CATEGORIES = {
    'circuit_breaker': '🔴 熔断器',
    'max_concurrent_positions': '📦 并发持仓上限',
    'daily_trade_limit': '📊 日内交易限额',
    'max_position': '📦 持仓上限',
    'quality_rejected': '🔬 质量过滤',
    'weak_signal': '📉 信号强度不足',
    'weak_signal_error': '❌ 信号错误',
    'mtf_rejected': '🔀 多周期不一致',
    'signal_cooldown': '⏳ 信号冷却',
    'existing_position': '📦 已有持仓',
    'decision_buffer_rejected': '📋 决策队列满',
}


class PostmarketIterator:
    """盘后持续迭代器"""
    
    def __init__(self, notifier=None):
        self.notifier = notifier
        self.signal_tracker = None
        self.decision_recorder = None
    
    def _lazy_init(self):
        """延迟初始化数据源"""
        if self.signal_tracker is None:
            try:
                from app.storage.signal_tracker import SignalTracker
                self.signal_tracker = SignalTracker()
            except Exception as e:
                logger.warning(f"[盘后迭代] signal_tracker 初始化失败: {e}")
        
        if self.decision_recorder is None:
            try:
                from app.storage.decision_recorder import get_decision_recorder
                self.decision_recorder = get_decision_recorder()
            except Exception as e:
                logger.warning(f"[盘后迭代] decision_recorder 初始化失败: {e}")
    
    def run_iteration(self) -> Dict[str, Any]:
        """执行盘后迭代分析"""
        self._lazy_init()
        
        logger.info("[盘后迭代] 开始执行迭代分析...")
        
        result = {
            "date": datetime.now().strftime('%Y-%m-%d'),
            "timestamp": datetime.now().isoformat(),
        }
        
        # 1. 漏斗趋势分析
        result["funnel_analysis"] = self._analyze_funnel_trend()
        
        # 2. 策略表现对比
        result["strategy_comparison"] = self._compare_strategy_performance()
        
        # 3. 拒绝原因深度分析
        result["reject_analysis"] = self._analyze_reject_reasons()
        
        # 4. 盈亏分布分析
        result["pnl_analysis"] = self._analyze_pnl_distribution()
        
        # 5. 参数优化建议
        result["optimization_suggestions"] = self._generate_optimization_suggestions(result)
        
        # 保存结果
        self._save_iteration_result(result)
        
        # 推送报告
        self._push_iteration_report(result)
        
        logger.info("[盘后迭代] 完成")
        return result
    
    def _analyze_funnel_trend(self) -> Dict[str, Any]:
        """分析漏斗趋势"""
        if self.signal_tracker is None:
            return {"error": "signal_tracker 不可用"}
        
        try:
            stats = self.signal_tracker.get_statistics()
        except Exception as e:
            return {"error": f"获取统计失败: {e}"}
        
        if not stats:
            return {"status": "no_signals", "message": "今日暂无信号记录"}
        
        # 获取历史诊断数据对比
        historical = self._load_historical_diagnoses(days=7)
        
        return {
            "today": {
                "total_signals": stats.get('total_signals', 0),
                "executed": stats.get('executed', 0),
                "rejected": stats.get('rejected', 0),
                "execution_rate": stats.get('execution_rate', 0),
                "win_rate": stats.get('win_rate', 0),
                "total_pnl": stats.get('total_pnl', 0),
                "avg_confidence": stats.get('avg_confidence', 0),
            },
            "historical_avg": self._calculate_historical_avg(historical),
            "trend": self._calculate_trend(stats, historical),
            "insights": self._generate_funnel_insights(stats, historical),
        }
    
    def _load_historical_diagnoses(self, days: int = 7) -> List[Dict]:
        """加载历史诊断数据"""
        historical = []
        today = datetime.now()
        
        for i in range(1, days + 1):
            date = (today - timedelta(days=i)).strftime('%Y-%m-%d')
            # 查找该日期的诊断文件
            files = list(DIAGNOSIS_KNOWLEDGE_DIR.glob(f"diagnosis_{date}_*.json"))
            for f in files:
                try:
                    with open(f, 'r', encoding='utf-8') as fh:
                        data = json.load(fh)
                        historical.append(data)
                except Exception:
                    pass
        
        return historical
    
    def _calculate_historical_avg(self, historical: List[Dict]) -> Dict[str, float]:
        """计算历史平均值"""
        if not historical:
            return {}
        
        # 取每个日期的最后一条诊断（收盘后）
        daily_last = {}
        for h in historical:
            date = h.get('date')
            if date:
                daily_last[date] = h
        
        if not daily_last:
            return {}
        
        totals = {"total": 0, "executed": 0, "rejected": 0}
        count = len(daily_last)
        
        for h in daily_last.values():
            summary = h.get('summary', {})
            totals["total"] += summary.get('total', 0)
            totals["executed"] += summary.get('critical', 0)  # 注意：这里应该用实际字段
        
        return {
            "avg_daily_signals": totals["total"] / count if count > 0 else 0,
        }
    
    def _calculate_trend(self, today_stats: Dict, historical: List[Dict]) -> str:
        """计算趋势"""
        if not historical:
            return "insufficient_data"
        
        today_exec_rate = today_stats.get('execution_rate', 0)
        
        # 简化趋势判断
        if today_exec_rate > 0.3:
            return "improving"
        elif today_exec_rate < 0.1:
            return "declining"
        else:
            return "stable"
    
    def _generate_funnel_insights(self, stats: Dict, historical: List[Dict]) -> List[str]:
        """生成漏斗洞察"""
        insights = []
        
        execution_rate = stats.get('execution_rate', 0)
        if execution_rate < 0.2:
            insights.append(f"⚠️ 执行率偏低 ({execution_rate:.0%})，建议检查风控阈值")
        elif execution_rate > 0.5:
            insights.append(f"✅ 执行率良好 ({execution_rate:.0%})")
        
        win_rate = stats.get('win_rate', 0)
        if win_rate < 0.4:
            insights.append(f"⚠️ 胜率偏低 ({win_rate:.0%})，建议优化信号筛选")
        elif win_rate > 0.6:
            insights.append(f"✅ 胜率良好 ({win_rate:.0%})")
        
        total_pnl = stats.get('total_pnl', 0)
        if total_pnl < 0:
            insights.append(f"📉 今日亏损 {total_pnl:.2f}，需关注策略表现")
        else:
            insights.append(f"📈 今日盈利 {total_pnl:.2f}")
        
        return insights
    
    def _compare_strategy_performance(self) -> Dict[str, Any]:
        """对比策略表现"""
        if self.decision_recorder is None:
            return {"error": "decision_recorder 不可用"}
        
        try:
            strategy_perf = self.decision_recorder.get_strategy_performance()
        except Exception as e:
            return {"error": f"获取策略表现失败: {e}"}
        
        if not strategy_perf:
            return {"status": "no_data", "message": "暂无策略表现记录"}
        
        # 排序策略表现
        sorted_strategies = sorted(
            strategy_perf.items(),
            key=lambda x: x[1].get('win_rate', 0),
            reverse=True
        )
        
        return {
            "strategies": {
                k: {
                    "total_votes": v.get('total_votes', 0),
                    "win_rate": v.get('win_rate', 0),
                    "total_pnl": v.get('total_pnl', 0),
                    "avg_score": v.get('avg_score', 0),
                }
                for k, v in sorted_strategies
            },
            "insights": [
                f"共 {len(strategy_perf)} 个策略有表现记录",
                f"最佳策略: {sorted_strategies[0][0] if sorted_strategies else 'N/A'} (胜率 {sorted_strategies[0][1].get('win_rate', 0):.0%})",
            ]
        }
    
    def _analyze_reject_reasons(self) -> Dict[str, Any]:
        """分析拒绝原因"""
        if self.signal_tracker is None:
            return {"error": "signal_tracker 不可用"}
        
        try:
            today_signals = self.signal_tracker.get_today_signals()
        except Exception as e:
            return {"error": f"获取信号失败: {e}"}
        
        if not today_signals:
            return {"status": "no_signals"}
        
        rejected = [s for s in today_signals if not s.executed and s.reject_reason]
        
        # 分类拒绝原因
        categorized_reasons = defaultdict(int)
        raw_reasons = defaultdict(int)
        
        for s in rejected:
            reason = s.reject_reason or "unknown"
            raw_reasons[reason] += 1
            
            # 分类
            categorized = False
            for key, label in REJECT_CATEGORIES.items():
                if key in reason.lower() or key in reason:
                    categorized_reasons[label] += 1
                    categorized = True
                    break
            if not categorized:
                categorized_reasons['❓ 其他'] += 1
        
        return {
            "total_rejected": len(rejected),
            "categorized_reasons": dict(categorized_reasons),
            "raw_reasons": dict(raw_reasons),
            "top_3_reasons": sorted(categorized_reasons.items(), key=lambda x: x[1], reverse=True)[:3],
        }
    
    def _analyze_pnl_distribution(self) -> Dict[str, Any]:
        """分析盈亏分布"""
        if self.signal_tracker is None:
            return {"error": "signal_tracker 不可用"}
        
        try:
            today_signals = self.signal_tracker.get_today_signals()
        except Exception as e:
            return {"error": f"获取信号失败: {e}"}
        
        exited_signals = [s for s in today_signals if s.pnl is not None]
        
        if not exited_signals:
            return {"status": "no_exited_signals"}
        
        winning = [s for s in exited_signals if s.pnl > 0]
        losing = [s for s in exited_signals if s.pnl <= 0]
        
        # 计算盈亏比
        total_profit = sum(s.pnl for s in winning) if winning else 0
        total_loss = sum(s.pnl for s in losing) if losing else 0
        profit_loss_ratio = abs(total_profit / total_loss) if total_loss != 0 else float('inf')
        
        return {
            "total_exited": len(exited_signals),
            "winning": len(winning),
            "losing": len(losing),
            "win_rate": len(winning) / len(exited_signals) if exited_signals else 0,
            "total_profit": total_profit,
            "total_loss": total_loss,
            "net_pnl": total_profit + total_loss,
            "profit_loss_ratio": profit_loss_ratio,
            "avg_win": total_profit / len(winning) if winning else 0,
            "avg_loss": total_loss / len(losing) if losing else 0,
        }
    
    def _generate_optimization_suggestions(self, result: Dict) -> List[str]:
        """生成优化建议"""
        suggestions = []
        
        # 基于漏斗分析
        funnel = result.get("funnel_analysis", {})
        if isinstance(funnel, dict):
            today = funnel.get("today", {})
            exec_rate = today.get('execution_rate', 0)
            if exec_rate < 0.2:
                suggestions.append("🔧 执行率偏低，建议检查风控阈值是否过严（unified_config.yaml: risk_limits）")
        
        # 基于策略表现
        strategy = result.get("strategy_comparison", {})
        if isinstance(strategy, dict) and "strategies" in strategy:
            for name, perf in strategy["strategies"].items():
                if perf.get('win_rate', 0) < 0.3 and perf.get('total_votes', 0) > 5:
                    suggestions.append(f"🔧 策略 {name} 胜率仅 {perf['win_rate']:.0%}，建议调整参数或降低权重")
        
        # 基于拒绝原因
        reject = result.get("reject_analysis", {})
        if isinstance(reject, dict):
            top_reasons = reject.get("top_3_reasons", [])
            if top_reasons:
                top_name, top_count = top_reasons[0]
                if top_count > 3:
                    suggestions.append(f"🔧 主要拒绝原因: {top_name} ({top_count}次)，建议针对性优化")
        
        # 基于盈亏分布
        pnl = result.get("pnl_analysis", {})
        if isinstance(pnl, dict):
            if pnl.get('profit_loss_ratio', 0) < 1.5:
                suggestions.append("🔧 盈亏比偏低 (<1.5)，建议优化止盈止损参数")
        
        if not suggestions:
            suggestions.append("✅ 今日表现良好，暂无优化建议")
        
        return suggestions
    
    def _save_iteration_result(self, result: Dict):
        """保存迭代结果"""
        today = datetime.now().strftime('%Y-%m-%d')
        result_file = DIAGNOSIS_KNOWLEDGE_DIR / f"iteration_{today}.json"
        
        try:
            with open(result_file, 'w', encoding='utf-8') as f:
                json.dump(result, f, indent=2, ensure_ascii=False, default=str)
            logger.info(f"[盘后迭代] 结果已保存: {result_file}")
        except Exception as e:
            logger.error(f"[盘后迭代] 保存失败: {e}")
    
    def _push_iteration_report(self, result: Dict):
        """推送迭代报告"""
        if not self.notifier:
            return
        
        funnel = result.get("funnel_analysis", {})
        today_stats = funnel.get("today", {}) if isinstance(funnel, dict) else {}
        suggestions = result.get("optimization_suggestions", [])
        pnl = result.get("pnl_analysis", {})
        
        lines = [
            f"### 📊 盘后迭代报告 | {result['date']}\n",
            f"**信号漏斗**:",
            f"- 总信号: {today_stats.get('total_signals', 0)}",
            f"- 执行率: {today_stats.get('execution_rate', 0):.0%}",
            f"- 胜率: {today_stats.get('win_rate', 0):.0%}",
            f"- 总盈亏: {today_stats.get('total_pnl', 0):.2f}",
            "",
        ]
        
        if isinstance(pnl, dict) and pnl.get('total_exited', 0) > 0:
            lines.extend([
                f"**盈亏分布**:",
                f"- 平仓: {pnl.get('total_exited', 0)} 笔",
                f"- 盈利: {pnl.get('winning', 0)} 笔 | 亏损: {pnl.get('losing', 0)} 笔",
                f"- 盈亏比: {pnl.get('profit_loss_ratio', 0):.2f}",
                "",
            ])
        
        lines.append("**优化建议**:")
        for i, s in enumerate(suggestions, 1):
            lines.append(f"{i}. {s}")
        
        content = "\n".join(lines)
        try:
            self.notifier.send_text(content)
            logger.info("[盘后迭代] 报告已推送")
        except Exception as e:
            logger.error(f"[盘后迭代] 推送失败: {e}")


# ─── 单例 ──────────────────────────────────────────────────
_instance = None
_lock = None

def get_postmarket_iterator(notifier=None) -> PostmarketIterator:
    global _instance, _lock
    if _lock is None:
        import threading
        _lock = threading.Lock()
    
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = PostmarketIterator(notifier=notifier)
    return _instance
