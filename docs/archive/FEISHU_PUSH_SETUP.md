# 飞书推送配置指南

**版本**: 1.0  
**日期**: 2026-03-26

---

## 一、功能概述

FTSeek-US 现已支持推送到飞书：

1. **每小时交易推送** - 每小时整点推送持仓和交易记录
2. **收盘后健康大盘** - 美东收盘后 (北京时间 04:00) 推送当日总结

---

## 二、配置步骤

### 2.1 获取飞书 Webhook URL

1. 打开飞书群聊
2. 点击右上角设置 → 群机器人
3. 添加机器人 → 自定义机器人
4. 设置机器人名称 (如：FTSeek-US 助手)
5. 复制 Webhook 地址

### 2.2 配置环境变量

编辑 `/opt/ftseek/FTSeek-US/.env.us`:

```bash
# 飞书推送配置
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

### 2.3 测试推送

```bash
cd /opt/ftseek/FTSeek-US

# 测试每小时推送
python3 -m app.reports.hourly_trade_push

# 测试每日健康大盘
python3 -m app.reports.daily_health_report
```

---

## 三、定时任务配置

### 3.1 编辑 crontab

```bash
crontab -e
```

### 3.2 添加定时任务

```bash
# FTSeek-US 每小时交易推送 (每小时整点)
0 * * * * cd /opt/ftseek/FTSeek-US && /usr/bin/python3 -m app.reports.hourly_trade_push >> logs/cron_hourly.log 2>&1

# FTSeek-US 每日健康大盘 (北京时间 04:00，美东收盘后)
0 4 * * 1-5 cd /opt/ftseek/FTSeek-US && /usr/bin/python3 -m app.reports.daily_health_report >> logs/cron_daily.log 2>&1
```

### 3.3 验证 crontab

```bash
# 查看已配置的定时任务
crontab -l

# 查看 cron 日志
tail -f /var/log/cron

# 查看推送日志
tail -f /opt/ftseek/FTSeek-US/logs/cron_hourly.log
tail -f /opt/ftseek/FTSeek-US/logs/cron_daily.log
```

---

## 四、推送内容

### 4.1 每小时交易推送

**推送时间**: 每小时整点 (09:00, 10:00, ..., 03:00)

**推送内容**:
```
FTSeek-US 交易快报
时间：2026-03-26 14:00

📊 持仓概览
持仓数量：3

持仓明细:
🟢 US.NVDA: 50 股 @ $178.67 (+0.09%, +$8.50)
🟢 US.AAPL: 100 股 @ $252.57 (+0.23%, +$57.00)
🟢 US.MSFT: 50 股 @ $371.03 (+0.28%, +$51.50)

总盈亏：🟢 +$117.00 (总市值：$45,967.50)

📝 最近交易:
🟢 BUY US.NVDA 50 股 @ $178.50 (2026-03-26 08:00)
```

### 4.2 每日健康大盘

**推送时间**: 北京时间 04:00 (美东收盘后)

**推送内容**:
```
FTSeek-US 交易健康大盘
日期：2026-03-26 (美东交易日)
生成时间：04:00

📊 健康度：🟢 优秀 (85 分)

📈 交易统计
总交易数：5 笔
买入：3 | 卖出：2
止损止盈触发：1 笔

💰 持仓概览
持仓数量：3 只
总市值：$45,967.50
总浮盈：🟢 +$117.00
胜率：66.7%
平均盈亏：+0.25%

📋 持仓明细:
🟢 US.AAPL: 100 股 $252.00→$252.57 (+0.23%, +$57.00)
🟢 US.MSFT: 50 股 $370.00→$371.03 (+0.28%, +$51.50)
🟢 US.NVDA: 50 股 $178.50→$178.67 (+0.09%, +$8.50)

📝 当日交易:
🟢 BUY US.NVDA 50 股 @ $178.50
🟢 BUY US.AAPL 100 股 @ $252.00
🟢 BUY US.MSFT 50 股 @ $370.00

