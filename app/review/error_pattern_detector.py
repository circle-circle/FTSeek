"""
错误模式识别模块

从历史交易中识别重复出现的错误模式
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from collections import defaultdict

from app.storage.trade_recorder import get_trade_recorder
from app.storage.review_tables import get_review_database

logger = logging.getLogger("ftseek.review.error_detector")
logger.propagate = False


class ErrorPatternDetector:
    """错误模式检测器"""
    
    # 预定义错误模式
    ERROR_PATTERNS = {
        'impulse_trading': {
            'name': '冲动交易',
            'category': '决策',
            'description': '未经充分分析就入场交易',
            'severity': 'high'
        },
        'stop_loss_violation': {
            'name': '止损违规',
            'category': '风控',
            'description': '未执行预设止损或擅自调整止损',
            'severity': 'critical'
        },
        'early_exit': {
            'name': '过早出场',
            'category': '时机',
            'description': '因恐惧或贪婪提前平仓',
            'severity': 'medium'
        },
        'late_entry': {
            'name': '过晚入场',
            'category': '时机',
            'description': '错过最佳入场点，追高杀跌',
            'severity': 'medium'
        },
        'oversized_position': {
            'name': '仓位过大',
            'category': '风控',
            'description': '单笔交易仓位超过风险限制',
            'severity': 'high'
        },
        'overtrading': {
            'name': '过度交易',
            'category': '决策',
            'description': '交易频率过高，缺乏耐心',
            'severity': 'medium'
        },
        'revenge_trading': {
            'name': '报复性交易',
            'category': '心理',
            'description': '亏损后立即交易试图回本',
            'severity': 'critical'
        },
        'holding_loser': {
            'name': '死扛亏损',
            'category': '心理',
            'description': '亏损时不愿止损，期待反转',
            'severity': 'critical'
        },
        'cutting_winner': {
            'name': '过早止盈',
            'category': '时机',
            'description': '盈利时过早平仓，错失大行情',
            'severity': 'medium'
        },
        'poor_timing': {
            'name': '时机不佳',
            'category': '时机',
            'description': '入场或出场时机评分持续偏低',
            'severity': 'medium'
        }
    }
    
    def __init__(self):
        self.trade_recorder = get_trade_recorder()
        self.review_db = get_review_database()
    
    def detect(self,
               period: str = 'last_30_days',
               min_occurrences: int = 2,
               account_type: str = None) -> List[Dict]:
        """
        检测错误模式
        
        Args:
            period: 时间周期
            min_occurrences: 最小出现次数
            account_type: 账户类型
        
        Returns:
            错误模式列表
        """
        logger.info(f"检测错误模式：{period}")
        
        # 获取交易记录
        trades = self.trade_recorder.get_records_with_pnl()
        
        # 时间过滤
        trades = self._filter_by_period(trades, period)
        
        # 账户类型过滤
        if account_type:
            trades = [t for t in trades if t.account_type == account_type]
        
        if not trades:
            return []
        
        patterns = []
        
        # 检测各类型错误
        patterns.extend(self._detect_impulse_trading(trades, min_occurrences))
        patterns.extend(self._detect_stop_loss_violation(trades, min_occurrences))
        patterns.extend(self._detect_early_exit(trades, min_occurrences))
        patterns.extend(self._detect_oversized_position(trades, min_occurrences))
        patterns.extend(self._detect_overtrading(trades, min_occurrences))
        patterns.extend(self._detect_revenge_trading(trades, min_occurrences))
        patterns.extend(self._detect_poor_timing(trades, min_occurrences))
        
        # 按严重程度和出现次数排序
        severity_order = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
        patterns.sort(key=lambda p: (severity_order.get(p['severity'], 4), -p['count']))
        
        logger.info(f"检测到 {len(patterns)} 个错误模式")
        return patterns
    
    def _filter_by_period(self, trades: List, period: str) -> List:
        """按时间周期过滤"""
        if period == 'all':
            return trades
        
        now = datetime.now()
        
        if period == 'last_7_days':
            cutoff = now - timedelta(days=7)
        elif period == 'last_30_days':
            cutoff = now - timedelta(days=30)
        elif period == 'last_90_days':
            cutoff = now - timedelta(days=90)
        else:
            cutoff = now - timedelta(days=30)
        
        return [
            t for t in trades
            if datetime.fromisoformat(t.timestamp[:19]) >= cutoff
        ]
    
    def _detect_impulse_trading(self, trades: List, min_count: int) -> List[Dict]:
        """检测冲动交易"""
        impulse_trades = [t for t in trades if t.decision_reason == 'impulse']
        
        if len(impulse_trades) < min_count:
            return []
        
        total_pnl = sum(t.pnl or 0 for t in impulse_trades)
        win_rate = len([t for t in impulse_trades if t.pnl and t.pnl > 0]) / len(impulse_trades)
        
        return [{
            'pattern_id': 'impulse_trading',
            'pattern_name': self.ERROR_PATTERNS['impulse_trading']['name'],
            'category': self.ERROR_PATTERNS['impulse_trading']['category'],
            'description': self.ERROR_PATTERNS['impulse_trading']['description'],
            'severity': self.ERROR_PATTERNS['impulse_trading']['severity'],
            'count': len(impulse_trades),
            'example_trades': [t.trade_id for t in impulse_trades[:5]],
            'avg_pnl_impact': round(total_pnl / len(impulse_trades), 2),
            'total_pnl_impact': round(total_pnl, 2),
            'win_rate': round(win_rate, 4),
            'suggested_action': '建立交易前检查清单，强制等待 5 分钟再决策',
            'frequency_trend': self._calculate_trend(trades, impulse_trades)
        }]
    
    def _detect_stop_loss_violation(self, trades: List, min_count: int) -> List[Dict]:
        """检测止损违规"""
        # 有计划止损但未执行的交易
        violation_trades = [
            t for t in trades
            if t.stop_loss_planned and t.stop_loss_planned > 0 and not t.stop_loss_executed
        ]
        
        if len(violation_trades) < min_count:
            return []
        
        total_pnl = sum(t.pnl or 0 for t in violation_trades)
        
        return [{
            'pattern_id': 'stop_loss_violation',
            'pattern_name': self.ERROR_PATTERNS['stop_loss_violation']['name'],
            'category': self.ERROR_PATTERNS['stop_loss_violation']['category'],
            'description': self.ERROR_PATTERNS['stop_loss_violation']['description'],
            'severity': self.ERROR_PATTERNS['stop_loss_violation']['severity'],
            'count': len(violation_trades),
            'example_trades': [t.trade_id for t in violation_trades[:5]],
            'avg_pnl_impact': round(total_pnl / len(violation_trades), 2),
            'total_pnl_impact': round(total_pnl, 2),
            'suggested_action': '设置自动止损单，减少人为干预',
            'frequency_trend': self._calculate_trend(trades, violation_trades)
        }]
    
    def _detect_early_exit(self, trades: List, min_count: int) -> List[Dict]:
        """检测过早出场"""
        # 出场时机评分低但盈利的交易 (可能过早止盈)
        early_exit_trades = [
            t for t in trades
            if (t.exit_timing_score or 5) < 4 and t.pnl and t.pnl > 0
        ]
        
        if len(early_exit_trades) < min_count:
            return []
        
        # 估算潜在利润损失
        avg_exit_score = sum(t.exit_timing_score or 5 for t in early_exit_trades) / len(early_exit_trades)
        
        return [{
            'pattern_id': 'early_exit',
            'pattern_name': self.ERROR_PATTERNS['early_exit']['name'],
            'category': self.ERROR_PATTERNS['early_exit']['category'],
            'description': self.ERROR_PATTERNS['early_exit']['description'],
            'severity': self.ERROR_PATTERNS['early_exit']['severity'],
            'count': len(early_exit_trades),
            'example_trades': [t.trade_id for t in early_exit_trades[:5]],
            'avg_exit_timing_score': round(avg_exit_score, 2),
            'suggested_action': '严格执行目标价位，避免因恐惧提前出场',
            'frequency_trend': self._calculate_trend(trades, early_exit_trades)
        }]
    
    def _detect_oversized_position(self, trades: List, min_count: int) -> List[Dict]:
        """检测仓位过大"""
        oversized_trades = [
            t for t in trades
            if (t.position_size_actual or 0) > 0.3  # 超过 30% 仓位
        ]
        
        if len(oversized_trades) < min_count:
            return []
        
        avg_position = sum(t.position_size_actual or 0 for t in oversized_trades) / len(oversized_trades)
        total_pnl = sum(t.pnl or 0 for t in oversized_trades)
        
        return [{
            'pattern_id': 'oversized_position',
            'pattern_name': self.ERROR_PATTERNS['oversized_position']['name'],
            'category': self.ERROR_PATTERNS['oversized_position']['category'],
            'description': self.ERROR_PATTERNS['oversized_position']['description'],
            'severity': self.ERROR_PATTERNS['oversized_position']['severity'],
            'count': len(oversized_trades),
            'example_trades': [t.trade_id for t in oversized_trades[:5]],
            'avg_position_size': round(avg_position, 4),
            'total_pnl_impact': round(total_pnl, 2),
            'suggested_action': '单笔交易仓位不超过 20%，分散风险',
            'frequency_trend': self._calculate_trend(trades, oversized_trades)
        }]
    
    def _detect_overtrading(self, trades: List, min_count: int) -> List[Dict]:
        """检测过度交易"""
        # 按日期分组
        daily_counts = defaultdict(int)
        for t in trades:
            date = t.timestamp[:10]
            daily_counts[date] += 1
        
        # 高频交易日
        high_freq_days = [date for date, count in daily_counts.items() if count >= 5]
        
        if len(high_freq_days) < min_count:
            return []
        
        # 高频日的交易
        overtrading_trades = [
            t for t in trades
            if t.timestamp[:10] in high_freq_days
        ]
        
        avg_trades_per_day = len(overtrading_trades) / len(high_freq_days)
        
        return [{
            'pattern_id': 'overtrading',
            'pattern_name': self.ERROR_PATTERNS['overtrading']['name'],
            'category': self.ERROR_PATTERNS['overtrading']['category'],
            'description': self.ERROR_PATTERNS['overtrading']['description'],
            'severity': self.ERROR_PATTERNS['overtrading']['severity'],
            'count': len(high_freq_days),
            'avg_trades_per_day': round(avg_trades_per_day, 1),
            'suggested_action': '设定每日最大交易次数 (建议≤3 笔)',
            'frequency_trend': 'stable'
        }]
    
    def _detect_revenge_trading(self, trades: List, min_count: int) -> List[Dict]:
        """检测报复性交易"""
        # 排序交易
        sorted_trades = sorted(trades, key=lambda t: t.timestamp)
        
        revenge_trades = []
        for i in range(1, len(sorted_trades)):
            prev = sorted_trades[i - 1]
            curr = sorted_trades[i]
            
            # 前一笔亏损且短时间内再次交易
            if prev.pnl and prev.pnl < 0:
                time_diff = (
                    datetime.fromisoformat(curr.timestamp[:19]) -
                    datetime.fromisoformat(prev.timestamp[:19])
                ).total_seconds()
                
                # 1 小时内再次交易
                if time_diff < 3600:
                    revenge_trades.append(curr)
        
        if len(revenge_trades) < min_count:
            return []
        
        revenge_win_rate = len([t for t in revenge_trades if t.pnl and t.pnl > 0]) / len(revenge_trades)
        
        return [{
            'pattern_id': 'revenge_trading',
            'pattern_name': self.ERROR_PATTERNS['revenge_trading']['name'],
            'category': self.ERROR_PATTERNS['revenge_trading']['category'],
            'description': self.ERROR_PATTERNS['revenge_trading']['description'],
            'severity': self.ERROR_PATTERNS['revenge_trading']['severity'],
            'count': len(revenge_trades),
            'example_trades': [t.trade_id for t in revenge_trades[:5]],
            'win_rate': round(revenge_win_rate, 4),
            'suggested_action': '亏损后强制休息 30 分钟，避免情绪化决策',
            'frequency_trend': 'stable'
        }]
    
    def _detect_poor_timing(self, trades: List, min_count: int) -> List[Dict]:
        """检测时机不佳"""
        poor_timing_trades = [
            t for t in trades
            if (t.entry_timing_score or 5) < 4 or (t.exit_timing_score or 5) < 4
        ]
        
        if len(poor_timing_trades) < min_count:
            return []
        
        avg_entry = sum(t.entry_timing_score or 5 for t in poor_timing_trades) / len(poor_timing_trades)
        avg_exit = sum(t.exit_timing_score or 5 for t in poor_timing_trades) / len(poor_timing_trades)
        
        return [{
            'pattern_id': 'poor_timing',
            'pattern_name': self.ERROR_PATTERNS['poor_timing']['name'],
            'category': self.ERROR_PATTERNS['poor_timing']['category'],
            'description': self.ERROR_PATTERNS['poor_timing']['description'],
            'severity': self.ERROR_PATTERNS['poor_timing']['severity'],
            'count': len(poor_timing_trades),
            'example_trades': [t.trade_id for t in poor_timing_trades[:5]],
            'avg_entry_timing_score': round(avg_entry, 2),
            'avg_exit_timing_score': round(avg_exit, 2),
            'suggested_action': '优化入场/出场策略，增加确认信号',
            'frequency_trend': self._calculate_trend(trades, poor_timing_trades)
        }]
    
    def _calculate_trend(self, all_trades: List, pattern_trades: List) -> str:
        """计算趋势 (increasing/stable/decreasing)"""
        if len(pattern_trades) < 3:
            return 'stable'
        
        # 按时间分组
        sorted_trades = sorted(all_trades, key=lambda t: t.timestamp)
        
        # 前后半段比较
        mid = len(sorted_trades) // 2
        first_half = sorted_trades[:mid]
        second_half = sorted_trades[mid:]
        
        first_count = len([t for t in first_half if t in pattern_trades])
        second_count = len([t for t in second_half if t in pattern_trades])
        
        if second_count > first_count * 1.5:
            return 'increasing'
        elif second_count < first_count * 0.5:
            return 'decreasing'
        else:
            return 'stable'
    
    def get_pattern_details(self, pattern_id: str) -> Optional[Dict]:
        """获取错误模式详情"""
        return self.ERROR_PATTERNS.get(pattern_id)
    
    def save_pattern(self, pattern_data: Dict) -> str:
        """保存错误模式到数据库"""
        return self.review_db.add_error_pattern(pattern_data)


# 便捷函数
def detect_error_patterns(period: str = 'last_30_days', min_occurrences: int = 2) -> List[Dict]:
    """检测错误模式"""
    detector = ErrorPatternDetector()
    return detector.detect(period, min_occurrences)
