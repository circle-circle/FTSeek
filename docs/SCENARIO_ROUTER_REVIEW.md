# FTSeek 场景识别与策略路由设计方案 Review 报告

> **Review 时间**: 2026-04-09  
> **Review 目标**: 基于现有框架能力，识别设计方案的风险、重复建设、可优化点

---

## 一、现有能力盘点

### 1.1 已实现的核心模块

| 模块 | 文件路径 | 核心能力 | 复用状态 |
|------|---------|---------|---------|
| **市场状态识别** | `app/strategy/market_regime.py` | 7 态识别 (强涨/弱涨/震荡/弱跌/强跌/高波/低波) | ✅ 可复用 |
| **共识引擎** | `app/strategy/consensus_engine.py` | 动态加权、ADX 市场状态自适应 | ✅ 可复用 |
| **策略权重计算** | `app/consensus/weight_calculator.py` | 基准权重、归因引擎集成 | ✅ 可复用 |
| **订单路由** | `app/execution/smart_router.py` | 市价/限价单选择、滑点控制 | ✅ 可复用 |
| **熔断器** | `app/risk/circuit_breaker.py` | 连续拒绝熔断、冷却机制 | ✅ 可复用 |
| **交易归因** | `app/analytics/attribution_engine.py` | 策略/市场状态/执行质量归因 | ✅ 可复用 |
| **决策引擎** | `app/v4/core/decision_engine.py` | 统一决策、风控检查 | ✅ 可复用 |
| **分层策略管理** | `app/strategy/layered_integration.py` | 三层策略架构 | ⚠️ 需对接 |

### 1.2 已有的场景识别能力

当前 `MarketRegimeDetector` 已实现：

```python
# 已支持的 7 态识别
class MarketRegimeType(Enum):
    STRONG_TREND_UP = "strong_trend_up"     # 强上涨
    WEAK_TREND_UP = "weak_trend_up"         # 弱上涨
    RANGE_BOUND = "range_bound"             # 震荡
    WEAK_TREND_DOWN = "weak_trend_down"     # 弱下跌
    STRONG_TREND_DOWN = "strong_trend_down" # 强下跌
    HIGH_VOLATILITY = "high_volatility"     # 高波动
    LOW_VOLATILITY = "low_volatility"       # 低波动
```

**已有特征计算**:
- ADX (趋势强度)
- ATR 百分位 (波动率状态)
- 价格 vs MA (趋势方向)
- 布林带宽度 (波动率确认)

**已有策略推荐**:
```python
def get_recommended_strategies(self, regime: MarketRegime) -> Dict[str, float]:
    # 已实现基于市场状态的策略权重推荐
```

---

## 二、设计方案与现有能力对比分析

### 2.1 场景分类体系对比

| 设计方案 | 现有实现 | 差异分析 |
|---------|---------|---------|
| 16 子场景 (TREND_START, TREND_STRONG, etc.) | 7 主场景 | **设计更细粒度**，但现有 7 态已覆盖核心 |
| 事件类 (GAP_EVENT, NEWS_DRIVEN) | ❌ 未实现 | **需新增**，但 `event_driven` 策略已存在 |
| 波动类 (VOL_CONTRACTION, VOL_EXPANSION) | HIGH/LOW_VOLATILITY | **可映射**，现有 ATR 百分位可直接用 |
| 反转/突破类 | ❌ 未实现 | **需新增**，但 TD/突破策略已存在 |

**建议**: 第一阶段先映射现有 7 态，第二阶段扩展事件/反转类场景

### 2.2 特征提取能力对比

| 设计方案需求 | 现有实现 | 复用情况 |
|-------------|---------|---------|
| 趋势特征 (ADX, MA 斜率，排列) | ✅ `market_regime.py` | 完全复用 |
| 波动特征 (ATR, BB 宽度) | ✅ `market_regime.py` | 完全复用 |
| 量能特征 (成交量比率) | ⚠️ 部分实现 | 需增强 |
| 结构特征 (HH/HL, 箱体) | ❌ 未实现 | 需新增 |
| 事件特征 (跳空，新闻) | ⚠️ 部分实现 | `event_driven.py` 有跳空检测 |
| 位置特征 (RSI, VWAP) | ⚠️ 部分实现 | RSI 已有，VWAP 需新增 |

