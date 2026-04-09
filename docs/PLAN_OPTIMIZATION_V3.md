# FTSeek 优化实施计划 V3.0

**版本**: 3.0  
**日期**: 2026-03-23  
**预计工期**: 5 个工作日  
**状态**: 待执行

---

## 一、实施阶段概览

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        实施阶段甘特图                                    │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  Phase 1: 策略去重与分层 ████████░░░░░░░░░░░░ Day 1                    │
│  Phase 2: 多周期窗口    ░░░░████████░░░░░░░░░ Day 2                    │
│  Phase 3: 动态阈值      ░░░░░░░░████████░░░░░ Day 2-3                  │
│  Phase 4: 情绪降级      ░░░░░░░░░░░░████████░ Day 3                    │
│  Phase 5: 退出引擎增强  ░░░░░░░░░░░░░░░░████████ Day 3-4               │
│  Phase 6: 成本约束      ░░░░░░░░░░░░░░░░░░░░████████ Day 4             │
│  Phase 7: 集成测试      ░░░░░░░░░░░░░░░░░░░░░░░░████████ Day 5         │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 二、详细实施计划

### Phase 1: 策略去重与分层 (Day 1)

#### 1.1 创建策略层级管理器

**文件**: `app/strategy/strategy_layer.py`

```python
"""策略层级管理器"""
from typing import Dict, List, Set
from dataclasses import dataclass
import logging

logger = logging.getLogger("ftseek.strategy_layer")


@dataclass
class StrategyLayer:
    """策略层定义"""
    name: str
    strategies: List[str]
    vote_weight: float
    mode: str = "vote"  # vote / confirm / filter


class StrategyLayerManager:
    """策略层级管理器 - 消除重复投票"""
    
    # 互斥规则: 当启用组合策略时，禁用其包含的原子策略
    EXCLUSION_RULES = {
        "confluence": ["rsi", "macd", "bollinger", "supertrend", "ma_cross"],
        "confluence_v2": ["rsi", "macd", "bollinger", "supertrend", "ma_cross"],
    }
    
    # 策略分组
    LAYERS = {
        "atomic": StrategyLayer(
            name="原子策略",
            strategies=["rsi", "macd", "bollinger", "supertrend", "ma_cross"],
            vote_weight=0.5,
        ),
        "composite": StrategyLayer(
            name="组合策略",
            strategies=["confluence", "confluence_v2"],
            vote_weight=1.2,
        ),
        "signal": StrategyLayer(
            name="信号策略",
            strategies=["price_break", "volume_alert"],
            vote_weight=0.4,
        ),
        "confirmation": StrategyLayer(
            name="确认策略",
            strategies=["sentiment_ai"],
            vote_weight=0.0,  # 不参与投票
            mode="confirm",
        ),
    }
    
    def __init__(self, enabled_strategies: List[str]):
        self.enabled_strategies = enabled_strategies
        self._effective_strategies = self._resolve_exclusions()
        logger.info(
            f"策略层级管理器初始化 | 启用策略: {enabled_strategies} | "
            f"有效投票策略: {self._effective_strategies}"
        )
    
    def _resolve_exclusions(self) -> List[str]:
        """解析互斥规则，返回有效投票策略列表"""
        effective = set(self.enabled_strategies)
        
        # 如果启用了组合策略，移除其包含的原子策略
        for composite, atomics in self.EXCLUSION_RULES.items():
            if composite in self.enabled_strategies:
                excluded = set(atomics) & effective
                if excluded:
                    effective -= excluded
                    logger.info(
                        f"[策略去重] {composite} 已启用，移除原子策略: {excluded}"
                    )
        
        return list(effective)
    
    def get_vote_weight(self, strategy: str) -> float:
        """获取策略的投票权重"""
        for layer in self.LAYERS.values():
            if strategy in layer.strategies:
                return layer.vote_weight
        return 0.5  # 默认权重
    
    def is_confirmation_strategy(self, strategy: str) -> bool:
        """判断是否为确认策略"""
        layer = self.LAYERS.get("confirmation")
        return layer and strategy in layer.strategies
    
    def get_effective_strategies(self) -> List[str]:
        """获取有效投票策略列表"""
        return self._effective_strategies
    
    def should_vote(self, strategy: str) -> bool:
        """判断策略是否应该参与投票"""
        return strategy in self._effective_strategies
```

#### 1.2 修改共识引擎集成

**文件**: `app/strategy/consensus_engine.py` (修改)

```python
# 在 ConsensusEngine.__init__ 中添加
from app.strategy.strategy_layer import StrategyLayerManager

class ConsensusEngine:
    def __init__(self, ..., enabled_strategies: List[str] = None):
        # ... 现有代码 ...
        
        # 新增: 策略层级管理
        self.layer_manager = StrategyLayerManager(enabled_strategies or [])
    
    def submit(self, signal: Signal):
        """提交信号 - 增加策略过滤"""
        # 检查策略是否应该投票
        if not self.layer_manager.should_vote(signal.strategy):
            logger.debug(
                f"[策略过滤] {signal.strategy} 不参与投票 "
                f"(被组合策略替代或为确认策略)"
            )
            return
        
        # ... 现有投票逻辑 ...
```

