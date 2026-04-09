"""权重计算器 - V2 增强版 (支持自动权重调整)

2026-04-06 P0-3 优化:
1. 添加基于胜率的自动权重调整
2. 与 AdaptiveWeightManager 集成
3. 提供权重更新 API

2026-04-09 归因系统集成:
4. 集成归因引擎的权重建议
5. 策略健康度动态调整
"""
import logging
from typing import Dict, Optional, List
from datetime import datetime, timedelta

logger = logging.getLogger("ftseek.consensus.weights")

# 策略分类
TREND_STRATEGIES = {"supertrend", "macd", "ma_cross"}
OSCILLATOR_STRATEGIES = {"rsi", "bollinger"}
HYBRID_STRATEGIES = {"confluence_v2", "fusion_v2"}
SENTIMENT_STRATEGIES = {"sentiment_ai", "sentiment"}

# 基准权重
DEFAULT_WEIGHTS = {
    "confluence_v2": 1.5,
    "supertrend": 1.0,
    "sentiment_ai": 0.5,
}

# P0-3: 调整边界
MAX_BOOST = 1.30  # 最多上调 30%
MAX_DECAY = 0.70  # 最多下调 30%
MIN_TRADES = 5    # 最少交易笔数

# 归因引擎集成开关
ATTRIBUTION_ENABLED = True


