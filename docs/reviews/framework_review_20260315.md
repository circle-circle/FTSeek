# FTSeek 策略框架全面 Review 报告

**Review 日期**: 2026-03-15 17:45  
**Review 范围**: 做空优化 + 港股特性适配后的完整框架  
**Review 目标**: 确保流程可正常走通，识别潜在风险

---

## 📋 执行摘要

### ✅ 已验证通过的部分

| 模块 | 状态 | 说明 |
|------|------|------|
| **RSI 策略参数** | ✅ 正确 | overbought=65 已应用 |
| **SuperTrend 参数** | ✅ 正确 | multiplier=2.5 已应用 |
| **配置文件** | ✅ 正确 | strategy_params.json 包含所有新参数 |
| **港股适配模块** | ✅ 创建 | hk_specific.py 功能完整 |
| **测试脚本** | ✅ 通过 | 5/5 测试通过 |

### ⚠️ 发现的问题

| 问题 | 严重性 | 状态 |
|------|--------|------|
| **港股适配模块未集成到主流程** | 🔴 高 | 需修复 |
| **分离参数未实际使用** | 🟡 中 | 需修复 |
| **update_params 未同步新参数** | 🟡 中 | 需修复 |
| **AI 因子未实际调用** | 🟡 中 | 需确认 |

---

## 🔍 详细问题分析

### 问题 1: 港股适配模块未集成到主流程 🔴 高优先级

**现状**:
- `app/strategy/hk_specific.py` 已创建
- 但 **没有被任何主流程模块调用**
- 检查 `data_subscriber.py` 和 `main.py`，未发现集成点

**影响**:
- T+0 交易支持：❌ 不生效
- 流动性分层：❌ 不生效
- 做空信号增强：❌ 不生效
- 无涨跌停适配：❌ 不生效

**修复方案**:

需要在 `data_subscriber.py` 的 `_on_consensus` 方法中集成港股适配器：

```python
# app/data_subscriber.py

# 1. 在 __init__ 中初始化港股适配器
from app.strategy.hk_specific import get_hk_adapter, HKMarketConfig

def __init__(self, strategies: List[BaseStrategy], notifier: DingTalkNotifier):
    # ... 现有代码 ...
    
    # 新增：港股适配器
    hk_config = HKMarketConfig(
        t0_trading=True,
        short_bias_factor=1.2,
        short_rsi_overbought=65,
    )
    self.hk_adapter = get_hk_adapter(hk_config)
```

```python
# 2. 在信号处理流程中应用港股适配
def _on_consensus(self, result: ConsensusResult):
    # ... 现有代码 ...
    
    # 新增：应用港股适配
    consensus_signal = Signal(...)
    
    # 应用港股特性处理
    if consensus_signal.code.startswith("HK."):
        consensus_signal = self.hk_adapter.process_signal(
            consensus_signal, 
            atr_percent=self._get_atr_percent(consensus_signal.code)
        )
        if consensus_signal is None:
            logger.info(f"[港股适配] 信号被过滤，跳过")
            return
```

**建议**: 立即创建集成补丁

---

### 问题 2: 分离参数未实际使用 🟡 中优先级

**现状**:
```python
# RSI 策略
self.overbought = 65  # 统一参数
self.short_overbought = 65  # 分离参数 (未使用)
self.long_oversold = 30  # 分离参数 (未使用)

# 实际信号生成逻辑
if prev_rsi >= self.overbought:  # 使用统一参数
    return SELL
```

**影响**:
- 无法独立调整做多/做空阈值
- 配置文件中 `long_signals` 和 `short_signals` 分离配置无效

**修复方案**:

```python
# RSI 策略 on_kline 方法修改

# 做多信号 (超卖反转)
if prev_rsi is not None and prev_rsi <= self.long_oversold and rsi_val > self.long_oversold:
    # ... BUY 逻辑 ...

# 做空信号 (超买回落) - 使用分离参数
if prev_rsi is not None and prev_rsi >= self.short_overbought and rsi_val < self.short_overbought:
    # ... SELL 逻辑 ...
```

