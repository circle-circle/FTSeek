"""
信号质量相关性分析器 (Signal Quality Correlation Analysis)

分析信号分数与最终收益的相关性，回答：
1. 信号分数 vs 盈亏相关性（Pearson 相关系数）
2. 置信度 vs 胜率关系（是否存在"高置信度=低胜率"异常）
3. 识别高噪声信号（频率高但胜率低）
4. 识别过拟合信号（样本内优异，样本外差）
"""
import json
import logging
import math
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass
from collections import defaultdict

logger = logging.getLogger("ftseek.analytics.signal_correlation")
logger.propagate = False

# TradeChain 存储目录
TRADE_CHAIN_DIR = Path('/opt/ftseek/FTSeek/data/trade_chains')


@dataclass
class CorrelationResult:
    """相关性分析结果"""
    pearson_r: float  # Pearson 相关系数
    p_value: float    # p 值
    n_samples: int    # 样本数
    significance: str  # significant/moderate/weak
    scatter_data: List[Dict]  # 散点图数据

    def to_dict(self) -> Dict:
        return {
            'pearson_r': round(self.pearson_r, 4),
            'p_value': round(self.p_value, 6),
            'n_samples': self.n_samples,
            'significance': self.significance,
            'interpretation': self._interpret(),
        }

    def _interpret(self) -> str:
        """解释相关性"""
        if abs(self.pearson_r) >= 0.7:
            strength = "强"
        elif abs(self.pearson_r) >= 0.4:
            strength = "中等"
        else:
            strength = "弱"

        direction = "正相关" if self.pearson_r > 0 else "负相关"

        if self.p_value < 0.05:
            sig = "统计显著"
        elif self.p_value < 0.1:
            sig = "边缘显著"
        else:
            sig = "不显著"

        return f"{strength}{direction} ({sig}, p={self.p_value:.4f})"


