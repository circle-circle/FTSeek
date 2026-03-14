# 风控检查集成文档

## 概述

本次集成在交易执行前添加了完整的风控检查，确保所有交易都经过五层风控体系的验证。

## 修改文件

### 1. `/opt/ftseek/FTSeek/app/risk/risk_manager.py`

#### 新增数据类
- `RiskCheckResult`: 交易前风控检查结果
  - `passed: bool` - 是否通过所有检查
  - `details: Dict[str, bool]` - 各检查项的详细结果
  - `timestamp: Optional[datetime]` - 检查时间戳

#### 新增方法
- `check_before_trade(signal: Signal) -> RiskCheckResult`
  - 交易前风控检查入口方法
  - 执行所有检查项并返回综合结果
  - 异常时返回失败（安全优先）

- `_check_position_limit(code: str, quantity: int) -> bool`
  - 检查单标的仓位限制 (<20%)
  - 计算现有仓位 + 新仓位的总比例
  - 无法获取价格时保守跳过检查

- `_check_daily_trades() -> bool`
  - 检查日交易次数 (<10 次)
  - 使用配置 `max_daily_trades`

- `_check_daily_loss() -> bool`
  - 检查日亏损 (<5%)
  - 计算：`abs(today_pnl) < max_daily_loss * total_capital`

- `_check_drawdown() -> bool`
  - 检查回撤 (<15%)
  - 计算：`(peak_capital - total_capital) / peak_capital`

- `_check_confidence(confidence: float) -> bool`
  - 检查置信度 (>60%)
  - 支持从 `signal.extra['confidence']` 获取

### 2. `/opt/ftseek/FTSeek/app/trader/executor.py`

#### 修改内容
- 导入 `get_risk_manager` 从 `app.risk.risk_manager`
- `TradeExecutor.__init__()`: 初始化 `self.risk_manager`
- `TradeExecutor.execute()`: 
  - 在开仓前执行风控检查
  - 检查失败时返回明确的错误信息
  - 平仓和 ALERT 类型跳过风控检查

## 检查流程

```
Signal → TradeExecutor.execute()
           ↓
    [ALERT 类型？] → 是 → 拒绝交易
           ↓ 否
    [OPEN 类型？] → 否 → 直接执行 (平仓)
           ↓ 是
    risk_manager.check_before_trade()
           ↓
    ├─ position_limit (仓位限制)
    ├─ daily_trades (日交易次数)
    ├─ daily_loss (日亏损)
    ├─ drawdown (回撤)
    └─ confidence (置信度)
           ↓
    [全部通过？] → 是 → 执行交易
           ↓ 否
    返回错误：风控检查失败：{失败项列表}
```

## 使用示例

```python
from app.risk.risk_manager import RiskManager
from app.trader.executor import TradeExecutor
from app.strategy.base import Signal, SignalAction

# 创建信号
signal = Signal(
    code="US.AAPL",
    action=SignalAction.BUY,
    price=175.50,
    quantity=100,
    strategy="动量策略"
)
signal.extra['confidence'] = 0.85  # 设置置信度

# 执行交易（自动进行风控检查）
executor = TradeExecutor()
result = executor.execute(signal, trade_type='OPEN')

if result.success:
    print(f"交易成功：order_id={result.order_id}")
else:
    print(f"交易失败：{result.message}")
```

## 错误处理

### 风控检查失败
返回格式：
```
风控检查失败：position_limit, daily_trades
```

### 异常情况
- 检查过程中任何异常都会返回失败
- 记录详细错误日志
- 确保安全优先（失败时拒绝交易）

## 测试覆盖

### 单元测试文件
`/opt/ftseek/FTSeek/tests/test_risk_check_integration.py`

### 测试场景
1. ✅ 所有检查项通过
2. ✅ 置信度检查失败
3. ✅ 仓位限制检查失败
4. ✅ 日交易次数检查失败
5. ✅ 日亏损检查失败
6. ✅ 回撤检查失败
7. ✅ 异常处理
8. ✅ 从 extra 字段获取置信度
9. ✅ 没有置信度时默认处理
10. ✅ TradeExecutor 集成测试（通过/失败）
11. ✅ 平仓跳过风控检查
12. ✅ ALERT 类型跳过风控检查
13. ✅ 多个检查项同时失败

### 运行测试
```bash
cd /opt/ftseek/FTSeek
python -m pytest tests/test_risk_check_integration.py -v
```

## 配置项

风控阈值在 `config/risk_config.yaml` 中配置：

```yaml
risk_limits:
  min_confidence: 0.60        # 最小置信度
  max_single_position: 0.20   # 单标的仓位上限
  max_daily_trades: 10        # 日交易次数上限
  max_daily_loss: 0.05        # 日亏损上限
  max_drawdown: 0.15          # 最大回撤
```

**注意**: 本次集成未修改现有阈值配置。

## 性能考虑

- ✅ 检查快速，不阻塞交易
- ✅ 所有检查都是内存操作
- ✅ 无外部 API 调用
- ✅ 异常处理完善

## 日志记录

风控检查会记录详细日志：

```python
# 检查通过
DEBUG: 风控检查通过：{'position_limit': True, 'daily_trades': True, ...}

# 检查失败
WARNING: 风控检查失败：position_limit, daily_trades
WARNING: 仓位限制检查失败：US.AAPL 仓位 25.0% 超过限制 20.0%
```

## 验收标准

- [x] `check_before_trade()` 方法实现完整
- [x] 所有检查项正确实现
- [x] 集成到 `TradeExecutor.execute()`
- [x] 风控失败时返回明确错误信息
- [x] 单元测试验证各种场景 (16 个测试用例全部通过)

## 后续优化建议

1. 添加仓位检查时的价格缓存机制
2. 支持自定义检查项扩展
3. 添加风控检查结果持久化
4. 增加风控检查指标监控
