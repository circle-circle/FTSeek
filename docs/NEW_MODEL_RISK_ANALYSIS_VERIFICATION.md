# 新模型风险分析报告 - 验证与评估

**日期**: 2026-04-09  
**验证执行**: Claude Code  
**验证范围**: 新模型提出的 3 个关键风险 + 优化机会

---

## 执行摘要

### 验证结论

| 风险编号 | 风险描述 | 验证状态 | 严重性评估 |
|----------|----------|----------|------------|
| 风险 1 | P0 动态风控未集成 | ⚠️ **部分存在** | 🟡 中等 |
| 风险 2 | 两套决策系统冲突 | ❌ **不成立** | 🟢 低风险 |
| 风险 3 | RSI/MACD 未用于决策 | ⚠️ **部分存在** | 🟡 中等 |

### 核心发现

1. **风险 1 (部分成立)**: 当前 `DecisionEngine` 确实未集成 `PreTradeRisk`，但系统中不存在所谓的"RiskAgent"类
2. **风险 2 (不成立)**: 代码中不存在 `run_voting()` 和 `run_full_decision_pipeline()` 两套系统冲突问题
3. **风险 3 (部分成立)**: RSI/MACD 已用于退出决策，但在入场信号生成阶段的整合可优化

---

## 详细验证

### 风险 1: P0 优化未完全集成到主决策链路

#### 新模型声称
> - P0 优化的动态风控 (RiskAgent) 和盈亏比过滤 未在_process_symbol_impl 中调用
> - 实际使用的是旧的 RiskEngine,静态阈值 (strength>=50, confidence>=0.50)
> - 动态风控 (牛/熊/震荡市差异化) 和盈亏比过滤 (Reward/Risk < 2.5:1) 完全未生效

#### 代码验证结果

**检查项 1**: 是否存在 `RiskAgent` 类？
```bash
grep -r "class RiskAgent" /opt/ftseek/FTSeek/app
# 结果：No files found
```
**结论**: ❌ 系统中不存在 `RiskAgent` 类

**检查项 2**: 是否存在 `pre_trade_check` 方法？
```bash
grep -r "pre_trade_check" /opt/ftseek/FTSeek/app
# 结果：仅在 pre_trade.py 中有 PreTradeRisk 类，但未被调用
```

**检查项 3**: 当前 DecisionEngine 使用的风控逻辑
```python
# app/v4/core/decision_engine.py L101-147
def process_signal(self, signal: Dict[str, Any]) -> DecisionResult:
    # 1. 全局开关
    if not self.rate_limiter.is_global_enabled():
        return DecisionResult(False, "GLOBAL_DISABLED", ...)
    
    # 2. 限流检查
    if not self.rate_limiter.should_allow(code, action):
        return DecisionResult(False, "RATE_LIMITED", ...)
    
    # 3. 时段检查
    if not self.session_manager.is_trading_session(code, timestamp):
        return DecisionResult(False, "NOT_IN_SESSION", ...)
    
    # 4. 获取模式
    mode = self.get_mode(code)
    
    # 5. 检查禁用
    if mode == TradingMode.DISABLED:
        return DecisionResult(False, "MODE_DISABLED", ...)
    
    # 6. 执行交易 (直接执行，无 PreTradeRisk 检查)
    if mode in [TradingMode.PAPER, TradingMode.LIVE]:
        result = self._execute_paper_trade(signal)
```

**检查项 4**: PreTradeRisk 类存在但未被使用
```python
# app/v4/risk/pre_trade.py
class PreTradeRisk:
    """交易前风控系统"""
    # 支持：资金检查、日交易次数限制、仓位限制、黑名单检查
    # 但该类在 DecisionEngine 中未被调用
```

#### 验证结论

✅ **部分成立** - 但描述有出入：

1. **正确点**: `DecisionEngine.process_signal()` 确实未调用 `PreTradeRisk` 进行资金/仓位检查
2. **错误点**: 
   - 不存在 `RiskAgent` 类
   - 不存在 `_process_symbol_impl` 方法
   - 不存在 "旧版 RiskEngine" 与 "新版 RiskAgent" 的对比
3. **实际情况**: 
   - 当前使用 `SignalRateLimiter` 限流
   - 当前使用 `MarketSessionManager` 时段检查
   - 缺少资金充足性检查和仓位集中度检查

