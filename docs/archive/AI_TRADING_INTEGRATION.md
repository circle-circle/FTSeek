# FTSeek-US 自动化交易集成说明

## 📊 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                    多AI决策系统                               │
│  分析师投票 → 研究员辩论 → 交易员决策 → 风控否决               │
└──────────────────────┬──────────────────────────────────────┘
                       ↓
              FinalDecision对象
                       ↓
┌─────────────────────────────────────────────────────────────┐
│                AITradingExecutor (AI交易执行器)               │
│  - 接收AI决策                                                │
│  - 创建Order + Position                                     │
│  - 设置止损止盈                                              │
│  - 提交执行引擎                                              │
└──────────────────────┬──────────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────────┐
│             ExecutionEngine / 模拟成交                        │
│  - 订单类型选择 (限价/市价)                                    │
│  - 智能路由                                                  │
│  - 幂等控制                                                  │
└──────────────────────┬──────────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────────┐
│            AIStopLossMonitor (AI止损止盈监控)                  │
│  - 实时监控持仓 (30秒间隔)                                    │
│  - 止损触发 → 自动平仓                                       │
│  - 止盈触发 → 部分/全部平仓                                   │
│  - 追踪止损 → 动态上调止损价                                  │
└──────────────────────┬──────────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────────┐
│                  推送通知 (钉钉 + 飞书)                        │
│  - 开仓通知 (含AI决策详情)                                    │
│  - 止损/止盈通知                                             │
│  - 平仓结果通知                                              │
└─────────────────────────────────────────────────────────────┘
```

## 🚀 核心组件

### 1. AITradingExecutor (AI交易执行器)

**文件**: `app/ai_trading_integration.py`

**职责**:
- 接收多AI决策系统的`FinalDecision`
- 转换为`Order`并提交执行引擎
- 创建`Position`记录并设置止损止盈
- 推送交易执行结果

**关键方法**:
```python
executor = AITradingExecutor(scheduler, dingtalk, feishu)

# 执行AI决策
result = executor.execute_decision(final_decision, current_price)

# 返回结果
{
    "status": "success",
    "action": "BUY",
    "symbol": "US.MU",
    "quantity": 100,
    "price": 128.50,
    "stop_loss": 118.22,
    "take_profits": [147.77, 160.62, 179.90],
    "order_id": "AI_US.MU_1775356949",
}
```

### 2. AIStopLossMonitor (AI止损止盈监控器)

**文件**: `app/ai_trading_integration.py`

**职责**:
- 监控所有AI决策产生的持仓
- 检查止损止盈触发
- 动态调整止损 (追踪止盈)
- 执行平仓并推送通知

**关键方法**:
```python
monitor = AIStopLossMonitor(scheduler, dingtalk)

# 检查所有AI持仓
results = monitor.check_all_positions()

# 单个持仓检查
result = monitor._check_position("US.MU", position)
```

**止损止盈策略**:
- **固定止损**: 入场价-8% (可配置)
- **分档止盈**: 
  - Level 1: +15% (平仓40%)
  - Level 2: +25% (平仓35%)
  - Level 3: +40% (平仓25%)
- **追踪止损**: 最高价-8%,只上调不下调

## 📝 集成步骤

### 步骤1: Scheduler初始化

在`scheduler_core.py`的`TradingScheduler.__init__()`中已自动初始化:

```python
# AI交易执行器
self.ai_executor = AITradingExecutor(
    scheduler=self,
    notifier=DingTalkNotifier(),
    feishu_notifier=FeishuNotifier(),
)

# AI止损监控器
self.ai_stoploss_monitor = AIStopLossMonitor(
    scheduler=self,
    notifier=DingTalkNotifier(),
)
```

### 步骤2: 在信号处理中使用AI执行器

在`scheduler_tasks.py`的`_process_symbol_impl()`中集成:

```python
def _process_symbol_impl(scheduler, symbol, vix, spy_context, market_sentiment):
    # ... 原有逻辑: 获取数据、投票、生成SignalScore ...
    
    # 新逻辑: 使用AI决策系统
    if scheduler.ai_executor:
        from app.agents.voting.voting_coordinator import VotingCoordinator
        from app.agents.final_decision import FinalDecision
        
        # 运行完整决策管线
        coordinator = VotingCoordinator(enable_debate=True)
        final_decision = coordinator.run_full_decision_pipeline(
            market_data=context,
            account_state={"daily_loss": 0, "total_position": 0.3},
        )
        
        # 执行AI决策
        current_price = context.get("price", 0)
        result = scheduler.ai_executor.execute_decision(final_decision, current_price)
        
        logger.info(f"[AI执行] {symbol}: {result}")
        return
    
    # 原有逻辑fallback
    # ...
```

### 步骤3: 定时监控止损止盈

在scheduler的定时任务中添加止损监控:

```python
# 每30秒检查一次AI持仓止损
self.scheduler.add_job(
    self._check_ai_stoploss,
    'interval',
    seconds=30,
    id='ai_stoploss_monitor',
    name='AI止损止盈监控',
)

def _check_ai_stoploss(self):
    """AI止损止盈监控定时任务"""
    if self.ai_stoploss_monitor:
        try:
            results = self.ai_stoploss_monitor.check_all_positions()
            if results:
                logger.info(f"[AI监控] 触发{len(results)}个止损/止盈事件")
        except Exception as e:
            logger.error(f"[AI监控] 检查失败: {e}")
