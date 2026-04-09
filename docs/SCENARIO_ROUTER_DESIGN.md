# FTSeek 场景识别与策略路由整合设计方案

> **版本**: v1.0  
> **创建时间**: 2026-04-09  
> **设计目标**: 构建场景驱动的策略路由系统，实现更广覆盖、更少冲突、更稳定的收益表现

---

## 一、核心设计理念

### 1.1 设计哲学

融合 Gemini 和 GPT 两个方案的共性优势，构建分层场景识别与动态策略路由系统：

| Gemini 方案优势 | GPT 方案优势 | 整合策略 |
|----------------|-------------|---------|
| 7 场景明确定义 | 10+ 场景细粒度 | **分层场景体系**：7 主场景 + 16 子场景 |
| 一致性平滑器 | 场景记忆器 | **双窗口防抖**：确认窗口 + 冷却窗口 |
| 场景性能矩阵 | 绩效反馈闭环 | **自适应学习**：用实盘表现反哺权重 |
| 三段式路由 | 四层架构 | **统一数据流**：特征→场景→路由→共识 |
| 信号抢占逻辑 | 优先级裁决 | **事件优先 + 多周期共振** |

### 1.2 核心洞察

> **问题本质不是"选什么策略"，而是：如何把市场从连续噪声，稳定地映射成有限个可执行场景。**

场景识别是整个系统的命门。如果场景识别不准，后面所有策略路由、共识加权、风控参数都会被带偏。

### 1.3 设计原则

1. **场景优先于指标** - 指标只是特征，场景才是交易语义
2. **规则优先于模型** - 第一版采用规则引擎 + 评分机制，保证可解释、易调试
3. **多周期共振** - 短周期负责入场时机，中周期负责结构确认，长周期负责方向过滤
4. **场景稳定性优先** - 场景不能频繁抖动，必须有持续确认和冷却机制
5. **支持反馈学习** - 场景识别结果必须记录并回测，用真实绩效反哺识别权重

---

## 二、总体架构设计

```
┌─────────────────────────────────────────────────────────────┐
│                    FTSeek 场景路由系统                       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  行情数据 → Feature Extractor → Scenario Classifier        │
│                              ↓                              │
│  ┌─────────────────────────────────────────────────────┐   │
│  │              Scenario Router (核心层)                │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌───────────┐  │   │
│  │  │ 策略激活器   │  │ 权重计算器   │  │ 优先级仲裁│  │   │
│  │  └──────────────┘  └──────────────┘  └───────────┘  │   │
│  └─────────────────────────────────────────────────────┘   │
│                              ↓                              │
│  激活策略池 → Consensus Engine → Risk Manager → Executor  │
│                              ↓                              │
│  Performance Feedback → 场景权重自适应调整                  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 2.1 数据流

```
行情数据 / 新闻数据 / 波动率数据
            ↓
      Feature Extractor (特征提取)
            ↓
       Rule Engine (规则引擎)
            ↓
      Scenario Scorer (场景评分)
            ↓
      Scenario Arbiter (场景裁决)
            ↓
      Scenario Memory (场景记忆)
            ↓
   输出场景标签 + 置信度
            ↓
      Scenario Router (策略路由)
            ↓
   策略池（按场景选择）
            ↓
   Consensus Engine 共识融合
            ↓
   Risk Manager 风控过滤
            ↓
   Trade Executor 执行
            ↓
   Performance Feedback 绩效反馈
