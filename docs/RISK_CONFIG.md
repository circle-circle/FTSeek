# FTSeek V3 风控配置指南

> 风险管理系统完整配置说明 | 2026-03-01

## 📋 目录

1. [风控架构](#风控架构)
2. [核心参数配置](#核心参数配置)
3. [实时监控配置](#实时监控配置)
4. [熔断机制](#熔断机制)
5. [钉钉推送配置](#钉钉推送配置)
6. [最佳实践](#最佳实践)

## 🏗️ 风控架构

FTSeek V3 风控系统采用三层防护架构：

```
┌─────────────────────────────────────────┐
│          交易前风控 (Pre-Trade)          │
│  - 交易次数检查                          │
│  - 仓位比例检查                          │
│  - 可用资金检查                          │
└─────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────┐
│          交易中风控 (In-Trade)           │
│  - 实时盈亏监控                          │
│  - 回撤计算                              │
│  - 异常检测                              │
└─────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────┐
│          交易后风控 (Post-Trade)         │
│  - 日终统计                              │
│  - 连续亏损评估                          │
│  - 熔断决策                              │
└─────────────────────────────────────────┘
```

## ⚙️ 核心参数配置

### 配置文件位置

`config/risk_config.yaml`

### 参数详解

```yaml
risk_management:
  # ========== 交易限制 ==========
  max_daily_trades: 10          # 每日最大交易次数
  max_daily_loss: 5000          # 每日最大亏损金额 (HKD)
  max_drawdown: 0.15            # 最大总回撤 (15%)
  
  # ========== 仓位管理 ==========
  max_position_ratio: 0.8       # 最大持仓比例 (80%)
  max_single_position: 0.3      # 单只股票最大仓位 (30%)
  min_cash_ratio: 0.2           # 最小现金比例 (20%)
  
  # ========== 熔断机制 ==========
  consecutive_loss_limit: 3     # 连续亏损停止交易阈值
  daily_loss_stop_ratio: 0.05   # 日亏损达 5% 停止交易
  
  # ========== 风险监控 ==========
  monitor:
    enabled: true               # 启用实时监控
    push_interval_minutes: 120  # 推送间隔 (分钟)
    push_times:                 # 定时推送时间点
      - "10:00"
      - "12:00"
      - "14:00"
      - "16:00"
    only_weekdays: true         # 仅工作日推送
```

### 参数说明表

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `max_daily_trades` | int | 10 | 每日最多交易次数，防止过度交易 |
| `max_daily_loss` | float | 5000 | 每日最大亏损金额，触及后停止交易 |
| `max_drawdown` | float | 0.15 | 从最高点的最大回撤，触及后清仓 |
| `max_position_ratio` | float | 0.8 | 持仓市值占总资产的最大比例 |
| `max_single_position` | float | 0.3 | 单只股票占持仓的最大比例 |
| `min_cash_ratio` | float | 0.2 | 最小现金保留比例 |
| `consecutive_loss_limit` | int | 3 | 连续亏损次数上限 |
| `daily_loss_stop_ratio` | float | 0.05 | 日亏损比例停止线 |

## 📊 实时监控配置

### 监控指标

风控系统实时监控以下指标：

1. **账户总值** - 实时总资产
2. **可用现金** - 可交易现金
3. **持仓市值** - 当前持仓总市值
4. **持仓数量** - 持仓股票数量
5. **今日交易次数** - 当日已交易次数
6. **今日盈亏** - 当日浮动盈亏
7. **本周盈亏** - 本周累计盈亏
8. **总回撤** - 从历史最高点的回撤
9. **连续亏损次数** - 连续亏损交易次数

### 监控状态

| 状态 | 标识 | 说明 |
|------|------|------|
| 正常 | ✅ 正常 | 所有指标在安全范围内 |
| 警告 | ⚠️ 警告 | 接近风控阈值 |
| 危险 | 🚨 危险 | 触及风控阈值，触发熔断 |

### 监控推送示例

```
📊 FTSeek 风险监控 (14:00)

账户总值：HK$1,025,340.00
可用现金：HK$512,670.00 (50%)
持仓市值：HK$512,670.00 (50%)

🟢 今日盈亏：HK$+8,450.00 (+0.83%)
🟢 本周盈亏：HK$+32,100.00 (+3.21%)
总回撤：2.10% (安全线 1500%)

持仓数量：3 只
今日交易：4/10 次
连续亏损：0 次

风控状态：✅ 正常
```

## 🛑 熔断机制

### 触发条件

以下任一条件触发熔断：

1. **日亏损超限**: 今日亏损 ≥ `max_daily_loss`
2. **回撤超限**: 总回撤 ≥ `max_drawdown`
3. **交易次数超限**: 今日交易 ≥ `max_daily_trades`
4. **连续亏损超限**: 连续亏损 ≥ `consecutive_loss_limit`
5. **仓位超限**: 持仓比例 ≥ `max_position_ratio`

### 熔断动作

| 级别 | 触发条件 | 动作 |
|------|----------|------|
| 一级熔断 | 触及警告线 | 发送警告通知，降低仓位 |
| 二级熔断 | 触及停止线 | 停止新开仓，推送警报 |
| 三级熔断 | 触及清仓线 | 全部清仓，紧急通知 |

### 恢复机制

熔断后自动恢复条件：

- 一级熔断：次日自动恢复
- 二级熔断：需手动确认恢复
- 三级熔断：需人工干预，重新评估策略

## 📱 钉钉推送配置

### 环境变量

```bash
# .env 文件
DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN
DINGTALK_SECRET=你的加签密钥
```

### 推送类型

1. **交易通知** - 每笔交易执行后推送
2. **风险监控** - 定时推送风控状态
3. **熔断警报** - 触发熔断时紧急推送
4. **日报/周报/月报** - 定期报告推送

### 推送时间配置

```yaml
risk_management:
  monitor:
    push_times:
      - "10:00"   # 早盘后
      - "12:00"   # 午间
      - "14:00"   # 午后
      - "16:00"   # 收盘前
```

### 推送窗口

推送在整点后 0-5 分钟内执行，避免整点网络拥堵。

## 🎯 最佳实践

### 保守型配置

适合风险厌恶型投资者：

```yaml
risk_management:
  max_daily_trades: 5
  max_daily_loss: 3000
  max_drawdown: 0.10
  max_position_ratio: 0.6
  max_single_position: 0.2
  consecutive_loss_limit: 2
```

### 平衡型配置

适合中等风险偏好：

```yaml
risk_management:
  max_daily_trades: 10
  max_daily_loss: 5000
  max_drawdown: 0.15
  max_position_ratio: 0.8
  max_single_position: 0.3
  consecutive_loss_limit: 3
```

### 激进型配置

适合高风险偏好（不推荐）：

```yaml
risk_management:
  max_daily_trades: 20
  max_daily_loss: 10000
  max_drawdown: 0.25
  max_position_ratio: 0.95
  max_single_position: 0.5
  consecutive_loss_limit: 5
```

### 配置建议

1. **新手建议**: 从保守型配置开始，逐步调整
2. **资金管理**: 始终保持 20% 以上现金
3. **分散投资**: 单只股票不超过 30%
4. **止损纪律**: 严格执行熔断机制
5. **定期复盘**: 根据实际表现调整参数

## 🔍 测试风控配置

### 单元测试

```bash
python3 scripts/test_risk_monitor.py
```

### 模拟测试

```python
from app.risk.risk_manager import RiskManager

# 创建风控管理器
risk_mgr = RiskManager()

# 测试风控检查
result = risk_mgr.check_trade_allowed(
    stock_code="HK.00700",
    quantity=100,
    price=300.0
)
print(f"交易允许：{result.allowed}")
print(f"风控状态：{result.status}")
```

### 查看风控日志

```bash
# 实时查看风控日志
tail -f logs/risk_monitor.log

# 查看今日风控事件
grep "$(date +%Y-%m-%d)" logs/risk_monitor.log
```

## 📞 故障排查

### 问题 1: 风控推送未收到

**检查清单:**
- [ ] 钉钉 Webhook URL 是否正确
- [ ] 加签密钥是否配置
- [ ] 推送时间是否在工作日
- [ ] 网络连接是否正常

### 问题 2: 风控参数未生效

**解决方案:**
```bash
# 重启服务
systemctl restart ftseek

# 或重新加载配置
python3 scripts/reload_config.py
```

### 问题 3: 误触发熔断

**分析步骤:**
1. 查看风控日志：`logs/risk_monitor.log`
2. 检查触发条件
3. 调整相应参数
4. 手动恢复交易

---

*最后更新：2026-03-01 | 版本：V3.0.0*