class WeightCalculator:
    """权重计算器 V2 (支持自动调整)"""

    def __init__(self):
        self.base_weights = DEFAULT_WEIGHTS.copy()
        # P0-3: 策略表现追踪
        self._strategy_stats: Dict[str, List[Dict]] = {}
        self._last_update: Optional[datetime] = None

        # 2026-04-09 归因引擎集成
        self._attribution_engine = None
        if ATTRIBUTION_ENABLED:
            try:
                from app.analytics.attribution_engine import get_attribution_engine
                self._attribution_engine = get_attribution_engine()
                logger.info("[权重计算器] 归因引擎已启用")
            except Exception as e:
                logger.warning(f"[权重计算器] 归因引擎初始化失败：{e}")
                self._attribution_engine = None

    def calculate_weight(self, strategy: str, market_regime: Optional[str] = None) -> float:
        """
        计算策略权重

        Args:
            strategy: 策略名称
            market_regime: 市场状态 ("trending" / "ranging" / None)

        Returns:
            权重值
        """
        base = self.base_weights.get(strategy, 1.0)

        # 市场状态调整
        if market_regime == "trending":
            if strategy in TREND_STRATEGIES:
                base *= 1.5
            elif strategy in OSCILLATOR_STRATEGIES:
                base *= 0.6
        elif market_regime == "ranging":
            if strategy in OSCILLATOR_STRATEGIES:
                base *= 1.5
            elif strategy in TREND_STRATEGIES:
                base *= 0.6

        # 2026-04-09 归因引擎集成：应用归因建议权重
        if ATTRIBUTION_ENABLED and self._attribution_engine is not None:
            try:
                recommendations = self._attribution_engine.get_weight_recommendations(days=30)
                if strategy in recommendations:
                    rec_weight = recommendations[strategy]
                    # 平滑过渡：基础权重和归因建议权重的加权平均
                    attribution_factor = 0.4  # 归因建议权重占比 40%
                    adjusted_base = base * (1 - attribution_factor) + rec_weight * attribution_factor
                    logger.debug(f"[{strategy}] 权重调整：{base:.2f} → {adjusted_base:.2f} (归因建议={rec_weight:.3f})")
                    base = adjusted_base
            except Exception as e:
                logger.debug(f"[{strategy}] 归因权重调整失败：{e}")

        logger.debug(f"[{strategy}] 权重：{base:.2f} (市场状态：{market_regime})")
        return base

    def update_weights(self, new_weights: Dict[str, float]):
        """更新基准权重"""
        self.base_weights.update(new_weights)
        logger.info(f"权重已更新：{new_weights}")

    # =====================================================================
    # P0-3: 自动权重调整方法 (2026-04-06 新增)
    # =====================================================================
    def record_trade(self, strategy: str, pnl: float, market_regime: str = "neutral"):
        """
        记录交易结果，用于权重调整

        Args:
            strategy: 策略名称
            pnl: 盈亏百分比
            market_regime: 市场状态
        """
        if strategy not in self._strategy_stats:
            self._strategy_stats[strategy] = []

        self._strategy_stats[strategy].append({
            'pnl': pnl,
            'regime': market_regime,
            'timestamp': datetime.now(),
        })

        # 清理旧数据 (保留 30 天)
        cutoff = datetime.now() - timedelta(days=30)
        self._strategy_stats[strategy] = [
            r for r in self._strategy_stats[strategy] if r['timestamp'] > cutoff
        ]

    def update_weights_from_performance(self, min_trades: int = MIN_TRADES) -> Dict[str, Dict]:
        """
        根据历史表现自动更新权重

        Args:
            min_trades: 最少交易笔数

        Returns:
            更新的权重详情
        """
        updated = {}

        for strategy, records in self._strategy_stats.items():
            if len(records) < min_trades:
                logger.debug(f"[{strategy}] 交易笔数不足 ({len(records)} < {min_trades})，跳过")
                continue

            # 计算胜率
            wins = sum(1 for r in records if r['pnl'] > 0)
            win_rate = wins / len(records)

            # 计算平均盈亏
            avg_pnl = sum(r['pnl'] for r in records) / len(records)

            # 线性映射：胜率 0% → 0.7x, 50% → 1.0x, 100% → 1.3x
            base_multiplier = MAX_DECAY + (MAX_BOOST - MAX_DECAY) * win_rate

            # 限制调整范围
            multiplier = max(MAX_DECAY, min(MAX_BOOST, base_multiplier))

            # 计算新权重
            old_weight = self.base_weights.get(strategy, 1.0)
            new_weight = round(old_weight * multiplier, 3)

            self.base_weights[strategy] = new_weight
            updated[strategy] = {
                'old': old_weight,
                'new': new_weight,
                'win_rate': round(win_rate, 3),
                'avg_pnl': round(avg_pnl, 4),
                'trades': len(records),
            }

            logger.info(
                f"[权重调整] {strategy}: {old_weight:.2f} → {new_weight:.2f} "
                f"(胜率{win_rate:.0%}, 平均盈亏{avg_pnl:.2%}, {len(records)}笔)"
            )

        self._last_update = datetime.now()
        return updated

    def get_strategy_performance(self) -> Dict[str, Dict]:
        """获取策略表现统计"""
        stats = {}
        cutoff = datetime.now() - timedelta(days=30)

        for strategy, records in self._strategy_stats.items():
            recent = [r for r in records if r['timestamp'] > cutoff]
            if not recent:
                continue

            wins = sum(1 for r in recent if r['pnl'] > 0)
            total_pnl = sum(r['pnl'] for r in recent)

            stats[strategy] = {
                'trades': len(recent),
                'wins': wins,
                'win_rate': round(wins / len(recent), 3) if recent else 0,
                'avg_pnl': round(total_pnl / len(recent), 4) if recent else 0,
                'total_pnl': round(total_pnl, 4),
                'current_weight': self.base_weights.get(strategy, 1.0),
            }

        return stats

    def sync_attribution_weights(self, days: int = 30) -> Dict[str, Dict]:
        """
        同步归因引擎的权重建议 (2026-04-09 新增)

        Args:
            days: 分析天数范围

        Returns:
            权重调整详情
        """
        if not ATTRIBUTION_ENABLED or self._attribution_engine is None:
            logger.debug("[权重同步] 归因引擎未启用")
            return {}

        try:
            recommendations = self._attribution_engine.get_weight_recommendations(days=days)

            if not recommendations:
                logger.info("[权重同步] 无数据")
                return {}

            updated = {}
            for strategy, rec_weight in recommendations.items():
                old_weight = self.base_weights.get(strategy, 1.0)

                # 平滑过渡：避免权重剧烈变化
                # 新权重 = 旧权重 * 0.8 + 建议权重 * 0.2
                new_weight = old_weight * 0.8 + rec_weight * 0.2

                # 限制调整幅度不超过 20%
                max_change = 0.2
                if abs(new_weight - old_weight) > max_change:
                    if new_weight > old_weight:
                        new_weight = old_weight + max_change
                    else:
                        new_weight = old_weight - max_change

                self.base_weights[strategy] = round(new_weight, 3)
                updated[strategy] = {
                    'old': old_weight,
                    'new': new_weight,
                    'recommendation': rec_weight,
                }

            logger.info(f"[权重同步] 已更新 {len(updated)} 个策略权重")
            for strategy, change in updated.items():
                logger.info(
                    f"  {strategy}: {change['old']:.3f} → {change['new']:.3f} "
                    f"(建议={change['recommendation']:.3f})"
                )

            self._last_update = datetime.now()
            return updated

        except Exception as e:
            logger.error(f"[权重同步] 失败：{e}")
            return {}