```

---

## 三、场景分类体系

### 3.1 7 大主场景 × 16 子场景

```python
SCENARIO_HIERARCHY = {
    # === 趋势类 ===
    "TREND": {
        "TREND_START":      {"id": 0, "priority": 3, "duration_min": 3},  # 趋势启动
        "TREND_STRONG":     {"id": 1, "priority": 4, "duration_min": 3},  # 强趋势
        "TREND_EXHAUST":    {"id": 2, "priority": 2, "duration_min": 2},  # 趋势衰竭
    },
    
    # === 震荡类 ===
    "RANGE": {
        "RANGE_BOUND":      {"id": 3, "priority": 2, "duration_min": 3},  # 箱体震荡
        "MEAN_REVERSION":   {"id": 4, "priority": 2, "duration_min": 2},  # 均值回归
    },
    
    # === 事件类 ===
    "EVENT": {
        "GAP_EVENT":        {"id": 5, "priority": 1, "duration_min": 1},  # 跳空事件
        "NEWS_DRIVEN":      {"id": 6, "priority": 1, "duration_min": 1},  # 新闻驱动
    },
    
    # === 波动类 ===
    "VOLATILITY": {
        "VOL_CONTRACTION":  {"id": 7, "priority": 3, "duration_min": 2},  # 波动收缩
        "VOL_EXPANSION":    {"id": 8, "priority": 2, "duration_min": 2},  # 波动扩张
    },
    
    # === 反转类 ===
    "REVERSAL": {
        "REVERSAL_SETUP":   {"id": 9, "priority": 2, "duration_min": 2},  # 反转预备
        "REVERSAL_CONFIRMED":{"id":10, "priority": 3, "duration_min": 2},  # 反转确认
    },
    
    # === 突破类 ===
    "BREAKOUT": {
        "BREAKOUT_SETUP":   {"id": 11, "priority": 3, "duration_min": 2},  # 突破预备
        "BREAKOUT_CONFIRMED":{"id":12, "priority": 4, "duration_min": 2},  # 突破确认
        "FALSE_BREAKOUT":   {"id": 13, "priority": 2, "duration_min": 1},  # 假突破
    },
    
    # === 特殊类 ===
    "SPECIAL": {
        "CROSS_MARKET":     {"id": 14, "priority": 2, "duration_min": 2},  # 跨市场联动
        "OPEN_CLOSE_EFFECT":{"id": 15, "priority": 2, "duration_min": 1},  # 开收盘效应
    },
}
```

### 3.2 场景特征定义

| ID | 场景类型 | 特征表现 (触发条件) | 建议策略组 |
|:---|:---|:---|:---|
| 0 | TREND_START | ADX>25 且上升，均线开始排列，成交量放大 | 123 法则、海龟系统 |
| 1 | TREND_STRONG | ADX>30, HH/HL 或 LH/LL 结构，价格偏离 MA>2% | SuperTrend、融合策略 |
| 2 | TREND_EXHAUST | ADX>40 但价格动量衰减，TD 计数耗尽 | TD 区间、摆荡交易 |
| 3 | RANGE_BOUND | ADX<20, 布林带收窄，RSI 在 40-60 震荡 | RSI、Bollinger |
| 4 | MEAN_REVERSION | 价格极端偏离 MA 后回归，RSI 超买/超卖 | 摆荡策略、融合策略 |
| 5 | GAP_EVENT | 开盘跳空>2%, 成交量放大>1.5 倍，有新闻/公告 | 跳空策略、事件驱动 |
| 6 | NEWS_DRIVEN | LLM 情绪评分>0.8, 突发新闻警报 | 事件驱动、情绪 AI |
| 7 | VOL_CONTRACTION | ATR 百分位<20, 布林带宽度收窄 | 震荡策略、Bollinger |
| 8 | VOL_EXPANSION | ATR 飙升>2σ, 布林带扩张，成交量放大 | 波动策略、跳空策略 |
| 9 | REVERSAL_SETUP | 长单边后动能衰减，结构破坏迹象 | 123 法则、TD 区间 |
| 10 | REVERSAL_CONFIRMED | 结构确认反转，价格突破关键位 | 摆荡策略、123 法则 |
| 11 | BREAKOUT_SETUP | 价格接近区间边界，蓄势待发 | 海龟系统、TD 突破 |
| 12 | BREAKOUT_CONFIRMED | 价格突破区间，成交量确认 | 海龟系统、融合策略 |
| 13 | FALSE_BREAKOUT | 突破后快速回调，成交量不支持 | 摆荡策略、RSI |
| 14 | CROSS_MARKET | 港股与隔夜美股涨跌高度正相关 | 跨市场套利 |
| 15 | OPEN_CLOSE_EFFECT | 开盘/收盘前后 30 分钟，成交量异常 | 开收盘策略 |

---

## 四、场景识别器设计

### 4.1 四层识别流水线

```python
class ScenarioClassifier:
    """
    场景分类器 - 四层流水线设计
    """
    
    def __init__(self):
        self.feature_extractor = FeatureExtractor()
        self.rule_engine = RuleEngine()         # 第一层：快速规则判断
        self.scenario_scorer = ScenarioScorer() # 第二层：多场景评分
        self.arbiter = ScenarioArbiter()        # 第三层：冲突裁决
        self.memory = ScenarioMemory()          # 第四层：平滑防抖
    
    def classify(self, code: str, kline_data: Dict, news_data: Dict = None) -> ScenarioOutput:
        """
        场景分类主流程
        """
        # Layer 1: 特征提取
        features = self.feature_extractor.extract(code, kline_data, news_data)
        
        # Layer 2: 规则引擎（快速通道）
        rule_result = self.rule_engine.evaluate(features)
        if rule_result.is_clear_match:
            # 规则明确匹配，快速返回
            return self._build_output(rule_result.scenario, features, method="RULE")
        
        # Layer 3: 多场景评分
        scores = self.scenario_scorer.score(features)
        
        # Layer 4: 冲突裁决
        primary_scenario, confidence = self.arbiter.select(scores, features)
        
        # Layer 5: 平滑防抖
        smoothed_scenario = self.memory.apply_smoothing(code, primary_scenario, confidence)
        
        return self._build_output(smoothed_scenario, features, confidence, method="SCORE")
