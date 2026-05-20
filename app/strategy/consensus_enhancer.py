"""
共识增强模块 (Consensus Enhancer) - Phase 1+3 集成

功能:
- 信号质量评分集成 (Phase 1)
- 情绪时效性衰减集成 (Phase 3)
- 综合质量评估 (质量分 + 时效分)
"""
import logging
from typing import Dict, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass

logger = logging.getLogger("ftseek.consensus.enhancer")
logger.propagate = False


@dataclass
class EnhancedSignal:
    """增强信号 (质量 + 时效 + LLM定性判断)"""
    code: str
    action: str                    # 'BUY' / 'SELL'
    original_score: float          # 原始共识分
    quality_score: float           # 信号质量分 (Phase 1)
    timeliness_factor: float       # 时效性因子 (Phase 3)
    llm_signal_impact_factor: float  # LLM signal_impact 乘数 (2026-05-17)
    final_score: float             # 最终得分
    news_age_minutes: int          # 新闻平均年龄
    volatility_state: str          # 市场波动状态
    recommendation: str            # push / delay / suppress


class ConsensusEnhancer:
    """
    共识增强器

    集成 Phase 1 (信号质量) 和 Phase 3 (时效性)
    """

    def __init__(self, config: Dict = None):
        """
        初始化增强器

        Args:
            config: 配置字典
        """
        self.config = config or {}

        # 质量阈值 (Phase 1)
        self.quality_threshold = self.config.get('quality_threshold', 0.30)  # 2026-04-14 对齐 unified_config.yaml
        self.high_quality_threshold = self.config.get('high_quality_threshold', 0.75)

        # 时效性配置 (Phase 3)
        self.max_news_age = self.config.get('max_news_age', 120)  # 120 分钟
        self.decay_rate = self.config.get('decay_rate', 0.05)    # 每分钟 5% 衰减

        # 缓存
        self._quality_cache: Dict[str, Tuple[float, datetime]] = {}
        self._timeliness_cache: Dict[str, Tuple[float, datetime]] = {}

        # 组件引用 (延迟初始化)
        self._signal_scorer = None
        self._decay_calculator = None
        self._vix_monitor = None

        logger.info("共识增强器已初始化")

    @property
    def signal_scorer(self):
        """延迟加载 SignalScorer"""
        if self._signal_scorer is None:
            try:
                from app.quality import SignalScorer
                self._signal_scorer = SignalScorer()
                logger.info("SignalScorer 已加载")
            except ImportError as e:
                logger.warning(f"SignalScorer 加载失败：{e}")
                return None
        return self._signal_scorer

    @property
    def decay_calculator(self):
        """延迟加载 SentimentDecay"""
        if self._decay_calculator is None:
            try:
                from app.factors.sentiment_decay import get_decay_calculator
                self._decay_calculator = get_decay_calculator(
                    mode="exponential",
                    half_life_minutes=30
                )
                logger.info("SentimentDecay 已加载")
            except ImportError as e:
                logger.warning(f"SentimentDecay 加载失败：{e}")
                return None
        return self._decay_calculator

    @property
    def vix_monitor(self):
        """延迟加载 VIXMonitor"""
        if self._vix_monitor is None:
            try:
                from app.risk.vix_monitor import get_vix_monitor
                self._vix_monitor = get_vix_monitor()
                logger.info("VIXMonitor 已加载")
            except ImportError as e:
                logger.warning(f"VIXMonitor 加载失败：{e}")
                return None
        return self._vix_monitor

    def enhance_signal(self, code: str, consensus_result) -> EnhancedSignal:
        """
        增强信号评估

        Args:
            code: 股票代码
            consensus_result: ConsensusResult 对象或 dict

        Returns:
            EnhancedSignal
        """
        # 1. 获取信号质量分 (Phase 1)
        quality_score = self._get_quality_score(code, consensus_result)

        # 2. 获取时效性因子 (Phase 3)
        timeliness_factor, news_age, vol_state = self._get_timeliness_factor(code)

        # 3. 计算最终得分 (兼容 dict 和对象)
        if isinstance(consensus_result, dict):
            original_score = consensus_result.get('score', consensus_result.get('probability', 0.5))
            action = consensus_result.get('action', 'BUY')
        else:
            original_score = getattr(consensus_result, 'score', getattr(consensus_result, 'probability', 0.5))
            action = getattr(consensus_result, 'action', 'BUY')
            if hasattr(action, 'value'):
                action = action.value

        final_score = original_score * quality_score * timeliness_factor

        # 3.5 LLM signal_impact 调整 (2026-05-17 新增)
        # 从动态池读取 LLM 对标的的定性判断，作为最终得分的乘数
        llm_factor = self._get_llm_signal_impact_factor(code)
        final_score *= llm_factor

        # 4. 给出推送建议
        recommendation = self._get_recommendation(quality_score, timeliness_factor, final_score)

        return EnhancedSignal(
            code=code,
            action=action if isinstance(action, str) else getattr(action, 'value', 'BUY'),
            original_score=original_score,
            quality_score=quality_score,
            timeliness_factor=timeliness_factor,
            llm_signal_impact_factor=llm_factor,
            final_score=final_score,
            news_age_minutes=news_age,
            volatility_state=vol_state,
            recommendation=recommendation,
        )

    def _get_quality_score(self, code: str, consensus_result) -> float:
        """
        获取信号质量分 (Phase 1)

        Args:
            code: 股票代码
            consensus_result: ConsensusResult 对象或 dict

        Returns:
            质量分 (0-1)
        """
        # 尝试从 SignalScorer 获取
        if self.signal_scorer:
            try:
                # 构建模拟信号对象 (兼容 dict 和对象)
                from app.strategy.base import Signal, SignalAction

                if isinstance(consensus_result, dict):
                    price = consensus_result.get('price', 0.0)
                    action_str = consensus_result.get('action', 'BUY')
                    confidence = consensus_result.get('confidence', 0.0)
                    reasons = consensus_result.get('reasons', [])
                    strategies = consensus_result.get('agree_strategies', [])
                    regime = consensus_result.get('market_regime', 'neutral')
                else:
                    price = getattr(consensus_result, 'price', 0.0)
                    action_str = getattr(consensus_result, 'action', SignalAction.BUY)
                    if hasattr(action_str, 'value'):
                        action_str = action_str.value
                    confidence = getattr(consensus_result, 'confidence', 0.0)
                    reasons = getattr(consensus_result, 'reasons', [])
                    strategies = getattr(consensus_result, 'agree_strategies', [])
                    regime = getattr(consensus_result, 'market_regime', 'neutral')

                signal = Signal(
                    code=code,
                    action=SignalAction.BUY if action_str == 'BUY' else SignalAction.SELL,
                    price=price,
                    reason=reasons[0] if reasons else "",
                    strategy=strategies[0] if strategies else "unknown",
                    confidence=confidence,
                )

                # 获取市场状态
                market_regime = self._map_regime(regime)

                # 评估质量
                quality = self.signal_scorer.evaluate(signal, market_regime)
                return quality.score

            except Exception as e:
                logger.debug(f"获取质量分失败：{e}")

        # 降级：使用 ConsensusEngine 的质量分
        if isinstance(consensus_result, dict):
            conf = consensus_result.get('confidence', 0.0)
        else:
            conf = getattr(consensus_result, 'confidence', 0.0)

        if conf > 0:
            # 置信度映射为质量分
            return min(1.0, conf * 1.2)

        return 1.0  # 默认满分

    def _get_timeliness_factor(self, code: str) -> Tuple[float, int, str]:
        """
        获取时效性因子 (Phase 3)

        Args:
            code: 股票代码

        Returns:
            (时效性因子，新闻年龄，波动状态)
        """
        # 尝试从新闻集成模块获取
        try:
            from app.factors.news_integration import get_news_sentiment_analyzer
            analyzer = get_news_sentiment_analyzer()

            # 获取最新分析结果
            result = analyzer.analyze_stock(code, lookback_hours=1)

            news_age = result.get('avg_age_minutes', 60)
            vol_state = result.get('volatility_state', 'normal')

            # 计算时效性因子 (基于新闻年龄和波动率)
            if self.decay_calculator:
                # 使用时间衰减
                base_factor = self.decay_calculator.calculate_decay_factor(news_age)
            else:
                # 简化线性衰减
                base_factor = max(0.1, 1.0 - (news_age * self.decay_rate))

            # 波动率调整 (高波动时更强调时效性)
            vol_factors = {
                'low': 1.0,
                'normal': 0.9,
                'high': 0.7,
                'extreme': 0.5,
            }
            vol_factor = vol_factors.get(vol_state, 0.8)

            timeliness = base_factor * vol_factor
            return timeliness, news_age, vol_state

        except Exception as e:
            logger.debug(f"获取时效性因子失败：{e}")
            return 0.8, 60, 'normal'  # 默认值

    def _map_regime(self, consensus_regime: str) -> str:
        """
        映射市场状态

        Args:
            consensus_regime: ConsensusEngine 的市场状态

        Returns:
            SignalScorer 的市场状态
        """
        mapping = {
            'trending': 'strong_trend_up',  # 默认映射为上涨趋势
            'ranging': 'range_bound',
            'neutral': 'default',
        }
        return mapping.get(consensus_regime, 'default')

    def _get_llm_signal_impact_factor(self, code: str) -> float:
        """2026-05-17 新增: 从动态池读取 LLM signal_impact 作为评分乘数

        映射规则:
        - boost → 1.15 (信号增强 15%)
        - neutral → 1.0 (无影响)
        - weaken → 0.80 (信号降权 20%)
        - block → 0.30 (信号严重降权 70%)

        Args:
            code: 股票代码 (e.g. "HK.00700" or "00700")

        Returns:
            乘数 (0.3 ~ 1.15)，默认 1.0
        """
        try:
            from app.utils.daily_active_pool import get_daily_active_pool
            pool = get_daily_active_pool(market_prefix="HK.")
            
            # 尝试原始 code
            entry = pool._entries.get(code)
            
            # 2026-05-17 P1: 前缀归一化 (兼容 "00700" 和 "HK.00700")
            if not entry:
                if code.startswith("HK."):
                    entry = pool._entries.get(code)
                else:
                    entry = pool._entries.get(f"HK.{code}")
            
            if entry and not entry.is_expired():
                impact = entry.metadata.get("signal_impact", "neutral")
                factors = {
                    "boost": 1.15,
                    "neutral": 1.0,
                    "weaken": 0.80,
                    "block": 0.30,
                }
                factor = factors.get(impact, 1.0)
                if factor != 1.0:
                    logger.info(f"[LLM信号评分] {code} signal_impact={impact} → 乘数={factor}")
                return factor
        except Exception as e:
            logger.debug(f"[LLM信号评分] 读取失败: {e}")
        return 1.0

    def _get_recommendation(self, quality: float,
                            timeliness: float,
                            final_score: float) -> str:
        """
        获取推送建议

        Args:
            quality: 信号质量分
            timeliness: 时效性因子
            final_score: 最终得分

        Returns:
            'push' / 'delay' / 'suppress'
        """
        # 高质量 + 高时效 = 立即推送
        if quality >= self.high_quality_threshold and timeliness >= 0.7:
            return 'push'

        # 低质量 = 过滤
        if quality < self.quality_threshold:
            return 'suppress'

        # 低时效 = 延迟推送
        if timeliness < 0.4:
            return 'delay'

        # 中等质量 = 正常推送
        if final_score >= 1.0:
            return 'push'
        elif final_score >= 0.5:
            return 'delay'
        else:
            return 'suppress'

    def get_statistics(self) -> Dict:
        """获取统计信息"""
        return {
            'quality_cache_size': len(self._quality_cache),
            'timeliness_cache_size': len(self._timeliness_cache),
            'components': {
                'signal_scorer': 'loaded' if self.signal_scorer else 'not_loaded',
                'decay_calculator': 'loaded' if self.decay_calculator else 'not_loaded',
                'vix_monitor': 'loaded' if self.vix_monitor else 'not_loaded',
            }
        }