```

## 🧪 测试验证

### 测试1: 完整决策+执行链路

```bash
cd /opt/ftseek/FTSeek-US
python3 scripts/test_auto_trading.py
```

**测试内容**:
- ✅ AI决策接收
- ✅ 订单创建
- ✅ 止损止盈设置
- ✅ 持仓记录
- ✅ 追踪止损
- ✅ 卖出平仓

### 测试2: MU真实场景推送

```bash
python3 scripts/test_mu_push_quick.py
```

**测试内容**:
- ✅ 钉钉推送完整决策报告
- ✅ 飞书推送完整决策报告

## 📊 实际运行示例

### 开仓通知 (钉钉/飞书)

```
✅ AI交易执行结果

标的: US.MU
操作: 买入
状态: 成功
价格: $128.50
数量: 100股
止损: $118.22
止盈: $147.77 / $160.62 / $179.90

AI决策强度: 68/100
AI信心: 68%

决策理由: HBM3E需求强劲,存储芯片周期复苏,财报超预期,技术面突破MA20支撑...

时间: 2026-04-05 10:40
```

### 止盈通知

```
🎯 AI持仓止盈L1通知

标的: US.MU
原因: 止盈L1
入场: $128.50
出场: $147.77
盈亏: 💰 $1927.00 (+15.00%)

持仓时长: 2天5小时

时间: 2026-04-07 15:30
```

### 止损通知

```
🛡️ AI持仓止损通知

标的: US.MU
原因: 止损
入场: $128.50
出场: $118.22
盈亏: 🔴 $-1028.00 (-8.00%)

持仓时长: 1天3小时

时间: 2026-04-06 13:45
```

## ⚙️ 配置说明

### 止损止盈参数

在`config_us.py`的`RISK_CONFIG`中配置:

```python
RISK_CONFIG = {
    "DEFAULT_STOP_LOSS": 0.08,        # 止损8%
    "DEFAULT_TAKE_PROFIT_1": 0.15,    # 止盈1档15%
    "DEFAULT_TAKE_PROFIT_2": 0.25,    # 止盈2档25%
    "DEFAULT_TAKE_PROFIT_3": 0.40,    # 止盈3档40%
    "TRAILING_STOP_ATR_MULT": 2.0,    # 追踪止损ATR倍数
}
```

### 股票下单数量

在`config/watchlist_us.json`中配置每只股票的下单数量:

```json
{
  "us_stocks": [
    {
      "code": "US.MU",
      "order_qty": 100,
      "stop_loss": -0.08,
      "take_profit": 0.20
    }
  ]
}
```

## 🔐 风控机制

### 一票否决权

风控官检查以下6项,任何一项不通过即否决交易:

1. **辩论分歧度**: >50%视为分歧过大
2. **日内亏损**: 超过账户5%停止交易
3. **总仓位风险**: 超过80%停止开新仓
4. **VIX极端值**: >30视为市场恐慌
5. **重大风险信号**: 黑名单股票、财报窗口等
6. **连续亏损**: 连续3笔亏损暂停交易

### 仓位管理

- **单只股票最大仓位**: 20% (TIER1) / 15% (TIER2)
- **AI建议仓位**: 根据`position_size` (0.0-0.20)
- **实际下单量**: 优先使用watchlist配置,其次AI建议,最后默认50股

## 📈 监控与告警

### 实时监控

- **持仓监控**: 每30秒检查一次
- **价格更新**: 从TwelveData/Finnhub获取实时报价
- **止损止盈**: 自动触发,无需人工干预

### 告警机制

- **一级告警**: 止损/止盈触发 (钉钉推送)
- **二级告警**: 执行失败 (钉钉+邮件)
- **三级告警**: 连续3次失败 (钉钉+邮件+紧急联系人)

## 🎯 完整工作流程

```
1. 盘前策略展望 (美东09:00)
   ↓
2. AI决策扫描 (每5分钟)
   ├─ 分析师投票 (4模型并行)
   ├─ 研究员辩论 (3轮)
   ├─ 交易员决策
   └─ 风控审查
   ↓
3. 自动下单 (AI执行器)
   ├─ 创建Order
   ├─ 设置止损止盈
   └─ 推送开仓通知
   ↓
4. 实时监控 (每30秒)
   ├─ 检查止损
   ├─ 检查止盈
   └─ 追踪止损
   ↓
5. 自动平仓
   ├─ 止损触发 → 全部平仓
   ├─ 止盈触发 → 部分/全部平仓
   └─ 推送平仓通知
   ↓
6. 盘后总结 (美东16:05)
   └─ 交易统计 + 策略分析
```

## ✅ 验证清单

- [x] AI决策系统正常工作
- [x] AITradingExecutor初始化成功
- [x] AIStopLossMonitor初始化成功
- [x] 订单创建与提交
- [x] 止损止盈设置
- [x] 持仓记录持久化
- [x] 追踪止损动态调整
- [x] 钉钉推送正常
- [x] 飞书推送正常
- [x] 卖出平仓正常
- [x] 盈亏计算准确

## 🚀 下一步

1. **实盘测试**: 在模拟环境中完整运行1-2周
2. **参数优化**: 根据实际表现调整止损止盈参数
3. **策略扩展**: 添加更多AI模型和辩论策略
4. **性能优化**: 提高监控频率和响应速度

---

**系统已完全打通,可以开始自动化交易!** 🎊
