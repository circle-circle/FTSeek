# FTSeek 交易系统优化报告

**日期**: 2026-04-09  
**问题**: 持仓已达上限，信号持续涌入导致熔断触发循环

---

## 问题分析

### 症状
- 系统持续对已满仓状态发出 BUY 信号
- 信号被拒绝后触发熔断，熔断解除后立即继续发信号
- 今日失败统计：20次持仓限制拒绝，导致多次熔断

### 根本原因
1. **信号生成逻辑缺陷**: 未在信号生成阶段检查持仓状态
2. **加仓逻辑混乱**: 对已有持仓股票仍发出加仓信号
3. **熔断机制过于敏感**: 持仓限制导致的拒绝也计入熔断计数
4. **决策缓冲层缺失持仓检查**: 信号进入队列后才检查持仓

---

## 优化内容

### 1. 决策缓冲层优化 (`app/decision_buffer.py`)

**新增功能**:
- `max_positions` 参数：控制最大持仓数量
- `_check_position_status()` 方法：持仓状态前置检查

**检查逻辑**:
```python
# 开仓信号时，检查持仓数量上限
if action == SignalAction.BUY and current_position is None:
    if len(positions) >= self.max_positions:
        return f"已达最大持仓数 ({len(positions)}/{self.max_positions})，拒绝开仓"

# 已有持仓时，记录但让执行层处理
if current_position is not None and action == SignalAction.BUY:
    logger.info(f"{code} 已有持仓，BUY信号将转为加仓/平仓逻辑")
```

### 2. 熔断器优化 (`app/risk/circuit_breaker.py`)

**参数调整**:
| 参数 | 原值 | 新值 | 说明 |
|------|------|------|------|
| `reject_limit` | 8 | 5 | 降低触发阈值 |
| `pause_duration` | 300s | 600s | 延长冷却时间 |

**逻辑优化**:
- 持仓限制导致的拒绝不计入熔断计数
- 新增 `POSITION_ERRORS` 关键词列表
- 分级告警阈值降低 (3次警告, 4次严重警告)

### 3. 执行器优化 (`app/trader/executor.py`)

**已有持仓时的 BUY 信号处理**:
```python
if position.action == 'BUY' and signal.action == SignalAction.BUY:
    pnl_pct = (current_price - entry_price) / entry_price
    
    # 盈利 3% 以上：拒绝加仓，让止盈引擎处理
    if pnl_pct >= 0.03:
        return TradeResult(success=False, message=f"existing_position_profitable")
    
    # 亏损 2% 以上：拒绝加仓，让止损引擎处理
    elif pnl_pct <= -0.02:
        return TradeResult(success=False, message=f"existing_position_loss")
```

### 4. 配置更新 (`config/risk_config.yaml`)

新增熔断器配置节:
```yaml
circuit_breaker:
  reject_limit: 5
  pause_duration: 600
  max_drawdown_trigger: 0.15
```

---

## 优化效果预期

| 指标 | 优化前 | 预期优化后 |
|------|--------|------------|
| 无效信号数量 | 高 (持仓满仍发信号) | 低 (前置过滤) |
| 熔断触发频率 | 高 (持仓限制计入) | 低 (持仓限制不计入) |
| 熔断冷却时间 | 5分钟 | 10分钟 |
| 信号处理效率 | 低 (队列拥堵) | 高 (提前拒绝) |

---

## 备份位置

所有修改前的原始文件已备份至:
```
/opt/ftseek/FTSeek/backups/20260409_optimization/
├── decision_buffer.py
├── circuit_breaker.py
└── executor.py
```

---

## 验证状态

- [x] 模块加载测试通过
- [x] 服务重启成功
- [x] 健康检查通过 (`/health` 返回 `{"status":"ok"}`)
- [ ] 实盘运行验证 (待观察)

---

## 后续建议

1. **监控**: 观察1-2个交易日，验证优化效果
2. **指标**: 关注 `failure_stats` 中 `position_limit` 类型的失败次数
3. **调整**: 如需增加持仓上限，修改 `risk_config.yaml` 中 `max_open_positions`

---

*报告生成时间: 2026-04-09 11:15*