# 全局单例
_enhancer = None


def get_consensus_enhancer(config: Dict = None) -> ConsensusEnhancer:
    """获取共识增强器单例"""
    global _enhancer
    if _enhancer is None:
        _enhancer = ConsensusEnhancer(config)
    return _enhancer


if __name__ == "__main__":
    # 测试
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    enhancer = get_consensus_enhancer()

    # 模拟 ConsensusResult
    from dataclasses import dataclass
    from app.strategy.base import SignalAction

    @dataclass
    class MockResult:
        code: str = "HK.00700"
        action: SignalAction = SignalAction.BUY
        price: float = 350.0
        score: float = 2.5
        market_regime: str = "trending"
        confidence: float = 0.85
        reasons: list = None
        agree_strategies: list = None

        def __post_init__(self):
            if self.reasons is None:
                self.reasons = ["测试信号"]
            if self.agree_strategies is None:
                self.agree_strategies = ["confluence_v2"]

    result = MockResult()
    enhanced = enhancer.enhance_signal(result.code, result)

    print(f"\n增强信号评估:")
    print(f"  代码：{enhanced.code}")
    print(f"  方向：{enhanced.action}")
    print(f"  原始分：{enhanced.original_score:.2f}")
    print(f"  质量分：{enhanced.quality_score:.2f}")
    print(f"  时效性因子：{enhanced.timeliness_factor:.2f}")
    print(f"  最终分：{enhanced.final_score:.2f}")
    print(f"  新闻年龄：{enhanced.news_age_minutes}分钟")
    print(f"  波动状态：{enhanced.volatility_state}")
    print(f"  建议：{enhanced.recommendation}")
