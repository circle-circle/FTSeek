"""
信号质量监控模块

统计 AI vs 非 AI 信号的准确率
"""
import os
import json
import time
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List
from dataclasses import dataclass, field

logger = logging.getLogger("ftseek.ai.signal_monitor")
logger.propagate = False  # 防止日志重复

# 监控数据目录
MONITOR_DIR = Path('/opt/ftseek/FTSeek/data/ai_monitor')
MONITOR_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class SignalRecord:
    """信号记录"""
    code: str
    timestamp: str
    strategy: str  # fusion_v2 / fusion_v3
    action: str  # BUY / SELL
    confidence: float
    ai_direction: str = ''
    ai_confidence: int = 0
    
    # 后续填充
    actual_direction: str = ''
    is_correct: bool = False


@dataclass
class DailyStats:
    """每日统计"""
    date: str
    total_signals: int = 0
    ai_signals: int = 0
    non_ai_signals: int = 0
    ai_correct: int = 0
    non_ai_correct: int = 0
    
    @property
    def ai_accuracy(self) -> float:
        if self.ai_signals == 0:
            return 0.0
        return self.ai_correct / self.ai_signals
    
    @property
    def non_ai_accuracy(self) -> float:
        if self.non_ai_signals == 0:
            return 0.0
        return self.non_ai_correct / self.non_ai_signals
    
    @property
    def ai_improvement(self) -> float:
        return self.ai_accuracy - self.non_ai_accuracy
    
    def to_dict(self) -> Dict:
        return {
            'date': self.date,
            'total_signals': self.total_signals,
            'ai_signals': self.ai_signals,
            'non_ai_signals': self.non_ai_signals,
            'ai_correct': self.ai_correct,
            'non_ai_correct': self.non_ai_correct,
            'ai_accuracy': round(self.ai_accuracy, 4),
            'non_ai_accuracy': round(self.non_ai_accuracy, 4),
            'ai_improvement': round(self.ai_improvement, 4)
        }


class SignalMonitor:
    """信号质量监控器"""
    
    def __init__(self):
        self.records: List[SignalRecord] = []
        self.daily_stats: Dict[str, DailyStats] = {}
        self._load_today_stats()
    
    def _load_today_stats(self):
        """加载今日统计"""
        today = datetime.now().strftime('%Y-%m-%d')
        stats_file = MONITOR_DIR / f'{today}.json'
        
        if stats_file.exists():
            try:
                with open(stats_file, 'r') as f:
                    data = json.load(f)
                    self.daily_stats[today] = DailyStats(**data)
                    logger.info(f"加载今日统计：{today}")
            except Exception as e:
                logger.error(f"加载统计失败：{e}")
    
    def record_signal(self, signal: SignalRecord):
        """记录信号"""
        self.records.append(signal)
        logger.debug(f"记录信号：{signal.code} - {signal.strategy}")
    
    def update_result(self, code: str, actual_direction: str):
        """更新信号结果"""
        for record in self.records:
            if record.code == code and not record.actual_direction:
                record.actual_direction = actual_direction
                record.is_correct = (record.action == 'BUY' and actual_direction == '上涨') or \
                                   (record.action == 'SELL' and actual_direction == '下跌')
                logger.info(f"更新信号结果：{code} - {'正确' if record.is_correct else '错误'}")
                break
    
    def save_daily_stats(self):
        """保存每日统计"""
        today = datetime.now().strftime('%Y-%m-%d')
        
        # 计算今日统计
        today_records = [r for r in self.records if r.timestamp.startswith(today)]
        
        stats = DailyStats(date=today)
        stats.total_signals = len(today_records)
        
        for record in today_records:
            if record.strategy == 'fusion_v3':
                stats.ai_signals += 1
                if record.is_correct:
                    stats.ai_correct += 1
            else:
                stats.non_ai_signals += 1
                if record.is_correct:
                    stats.non_ai_correct += 1
        
        self.daily_stats[today] = stats
        
        # 保存到文件
        stats_file = MONITOR_DIR / f'{today}.json'
        with open(stats_file, 'w') as f:
            json.dump(stats.to_dict(), f, indent=2, ensure_ascii=False)
        
        logger.info(f"保存今日统计：{stats.to_dict()}")
    
    def get_report(self, days: int = 7) -> Dict:
        """获取监控报告"""
        today = datetime.now()
        report = {
            'summary': {},
            'daily_stats': [],
            'recommendations': []
        }
        
        # 汇总统计
        total_ai_signals = 0
        total_ai_correct = 0
        total_non_ai_signals = 0
        total_non_ai_correct = 0
        
        for i in range(days):
            date = (today - timedelta(days=i)).strftime('%Y-%m-%d')
            if date in self.daily_stats:
                stats = self.daily_stats[date]
                report['daily_stats'].append(stats.to_dict())
                
                total_ai_signals += stats.ai_signals
                total_ai_correct += stats.ai_correct
                total_non_ai_signals += stats.non_ai_signals
                total_non_ai_correct += stats.non_ai_correct
        
        # 总体统计
        if total_ai_signals > 0:
            report['summary'] = {
                'ai_accuracy': round(total_ai_correct / total_ai_signals, 4),
                'non_ai_accuracy': round(total_non_ai_correct / total_non_ai_signals, 4) if total_non_ai_signals > 0 else 0,
                'ai_improvement': round(total_ai_correct / total_ai_signals - total_non_ai_correct / total_non_ai_signals, 4) if total_non_ai_signals > 0 else 0,
                'total_signals': total_ai_signals + total_non_ai_signals,
                'ai_signals': total_ai_signals,
                'days': days
            }
            
            # 生成建议
            if report['summary']['ai_accuracy'] > 0.6:
                report['recommendations'].append('✅ AI 信号准确率良好，可以考虑增加流量比例')
            elif report['summary']['ai_accuracy'] < 0.5:
                report['recommendations'].append('⚠️ AI 信号准确率偏低，建议调整 AI 权重或暂停使用')
            
            if report['summary']['ai_improvement'] > 0.05:
                report['recommendations'].append('✅ AI 带来显著提升，建议保持当前配置')
        
        return report


# 全局单例
_monitor = None

def get_signal_monitor() -> SignalMonitor:
    """获取信号监控器单例"""
    global _monitor
    if _monitor is None:
        _monitor = SignalMonitor()
    return _monitor
