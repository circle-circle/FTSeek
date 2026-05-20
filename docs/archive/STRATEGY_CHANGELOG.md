# FTSeek-US 策略变更日志 (Strategy Changelog)

**目的**: 记录所有策略参数调整的详细信息，包括调整日期、内容、回测结果对比和实盘表现影响

---

## 最新变更

### 2026-04-06 | P1 策略模块化重构 | 版本 v1.2.0

**变更类型**: 架构优化

**变更内容**:
1. 创建 `app/decision/strategies/` 模块目录
2. 定义策略基类 `BaseStrategy` 和统一接口
3. 实现策略自动注册机制
4. 迁移/新建三个内置策略:
   - `price_break`: 价格突破策略
   - `ma_cross`: 均线交叉策略
   - `rsi_rebound`: RSI 反弹策略
5. 更新 `signal_generator.py` 支持从新模块加载策略

**影响范围**:
- 策略代码集中管理，便于维护
- 新策略开发只需继承基类并添加 `@register_strategy` 装饰器
- 向后兼容旧接口

**新增文件**:
- `app/decision/strategies/__init__.py`
- `app/decision/strategies/base.py` (基类 + 注册机制)
- `app/decision/strategies/price_break.py`
- `app/decision/strategies/ma_cross.py`
- `app/decision/strategies/rsi_rebound.py`
- `app/decision/strategies/README.md` (使用文档)

**使用示例**:
```python
from app.decision.strategies import get_strategy, list_strategies

# 列出所有策略
strategies = list_strategies()

# 获取策略实例
strategy = get_strategy("price_break")

# 执行策略
result = strategy.execute("AAPL", market_data)
```

**文档链接**:
- `app/decision/strategies/README.md`
- `app/decision/strategies/base.py`

---

### 2026-04-06 | P0 配置统一化修复 | 版本 v1.1.0

**变更类型**: 配置统一化

**变更内容**:
1. `pre_trade_check.py` 中的 `min_confidence` 从硬编码 0.65 改为从 `RISK_CONFIG` 读取
2. `config_us.py` 中 `MIN_CONFIDENCE` 从 0.50 统一为 0.55
3. 新增 `MIN_CONSENSUS` 配置项 (0.55)
4. 增强配置验证机制（一致性检查、阈值合理性检查）

**影响范围**:
- 预交易风控检查逻辑
- 配置文件层级统一

**回测结果**: 待验证

**实盘表现**: 待观察

**文档链接**:
- `app/risk/pre_trade_check.py`
- `config/config_us.py`

---

### 2026-03-30 | 方案 E 投票参数优化 | 版本 v1.0.0

**变更类型**: 策略参数优化

**变更内容**:
1. 中性区间：45-55 → 40-60
2. 信心阈值：0.65 → 0.55
3. Fallback 置信度：0.40/0.30/0.20 → 0.50/0.40/0.30
4. VIX 阈值：单一 25 → 三阈值 20/25/30
5. 动态仓位管理：新增 5-15% 分级

**影响范围**:
- 投票系统信号通过率提升 ~25%
- 中性过滤率降低至 ~22%（原 39.6%）
- 年交易次数预期：45 → 65-70 次

**回测结果**: 
- 参见 `docs/BACKTEST_VERIFICATION_SCHEME_E_20260330.md`
- Sharpe Ratio: 1.2 → 1.5
- Max Drawdown: -12% → -9%

**实盘表现**: 待观察

**文档链接**:
- `docs/VOTING_OPTIMIZATION_COMPLETE_20260330.md`
- `app/agents/voting/voting_config.py`

---

### 2026-03-27 | Voting 系统 P0 修复 | 版本 v0.9.0

**变更类型**: Bug 修复 + 参数调整

**变更内容**:
1. 修复 GLM5/QWEN35 枚举引用错误
2. 更新测试用例适配 2 模型配置
3. 统一超时设置为 90 秒

**影响范围**:
- 投票系统稳定性提升
- 测试覆盖率保持 100%

**回测结果**: 参见 `docs/BACKTEST_VOTING_OPTIMIZED_20260327.md`

**实盘表现**: 无负面影响

**文档链接**:
- `docs/VOTING_P0_FIX_20260327.md`
- `app/agents/voting/`

---

## 历史变更

### 2026-03-24 | 初始策略配置 | 版本 v0.1.0

**变更类型**: 初始版本

**变更内容**:
1. 建立四大引擎架构（决策/风险/执行/观测）
2. 多 Agent 投票系统上线
3. 基础风控参数设置

**影响范围**: 系统从 0 到 1

**回测结果**: 参见 `docs/BACKTEST_REPORT_20260327.md`

**实盘表现**: 模拟盘运行稳定

**文档链接**:
- `ARCHITECTURE.md`
- `docs/PHASE1_COMPLETE.md`

---

## 变更记录模板

### YYYY-MM-DD | 变更标题 | 版本 vX.X.X

**变更类型**: 配置统一化/策略优化/Bug 修复/新增策略

**变更内容**:
- 变更点 1
- 变更点 2

**影响范围**:
- 影响的模块
- 预期的信号通过率变化
- 预期的交易频率变化

**回测结果**:
- 回测周期
- Sharpe Ratio 变化
- Max Drawdown 变化
- 年化收益变化

**实盘表现**:
- 实盘运行天数
- 实际交易次数
- 实际胜率
- 实际问题记录

**文档链接**:
- 相关代码文件
- 相关文档

---

## 配置版本对照表

| 版本 | 日期 | MIN_CONFIDENCE | MIN_CONSENSUS | NEUTRAL_LOW | NEUTRAL_HIGH | 备注 |
|------|------|----------------|---------------|-------------|--------------|------|
| v1.1.0 | 2026-04-06 | 0.55 | 0.55 | 40 | 60 | P0 配置统一化 |
| v1.0.0 | 2026-03-30 | 0.55 | - | 40 | 60 | 方案 E 优化 |
| v0.9.0 | 2026-03-27 | 0.60 | - | 45 | 55 | P0 修复 |
| v0.1.0 | 2026-03-24 | 0.50 | - | 45 | 55 | 初始版本 |

---

*最后更新：2026-04-06*
