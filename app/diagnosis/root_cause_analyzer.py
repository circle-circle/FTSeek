#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek 根因分析器 (Layer 2)

功能:
1. 单股拒绝时间线分析
2. 根因分类统计
3. 管线健康度评估
4. 提供 API 端点数据

数据源:
- /opt/ftseek/FTSeek/data/signal_records/signals_YYYY-MM-DD.json
- /opt/ftseek/FTSeek/data/decisions_hk.db
"""

import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from collections import defaultdict

logger = logging.getLogger("ftseek.diagnosis.root_cause")


class RootCauseAnalyzer:
    """根因分析器"""
    
    def __init__(self):
        self.signal_tracker = None
        self.decision_recorder = None
    
    def _lazy_init(self):
        """延迟初始化数据源"""
        if self.signal_tracker is None:
            try:
                from app.storage.signal_tracker import SignalTracker
                self.signal_tracker = SignalTracker()
            except Exception as e:
                logger.warning(f"[根因分析] signal_tracker 初始化失败: {e}")
        
        if self.decision_recorder is None:
            try:
                from app.storage.decision_recorder import get_decision_recorder
                self.decision_recorder = get_decision_recorder()
            except Exception as e:
                logger.warning(f"[根因分析] decision_recorder 初始化失败: {e}")
    
    def analyze_stock_root_cause(self, code: str, date: str = None) -> Dict[str, Any]:
        """
        单股根因分析
        
        Args:
            code: 股票代码 (如 "HK.00700")
            date: 日期 (YYYY-MM-DD)，默认今日
        
        Returns:
            根因分析报告
        """
        self._lazy_init()
        
        if self.signal_tracker is None:
            return {"error": "signal_tracker 不可用"}
        
        try:
            today_signals = self.signal_tracker.get_today_signals()
        except Exception as e:
            return {"error": f"获取信号失败: {e}"}
        
        # 过滤该股票的信号
        code_signals = [s for s in today_signals if s.code == code]
        
        if not code_signals:
            return {
                "code": code,
                "date": date or datetime.now().strftime('%Y-%m-%d'),
                "signal_count": 0,
                "message": f"未找到 {code} 的今日信号记录",
            }
        
        # 构建时间线
        timeline = []
        for s in sorted(code_signals, key=lambda x: x.timestamp):
            timeline.append({
                "signal_id": s.signal_id,
                "timestamp": s.timestamp,
                "action": s.action,
                "confidence": round(s.confidence, 4),
                "consensus_score": round(s.consensus_score, 4),
                "executed": s.executed,
                "reject_reason": s.reject_reason,
                "execution_price": s.execution_price,
                "exit_price": s.exit_price,
                "pnl": s.pnl,
                "strategy": s.strategy,
            })
        
        # 根因分类
        reject_reasons = defaultdict(int)
        for s in code_signals:
            if not s.executed and s.reject_reason:
                # 分类拒绝原因
                reason = self._categorize_reject_reason(s.reject_reason)
                reject_reasons[reason] += 1
        
        # 根因分析
        root_causes = self._analyze_root_causes(code_signals, reject_reasons)
        
        return {
            "code": code,
            "date": date or datetime.now().strftime('%Y-%m-%d'),
            "signal_count": len(code_signals),
            "timeline": timeline,
            "root_causes": dict(reject_reasons),
            "analysis": root_causes,
            "summary": {
                "total_signals": len(code_signals),
                "executed": sum(1 for s in code_signals if s.executed),
                "rejected": sum(1 for s in code_signals if not s.executed),
                "avg_confidence": round(sum(s.confidence for s in code_signals) / len(code_signals), 4),
                "strategies_involved": list(set(s.strategy for s in code_signals)),
            },
        }
    
    def _categorize_reject_reason(self, reason: str) -> str:
        """分类拒绝原因"""
        if not reason:
            return "unknown"
        
        reason_lower = reason.lower()
        
        if "confidence" in reason_lower or "置信度" in reason_lower:
            return "confidence_too_low"
        elif "cooldown" in reason_lower or "冷却" in reason_lower:
            return "cooldown_period"
        elif "position" in reason_lower or "仓位" in reason_lower:
            return "position_limit"
        elif "market" in reason_lower or "市场" in reason_lower:
            return "market_condition"
        elif "risk" in reason_lower or "风控" in reason_lower:
            return "risk_control"
        elif "duplicate" in reason_lower or "重复" in reason_lower:
            return "duplicate_signal"
        else:
            return "other"
    
    def _analyze_root_causes(self, signals: List, reject_reasons: Dict) -> Dict[str, Any]:
        """分析根因"""
        analysis = {
            "primary_cause": None,
            "confidence_issue": False,
            "timing_issue": False,
            "strategy_conflict": False,
            "suggestions": [],
        }
        
        # 检查主要拒绝原因
        if reject_reasons:
            primary = max(reject_reasons.items(), key=lambda x: x[1])
            analysis["primary_cause"] = primary[0]
            
            if primary[0] == "confidence_too_low":
                analysis["confidence_issue"] = True
                analysis["suggestions"].append("信号置信度持续偏低，建议检查策略参数或市场环境")
            elif primary[0] == "cooldown_period":
                analysis["timing_issue"] = True
                analysis["suggestions"].append("冷却期频繁触发，建议评估冷却时间设置是否合理")
            elif primary[0] == "position_limit":
                analysis["suggestions"].append("仓位限制导致信号被拒，建议检查仓位管理配置")
        
        # 检查策略冲突
        actions = set(s.action for s in signals)
        if len(actions) > 1:
            analysis["strategy_conflict"] = True
            analysis["suggestions"].append("同一标的出现多策略方向冲突，需人工判断")
        
        # 检查置信度趋势
        if len(signals) >= 3:
            sorted_signals = sorted(signals, key=lambda s: s.timestamp)
            first_conf = sorted_signals[0].confidence
            last_conf = sorted_signals[-1].confidence
            if last_conf < first_conf - 0.1:
                analysis["suggestions"].append(f"置信度呈下降趋势 ({first_conf:.2f} → {last_conf:.2f})")
        
        return analysis
    
    def analyze_pipeline_health(self, date: str = None) -> Dict[str, Any]:
        """
        管线健康度分析
        
        Returns:
            管线健康报告
        """
        self._lazy_init()
        
        if self.signal_tracker is None:
            return {"error": "signal_tracker 不可用"}
        
        try:
            stats = self.signal_tracker.get_statistics()
        except Exception as e:
            return {"error": f"获取统计失败: {e}"}
        
        if not stats:
            return {
                "date": date or datetime.now().strftime('%Y-%m-%d'),
                "status": "no_signals",
                "message": "今日暂无信号记录",
            }
        
        # 计算健康分数
        health_score = self._calculate_health_score(stats)
        
        # 识别问题
        issues = []
        if stats.get('execution_rate', 0) < 0.3:
            issues.append({
                "type": "low_execution_rate",
                "severity": "warning",
                "message": f"执行率偏低 ({stats['execution_rate']:.0%})，可能原因：风控过严/信号质量差",
            })
        
        if stats.get('win_rate', 0) < 0.4 and stats.get('total_pnl', 0) < 0:
            issues.append({
                "type": "low_win_rate",
                "severity": "warning",
                "message": f"胜率偏低 ({stats['win_rate']:.0%}) 且总盈亏为负",
            })
        
        # 置信度分布分析
        confidence_buckets = stats.get('confidence_buckets', {})
        low_conf_count = confidence_buckets.get('0.40-0.50', {}).get('count', 0)
        if low_conf_count > 5:
            issues.append({
                "type": "too_many_low_confidence",
                "severity": "info",
                "message": f"低置信度信号过多 ({low_conf_count}个)，建议提高 min_confidence 阈值",
            })
        
        return {
            "date": date or datetime.now().strftime('%Y-%m-%d'),
            "health_score": health_score,
            "status": "healthy" if health_score >= 80 else ("warning" if health_score >= 60 else "critical"),
            "funnel": {
                "total_signals": stats.get('total_signals', 0),
                "executed": stats.get('executed', 0),
                "rejected": stats.get('rejected', 0),
                "execution_rate": stats.get('execution_rate', 0),
                "win_rate": stats.get('win_rate', 0),
                "total_pnl": stats.get('total_pnl', 0),
            },
            "confidence_buckets": confidence_buckets,
            "issues": issues,
            "timestamp": datetime.now().isoformat(),
        }
    
    def _calculate_health_score(self, stats: Dict) -> int:
        """计算管线健康分数 (0-100)"""
        score = 100
        
        # 执行率权重 30%
        execution_rate = stats.get('execution_rate', 0)
        if execution_rate < 0.2:
            score -= 30
        elif execution_rate < 0.4:
            score -= 15
        
        # 胜率权重 30%
        win_rate = stats.get('win_rate', 0)
        if win_rate < 0.3:
            score -= 30
        elif win_rate < 0.4:
            score -= 15
        
        # 信号数量权重 20%
        total_signals = stats.get('total_signals', 0)
        if total_signals == 0:
            score -= 20
        elif total_signals < 3:
            score -= 10
        
        # 盈亏权重 20%
        total_pnl = stats.get('total_pnl', 0)
        if total_pnl < -1000:
            score -= 20
        elif total_pnl < 0:
            score -= 10
        
        return max(0, min(100, score))


# ─── 单例 ──────────────────────────────────────────────────
_instance = None
_lock = None

def get_root_cause_analyzer() -> RootCauseAnalyzer:
    global _instance, _lock
    if _lock is None:
        import threading
        _lock = threading.Lock()
    
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = RootCauseAnalyzer()
    return _instance