**建议**: 下次迭代时实现

---

### 问题 3: update_params 未同步新参数 🟡 中优先级

**现状**:
```python
# RSI 策略
def update_params(self, params: dict):
    if "overbought" in params:
        self.overbought = float(params["overbought"])
    # ❌ 缺少 short_overbought 和 long_oversold

# SuperTrend 策略
def update_params(self, params: dict):
    if "multiplier" in params:
        self.multiplier = float(params["multiplier"])
    # ❌ 缺少 short_multiplier 和 long_multiplier
```

**影响**:
- 钉钉命令 `/调参 rsi short_overbought=65` 无效
- 动态参数调整功能不完整

**修复方案**:

```python
# RSI 策略
def update_params(self, params: dict):
    if "period" in params:
        self.period = int(params["period"])
    if "overbought" in params:
        self.overbought = float(params["overbought"])
    if "oversold" in params:
        self.oversold = float(params["oversold"])
    # 新增
    if "short_overbought" in params:
        self.short_overbought = float(params["short_overbought"])
    if "long_oversold" in params:
        self.long_oversold = float(params["long_oversold"])
    if "enabled" in params:
        self.enabled = bool(params["enabled"])
    logger.info(f"RSI 参数已更新：period={self.period}, OB={self.overbought}, "
               f"short_OB={self.short_overbought}, OS={self.oversold}")
```

**建议**: 立即修复 (5 分钟工作量)

---

### 问题 4: AI 因子未实际调用 🟡 中优先级

**现状**:
- `config/strategy_params.json` 配置了 `ai_weight: 0.20`
- `app/ai/` 目录存在 AI 模块 (Phase 2 成果)
- 但 **未在主流程中实际调用 AI 分析**

**检查点**:
```bash
# 检查 AI 模块是否被调用
grep -rn "from app.ai" /opt/ftseek/FTSeek/app/*.py
grep -rn "ai_weight" /opt/ftseek/FTSeek/app/*.py
```

**影响**:
- AI 预测因子权重 20% 配置无效
- Fusion 引擎实际只使用技术面 + 情绪面

**建议**: 
1. 确认 Phase 2 AI 集成状态
2. 如未完成，临时移除 AI 权重配置避免混淆
3. 或在 Fusion 引擎中启用 AI 调用

---

## 🔧 配置一致性检查

### ✅ 配置文件与代码一致性

| 参数 | 配置文件 | 代码默认值 | 状态 |
|------|----------|-----------|------|
| `rsi.overbought` | 65 | 65 | ✅ 一致 |
| `rsi.oversold` | 30 | 30 | ✅ 一致 |
| `supertrend.multiplier` | 2.5 | 2.5 | ✅ 一致 |
| `ai_weight` | 0.20 | N/A | ⚠️ 未使用 |
| `short_bias_factor` | 1.2 | N/A | ⚠️ 未使用 |

### ✅ 信号流程完整性

```
行情数据 (Futu API)
    ↓
策略层 (7 大策略)
    ↓
共识引擎 (加权评分)
    ↓
[❌ 缺失] 港股适配层
    ↓
交易执行器
    ↓
钉钉推送
```

**缺失环节**: 港股适配层未集成

---

## 📊 风险评估

### 高风险 🔴

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|----------|
| **港股特性不生效** | 100% | 高 | 立即集成 hk_specific 模块 |
| **T+0 交易被拒绝** | 100% | 中 | 明确告知用户暂不支持 |

### 中风险 🟡

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|----------|
| **分离参数无效** | 100% | 中 | 文档说明当前使用统一参数 |
| **AI 权重配置误导** | 50% | 低 | 临时移除或标注"实验性" |
| **动态调参不完整** | 50% | 低 | 补充 update_params |

### 低风险 🟢

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|----------|
| **配置参数过多** | 30% | 低 | 整理配置文件结构 |
| **日志输出过多** | 20% | 低 | 调整日志级别 |

