# 混合权重架构实施报告

## 概述

2026-04-09 完成混合权重架构实施，整合 WeightCalculator 和 WeightEngine 两套系统，实现更动态、多维度的策略权重分配。

## 架构设计

### 核心思想

```
┌─────────────────────────────────────────────────────────────┐
│                    混合权重架构                               │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  WeightCalculator          WeightEngine                     │
│  ├─ 策略胜率调整            ├─ 新闻权重 (0-40%)              │
│  ├─ 市场状态调整            ├─ HK/US 跟随权重 (0-35%)        │
│  ├─ 归因引擎建议            └─ 技术权重 (25-100%)            │
│  └─ 基准权重                                              │
│                                                             │
│              ↓                    ↓                         │
│              └──────┬───────────────┘                      │
│                     │                                      │
│              ConsensusEngine                               │
│              (整合两者输出)                                │
│                     ↓                                      │
│              最终权重 = 基础 × 市场 × 场景 × 新闻 × 混合调整   │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 权重计算公式

```
最终权重 = base × market_adjustment × scenario_weight × news_multiplier × hybrid_adjustment

其中:
- base: 基准权重 (来自 base_weights)
- market_adjustment: 市场状态调整 (趋势/震荡市增益/衰减)
- scenario_weight: 场景路由权重 (Phase 1 场景识别系统)
- news_multiplier: 突发新闻倍增器 (情绪策略×2.0)
- hybrid_adjustment: WeightEngine 因子调整 (新闻因子 + HK/US 因子)
```

### WeightEngine 三维度权重分配

```
1. 新闻权重 (0-40%)
   - 重大新闻 (≥2 条): 40%
   - 一般新闻 (≥1 条或≥3 条): 25%
   - 无新闻：0%

2. HK/US 跟随权重 (0-35%)
   - 权重 = 相关系数 × 35%
   - 同向跟随：全额
   - 反向跟随：50%

3. 技术权重 (25-100%)
   - 技术权重 = 100% - 新闻权重 - HK/US 权重
   - 确保≥25% 最低权重
```

## 实施变更

### 1. ConsensusEngine 修改

文件：`app/strategy/consensus_engine.py`

**新增常量：**
```python
HYBRID_WEIGHT_ENABLED = True  # 启用混合权重架构
WEIGHT_ENGINE_PATH = "/opt/ftseek/FTSeek/strategies/weight_engine.py"
```

**新增方法：**
- `_init_weight_engine()`: 延迟初始化 WeightEngine
- `_get_hybrid_weight()`: 混合权重计算
- `_get_news_factor()`: 获取新闻因子
- `_get_hk_us_factor()`: 获取 HK/US 因子

**修改方法：**
- `_calc_weight()`: 返回类型从 `float` 改为 `Tuple[float, Dict]`
- 调用点更新：886 行、1293 行

### 2. 测试文件

新增：`tests/test_hybrid_weight.py` (13 个测试用例)

测试覆盖：
- WeightEngine 集成状态
- HK/US 映射验证
- 混合权重计算逻辑
- 市场状态增益/衰减
- 权重详情返回
- WeightEngine 直接测试

## 配置说明

### 开关控制

```python
# 在 app/strategy/consensus_engine.py 中
HYBRID_WEIGHT_ENABLED = True  # False=降级到原有逻辑
```

### WeightEngine 配置

```python
# 在 strategies/weight_engine.py 中
news_max_weight: float = 0.40       # 新闻最大权重 40%
news_normal_weight: float = 0.25    # 一般新闻权重 25%
hk_us_max_weight: float = 0.35      # 港股美股最大权重 35%
technical_min_weight: float = 0.25  # 技术最小权重 25%
correlation_threshold: float = 0.6  # 相关性阈值
```

### HK/US 映射

文件：`config/hk_us_mapping.json`

示例：
```json
{
  "mapping": [
    {"hk_stock": "00700.HK", "us_stock": "TCEHY", "correlation": 0.92, "enabled": true},
    {"hk_stock": "09988.HK", "us_stock": "BABA", "correlation": 0.89, "enabled": true},
    {"hk_stock": "03690.HK", "us_stock": "MPNGY", "correlation": 0.81, "enabled": true}
  ]
}
```

## 测试结果

### 测试运行

```bash
python -m pytest tests/test_hybrid_weight.py -v
```

### 测试结果

```
======================== 13 passed, 1 warning ========================
✅ test_hk_us_mapping
✅ test_hybrid_architecture_switch
✅ test_hybrid_weight_calculation_basic
✅ test_hybrid_weight_ranging_market
✅ test_hybrid_weight_trending_market
✅ test_sentiment_strategy_news_multiplier
✅ test_weight_calculation_range
✅ test_weight_details_return
✅ test_weight_engine_config
✅ test_weight_engine_integration
✅ test_hk_us_weight_calculation
✅ test_news_weight_calculation
✅ test_technical_weight_calculation
```

## 使用示例

### 获取策略权重

```python
from app.strategy.consensus_engine import ConsensusEngine

