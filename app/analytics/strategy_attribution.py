"""
策略维度归因分析器 (Strategy Attribution Analysis)

按策略×市场状态×信号强度分组统计，回答：
1. 哪些信号真正有效？（按策略/形态/市场状态排名）
2. 哪些信号看似强实际亏钱？（高频错误/高噪声/过拟合）
3. 收益和胜率由什么驱动？（方向判断/仓位控制/止损执行）
"""
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from collections import defaultdict

logger = logging.getLogger("ftseek.analytics.strategy_attribution")
logger.propagate = False

# TradeChain 存储目录
TRADE_CHAIN_DIR = Path('/opt/ftseek/FTSeek/data/trade_chains')


@dataclass
class StrategyMetrics:
    """策略维度指标"""
    strategy: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    avg_holding_minutes: int = 0
    avg_slippage_bps: float = 0.0

    def to_dict(self) -> Dict:
        return {
            'strategy': self.strategy,
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'total_pnl': round(self.total_pnl, 2),
            'avg_pnl': round(self.avg_pnl, 2),
            'win_rate': round(self.win_rate, 4),
            'avg_win': round(self.avg_win, 2),
            'avg_loss': round(self.avg_loss, 2),
            'profit_factor': round(self.profit_factor, 2),
            'sharpe_ratio': round(self.sharpe_ratio, 2),
            'max_drawdown': round(self.max_drawdown, 2),
            'avg_holding_minutes': self.avg_holding_minutes,
            'avg_slippage_bps': round(self.avg_slippage_bps, 2),
        }


@dataclass
class MarketRegimeMetrics:
    """市场状态维度指标"""
    regime: str
    total_trades: int = 0
    winning_trades: int = 0
    total_pnl: float = 0.0
    win_rate: float = 0.0
    avg_pnl: float = 0.0

    def to_dict(self) -> Dict:
        return {
            'regime': self.regime,
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'total_pnl': round(self.total_pnl, 2),
            'win_rate': round(self.win_rate, 4),
            'avg_pnl': round(self.avg_pnl, 2),
        }


@dataclass
class SignalStrengthMetrics:
    """信号强度维度指标"""
    strength_bucket: str  # 0.0-0.3, 0.3-0.5, 0.5-0.7, 0.7-1.0
    total_trades: int = 0
    winning_trades: int = 0
    total_pnl: float = 0.0
    win_rate: float = 0.0
    avg_pnl: float = 0.0
    avg_score: float = 0.0

    def to_dict(self) -> Dict:
        return {
            'strength_bucket': self.strength_bucket,
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'total_pnl': round(self.total_pnl, 2),
            'win_rate': round(self.win_rate, 4),
            'avg_pnl': round(self.avg_pnl, 2),
            'avg_score': round(self.avg_score, 2),
        }