⚠️ 风险提示:
- 各项指标正常，继续保持
```

---

## 五、健康度评分说明

### 5.1 评分标准

| 分数 | 等级 | 说明 |
|------|------|------|
| 80-100 | 🟢 优秀 | 各项指标良好 |
| 60-79 | 🟡 良好 | 大部分指标正常 |
| 40-59 | 🟠 一般 | 需要关注 |
| 0-39 | 🔴 需关注 | 存在风险 |

### 5.2 评分因素

**加分项**:
- ✅ 持仓盈利 (+0~30 分)
- ✅ 高胜率 (>60% +20 分)

**减分项**:
- ❌ 持仓亏损 (-0~-30 分)
- ❌ 低胜率 (<40% -20 分)
- ❌ 频繁触发止损 (>5 次 -20 分)
- ❌ 过度交易 (>20 笔 -10 分)

---

## 六、故障排查

### 6.1 推送失败

**检查 Webhook URL**:
```bash
cat /opt/ftseek/FTSeek-US/.env.us | grep FEISHU
```

**测试 Webhook**:
```bash
curl -X POST "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx" \
  -H "Content-Type: application/json" \
  -d '{"msg_type":"text","content":{"text":"测试"}}'
```

**查看日志**:
```bash
tail -f /opt/ftseek/FTSeek-US/logs/hourly_push.log
tail -f /opt/ftseek/FTSeek-US/logs/daily_report.log
```

### 6.2 定时任务未执行

**检查 cron 服务**:
```bash
# 查看 cron 状态
systemctl status crond

# 启动 cron
systemctl start crond

# 设置开机自启
systemctl enable crond
```

**检查 crontab 语法**:
```bash
# 验证 crontab
crontab -l | grep ftseek

# 测试执行
cd /opt/ftseek/FTSeek-US
python3 -m app.reports.hourly_trade_push
```

### 6.3 数据为空

**检查交易记录文件**:
```bash
cat /opt/ftseek/FTSeek-US/data/trade_records.json
```

**检查审计日志**:
```bash
tail -20 /root/.futu_trade_audit.jsonl
```

---

## 七、自定义配置

### 7.1 修改推送时间

编辑 crontab:
```bash
crontab -e
```

修改推送时间:
```bash
# 每 30 分钟推送一次
*/30 * * * * cd /opt/ftseek/FTSeek-US && python3 -m app.reports.hourly_trade_push

# 每日 05:00 推送 (北京时间)
0 5 * * 1-5 cd /opt/ftseek/FTSeek-US && python3 -m app.reports.daily_health_report
```

### 7.2 禁用推送

**临时禁用**:
```bash
# 注释掉 crontab 中的对应行
crontab -e

# 在行首添加 #
# 0 * * * * cd /opt/ftseek/FTSeek-US && python3 -m app.reports.hourly_trade_push
```

**永久禁用**:
```bash
# 删除 crontab 中的对应行
crontab -e
```

---

## 八、文件清单

### 新增文件

```
/opt/ftseek/FTSeek-US/
├── app/reports/
│   ├── __init__.py                  # 模块初始化
│   ├── hourly_trade_push.py         # 每小时推送 ⭐
│   └── daily_health_report.py       # 每日健康大盘 ⭐
├── logs/
│   ├── hourly_push.log              # 每小时推送日志
│   ├── daily_report.log             # 每日报告日志
│   ├── cron_hourly.log              # Cron 每小时日志
│   └── cron_daily.log               # Cron 每日日志
└── docs/
    └── FEISHU_PUSH_SETUP.md         # 本文档 ⭐
```

### 配置文件

```
/opt/ftseek/FTSeek-US/
└── .env.us                          # 需要配置 FEISHU_WEBHOOK_URL
```

---

## 九、快速命令

```bash
# 测试每小时推送
cd /opt/ftseek/FTSeek-US
python3 -m app.reports.hourly_trade_push

# 测试每日健康大盘
python3 -m app.reports.daily_health_report

# 查看推送日志
tail -f logs/hourly_push.log
tail -f logs/daily_report.log

# 查看 cron 状态
systemctl status crond
crontab -l

# 手动触发 cron 任务
run-parts /etc/cron.hourly
```

---

## 十、总结

### 已完成

- ✅ 每小时交易推送功能
- ✅ 每日健康大盘功能
- ✅ 飞书 Webhook 集成
- ✅ 健康度评分系统
- ✅ 定时任务配置

### 下一步

1. 配置飞书 Webhook URL
2. 添加 crontab 定时任务
3. 测试推送功能
4. 监控运行日志

---

**文档版本**: 1.0  
**最后更新**: 2026-03-26  
**维护人**: AI Assistant