```

### 4.2 特征提取器

```python
class FeatureExtractor:
    """
    特征提取器 - 6 大维度
    """
    
    def extract(self, code: str, kline_data: Dict, news_data: Dict = None) -> Features:
        return Features(
            # 1. 趋势特征
            trend=TrendFeatures(
                adx=calc_adx(kline_data),
                ma_slope=calc_ma_slope(kline_data),
                ma_alignment=check_ma_alignment(kline_data),
                structure=identify_structure(kline_data),  # HH/HL or LH/LL
            ),
            
            # 2. 波动特征
            volatility=VolatilityFeatures(
                atr=calc_atr(kline_data),
                bb_width=calc_bollinger_width(kline_data),
                realized_vol=calc_realized_vol(kline_data),
            ),
            
            # 3. 量能特征
            volume=VolumeFeatures(
                volume_ratio=calc_volume_ratio(kline_data),
                volume_trend=calc_volume_slope(kline_data),
            ),
            
            # 4. 结构特征
            structure=StructureFeatures(
                box_range=identify_box_range(kline_data),
                breakout_level=check_breakout(kline_data),
            ),
            
            # 5. 事件特征
            event=EventFeatures(
                gap_pct=calc_gap_percent(kline_data),
                news_score=news_data.get('sentiment_score', 0) if news_data else 0,
                has_announcement=news_data.get('has_announcement', False) if news_data else False,
            ),
            
            # 6. 位置特征
            position=PositionFeatures(
                price_vs_vwap=calc_price_vs_vwap(kline_data),
                price_vs_ma=calc_price_vs_ma(kline_data),
                rsi=calc_rsi(kline_data),
            ),
        )
```

### 4.3 规则引擎

```python
class RuleEngine:
    """
    规则引擎 - 快速通道判断
    """
    
    def evaluate(self, features: Features) -> RuleResult:
        # GAP_EVENT - 最高优先级
        if self._check_gap_event(features):
            return RuleResult(scenario="GAP_EVENT", is_clear_match=True)
        
        # NEWS_DRIVEN
        if self._check_news_driven(features):
            return RuleResult(scenario="NEWS_DRIVEN", is_clear_match=True)
        
        # TREND_STRONG
        if self._check_trend_strong(features):
            return RuleResult(scenario="TREND_STRONG", is_clear_match=True)
        
        # RANGE_BOUND
        if self._check_range_bound(features):
            return RuleResult(scenario="RANGE_BOUND", is_clear_match=True)
        
        # VOL_EXPANSION
        if self._check_vol_expansion(features):
            return RuleResult(scenario="VOL_EXPANSION", is_clear_match=True)
        
        # 无明确匹配，进入评分流程
        return RuleResult(scenario=None, is_clear_match=False)
    
    def _check_gap_event(self, features: Features) -> bool:
        """跳空事件规则"""
        return (
            features.event.gap_pct >= 2.0 and
            features.volume.volume_ratio >= 1.5 and
            (features.event.news_score > 0.5 or features.event.has_announcement)
        )
    
    def _check_trend_strong(self, features: Features) -> bool:
        """强趋势规则"""
        return (
            features.trend.adx >= 25 and
            features.trend.ma_alignment in ["bullish", "bearish"] and
            features.trend.structure in ["HH_HL", "LH_LL"]
        )
    
    def _check_range_bound(self, features: Features) -> bool:
        """震荡市规则"""
        return (
            features.trend.adx < 20 and
            features.volatility.bb_width < 0.05 and
            40 <= features.position.rsi <= 60
        )
    
    def _check_vol_expansion(self, features: Features) -> bool:
        """波动扩张规则"""
        return (
            features.volatility.atr >= 2.0 * features.volatility.atr_ma and
            features.volume.volume_ratio >= 1.5
        )
