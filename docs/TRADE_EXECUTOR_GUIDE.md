# FTSeek 交易执行器 - 使用指南

## 📋 功能概述

增强版交易执行器支持：
- ✅ 富途实盘下单联动
- ✅ 模拟盘测试
- ✅ 完整交易记录
- ✅ 收益率自动计算
- ✅ 钉钉执行结果推送
- ✅ 交易对自动配对

---

## 🚀 快速开始

### 1. 查看待确认信号

```bash
cd /opt/ftseek/FTSeek
export $(cat .env | grep -v '^#' | xargs)

# 查询待确认信号
python3 scripts/query_signals.py --status pending_confirmation --limit 10
```

### 2. 执行交易确认

**模拟盘模式:**
```bash
# 确认买入
python3 scripts/trade_executor.py --signal-id <信号 ID> --action BUY --mode paper

# 确认卖出
python3 scripts/trade_executor.py --signal-id <信号 ID> --action SELL --mode paper
```

**实盘模式:**
```bash
# 确认买入 (实际下单)
python3 scripts/trade_executor.py --signal-id <信号 ID> --action BUY --mode real

# 确认卖出 (实际下单)
python3 scripts/trade_executor.py --signal-id <信号 ID> --action SELL --mode real
```

---

## 📊 执行流程

### 买入执行

```
1. 收到钉钉买卖信号推送
   ↓
2. 在服务器运行确认命令
   ↓
3. 执行器下单 (模拟/实盘)
   ↓
4. 记录到数据库
   ↓
5. 推送执行结果到钉钉
```

### 卖出执行 (含收益计算)

```
1. 收到钉钉买卖信号推送
   ↓
2. 在服务器运行确认命令
   ↓
3. 执行器下单 (模拟/实盘)
   ↓
4. 查找对应买入记录
   ↓
5. 计算收益率和收益金额
   ↓
6. 记录交易对到 trade_pairs 表
   ↓
7. 推送执行结果 (含收益) 到钉钉
```

---

## 📱 钉钉推送示例

### 买入信号推送

```
🟢 交易信号确认

腾讯控股 (HK.00700)

---

方向：买入
策略：均线交叉
价格：514.00 HKD
数量：100 股
原因：MA5(515.00) 上穿 MA20(510.00), 金叉买入

---

🕒 时间：2026-03-02 13:30:00
📊 模式：模拟

---

确认方式:

cd /opt/ftseek/FTSeek
export $(cat .env | grep -v '^#' | xargs)
python3 scripts/trade_executor.py --signal-id abc123 --action BUY --mode paper

*请在服务器上运行以上命令确认交易*
```

### 执行结果推送 (买入)

```
✅ 交易执行结果

腾讯控股 (HK.00700)

---

方向：买入
状态：成功
模式：模拟

订单 ID: PAPER_abc123_1709358600
成交价格：514.00 HKD
成交数量：100 股

---

🕒 时间：2026-03-02 13:30:00
```

### 执行结果推送 (卖出含收益)

```
✅ 交易执行结果

腾讯控股 (HK.00700)

---

方向：卖出
状态：成功
模式：模拟

订单 ID: PAPER_def456_1709362200
成交价格：530.00 HKD
成交数量：100 股

---

📈 收益统计

收益金额：+1,600.00 HKD
收益率：+3.11%
买入价：514.00 HKD
卖出价：530.00 HKD

---

🕒 时间：2026-03-02 14:30:00
```

---

## 🗄️ 数据库记录

### signals 表

| 字段 | 说明 |
|------|------|
| id | 信号 ID |
| code | 股票代码 |
| action | buy/sell |
| price | 价格 |
| quantity | 数量 |
| status | pending/confirmed/executed/failed |
| executed_at | 执行时间 |
| order_id | 订单 ID |

### trade_pairs 表

| 字段 | 说明 |
|------|------|
| id | 交易对 ID |
| buy_signal_id | 买入信号 ID |
| sell_signal_id | 卖出信号 ID |
| code | 股票代码 |
| buy_price | 买入价 |
| sell_price | 卖出价 |
| quantity | 数量 |
| return_rate | 收益率 (%) |
| profit | 收益金额 |
| open_time | 开仓时间 |
| close_time | 平仓时间 |
| status | closed |

---

## 🔧 常用命令

### 查询信号