#### 建议修复

```python
# 在 DecisionEngine.process_signal() 中添加 PreTradeRisk 检查

from app.v4.risk.pre_trade import PreTradeRisk

class DecisionEngine:
    def __init__(self, ...):
        # ... 现有初始化
        self.pre_trade_risk = PreTradeRisk(config={
            'max_daily_trades': 10,
            'max_single_position': 0.20,
            'max_total_position': 0.60,
        })
    
    def process_signal(self, signal: Dict[str, Any]) -> DecisionResult:
        # ... 现有检查
        
        # 新增：交易前风控检查
        account_info = self._get_account_info()  # 获取账户信息
        required_capital = signal['price'] * signal['quantity']
        
        risk_report = self.pre_trade_risk.full_check(
            signal=signal,
            account_info=account_info
        )
        
        if not risk_report.all_passed:
            failed_checks = [c for c in risk_report.checks if not c.passed]
            return DecisionResult(
                False, 
                "RISK_CHECK_FAILED", 
                self.get_mode(code), 
                False, 
                message=f"风控检查失败：{failed_checks[0].message}"
            )
        
        # ... 继续执行交易
```

---

### 风险 2: 两套决策系统并存冲突

#### 新模型声称
> - 系统 1: scheduler_tasks.py 调用 run_voting() (仅投票)
> - 系统 2: voting_coordinator.py 的 run_full_decision_pipeline() (完整管线)
> - 实际从未调用完整管线，辩论/交易员决策/风控否决从未使用
> - ai_executor 期望接收 FinalDecision,但实际收到的是普通 Dict

#### 代码验证结果

**检查项 1**: 是否存在 `scheduler_tasks.py`?
```bash
find /opt/ftseek/FTSeek -name "scheduler_tasks.py"
# 结果：File does not exist
```
**结论**: ❌ 文件不存在

**检查项 2**: 是否存在 `voting_coordinator.py`?
```bash
find /opt/ftseek/FTSeek -name "voting_coordinator.py"
# 结果：No files found
```
**结论**: ❌ 文件不存在

**检查项 3**: 是否存在 `run_voting` 或 `run_full_decision_pipeline` 方法？
```bash
grep -r "run_voting\|run_full_decision_pipeline" /opt/ftseek/FTSeek/app
# 结果：No matches found
```
**结论**: ❌ 方法不存在

**检查项 4**: 是否存在 `AiExecutor` 类？
```bash
grep -r "class.*AiExecutor" /opt/ftseek/FTSeek/app
# 结果：No files found
```
**结论**: ❌ 类不存在

**检查项 5**: 当前实际使用的决策流程
```python
# app/v4/core/decision_engine.py
class DecisionEngine:
    """统一决策引擎 V3 - 自动模拟交易"""
    
    def process_signal(self, signal: Dict[str, Any]) -> DecisionResult:
        """处理交易信号 (自动执行，无需确认)"""
        # 1. 限流检查
        # 2. 时段检查
        # 3. 模式检查
        # 4. 执行交易
        # 5. 自动退出检查
```

#### 验证结论

❌ **不成立** - 新模型声称的代码在当前系统中不存在：

1. `scheduler_tasks.py` - 不存在
2. `voting_coordinator.py` - 不存在
3. `run_voting()` / `run_full_decision_pipeline()` - 不存在
4. `AiExecutor` - 不存在

**实际情况**: 
- 当前系统使用 `DecisionEngine` 作为统一决策入口
- 决策流程：信号 → 限流 → 时段检查 → 执行 → 退出检查
- 不存在"两套系统冲突"问题

**可能的原因**: 新模型可能基于过时的代码版本或幻觉生成了不存在的代码

---

### 风险 3: RSI/MACD 技术指标未用于决策

#### 新模型声称
> - P0 优化已添加 RSI/MACD 计算，数据可用
> - 但投票提示词明确排除 RSI/MACD ("已移除 GLM5")
> - 计算了但不使用，数据浪费

#### 代码验证结果

**检查项 1**: RSI/MACD 是否被计算？
```bash
grep -r "rsi\|macd" /opt/ftseek/FTSeek/app/v4 --include="*.py" | head -20
```

