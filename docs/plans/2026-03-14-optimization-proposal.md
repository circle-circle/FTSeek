# FTSeek 策略优化与迭代改进方案

**创建时间**: 2026-03-14 20:20  
**目标**: 提升交易记录迭代性，逐步提高收益率  
**适用**: 港股开市后实盘验证

---

## 📊 当前架构评估

### ✅ 已有优势

| 模块 | 状态 | 评分 |
|------|------|------|
| **因子库** | 7 个因子 (资金流/情绪/ESG 等) | ⭐⭐⭐⭐ |
| **AI 集成** | Bailian API + 情绪/预测因子 | ⭐⭐⭐⭐⭐ |
| **融合策略** | FusionV3 (AI+ 传统) | ⭐⭐⭐⭐ |
| **风控系统** | 五层风控 + 成本监控 | ⭐⭐⭐⭐ |
| **A/B 测试** | 90%/10% 流量分配 | ⭐⭐⭐⭐ |
| **监控报告** | 信号质量 + 成本追踪 | ⭐⭐⭐⭐ |

### ⚠️ 改进空间

| 维度 | 问题 | 优先级 |
|------|------|--------|
| **交易记录** | 缺少结构化复盘 | 🔴 高 |
| **策略迭代** | 无自动参数优化 | 🔴 高 |
| **收益归因** | 缺少策略贡献分析 | 🔴 高 |
| **风险控制** | 缺少动态仓位管理 | 🟡 中 |
| **数据闭环** | 预测 vs 实际对比不足 | 🟡 中 |

---

## 🎯 优化方案

### Phase 4: 迭代性交易记录系统 (1 周)

#### Task 4.1: 交易记录增强

**目标**: 每次交易记录完整上下文，支持后续分析

**数据结构**:
```python
@dataclass
class TradeRecord:
    # 基础信息
    trade_id: str
    code: str
    timestamp: str
    strategy: str  # fusion_v2/fusion_v3
    
    # 信号信息
    signal_confidence: float
    ai_score: float
    factor_scores: Dict  # 各因子分数
    
    # 执行信息
    action: str  # BUY/SELL
    entry_price: float
    quantity: int
    position_size: float  # 仓位占比
    
    # 风控信息
    stop_loss: float
    take_profit: float
    risk_reward_ratio: float
    
    # 结果信息 (平仓后填充)
    exit_price: Optional[float]
    exit_timestamp: Optional[str]
    exit_reason: Optional[str]  # 止盈/止损/信号反转
    pnl: Optional[float]
    pnl_percent: Optional[float]
    holding_period: Optional[int]  # 持有天数
    
    # 市场环境
    market_regime: str  # 上涨/下跌/震荡
    volatility: float
    
    # 复盘信息
    lessons_learned: str
    is_good_trade: bool  # 是否符合策略
```

**实现文件**:
- `app/storage/trade_recorder.py` - 交易记录器
- `app/storage/trade_analysis.py` - 交易分析器

---

#### Task 4.2: 收益归因分析

**目标**: 分析每次收益的来源 (AI/传统因子/运气)

**分析维度**:
```python
class PerformanceAttribution:
    """收益归因分析"""
    
    def analyze_by_strategy(self) -> Dict:
        """按策略分析收益贡献"""
        return {
            'fusion_v2': {'trades': 50, 'pnl': 10000, 'win_rate': 0.55},
            'fusion_v3': {'trades': 6, 'pnl': 1500, 'win_rate': 0.67},
            'ai_contribution': '+12%'  # AI 带来的额外收益
        }
    
    def analyze_by_factor(self) -> Dict:
        """按因子分析预测准确性"""
        return {
            'ai_prediction': {'accuracy': 0.65, 'avg_pnl': 250},
            'capital_flow': {'accuracy': 0.58, 'avg_pnl': 180},
            'sentiment': {'accuracy': 0.60, 'avg_pnl': 200}
        }
    
    def analyze_by_market_regime(self) -> Dict:
        """按市场环境分析"""
        return {
            'uptrend': {'trades': 20, 'win_rate': 0.70},
            'downtrend': {'trades': 10, 'win_rate': 0.40},
            'sideways': {'trades': 26, 'win_rate': 0.50}
        }
```

**实现文件**:
- `app/analytics/performance_attribution.py`

---

#### Task 4.3: 策略参数自动优化

**目标**: 基于历史交易数据，自动优化策略参数

**优化目标**:
```python
optimization_config = {
    'objective': 'sharpe_ratio',  # 最大化夏普比率
    'constraints': {
        'max_drawdown': 0.15,  # 最大回撤 < 15%
        'min_win_rate': 0.50,  # 最低胜率 50%
        'min_trades': 30  # 最少交易数
    },
    'parameters': {
        'ai_weight': {'range': [0.2, 0.5], 'step': 0.05},
        'stop_loss': {'range': [0.03, 0.08], 'step': 0.01},
        'take_profit': {'range': [0.05, 0.15], 'step': 0.01},
        'position_size': {'range': [0.1, 0.3], 'step': 0.05}
    }
}
```

