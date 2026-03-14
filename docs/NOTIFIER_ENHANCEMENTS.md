# 钉钉推送增强 - 使用说明

**文件路径**: `app/notifier/dingtalk.py`  
**更新日期**: 2026-03-01  
**版本**: v3.0

---

## 📋 新增方法

### 1. `send_confirm_reminder(signal, elapsed_minutes)`
**用途**: 发送确认提醒（15 分钟未响应）

**参数**:
- `signal`: Signal 对象，待确认的信号
- `elapsed_minutes`: int, 已等待的分钟数

**示例**:
```python
notifier.send_confirm_reminder(signal, elapsed_minutes=15)
```

**消息内容**:
- 信号基本信息（标的、方向、价格、数量）
- 已等待时间
- 剩余过期时间
- 三按钮操作（确认/延期/忽略）

---

### 2. `send_risk_alert(alert_type, details)`
**用途**: 发送风险预警（五层风控触发）

**参数**:
- `alert_type`: str, 预警类型
  - `"signal"` - 信号级风控
  - `"order"` - 订单级风控
  - `"position"` - 仓位级风控
  - `"account"` - 账户级风控
  - `"system"` - 系统级风控
- `details`: dict, 预警详情数据

**示例**:
```python
# 信号级风控
notifier.send_risk_alert("signal", {
    "confidence": 0.45,
    "threshold": 0.60
})

# 仓位级风控
notifier.send_risk_alert("position", {
    "reason": "仓位超限",
    "current_position": 0.85,
    "max_position": 0.80,
    "today_trades": 12,
    "max_daily": 10
})
```

**特性**:
- 自动@所有人
- 根据预警类型显示不同图标
- 详细的风控参数展示

---

### 3. `send_daily_stats(stats)`
**用途**: 发送收盘统计（每日 16:30）

**参数**:
- `stats`: dict, 统计数据字典

**示例**:
```python
stats = {
    "date": "2026-03-01",
    "today_signals": 8,
    "today_executed": 5,
    "today_ignored": 3,
    "today_pnl": 12450.00,
    "today_pnl_pct": 1.23,
    "week_pnl": 45600.00,
    "week_pnl_pct": 4.56,
    "positions": [
        {"code": "HK.00700", "name": "腾讯控股", "pnl_pct": 2.3},
        {"code": "HK.09988", "name": "阿里巴巴", "pnl_pct": 1.8},
    ],
    "risk_ok": True,
    "consecutive_loss": 0,
    "drawdown": -2.1,
    "max_drawdown": 15
}
notifier.send_daily_stats(stats)
```

**展示内容**:
- 今日信号统计
- 今日/本周盈亏
- 当前持仓列表
- 风控状态

---

### 4. `send_weekly_report(report)`
**用途**: 发送周报（周一 09:00）

**参数**:
- `report`: dict, 周报数据字典

**示例**:
```python
report = {
    "week": "2026-W09",
    "total_trades": 25,
    "wins": 16,
    "pnl": 85300.00,
    "pnl_pct": 8.53,
    "max_drawdown": -3.2,
    "profit_loss_ratio": 2.1,
    "strategies": {
        "融合 v2": {"win_rate": 0.68, "count": 15},
        "均线交叉": {"win_rate": 0.60, "count": 7},
    },
    "best_trade": {"code": "HK.00700", "pnl_pct": 12.5},
    "worst_trade": {"code": "HK.09992", "pnl_pct": -4.2},
    "outlook": "关注财报季，谨慎交易"
}
notifier.send_weekly_report(report)
```

**展示内容**:
- 本周交易汇总
- 胜率/盈亏比
- 各策略表现
- 最佳/最差交易
- 下周展望

---

### 5. `send_monthly_report(report)`
**用途**: 发送月报（每月 1 日 09:00）

**参数**:
- `report`: dict, 月报数据字典

