"""
错误模式检测器

自动检测交易中的常见错误模式，生成改进建议
"""
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from pathlib import Path
from dataclasses import dataclass
import json

logger = logging.getLogger("ftseek_us.review.error_detector")
logger.propagate = False


@dataclass
class ErrorPattern:
    """错误模式"""
    pattern_id: str
    pattern_name: str
    pattern_type: str  # entry/exit/risk/psychology/strategy
    severity: str  # low/medium/high/critical
    count: int
    total_loss: float
    avg_loss: float
    affected_trades: List[int]
    description: str
    suggestion: str


class ErrorPatternDetector:
    """错误模式检测器"""
    
    # 错误模式定义
    ERROR_PATTERNS = {
        # 入场错误
        "low_signal_entry": {
            "name": "低信号强度入场",
            "type": "entry",
            "condition": lambda t: (t.get("final_strength") or 50) < 50,
            "description": "信号强度低于 50 时入场",
            "suggestion": "提高信号强度阈值至 50 以上",
            "severity": "medium",
        },
        "low_confidence_entry": {
            "name": "低置信度入场",
            "type": "entry",
            "condition": lambda t: (t.get("signal_confidence") or 0.5) < 0.5,
            "description": "信号置信度低于 50% 时入场",
            "suggestion": "提高置信度阈值至 50% 以上",
            "severity": "medium",
        },
        "high_disagreement_entry": {
            "name": "高分歧入场",
            "type": "entry",
            "condition": lambda t: (t.get("consensus_level") or 1) < 0.5,
            "description": "模型一致性低于 50% 时入场",
            "suggestion": "提高一致性阈值，等待模型达成共识",
            "severity": "high",
        },
        
        # 出场错误
        "early_exit": {
            "name": "过早平仓",
            "type": "exit",
            "condition": lambda t: (
                t.get("holding_hours", 0) < 1 and 
                (t.get("realized_pnl", 0) or 0) > 0
            ),
            "description": "持仓不足 1 小时即平仓，可能错过更大收益",
            "suggestion": "延长持仓时间，设置更合理的止盈目标",
            "severity": "low",
        },
        "late_stop_loss": {
            "name": "止损延迟",
            "type": "exit",
            "condition": lambda t: (
                (t.get("realized_pnl", 0) or 0) < -500 and
                t.get("exit_reason") != "STOP_LOSS"
            ),
            "description": "亏损较大但未触发止损",
            "suggestion": "严格执行止损纪律，避免亏损扩大",
            "severity": "high",
        },
        
        # 风控错误
        "stop_loss_violation": {
            "name": "止损违规",
            "type": "risk",
            "condition": lambda t: (
                (t.get("max_drawdown") or 0) > 0.05 and
                (t.get("realized_pnl", 0) or 0) < 0
            ),
            "description": "持仓期间回撤超过 5% 导致亏损",
            "suggestion": "加强持仓监控，及时调整止损位置",
            "severity": "high",
        },
        "large_loss": {
            "name": "大额亏损",
            "type": "risk",
            "condition": lambda t: (t.get("realized_pnl", 0) or 0) < -1000,
            "description": "单笔亏损超过 $1000",
            "suggestion": "控制单笔仓位，设置最大亏损限制",
            "severity": "critical",
        },
        
        # 策略错误
        "strategy_underperform": {
            "name": "策略表现不佳",
            "type": "strategy",
            "condition": lambda t, stats: (
                stats.get("strategy", {}).get(t.get("strategy_id", ""), {}).get("win_rate", 0.5) < 0.4
            ),
            "description": "策略胜率低于 40%",
            "suggestion": "暂停该策略，重新评估策略逻辑",
            "severity": "high",
        },
    }
    
    def __init__(self, db_path: str = None):
        if db_path:
            self.db_path = Path(db_path)
        else:
            from app.core.paths import get_trade_records_db
            self.db_path = get_trade_records_db()
        
        logger.info(f"错误模式检测器已初始化：{self.db_path}")
    
    def _get_conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path))
    
    def detect_patterns(
        self,
        trades: List[Dict] = None,
        days: int = 30,
    ) -> List[ErrorPattern]:
        """
        检测错误模式
        
        Args:
            trades: 交易记录列表，如果为 None 则从数据库加载
            days: 加载最近多少天的数据
        
        Returns:
            检测到的错误模式列表
        """
        if trades is None:
            trades = self._load_recent_trades(days)
        
        if not trades:
            return []
        
        # 计算策略统计
        strategy_stats = self._calculate_strategy_stats(trades)
        
        patterns = []
        
        for pattern_id, pattern_def in self.ERROR_PATTERNS.items():
            affected_trades = []
            total_loss = 0
            
            for trade in trades:
                try:
                    # 检查条件
                    if pattern_def["type"] == "strategy":
                        condition_met = pattern_def["condition"](trade, strategy_stats)
                    else:
                        condition_met = pattern_def["condition"](trade)
                    
                    if condition_met:
                        affected_trades.append(trade.get("id", 0))
                        pnl = trade.get("realized_pnl", 0) or 0
                        if pnl < 0:
                            total_loss += abs(pnl)
                except Exception:
                    continue
            
            if affected_trades:
                avg_loss = total_loss / len(affected_trades) if affected_trades else 0
                
                patterns.append(ErrorPattern(
                    pattern_id=pattern_id,
                    pattern_name=pattern_def["name"],
                    pattern_type=pattern_def["type"],
                    severity=pattern_def["severity"],
                    count=len(affected_trades),
                    total_loss=total_loss,
                    avg_loss=avg_loss,
                    affected_trades=affected_trades,
                    description=pattern_def["description"],
                    suggestion=pattern_def["suggestion"],
                ))
        
        # 按严重程度和出现次数排序
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        patterns.sort(key=lambda p: (severity_order.get(p.severity, 4), -p.count))
        
        return patterns
    
    def _load_recent_trades(self, days: int) -> List[Dict]:
        """加载最近的交易记录"""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
            
            rows = conn.execute("""
                SELECT * FROM trade_records
                WHERE trade_date >= ?
                ORDER BY trade_date DESC
            """, (cutoff,)).fetchall()
            
            conn.close()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"加载交易记录失败：{e}")
            return []
    
    def _calculate_strategy_stats(self, trades: List[Dict]) -> Dict:
        """计算策略统计"""
        stats = {"strategy": {}}
        
        for trade in trades:
            strategy = trade.get("strategy_id", "unknown") or "unknown"
            if strategy not in stats["strategy"]:
                stats["strategy"][strategy] = {"trades": 0, "wins": 0, "pnl": 0}
            
            stats["strategy"][strategy]["trades"] += 1
            pnl = trade.get("realized_pnl", 0) or 0
            stats["strategy"][strategy]["pnl"] += pnl
            if pnl > 0:
                stats["strategy"][strategy]["wins"] += 1
        
        # 计算胜率
        for strategy, data in stats["strategy"].items():
            data["win_rate"] = data["wins"] / data["trades"] if data["trades"] > 0 else 0
        
        return stats
    
    def generate_report(self, days: int = 30) -> Dict:
        """
        生成错误模式报告
        
        Args:
            days: 分析最近多少天的数据
        
        Returns:
            错误模式报告
        """
        patterns = self.detect_patterns(days=days)
        
        # 按类型分组
        by_type = {}
        for pattern in patterns:
            if pattern.pattern_type not in by_type:
                by_type[pattern.pattern_type] = []
            by_type[pattern.pattern_type].append({
                "name": pattern.pattern_name,
                "severity": pattern.severity,
                "count": pattern.count,
                "total_loss": pattern.total_loss,
                "description": pattern.description,
                "suggestion": pattern.suggestion,
            })
        
        # 汇总统计
        total_errors = sum(p.count for p in patterns)
        total_loss = sum(p.total_loss for p in patterns)
        critical_count = sum(1 for p in patterns if p.severity == "critical")
        high_count = sum(1 for p in patterns if p.severity == "high")
        
        # 生成优先改进建议
        priority_suggestions = []
        for pattern in patterns[:5]:
            if pattern.severity in ["critical", "high"]:
                priority_suggestions.append({
                    "priority": "高" if pattern.severity == "critical" else "中",
                    "issue": pattern.pattern_name,
                    "action": pattern.suggestion,
                    "impact": f"影响 {pattern.count} 笔交易，累计损失 ${pattern.total_loss:.2f}",
                })
        
        return {
            "report_date": datetime.now().strftime('%Y-%m-%d'),
            "analysis_period": f"最近 {days} 天",
            "summary": {
                "total_errors": total_errors,
                "total_loss": round(total_loss, 2),
                "critical_count": critical_count,
                "high_count": high_count,
            },
            "by_type": by_type,
            "priority_suggestions": priority_suggestions,
        }


# 全局实例
_detector: Optional[ErrorPatternDetector] = None


def get_error_detector() -> ErrorPatternDetector:
    """获取错误模式检测器实例"""
    global _detector
    if _detector is None:
        _detector = ErrorPatternDetector()
    return _detector


def detect_error_patterns(days: int = 30) -> List[ErrorPattern]:
    """检测错误模式"""
    return get_error_detector().detect_patterns(days=days)


def generate_error_report(days: int = 30) -> Dict:
    """生成错误模式报告"""
    return get_error_detector().generate_report(days)