```

### 4.4 场景评分器

```python
class ScenarioScorer:
    """
    场景评分器 - 多场景加权打分
    """
    
    def score(self, features: Features) -> Dict[str, float]:
        scores = {}
        
        # TREND_STRONG 评分
        scores["TREND_STRONG"] = (
            0.25 * self._adx_score(features.trend.adx) +
            0.20 * self._ma_alignment_score(features.trend.ma_alignment) +
            0.20 * self._structure_score(features.trend.structure) +
            0.15 * self._volume_score(features.volume.volume_ratio) +
            0.20 * self._pullback_score(features.trend.pullback_quality)
        )
        
        # RANGE_BOUND 评分
        scores["RANGE_BOUND"] = (
            0.30 * self._low_adx_score(features.trend.adx) +
            0.25 * self._bb_width_score(features.volatility.bb_width) +
            0.25 * self._rsi_range_score(features.position.rsi) +
            0.20 * self._box_range_score(features.structure.box_range)
        )
        
        # GAP_EVENT 评分
        scores["GAP_EVENT"] = (
            0.40 * self._gap_score(features.event.gap_pct) +
            0.30 * self._volume_gap_score(features.volume.volume_ratio) +
            0.30 * self._news_score(features.event.news_score)
        )
        
        # ... 其他场景评分
        
        # 归一化
        total = sum(scores.values())
        if total > 0:
            scores = {k: v/total for k, v in scores.items()}
        
        return scores
```

### 4.5 场景裁决器

```python
class ScenarioArbiter:
    """
    场景裁决器 - 冲突处理
    """
    
    def select(self, scores: Dict[str, float], features: Features) -> Tuple[str, float]:
        """
        根据评分和特征选择最终场景
        
        裁决原则:
        1. 事件类优先于技术类
        2. 高波动优先于普通趋势
        3. 趋势启动优先于震荡
        4. 强场景优先于弱场景
        5. 多周期共振优先于单周期信号
        """
        # 事件类优先
        if scores.get("GAP_EVENT", 0) > 0.6:
            return "GAP_EVENT", scores["GAP_EVENT"]
        if scores.get("NEWS_DRIVEN", 0) > 0.6:
            return "NEWS_DRIVEN", scores["NEWS_DRIVEN"]
        
        # 高波动优先
        if scores.get("VOL_EXPANSION", 0) > 0.5:
            return "VOL_EXPANSION", scores["VOL_EXPANSION"]
        
        # 趋势优先于震荡
        trend_score = scores.get("TREND_STRONG", 0) + scores.get("TREND_START", 0)
        range_score = scores.get("RANGE_BOUND", 0)
        
        if trend_score > 0.5 and trend_score > range_score:
            return "TREND_STRONG", trend_score
        
        # 默认返回最高分
        best_scenario = max(scores, key=scores.get)
        return best_scenario, scores[best_scenario]
```

### 4.6 场景记忆器（防抖）

```python
class ScenarioMemory:
    """
    场景记忆器 - 平滑防抖
    """
    
    def __init__(self, confirm_window: int = 3, cool_down_window: int = 2):
        self.confirm_window = confirm_window  # 场景确认需要连续 K 线数
        self.cool_down_window = cool_down_window  # 切换后冷却期
        self.scenario_history: Dict[str, List[str]] = defaultdict(list)
        self.last_switch_time: Dict[str, float] = {}
    
    def apply_smoothing(self, code: str, candidate_scenario: str, confidence: float) -> str:
        """
        应用平滑逻辑
        
        规则:
        1. 场景必须连续 confirm_window 次出现才切换
        2. 切换后 cool_down_window 分钟内不允许回切
        3. 置信度低于阈值时保持原场景
        """
        history = self.scenario_history[code]
        
        # 添加当前候选
        history.append(candidate_scenario)
        if len(history) > self.confirm_window + 2:
            history.pop(0)
        
        # 置信度检查
        if confidence < 0.65:
            return history[-2] if len(history) > 1 else candidate_scenario
        
        # 连续确认检查
        recent = history[-self.confirm_window:]
        if len(recent) >= self.confirm_window:
            # 检查是否连续一致
            if all(s == candidate_scenario for s in recent):
                # 检查冷却期
                if code in self.last_switch_time:
                    elapsed = time.time() - self.last_switch_time[code]
                    if elapsed < self.cool_down_window * 60:
                        return history[-2] if len(history) > 1 else candidate_scenario
                
                # 切换场景
                self.last_switch_time[code] = time.time()
                return candidate_scenario
        
        # 返回众数
        return max(set(history), key=history.count)