**优化方法**:
1. **网格搜索**: 遍历参数组合
2. **贝叶斯优化**: 智能搜索最优参数
3. **交叉验证**: 避免过拟合

**实现文件**:
- `app/optimization/strategy_optimizer.py`

---

### Phase 5: 动态风险控制 (1 周)

#### Task 5.1: 动态仓位管理

**目标**: 根据市场环境和策略表现动态调整仓位

**仓位公式**:
```python
def calculate_position_size(signal_confidence, market_regime, recent_performance):
    """
    动态仓位计算
    
    Args:
        signal_confidence: 信号置信度 (0-1)
        market_regime: 市场环境 (上涨/下跌/震荡)
        recent_performance: 近期策略表现 (近 10 笔交易胜率)
    """
    base_position = 0.10  # 基础仓位 10%
    
    # 信号置信度调整
    confidence_multiplier = 0.5 + signal_confidence  # 0.5-1.5x
    
    # 市场环境调整
    regime_map = {
        'uptrend': 1.2,
        'sideways': 1.0,
        'downtrend': 0.5
    }
    regime_multiplier = regime_map.get(market_regime, 1.0)
    
    # 近期表现调整
    if recent_performance > 0.60:
        performance_multiplier = 1.2
    elif recent_performance < 0.40:
        performance_multiplier = 0.5
    else:
        performance_multiplier = 1.0
    
    # 最终仓位
    position = base_position * confidence_multiplier * regime_multiplier * performance_multiplier
    
    # 限制在 5%-30% 范围
    return max(0.05, min(0.30, position))
```

**实现文件**:
- `app/risk/dynamic_position_sizer.py`

---

#### Task 5.2: 实时风险预警

**目标**: 实时监控风险指标，提前预警

**预警指标**:
```python
risk_alerts = {
    'consecutive_loss': {
        'warning': 3,  # 连续亏损 3 笔
        'critical': 5,  # 连续亏损 5 笔
        'action': 'reduce_position'  # 降低仓位
    },
    'daily_loss': {
        'warning': 0.03,  # 日亏损 3%
        'critical': 0.05,  # 日亏损 5%
        'action': 'stop_trading'  # 停止交易
    },
    'drawdown': {
        'warning': 0.10,  # 回撤 10%
        'critical': 0.15,  # 回撤 15%
        'action': 'liquidate'  # 清仓
    },
    'ai_accuracy_drop': {
        'warning': 0.45,  # AI 准确率 < 45%
        'critical': 0.35,  # AI 准确率 < 35%
        'action': 'disable_ai'  # 禁用 AI
    }
}
```

**实现文件**:
- `app/risk/realtime_alert.py`

---

### Phase 6: 数据驱动迭代 (持续)

#### Task 6.1: 预测 vs 实际对比

**目标**: 追踪 AI 预测准确性，持续改进

**记录内容**:
```python
prediction_tracking = {
    'prediction': {
        'direction': '上涨',
        'confidence': 85,
        'timestamp': '2026-03-15 09:30'
    },
    'actual': {
        'direction': '上涨',
        'max_gain': 0.05,
        'max_loss': -0.02,
        'final_return': 0.03,
        'holding_period': 3
    },
    'accuracy': {
        'direction_correct': True,
        'confidence_calibrated': True,  # 高置信度是否对应高准确率
        'reason_quality': 0.8
    }
}
```

**可视化**:
- AI 预测准确率趋势图
- 置信度校准曲线
- 预测理由质量分析

**实现文件**:
- `app/analytics/prediction_tracker.py`

---

#### Task 6.2: 每周策略复盘

**目标**: 每周自动生成绩效复盘报告

**报告内容**:
```markdown
## 周策略复盘 (2026-03-15 ~ 2026-03-21)

### 交易汇总
- 总交易数：56 笔
- 胜率：58.9%
- 总收益：+HK$ 12,500
- 夏普比率：1.85

### 策略表现
| 策略 | 交易数 | 胜率 | 收益 | 夏普 |
|------|--------|------|------|------|
| FusionV2 | 50 | 56% | +10,000 | 1.75 |
| FusionV3 | 6 | 83% | +2,500 | 2.50 |

### AI 贡献度
- AI 信号胜率：67% (vs 传统 56%)
- AI 带来额外收益：+15%
- AI 准确率趋势：↑ 上升

### 改进建议
1. AI 权重可从 30% 提升至 40%
2. 下跌市场表现不佳，建议降低仓位
3. 连续亏损 3 笔后应暂停交易
```

