"""
场景分类器 (Scenario Classifier)

基于市场状态识别 (MarketRegimeDetector) 的扩展，实现细粒度场景分类。
支持 7 大主场景 × 16 子场景的分类体系。

主场景分类:
1. TREND_STRONG - 强趋势 (ADX > 30, 明确方向)
2. TREND_START - 趋势启动 (ADX 上升初期，20-30)
3. TREND_EXHAUST - 趋势衰竭 (ADX 极高但价格动量衰减)
4. RANGE_BOUND - 震荡市 (ADX < 20)
5. VOL_EXPANSION - 波动扩张 (ATR 百分位快速上升)
6. VOL_CONTRACTION - 波动收缩 (ATR 百分位下降)
7. GAP_EVENT - 跳空事件 (开盘跳空 > 2%)
8. REVERSAL_SETUP - 反转预备 (超买/超卖 + 动量衰竭)
9. BREAKOUT_SETUP - 突破预备 (布林带收窄 + 低波动)
"""
import logging
import time
from enum import Enum
from typing import Dict, Optional, Tuple, List, Any
from dataclasses import dataclass, field
from datetime import datetime

from app.strategy.market_regime import (
    MarketRegimeDetector, MarketRegime, MarketRegimeType,
    TrendDirection, VolatilityState
)
from .features import FeatureExtractor, Features
from .multi_timeframe import MultiTimeframeFeatureExtractor, MultiTimeframeFeatures
from .log import get_scenario_log_manager

logger = logging.getLogger("ftseek.scenario.classifier")
# 2026-04-10: propagate=True (默认), 通过 ftseek 父级 logger 输出


class ScenarioType(Enum):
    """场景类型枚举"""
    # 趋势类
    TREND_STRONG = "trend_strong"       # 强趋势
    TREND_START = "trend_start"         # 趋势启动
    TREND_EXHAUST = "trend_exhaust"     # 趋势衰竭

    # 震荡类
    RANGE_BOUND = "range_bound"         # 震荡市

    # 波动类
    VOL_EXPANSION = "vol_expansion"     # 波动扩张
    VOL_CONTRACTION = "vol_contraction" # 波动收缩

    # 事件类
    GAP_EVENT = "gap_event"             # 跳空事件
    NEWS_DRIVEN = "news_driven"         # 新闻驱动

    # 反转/突破类
    REVERSAL_SETUP = "reversal_setup"   # 反转预备
    BREAKOUT_SETUP = "breakout_setup"   # 突破预备

    # 灰区: 信号冲突/不确定，启用防守模式（落袋为安）
    GRAY_ZONE = "gray_zone"

    # 默认
    UNKNOWN = "unknown"


@dataclass
class ScenarioOutput:
    """场景分类输出"""
    scenario_type: ScenarioType
    confidence: float           # 置信度 (0-1)
    macro_regime: MarketRegimeType  # 宏观市场状态
    adx_value: float
    atr_percentile: float
    features: Optional[Features] = None
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)  # 辅助标签 (2026-04-13 新增)
    gray_zone_source: str = ""  # 灰区触发源 (conflict/low_conf)

    def to_dict(self) -> Dict:
        return {
            "scenario": self.scenario_type.value,
            "confidence": round(self.confidence, 2),
            "macro_regime": self.macro_regime.value,
            "adx": round(self.adx_value, 2),
            "atr_percentile": round(self.atr_percentile, 2),
            "timestamp": self.timestamp,
            "metadata": self.metadata,
            "tags": self.tags,
            "gray_zone_source": self.gray_zone_source,
        }


