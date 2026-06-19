# 方向识别改进方案

## 问题诊断

当前系统方向识别存在 **SHORT 偏向**：
- SHORT: 53% WR, +$24K
- LONG: 39% WR, -$1K

根因：
1. LONG 被 30% strength 惩罚（RFC-058）
2. BEAR 市场完全禁止 LONG
3. 缺乏 LONG 保护机制
4. 方向判断维度单一（仅技术面）

## 改进方案

### Phase 1: 动态惩罚（立即可做）

**当前**：LONG 固定 30% 惩罚
**改为**：根据市场环境动态调整

```python
def get_long_penalty(regime: str) -> float:
    """根据市场环境动态调整 LONG 惩罚"""
    penalties = {
        'STRONG_BULL': 0.0,    # 不惩罚
        'BULL': 0.0,           # 不惩罚
        'CAUTION': 0.15,       # 15% 惩罚
        'BEAR': 0.30,          # 30% 惩罚
    }
    return penalties.get(regime, 0.15)
```

**预期效果**：
- BULL 市场 LONG 信号增加 30%
- 整体 LONG 胜率提升到 45%+

### Phase 2: BEAR 市场 LONG 豁免（1-2 周）

**当前**：BEAR 市场完全禁止 LONG
**改为**：允许高质量 LONG

```python
# regime_filter.py
if regime == BEAR:
    allowed_directions = ["SHORT"]
    # 新增：strength > 80 的 LONG 允许
    min_strength_long_exception = 80
```

**逻辑**：
- 弱势市场中，个股强势反弹是稀缺机会
- 高强度信号（>80）代表多重确认，值得尝试

### Phase 3: LONG 保护机制（2-4 周）

**新增**：弱势市场 LONG 过滤

```python
# signal_stage.py
if regime in ['CAUTION', 'BEAR']:
    # 过滤弱势 LONG
    if direction == "LONG" and strength < 60:
        logger.info(f"{symbol} LONG 弱信号过滤 (regime={regime})")
        continue
```

**对称设计**：
- SHORT: 牛市过滤弱信号（已有）
- LONG: 熊市过滤弱信号（新增）

### Phase 4: 多维度方向判断（1-2 月）

**当前**：仅技术面 3 指标
**扩展**：加入资金流、情绪、基本面

```python
def _enhanced_direction(ctx: SymbolContext) -> str:
    """增强版方向判断"""
    scores = []
    
    # 1. 技术面（现有）
    tech_score = _momentum_direction(ctx)
    scores.append(('technical', tech_score, 0.4))
    
    # 2. 资金流（新增）
    # - 大单净买入/卖出
    # - 主力资金流向
    flow_score = _analyze_order_flow(ctx)
    scores.append(('flow', flow_score, 0.25))
    
    # 3. 市场情绪（新增）
    # - 新闻情绪
    # - 社交媒体热度
    sentiment_score = _analyze_sentiment(ctx)
    scores.append(('sentiment', sentiment_score, 0.2))
    
    # 4. 基本面（新增）
    # - 财报预期
    # - 估值水平
    fundamental_score = _analyze_fundamentals(ctx)
    scores.append(('fundamental', fundamental_score, 0.15))
    
    # 加权投票
    weighted = sum(s * w for _, s, w in scores if s != 0)
    if weighted >= 0.5:
        return "LONG"
    elif weighted <= -0.5:
        return "SHORT"
    return "NEUTRAL"
```

## 验证计划

### Shadow 验证（Phase 1-3）

1. **数据收集**：记录所有 LONG/SHORT 信号（包括被过滤的）
2. **回测分析**：
   - 如果去掉 LONG 惩罚，胜率如何？
   - 如果 BEAR 允许 LONG，收益如何？
3. **A/B 测试**：shadow 模式同时运行新旧逻辑

### 指标监控

```
每日报告：
- LONG 信号数 / SHORT 信号数
- LONG 胜率 / SHORT 胜率
- LONG 平均收益 / SHORT 平均收益
- 方向分布（按市场环境）
```

### 成功标准

- LONG 胜率从 39% 提升到 **45%+**
- LONG/SHORT 信号比例从 0.69 提升到 **0.85+**
- 整体收益提升 **10%+**

## 实施优先级

1. **Phase 1**: 动态惩罚（改 1 行代码，立即可做）
2. **Phase 2**: BEAR LONG 豁免（改配置，1-2 周）
3. **Phase 3**: LONG 保护机制（新增逻辑，2-4 周）
4. **Phase 4**: 多维度判断（大重构，1-2 月）

## 风险评估

**低风险**：
- Phase 1-3 都是渐进式改进
- Shadow 验证确保不回退

**中风险**：
- Phase 4 大重构可能引入新 bug
- 需要充分测试

**缓解措施**：
- 每个 Phase 独立验证
- 保留回滚能力
- 监控关键指标异常

## 结论

当前系统的 SHORT 偏向是 **设计缺陷**，不是市场特性。

通过 4 个 Phase 的改进，可以：
1. 平衡 LONG/SHORT 机会
2. 提升整体收益
3. 增强系统鲁棒性

**建议立即启动 Phase 1**，用 shadow 验证效果。