---

## ✅ 立即修复清单

### 修复 1: 集成港股适配模块 (高优先级)

**文件**: `app/data_subscriber.py`

**修改位置**: `__init__` 方法和 `_on_consensus` 方法

**代码补丁**:
```python
# 在 __init__ 中添加
from app.strategy.hk_specific import get_hk_adapter, HKMarketConfig

self.hk_adapter = get_hk_adapter(HKMarketConfig(
    t0_trading=True,
    short_bias_factor=1.2,
    no_price_limit=True,
))

# 在 _on_consensus 中添加 (共识信号创建后)
# 应用港股适配
if consensus_signal.code.startswith("HK."):
    atr_pct = self._get_atr_percent(consensus_signal.code)
    consensus_signal = self.hk_adapter.process_signal(consensus_signal, atr_pct)
    if consensus_signal is None:
        logger.info(f"[港股适配] {code} 信号被过滤")
        return
```

### 修复 2: 补充 update_params (中优先级)

**文件**: `app/strategy/rsi_strategy.py` 和 `app/strategy/supertrend_strategy.py`

**修改内容**: 添加 `short_overbought`, `long_oversold`, `short_multiplier`, `long_multiplier` 支持

### 修复 3: 清理未使用配置 (低优先级)

**文件**: `config/strategy_params.json`

**修改内容**: 
- 移除或标注 `ai_weight` 为"实验性"
- 移除 `long_signals`/`short_signals` 分离配置 (直到实现)

---

## 🧪 验证测试建议

### 测试 1: 港股适配集成验证
```bash
# 重启服务后检查日志
tail -f /opt/ftseek/FTSeek/logs/ftseek.log | grep "港股"

# 预期输出:
# "港股特性适配引擎初始化完成"
# "[港股适配] HK.00700 信号处理完成"
```

### 测试 2: 做空信号验证
```bash
# 检查 RSI 超买阈值
grep "RSI 超买" /opt/ftseek/FTSeek/logs/ftseek.log

# 预期输出应包含：overbought=65
```

### 测试 3: 动态调参验证
```bash
# 钉钉命令测试
/调参 rsi overbought=65
/策略详情 rsi

# 验证返回参数包含 short_overbought
```

---

## 📈 整体评价

### 架构设计 ⭐⭐⭐⭐☆ (4/5)
- ✅ 模块化设计清晰
- ✅ 策略/融合/共识分层合理
- ⚠️ 港股适配层集成滞后

### 代码质量 ⭐⭐⭐⭐☆ (4/5)
- ✅ 代码规范良好
- ✅ 日志输出详细
- ⚠️ 部分参数未实际使用

### 配置管理 ⭐⭐⭐☆☆ (3/5)
- ✅ 配置文件结构清晰
- ⚠️ 存在未使用配置项
- ⚠️ 配置与代码同步不及时

### 测试覆盖 ⭐⭐⭐⭐☆ (4/5)
- ✅ 单元测试完善
- ✅ 集成测试脚本可用
- ⚠️ 端到端测试不足

---

## 🎯 建议优先级

### 立即执行 (今天)
1. ✅ 集成港股适配模块到主流程
2. ✅ 补充 update_params 方法

### 短期 (本周)
3. 验证 AI 因子集成状态
4. 清理未使用配置项
5. 端到端测试 (信号→交易→推送)

### 中期 (下周)
6. 实现分离参数 (long_*/short_*)
7. 完善动态调参功能
8. 添加监控面板

---

## 📞 后续跟进

**下次 Review**: 2026-03-22 (开市后 1 周)

**关键指标监控**:
- SELL 信号数量 (目标：+50%)
- 港股适配调用次数 (目标：>10 次/天)
- T+0 交易成功率 (目标：>90%)
- AI 因子准确率 (目标：>50%)

---

**Report Generated**: 2026-03-15 17:50  
**Reviewer**: 阿呆 (AI 助手)  
**Status**: 🔴 需要立即修复 (港股适配未集成)