engine = ConsensusEngine()

# 获取某策略在特定市场状态下的权重
weight, details = engine._calc_weight('confluence_v2', 'trending', '00700.HK')

print(f"权重：{weight:.2f}")
print(f"详情：{details}")
```

### 输出示例

```
权重：2.70
详情：{
    'base_weight': 1.5,
    'market_adjustment': 1.0,
    'scenario_weight': 1.0,
    'news_multiplier': 1.0,
    'hybrid_adjustment': 1.8  # 新闻因子 + HK/US 因子共同作用
}
```

### 查询 HK/US 配对

```python
from strategies.weight_engine import WeightEngine

engine = WeightEngine()

# 获取港股对应的美股
pair = engine.get_hk_us_pair('00700.HK')
print(f"00700.HK → {pair['us_stock']} (相关性={pair['correlation']:.2f})")
```

## 兼容性说明

### 降级模式

当 `HYBRID_WEIGHT_ENABLED = False` 或 WeightEngine 初始化失败时，系统自动降级到原有逻辑：

```python
# 原有计算逻辑保持不变
final_weight = base * market_adjustment * scenario_weight * news_multiplier
```

### 返回格式变更

`_calc_weight()` 返回类型从 `float` 改为 `Tuple[float, Dict]`，所有调用点已更新：

```python
# 旧代码
w = self._calc_weight(v.strategy, regime, code)

# 新代码
w, w_details = self._calc_weight(v.strategy, regime, code)
```

## 后续优化建议

### P3 优化项

1. **异步调用优化**: 当前 `_get_news_factor()` 使用 `asyncio.get_event_loop()` 同步调用，可优化为纯异步
2. **缓存策略**: 新闻因子和 HK/US 因子可添加缓存，减少重复计算
3. **权重平滑**: 添加权重变化平滑机制，避免权重剧烈波动
4. **实时相关性**: 实现实时 HK/US 相关性计算，而非使用预计算值

### P4 扩展项

1. **多因子扩展**: 增加资金流向、市场情绪等因子维度
2. **机器学习权重**: 引入 ML 模型预测最优权重分配
3. **回测验证**: 通过历史数据回测验证混合架构效果

## 相关文件

- `app/strategy/consensus_engine.py` - 共识引擎（混合权重集成）
- `strategies/weight_engine.py` - 权重分配引擎
- `config/hk_us_mapping.json` - HK/US 股票映射表
- `tests/test_hybrid_weight.py` - 混合架构测试
- `modules/news_monitor.py` - 新闻监控模块
- `app/consensus/weight_calculator.py` - 策略权重计算器

---

**实施日期**: 2026-04-09  
**版本**: v1.0  
**状态**: ✅ 已完成并测试通过