```

---

## 五、策略路由器设计

### 5.1 路由矩阵

| 场景 | 主策略 (1.5x) | 辅助策略 (1.0x) | 抑制策略 (0.3x) | 风控模板 |
|:---|:---|:---|:---|:---|
| `TREND_START` | 123 法则 | 海龟系统、TD 区间 | RSI、Bollinger | 趋势止损 |
| `TREND_STRONG` | 海龟系统 | 123 法则、融合策略 | RSI、震荡策略 | 移动止损 |
| `TREND_EXHAUST` | TD 区间 | 123 法则、摆荡 | 趋势跟踪 | 紧止损 |
| `RANGE_BOUND` | RSI、Bollinger | 融合策略、开收盘 | SuperTrend | 区间止损 |
| `MEAN_REVERSION` | 摆荡策略 | 融合策略 | 趋势跟踪 | 快速止盈 |
| `GAP_EVENT` | 跳空策略 | 事件驱动、情绪 AI | 技术指标 | 失败即止损 |
| `NEWS_DRIVEN` | 事件驱动 | 情绪 AI、融合策略 | 纯技术策略 | 紧止损 |
| `VOL_CONTRACTION` | 震荡策略 | Bollinger、RSI | 趋势策略 | 等待突破 |
| `VOL_EXPANSION` | 波动策略 | 跳空策略、123 法则 | 均值回归 | 分段止盈 |
| `REVERSAL_SETUP` | 123 法则 | TD 区间、摆荡 | 趋势跟踪 | 严格止损 |
| `REVERSAL_CONFIRMED` | 摆荡策略 | 123 法则 | 趋势跟踪 | 快速止盈 |
| `BREAKOUT_SETUP` | 海龟系统 | 123 法则、TD 突破 | 震荡策略 | 突破确认 |
| `BREAKOUT_CONFIRMED` | 海龟系统 | 融合策略、趋势 | 震荡策略 | 移动止损 |
| `FALSE_BREAKOUT` | 摆荡策略 | RSI、Bollinger | 突破策略 | 紧止损 |
| `CROSS_MARKET` | 跨市场套利 | 融合策略 | - | 对冲风控 |
| `OPEN_CLOSE_EFFECT` | 开收盘策略 | 融合策略 | - | 短周期 |

### 5.2 路由器实现

```python
class ScenarioRouter:
    """
    场景路由器 - 策略调度核心
    """
    
    def __init__(self):
        self.routing_matrix = self._build_routing_matrix()
        self.base_weights = {
            "confluence_v2": 1.0,
            "supertrend": 1.0,
            "sentiment_ai": 0.5,
            "event_driven": 0.8,
            "cross_market_arbitrage": 0.6,
            "enhanced_confluence": 1.0,
            "open_close_effect": 0.7,
        }
    
    def route(self, scenario: ScenarioOutput) -> RouteResult:
        """
        根据场景输出路由结果
        """
        config = self.routing_matrix.get(scenario.primary_scenario)
        
        if not config:
            return self._default_route()
        
        # 计算最终权重
        final_weights = {}
        for strategy, base in self.base_weights.items():
            if strategy in config["active_strategies"]:
                multiplier = config["weight_multipliers"].get(strategy, 1.0)
                final_weights[strategy] = base * multiplier
            else:
                final_weights[strategy] = 0.0
        
        return RouteResult(
            active_strategies=config["active_strategies"],
            primary_strategy=config["primary_strategy"],
            secondary_strategies=config["secondary_strategies"],
            weights=final_weights,
            risk_template=config["risk_template"],
            priority=config["priority"],
        )
    
    def _build_routing_matrix(self) -> Dict:
        """构建路由矩阵"""
        return {
            "TREND_STRONG": RouteConfig(
                active_strategies=["supertrend", "confluence_v2", "enhanced_confluence"],
                primary_strategy="supertrend",
                secondary_strategies=["confluence_v2", "enhanced_confluence"],
                weight_multipliers={"supertrend": 1.5, "confluence_v2": 1.2, "rsi": 0.3},
                risk_template="trend_stop",
                priority=4,
            ),
            "RANGE_BOUND": RouteConfig(
                active_strategies=["rsi", "bollinger", "confluence_v2", "open_close_effect"],
                primary_strategy="rsi",
                secondary_strategies=["bollinger", "confluence_v2"],
                weight_multipliers={"rsi": 1.5, "bollinger": 1.5, "supertrend": 0.3},
                risk_template="range_stop",
                priority=2,
            ),
            "GAP_EVENT": RouteConfig(
                active_strategies=["event_driven", "sentiment_ai", "confluence_v2"],
                primary_strategy="event_driven",
                secondary_strategies=["sentiment_ai"],
                weight_multipliers={"event_driven": 2.0, "sentiment_ai": 1.5},
                risk_template="gap_fail_stop",
                priority=1,  # 最高优先级
            ),
            # ... 其他场景配置
        }
