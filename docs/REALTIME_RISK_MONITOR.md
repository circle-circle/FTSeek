# 🛡️ 实时风险监控模块

**实施日期**: 2026-02-26  
**版本**: v1.0

---

## ✅ 核心功能

### 1. 实时风险仪表盘

**风险评分** (0-100):
- 0-40: LOW (低风险)
- 40-60: MEDIUM (中风险)
- 60-80: HIGH (高风险)
- 80-100: CRITICAL (极高风险)

**评分维度**:
- 回撤风险 (0-40 分)
- 仓位风险 (0-20 分)
- 集中度风险 (0-20 分)
- 持仓风险 (0-20 分)

### 2. 持仓风险监控

**监控指标**:
- 未实现盈亏
- 止损距离
- 风险等级 (LOW/MEDIUM/HIGH)
- 持仓天数

### 3. 实时警报系统

**警报类型**:
- `STOP_LOSS_WARNING` - 接近止损 (CRITICAL)
- `STOP_LOSS_APPROACHING` - 止损预警 (WARNING)
- `LARGE_LOSS` - 大额亏损 (WARNING)
- `DRAWDOWN_WARNING` - 回撤超标 (CRITICAL)
- `HIGH_CONCENTRATION` - 集中度过高 (WARNING)
- `HIGH_EXPOSURE` - 仓位过高 (WARNING)

**警报级别**:
- INFO - 信息
- WARNING - 警告
- CRITICAL - 严重

---

## 🚀 使用方式

### API 接口

#### 1. 获取风险仪表盘

```bash
TOKEN="5a8f072c2cba9db1579dd829214a38112fdcddefe089d4ba58e05742097dd272"

curl "http://localhost:8899/api/risk/dashboard?token=$TOKEN"
```

**响应示例**:
```json
{
  "status": "ok",
  "dashboard": {
    "timestamp": "2026-02-26 15:06:33",
    "risk_score": 25.5,
    "risk_level": "LOW",
    "metrics": {
      "total_capital": 100000.0,
      "total_exposure": "35.2%",
      "current_drawdown": "2.1%",
      "n_positions": 3
    },
    "positions": {
      "HK.00700": {
        "market_value": 19000,
        "unrealized_pnl_percent": "2.5%",
        "stop_loss_distance": "3.2%",
        "risk_level": "LOW"
      }
    },
    "recent_alerts": [],
    "summary": {
      "total_positions": 1,
      "high_risk_positions": 0
    }
  }
}
```

#### 2. 获取最近警报

```bash
curl "http://localhost:8899/api/risk/alerts?limit=10&token=$TOKEN"
```

#### 3. 获取持仓风险

```bash
curl "http://localhost:8899/api/risk/positions?token=$TOKEN"
```

#### 4. 导出风险报告

```bash
curl "http://localhost:8899/api/risk/report?token=$TOKEN"
```

#### 5. 监控器状态

```bash
curl "http://localhost:8899/api/risk/status?token=$TOKEN"
```

---

## 📊 监控阈值配置

```python
# 默认阈值
stop_loss_warning_threshold = 0.02      # 距离止损 2% 预警
large_loss_threshold = -0.05            # 亏损 5% 预警
concentration_threshold = 0.30          # 集中度 30% 预警
drawdown_warning_threshold = 0.10       # 回撤 10% 预警
```

---

## 💡 实战应用

### 场景 1: 止损预警

```
当持仓距离止损位<2% 时:
- 发送 CRITICAL 级别警报
- 风险评分 +10 分
- 记录到警报历史
```

### 场景 2: 大额亏损

```
当持仓亏损>5% 时:
- 发送 WARNING 级别警报
- 通知风险管理员
- 建议减仓或平仓
```

### 场景 3: 集中度过高

```
当单标的仓位>30% 时:
- 发送 WARNING 级别警报
- 建议分散投资
- 限制新增仓位
```

---

## 🔧 集成到钉钉通知

```python
from app.risk.realtime_monitor import RiskAlert

def send_dingtalk_alert(alert: RiskAlert):
    """发送钉钉警报"""
    if alert.level == "CRITICAL":
        color = "#FF0000"  # 红色
        emoji = "🚨"
    elif alert.level == "WARNING":
        color = "#FFA500"  # 橙色
        emoji = "⚠️"
    else:
        color = "#008000"  # 绿色
        emoji = "ℹ️"
    
    message = (
        f"{emoji} **{alert.level}**\n\n"
        f"**类型**: {alert.type}\n"
        f"**内容**: {alert.message}\n"
        f"**时间**: {alert.timestamp}\n"
    )
    
    if alert.code:
        message += f"**标的**: {alert.code}\n"
    if alert.value is not None:
        message += f"**数值**: {alert.value:.2%}\n"
    if alert.threshold is not None:
        message += f"**阈值**: {alert.threshold:.2%}\n"
    
    # 发送钉钉消息
    notifier.send_text(message)

# 注册警报回调
monitor = init_realtime_monitor(risk_manager, alert_callbacks=[send_dingtalk_alert])
```

---

## 📁 文件清单

```
/opt/ftseek/FTSeek/
├── app/risk/
│   ├── realtime_monitor.py    ⭐ 实时监控核心 (13.6KB)
│   ├── api.py                 ⭐ 监控 API (2.8KB)
│   └── __init__.py            ✏️ 更新
├── docs/
│   └── REALTIME_RISK_MONITOR.md ⭐ 文档
└── app/
    ├── main.py                ✏️ 集成监控
    └── server.py              ✏️ 注册 API
```

---

## ⚠️ 注意事项

### 1. 监控频率

- 默认 5 秒检查一次
- 高频监控会增加 CPU 使用
- 低频监控可能错过风险

### 2. 警报处理

- CRITICAL 警报需要立即处理
- WARNING 警报需要关注
- 定期查看警报历史

### 3. 风险评分

- 评分是相对的，不是绝对的
- 不同策略风险承受能力不同
- 结合自身情况调整阈值

---

## 📈 测试结果

### API 测试

```bash
$ curl "http://localhost:8899/api/risk/dashboard?token=$TOKEN"

{
  "status": "ok",
  "dashboard": {
    "risk_score": 0.0,
    "risk_level": "LOW",
    ...
  }
}
```

✅ 测试通过！

### 日志输出

```
2026-02-26 15:04:34,580 [ftseek.risk.realtime_monitor] INFO: 实时风险监控已启动
2026-02-26 15:04:34,581 [ftseek.main] INFO: 实时风险监控已启动
```

✅ 正常运行！

---

**实施完成**: 2026-02-26 15:06  
**测试状态**: ✅ 通过