---

### Phase 2: 多周期窗口 (Day 2)

#### 2.1 创建多周期窗口管理器

**文件**: `app/signal_engine/multi_timeframe.py`

```python
"""多周期窗口管理器"""
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum
import logging

logger = logging.getLogger("ftseek.multi_timeframe")


class TrendDirection(Enum):
    UP = "up"
    DOWN = "down"
    NEUTRAL = "neutral"


@dataclass
class TimeframeSignal:
    """周期信号"""
    timeframe: str
    direction: TrendDirection
    confidence: float
    timestamp: float
    strategies: List[str]


class MultiTimeframeManager:
    """多周期窗口管理器"""
    
    CONFIG = {
        "daily": {
            "window": 86400,
            "weight": 0.3,
            "strategies": ["ma_trend"],
        },
        "hourly": {
            "window": 300,
            "weight": 0.4,
            "strategies": ["macd", "bollinger", "supertrend"],
        },
        "minute": {
            "window_min": 30,
            "window_max": 120,
            "weight": 0.3,
            "strategies": ["rsi", "volume_alert"],
        },
    }
    
    def __init__(self):
        # 各周期信号缓存
        self._signals: Dict[str, Dict[str, TimeframeSignal]] = defaultdict(dict)
        # 周期对齐锁
        self._lock = threading.Lock()
        # 动态窗口缓存 (基于波动率)
        self._dynamic_windows: Dict[str, int] = {}
        
        logger.info("多周期窗口管理器初始化完成")
    
    def update_signal(
        self,
        code: str,
        timeframe: str,
        direction: TrendDirection,
        confidence: float,
        strategies: List[str],
    ):
        """更新周期信号"""
        with self._lock:
            self._signals[code][timeframe] = TimeframeSignal(
                timeframe=timeframe,
                direction=direction,
                confidence=confidence,
                timestamp=time.time(),
                strategies=strategies,
            )
            logger.debug(
                f"[多周期] {code} {timeframe} 方向={direction.value} "
                f"置信度={confidence:.2f}"
            )
    
    def check_alignment(
        self,
        code: str,
        signal_direction: TrendDirection,
    ) -> Tuple[bool, float]:
        """
        检查多周期对齐
        
        Returns:
            (is_aligned, confidence_multiplier)
        """
        with self._lock:
            code_signals = self._signals.get(code, {})
            
            if not code_signals:
                return True, 1.0  # 无多周期数据时不过滤
            
            daily = code_signals.get("daily")
            hourly = code_signals.get("hourly")
            minute = code_signals.get("minute")
            
            # 三周期完全对齐
            if daily and hourly and minute:
                if (daily.direction == signal_direction and
                    hourly.direction == signal_direction and
                    minute.direction == signal_direction):
                    logger.info(
                        f"[多周期对齐] {code} 三周期同向 {signal_direction.value} "
                        f"置信度×1.3"
                    )
                    return True, 1.3
            
            # 日线反向
            if daily and daily.direction != TrendDirection.NEUTRAL:
                if daily.direction != signal_direction:
                    logger.info(
                        f"[多周期警告] {code} 信号{signal_direction.value} "
                        f"与日线{daily.direction.value}反向，置信度×0.5"
                    )
                    return True, 0.5
            
            # 两周期对齐
            aligned_count = 0
            for tf in ["hourly", "minute"]:
                if code_signals.get(tf, TimeframeSignal("", TrendDirection.NEUTRAL, 0, 0, [])).direction == signal_direction:
                    aligned_count += 1
            
            if aligned_count >= 2:
                return True, 1.1
            
            return True, 1.0
    
    def get_dynamic_window(self, code: str, volatility: float) -> int:
        """
        根据波动率动态计算窗口大小
        
        高波动 → 短窗口 (快速反应)
        低波动 → 长窗口 (等待确认)
        """
        base_window = 60  # 基准60秒
        
        # 波动率调整: volatility 0.01-0.05
        # 高波动(0.05) → 窗口×0.5 = 30秒
        # 低波动(0.01) → 窗口×2 = 120秒
        adjust_factor = 2.0 - volatility * 30  # 1.5 ~ 0.5
        adjust_factor = max(0.5, min(2.0, adjust_factor))
        
        window = int(base_window * adjust_factor)
        window = max(30, min(120, window))  # 限制范围
        
        self._dynamic_windows[code] = window
        return window
```

#### 2.2 集成到数据订阅器

**文件**: `app/data_subscriber.py` (修改)

