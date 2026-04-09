"""
优化建议生成器 (Optimization Advisor)

根据归因分析结果自动生成优化建议，回答：
1. 哪些策略需要降权？（高波动下持续亏损）
2. 哪些信号需要 recalibrate？（高置信度=低胜率）
3. 哪些信号需要加入黑名单？（高频负收益）
4. 执行策略是否需要调整？（滑点过大）
"""
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("ftseek.analytics.optimization_advisor")
logger.propagate = False


class RecommendationType(str, Enum):
    """推荐类型"""
    DOWNWEIGHT = "downweight"           # 降权
    UPWEIGHT = "upweight"               # 加权
    BLACKLIST = "blacklist"             # 加入黑名单
    RECALIBRATE = "recalibrate"         # 重新校准
    ADJUST_EXECUTION = "adjust_execution"  # 调整执行策略
    ADJUST_RISK = "adjust_risk"         # 调整风控参数
    SUPPRESS = "suppress"               # 抑制信号


class RecommendationPriority(str, Enum):
    """推荐优先级"""
    CRITICAL = "critical"   #  critical - 需要立即处理
    HIGH = "high"           # 高优先级
    MEDIUM = "medium"       # 中等优先级
    LOW = "low"             # 低优先级


@dataclass
class Recommendation:
    """优化建议"""
    id: str
    type: RecommendationType
    priority: RecommendationPriority
    target: str             # 目标策略/信号/参数
    reason: str             # 原因描述
    suggestion: str         # 具体建议
    expected_impact: str    # 预期影响
    confidence: float       # 置信度 0-1
    data_support: Dict = field(default_factory=dict)  # 数据支持
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict:
        return {
            'id': self.id,
            'type': self.type.value,
            'priority': self.priority.value,
            'target': self.target,
            'reason': self.reason,
            'suggestion': self.suggestion,
            'expected_impact': self.expected_impact,
            'confidence': round(self.confidence, 2),
            'data_support': self.data_support,
            'created_at': self.created_at,
        }