**结果**:
```python
# app/v4/strategy/confidence_scorer.py L8
# 技术面 (40%): 形态完成度、突破有效性、均线排列、RSI/MACD 指标

# app/v4/risk/overnight_hold.py L93, 145-150
# market_data: 市场数据 {high, volume_ratio, rsi, macd_golden}
rsi = market_data.get("rsi", 50)
if rsi > 60:
    reasons.append(f"RSI={rsi:.0f} 强势区")
if market_data.get("macd_golden"):
    reasons.append("MACD 金叉")

# app/v4/core/decision_engine.py L408
indicators = {'rsi': 50, 'macd_histogram': 0}  # 默认中性
# TODO: 从信号引擎获取实际指标
```

**检查项 2**: RSI/MACD 是否用于决策？

**发现**:
1. ✅ `confidence_scorer.py` - RSI/MACD 作为技术面评分维度之一 (40% 权重)
2. ✅ `overnight_hold.py` - RSI/MACD 用于持仓过夜决策
3. ⚠️ `decision_engine.py` - 有 TODO 注释，计划获取实际指标但目前使用默认值

#### 验证结论

⚠️ **部分成立** - 有部分真实问题：

1. **正确点**: 
   - `decision_engine.py` L408-409 确实有 TODO，当前使用默认中性值
   - RSI/MACD 在退出决策中使用，但在入场决策中整合不完整

2. **错误点**:
   - RSI/MACD 并非"完全未使用"
   - 在 `confidence_scorer` 和 `overnight_hold` 中已有应用

3. **实际问题**:
   - 入场决策时 RSI/MACD 数据未从信号层传递到决策层
   - 存在数据断点

#### 建议修复

```python
# 1. 在信号生成层添加 RSI/MACD 计算
# app/v4/strategy/signal_generator.py

def generate_signal(self, code: str, kline_data: Dict) -> Signal:
    # 计算技术指标
    indicators = {
        'rsi': self._calc_rsi(kline_data['close'], 14),
        'macd': self._calc_macd(kline_data['close']),
        'macd_histogram': ...,
        'macd_signal': ...,
    }
    
    signal = Signal(
        code=code,
        indicators=indicators,  # 传递指标到决策层
        ...
    )
```

```python
# 2. 在 DecisionEngine 中使用实际指标
# app/v4/core/decision_engine.py L408

# 修复前
indicators = {'rsi': 50, 'macd_histogram': 0}  # 默认中性
# TODO: 从信号引擎获取实际指标

# 修复后
indicators = signal.get('indicators', {'rsi': 50, 'macd_histogram': 0})
```

---

## 优化机会评估

### 新模型提出的优化机会

| 优化项 | 预期收益 | 可行性 | 建议优先级 |
|--------|----------|--------|------------|
| 智能订单类型 (限价/TWAP/VWAP) | 滑点 -70% | 🟢 高 | 🟢 P1 |
| 降级 fallback 机制 | 可用性 +4.9% | 🟢 高 | 🟢 P1 |
| 新闻情绪动态权重 | 准确率 +15% | 🟡 中 | 🟡 P2 |
| 自动调优定时任务 | - | 🟢 高 | 🟢 P1 |
| 性能监控指标 | - | 🟢 高 | 🟢 P0 |

### 可行性分析

#### 1. 智能订单类型 ✅ 可行

当前已有基础：
```python
# app/v4/risk/pre_trade.py 支持订单类型检查
# app/execution/smart_router.py 已有智能订单路由
```

**实施建议**:
```python
class SmartOrderRouter:
    def select_order_type(self, signal: Signal) -> str:
        """根据市场条件选择订单类型"""
        spread_pct = self._calc_spread_pct(signal.code)
        volatility = self._calc_volatility(signal.code)
        
        if spread_pct > 0.01 or volatility > 0.03:
            return "LIMIT"  # 高波动/大价差用限价单
        elif signal.quantity > 1000:
            return "TWAP"  # 大单用 TWAP
        else:
            return "MARKET"  # 默认市价单
```

#### 2. 降级 fallback 机制 ✅ 可行

当前已有部分基础：
```python
# app/v4/core/decision_engine.py 有异常处理
# 但缺少 LLM 故障降级
```