```python
# 在 DataSubscriber.__init__ 中添加
from app.signal_engine.multi_timeframe import MultiTimeframeManager, TrendDirection

class DataSubscriber:
    def __init__(self, ...):
        # ... 现有代码 ...
        
        # 新增: 多周期管理器
        self.mtf_manager = MultiTimeframeManager()
    
    # 在 K线处理中添加多周期信号更新
    def _process_kline_for_mtf(self, code: str, kline: dict, k_type: str):
        """处理K线并更新多周期信号"""
        # 日K → 更新日线趋势
        if k_type == "K_DAY":
            # 简单均线趋势判断
            buf = self._kline_buffers[code]
            closes = list(buf.close)
            if len(closes) >= 20:
                ma20 = sum(closes[-20:]) / 20
                trend = TrendDirection.UP if closes[-1] > ma20 else TrendDirection.DOWN
                self.mtf_manager.update_signal(code, "daily", trend, 0.7, ["ma_trend"])
        
        # 60分钟K → 更新小时信号
        elif k_type == "K_60M":
            # ... 小时级别信号处理 ...
            pass
```

---

### Phase 3: 动态阈值 (Day 2-3)

#### 3.1 创建动态阈值管理器

**文件**: `app/signal_engine/dynamic_threshold.py`

```python
"""动态阈值管理器"""
from dataclasses import dataclass
from typing import Optional
import logging

logger = logging.getLogger("ftseek.dynamic_threshold")


@dataclass
class ThresholdContext:
    """阈值上下文"""
    volatility: float      # ATR% (0.01-0.05)
    adx: float             # ADX值 (10-50)
    signal_count: int      # 最近1小时信号数
    win_rate: float        # 近期胜率 (0-1)


class DynamicThresholdManager:
    """动态阈值管理器"""
    
    def __init__(self):
        self.base_threshold = 1.0
        self.min_threshold = 0.6
        self.max_threshold = 2.0
        
        # 调整因子权重
        self.weights = {
            "volatility": 0.3,
            "adx": 0.2,
            "frequency": 0.3,
            "win_rate": 0.2,
        }
        
        logger.info(
            f"动态阈值管理器初始化 | 基准={self.base_threshold} | "
            f"范围=[{self.min_threshold}, {self.max_threshold}]"
        )
    
    def calculate_threshold(self, ctx: ThresholdContext) -> float:
        """
        计算动态阈值
        
        高波动 + 低ADX + 高信号频率 → 提高阈值 (过滤噪音)
        低波动 + 高ADX + 低信号频率 → 降低阈值 (捕捉信号)
        """
        # 1. 波动率调整
        # ATR% 0.01 → 1.0, 0.05 → 1.4
        vol_factor = 1.0 + (ctx.volatility - 0.02) * 10
        vol_factor = max(0.8, min(1.4, vol_factor))
        
        # 2. ADX调整
        # ADX=30 (趋势强) → 降低阈值 0.9
        # ADX=15 (震荡) → 提高阈值 1.1
        adx_factor = 1.0 - (ctx.adx - 20) * 0.01
        adx_factor = max(0.9, min(1.1, adx_factor))
        
        # 3. 信号频率调整
        # 信号过多 → 提高阈值
        freq_factor = 1.0 + max(0, (ctx.signal_count - 20)) * 0.005
        freq_factor = max(1.0, min(1.3, freq_factor))
        
        # 4. 胜率调整
        # 胜率高 → 可以降低阈值
        win_factor = 1.0 - (ctx.win_rate - 0.5) * 0.2
        win_factor = max(0.9, min(1.1, win_factor))
        
        # 综合计算
        threshold = (
            self.base_threshold
            * (vol_factor * self.weights["volatility"]
               + adx_factor * self.weights["adx"]
               + freq_factor * self.weights["frequency"]
               + win_factor * self.weights["win_rate"])
        )
        
        threshold = max(self.min_threshold, min(self.max_threshold, threshold))
        
        logger.debug(
            f"动态阈值计算: vol={vol_factor:.2f} adx={adx_factor:.2f} "
            f"freq={freq_factor:.2f} win={win_factor:.2f} → {threshold:.2f}"
        )
        
        return round(threshold, 2)
    
    def update_base_threshold(self, new_base: float):
        """更新基准阈值"""
        self.base_threshold = max(self.min_threshold, min(self.max_threshold, new_base))
        logger.info(f"基准阈值更新: {self.base_threshold}")
```

#### 3.2 集成到共识引擎

**文件**: `app/strategy/consensus_engine.py` (修改)

```python
from app.signal_engine.dynamic_threshold import DynamicThresholdManager, ThresholdContext

class ConsensusEngine:
    def __init__(self, ...):
        # ... 现有代码 ...
        
        # 新增: 动态阈值管理器
        self.threshold_manager = DynamicThresholdManager()
    
    def _evaluate(self, code: str):
        """评估 - 使用动态阈值"""
        # ... 获取信号和上下文 ...
        
        # 计算动态阈值
        ctx = ThresholdContext(
            volatility=self._get_volatility(code),
            adx=self._adx_cache.get(code, 20),
            signal_count=self._get_recent_signal_count(code),
            win_rate=self._get_recent_win_rate(code),
        )
        dynamic_threshold = self.threshold_manager.calculate_threshold(ctx)
        
        # 使用动态阈值评估
        if score >= dynamic_threshold:
            # BUY 信号
            pass
        elif score <= -dynamic_threshold:
            # SELL 信号
            pass
```