```

---

## 六、共识层集成

### 6.1 加权共识公式

```python
def calculate_consensus_score(signals: List[Signal], route: RouteResult) -> ConsensusResult:
    """
    加权共识评分
    
    final_score = Σ(strategy_weight × direction × confidence × regime_fit × quality_factor)
    """
    total_score = 0.0
    total_weight = 0.0
    
    for signal in signals:
        strategy = signal.strategy
        if strategy not in route.weights:
            continue
        
        # 基础权重
        base_weight = route.weights.get(strategy, 0.0)
        if base_weight <= 0:
            continue
        
        # 场景适配度（该策略在历史该场景下的表现）
        regime_fit = get_strategy_regime_performance(strategy, route.scenario)
        
        # 质量因子（该策略近期表现）
        quality_factor = get_strategy_quality_factor(strategy, window_days=30)
        
        # 最终权重
        final_weight = base_weight * regime_fit * quality_factor
        
        # 累加
        total_score += final_weight * signal.direction * signal.confidence
        total_weight += final_weight
    
    # 归一化
    normalized_score = total_score / total_weight if total_weight > 0 else 0.0
    
    return ConsensusResult(
        score=normalized_score,
        total_weight=total_weight,
        signal_count=len(signals),
        recommendation="BUY" if normalized_score > 0.65 else "SELL" if normalized_score < -0.65 else "HOLD",
    )
```

### 6.2 信号优先级处理

```python
class DecisionBuffer:
    """
    决策缓冲器 - 支持信号优先级抢占
    """
    
    def __init__(self, max_size: int = 50):
        self.buffer: List[PrioritizedSignal] = []
        self.max_size = max_size
    
    def add_signal(self, signal: Signal, priority: int) -> bool:
        """
        添加信号到缓冲区
        
        优先级: 1-9, 9 最高
        高优先级信号可以挤掉低优先级信号
        """
        if len(self.buffer) >= self.max_size:
            # 缓冲区已满，检查是否可以挤掉低优先级信号
            min_priority_idx = min(range(len(self.buffer)), 
                                   key=lambda i: self.buffer[i].priority)
            if priority > self.buffer[min_priority_idx].priority:
                # 挤掉低优先级信号
                self.buffer.pop(min_priority_idx)
            else:
                logger.debug(f"[决策缓冲] ❌ 拒绝信号：优先级{priority} 低于队列最低")
                return False
        
        self.buffer.append(PrioritizedSignal(signal=signal, priority=priority))
        self.buffer.sort(key=lambda x: x.priority, reverse=True)
        return True
    
    def get_next_signal(self) -> Optional[Signal]:
        """获取下一个待处理信号"""
        if not self.buffer:
            return None
        return self.buffer.pop(0).signal
```

---

## 七、绩效反馈机制

```python
class PerformanceFeedback:
    """
    绩效反馈 - 场景识别自适应学习
    """
    
    def __init__(self):
        self.db = get_database()
        self.scenario_stats = defaultdict(lambda: {
            "trades": 0,
            "wins": 0,
            "total_pnl": 0.0,
            "win_rate": 0.0,
        })
    
    def record_trade(self, scenario: str, strategy: str, pnl: float, pnl_pct: float):
        """记录交易结果"""
        stats = self.scenario_stats[scenario]
        stats["trades"] += 1
        if pnl > 0:
            stats["wins"] += 1
        stats["total_pnl"] += pnl
        stats["win_rate"] = stats["wins"] / stats["trades"] if stats["trades"] > 0 else 0
    
    def get_scenario_accuracy(self, scenario: str) -> float:
        """获取某场景的识别准确率"""
        stats = self.scenario_stats.get(scenario)
        if not stats or stats["trades"] < 5:
            return 0.5  # 数据不足返回中性值
        return stats["win_rate"]
    
    def adjust_weights(self, scenario: str, router: ScenarioRouter):
        """根据场景表现调整权重"""
        accuracy = self.get_scenario_accuracy(scenario)
        
        if accuracy < 0.4:
            # 该场景识别不准，降低相关策略权重
            logger.warning(f"场景 {scenario} 准确率仅 {accuracy:.2f}，降权处理")
            router.reduce_scenario_weights(scenario, factor=0.8)
        elif accuracy > 0.7:
            # 该场景识别准确，提升权重
            logger.info(f"场景 {scenario} 准确率 {accuracy:.2f}，加权处理")
            router.increase_scenario_weights(scenario, factor=1.1)
```

---

## 八、输出数据结构

```python
@dataclass
class ScenarioOutput:
    """场景识别输出"""
    code: str
    timestamp: str
    primary_scenario: str
    primary_confidence: float
    secondary_scenarios: List[Dict]
    features: Dict
    reason: List[str]
    
    def to_dict(self) -> Dict:
        return {
            "symbol": self.code,
            "timestamp": self.timestamp,
            "primary_scenario": self.primary_scenario,
            "primary_confidence": round(self.primary_confidence, 2),
            "secondary_scenarios": self.secondary_scenarios,
            "features": self.features,
            "reason": self.reason,
        }