class OptimizationAdvisor:
    """
    优化建议生成器

    核心规则:
    1. 如果某策略在高波动下持续亏损 → 建议降权
    2. 如果高置信度信号胜率反而低 → 建议调整评分逻辑
    3. 如果某信号频率高但收益为负 → 建议加入黑名单
    4. 如果滑点过大侵蚀利润 → 建议调整执行策略
    """

    def __init__(self):
        self.recommendations: List[Recommendation] = []

    def generate_recommendations(
        self,
        strategy_attribution: Dict,
        signal_correlation: Dict,
        execution_stats: Dict = None
    ) -> List[Recommendation]:
        """
        生成优化建议

        Args:
            strategy_attribution: 策略归因分析结果
            signal_correlation: 信号相关性分析结果
            execution_stats: 执行统计数据

        Returns:
            List[Recommendation]: 优化建议列表
        """
        self.recommendations = []

        # 规则 1: 检查各策略表现，生成降权/加权建议
        self._check_strategy_performance(strategy_attribution)

        # 规则 2: 检查置信度与胜率关系
        self._check_confidence_calibration(signal_correlation)

        # 规则 3: 检查噪声信号和过拟合信号
        self._check_noise_and_overfit(signal_correlation)

        # 规则 4: 检查执行质量
        if execution_stats:
            self._check_execution_quality(execution_stats)

        # 按优先级排序
        self.recommendations.sort(key=lambda r: self._priority_score(r.priority), reverse=True)

        logger.info(f"生成优化建议：{len(self.recommendations)} 条")
        return self.recommendations

    def _priority_score(self, priority: RecommendationPriority) -> int:
        """优先级打分"""
        scores = {
            RecommendationPriority.CRITICAL: 4,
            RecommendationPriority.HIGH: 3,
            RecommendationPriority.MEDIUM: 2,
            RecommendationPriority.LOW: 1,
        }
        return scores.get(priority, 0)

    def _check_strategy_performance(self, attribution: Dict) -> None:
        """
        检查策略表现，生成降权/加权建议

        规则:
        - 胜率 < 35% 且总盈亏 < 0 → 降权
        - 胜率 > 55% 且总盈亏 > 0 → 加权
        """
        by_strategy = attribution.get('by_strategy', {})

        for strategy, metrics in by_strategy.items():
            win_rate = metrics.get('win_rate', 0)
            total_pnl = metrics.get('total_pnl', 0)
            total_trades = metrics.get('total_trades', 0)

            if total_trades < 3:
                continue  # 样本太少

            # 降权建议
            if win_rate < 0.35 and total_pnl < 0:
                rec = Recommendation(
                    id=f"DOWNWEIGHT-{strategy[:10].upper()}",
                    type=RecommendationType.DOWNWEIGHT,
                    priority=RecommendationPriority.HIGH,
                    target=strategy,
                    reason=f"策略{strategy}在高波动下持续亏损",
                    suggestion="权重降低 20%",
                    expected_impact=f"减少亏损 {abs(total_pnl):.0f}",
                    confidence=0.8,
                    data_support={
                        'win_rate': win_rate,
                        'total_pnl': total_pnl,
                        'total_trades': total_trades,
                    }
                )
                self.recommendations.append(rec)

            # 加权建议
            elif win_rate > 0.55 and total_pnl > 0:
                rec = Recommendation(
                    id=f"UPWEIGHT-{strategy[:10].upper()}",
                    type=RecommendationType.UPWEIGHT,
                    priority=RecommendationPriority.MEDIUM,
                    target=strategy,
                    reason=f"策略{strategy}表现优异",
                    suggestion="权重提高 15%",
                    expected_impact=f"增加收益 {total_pnl * 0.15:.0f}",
                    confidence=0.7,
                    data_support={
                        'win_rate': win_rate,
                        'total_pnl': total_pnl,
                        'total_trades': total_trades,
                    }
                )
                self.recommendations.append(rec)

    def _check_confidence_calibration(self, correlation: Dict) -> None:
        """
        检查置信度校准

        规则:
        - 高置信度区间胜率 < 低置信度区间胜率的 80% → 重新校准
        """
        confidence_data = correlation.get('confidence_vs_winrate', {})
        anomaly = confidence_data.get('anomaly_detected', False)
        description = confidence_data.get('anomaly_description', '')

        if anomaly:
            buckets = confidence_data.get('buckets', {})
            high_conf = buckets.get('0.7-1.0 (高置信)', {})
            low_conf = buckets.get('0.0-0.3 (低置信)', {})

            rec = Recommendation(
                id="RECALIBRATE-CONFIDENCE",
                type=RecommendationType.RECALIBRATE,
                priority=RecommendationPriority.CRITICAL,
                target="confidence_scorer",
                reason=description or "置信度与胜率倒挂",
                suggestion="重新校准置信度计算逻辑，检查评分因子权重",
                expected_impact="提高高置信度信号的预测准确性",
                confidence=0.85,
                data_support={
                    'high_confidence_winrate': high_conf.get('win_rate', 0),
                    'low_confidence_winrate': low_conf.get('win_rate', 0),
                }
            )
            self.recommendations.append(rec)

    def _check_noise_and_overfit(self, correlation: Dict) -> None:
        """
        检查噪声信号和过拟合信号

        规则:
        - 信号频率高但收益为负 → 加入黑名单
        - 过度集中于特定市场状态 → 标记警告
        """
        # 噪声信号
        noise_signals = correlation.get('noise_signals', [])
        for signal in noise_signals[:3]:  # 最多处理前 3 个
            rec = Recommendation(
                id=f"BLACKLIST-{signal['signal'][:10].upper()}",
                type=RecommendationType.BLACKLIST,
                priority=RecommendationPriority.HIGH,
                target=signal['signal'],
                reason=signal['reason'],
                suggestion="加入信号黑名单，停止生成该信号",
                expected_impact=f"避免亏损 {abs(signal['total_pnl']):.0f}",
                confidence=0.75,
                data_support={
                    'total_trades': signal['total_trades'],
                    'win_rate': signal['win_rate'],
                    'total_pnl': signal['total_pnl'],
                }
            )
            self.recommendations.append(rec)

        # 过拟合信号
        overfit_signals = correlation.get('overfit_signals', [])
        for signal in overfit_signals[:3]:  # 最多处理前 3 个
            rec = Recommendation(
                id=f"SUPPRESS-{signal['signal'][:10].upper()}",
                type=RecommendationType.SUPPRESS,
                priority=RecommendationPriority.MEDIUM,
                target=signal['signal'],
                reason=signal['reason'],
                suggestion=f"在{signal['concentrated_regime']}以外的市场状态抑制该信号",
                expected_impact="提高信号在不同市场状态的泛化能力",
                confidence=0.7,
                data_support={
                    'concentration_ratio': signal['concentration_ratio'],
                    'trades_in_regime': signal['trades_in_regime'],
                    'total_trades': signal['total_trades'],
                }
            )
            self.recommendations.append(rec)

    def _check_execution_quality(self, execution_stats: Dict) -> None:
        """
        检查执行质量

        规则:
        - 平均滑点 > 20bps → 改用限价单
        - 平均延迟 > 500ms → 优化执行链路
        """
        avg_slippage = execution_stats.get('avg_slippage_bps', 0)
        avg_latency = execution_stats.get('avg_latency_ms', 0)

        if avg_slippage > 20:
            rec = Recommendation(
                id="ADJUST-EXECUTION-SLIPPAGE",
                type=RecommendationType.ADJUST_EXECUTION,
                priority=RecommendationPriority.HIGH,
                target="order_execution",
                reason=f"平均滑点过高 ({avg_slippage:.1f}bps)",
                suggestion="改用限价单，或调整滑点容忍度",
                expected_impact=f"减少滑点损失 {avg_slippage - 10:.0f}bps/笔",
                confidence=0.9,
                data_support={
                    'avg_slippage_bps': avg_slippage,
                }
            )
            self.recommendations.append(rec)

        if avg_latency > 500:
            rec = Recommendation(
                id="ADJUST-EXECUTION-LATENCY",
                type=RecommendationType.ADJUST_EXECUTION,
                priority=RecommendationPriority.MEDIUM,
                target="execution_pipeline",
                reason=f"执行延迟过高 ({avg_latency}ms)",
                suggestion="优化执行链路，减少中间环节",
                expected_impact=f"减少延迟 {avg_latency - 200}ms",
                confidence=0.8,
                data_support={
                    'avg_latency_ms': avg_latency,
                }
            )
            self.recommendations.append(rec)

    def get_summary(self) -> Dict:
        """
        获取建议摘要

        Returns:
            Dict: 摘要信息
        """
        by_type = {}
        by_priority = {}

        for rec in self.recommendations:
            by_type[rec.type.value] = by_type.get(rec.type.value, 0) + 1
            by_priority[rec.priority.value] = by_priority.get(rec.priority.value, 0) + 1

        return {
            'total_recommendations': len(self.recommendations),
            'by_type': by_type,
            'by_priority': by_priority,
            'top_recommendations': [r.to_dict() for r in self.recommendations[:5]],
        }