---

### Phase 4: 情绪策略降级 (Day 3)

#### 4.1 创建情绪确认器

**文件**: `app/factors/sentiment_confirm.py`

```python
"""情绪确认器 - 将情绪从投票变量降级为确认/过滤变量"""
from dataclasses import dataclass
from typing import Optional, Tuple
from datetime import datetime, timedelta
import logging

logger = logging.getLogger("ftseek.sentiment_confirm")


@dataclass
class SentimentConfirmResult:
    """情绪确认结果"""
    confirmed: bool           # 是否确认信号
    confidence_adjust: float  # 置信度调整 (0.7-1.3)
    reason: str               # 原因说明
    should_filter: bool       # 是否应过滤信号


class SentimentConfirmer:
    """情绪确认器"""
    
    # 极端情绪阈值
    EXTREME_POSITIVE = 0.7
    EXTREME_NEGATIVE = -0.7
    
    # 情绪数据过期时间
    SENTIMENT_EXPIRE_MINUTES = 30
    
    def __init__(self):
        self._sentiment_cache = {}  # code -> (score, timestamp)
        logger.info("情绪确认器初始化完成")
    
    def update_sentiment(self, code: str, score: float):
        """更新情绪分数"""
        self._sentiment_cache[code] = (score, datetime.now())
        logger.debug(f"[情绪更新] {code} 分数={score:.2f}")
    
    def check_signal(
        self,
        code: str,
        signal_direction: str,  # "BUY" or "SELL"
    ) -> SentimentConfirmResult:
        """
        检查信号是否被情绪确认
        
        情绪作为确认变量，不参与投票:
        - 情绪一致 → 置信度×1.2
        - 情绪反向(弱) → 不影响
        - 情绪反向(强) → 置信度×0.7
        - 极端反向 → 过滤信号
        """
        cached = self._sentiment_cache.get(code)
        
        if not cached:
            return SentimentConfirmResult(
                confirmed=True,
                confidence_adjust=1.0,
                reason="无情绪数据",
                should_filter=False,
            )
        
        sentiment_score, timestamp = cached
        
        # 检查情绪数据是否过期
        age = (datetime.now() - timestamp).total_seconds() / 60
        if age > self.SENTIMENT_EXPIRE_MINUTES:
            return SentimentConfirmResult(
                confirmed=True,
                confidence_adjust=1.0,
                reason=f"情绪数据已过期({age:.0f}分钟)",
                should_filter=False,
            )
        
        # 判断情绪方向
        if sentiment_score > 0.2:
            sentiment_direction = "BUY"
        elif sentiment_score < -0.2:
            sentiment_direction = "SELL"
        else:
            sentiment_direction = "NEUTRAL"
        
        # 情绪确认逻辑
        if sentiment_direction == "NEUTRAL":
            return SentimentConfirmResult(
                confirmed=True,
                confidence_adjust=1.0,
                reason="情绪中性",
                should_filter=False,
            )
        
        # 情绪与信号同向
        if sentiment_direction == signal_direction:
            return SentimentConfirmResult(
                confirmed=True,
                confidence_adjust=1.2,
                reason=f"情绪确认: {sentiment_direction} 一致 (分数={sentiment_score:.2f})",
                should_filter=False,
            )
        
        # 情绪与信号反向
        abs_score = abs(sentiment_score)
        
        # 极端反向 → 过滤
        if abs_score >= self.EXTREME_POSITIVE or abs_score >= abs(self.EXTREME_NEGATIVE):
            return SentimentConfirmResult(
                confirmed=False,
                confidence_adjust=0.7,
                reason=f"极端情绪反向: 情绪={sentiment_direction} 信号={signal_direction}",
                should_filter=True,
            )
        
        # 一般反向 → 降权
        if abs_score > 0.4:
            return SentimentConfirmResult(
                confirmed=True,
                confidence_adjust=0.7,
                reason=f"情绪反向(弱): 情绪={sentiment_direction} 信号={signal_direction}",
                should_filter=False,
            )
        
        # 弱反向 → 不影响
        return SentimentConfirmResult(
            confirmed=True,
            confidence_adjust=1.0,
            reason="情绪反向(极弱)，不影响",
            should_filter=False,
        )
```

#### 4.2 修改共识引擎集成

**文件**: `app/strategy/consensus_engine.py` (修改)