```bash
# 查询所有待确认信号
python3 scripts/query_signals.py --status pending_confirmation

# 查询某股票的信号
python3 scripts/query_signals.py --stock HK0700 --limit 10

# 查询今日信号
python3 scripts/query_signals.py --from 2026-03-02 --to 2026-03-02
```

### 查询交易对

```bash
# 查询所有交易对
sqlite3 data/ftseek.db "SELECT * FROM trade_pairs ORDER BY close_time DESC LIMIT 10;"

# 查询某股票的交易对
sqlite3 data/ftseek.db "SELECT code, buy_price, sell_price, return_rate, profit FROM trade_pairs WHERE code='HK.00700' ORDER BY close_time DESC;"

# 统计总收益
sqlite3 data/ftseek.db "SELECT SUM(profit) as total_profit, COUNT(*) as total_trades FROM trade_pairs;"
```

### 查询绩效

```bash
# 使用绩效追踪器
cd /opt/ftseek/FTSeek
export $(cat .env | grep -v '^#' | xargs)
python3 -c "
from app.performance.tracker import get_performance_tracker
tracker = get_performance_tracker()

# 获取总收益率
result = tracker.get_total_return(days=7)
print(f'7 天总收益：{result}')

# 获取持仓
position = tracker.get_position(code='HK.00700')
print(f'腾讯持仓：{position}')
"
```

---

## ⚙️ 配置说明

### 环境变量 (.env)

```bash
# 富途配置
FUTU_HOST=127.0.0.1
FUTU_PORT=11111
FUTU_PASSWORD=你的交易密码 (MD5)

# 钉钉推送
DINGTALK_WEBHOOK_URL=https://...
DINGTALK_SECRET=SEC...

# 交易模式
FTSEEK_TRADING_MODE=paper  # paper=模拟，real=实盘
```

### 交易模式切换

**全局切换:**
```bash
# 编辑 .env 文件
vim .env

# 修改
FTSEEK_TRADING_MODE=real  # 实盘模式
```

**命令行指定:**
```bash
# 单次执行使用实盘
python3 scripts/trade_executor.py --signal-id xxx --action BUY --mode real
```

---

## ⚠️ 注意事项

### 模拟盘模式

- ✅ 不实际下单
- ✅ 生成模拟订单 ID (PAPER_xxx)
- ✅ 适合测试和验证策略
- ✅ 收益计算逻辑与实盘一致

### 实盘模式

- ⚠️ **实际下单交易**
- ⚠️ 需要正确配置交易密码
- ⚠️ 请谨慎确认每个信号
- ⚠️ 建议先用模拟盘验证

### 安全建议

1. **先模拟后实盘** - 用模拟盘验证策略和流程
2. **小额测试** - 实盘初期使用小金额
3. **定期复盘** - 每周查看交易对和收益率
4. **设置止损** - 在策略中设置止损条件

---

## 📈 绩效分析

### 生成周报

```bash
# 生成本周周报
python3 scripts/generate_weekly_report.py --week this

# 生成并推送周报
python3 scripts/generate_weekly_report.py --week this --push
```

### 回测对比

```bash
# 生成 30 天对比报告
python3 scripts/backtest_compare_report.py --period 30d

# 生成并推送对比报告
python3 scripts/backtest_compare_report.py --period 30d --push
```

### 查询收益率统计

```bash
sqlite3 data/ftseek.db << EOF
SELECT 
    code as 股票，
    COUNT(*) as 交易次数，
    AVG(return_rate) as 平均收益率，
    SUM(profit) as 总收益
FROM trade_pairs 
GROUP BY code
ORDER BY 总收益 DESC;
EOF
```

---

## 🚨 故障排查

### 信号不存在

```bash
# 检查信号 ID 是否正确
python3 scripts/query_signals.py --limit 5

# 检查信号状态
sqlite3 data/ftseek.db "SELECT id, status FROM signals WHERE id='<信号 ID>';"
```

### 富途下单失败

```bash
# 检查 OpenD 状态
ps aux | grep FutuOpenD

# 检查端口
netstat -tlnp | grep 11111

# 检查交易密码配置
cat .env | grep FUTU_PASSWORD
```

### 钉钉推送失败

```bash
# 测试推送
python3 scripts/test_dingtalk_enhanced.py

# 检查 Webhook 配置
cat .env | grep DINGTALK
```

---

*最后更新：2026-03-02 | FTSeek V3*