class StrategyAttribution:
    """
    策略归因分析器

    核心分析维度:
    1. by_strategy - 按策略分组统计
    2. by_market_regime - 按市场状态分组统计
    3. by_signal_strength - 按信号强度分组统计
    4. by_strategy_and_regime - 策略×市场状态交叉分析
    5. identify_noise_signals - 识别高噪声信号
    6. identify_overfit_signals - 识别过拟合信号
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
                logger.debug(f"跳过 TradeChain 文件 {file}: {e}")

        logger.info(f"加载 TradeChain: {len(self.chains)} 条")

    def _get_closed_chains(self) -> List[Dict]:
        """获取已平仓的交易链路"""
        return [c for c in self.chains if c.get('is_closed', False)]

    def by_strategy(self) -> Dict[str, StrategyMetrics]:
        """
        按策略分组统计

        Returns:
            Dict[str, StrategyMetrics]: 各策略的指标
        """
        closed = self._get_closed_chains()
        strategy_data = defaultdict(lambda: {
            'trades': [],
            'pnls': [],
            'wins': [],
            'losses': [],
            'slippages': [],
            'holding_times': [],
        })

        for chain in closed:
            strategy = chain.get('decision', {}).get('decision_reason', 'unknown')
            result = chain.get('result', {})

            if not result:
                continue

            pnl = result.get('pnl', 0.0)
            is_win = result.get('pnl', 0.0) > 0

            strategy_data[strategy]['trades'].append(chain)
            strategy_data[strategy]['pnls'].append(pnl)
            if is_win:
                strategy_data[strategy]['wins'].append(pnl)
            else:
                strategy_data[strategy]['losses'].append(pnl)

            execution = chain.get('execution', {})
            strategy_data[strategy]['slippages'].append(execution.get('slippage_bps', 0.0))
            strategy_data[strategy]['holding_times'].append(
                result.get('holding_period_minutes', 0)
            )

        # 计算指标
        metrics = {}
        for strategy, data in strategy_data.items():
            m = StrategyMetrics(strategy=strategy)
            m.total_trades = len(data['trades'])
            m.total_pnl = sum(data['pnls'])
            m.avg_pnl = m.total_pnl / m.total_trades if m.total_trades > 0 else 0.0
            m.winning_trades = len(data['wins'])
            m.losing_trades = len(data['losses'])
            m.win_rate = m.winning_trades / m.total_trades if m.total_trades > 0 else 0.0
            m.avg_win = sum(data['wins']) / len(data['wins']) if data['wins'] else 0.0
            m.avg_loss = sum(data['losses']) / len(data['losses']) if data['losses'] else 0.0

            # 盈亏比
            if abs(m.avg_loss) > 0:
                m.profit_factor = abs(m.avg_win / m.avg_loss)

            # Sharpe 比率 (简化版)
            if len(data['pnls']) > 1:
                import statistics
                try:
                    sharpe = statistics.mean(data['pnls']) / (statistics.stdev(data['pnls']) + 0.001)
                    m.sharpe_ratio = sharpe
                except (statistics.StatisticsError, ZeroDivisionError, KeyError) as e:
                    logger.debug(f"Sharpe 比率计算失败：{e}")
                    m.sharpe_ratio = 0.0

            # 平均持仓时间
            m.avg_holding_minutes = int(sum(data['holding_times']) / len(data['holding_times'])) if data['holding_times'] else 0

            # 平均滑点
            m.avg_slippage_bps = sum(data['slippages']) / len(data['slippages']) if data['slippages'] else 0.0

            metrics[strategy] = m

        return metrics

    def by_market_regime(self) -> Dict[str, MarketRegimeMetrics]:
        """
        按市场状态分组统计

        Returns:
            Dict[str, MarketRegimeMetrics]: 各市场状态的指标
        """
        closed = self._get_closed_chains()
        regime_data = defaultdict(lambda: {'trades': [], 'pnls': [], 'wins': []})

        for chain in closed:
            regime = chain.get('signal', {}).get('market_regime', 'unknown')
            result = chain.get('result', {})

            if not result:
                continue

            pnl = result.get('pnl', 0.0)
            is_win = result.get('pnl', 0.0) > 0

            regime_data[regime]['trades'].append(chain)
            regime_data[regime]['pnls'].append(pnl)
            if is_win:
                regime_data[regime]['wins'].append(1)
            else:
                regime_data[regime]['wins'].append(0)

        # 计算指标
        metrics = {}
        for regime, data in regime_data.items():
            m = MarketRegimeMetrics(regime=regime)
            m.total_trades = len(data['trades'])
            m.total_pnl = sum(data['pnls'])
            m.avg_pnl = m.total_pnl / m.total_trades if m.total_trades > 0 else 0.0
            m.winning_trades = sum(data['wins'])
            m.win_rate = m.winning_trades / m.total_trades if m.total_trades > 0 else 0.0

            metrics[regime] = m

        return metrics

    def by_signal_strength(self) -> Dict[str, SignalStrengthMetrics]:
        """
        按信号强度分组统计

        强度分档:
        - 0.0-0.3: 弱信号
        - 0.3-0.5: 中等偏弱
        - 0.5-0.7: 中等偏强
        - 0.7-1.0: 强信号

        Returns:
            Dict[str, SignalStrengthMetrics]: 各强度区间的指标
        """
        closed = self._get_closed_chains()

        def get_bucket(score: float) -> str:
            if score < 0.3:
                return "0.0-0.3 (弱信号)"
            elif score < 0.5:
                return "0.3-0.5 (中等偏弱)"
            elif score < 0.7:
                return "0.5-0.7 (中等偏强)"
            else:
                return "0.7-1.0 (强信号)"

        bucket_data = defaultdict(lambda: {'trades': [], 'pnls': [], 'wins': [], 'scores': []})

        for chain in closed:
            score = chain.get('signal', {}).get('signal_score', 0.0)
            bucket = get_bucket(score)
            result = chain.get('result', {})

            if not result:
                continue

            pnl = result.get('pnl', 0.0)
            is_win = result.get('pnl', 0.0) > 0

            bucket_data[bucket]['trades'].append(chain)
            bucket_data[bucket]['pnls'].append(pnl)
            bucket_data[bucket]['wins'].append(1 if is_win else 0)
            bucket_data[bucket]['scores'].append(score)

        # 计算指标
        metrics = {}
        for bucket, data in bucket_data.items():
            m = SignalStrengthMetrics(strength_bucket=bucket)
            m.total_trades = len(data['trades'])
            m.total_pnl = sum(data['pnls'])
            m.avg_pnl = m.total_pnl / m.total_trades if m.total_trades > 0 else 0.0
            m.winning_trades = sum(data['wins'])
            m.win_rate = m.winning_trades / m.total_trades if m.total_trades > 0 else 0.0
            m.avg_score = sum(data['scores']) / len(data['scores']) if data['scores'] else 0.0

            metrics[bucket] = m

        return metrics

    def by_strategy_and_regime(self) -> Dict[Tuple[str, str], Dict]:
        """
        策略×市场状态 交叉分析

        Returns:
            Dict[Tuple[str, str], Dict]: (策略，市场状态) -> 指标
        """
        closed = self._get_closed_chains()
        cross_data = defaultdict(lambda: {'trades': [], 'pnls': [], 'wins': []})

        for chain in closed:
            strategy = chain.get('decision', {}).get('decision_reason', 'unknown')
            regime = chain.get('signal', {}).get('market_regime', 'unknown')
            result = chain.get('result', {})

            if not result:
                continue

            pnl = result.get('pnl', 0.0)
            is_win = result.get('pnl', 0.0) > 0

            key = (strategy, regime)
            cross_data[key]['trades'].append(chain)
            cross_data[key]['pnls'].append(pnl)
            if is_win:
                cross_data[key]['wins'].append(1)
            else:
                cross_data[key]['wins'].append(0)

        # 计算指标
        metrics = {}
        for (strategy, regime), data in cross_data.items():
            total_trades = len(data['trades'])
            total_pnl = sum(data['pnls'])
            winning_trades = sum(data['wins'])
            win_rate = winning_trades / total_trades if total_trades > 0 else 0.0

            metrics[(strategy, regime)] = {
                'strategy': strategy,
                'regime': regime,
                'total_trades': total_trades,
                'winning_trades': winning_trades,
                'total_pnl': round(total_pnl, 2),
                'win_rate': round(win_rate, 4),
                'avg_pnl': round(total_pnl / total_trades, 2) if total_trades > 0 else 0.0,
            }

        return metrics

    def identify_noise_signals(self, min_freq: int = 3, max_win_rate: float = 0.35) -> List[Dict]:
        """
        识别高噪声信号

        标准:
        - 信号频率 >= min_freq
        - 胜率 <= max_win_rate

        Args:
            min_freq: 最小出现次数
            max_win_rate: 最大胜率阈值

        Returns:
            List[Dict]: 噪声信号列表
        """
        closed = self._get_closed_chains()
        signal_data = defaultdict(lambda: {'pnls': [], 'wins': []})

        for chain in closed:
            # 使用 strategy 作为信号标识
            signal = chain.get('decision', {}).get('decision_reason', 'unknown')
            result = chain.get('result', {})

            if not result:
                continue

            pnl = result.get('pnl', 0.0)
            is_win = pnl > 0

            signal_data[signal]['pnls'].append(pnl)
            signal_data[signal]['wins'].append(1 if is_win else 0)

        noise_signals = []
        for signal, data in signal_data.items():
            total = len(data['pnls'])
            if total < min_freq:
                continue

            win_rate = sum(data['wins']) / total
            total_pnl = sum(data['pnls'])

            if win_rate <= max_win_rate:
                noise_signals.append({
                    'signal': signal,
                    'total_trades': total,
                    'win_rate': round(win_rate, 4),
                    'total_pnl': round(total_pnl, 2),
                    'avg_pnl': round(total_pnl / total, 2),
                    'reason': f'高频低胜率 (win_rate={win_rate:.2%})',
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
        cross_data = defaultdict(lambda: {'pnls': [], 'count': 0})
        strategy_total = defaultdict(lambda: {'pnls': [], 'count': 0})

        for chain in closed:
            strategy = chain.get('decision', {}).get('decision_reason', 'unknown')
            regime = chain.get('signal', {}).get('market_regime', 'unknown')
            result = chain.get('result', {})

            if not result:
                continue

            pnl = result.get('pnl', 0.0)

            key = (strategy, regime)
            cross_data[key]['pnls'].append(pnl)
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
            if total < 5:  # 样本太少，跳过
                continue

            # 检查是否有单一市场状态占比 > 80%
            max_regime = max(regime_counts, key=regime_counts.get)
            max_ratio = regime_counts[max_regime] / total

            if max_ratio > 0.8:
                # 计算集中市场状态 vs 其他市场状态的表现为对比
                concentrated_pnl = sum(cross_data[(strategy, max_regime)]['pnls'])
                other_pnls = []
                for (s, r), data in cross_data.items():
                    if s == strategy and r != max_regime:
                        other_pnls.extend(data['pnls'])

                other_total = sum(other_pnls) if other_pnls else 0
                concentrated_avg = concentrated_pnl / regime_counts[max_regime]
                other_avg = other_total / len(other_pnls) if other_pnls else 0

                overfit_signals.append({
                    'signal': strategy,
                    'concentrated_regime': max_regime,
                    'concentration_ratio': round(max_ratio, 4),
                    'trades_in_regime': regime_counts[max_regime],
                    'total_trades': total,
                    'avg_pnl_in_regime': round(concentrated_avg, 2),
                    'avg_pnl_other': round(other_avg, 2),
                    'reason': f'过度集中于{max_regime} ({max_ratio:.0%})',
                })

        return sorted(overfit_signals, key=lambda x: x['concentration_ratio'], reverse=True)

    def get_full_report(self) -> Dict:
        """
        获取完整归因报告

        Returns:
            Dict: 包含所有维度的分析报告
        """
        return {
            'generated_at': datetime.now().isoformat(),
            'total_chains': len(self.chains),
            'closed_chains': len(self._get_closed_chains()),
            'by_strategy': {k: v.to_dict() for k, v in self.by_strategy().items()},
            'by_market_regime': {k: v.to_dict() for k, v in self.by_market_regime().items()},
            'by_signal_strength': {k: v.to_dict() for k, v in self.by_signal_strength().items()},
            'strategy_regime_cross': self.by_strategy_and_regime(),
            'noise_signals': self.identify_noise_signals(),
            'overfit_signals': self.identify_overfit_signals(),
        }


# 全局单例
_attribution: Optional[StrategyAttribution] = None


def get_strategy_attribution() -> StrategyAttribution:
    """获取策略归因分析器单例"""
    global _attribution
    if _attribution is None:
        _attribution = StrategyAttribution()
    return _attribution


if __name__ == '__main__':
    # 测试运行
    attribution = get_strategy_attribution()
    report = attribution.get_full_report()

    print("=" * 60)
    print("策略归因分析报告")
    print("=" * 60)
    print(f"总交易数：{report['total_chains']}")
    print(f"已平仓数：{report['closed_chains']}")
    print()

    print("按策略分组:")
    for strategy, metrics in report['by_strategy'].items():
        print(f"  {strategy}: {metrics['total_trades']}笔，胜率={metrics['win_rate']:.1%}, 总盈亏={metrics['total_pnl']:.2f}")

    print()
    print("按市场状态分组:")
    for regime, metrics in report['by_market_regime'].items():
        print(f"  {regime}: {metrics['total_trades']}笔，胜率={metrics['win_rate']:.1%}, 总盈亏={metrics['total_pnl']:.2f}")

    print()
    print("按信号强度分组:")
    for bucket, metrics in report['by_signal_strength'].items():
        print(f"  {bucket}: {metrics['total_trades']}笔，胜率={metrics['win_rate']:.1%}, 总盈亏={metrics['total_pnl']:.2f}")

    print()
    print("噪声信号:")
    for signal in report['noise_signals'][:5]:
        print(f"  {signal['signal']}: 胜率={signal['win_rate']:.1%}, 总盈亏={signal['total_pnl']:.2f} - {signal['reason']}")

    print()
    print("过拟合信号:")
    for signal in report['overfit_signals'][:5]:
        print(f"  {signal['signal']}: {signal['reason']}")