```python
from app.factors.sentiment_confirm import SentimentConfirmer

class ConsensusEngine:
    def __init__(self, ...):
        # ... 现有代码 ...
        
        # 新增: 情绪确认器
        self.sentiment_confirmer = SentimentConfirmer()
    
    def _evaluate(self, code: str):
        """评估 - 增加情绪确认"""
        # ... 计算分数 ...
        
        if result:
            # 情绪确认检查
            signal_direction = result.action.value
            confirm_result = self.sentiment_confirmer.check_signal(
                code, signal_direction
            )
            
            # 极端反向 → 过滤
            if confirm_result.should_filter:
                logger.info(
                    f"[情绪过滤] {code} {signal_direction} 信号被情绪过滤: "
                    f"{confirm_result.reason}"
                )
                return
            
            # 调整置信度
            if confirm_result.confidence_adjust != 1.0:
                new_confidence = result.confidence * confirm_result.confidence_adjust
                result = dataclasses.replace(result, confidence=round(new_confidence, 2))
                logger.info(
                    f"[情绪确认] {code} 置信度调整: "
                    f"{result.confidence/confirm_result.confidence_adjust:.2f} → "
                    f"{result.confidence:.2f} ({confirm_result.reason})"
                )
```

---

### Phase 5: 退出引擎增强 (Day 3-4)

#### 5.1 增强退出引擎

**文件**: `app/risk/dynamic_exit_manager.py` (修改)

```python
# 添加成本感知退出

class DynamicExitManager:
    # ... 现有代码 ...
    
    def __init__(self, ...):
        # ... 现有代码 ...
        
        # 新增: 成本配置
        self.cost_config = {
            "HK": {
                "commission_rate": 0.0003,
                "commission_min": 3.0,
                "stamp_duty": 0.001,  # 仅卖出
                "trading_fee": 0.00005,
                "total_estimated": 0.0017,  # 约0.17%
            },
            "US": {
                "commission_rate": 0,
                "trading_fee": 0.0002,
                "total_estimated": 0.0004,  # 约0.04%
            },
        }
    
    def calculate_min_profit_target(self, market: str, entry_price: float) -> float:
        """
        计算最小盈利目标 (覆盖成本×2)
        """
        config = self.cost_config.get(market, self.cost_config["HK"])
        min_profit = config["total_estimated"] * 2  # 成本×2
        return min_profit
    
    def check_exit_priority(
        self,
        code: str,
        entry_price: float,
        current_price: float,
        ...
    ) -> 'ExitDecision':
        """增强版退出优先级检查 - 加入成本感知"""
        
        # ... 现有优先级检查 ...
        
        # 新增: 成本感知检查
        market = "HK" if code.startswith("HK.") else "US"
        min_profit = self.calculate_min_profit_target(market, entry_price)
        pnl_pct = (current_price - entry_price) / entry_price
        
        # 如果盈利但不足以覆盖成本，不触发止盈
        if pnl_pct > 0 and pnl_pct < min_profit:
            logger.debug(
                f"[成本感知] {code} 盈利{pnl_pct:.2%}不足覆盖成本{min_profit:.2%}，"
                f"暂不止盈"
            )
            return ExitDecision(should_exit=False)
        
        # ... 返回退出决策 ...
```

---

### Phase 6: 成本约束 (Day 4)

#### 6.1 创建成本约束检查器

**文件**: `app/risk/cost_constraint.py`

