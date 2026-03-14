# ReportGenerator - 交易报告生成器

## 概述

ReportGenerator 负责生成每日/每周交易报告并推送到钉钉。

## 功能

### 日报
- 当日交易明细（买入/卖出）
- 当日收益（绝对值 + 百分比）
- 当前持仓状态
- 累计收益

### 周报
- 周度交易汇总
- 周度收益率
- 胜率（盈利交易数/总交易数）
- 最大回撤
- 夏普比率

## API

```python
from app.v4.report.report_generator import ReportGenerator

# 初始化
generator = ReportGenerator(
    db_path='/path/to/database.db',
    dingtalk_webhook='https://oapi.dingtalk.com/robot/send?access_token=xxx'
)

# 生成日报
daily_report = generator.generate_daily_report(code='0700.HK')  # 单只股票
daily_report = generator.generate_daily_report()  # 所有股票

# 生成周报
weekly_report = generator.generate_weekly_report(code='0700.HK')
weekly_report = generator.generate_weekly_report()

# 推送报告
result = generator.send_daily_report(code='0700.HK')
result = generator.send_weekly_report(code='0700.HK')

# 记录交易（供其他模块调用）
generator.record_trade(
    code='0700.HK',
    action='BUY',
    quantity=100,
    price=380.5,
    pnl=0,
    pnl_percent=0
)

# 更新持仓
generator.update_position(
    code='0700.HK',
    quantity=100,
    avg_cost=380.5,
    current_price=385.2,
    market_type='HK'
)

# 更新绩效
generator.update_performance(
    code='0700.HK',
    date='2026-03-12',
    daily_return=0.032,
    cumulative_return=0.058,
    trade_count=2,
    win_count=1,
    max_drawdown=-0.01,
    sharpe_ratio=1.5
)
```

## 推送时间

- **日报**: 收盘后 30 分钟
  - 港股：16:30 HKT
  - 美股：次日 04:30 EST
- **周报**: 周日晚 20:00 (Asia/Shanghai)

## 钉钉消息格式

### 日报示例

```
━━━━━━━━━━━━━━━━━━━━━━
📊 交易日报 - 2026-03-12
━━━━━━━━━━━━━━━━━━━━━━

【腾讯 0700.HK】
├─ 模式：模拟
├─ 当日交易：2 笔 (1 买 1 卖)
├─ 当日收益：+¥1,250 (+3.2%)
├─ 累计收益：+¥5,680 (+5.8%) ✅
└─ 当前持仓：100 股 @ ¥385.2

【MU 美股】
├─ 模式：模拟
├─ 当日交易：1 笔 (1 买)
├─ 当日收益：$0 (持仓中)
├─ 累计收益：$0 (新建仓)
└─ 当前持仓：50 股 @ $85.3

【汇总】
├─ 总收益：¥5,680 + $0
└─ 目标进度：58% (目标 5%)
━━━━━━━━━━━━━━━━━━━━━━
```

### 周报示例

```
━━━━━━━━━━━━━━━━━━━━━━
📈 交易周报 - 2026-W11
━━━━━━━━━━━━━━━━━━━━━━

【腾讯 0700.HK】
├─ 周收益率：+8.5%
├─ 交易次数：5 笔
├─ 胜率：80% (4/5)
├─ 最大回撤：-2.1%
└─ 夏普比率：1.85

【MU 美股】
├─ 周收益率：+3.2%
├─ 交易次数：3 笔
├─ 胜率：67% (2/3)
├─ 最大回撤：-1.5%
└─ 夏普比率：1.42

【建议】
├─ 腾讯：连续 3 天>5%，建议切换实盘
└─ MU: 保持模拟模式 (观察中)
━━━━━━━━━━━━━━━━━━━━━━
```

## 数据库表

ReportGenerator 使用以下数据库表：

### report_trades
交易记录表
- code: 股票代码
- action: BUY/SELL
- quantity: 数量
- price: 价格
- pnl: 盈亏
- pnl_percent: 盈亏百分比
- timestamp: 时间戳

### report_positions
持仓表
- code: 股票代码
- quantity: 数量
- avg_cost: 成本价
- current_price: 当前价
- market_type: HK/US/CN

### report_performance
绩效表
- code: 股票代码
- date: 日期
- daily_return: 日收益率
- cumulative_return: 累计收益率
- trade_count: 交易次数
- win_count: 盈利次数
- max_drawdown: 最大回撤
- sharpe_ratio: 夏普比率

## 测试

运行单元测试：

```bash
cd /opt/ftseek/FTSeek
python -m pytest tests/v4/test_report_generator.py -v
```

## 集成示例

### 与 PaperTrader 集成

```python
from app.v4.paper_trading import PaperTradingEngine
from app.v4.report.report_generator import ReportGenerator

# 初始化
paper_trader = PaperTradingEngine(...)
report_gen = ReportGenerator(db_path='...', dingtalk_webhook='...')

# 执行交易后记录
trade = paper_trader.execute(signal)
report_gen.record_trade(
    code=trade.code,
    action=trade.action,
    quantity=trade.quantity,
    price=trade.price,
    pnl=trade.pnl,
    pnl_percent=trade.pnl_percent
)

# 更新持仓
position = paper_trader.get_position(trade.code)
report_gen.update_position(
    code=trade.code,
    quantity=position.quantity,
    avg_cost=position.avg_cost,
    current_price=position.current_price
)
```

### 定时任务配置

```python
# 使用 APScheduler 或其他定时任务库
from apscheduler.schedulers.blocking import BlockingScheduler
from app.v4.report.report_generator import get_report_generator

scheduler = BlockingScheduler()

# 港股日报 - 每个交易日 16:30
@scheduler.scheduled_job('cron', hour=16, minute=30, day_of_week='mon-fri')
def send_hk_daily_report():
    report_gen = get_report_generator()
    report_gen.send_daily_report(code='0700.HK')

# 美股日报 - 每个交易日 04:30
@scheduler.scheduled_job('cron', hour=4, minute=30, day_of_week='mon-fri')
def send_us_daily_report():
    report_gen = get_report_generator()
    report_gen.send_daily_report(code='MU')

# 周报 - 周日 20:00
@scheduler.scheduled_job('cron', hour=20, minute=0, day_of_week='sun')
def send_weekly_report():
    report_gen = get_report_generator()
    report_gen.send_weekly_report()

scheduler.start()
```

## 注意事项

1. **数据库路径**: 建议使用统一的数据目录
2. **Webhook 安全**: 钉钉 webhook URL 应通过环境变量配置
3. **推送失败**: 推送失败会记录日志，不会抛出异常
4. **多股票支持**: 支持同时追踪多只股票的报告
5. **时区处理**: 所有时间使用 Asia/Shanghai 时区

## 依赖

- requests (钉钉推送)
- sqlite3 (数据库，Python 内置)