# 便捷函数
def generate_optimization_recommendations(
    strategy_attribution: Dict,
    signal_correlation: Dict,
    execution_stats: Dict = None
) -> List[Dict]:
    """
    便捷函数：生成优化建议

    使用示例:
        recommendations = generate_optimization_recommendations(
            strategy_attribution=attribution,
            signal_correlation=correlation,
            execution_stats=stats
        )
    """
    advisor = OptimizationAdvisor()
    recommendations = advisor.generate_recommendations(
        strategy_attribution,
        signal_correlation,
        execution_stats
    )
    return [r.to_dict() for r in recommendations]


if __name__ == '__main__':
    # 测试运行
    # 模拟输入数据
    test_attribution = {
        'by_strategy': {
            'technical': {
                'total_trades': 20,
                'win_rate': 0.30,
                'total_pnl': -500,
            },
            'sentiment': {
                'total_trades': 15,
                'win_rate': 0.60,
                'total_pnl': 800,
            },
            'confluence': {
                'total_trades': 30,
                'win_rate': 0.50,
                'total_pnl': 300,
            },
        }
    }

    test_correlation = {
        'confidence_vs_winrate': {
            'anomaly_detected': True,
            'anomaly_description': '高置信度区间胜率 (35%) 显著低于低置信度区间 (55%)',
            'buckets': {
                '0.7-1.0 (高置信)': {'win_rate': 0.35},
                '0.0-0.3 (低置信)': {'win_rate': 0.55},
            }
        },
        'noise_signals': [
            {
                'signal': 'impulse_buy',
                'total_trades': 10,
                'win_rate': 0.20,
                'total_pnl': -300,
                'reason': '高频负收益',
            }
        ],
        'overfit_signals': [
            {
                'signal': 'trend_following',
                'concentrated_regime': 'trending_up',
                'concentration_ratio': 0.85,
                'trades_in_regime': 17,
                'total_trades': 20,
                'reason': '过度集中于 trending_up',
            }
        ]
    }

    test_execution = {
        'avg_slippage_bps': 25,
        'avg_latency_ms': 600,
    }

    recommendations = generate_optimization_recommendations(
        test_attribution,
        test_correlation,
        test_execution
    )

    print("=" * 60)
    print("优化建议报告")
    print("=" * 60)

    for rec in recommendations:
        print(f"\n[{rec['priority'].upper()}] {rec['type']}: {rec['target']}")
        print(f"  原因：{rec['reason']}")
        print(f"  建议：{rec['suggestion']}")
        print(f"  预期影响：{rec['expected_impact']}")
        print(f"  置信度：{rec['confidence']:.0%}")