```python
"""成本约束检查器"""
from dataclasses import dataclass
from typing import Optional, Tuple
import logging

logger = logging.getLogger("ftseek.cost_constraint")


@dataclass
class CostAnalysis:
    """成本分析结果"""
    commission: float        # 佣金
    stamp_duty: float        # 印花税
    trading_fee: float       # 交易费
    total_cost: float        # 总成本
    cost_pct: float          # 成本占比
    min_profit_target: float # 最小盈利目标


@dataclass
class LiquidityCheck:
    """流动性检查结果"""
    passed: bool
    daily_volume: float      # 日均成交额
    spread: float            # 买卖价差
    position_limit: float    # 仓位限制
    use_limit_order: bool    # 是否必须限价单
    reason: str


class CostConstraintChecker:
    """成本约束检查器"""
    
    COST_CONFIG = {
        "HK": {
            "commission_rate": 0.0003,
            "commission_min": 3.0,
            "stamp_duty_rate": 0.001,  # 仅卖出
            "trading_fee_rate": 0.00005,
        },
        "US": {
            "commission_rate": 0,
            "trading_fee_rate": 0.0002,
        },
    }
    
    LIQUIDITY_THRESHOLDS = {
        "min_daily_volume_hkd": 10_000_000,  # 1000万港币
        "min_daily_volume_usd": 5_000_000,   # 500万美元
        "spread_limit_limit_order": 0.01,    # 价差>1%必须限价单
        "spread_limit_no_trade": 0.02,       # 价差>2%禁止交易
    }
    
    def __init__(self):
        # 每日交易次数记录
        self._daily_trades = {}  # code -> count
        logger.info("成本约束检查器初始化完成")
    
    def analyze_cost(
        self,
        code: str,
        quantity: int,
        price: float,
        is_sell: bool = False,
    ) -> CostAnalysis:
        """分析交易成本"""
        market = "HK" if code.startswith("HK.") else "US"
        config = self.COST_CONFIG[market]
        
        amount = quantity * price
        
        # 佣金
        commission = max(amount * config["commission_rate"], config.get("commission_min", 0))
        
        # 印花税 (仅港股卖出)
        stamp_duty = 0
        if is_sell and market == "HK":
            stamp_duty = amount * config["stamp_duty_rate"]
        
        # 交易费
        trading_fee = amount * config["trading_fee_rate"]
        
        total_cost = commission + stamp_duty + trading_fee
        cost_pct = total_cost / amount if amount > 0 else 0
        min_profit_target = cost_pct * 2  # 成本×2
        
        return CostAnalysis(
            commission=commission,
            stamp_duty=stamp_duty,
            trading_fee=trading_fee,
            total_cost=total_cost,
            cost_pct=cost_pct,
            min_profit_target=min_profit_target,
        )
    
    def check_liquidity(
        self,
        code: str,
        daily_volume: float,
        spread: float,
    ) -> LiquidityCheck:
        """检查流动性约束"""
        market = "HK" if code.startswith("HK.") else "US"
        
        # 检查成交额
        min_volume = (
            self.LIQUIDITY_THRESHOLDS["min_daily_volume_hkd"]
            if market == "HK" else
            self.LIQUIDITY_THRESHOLDS["min_daily_volume_usd"]
        )
        
        if daily_volume < min_volume:
            return LiquidityCheck(
                passed=False,
                daily_volume=daily_volume,
                spread=spread,
                position_limit=0,
                use_limit_order=True,
                reason=f"成交额不足: {daily_volume:.0f} < {min_volume:.0f}",
            )
        
        # 检查价差
        if spread >= self.LIQUIDITY_THRESHOLDS["spread_limit_no_trade"]:
            return LiquidityCheck(
                passed=False,
                daily_volume=daily_volume,
                spread=spread,
                position_limit=0,
                use_limit_order=True,
                reason=f"价差过大: {spread:.2%} >= 2%",
            )
        
        # 确定仓位限制和订单类型
        use_limit_order = spread >= self.LIQUIDITY_THRESHOLDS["spread_limit_limit_order"]
        
        # 根据流动性确定仓位限制
        if daily_volume >= min_volume * 10:
            position_limit = 0.15  # 高流动性
        elif daily_volume >= min_volume * 3:
            position_limit = 0.10  # 中等流动性
        else:
            position_limit = 0.05  # 低流动性
        
        return LiquidityCheck(
            passed=True,
            daily_volume=daily_volume,
            spread=spread,
            position_limit=position_limit,
            use_limit_order=use_limit_order,
            reason="流动性检查通过",
        )
    
    def check_daily_trade_limit(self, code: str, daily_volume: float) -> Tuple[bool, int]:
        """
        检查日内交易次数限制
        
        Returns:
            (can_trade, remaining_trades)
        """
        # 计算最大交易次数: 基础3次 + 每10亿成交额+2次
        max_trades = 3 + int(daily_volume / 1_000_000_000) * 2
        max_trades = min(max_trades, 10)  # 最多10次
        
        current_trades = self._daily_trades.get(code, 0)
        remaining = max_trades - current_trades
        
        if remaining <= 0:
            return False, 0
        
        return True, remaining
    
    def record_trade(self, code: str):
        """记录交易"""
        self._daily_trades[code] = self._daily_trades.get(code, 0) + 1
    
    def reset_daily(self):
        """重置每日计数"""
        self._daily_trades = {}
```

#### 6.2 集成到交易执行器

**文件**: `app/trader/executor.py` (修改)

```python
from app.risk.cost_constraint import CostConstraintChecker

class TradeExecutor:
    def __init__(self, ...):
        # ... 现有代码 ...
        
        # 新增: 成本约束检查器
        self.cost_checker = CostConstraintChecker()
    
    def execute(self, signal: Signal, trade_type: str = 'AUTO') -> TradeResult:
        """执行交易 - 增加成本检查"""
        
        # ... 现有检查 ...
        
        # 新增: 成本分析
        is_sell = signal.action == SignalAction.SELL
        cost_analysis = self.cost_checker.analyze_cost(
            signal.code, signal.quantity, signal.price, is_sell
        )
        
        logger.info(
            f"[成本分析] {signal.code} 交易额={signal.quantity * signal.price:.2f} "
            f"成本={cost_analysis.total_cost:.2f} ({cost_analysis.cost_pct:.2%}) "
            f"最小盈利目标={cost_analysis.min_profit_target:.2%}"
        )
        
        # 新增: 流动性检查
        # ... 需要从行情获取 daily_volume 和 spread ...
        
        # ... 后续交易逻辑 ...
```

---

### Phase 7: 集成测试 (Day 5)