class SignalQualityCorrelation:
    """
    信号质量相关性分析器

    核心分析:
    1. score_vs_pnl_correlation - 信号分数 vs 盈亏相关性
    2. confidence_vs_winrate - 置信度 vs 胜率关系
    3. identify_noise_signals - 识别高噪声信号
    4. identify_overfit_signals - 识别过拟合信号
    """

    def __init__(self):
        self.chains: List[Dict] = []
        self._load_all_chains()

    def _load_all_chains(self):
        """加载所有交易链路"""
        if not TRADE_CHAIN_DIR.exists():
            logger.warning(f"TradeChain 目录不存在：{TRADE_CHAIN_DIR}")
            return

        for file in TRADE_CHAIN_DIR.glob('*.json'):
            try:
                with open(file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.chains.extend(data)
            except Exception as e:
                logger.error(f"加载 TradeChain 文件失败 {file}: {e}")

        logger.info(f"加载 TradeChain: {len(self.chains)} 条")

    def _get_closed_chains(self) -> List[Dict]:
        """获取已平仓的交易链路"""
        return [c for c in self.chains if c.get('is_closed', False)]

    def _calculate_pearson_correlation(self, x: List[float], y: List[float]) -> Tuple[float, float]:
        """
        计算 Pearson 相关系数和 p 值

        Args:
            x: X 变量列表
            y: Y 变量列表

        Returns:
            (pearson_r, p_value)
        """
        n = len(x)
        if n < 3:
            return 0.0, 1.0

        # 计算均值
        mean_x = sum(x) / n
        mean_y = sum(y) / n

        # 计算协方差和标准差
        cov = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
        var_x = sum((x[i] - mean_x) ** 2 for i in range(n))
        var_y = sum((y[i] - mean_y) ** 2 for i in range(n))

        if var_x == 0 or var_y == 0:
            return 0.0, 1.0

        # Pearson 相关系数
        r = cov / math.sqrt(var_x * var_y)

        # p 值近似计算 (使用 t 分布)
        if abs(r) >= 1:
            p_value = 0.0 if abs(r) == 1 else 1.0
        else:
            t_stat = r * math.sqrt((n - 2) / (1 - r ** 2))
            # 简化 p 值计算 (使用正态近似)
            p_value = 2 * (1 - self._normal_cdf(abs(t_stat)))

        return r, p_value

    def _normal_cdf(self, x: float) -> float:
        """标准正态分布累积分布函数近似"""
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    def score_vs_pnl_correlation(self) -> CorrelationResult:
        """
        信号分数 vs 盈亏相关性分析

        Returns:
            CorrelationResult: 相关性分析结果
        """
        closed = self._get_closed_chains()

        scores = []
        pnls = []
        scatter_data = []

        for chain in closed:
            score = chain.get('signal', {}).get('signal_score', 0.0)
            pnl = chain.get('result', {}).get('pnl', 0.0)

            if score > 0 and pnl is not None:
                scores.append(score)
                pnls.append(pnl)
                scatter_data.append({
                    'score': score,
                    'pnl': pnl,
                    'code': chain.get('code', ''),
                    'exit_reason': chain.get('result', {}).get('exit_reason', ''),
                })

        if len(scores) < 3:
            return CorrelationResult(
                pearson_r=0.0,
                p_value=1.0,
                n_samples=len(scores),
                significance='insufficient_data',
                scatter_data=scatter_data
            )

        r, p_value = self._calculate_pearson_correlation(scores, pnls)

        # 判断显著性
        if p_value < 0.05:
            significance = 'significant'
        elif p_value < 0.1:
            significance = 'marginal'
        else:
            significance = 'not_significant'

        return CorrelationResult(
            pearson_r=r,
            p_value=p_value,
            n_samples=len(scores),
            significance=significance,
            scatter_data=scatter_data[:100]  # 限制散点图数据量
        )

    def confidence_vs_winrate(self) -> Dict[str, Any]:
        """
        置信度 vs 胜率关系分析

        分析各置信度区间的胜率，检测是否存在"高置信度=低胜率"的异常

        Returns:
            Dict: 各置信度区间的胜率分析
        """
        closed = self._get_closed_chains()

        # 按置信度分档
        confidence_buckets = {
            '0.0-0.3 (低置信)': [],
            '0.3-0.5 (中低置信)': [],
            '0.5-0.7 (中高置信)': [],
            '0.7-1.0 (高置信)': [],
        }

        for chain in closed:
            confidence = chain.get('signal', {}).get('signal_confidence', 0.0)
            pnl = chain.get('result', {}).get('pnl', 0.0)

            if confidence <= 0:
                continue

            if confidence < 0.3:
                bucket = '0.0-0.3 (低置信)'
            elif confidence < 0.5:
                bucket = '0.3-0.5 (中低置信)'
            elif confidence < 0.7:
                bucket = '0.5-0.7 (中高置信)'
            else:
                bucket = '0.7-1.0 (高置信)'

            confidence_buckets[bucket].append(1 if pnl > 0 else 0)

        # 计算各区间胜率
        result = {
            'buckets': {},
            'anomaly_detected': False,
            'anomaly_description': '',
        }

        winrates = []
        for bucket, wins in confidence_buckets.items():
            if len(wins) > 0:
                win_rate = sum(wins) / len(wins)
                result['buckets'][bucket] = {
                    'trades': len(wins),
                    'wins': sum(wins),
                    'win_rate': round(win_rate, 4),
                }
                winrates.append((bucket, win_rate))
            else:
                result['buckets'][bucket] = {
                    'trades': 0,
                    'wins': 0,
                    'win_rate': 0.0,
                }

        # 检测异常：高置信度区间胜率是否低于低置信度区间
        if len(winrates) >= 2:
            # 找出最高和最低置信度区间的胜率
            high_conf = next((b for b in winrates if '高置信' in b[0]), None)
            low_conf = next((b for b in winrates if '低置信' in b[0]), None)

            if high_conf and low_conf:
                if high_conf[1] < low_conf[1] * 0.8:  # 高置信胜率低于低置信的 80%
                    result['anomaly_detected'] = True
                    result['anomaly_description'] = (
                        f"高置信度区间胜率 ({high_conf[1]:.1%}) 显著低于低置信度区间 ({low_conf[1]:.1%})"
                    )

        return result

    def score_vs_pnl_by_strategy(self) -> Dict[str, CorrelationResult]:
        """
        按策略分组的信号分数 vs 盈亏相关性

        Returns:
            Dict[str, CorrelationResult]: 各策略的相关性
        """
        closed = self._get_closed_chains()
        strategy_data = defaultdict(lambda: {'scores': [], 'pnls': []})

        for chain in closed:
            strategy = chain.get('decision', {}).get('decision_reason', 'unknown')
            score = chain.get('signal', {}).get('signal_score', 0.0)
            pnl = chain.get('result', {}).get('pnl', 0.0)

            if score > 0 and pnl is not None:
                strategy_data[strategy]['scores'].append(score)
                strategy_data[strategy]['pnls'].append(pnl)

        results = {}
        for strategy, data in strategy_data.items():
            if len(data['scores']) < 3:
                results[strategy] = CorrelationResult(
                    pearson_r=0.0,
                    p_value=1.0,
                    n_samples=len(data['scores']),
                    significance='insufficient_data',
                    scatter_data=[]
                )
            else:
                r, p_value = self._calculate_pearson_correlation(data['scores'], data['pnls'])

                if p_value < 0.05:
                    significance = 'significant'
                elif p_value < 0.1:
                    significance = 'marginal'
                else:
                    significance = 'not_significant'

                results[strategy] = CorrelationResult(
                    pearson_r=r,
                    p_value=p_value,
                    n_samples=len(data['scores']),
                    significance=significance,
                    scatter_data=[]
                )

        return results

    def identify_noise_signals(self, min_freq: int = 3, max_score_pnl_corr: float = 0.1) -> List[Dict]:
        """
        识别高噪声信号

        标准:
        - 信号频率 >= min_freq
        - 信号分数与盈亏相关性 <= max_score_pnl_corr (弱相关或负相关)

        Args:
            min_freq: 最小出现次数
            max_score_pnl_corr: 最大相关性阈值

        Returns:
            List[Dict]: 噪声信号列表
        """
        closed = self._get_closed_chains()
        strategy_data = defaultdict(lambda: {
            'scores': [], 'pnls': [], 'wins': [], 'count': 0
        })

        for chain in closed:
            strategy = chain.get('decision', {}).get('decision_reason', 'unknown')
            score = chain.get('signal', {}).get('signal_score', 0.0)
            pnl = chain.get('result', {}).get('pnl', 0.0)

            if pnl is not None:
                strategy_data[strategy]['scores'].append(score)
                strategy_data[strategy]['pnls'].append(pnl)
                strategy_data[strategy]['wins'].append(1 if pnl > 0 else 0)
                strategy_data[strategy]['count'] += 1

        noise_signals = []
        for strategy, data in strategy_data.items():
            if data['count'] < min_freq:
                continue

            # 计算相关性和胜率
            r, _ = self._calculate_pearson_correlation(data['scores'], data['pnls'])
            win_rate = sum(data['wins']) / data['count']
            total_pnl = sum(data['pnls'])

            # 判断是否为噪声信号
            if abs(r) <= max_score_pnl_corr or (win_rate < 0.4 and total_pnl < 0):
                noise_signals.append({
                    'signal': strategy,
                    'total_trades': data['count'],
                    'win_rate': round(win_rate, 4),
                    'total_pnl': round(total_pnl, 2),
                    'score_pnl_correlation': round(r, 4),
                    'avg_score': round(sum(data['scores']) / len(data['scores']), 2),
                    'reason': '信号分数与盈亏无相关性' if abs(r) <= max_score_pnl_corr else '低胜率负收益',
                })

        return sorted(noise_signals, key=lambda x: x['total_pnl'])

    def identify_overfit_signals(self) -> List[Dict]:
        """
        识别过拟合信号

        标准:
        - 在特定市场状态过度集中 (>80%)
        - 在其他市场状态表现显著下降

        Returns:
            List[Dict]: 过拟合信号列表
        """
        closed = self._get_closed_chains()

        # 按策略×市场状态分组
        cross_data = defaultdict(lambda: {'pnls': [], 'scores': [], 'count': 0})
        strategy_total = defaultdict(lambda: {'pnls': [], 'count': 0})

        for chain in closed:
            strategy = chain.get('decision', {}).get('decision_reason', 'unknown')
            regime = chain.get('signal', {}).get('market_regime', 'unknown')
            result = chain.get('result', {})

            if not result:
                continue

            pnl = result.get('pnl', 0.0)
            score = chain.get('signal', {}).get('signal_score', 0.0)

            key = (strategy, regime)
            cross_data[key]['pnls'].append(pnl)
            cross_data[key]['scores'].append(score)
            cross_data[key]['count'] += 1

            strategy_total[strategy]['pnls'].append(pnl)
            strategy_total[strategy]['count'] += 1

        overfit_signals = []
        for strategy in strategy_total.keys():
            # 找出该策略在各市场状态的分布
            regime_counts = {}
            for (s, r), data in cross_data.items():
                if s == strategy:
                    regime_counts[r] = data['count']

            total = strategy_total[strategy]['count']
            if total < 5:
                continue

            # 检查是否有单一市场状态占比 > 80%
            max_regime = max(regime_counts, key=regime_counts.get)
            max_ratio = regime_counts[max_regime] / total

            if max_ratio > 0.8:
                # 计算集中市场状态 vs 其他市场状态的表现为对比
                concentrated_data = cross_data[(strategy, max_regime)]
                concentrated_avg_score = sum(concentrated_data['scores']) / len(concentrated_data['scores'])

                other_scores = []
                for (s, r), data in cross_data.items():
                    if s == strategy and r != max_regime:
                        other_scores.extend(data['scores'])

                other_avg_score = sum(other_scores) / len(other_scores) if other_scores else 0

                overfit_signals.append({
                    'signal': strategy,
                    'concentrated_regime': max_regime,
                    'concentration_ratio': round(max_ratio, 4),
                    'trades_in_regime': regime_counts[max_regime],
                    'total_trades': total,
                    'avg_score_in_regime': round(concentrated_avg_score, 2),
                    'avg_score_other': round(other_avg_score, 2),
                    'reason': f'过度集中于{max_regime} ({max_ratio:.0%})',
                })

        return sorted(overfit_signals, key=lambda x: x['concentration_ratio'], reverse=True)

    def get_full_report(self) -> Dict:
        """
        获取完整分析报告

        Returns:
            Dict: 包含所有分析的报告中
        """
        score_pnl = self.score_vs_pnl_correlation()
        confidence_winrate = self.confidence_vs_winrate()
        by_strategy = self.score_vs_pnl_by_strategy()
        noise = self.identify_noise_signals()
        overfit = self.identify_overfit_signals()

        return {
            'generated_at': datetime.now().isoformat(),
            'total_chains': len(self.chains),
            'closed_chains': len(self._get_closed_chains()),
            'score_pnl_correlation': score_pnl.to_dict(),
            'confidence_vs_winrate': confidence_winrate,
            'by_strategy': {k: v.to_dict() for k, v in by_strategy.items()},
            'noise_signals': noise,
            'overfit_signals': overfit,
        }


# 全局单例
_correlation: Optional[SignalQualityCorrelation] = None


def get_signal_quality_correlation() -> SignalQualityCorrelation:
    """获取信号质量相关性分析器单例"""
    global _correlation
    if _correlation is None:
        _correlation = SignalQualityCorrelation()
    return _correlation


if __name__ == '__main__':
    # 测试运行
    correlation = get_signal_quality_correlation()
    report = correlation.get_full_report()

    print("=" * 60)
    print("信号质量相关性分析报告")
    print("=" * 60)
    print(f"总交易数：{report['total_chains']}")
    print(f"已平仓数：{report['closed_chains']}")
    print()

    print("信号分数 vs 盈亏相关性:")
    corr = report['score_pnl_correlation']
    print(f"  Pearson r = {corr['pearson_r']:.4f}")
    print(f"  p-value = {corr['p_value']:.6f}")
    print(f"  样本数 = {corr['n_samples']}")
    print(f"  解释：{corr['interpretation']}")
    print()

    print("置信度 vs 胜率关系:")
    for bucket, data in report['confidence_vs_winrate']['buckets'].items():
        if data['trades'] > 0:
            print(f"  {bucket}: {data['trades']}笔，胜率={data['win_rate']:.1%}")
    if report['confidence_vs_winrate']['anomaly_detected']:
        print(f"  ⚠️ 异常检测：{report['confidence_vs_winrate']['anomaly_description']}")
    print()

    print("按策略相关性:")
    for strategy, corr in report['by_strategy'].items():
        print(f"  {strategy}: r={corr['pearson_r']:.4f} ({corr['interpretation']})")
    print()

    print("噪声信号:")
    for signal in report['noise_signals'][:5]:
        print(f"  {signal['signal']}: 胜率={signal['win_rate']:.1%}, 总盈亏={signal['total_pnl']:.2f} - {signal['reason']}")
    print()

    print("过拟合信号:")
    for signal in report['overfit_signals'][:5]:
        print(f"  {signal['signal']}: {signal['reason']}")