class ScenarioClassifier:
    """
    场景分类器

    继承 MarketRegimeDetector 的宏观状态识别能力，
    进一步细分为 16 子场景。

    识别流程:
    1. 获取宏观市场状态 (MarketRegimeDetector.detect())
    2. 提取细粒度特征 (FeatureExtractor)
    3. 规则引擎初判
    4. 置信度评分
    5. 场景记忆平滑 (防抖动)
    """

    # 配置参数
    ADX_START_MIN = 20.0       # 趋势启动 ADX 下限
    ADX_START_MAX = 30.0       # 趋势启动 ADX 上限
    ADX_STRONG_MIN = 30.0      # 强趋势 ADX 下限
    ADX_EXHAUST_MIN = 40.0     # 趋势衰竭 ADX 下限
    ADX_EXHAUST_DIVERGENCE = 0.3  # ADX-价格背离阈值

    ATR_EXPANSION_THRESHOLD = 0.15  # ATR 扩张阈值 (15% 上升)
    ATR_CONTRACTION_THRESHOLD = 0.15  # ATR 收缩阈值

    GAP_THRESHOLD = 0.02       # 跳空阈值 (2%)

    RSI_OVERBOUGHT = 70.0      # 超买阈值
    RSI_OVERSOLD = 30.0        # 超卖阈值

    BB_SQUEEZE_THRESHOLD = 0.05  # 布林带收窄阈值 (5%)

    CONFIRM_WINDOW = 3         # 场景确认 K 线数
    COOL_DOWN_MINUTES = 2      # 场景切换冷却时间 (分钟)
    CONFIDENCE_THRESHOLD = 0.65  # 置信度阈值 (低于此值返回 UNKNOWN)

    def __init__(self, config: Dict = None):
        """
        初始化场景分类器

        Args:
            config: 配置字典，可覆盖默认阈值
        """
        self.config = config or {}

        # 覆盖默认阈值
        self.ADX_START_MIN = self.config.get('adx_start_min', 20.0)
        self.ADX_START_MAX = self.config.get('adx_start_max', 30.0)
        self.ADX_STRONG_MIN = self.config.get('adx_strong_min', 30.0)
        self.ADX_EXHAUST_MIN = self.config.get('adx_exhaust_min', 40.0)
        self.GAP_THRESHOLD = self.config.get('gap_threshold', 0.02)
        self.RSI_OVERBOUGHT = self.config.get('rsi_overbought', 70.0)
        self.RSI_OVERSOLD = self.config.get('rsi_oversold', 30.0)
        self.CONFIRM_WINDOW = self.config.get('confirm_window', 3)
        self.COOL_DOWN_MINUTES = self.config.get('cool_down_minutes', 2)
        self.CONFIDENCE_THRESHOLD = self.config.get('confidence_threshold', 0.65)

        # 灰区检测参数
        self.GRAY_ZONE_CONFLICT_GAP = self.config.get('gray_zone_conflict_gap', 0.10)
        self.GRAY_ZONE_MIN_CONFIDENCE = self.config.get('gray_zone_min_confidence', 0.45)
        # 高优先级场景绕过灰区的最低置信度门槛 (2026-04-11 防守一致性修复)
        # 低于此门槛的GAP_EVENT/NEWS_DRIVEN不走快速通道，而是进入完整候选池
        self.GRAY_ZONE_HIGH_PRIORITY_BYPASS_MIN = self.config.get(
            'gray_zone_high_priority_bypass_min', 0.65
        )

        # 宏观市场状态检测器 (复用现有能力)
        self.regime_detector = MarketRegimeDetector(config)

        # 特征提取器
        self.feature_extractor = FeatureExtractor(config)

        # 多周期特征提取器 (Phase 2 新增)
        self.multi_tf_extractor = MultiTimeframeFeatureExtractor(config)

        # 场景日志管理器 (Phase 2 新增)
        self.log_manager = get_scenario_log_manager()

        # 2026-04-10: _scenario_cache 已移除，平滑逻辑统一由 ScenarioMemory 负责

        # 场景切换历史 {code: [(scenario, timestamp), ...]}
        self._scenario_history: Dict[str, List[Tuple[ScenarioType, float]]] = {}

        logger.info(
            f"场景分类器已初始化 | "
            f"ADX 启动={self.ADX_START_MIN}-{self.ADX_START_MAX} | "
            f"ADX 强={self.ADX_STRONG_MIN}+ | "
            f"ADX 衰竭={self.ADX_EXHAUST_MIN}+ | "
            f"跳空阈值={self.GAP_THRESHOLD*100:.0f}% | "
            f"置信度阈值={self.CONFIDENCE_THRESHOLD:.2f}"
        )

    def classify(self, code: str, klines: List[Dict], news_data: Dict = None,
                 multi_timeframe_klines: Dict[str, List[Dict]] = None) -> ScenarioOutput:
        """
        场景分类主入口

        Args:
            code: 股票代码
            klines: K 线数据列表 (主周期)
            news_data: 新闻数据 (可选)
            multi_timeframe_klines: 多周期 K 线数据 {timeframe: [klines]} (可选)

        Returns:
            ScenarioOutput: 场景分类结果
        """
        # 0. 事件类场景优先检测 (Phase 2: 无需依赖宏观状态)
        features_early = self.feature_extractor.extract(code, klines, news_data)

        # 跳空事件检测 (最高优先级)
        # 2026-04-11 灰区防守一致性修复：置信度 >= bypass_min 才走快速通道绕过灰区检测
        # 置信度不足时进入完整候选池，让灰区检测有机会生效
        if abs(features_early.event.gap_pct) >= self.GAP_THRESHOLD:
            confidence = min(0.95, 0.7 + abs(features_early.event.gap_pct) * 0.5)
            if features_early.volume.volume_ratio > 1.5:
                confidence = min(0.95, confidence + 0.1)
            logger.info(f"[{code}] 跳空事件检测：{features_early.event.gap_pct*100:.1f}%")
            if confidence >= self.GRAY_ZONE_HIGH_PRIORITY_BYPASS_MIN:
                # 置信度足够高 → 快速通道，允许直接绕过灰区检测
                return self._create_output(code, ScenarioType.GAP_EVENT, confidence, features_early)
            else:
                # 置信度不足 → 记录并继续完整流程（灰区检测将在后续运行）
                logger.info(
                    f"[{code}] 跳空事件置信度={confidence:.2f} < 灰区绕过门槛"
                    f"={self.GRAY_ZONE_HIGH_PRIORITY_BYPASS_MIN:.2f}，进入候选池"
                )

        # 新闻驱动检测 (高优先级)
        # 2026-04-11 灰区防守一致性修复：同上，置信度不足时不直接返回
        news_score = features_early.event.news_score
        news_intensity = features_early.event.metadata.get('news_intensity', 0.0)
        if news_score != 0.0 and news_intensity >= 0.5:
            news_confidence = min(0.9, 0.6 + news_intensity * 0.3)
            if features_early.event.metadata.get('breaking_news', False):
                news_confidence = min(0.95, news_confidence + 0.1)
            logger.info(f"[{code}] 新闻驱动检测：score={news_score:.2f}")
            if news_confidence >= self.GRAY_ZONE_HIGH_PRIORITY_BYPASS_MIN:
                # 置信度足够高 → 快速通道，允许直接绕过灰区检测
                return self._create_output(code, ScenarioType.NEWS_DRIVEN, news_confidence, features_early)
            else:
                # 置信度不足 → 记录并继续完整流程
                logger.info(
                    f"[{code}] 新闻驱动置信度={news_confidence:.2f} < 灰区绕过门槛"
                    f"={self.GRAY_ZONE_HIGH_PRIORITY_BYPASS_MIN:.2f}，进入候选池"
                )

        # 1. 获取宏观市场状态
        macro_regime = self.regime_detector.detect(code)
        if macro_regime is None:
            logger.debug(f"{code} 宏观市场状态数据不足")
            return self._unknown_scenario(code, features_early)

        # 2. 提取细粒度特征 (复用已计算的特征)
        features = features_early

        # 3. 多周期特征分析 (Phase 2 新增)
        multi_tf_features = None
        confidence_boost = 1.0
        if multi_timeframe_klines:
            multi_tf_features = self.multi_tf_extractor.extract(code, multi_timeframe_klines)
            confidence_boost = multi_tf_features.confidence_boost
            logger.debug(f"[{code}] 多周期置信度提升：{confidence_boost:.2f}x")

        # 4. 规则引擎初判
        candidate_scenario, confidence, gz_source = self._rule_engine(macro_regime, features, news_data)

        # 5. 多周期置信度调整 (Phase 2)
        if multi_tf_features:
            adjusted_confidence = min(0.95, confidence * confidence_boost)
            logger.debug(f"[{code}] 置信度调整：{confidence:.2f} -> {adjusted_confidence:.2f}")
            confidence = adjusted_confidence

            # 多周期共振检测：提升趋势类场景置信度
            if multi_tf_features.resonance_detected and candidate_scenario in [
                ScenarioType.TREND_STRONG, ScenarioType.TREND_START,
                ScenarioType.TREND_EXHAUST
            ]:
                confidence = min(0.95, confidence + 0.1)
                logger.info(f"[{code}] 多周期共振 detected → 置信度 +0.1")

        # 6. 场景由 ScenarioMemory 负责平滑 (2026-04-10: 移除双重平滑)
        smoothed_scenario = candidate_scenario

        # 7. 置信度阈值检查
        if confidence < self.CONFIDENCE_THRESHOLD:
            logger.debug(f"{code} 置信度{confidence:.2f} < {self.CONFIDENCE_THRESHOLD}, 返回 UNKNOWN")
            return self._unknown_scenario(code, features, macro_regime)

        # 8. 记录场景切换历史 (内存 + 数据库)
        self._record_scenario_change(code, smoothed_scenario)

        # 9. 记录到数据库 (Phase 2 新增)
        try:
            mtf_dict = multi_tf_features.to_dict() if multi_tf_features else {}
            self.log_manager.log_scenario_change(
                code=code,
                scenario=smoothed_scenario.value,
                confidence=confidence,
                adx_value=macro_regime.adx_value,
                macro_regime=macro_regime.regime_type.value,
                mtf_alignment=mtf_dict.get('trend_alignment', 0),
                multi_timeframe=mtf_dict,
                triggered_by='auto'
            )
        except Exception as e:
            logger.debug(f"记录场景日志失败：{e}")

        # 10. 场景辅助标签收集 (2026-04-13 新增: 场景标签叠加)
        tags = []
        try:
            # 波动叠加: 任何场景下 ATR 扩张都可以叠加
            if features.volatility.atr_ratio >= (1 + self.ATR_EXPANSION_THRESHOLD):
                tags.append("vol_expanding")
            # 反转预备叠加: 趋势+超买/超卖
            if features.position.rsi > 70 or features.position.rsi < 30:
                tags.append("reversal_risk")
            # 均值回归叠加: 价格远离均线
            if abs(features.position.price_vs_ma) > 0.03:
                tags.append("mean_reversion")
            # 多周期分歧
            if multi_tf_features and multi_tf_features.alignment_score < 0.3:
                tags.append("mtf_divergent")
        except Exception as e:
            logger.debug(f"[{code}] 标签收集异常: {e}")

        # 11. 输出结果
        output = ScenarioOutput(
            scenario_type=smoothed_scenario,
            confidence=confidence,
            macro_regime=macro_regime.regime_type,
            adx_value=macro_regime.adx_value,
            atr_percentile=macro_regime.atr_percentile,
            features=features,
            metadata={
                "trend_direction": macro_regime.trend_direction.value,
                "volatility_state": macro_regime.volatility_state.value,
                "price_vs_ma": macro_regime.price_vs_ma,
                "bb_width": macro_regime.bb_width,
                "multi_timeframe": multi_tf_features.to_dict() if multi_tf_features else None,
                "confidence_boost": confidence_boost,
                "resonance": multi_tf_features.resonance_detected if multi_tf_features else False,
            },
            gray_zone_source=gz_source,
            tags=tags,
        )

        logger.info(
            f"[{code}] 场景分类：{smoothed_scenario.value} | "
            f"宏观={macro_regime.regime_type.value} | "
            f"ADX={macro_regime.adx_value:.1f} | "
            f"置信度={confidence:.2f}"
        )

        return output

    def _rule_engine(self, regime: MarketRegime, features: Features,
                     news_data: Dict = None) -> Tuple[ScenarioType, float, str]:
        """
        规则引擎：基于宏观状态和特征进行场景分类

        Returns:
            (scenario_type, confidence, gray_zone_source)
        """
        # 场景候选池
        candidates: List[Tuple[ScenarioType, float]] = []

        # --- 事件类场景优先 (最高优先级) ---
        # 1. 跳空事件检测 (Phase 2: 增强实现)
        gap_threshold = self.GAP_THRESHOLD  # 2% 跳空
        if abs(features.event.gap_pct) >= gap_threshold:
            # 基础置信度：0.7 + 跳空幅度加成
            confidence = min(0.95, 0.7 + abs(features.event.gap_pct) * 0.5)

            # 跳空 + 高成交量 = 更高置信度
            if features.volume.volume_ratio > 1.5:
                confidence = min(0.95, confidence + 0.1)

            # 跳空方向判断
            if features.event.gap_pct > 0:
                logger.info(f"向上跳空：{features.event.gap_pct*100:.1f}%")
            else:
                logger.info(f"向下跳空：{features.event.gap_pct*100:.1f}%")

            candidates.append((ScenarioType.GAP_EVENT, confidence))

        # 2. 新闻驱动检测 (Phase 2: 增强实现)
        news_score = features.event.news_score
        news_intensity = features.event.metadata.get('news_intensity', 0.0)
        breaking_news = features.event.metadata.get('breaking_news', False)

        if news_score != 0.0:
            # 基础置信度：0.6 + 新闻强度加成
            news_confidence = min(0.9, 0.6 + news_intensity * 0.3)

            # 突发新闻额外加成
            if breaking_news:
                news_confidence = min(0.95, news_confidence + 0.1)

            # 事件强度分级 (2026-04-13 新增: CRITICAL 级别直接高置信度)
            event_impact = features.event.metadata.get('impact_level', '')
            if event_impact == 'critical':
                news_confidence = max(news_confidence, 0.85)
            elif event_impact == 'high':
                news_confidence = max(news_confidence, 0.75)

            # 新闻 + 跳空 = 事件共振
            if abs(features.event.gap_pct) >= gap_threshold * 0.5:
                news_confidence = min(0.95, news_confidence + 0.1)
                logger.info(f"新闻驱动 + 跳空共振：news_score={news_score:.2f}")

            candidates.append((ScenarioType.NEWS_DRIVEN, news_confidence))

        # --- 趋势类场景 ---
        if regime.regime_type in (MarketRegimeType.STRONG_TREND_UP,
                                   MarketRegimeType.STRONG_TREND_DOWN):
            # 强趋势 → TREND_STRONG
            candidates.append((ScenarioType.TREND_STRONG, regime.confidence))

            # 检查趋势衰竭信号 (ADX 极高但 RSI 背离)
            if regime.adx_value >= self.ADX_EXHAUST_MIN:
                if self._check_divergence(regime, features):
                    candidates.append((ScenarioType.TREND_EXHAUST, 0.75))

        elif regime.regime_type in (MarketRegimeType.WEAK_TREND_UP,
                                     MarketRegimeType.WEAK_TREND_DOWN):
            # 弱趋势 + ADX 上升 → TREND_START
            if self.ADX_START_MIN <= regime.adx_value <= self.ADX_START_MAX:
                candidates.append((ScenarioType.TREND_START, 0.7))

        # --- 波动类场景 ---
        if regime.volatility_state == VolatilityState.HIGH:
            # 检查 ATR 是否在快速上升
            if self._check_atr_expansion(features):
                candidates.append((ScenarioType.VOL_EXPANSION, 0.8))

        elif regime.volatility_state == VolatilityState.LOW:
            # 低波动 → 可能是波动收缩
            candidates.append((ScenarioType.VOL_CONTRACTION, 0.7))

            # 检查是否是突破预备 (布林带收窄)
            if features.volatility.bb_width < self.BB_SQUEEZE_THRESHOLD:
                candidates.append((ScenarioType.BREAKOUT_SETUP, 0.75))

        # --- 震荡类场景 ---
        if regime.regime_type == MarketRegimeType.RANGE_BOUND:
            candidates.append((ScenarioType.RANGE_BOUND, regime.confidence))

            # 检查反转预备 (超买/超卖)
            if self._check_reversal_setup(features):
                candidates.append((ScenarioType.REVERSAL_SETUP, 0.7))

        # --- 突破预备检测 (独立于宏观状态) ---
        if not any(s == ScenarioType.BREAKOUT_SETUP for s, _ in candidates):
            if self._check_breakout_setup(features):
                candidates.append((ScenarioType.BREAKOUT_SETUP, 0.7))

        # 返回置信度最高的场景
        if not candidates:
            return ScenarioType.UNKNOWN, 0.5

        # 按置信度排序，返回最高
        candidates.sort(key=lambda x: x[1], reverse=True)
        best_scenario, best_confidence = candidates[0]

        # ============================================================
        # 灰区检测 (2026-04-11 新增)
        # 有候选但不确定 -> GRAY_ZONE，启用落袋为安防守模式
        # ============================================================

        # 灰区检测1: 候选冲突 (top2 置信度差距 < 阈值，两者都有一定可信度)
        if len(candidates) >= 2:
            gap = candidates[0][1] - candidates[1][1]
            if gap < self.GRAY_ZONE_CONFLICT_GAP and candidates[1][1] >= 0.55:
                logger.info(
                    f"[灰区] 候选冲突: {candidates[0][0].value}={candidates[0][1]:.2f} "
                    f"vs {candidates[1][0].value}={candidates[1][1]:.2f} (gap={gap:.2f})"
                )
                return ScenarioType.GRAY_ZONE, best_confidence, 'conflict'

        # 灰区检测2: 低置信度带 (有候选但不够确信)
        if best_confidence < self.CONFIDENCE_THRESHOLD:
            if best_confidence >= self.GRAY_ZONE_MIN_CONFIDENCE:
                logger.info(
                    f"[灰区] 低置信度: {best_scenario.value}={best_confidence:.2f} "
                    f"(阈值={self.CONFIDENCE_THRESHOLD}, 灰区下限={self.GRAY_ZONE_MIN_CONFIDENCE})"
                )
                return ScenarioType.GRAY_ZONE, best_confidence, 'low_conf'

        return best_scenario, best_confidence, ''

    def _check_divergence(self, regime: MarketRegime, features: Features) -> bool:
        """检查 ADX-价格背离 (趋势衰竭信号)"""
        # ADX 极高 (>40) 但 RSI 与价格方向背离
        if regime.adx_value < self.ADX_EXHAUST_MIN:
            return False

        rsi = features.position.rsi
        price_vs_ma = features.position.price_vs_ma

        # 上涨趋势衰竭：ADX 高 + RSI 超买 + 价格动量衰减
        if regime.trend_direction == TrendDirection.STRONG_UP:
            return rsi >= self.RSI_OVERBOUGHT and price_vs_ma < 0.05

        # 下跌趋势衰竭：ADX 高 + RSI 超卖 + 价格动量衰减
        elif regime.trend_direction == TrendDirection.STRONG_DOWN:
            return rsi <= self.RSI_OVERSOLD and price_vs_ma > -0.05

        return False

    def _check_atr_expansion(self, features: Features) -> bool:
        """检查 ATR 是否在快速扩张"""
        # ATR 比率 > 1.15 表示 ATR 较平均值上升 15%
        return features.volatility.atr_ratio >= (1 + self.ATR_EXPANSION_THRESHOLD)

    def _check_reversal_setup(self, features: Features) -> bool:
        """检查反转预备条件"""
        rsi = features.position.rsi

        # 超买或超卖
        if rsi >= self.RSI_OVERBOUGHT or rsi <= self.RSI_OVERSOLD:
            return True

        return False

    def _check_breakout_setup(self, features: Features) -> bool:
        """检查突破预备条件"""
        # 布林带宽度 < 阈值 + 低波动
        bb_width = features.volatility.bb_width

        if bb_width < self.BB_SQUEEZE_THRESHOLD:
            return True

        return False

    # 2026-04-10: _apply_scenario_memory() 已移除
    # 平滑逻辑统一由 ScenarioMemory 状态机负责，消除双重平滑问题

    def _record_scenario_change(self, code: str, scenario: ScenarioType):
        """记录场景切换历史"""
        if code not in self._scenario_history:
            self._scenario_history[code] = []

        self._scenario_history[code].append((scenario, time.time()))

        # 保留最近 100 条记录
        if len(self._scenario_history[code]) > 100:
            self._scenario_history[code] = self._scenario_history[code][-100:]

    def _create_output(self, code: str, scenario: ScenarioType, confidence: float,
                       features: Features) -> ScenarioOutput:
        """
        创建场景输出 (用于事件类场景直接返回)

        Args:
            code: 股票代码
            scenario: 场景类型
            confidence: 置信度
            features: 特征集合

        Returns:
            ScenarioOutput: 场景分类结果
        """
        # 2026-04-10: 平滑由 ScenarioMemory 负责，直接使用原始场景
        smoothed_scenario = scenario

        # 记录场景切换历史
        self._record_scenario_change(code, smoothed_scenario)

        return ScenarioOutput(
            scenario_type=smoothed_scenario,
            confidence=confidence,
            macro_regime=MarketRegimeType.RANGE_BOUND,  # 默认宏观状态
            adx_value=0.0,
            atr_percentile=50.0,
            features=features,
            metadata={"direct_detection": True, "reason": "event_driven_scenario"}
        )

    def _unknown_scenario(self, code: str, features: Features = None,
                          regime: MarketRegime = None) -> ScenarioOutput:
        """返回 UNKNOWN 场景"""
        return ScenarioOutput(
            scenario_type=ScenarioType.UNKNOWN,
            confidence=0.5,
            macro_regime=regime.regime_type if regime else MarketRegimeType.RANGE_BOUND,
            adx_value=regime.adx_value if regime else 0.0,
            atr_percentile=regime.atr_percentile if regime else 50.0,
            features=features,
            metadata={"reason": "confidence_below_threshold_or_data_insufficient"}
        )

    def get_scenario_history(self, code: str) -> List[Dict]:
        """获取场景切换历史"""
        history = self._scenario_history.get(code, [])
        return [
            {"scenario": s.value, "timestamp": t}
            for s, t in history
        ]

    def clear_cache(self, code: str = None):
        """清除缓存"""
        if code:
            self._scenario_history.pop(code, None)
        else:
            self._scenario_history.clear()


# 全局单例
_classifier: Optional[ScenarioClassifier] = None


def get_scenario_classifier(config: Dict = None) -> ScenarioClassifier:
    """获取场景分类器单例"""
    global _classifier
    if _classifier is None:
        _classifier = ScenarioClassifier(config)
    return _classifier
