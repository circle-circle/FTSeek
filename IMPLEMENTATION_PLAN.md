# 动态权重分配系统实施计划

## 系统架构

```
三维度动态权重分配系统
├── 新闻监控模块 (news_monitor.py)
│   ├── 多源聚合 (新浪 + 腾讯 + 富途新闻)
│   ├── 情感分析 (Qwen API)
│   └── 置信度计算 (<10 秒延迟)
├── 权重分配引擎 (weight_engine.py)
│   ├── 新闻权重 (0-40%)
│   ├── 港股美股跟随权重 (0-35%)
│   └── 技术指标权重 (25-100%)
├── 港股美股映射 (hk_us_mapping.json)
│   └── ADR 相关性计算 (相关系数≥0.6)
├── 决策日志系统 (decision_log.py)
│   └── 每笔交易权重分配详情 (可追溯)
└── 回测验证脚本 (backtest_weight.py)
    └── 3 个月数据验证 (胜率≥45%, 年化≥20%)
```

## 时间线

| 阶段 | 时间 | 交付物 | 验收标准 |
|------|------|--------|----------|
| Day 1-2 | 4/8-4/9 | news_monitor.py + hk_us_mapping.json | 新闻延迟<10 秒，映射表覆盖 50+ 股票对 |
| Day 3-4 | 4/10-4/11 | weight_engine.py + decision_log.py | 权重总和=100%, 日志可追溯 |
| Day 5 | 4/12 | backtest_weight.py + 单元测试 | 覆盖率≥80% |
| Day 6-7 | 4/13-4/14 | 回测验证 + 修复 | 胜率≥45%, 年化≥20% |

## 核心算法

### 1. 新闻权重计算
```python
def calculate_news_weight(news_items):
    if not news_items:
        return 0.0
    
    # 重大新闻：涉及财报、并购、监管、重大合同
    major_keywords = ['财报', '并购', '监管', '合同', '诉讼', '处罚']
    
    major_count = sum(1 for n in news_items if any(k in n['title'] for k in major_keywords))
    
    if major_count >= 2:
        return 0.40  # 重大新闻 40%
    elif major_count == 1 or len(news_items) >= 3:
        return 0.25  # 一般新闻 25%
    else:
        return 0.0   # 无新闻 0%
```

### 2. 港股美股相关性计算
```python
def calculate_hk_us_correlation(hk_stock, us_stock, days=60):
    """计算港股与对应美股的相关系数"""
    hk_prices = get_historical_prices(hk_stock, days)
    us_prices = get_historical_prices(us_stock, days)
    
    correlation = np.corrcoef(hk_prices, us_prices)[0, 1]
    
    # 只有相关系数≥0.6 才启用
    if correlation >= 0.6:
        return correlation * 0.35  # 最大 35%
    else:
        return 0.0
```

### 3. 技术指标权重
```python
def calculate_technical_weight(signals):
    """技术指标权重 = 100% - 新闻权重 - 港股美股权重"""
    news_weight = calculate_news_weight(news_items)
    hk_us_weight = calculate_hk_us_correlation(hk_stock, us_stock)
    
    technical_weight = 1.0 - news_weight - hk_us_weight
    
    # 确保技术指标权重在 25-100% 范围内
    return max(0.25, min(1.0, technical_weight))
```

### 4. 权重归一化
```python
def normalize_weights(weights):
    """确保权重总和=100%"""
    total = sum(weights.values())
    
    if total == 0:
        # 默认权重分配
        return {'news': 0.0, 'hk_us': 0.0, 'technical': 1.0}
    
    return {k: v / total for k, v in weights.items()}
```

## 硬约束检查

```python
def validate_constraints(weights, trade):
    """验证硬约束"""
    # 1. 权重总和=100%
    assert abs(sum(weights.values()) - 1.0) < 0.001, "权重总和≠100%"
    
    # 2. 单笔亏损≤2%
    if trade['loss_percent'] > 0.02:
        return False, "单笔亏损>2%"
    
    # 3. 多新闻源触发 (≥2 个源)
    if len(trade['news_sources']) < 2:
        return False, "单一新闻源触发"
    
    # 4. 决策链路可追溯
    if 'decision_id' not in trade:
        return False, "决策链路不可追溯"
    
    # 5. 日交易≤5 笔
    daily_trades = get_daily_trade_count()
    if daily_trades > 5:
        return False, "日交易>5 笔"
    
    return True, "通过"
```

## 文件结构

```
/opt/ftseek/FTSeek/
├── modules/
│   ├── __init__.py
│   ├── news_monitor.py        # 新闻监控模块
│   └── decision_log.py        # 决策日志系统
├── strategies/
│   ├── __init__.py
│   └── weight_engine.py       # 权重分配引擎
├── config/
│   └── hk_us_mapping.json     # 港股美股映射表
└── backtest/
    └── backtest_weight.py     # 回测验证脚本
```

## 单元测试计划

```python
# tests/test_weight_engine.py
class TestWeightEngine:
    def test_news_weight_major(self):
        """测试重大新闻权重=40%"""
        
    def test_news_weight_normal(self):
        """测试一般新闻权重=25%"""
        
    def test_news_weight_none(self):
        """测试无新闻权重=0%"""
        
    def test_hk_us_correlation_threshold(self):
        """测试相关系数<0.6 时权重=0%"""
        
    def test_weight_normalization(self):
        """测试权重归一化总和=100%"""
        
    def test_constraint_single_loss(self):
        """测试单笔亏损>2% 约束"""
        
    def test_constraint_news_source(self):
        """测试单一新闻源约束"""
        
    def test_constraint_daily_trades(self):
        """测试日交易>5 笔约束"""
```

## 集成点

### 1. 与现有新闻系统集成
```python
# 复用 app/news_push/collector.py
from app.news_push.collector import NewsCollector

class NewsMonitor:
    def __init__(self):
        self.collector = NewsCollector()
```

### 2. 与策略系统集成
```python
# 在 app/strategy/__init__.py 注册
from strategies.weight_engine import WeightEngine

STRATEGY_REGISTRY['weight_engine'] = WeightEngine
```

### 3. 与决策日志集成
```python
# 复用 app/logging/ 模块
from app.logging.logger import get_logger

logger = get_logger('weight_engine')
```

## 验收标准

### 单元测试
- [ ] 覆盖率≥80%
- [ ] 所有边界条件测试通过
- [ ] 约束验证测试通过

### 回测验证
- [ ] 3 个月历史数据
- [ ] 胜率≥45%
- [ ] 年化收益≥20%
- [ ] 最大回撤<5%

### 实盘验证
- [ ] 5% 仓位起
- [ ] 新闻延迟<10 秒
- [ ] 决策链路可追溯
- [ ] 无硬约束违反

---

*开始编码：Day 1-2*
