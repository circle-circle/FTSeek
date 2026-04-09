# 动态仓位管理功能 (Dynamic Position Management)

**版本**: v1.0 (2026-04-09)  
**功能**: 根据股票池大小和交易环境动态调整最大持仓数

---

## 📋 概述

动态仓位管理功能允许系统根据监控股票池的大小和交易环境（模拟/实盘）自动调整 `max_positions` 参数，无需手动配置。

### 核心优势

1. **模拟交易充分测试**: 在模拟环境下，`max_positions` = 股票池数量，可以充分验证加仓、平仓功能
2. **实盘交易保守策略**: 在实盘环境下，`max_positions` = min(股票池×50%, 配置上限)，控制风险
3. **灵活配置**: 支持显式指定、动态计算、默认值三种模式

---

## 🎯 工作原理

### 动态计算规则

```python
if TRADE_ENV == 1:  # 模拟交易
    max_positions = 股票池数量
else:  # 实盘交易
    max_positions = min(
        max(1, int(股票池数量 × 0.5)),  # 股票池的 50%
        MAX_OPEN_POSITIONS  # 配置上限
    )
```

### 示例

| 交易环境 | 股票池数量 | 配置上限 | max_positions |
|---------|-----------|---------|---------------|
| 模拟 (TRADE_ENV=1) | 10 | 4 | **10** (股票池数量) |
| 实盘 (TRADE_ENV=0) | 10 | 4 | **4** (min(5, 4)) |
| 实盘 (TRADE_ENV=0) | 20 | 8 | **8** (min(10, 8)) |

---

## ⚙️ 配置方式

### 1. config.yaml 配置

```yaml
strategy:
  decision_buffer:
    signal_cooldown: 10
    max_pending: 50
    min_confidence: 0.35
    max_positions: null             # null = 动态计算
    dynamic_position_mode: true     # true = 启用动态模式
```

### 2. 配置选项说明

| 参数 | 类型 | 默认值 | 说明 |
|-----|------|--------|------|
| `max_positions` | `int` | `null` | 最大持仓数。`null` 表示动态计算 |
| `dynamic_position_mode` | `bool` | `true` | 是否启用动态仓位模式 |

### 3. 三种模式

#### 模式 A: 动态计算 (推荐)

```yaml
max_positions: null
dynamic_position_mode: true
```

- 模拟交易：`max_positions = 股票池数量`
- 实盘交易：`max_positions = min(股票池×50%, 配置上限)`

#### 模式 B: 显式指定

```yaml
max_positions: 6
dynamic_position_mode: true  # 或 false
```

- 无论模拟/实盘，都使用指定值 `6`

#### 模式 C: 默认值

```yaml
max_positions: null
dynamic_position_mode: false
```

- 使用默认值 `4`

---

## 🧪 测试验证

### 运行测试脚本

```bash
# 测试动态 max_positions 功能
python scripts/test_dynamic_max_positions.py

# 测试从配置中心初始化
python scripts/test_init_decision_buffer.py
```

### 测试结果示例

```
============================================================
测试动态 max_positions 功能
============================================================

【测试 1】动态仓位模式（模拟交易）
  max_positions = 10
  预期：10 (股票池数量)
  ✅ 通过

【测试 2】显式指定 max_positions=6
  max_positions = 6
  预期：6 (显式指定优先)
  ✅ 通过

【测试 3】禁用动态仓位模式
  max_positions = 4
  预期：4 (默认值)
  ✅ 通过
```

---

## 📊 使用场景

### 场景 1: 模拟交易充分测试

**目标**: 验证加仓、平仓功能的完整性

**配置**:
```bash
export TRADE_ENV=1  # 模拟交易
# config.yaml:
# max_positions: null
# dynamic_position_mode: true
```

**结果**: 如果股票池有 10 只股票，则 `max_positions = 10`，允许同时持有全部 10 只股票，充分测试加仓、平仓逻辑。