### 2.3 路由能力对比

| 设计方案 | 现有实现 | 差距分析 |
|---------|---------|---------|
| 场景→策略映射表 | ❌ 未实现 | **需新增核心逻辑** |
| 动态权重计算 | ✅ `weight_calculator.py` | 可集成 |
| 信号优先级抢占 | ⚠️ 部分实现 | `DecisionBuffer` 有优先级，需增强 |
| 风控模板切换 | ❌ 未实现 | **需新增** |

---

## 三、风险识别

### 3.1 高风险项 🔴

| 风险 | 描述 | 影响 | 缓解方案 |
|------|------|------|---------|
| **场景识别与 MarketRegime 重复** | 设计方案的 7 态与现有 `MarketRegimeDetector` 功能重叠 | 可能导致两套系统冲突 | 明确分层：现有做宏观，新方案做细粒度 |
| **共识引擎已有 ADX 自适应** | `consensus_engine.py` 已根据 ADX 调整权重 | 新路由可能被架空 | 将场景路由作为共识引擎的上层 |
| **特征计算分散** | 特征分散在 `indicators.py`、`market_regime.py` 等多处 | 维护成本高，数据不一致 | 统一特征提取器 |
| **事件驱动场景无数据支撑** | GAP_EVENT 需要跳空检测，现有能力不足 | 场景识别准确率低 | 先实现基础场景，事件类延后 |

### 3.2 中风险项 🟡

| 风险 | 描述 | 影响 | 缓解方案 |
|------|------|------|---------|
| **场景防抖参数调优** | confirm_window=3, cool_down=2min 需实测 | 过短抖动/过长延迟 | 添加参数配置化，支持回测优化 |
| **策略映射表配置复杂** | 16 场景 × 7 策略 = 112 个配置项 | 配置错误风险 | 提供默认配置 + 可视化编辑工具 |
| **绩效反馈数据不足** | 现有 `signal_outcomes` 表缺少场景标签 | 无法计算场景准确率 | 扩展表结构，添加 `scenario` 字段 |
| **与 V4 决策引擎集成** | `DecisionEngine` 已有独立风控 | 可能与场景风控冲突 | 明确场景风控负责模板选择，V4 负责执行检查 |

### 3.3 低风险项 🟢

| 风险 | 描述 | 影响 | 缓解方案 |
|------|------|------|---------|
| **日志输出过多** | 场景识别每根 K 线输出 | 日志膨胀 | 仅场景切换时输出 INFO |
| **API 接口命名冲突** | `/api/v2/scenario/` 可能与现有冲突 | 路由冲突 | 检查现有 API 路由表 |

---

## 四、整合优化建议

### 4.1 架构整合方案

```
┌─────────────────────────────────────────────────────────────┐
│                    FTSeek 场景路由系统                       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  现有能力复用层:                                             │
│  - MarketRegimeDetector (宏观 7 态)                          │
│  - ConsensusEngine (ADX 自适应权重)                          │
│  - WeightCalculator (基准权重 + 归因集成)                    │
│  - SmartOrderRouter (订单类型选择)                          │
│                                                             │
│  新增能力层:                                                 │
│  - ScenarioClassifier (细粒度 16 场景)                        │
│  - ScenarioRouter (场景→策略映射)                           │
│  - PerformanceFeedback (场景绩效学习)                        │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 4.2 关键整合点

#### **整合点 1: MarketRegimeDetector → ScenarioClassifier**

```python
# 新方案：扩展现有类，而非替换
class ScenarioClassifier(MarketRegimeDetector):
    """场景分类器 - 继承现有 MarketRegimeDetector"""
    
    def __init__(self):
        super().__init__()  # 复用现有初始化
        self.extended_scenarios = self._init_extended_scenarios()
    
    def classify(self, code: str, kline_data: Dict, news_data: Dict = None) -> ScenarioOutput:
        # 1. 复用现有 detect() 获取宏观 7 态
        macro_regime = super().detect(code)
        
        # 2. 基于宏观状态，进一步细分为 16 子场景
        return self._refine_scenario(macro_regime, kline_data, news_data)
