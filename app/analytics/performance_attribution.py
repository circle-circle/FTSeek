"""
收益归因分析模块

分析收益来源 (AI/传统因子/市场环境)
"""
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List
from collections import defaultdict

logger = logging.getLogger("ftseek.analytics.performance_attribution")

# 从交易记录目录加载数据
TRADE_RECORDS_DIR = Path('/opt/ftseek/FTSeek/data/trade_records')


class PerformanceAttribution:
    """收益归因分析器"""
    
    def __init__(self):
        self.records = []
        self._load_all_records()
    
    def _load_all_records(self):
        """加载所有交易记录"""
        if not TRADE_RECORDS_DIR.exists():
            logger.warning("交易记录目录不存在")
            return
        
        for file in TRADE_RECORDS_DIR.glob('*.json'):
            try:
                with open(file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.records.extend(data)
            except Exception as e:
                logger.error(f"加载文件失败 {file}: {e}")
        
        logger.info(f"加载交易记录：{len(self.records)} 条")
    
    def analyze_by_strategy(self) -> Dict:
        """按策略分析收益贡献"""
        strategy_stats = defaultdict(lambda: {
            'trades': 0,
            'winning': 0,
            'losing': 0,
            'total_pnl': 0.0,
            'win_rate': 0.0
        })
        
        for record in self.records:
            if record.get('exit_price') is None:
                continue  # 跳过未平仓
            
            strategy = record.get('strategy', 'unknown')
            pnl = record.get('pnl', 0) or 0
            
            strategy_stats[strategy]['trades'] += 1
            strategy_stats[strategy]['total_pnl'] += pnl
            
            if pnl > 0:
                strategy_stats[strategy]['winning'] += 1
            else:
                strategy_stats[strategy]['losing'] += 1
        
        # 计算胜率
        for strategy, stats in strategy_stats.items():
            if stats['trades'] > 0:
                stats['win_rate'] = round(stats['winning'] / stats['trades'], 4)
            stats['total_pnl'] = round(stats['total_pnl'], 2)
        
        return dict(strategy_stats)
    
    def analyze_by_factor(self) -> Dict:
        """按因子分析预测准确性"""
        factor_stats = {
            'ai_prediction': {'correct': 0, 'total': 0, 'avg_pnl': 0.0, 'total_pnl': 0.0},
            'capital_flow': {'correct': 0, 'total': 0, 'avg_pnl': 0.0, 'total_pnl': 0.0},
            'sentiment': {'correct': 0, 'total': 0, 'avg_pnl': 0.0, 'total_pnl': 0.0}
        }
        
        for record in self.records:
            if record.get('exit_price') is None:
                continue
            
            pnl = record.get('pnl', 0) or 0
            pnl_sign = 1 if pnl > 0 else -1
            
            # AI 预测准确性
            ai_score = record.get('ai_score', 0)
            if ai_score != 0:
                factor_stats['ai_prediction']['total'] += 1
                factor_stats['ai_prediction']['total_pnl'] += pnl
                
                # AI 预测方向与实际盈亏方向一致
                if (ai_score > 0 and pnl > 0) or (ai_score < 0 and pnl < 0):
                    factor_stats['ai_prediction']['correct'] += 1
            
            # 因子分数准确性
            factor_scores = record.get('factor_scores', {})
            for factor_name, factor_value in factor_scores.items():
                if factor_name in factor_stats and factor_value != 0:
                    factor_stats[factor_name]['total'] += 1
                    factor_stats[factor_name]['total_pnl'] += pnl
                    
                    if (factor_value > 0 and pnl > 0) or (factor_value < 0 and pnl < 0):
                        factor_stats[factor_name]['correct'] += 1
        
        # 计算准确率和平均盈亏
        for factor_name, stats in factor_stats.items():
            if stats['total'] > 0:
                stats['accuracy'] = round(stats['correct'] / stats['total'], 4)
                stats['avg_pnl'] = round(stats['total_pnl'] / stats['total'], 2)
            stats['total_pnl'] = round(stats['total_pnl'], 2)
        
        return factor_stats
    
    def analyze_by_market_regime(self) -> Dict:
        """按市场环境分析"""
        regime_stats = defaultdict(lambda: {
            'trades': 0,
            'winning': 0,
            'total_pnl': 0.0,
            'win_rate': 0.0,
            'avg_pnl': 0.0
        })
        
        for record in self.records:
            if record.get('exit_price') is None:
                continue
            
            regime = record.get('market_regime', 'unknown')
            pnl = record.get('pnl', 0) or 0
            
            regime_stats[regime]['trades'] += 1
            regime_stats[regime]['total_pnl'] += pnl
            
            if pnl > 0:
                regime_stats[regime]['winning'] += 1
        
        # 计算胜率和平均盈亏
        for regime, stats in regime_stats.items():
            if stats['trades'] > 0:
                stats['win_rate'] = round(stats['winning'] / stats['trades'], 4)
                stats['avg_pnl'] = round(stats['total_pnl'] / stats['trades'], 2)
            stats['total_pnl'] = round(stats['total_pnl'], 2)
        
        return dict(regime_stats)
    
    def analyze_ai_contribution(self) -> Dict:
        """分析 AI 贡献度"""
        ai_trades = []
        non_ai_trades = []
        
        for record in self.records:
            if record.get('exit_price') is None:
                continue
            
            strategy = record.get('strategy', '')
            pnl = record.get('pnl', 0) or 0
            
            if strategy == 'fusion_v3':
                ai_trades.append(pnl)
            else:
                non_ai_trades.append(pnl)
        
        # 计算 AI 带来的额外收益
        ai_avg = sum(ai_trades) / len(ai_trades) if ai_trades else 0
        non_ai_avg = sum(non_ai_trades) / len(non_ai_trades) if non_ai_trades else 0
        
        ai_contribution = ((ai_avg - non_ai_avg) / abs(non_ai_avg) * 100) if non_ai_avg != 0 else 0
        
        return {
            'ai_trades': len(ai_trades),
            'non_ai_trades': len(non_ai_trades),
            'ai_avg_pnl': round(ai_avg, 2),
            'non_ai_avg_pnl': round(non_ai_avg, 2),
            'ai_contribution_percent': round(ai_contribution, 2),
            'ai_total_pnl': round(sum(ai_trades), 2),
            'non_ai_total_pnl': round(sum(non_ai_trades), 2)
        }
    
    def generate_report(self, days: int = 7) -> Dict:
        """生成归因报告"""
        # 过滤最近 N 天的数据
        cutoff_date = datetime.now() - timedelta(days=days)
        recent_records = [
            r for r in self.records
            if r.get('timestamp', '') >= cutoff_date.strftime('%Y-%m-%d')
        ]
        
        if not recent_records:
            return {'error': f'最近{days}天无交易记录'}
        
        report = {
            'period': f'{cutoff_date.strftime("%Y-%m-%d")} ~ {datetime.now().strftime("%Y-%m-%d")}',
            'total_trades': len(recent_records),
            'closed_trades': len([r for r in recent_records if r.get('exit_price')]),
            'by_strategy': self.analyze_by_strategy(),
            'by_factor': self.analyze_by_factor(),
            'by_market_regime': self.analyze_by_market_regime(),
            'ai_contribution': self.analyze_ai_contribution(),
            'recommendations': self._generate_recommendations()
        }
        
        return report
    
    def _generate_recommendations(self) -> List[str]:
        """生成改进建议"""
        recommendations = []
        
        # 基于策略表现
        strategy_stats = self.analyze_by_strategy()
        if 'fusion_v3' in strategy_stats:
            v3_stats = strategy_stats['fusion_v3']
            if v3_stats['win_rate'] > 0.60:
                recommendations.append('✅ FusionV3 (AI 增强) 表现良好，可考虑增加 AI 权重')
            elif v3_stats['win_rate'] < 0.45:
                recommendations.append('⚠️ FusionV3 表现不佳，建议降低 AI 权重或检查 AI 预测质量')
        
        # 基于 AI 贡献
        ai_contrib = self.analyze_ai_contribution()
        if ai_contrib['ai_contribution_percent'] > 10:
            recommendations.append(f'✅ AI 带来 +{ai_contrib["ai_contribution_percent"]}% 额外收益')
        elif ai_contrib['ai_contribution_percent'] < -10:
            recommendations.append(f'⚠️ AI 拖累收益 {ai_contrib["ai_contribution_percent"]}%, 建议调整')
        
        # 基于市场环境
        regime_stats = self.analyze_by_market_regime()
        if 'downtrend' in regime_stats:
            if regime_stats['downtrend']['win_rate'] < 0.40:
                recommendations.append('⚠️ 下跌市场表现不佳，建议降低仓位或暂停交易')
        
        if not recommendations:
            recommendations.append('📊 继续按当前策略执行，保持监控')
        
        return recommendations


# 全局单例
_attribution = None

def get_performance_attribution() -> PerformanceAttribution:
    """获取收益归因分析器单例"""
    global _attribution
    if _attribution is None:
        _attribution = PerformanceAttribution()
    return _attribution