@dataclass
class RouteResult:
    """策略路由结果"""
    active_strategies: List[str]
    primary_strategy: str
    secondary_strategies: List[str]
    weights: Dict[str, float]
    risk_template: str
    priority: int
    
    def to_dict(self) -> Dict:
        return {
            "active_strategies": self.active_strategies,
            "primary_strategy": self.primary_strategy,
            "secondary_strategies": self.secondary_strategies,
            "weights": {k: round(v, 2) for k, v in self.weights.items()},
            "risk_template": self.risk_template,
            "priority": self.priority,
        }
```

---

## 九、实施路线图

### Phase 1 (1-2 周): 基础框架搭建

- [ ] 创建 `app/scenario/scenario_classifier.py`
- [ ] 实现 `FeatureExtractor` 6 大维度
- [ ] 实现 `RuleEngine` 5 核心规则
- [ ] 实现 `ScenarioMemory` 平滑防抖
- [ ] 集成至 `data_subscriber.py`

### Phase 2 (1-2 周): 路由层实现

- [ ] 创建 `app/scenario/scenario_router.py`
- [ ] 配置场景 - 策略映射表
- [ ] 集成至 `ConsensusEngine`
- [ ] 添加风控模板切换
- [ ] 实现信号优先级处理

### Phase 3 (1 周): 反馈学习

- [ ] 创建 `app/scenario/performance_feedback.py`
- [ ] 交易记录增加场景标签
- [ ] 实现权重自适应调整
- [ ] 添加场景准确率 Dashboard

### Phase 4 (持续): 模型增强

- [ ] 引入 HMM/GMM 辅助场景概率
- [ ] 多周期共振确认
- [ ] 场景识别可视化
- [ ] A/B 测试验证效果

---

## 十、核心代码骨架

### 10.1 文件结构

```
app/scenario/
├── __init__.py
├── scenario_classifier.py    # 场景分类器
├── scenario_router.py        # 策略路由器
├── performance_feedback.py   # 绩效反馈
├── feature_extractor.py      # 特征提取器
├── rule_engine.py           # 规则引擎
├── scenario_scorer.py       # 场景评分器
├── scenario_arbiter.py      # 场景裁决器
└── scenario_memory.py       # 场景记忆器
```

### 10.2 核心类骨架

```python
"""
FTSeek 场景路由系统

场景识别 → 策略路由 → 共识融合 → 风控执行 → 绩效反馈
"""
from dataclasses import dataclass
from typing import Dict, List, Optional
from enum import Enum

class ScenarioType(str, Enum):
    TREND_START = "trend_start"
    TREND_STRONG = "trend_strong"
    TREND_EXHAUST = "trend_exhaust"
    RANGE_BOUND = "range_bound"
    MEAN_REVERSION = "mean_reversion"
    GAP_EVENT = "gap_event"
    NEWS_DRIVEN = "news_driven"
    VOL_CONTRACTION = "vol_contraction"
    VOL_EXPANSION = "vol_expansion"
    REVERSAL_SETUP = "reversal_setup"
    REVERSAL_CONFIRMED = "reversal_confirmed"
    BREAKOUT_SETUP = "breakout_setup"
    BREAKOUT_CONFIRMED = "breakout_confirmed"
    FALSE_BREAKOUT = "false_breakout"
    CROSS_MARKET = "cross_market"
    OPEN_CLOSE_EFFECT = "open_close_effect"


@dataclass
class ScenarioOutput:
    code: str
    primary_scenario: ScenarioType
    confidence: float
    secondary_scenarios: List[Dict]
    features: Dict
    reason: List[str]


@dataclass
class RouteResult:
    active_strategies: List[str]
    primary_strategy: str
    secondary_strategies: List[str]
    weights: Dict[str, float]
    risk_template: str
    priority: int


class ScenarioRouter:
    """场景路由器 - FTSeek 交易决策核心"""
    
    def __init__(self):
        self.classifier = ScenarioClassifier()
        self.router = RouterEngine()
        self.feedback = PerformanceFeedback()
    
    def process(self, code: str, kline_data: Dict, news_data: Dict = None) -> RouteResult:
        """
        场景路由主流程
        
        1. 场景识别
        2. 策略路由
        3. 记录用于后续学习
        """
        # 1. 场景识别
        scenario = self.classifier.classify(code, kline_data, news_data)
        
        # 2. 策略路由
        route = self.router.route(scenario)
        
        # 3. 记录用于后续学习
        self._log_scenario(scenario, route)
        
        return route