#### 7.1 单元测试

**文件**: `tests/test_optimization_v3.py`

```python
"""V3优化单元测试"""
import pytest
from app.strategy.strategy_layer import StrategyLayerManager
from app.signal_engine.multi_timeframe import MultiTimeframeManager, TrendDirection
from app.signal_engine.dynamic_threshold import DynamicThresholdManager, ThresholdContext
from app.factors.sentiment_confirm import SentimentConfirmer
from app.risk.cost_constraint import CostConstraintChecker


class TestStrategyLayerManager:
    """策略层级管理器测试"""
    
    def test_exclusion_rule(self):
        """测试互斥规则"""
        manager = StrategyLayerManager(["confluence", "rsi", "macd"])
        
        # confluence 启用时，rsi 和 macd 应被排除
        assert "confluence" in manager.get_effective_strategies()
        assert "rsi" not in manager.get_effective_strategies()
        assert "macd" not in manager.get_effective_strategies()
    
    def test_vote_weight(self):
        """测试投票权重"""
        manager = StrategyLayerManager(["confluence"])
        
        assert manager.get_vote_weight("confluence") == 1.2
        assert manager.get_vote_weight("rsi") == 0.5


class TestMultiTimeframeManager:
    """多周期窗口测试"""
    
    def test_alignment(self):
        """测试多周期对齐"""
        manager = MultiTimeframeManager()
        
        # 设置三周期同向
        manager.update_signal("HK.09992", "daily", TrendDirection.UP, 0.8, [])
        manager.update_signal("HK.09992", "hourly", TrendDirection.UP, 0.7, [])
        manager.update_signal("HK.09992", "minute", TrendDirection.UP, 0.6, [])
        
        is_aligned, multiplier = manager.check_alignment("HK.09992", TrendDirection.UP)
        
        assert is_aligned is True
        assert multiplier == 1.3  # 三周期对齐
    
    def test_counter_trend(self):
        """测试逆趋势信号"""
        manager = MultiTimeframeManager()
        
        # 日线向下，分钟向上
        manager.update_signal("HK.09992", "daily", TrendDirection.DOWN, 0.8, [])
        manager.update_signal("HK.09992", "minute", TrendDirection.UP, 0.6, [])
        
        is_aligned, multiplier = manager.check_alignment("HK.09992", TrendDirection.UP)
        
        assert is_aligned is True
        assert multiplier == 0.5  # 逆日线降权


class TestDynamicThreshold:
    """动态阈值测试"""
    
    def test_high_volatility(self):
        """测试高波动阈值提高"""
        manager = DynamicThresholdManager()
        
        ctx = ThresholdContext(
            volatility=0.04,  # 高波动
            adx=15,           # 低ADX
            signal_count=30,  # 信号多
            win_rate=0.4,     # 胜率低
        )
        
        threshold = manager.calculate_threshold(ctx)
        
        # 高波动应提高阈值
        assert threshold > manager.base_threshold
    
    def test_trending_market(self):
        """测试趋势市场阈值降低"""
        manager = DynamicThresholdManager()
        
        ctx = ThresholdContext(
            volatility=0.02,
            adx=35,  # 高ADX趋势
            signal_count=10,
            win_rate=0.6,
        )
        
        threshold = manager.calculate_threshold(ctx)
        
        # 趋势市场可以降低阈值
        assert threshold <= manager.base_threshold


class TestSentimentConfirmer:
    """情绪确认器测试"""
    
    def test_sentiment_confirm(self):
        """测试情绪确认"""
        confirmer = SentimentConfirmer()
        confirmer.update_sentiment("HK.09992", 0.5)  # 正面情绪
        
        result = confirmer.check_signal("HK.09992", "BUY")
        
        assert result.confirmed is True
        assert result.confidence_adjust == 1.2  # 情绪一致提升
    
    def test_sentiment_filter(self):
        """测试情绪过滤"""
        confirmer = SentimentConfirmer()
        confirmer.update_sentiment("HK.09992", 0.8)  # 极端正面
        
        result = confirmer.check_signal("HK.09992", "SELL")
        
        assert result.should_filter is True  # 极端反向过滤


class TestCostConstraint:
    """成本约束测试"""
    
    def test_cost_analysis(self):
        """测试成本分析"""
        checker = CostConstraintChecker()
        
        # 港股买入
        result = checker.analyze_cost("HK.09992", 200, 200.0, is_sell=False)
        
        assert result.total_cost > 0
        assert result.min_profit_target > 0
    
    def test_liquidity_check(self):
        """测试流动性检查"""
        checker = CostConstraintChecker()
        
        # 低流动性
        result = checker.check_liquidity("HK.09992", 5_000_000, 0.005)
        assert result.passed is False  # 成交额不足
        
        # 高流动性
        result = checker.check_liquidity("HK.09992", 50_000_000, 0.005)
        assert result.passed is True
```

#### 7.2 集成测试脚本

**文件**: `scripts/test_v3_integration.py`