**示例**:
```python
report = {
    "month": "2026-02",
    "total_trades": 98,
    "wins": 62,
    "pnl": 256000.00,
    "pnl_pct": 25.6,
    "max_drawdown": -5.8,
    "sharpe_ratio": 1.85,
    "sortino_ratio": 2.34,
    "strategies": {
        "融合 v2": {"pnl": 156000.00, "count": 60},
        "均线交叉": {"pnl": 68000.00, "count": 25},
    },
    "plan": "继续优化策略，控制风险"
}
notifier.send_monthly_report(report)
```

**展示内容**:
- 月度交易汇总
- 收益率/最大回撤
- 夏普比率/索提诺比率
- 策略归因分析
- 下月计划

---

### 6. `send_risk_monitor(monitor)`
**用途**: 发送风险监控面板（每 2 小时）

**参数**:
- `monitor`: dict, 监控数据字典

**示例**:
```python
monitor = {
    "time": "14:00",
    "total_value": 1025340.00,
    "cash": 512670.00,
    "position_value": 512670.00,
    "today_pnl": 8450.00,
    "today_pnl_pct": 0.83,
    "week_pnl": 32100.00,
    "week_pnl_pct": 3.21,
    "drawdown": -2.1,
    "position_count": 3,
    "today_trades": 4,
    "max_daily": 10,
    "consecutive_loss": 0,
    "risk_ok": True
}
notifier.send_risk_monitor(monitor)
```

**展示内容**:
- 账户总值/现金/持仓
- 今日/本周/总回撤
- 持仓数量/交易次数
- 风控状态

---

## 🎯 使用场景

### 场景 1: 信号确认流程
```python
# 1. 发送买卖信号（已有 send_signal）
notifier.send_signal(signal)

# 2. 15 分钟后未响应，发送提醒
notifier.send_confirm_reminder(signal, elapsed_minutes=15)

# 3. 用户点击按钮后，发送执行结果（已有 send_execution_result）
notifier.send_execution_result(signal, success=True)
```

### 场景 2: 风控预警
```python
# 风控检查失败时发送预警
if not risk_manager.check_signal(signal):
    notifier.send_risk_alert("signal", {
        "confidence": signal.confidence,
        "threshold": config.MIN_CONFIDENCE
    })
```

### 场景 3: 定时统计推送
```python
# 每日 16:30 收盘统计
stats = calculate_daily_stats()
notifier.send_daily_stats(stats)

# 每 2 小时风险监控
monitor = get_risk_monitor_data()
notifier.send_risk_monitor(monitor)
```

---

## ✅ 验收标准

- [x] 买卖信号推送正常（已有 send_signal，支持三按钮）
- [x] 确认提醒推送正常（新增 send_confirm_reminder）
- [x] 执行结果推送正常（已有 send_execution_result）
- [x] 风险预警推送正常（新增 send_risk_alert）
- [x] 收盘统计推送正常（新增 send_daily_stats）
- [x] 周报推送正常（新增 send_weekly_report）
- [x] 月报推送正常（新增 send_monthly_report）
- [x] 风险监控面板正常（新增 send_risk_monitor）
- [x] 所有方法语法检查通过
- [x] 限流机制正常工作（继承自原有代码）

---

## 🔧 配置要求

确保以下环境变量已设置：

```bash
export DINGTALK_WEBHOOK_URL="https://oapi.dingtalk.com/robot/send?access_token=xxx"
export DINGTALK_SECRET="SECxxx"  # 可选，加签密钥
export CALLBACK_EXTERNAL_URL="https://your-domain.com"
export API_TOKEN="your_api_token"
```

---

## 📝 注意事项

1. **限流保护**: 所有推送方法都继承自 `_can_send()` 限流检查，尊重钉钉 20 条/分钟限制
2. **降级处理**: 如果 webhook 未配置，方法会返回 False 但不报错
3. **风控预警**: `send_risk_alert` 会自动@所有人，其他消息不会
4. **按钮回调**: 确认/延期/忽略按钮需要配置回调接口处理

---

**更新完成时间**: 2026-03-01 10:54
**更新人**: 阿呆 (FTSeek 推送子代理)
