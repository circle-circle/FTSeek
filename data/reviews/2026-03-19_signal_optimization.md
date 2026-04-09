# 信号执行优化报告

**日期**: 2026-03-19
**时间**: 17:10

---

## 问题诊断

### 信号执行情况

| 指标 | 数值 |
|------|------|
| 总信号数 | 44 |
| 已执行 | 0 |
| 被拒绝 | 38 |
| 待处理 | 6 |
| 执行率 | 0.0% |

### 拒绝原因分析

| 原因 | 数量 | 占比 |
|------|------|------|
| TrdSide 未定义 | 14 | 37% |
| OrderRetryManager 属性错误 | 8 | 21% |
| 价格精度问题 | 6 | 16% |
| 碎股限制 | 6 | 16% |
| .value 代码错误 | 4 | 11% |

---

## 修复措施

### 1. ✅ TrdSide 未定义 (14个)

**问题**: 函数内部导入，执行时未找到

**修复**:
- 在 `app/trader/executor.py` 文件顶部统一导入
- 删除函数内部的重复导入

```python
# 文件顶部添加
from futu import TrdSide, OrderType, TrdEnv
```

### 2. ✅ OrderRetryManager.RetryReason (8个)

**问题**: RetryReason 需要单独导入，不是 OrderRetryManager 的属性

**修复**:
```python
# 修改导入方式
from app.execution.order_retry import order_retry_manager, RetryReason
```

### 3. ✅ 价格精度问题 (6个)

**问题**: 重试时的价格计算未做精度处理

**修复**:
- 在 `app/execution/order_retry.py` 的 `get_next_price` 方法中添加港股价格精度处理
- 添加 `_round_hk_price` 方法

### 4. ✅ 碎股限制 (6个)

**问题**: 中芯国际每手是 **500股**，而非之前配置的 200股

**修复**:
- 更新 `app/execution/smart_router.py` 中的 lot_sizes 配置
- 更新 `app/config.py` 添加 `DEFAULT_ORDER_QTY_BY_CODE` 配置
- 修改 `app/trader/executor.py` 的 `_get_qty` 方法

**每手股数配置**:
| 股票 | 每手股数 |
|------|----------|
| HK.09992 (泡泡玛特) | 200 |
| HK.00981 (中芯国际) | **500** (修正) |
| HK.03690 (美团) | 100 |

### 5. ✅ .value 代码错误 (4个)

**问题**: SignalAction 可能是枚举或字符串

**修复**:
```python
# 兼容处理
action_str = signal.action.value if hasattr(signal.action, 'value') else str(signal.action)
```

---

## 修复文件清单

| 文件 | 修改内容 |
|------|----------|
| `app/trader/executor.py` | 导入优化、_get_qty 方法 |
| `app/execution/order_retry.py` | 价格精度处理 |
| `app/execution/smart_router.py` | lot_sizes 配置 |
| `app/config.py` | DEFAULT_ORDER_QTY_BY_CODE |
| `app/strategy/hk_specific.py` | .value 兼容处理 |
| `app/data_subscriber.py` | .value 兼容处理 |
| `start.sh` | 环境变量更新 |

---

## 验证结果

### 数量计算测试

```
HK.09992 (泡泡玛特): 200股 (1手) ✅
HK.00981 (中芯国际): 500股 (1手) ✅
HK.03690 (美团): 100股 (1手) ✅
```

### 服务状态

```
✅ 共识阈值: 0.4 (正确)
✅ 熔断器: 已初始化
✅ 策略: 已启用
✅ 订阅: 已成功
```

---

## 预期改进

| 指标 | 修复前 | 预期 |
|------|--------|------|
| 信号执行率 | 0% | >80% |
| 碎股错误 | 6次 | 0次 |
| 代码错误 | 26次 | 0次 |

---

## 后续监控

1. **明日交易观察** - 验证修复是否生效
2. **执行率统计** - 检查信号是否正常执行
3. **错误日志监控** - 确认无新错误

---

*生成时间: 2026-03-19 17:10*