```

---

## 十一、关键参数建议

| 参数 | 建议值 | 作用 |
|:---|---:|:---|
| 场景确认根数 | 3 根 K 线 | 防抖 |
| 场景切换冷却 | 2~5 分钟 | 防频繁切换 |
| 主场景置信度阈值 | 0.65 | 控制准确率 |
| 辅场景置信度阈值 | 0.45 | 保留次优信息 |
| 跳空阈值 | 1.5%~2% | 识别事件 |
| 强趋势 ADX 阈值 | >25 | 识别趋势 |
| 震荡 ADX 阈值 | <20 | 识别震荡 |
| 波动扩张 ATR 倍数 | >2.0σ | 识别波动扩张 |
| 成交量放大倍数 | >1.5 倍 | 识别放量 |

---

## 十二、预期收益

该方案的主要价值不在于"某一策略更神"，而在于：

1. **提高策略在不同市场阶段的覆盖率** - 场景驱动确保合适策略用于合适行情
2. **减少错误策略在错误场景中的交易** - 抑制策略权重降低无效损耗
3. **提升共识信号质量** - 场景适配度纳入共识打分
4. **改善收益曲线平滑度** - 减少震荡市回撤
5. **降低回撤和连续亏损概率** - 场景识别不准时自动降权
6. **增强系统可解释性和可维护性** - 场景标签清晰，便于复盘分析

---

## 十三、API 接口设计

### 13.1 场景识别 API

```
GET /api/v2/scenario/current?code=HK.00700

Response:
{
    "symbol": "HK.00700",
    "timestamp": "2026-04-09T10:30:00",
    "primary_scenario": "TREND_STRONG",
    "primary_confidence": 0.87,
    "secondary_scenarios": [
        {"name": "BREAKOUT_CONFIRMED", "confidence": 0.63},
        {"name": "VOL_EXPANSION", "confidence": 0.58}
    ],
    "features": {
        "adx": 31.2,
        "atr": 1.45,
        "volume_ratio": 1.8,
        "bb_width": 0.042
    },
    "reason": [
        "ADX 高于阈值",
        "均线多头排列",
        "HH/HL 结构成立",
        "成交量持续放大"
    ]
}
```

### 13.2 策略路由 API

```
GET /api/v2/scenario/route?code=HK.00700

Response:
{
    "symbol": "HK.00700",
    "scenario": "TREND_STRONG",
    "active_strategies": ["supertrend", "confluence_v2", "enhanced_confluence"],
    "primary_strategy": "supertrend",
    "weights": {
        "supertrend": 1.5,
        "confluence_v2": 1.2,
        "enhanced_confluence": 1.0,
        "rsi": 0.3,
        "bollinger": 0.3
    },
    "risk_template": "trend_stop",
    "priority": 4
}
```

### 13.3 场景统计 API

```
GET /api/v2/scenario/stats?days=30

Response:
{
    "scenarios": {
        "TREND_STRONG": {
            "trades": 45,
            "win_rate": 0.67,
            "total_pnl": 12500.0,
            "avg_pnl": 277.78
        },
        "RANGE_BOUND": {
            "trades": 32,
            "win_rate": 0.53,
            "total_pnl": 3200.0,
            "avg_pnl": 100.0
        }
        // ...
    }
}
```

---

## 十四、与现有模块集成

### 14.1 与 MarketRegimeDetector 的关系

- `MarketRegimeDetector` 负责宏观市场状态（趋势市/震荡市/高波动市）
- `Scenario Classifier` 负责更细粒度的交易场景（16 子场景）
- 两者可共存，宏观状态用于过滤，细粒度场景用于路由

### 14.2 与 ConsensusEngine 的关系

- 场景识别结果决定哪些策略参与共识
- 场景适配度纳入共识打分公式
- 策略权重由路由结果动态调整

### 14.3 与 RiskManager 的关系

- 不同场景对应不同风控模板
- 场景识别置信度影响仓位上限
- 低置信度场景触发保守风控

### 14.4 与 StrategyManager 的关系

- 场景标签是策略启停的入口
- 策略根据场景标签自我激活/抑制
- 策略表现反馈至场景学习系统

---

## 十五、总结

> **FTSeek 场景识别与策略路由的核心，是先识别市场处于什么状态，再调用最适合该状态的交易系统组合，通过共识融合和动态风控完成交易决策，从而实现更广覆盖、更少冲突、更稳定的收益表现。**

### 核心价值

1. **环境意识** - 系统从"被动执行"升级为"具备环境感知的交易大脑"
2. **动态适配** - 策略权重随市场场景自动调整
3. **反馈学习** - 用实盘表现持续优化场景识别和策略路由
4. **可解释性** - 场景标签清晰，便于复盘和调试

---

**文档版本**: v1.0  
**创建时间**: 2026-04-09  
**作者**: FTSeek Team  
**参考**: Gemini 方案 + GPT 方案整合
