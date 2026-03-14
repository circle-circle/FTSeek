"""
AI 预测追踪模块

追踪 AI 预测准确性，持续改进
"""
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict

logger = logging.getLogger("ftseek.analytics.prediction_tracker")

# 预测追踪存储目录
PREDICTION_TRACKING_DIR = Path('/opt/ftseek/FTSeek/data/prediction_tracking')
PREDICTION_TRACKING_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class PredictionRecord:
    """预测记录"""
    prediction_id: str
    code: str
    timestamp: str
    prediction_direction: str  # 上涨/下跌/震荡
    prediction_confidence: int  # 0-100
    ai_score: float  # -1 到 1
    
    # 实际结果 (后续填充)
    actual_direction: Optional[str] = None
    max_gain: Optional[float] = None  # 最大涨幅
    max_loss: Optional[float] = None  # 最大跌幅
    final_return: Optional[float] = None  # 最终收益
    holding_period: Optional[int] = None  # 持有天数
    
    # 准确性评估
    direction_correct: Optional[bool] = None
    confidence_calibrated: Optional[bool] = None  # 高置信度是否对应高准确率
    
    def to_dict(self) -> Dict:
        return asdict(self)


class PredictionTracker:
    """AI 预测追踪器"""
    
    def __init__(self):
        self.records: List[PredictionRecord] = []
        self._load_today_records()
    
    def _load_today_records(self):
        """加载今日预测记录"""
        today = datetime.now().strftime('%Y-%m-%d')
        records_file = PREDICTION_TRACKING_DIR / f'{today}.json'
        
        if records_file.exists():
            try:
                with open(records_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.records = [PredictionRecord(**r) for r in data]
                    logger.info(f"加载今日预测记录：{len(self.records)} 条")
            except Exception as e:
                logger.error(f"加载记录失败：{e}")
    
    def record_prediction(self,
                         code: str,
                         direction: str,
                         confidence: int,
                         ai_score: float,
                         prediction_id: str = None) -> PredictionRecord:
        """
        记录 AI 预测
        
        Args:
            code: 股票代码
            direction: 预测方向 (上涨/下跌/震荡)
            confidence: 置信度 (0-100)
            ai_score: AI 分数 (-1 到 1)
            prediction_id: 预测 ID
        
        Returns:
            PredictionRecord: 预测记录
        """
        import uuid
        
        record = PredictionRecord(
            prediction_id=prediction_id or str(uuid.uuid4())[:8],
            code=code,
            timestamp=datetime.now().isoformat(),
            prediction_direction=direction,
            prediction_confidence=confidence,
            ai_score=ai_score
        )
        
        self.records.append(record)
        self._save_records()
        
        logger.info(f"记录 AI 预测：{code} {direction} (置信度：{confidence}%)")
        return record
    
    def update_result(self,
                     prediction_id: str,
                     actual_direction: str,
                     max_gain: float,
                     max_loss: float,
                     final_return: float,
                     holding_period: int) -> Optional[PredictionRecord]:
        """
        更新预测结果
        
        Args:
            prediction_id: 预测 ID
            actual_direction: 实际方向
            max_gain: 最大涨幅
            max_loss: 最大跌幅
            final_return: 最终收益
            holding_period: 持有天数
        
        Returns:
            PredictionRecord: 更新后的记录
        """
        # 查找记录
        record = None
        for r in self.records:
            if r.prediction_id == prediction_id:
                record = r
                break
        
        if not record:
            logger.error(f"未找到预测记录：{prediction_id}")
            return None
        
        # 更新结果
        record.actual_direction = actual_direction
        record.max_gain = max_gain
        record.max_loss = max_loss
        record.final_return = final_return
        record.holding_period = holding_period
        
        # 评估准确性
        record.direction_correct = (record.prediction_direction == actual_direction)
        
        # 置信度校准评估
        # 高置信度 (>70) 应该对应正确预测
        if record.prediction_confidence > 70:
            record.confidence_calibrated = record.direction_correct
        else:
            record.confidence_calibrated = True  # 低置信度不要求
        
        self._save_records()
        
        logger.info(f"更新预测结果：{record.code} 准确性：{'正确' if record.direction_correct else '错误'}")
        return record
    
    def _save_records(self):
        """保存预测记录"""
        today = datetime.now().strftime('%Y-%m-%d')
        records_file = PREDICTION_TRACKING_DIR / f'{today}.json'
        
        data = [r.to_dict() for r in self.records]
        
        with open(records_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    
    def get_accuracy_stats(self, days: int = 7) -> Dict:
        """获取准确性统计"""
        cutoff_date = datetime.now() - timedelta(days=days)
        recent_records = [
            r for r in self.records
            if r.timestamp >= cutoff_date.strftime('%Y-%m-%d')
            and r.actual_direction is not None  # 已有结果
        ]
        
        if not recent_records:
            return {'error': f'最近{days}天无预测结果'}
        
        total = len(recent_records)
        correct = sum(1 for r in recent_records if r.direction_correct)
        accuracy = correct / total if total > 0 else 0
        
        # 按置信度分组
        high_confidence = [r for r in recent_records if r.prediction_confidence >= 70]
        medium_confidence = [r for r in recent_records if 40 <= r.prediction_confidence < 70]
        low_confidence = [r for r in recent_records if r.prediction_confidence < 40]
        
        stats = {
            'period': f'{cutoff_date.strftime("%Y-%m-%d")} ~ {datetime.now().strftime("%Y-%m-%d")}',
            'total_predictions': total,
            'correct_predictions': correct,
            'accuracy': round(accuracy, 4),
            'by_confidence': {
                'high': {
                    'count': len(high_confidence),
                    'accuracy': round(sum(1 for r in high_confidence if r.direction_correct) / len(high_confidence), 4) if high_confidence else 0
                },
                'medium': {
                    'count': len(medium_confidence),
                    'accuracy': round(sum(1 for r in medium_confidence if r.direction_correct) / len(medium_confidence), 4) if medium_confidence else 0
                },
                'low': {
                    'count': len(low_confidence),
                    'accuracy': round(sum(1 for r in low_confidence if r.direction_correct) / len(low_confidence), 4) if low_confidence else 0
                }
            },
            'avg_return_when_correct': 0,
            'avg_return_when_wrong': 0
        }
        
        # 计算正确/错误时的平均收益
        correct_returns = [r.final_return for r in recent_records if r.direction_correct and r.final_return]
        wrong_returns = [r.final_return for r in recent_records if not r.direction_correct and r.final_return]
        
        if correct_returns:
            stats['avg_return_when_correct'] = round(sum(correct_returns) / len(correct_returns), 4)
        if wrong_returns:
            stats['avg_return_when_wrong'] = round(sum(wrong_returns) / len(wrong_returns), 4)
        
        return stats
    
    def get_calibration_report(self) -> Dict:
        """获取置信度校准报告"""
        records_with_result = [r for r in self.records if r.actual_direction is not None]
        
        if not records_with_result:
            return {'error': '无预测结果'}
        
        # 按置信度区间统计准确率
        confidence_buckets = {
            '90-100': [],
            '80-89': [],
            '70-79': [],
            '60-69': [],
            '50-59': [],
            '<50': []
        }
        
        for r in records_with_result:
            conf = r.prediction_confidence
            if conf >= 90:
                confidence_buckets['90-100'].append(r)
            elif conf >= 80:
                confidence_buckets['80-89'].append(r)
            elif conf >= 70:
                confidence_buckets['70-79'].append(r)
            elif conf >= 60:
                confidence_buckets['60-69'].append(r)
            elif conf >= 50:
                confidence_buckets['50-59'].append(r)
            else:
                confidence_buckets['<50'].append(r)
        
        report = {}
        for bucket, records in confidence_buckets.items():
            if records:
                accuracy = sum(1 for r in records if r.direction_correct) / len(records)
                report[bucket] = {
                    'count': len(records),
                    'accuracy': round(accuracy, 4),
                    'avg_return': round(sum(r.final_return or 0 for r in records) / len(records), 4)
                }
        
        return report
    
    def generate_report(self, days: int = 7) -> Dict:
        """生成预测追踪报告"""
        accuracy_stats = self.get_accuracy_stats(days)
        calibration_report = self.get_calibration_report()
        
        report = {
            'accuracy_stats': accuracy_stats,
            'calibration': calibration_report,
            'recommendations': self._generate_recommendations(accuracy_stats)
        }
        
        return report
    
    def _generate_recommendations(self, accuracy_stats: Dict) -> List[str]:
        """生成改进建议"""
        recommendations = []
        
        if 'error' in accuracy_stats:
            return ['📊 数据不足，继续收集预测结果']
        
        accuracy = accuracy_stats.get('accuracy', 0)
        
        if accuracy > 0.65:
            recommendations.append(f'✅ AI 预测准确率 {accuracy:.1%}，表现优秀')
        elif accuracy > 0.55:
            recommendations.append(f'✅ AI 预测准确率 {accuracy:.1%}，表现良好')
        elif accuracy > 0.45:
            recommendations.append(f'⚠️ AI 预测准确率 {accuracy:.1%}，需要改进')
        else:
            recommendations.append(f'❌ AI 预测准确率 {accuracy:.1%}，建议调整或暂停使用')
        
        # 置信度校准建议
        by_confidence = accuracy_stats.get('by_confidence', {})
        if 'high' in by_confidence:
            high_acc = by_confidence['high'].get('accuracy', 0)
            if high_acc < 0.60:
                recommendations.append('⚠️ 高置信度预测准确率偏低，建议降低 AI 权重')
        
        return recommendations


# 全局单例
_tracker = None

def get_prediction_tracker() -> PredictionTracker:
    """获取 AI 预测追踪器单例"""
    global _tracker
    if _tracker is None:
        _tracker = PredictionTracker()
    return _tracker