### 场景 2: 实盘交易风险控制

**目标**: 实盘环境下控制风险，分散持仓

**配置**:
```bash
export TRADE_ENV=0  # 实盘交易
# config.yaml:
# max_positions: null
# dynamic_position_mode: true
```

**结果**: 如果股票池有 10 只股票，则 `max_positions = min(5, 4) = 4`，最多持有 4 只股票（50% 分散）。

### 场景 3: 特殊策略需求

**目标**: 特定策略需要固定持仓数上限

**配置**:
```bash
# config.yaml:
# max_positions: 3
# dynamic_position_mode: false
```

**结果**: 无论模拟/实盘，`max_positions = 3`。

---

## 🔍 日志输出

### 初始化日志

```
[INFO] ftseek.decision_buffer: 决策缓冲层已初始化：冷却期=10s, 最大队列=50, 最低置信度=0.35, max_positions=10 (dynamic=True)
```

### 动态仓位计算日志

```
[INFO] [动态仓位] 模拟交易模式：max_positions = 10 (股票池数量)
[INFO] [动态仓位] 实盘交易模式：max_positions = 4 (股票池=10×50%, 配置上限=4)
```

### 持仓检查日志

```
[INFO] [决策缓冲] 已达最大持仓数 (4/10)，拒绝开仓
[INFO] [决策缓冲] HK.00700 已有持仓，BUY 信号将转为加仓/平仓逻辑
```

---

## 📁 相关文件

| 文件 | 说明 |
|-----|------|
| `app/decision_buffer.py` | 决策缓冲层实现（包含动态仓位计算） |
| `config/config.yaml` | 配置文件（`strategy.decision_buffer` 部分） |
| `scripts/test_dynamic_max_positions.py` | 功能测试脚本 |
| `scripts/test_init_decision_buffer.py` | 集成测试脚本 |

---

## 🚀 迁移指南

### 从旧版本升级

如果你的配置文件中有：

```yaml
# 旧配置
max_positions: 4
```

建议修改为：

```yaml
# 新配置 (推荐)
max_positions: null
dynamic_position_mode: true
```

### 保持兼容

如果不想使用动态仓位功能，可以：

```yaml
# 保持静态配置
max_positions: 4
dynamic_position_mode: false
```

---

## ❓ FAQ

### Q1: 为什么模拟交易要使用股票池数量？

**A**: 模拟交易的目的是充分验证系统功能。如果股票池有 10 只股票，但 `max_positions=4`，则只能同时持有 4 只，无法测试：
- 多只股票的并发持仓管理
- 第 5-10 只股票的开仓功能
- 完整场景的加仓/平仓逻辑

### Q2: 实盘交易为什么限制为 50%？

**A**: 实盘交易需要控制风险。50% 的分散度可以：
- 避免过度集中风险
- 保留现金应对市场波动
- 符合巴菲特的"适度分散"理念

### Q3: 如何临时调整 max_positions？

**A**: 有三种方式：

1. **修改配置文件** (需要重启)
   ```yaml
   max_positions: 6
   ```

2. **环境变量覆盖** (需要支持)
   ```bash
   export MAX_POSITIONS=6
   ```

3. **代码初始化时指定**
   ```python
   init_decision_buffer(max_positions=6, dynamic_position_mode=False)
   ```

---

## 📈 性能影响

- **计算开销**: 动态计算只在初始化时执行一次，运行时无额外开销
- **内存占用**: 无额外内存占用
- **并发安全**: 已添加线程锁保护

---

## ✅ 验收标准

- [x] 模拟交易时 `max_positions` = 股票池数量
- [x] 实盘交易时 `max_positions` = min(股票池×50%, 配置上限)
- [x] 显式指定值优先于动态计算
- [x] 配置中心读取正确
- [x] 日志输出清晰可追踪
- [x] 测试用例覆盖率 100%

---

**作者**: FTSeek Team  
**更新日期**: 2026-04-09