```python
"""V3优化集成测试"""
import sys
sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.strategy.consensus_engine import ConsensusEngine
from app.strategy.base import Signal, SignalAction
from app.strategy.strategy_layer import StrategyLayerManager
from app.signal_engine.multi_timeframe import MultiTimeframeManager, TrendDirection
from app.signal_engine.dynamic_threshold import DynamicThresholdManager, ThresholdContext
from app.factors.sentiment_confirm import SentimentConfirmer


def test_full_flow():
    """测试完整信号流程"""
    print("\n=== V3优化集成测试 ===\n")
    
    # 1. 初始化各组件
    print("1. 初始化组件...")
    layer_manager = StrategyLayerManager(["confluence", "price_break"])
    mtf_manager = MultiTimeframeManager()
    threshold_manager = DynamicThresholdManager()
    sentiment_confirmer = SentimentConfirmer()
    
    # 2. 模拟多周期信号
    print("\n2. 模拟多周期信号...")
    mtf_manager.update_signal("HK.09992", "daily", TrendDirection.UP, 0.8, [])
    mtf_manager.update_signal("HK.09992", "hourly", TrendDirection.UP, 0.7, [])
    mtf_manager.update_signal("HK.09992", "minute", TrendDirection.UP, 0.6, [])
    
    # 3. 计算动态阈值
    print("\n3. 计算动态阈值...")
    ctx = ThresholdContext(
        volatility=0.025,
        adx=28,
        signal_count=15,
        win_rate=0.55,
    )
    threshold = threshold_manager.calculate_threshold(ctx)
    print(f"   动态阈值: {threshold:.2f}")
    
    # 4. 模拟策略投票
    print("\n4. 策略投票...")
    effective_strategies = layer_manager.get_effective_strategies()
    print(f"   有效投票策略: {effective_strategies}")
    
    # 5. 情绪确认
    print("\n5. 情绪确认...")
    sentiment_confirmer.update_sentiment("HK.09992", 0.3)
    confirm_result = sentiment_confirmer.check_signal("HK.09992", "BUY")
    print(f"   情绪确认: {confirm_result.reason}")
    print(f"   置信度调整: {confirm_result.confidence_adjust:.2f}")
    
    # 6. 多周期对齐
    print("\n6. 多周期对齐检查...")
    is_aligned, multiplier = mtf_manager.check_alignment("HK.09992", TrendDirection.UP)
    print(f"   对齐结果: {is_aligned}")
    print(f"   置信度乘数: {multiplier:.2f}")
    
    print("\n=== 测试完成 ===\n")


if __name__ == "__main__":
    test_full_flow()
```

---

## 三、部署计划

### 3.1 部署步骤

```bash
# Day 1: 策略去重
cd /opt/ftseek/FTSeek
git checkout -b feature/v3-optimization

# 创建新模块
touch app/strategy/strategy_layer.py
# 编辑 consensus_engine.py 集成策略分层

# Day 2: 多周期窗口
touch app/signal_engine/multi_timeframe.py
touch app/signal_engine/dynamic_threshold.py

# Day 3: 情绪降级 + 退出增强
touch app/factors/sentiment_confirm.py
# 修改 dynamic_exit_manager.py

# Day 4: 成本约束
touch app/risk/cost_constraint.py
# 修改 executor.py

# Day 5: 测试
pytest tests/test_optimization_v3.py -v
python scripts/test_v3_integration.py

# 部署
git add .
git commit -m "feat: V3优化 - 策略去重/多周期/动态阈值/情绪降级/成本约束"
git push origin feature/v3-optimization
```

### 3.2 回滚计划

```bash
# 如果出现问题，回滚到稳定版本
git checkout master
bash start.sh
```

---

## 四、验收清单

### 4.1 功能验收

- [ ] **策略去重**: 启用 confluence 时，rsi/macd/bollinger 不参与投票
- [ ] **多周期窗口**: 日线/小时/分钟三级窗口正常工作
- [ ] **动态阈值**: 阈值根据波动率/ADX/信号频率自动调整
- [ ] **情绪降级**: 情绪不参与投票，仅作为确认/过滤变量
- [ ] **成本约束**: 交易前进行成本分析和流动性检查
- [ ] **退出引擎**: 按优先级正确执行退出逻辑

### 4.2 性能验收

| 指标 | 基线值 | 目标值 | 验收标准 |
|------|--------|--------|----------|
| 日信号数 | 150+ | 20-50 | ≤50 |
| 信号胜率 | 23% | ≥55% | ≥50% |
| 策略冗余 | 60% | 0% | 0% |
| 日均交易成本 | 未知 | <利润10% | <10% |

### 4.3 文档验收

- [ ] SPEC文档完整
- [ ] PLAN文档完整
- [ ] 代码注释完整
- [ ] 配置文件说明完整

---

*文档版本: 3.0*  
*创建时间: 2026-03-23*  
*状态: 待执行*