**实现文件**:
- `app/reports/weekly_review.py`

---

#### Task 6.3: 策略版本管理

**目标**: 追踪策略演进，支持回滚

**版本记录**:
```python
strategy_versions = {
    'v1.0': {
        'date': '2026-03-01',
        'changes': ['初始版本', 'FusionV2'],
        'performance': {'sharpe': 1.5, 'win_rate': 0.52}
    },
    'v2.0': {
        'date': '2026-03-14',
        'changes': ['AI 集成', 'FusionV3', '缓存优化'],
        'performance': {'sharpe': 1.8, 'win_rate': 0.60}
    },
    'v2.1': {
        'date': '2026-03-21',
        'changes': ['动态仓位', '风险预警'],
        'performance': {'sharpe': 2.0, 'win_rate': 0.62}
    }
}
```

**实现文件**:
- `app/core/strategy_versioning.py`

---

## 📈 预期收益提升路径

### 阶段目标

| 阶段 | 时间 | 目标胜率 | 目标夏普 | 关键改进 |
|------|------|----------|----------|----------|
| **当前** | 3/14 | 55-60% | 1.5-1.8 | 基础架构 |
| **Phase 4** | 3/21 | 58-63% | 1.8-2.0 | 交易记录 + 归因 |
| **Phase 5** | 3/28 | 60-65% | 2.0-2.2 | 动态风控 |
| **Phase 6** | 4/04 | 62-68% | 2.2-2.5 | 数据驱动迭代 |

### 收益提升来源

| 来源 | 预期贡献 | 实现方式 |
|------|----------|----------|
| **AI 优化** | +5-8% | 提高 AI 权重 + 改进提示词 |
| **参数优化** | +3-5% | 自动参数搜索 |
| **动态仓位** | +2-3% | 根据表现调整仓位 |
| **风险降低** | +2-3% | 减少大亏损 |
| **总计** | **+12-19%** | - |

---

## 🚀 实施优先级

### 高优先级 (3/15-3/21)
1. **Task 4.1**: 交易记录增强 ✅ 基础
2. **Task 4.2**: 收益归因分析 ✅ 了解收益来源
3. **Task 6.1**: 预测 vs 实际对比 ✅ 改进 AI

### 中优先级 (3/22-3/28)
4. **Task 5.1**: 动态仓位管理
5. **Task 5.2**: 实时风险预警
6. **Task 6.2**: 每周策略复盘

### 低优先级 (3/29-4/04)
7. **Task 4.3**: 策略参数自动优化
8. **Task 6.3**: 策略版本管理

---

## 📋 开市前准备清单

### 3/15 (周一) 开市前
- [ ] 确认 API Key 有效
- [ ] 测试 AI API 连接
- [ ] 验证 FusionV3 信号生成
- [ ] 检查钉钉推送正常
- [ ] 确认风控参数配置

### 3/15-3/21 第一周
- [ ] 启用交易记录增强
- [ ] 监控 AI 预测准确率
- [ ] 每日检查成本使用
- [ ] 周末生成首份周报

### 3/22-3/28 第二周
- [ ] 根据第一周数据优化参数
- [ ] 启用动态仓位管理
- [ ] 配置风险预警阈值

---

## 💡 关键建议

### 1. 数据质量优先
- 每次交易记录完整上下文
- 预测和实际结果必须对比
- 定期清理低质量数据

### 2. 小步快跑迭代
- 每周一个小改进
- A/B 测试验证效果
- 无效改进及时回滚

### 3. 风险控制第一
- 单笔亏损不超过 2%
- 日亏损不超过 5%
- 连续亏损 5 笔暂停交易

### 4. AI 辅助而非主导
- AI 权重建议 30-40%
- 高置信度才使用 AI
- 定期评估 AI 贡献度

### 5. 保持策略简洁
- 避免过度拟合
- 因子数量控制在 10 个以内
- 参数不宜过多

---

## 📄 实施文档

| 文档 | 路径 |
|------|------|
| 优化方案 | `docs/plans/2026-03-14-optimization-proposal.md` |
| 交易记录设计 | `docs/design/trade-recorder-design.md` |
| 收益归因指南 | `docs/guides/performance-attribution.md` |
| 参数优化手册 | `docs/guides/strategy-optimization.md` |

---

## ✅ 下一步行动

**3/15 开市后立即执行**:
1. 启用增强交易记录
2. 开始追踪 AI 预测准确率
3. 每日生成交易报告
4. 周末生成首份周报

**预计完成时间**: 2026-04-04 (3 周)

---

*方案版本：V1.0*  
*创建时间：2026-03-14 20:20*  
*状态：待执行*