**实施建议**:
```python
class LLMFallback:
    """LLM 故障降级机制"""
    
    def __init__(self):
        self.primary_provider = "qwen-max"
        self.fallback_providers = ["qwen-plus", "gpt-3.5-turbo"]
        self._consecutive_failures = 0
    
    def call(self, prompt: str) -> Optional[str]:
        """带降级的 LLM 调用"""
        for provider in [self.primary_provider] + self.fallback_providers:
            try:
                response = self._call_llm(prompt, provider)
                self._consecutive_failures = 0
                return response
            except Exception as e:
                self._consecutive_failures += 1
                logger.warning(f"LLM {provider} 失败：{e}")
                continue
        
        # 所有提供商都失败，使用规则降级
        return self._rule_based_fallback(prompt)
```

#### 3. 新闻情绪动态权重 ✅ 可行

**实施建议**:
```python
class DynamicSentimentWeight:
    """新闻情绪动态权重"""
    
    def __init__(self):
        self.history: List[Dict] = []  # 历史记录
    
    def record_outcome(self, sentiment_score: float, actual_return: float):
        """记录情绪分数与实际收益"""
        self.history.append({
            'sentiment': sentiment_score,
            'actual_return': actual_return,
            'timestamp': datetime.now()
        })
    
    def get_optimal_weight(self, lookback_days: int = 30) -> float:
        """根据历史准确率计算最优权重"""
        recent = [h for h in self.history 
                  if h['timestamp'] >= datetime.now() - timedelta(days=lookback_days)]
        
        if len(recent) < 5:
            return 0.2  # 数据不足，使用默认权重
        
        # 计算情绪分数与实际收益的相关性
        sentiments = [h['sentiment'] for h in recent]
        returns = [h['actual_return'] for h in recent]
        
        correlation = np.corrcoef(sentiments, returns)[0, 1]
        
        # 相关性越高，权重越高
        if correlation > 0.5:
            return 0.4  # 高相关性，高权重
        elif correlation > 0.2:
            return 0.3  # 中相关性，中权重
        else:
            return 0.15  # 低相关性，低权重
```

---

## 修复优先级重新评估

基于验证结果，重新评估修复优先级：

| 优先级 | 问题 | 修复难度 | 预期收益 | 建议时间 |
|--------|------|----------|----------|----------|
| 🔴 **P0** | 添加 PreTradeRisk 检查 | 低 | 避免过度交易 | 立即 |
| 🔴 **P0** | RSI/MACD 数据传递修复 | 低 | 提升决策质量 | 立即 |
| 🟡 **P1** | 智能订单类型 | 中 | 滑点-30~50% | 本周 |
| 🟡 **P1** | LLM 降级 fallback | 中 | 可用性提升 | 本周 |
| 🟢 **P2** | 新闻情绪动态权重 | 中 | 准确率+10~15% | 本月 |
| 🟢 **P2** | 性能监控指标 | 低 | 数据驱动优化 | 本月 |

---

## 总结

### 新模型分析的准确性

| 维度 | 准确率 | 说明 |
|------|--------|------|
| 风险识别 | ~40% | 3 个风险中仅 1 个部分成立 |
| 代码引用 | ~0% | 引用的文件/类/方法大多不存在 |
| 优化建议 | ~80% | 优化方向合理，但收益数据无依据 |

### 建议行动

1. **立即修复 (P0)**:
   - [x] 在 `DecisionEngine` 中集成 `PreTradeRisk` 检查 - ✅ 已完成，见 `docs/PRE_TRADE_RISK_INTEGRATION_REPORT.md`
   - [x] 修复 RSI/MACD 数据传递链路 - ✅ 已完成，见 `docs/RSI_MACD_DATA_FIX.md`

2. **本周完成 (P1)**:
   - [ ] 实现智能订单类型选择
   - [ ] 实现 LLM 降级 fallback 机制

3. **本月完成 (P2)**:
   - [ ] 实现新闻情绪动态权重
   - [ ] 添加性能监控指标

### 风险提示

⚠️ **新模型分析的可信度问题**:
- 新模型引用的代码文件 (`scheduler_tasks.py`, `voting_coordinator.py`) 不存在
- 新模型提到的类 (`RiskAgent`, `AiExecutor`) 不存在
- 新模型提到的方法 (`run_voting`, `run_full_decision_pipeline`, `_process_symbol_impl`) 不存在
- **建议**: 对新模型的分析结果需要人工验证后再采纳

---

**验证者**: Claude Code  
**验证日期**: 2026-04-09  
**验证方法**: 代码搜索 + 文件读取 + 逻辑分析