```

#### **整合点 2: ConsensusEngine 集成场景权重**

```python
# 修改现有 consensus_engine.py 的 calculate_weight 方法
def calculate_weight(self, strategy: str, market_regime: str = None, 
                     scenario: str = None) -> float:
    """增加 scenario 参数"""
    base = self.base_weights.get(strategy, 1.0)
    
    # 原有 ADX 自适应逻辑保持不变
    # ...
    
    # 新增：场景适配度调整
    if scenario:
        scenario_fit = self._get_scenario_fit(strategy, scenario)
        base *= scenario_fit
    
    return base
```

#### **整合点 3: AttributionEngine 扩展场景统计**

```python
# 扩展现有 signal_outcomes 表结构
# 添加 scenario 字段
ALTER TABLE signal_outcomes ADD COLUMN scenario TEXT;

# 在 AttributionEngine 中添加场景归因方法
def analyze_scenario_performance(self, days: int = 30) -> Dict[str, ScenarioAttribution]:
    """场景表现归因"""
    # 查询带有场景标签的交易记录
    # 计算各场景的胜率、盈亏比等
```

### 4.3 分阶段实施路线

#### **Phase 1 (1 周): 最小可行方案**

目标：复用现有能力，实现基础场景路由

- [ ] 扩展 `MarketRegimeDetector` 增加 3 个子场景识别
  - `TREND_START` (ADX 上升初期)
  - `TREND_EXHAUST` (ADX 极高但价格动量衰减)
  - `VOL_EXPANSION` (ATR 百分位快速上升)
- [ ] 创建 `ScenarioRouter` 基础类
- [ ] 配置 5 个核心场景的策略映射
- [ ] 集成至 `ConsensusEngine`
- [ ] 添加场景标签到 `signal_outcomes` 表

#### **Phase 2 (1 周): 增强场景识别**

- [ ] 实现特征提取器 (统一特征计算)
- [ ] 增加事件类场景 (GAP_EVENT, NEWS_DRIVEN)
- [ ] 实现场景记忆器 (防抖)
- [ ] 添加场景 API 接口

#### **Phase 3 (1 周): 绩效反馈**

- [ ] 创建 `PerformanceFeedback` 类
- [ ] 实现场景准确率计算
- [ ] 实现权重自适应调整
- [ ] 添加场景统计 Dashboard

---

## 五、配置优化建议

### 5.1 场景参数配置化

```yaml
# config/scenario_config.yaml
scenario_classifier:
  confirm_window: 3        # 场景确认 K 线数
  cool_down_minutes: 2     # 切换冷却时间
  confidence_threshold: 0.65  # 置信度阈值

scenarios:
  TREND_START:
    priority: 3
    min_duration_minutes: 3
    strategies:
      primary: "turtle"
      secondary: ["123_rule", "td_range"]
      suppressed: ["rsi", "bollinger"]
  
  TREND_STRONG:
    priority: 4
    min_duration_minutes: 3
    strategies:
      primary: "turtle"
      secondary: ["123_rule"]
      suppressed: ["rsi"]
  
  # ... 其他场景
```

### 5.2 策略映射表简化

```python
# 使用继承和默认值减少配置量
DEFAULT_ROUTING_CONFIG = {
    "primary_weight": 1.5,
    "secondary_weight": 1.0,
    "suppressed_weight": 0.3,
}

SCENARIO_ROUTING = {
    "TREND_STRONG": {
        "primary": ["supertrend", "confluence_v2"],
        "secondary": ["enhanced_confluence"],
        # 继承默认权重
    },
    "RANGE_BOUND": {
        "primary": ["rsi", "bollinger"],
        "secondary": ["confluence_v2"],
    },
}
```

---

## 六、代码复用清单

### 6.1 可直接复用的模块

| 模块 | 复用方式 | 修改量 |
|------|---------|-------|
| `MarketRegimeDetector` | 继承扩展 | 20% |
| `ConsensusEngine` | 增加 scenario 参数 | 10% |
| `WeightCalculator` | 增加场景权重同步 | 15% |
| `SmartOrderRouter` | 直接使用 | 0% |
| `CircuitBreaker` | 增加场景感知 | 10% |
| `AttributionEngine` | 增加场景归因 | 25% |

### 6.2 需新增的模块

| 模块 | 文件路径 | 优先级 |
|------|---------|-------|
| `ScenarioClassifier` | `app/scenario/classifier.py` | P0 |
| `ScenarioRouter` | `app/scenario/router.py` | P0 |
| `FeatureExtractor` | `app/scenario/features.py` | P0 |
| `PerformanceFeedback` | `app/scenario/feedback.py` | P1 |
| `ScenarioMemory` | `app/scenario/memory.py` | P1 |

---

## 七、关键风险缓解

### 7.1 场景识别准确率风险

**风险**: 新场景识别准确率低于预期，导致策略路由错误

**缓解方案**:
1. **双轨运行**: 初期同时输出两套场景识别结果，对比验证
2. **置信度阈值**: 低置信度 (<0.65) 时使用默认策略组合
3. **AB 测试**: 部分标的启用场景路由，对比效果

### 7.2 系统性能风险

**风险**: 场景识别增加计算负担，影响交易延迟

**缓解方案**:
1. **异步计算**: 场景识别在后台线程运行
2. **缓存机制**: 场景结果缓存 1 分钟，避免重复计算
3. **按需计算**: 仅在 K 线更新时计算，非 Tick 级

### 7.3 配置错误风险

**风险**: 策略映射表配置错误导致策略失效

**缓解方案**:
1. **配置校验**: 启动时验证配置完整性
2. **默认兜底**: 配置错误时使用默认映射
3. **可视化编辑**: 提供配置编辑工具

---

## 八、总体评估

### 8.1 方案可行性

| 维度 | 评分 | 说明 |
|------|------|------|
| 技术可行性 | ⭐⭐⭐⭐ | 现有能力可支撑 70%，需新增 30% |
| 实施复杂度 | ⭐⭐⭐ | 中等，主要复杂度在配置和调优 |
| 预期收益 | ⭐⭐⭐⭐⭐ | 显著提升策略适配度 |
| 风险可控性 | ⭐⭐⭐⭐ | 主要风险可缓解 |

### 8.2 核心建议

1. **优先复用**：充分利用现有 `MarketRegimeDetector` 和 `ConsensusEngine`
2. **分阶段落地**：Phase 1 先实现基础能力，验证后再扩展
3. **配置驱动**：场景映射表配置化，避免硬编码
4. **数据驱动**：尽快积累场景绩效数据，支持反馈学习

### 8.3 否决项检查

以下情况应暂停实施：

- [ ] 回测显示场景识别准确率 < 50%
- [ ] 实盘测试连续亏损 > 5 次
- [ ] 系统延迟增加 > 100ms
- [ ] 配置复杂度导致维护成本过高

---

## 九、下一步行动

### 9.1 立即行动 (本周)

1. **扩展 `MarketRegimeDetector`**：增加 3 个子场景识别
2. **创建 `ScenarioRouter` 原型**：实现基础路由逻辑
3. **修改 `signal_outcomes` 表**：添加场景字段
4. **配置 5 个核心场景映射**：TREND_STRONG, RANGE_BOUND, GAP_EVENT, VOL_EXPANSION, REVERSAL_SETUP

### 9.2 短期计划 (2 周内)

1. **集成至 `ConsensusEngine`**：场景权重纳入共识打分
2. **添加场景 API**：提供场景查询接口
3. **实现场景日志**：追踪场景切换历史
4. **开始积累绩效数据**：为反馈学习做准备

### 9.3 中期计划 (1 个月内)

1. **实现绩效反馈**：场景准确率计算和权重调整
2. **扩展至 16 场景**：完整场景体系
3. **Dashboard 可视化**：场景热力图、准确率统计
4. **回测验证**：对比场景路由前后的收益曲线

---

**Review 结论**: 设计方案整体可行，但需充分复用现有能力，避免重复建设。建议按 Phase 分阶段实施，优先验证核心价值后再扩展。

---

**Review 人**: FTSeek Team  
**Review 时间**: 2026-04-09  
**版本**: v1.0
