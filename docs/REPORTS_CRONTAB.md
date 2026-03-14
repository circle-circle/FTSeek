# FTSeek 报告定时任务配置

## Crontab 配置

将以下配置添加到 crontab (`crontab -e`):

```bash
# FTSeek 报告推送任务
# 环境变量
FTSEEK_HOME=/opt/ftseek/FTSeek
PYTHON=/usr/bin/python3

# 周报：每周一 09:00 推送
0 9 * * 1 cd $FTSEEK_HOME && $PYTHON scripts/weekly_report.py >> /var/log/ftseek/weekly_report.log 2>&1

# 月报：每月 1 日 09:00 推送
0 9 1 * * cd $FTSEEK_HOME && $PYTHON scripts/monthly_report.py >> /var/log/ftseek/monthly_report.log 2>&1
```

## 安装步骤

### 1. 创建日志目录

```bash
sudo mkdir -p /var/log/ftseek
sudo chown $(whoami):$(whoami) /var/log/ftseek
```

### 2. 编辑 Crontab

```bash
crontab -e
```

### 3. 添加配置

将上面的配置粘贴到 crontab 文件末尾，保存退出。

### 4. 验证配置

```bash
# 查看当前 crontab
crontab -l

# 查看 cron 服务状态
systemctl status cron
```

## 手动测试

### 测试周报

```bash
cd /opt/ftseek/FTSeek
export API_TOKEN="your_api_token"
export DINGTALK_WEBHOOK_URL="your_webhook_url"
export DINGTALK_SECRET="your_secret"
export CALLBACK_EXTERNAL_URL="http://localhost:8000"
python3 scripts/weekly_report.py
```

### 测试月报

```bash
cd /opt/ftseek/FTSeek
export API_TOKEN="your_api_token"
export DINGTALK_WEBHOOK_URL="your_webhook_url"
export DINGTALK_SECRET="your_secret"
export CALLBACK_EXTERNAL_URL="http://localhost:8000"
python3 scripts/monthly_report.py
```

## 日志查看

```bash
# 查看周报日志
tail -f /var/log/ftseek/weekly_report.log

# 查看月报日志
tail -f /var/log/ftseek/monthly_report.log

# 查看最近的报告推送记录
grep "推送成功" /var/log/ftseek/*.log
```

## 故障排查

### 问题：报告未推送

1. 检查 cron 服务是否运行：
   ```bash
   systemctl status cron
   ```

2. 检查 cron 日志：
   ```bash
   grep CRON /var/log/syslog | tail -20
   ```

3. 手动运行脚本测试：
   ```bash
   python3 scripts/weekly_report.py
   ```

### 问题：钉钉推送失败

1. 检查 Webhook URL 是否正确
2. 检查 Secret 配置是否正确
3. 检查网络连接：
   ```bash
   curl -I https://oapi.dingtalk.com
   ```

### 问题：统计数据为空

1. 检查数据库是否有交易记录：
   ```bash
   sqlite3 /opt/ftseek/FTSeek/data/ftseek.db "SELECT COUNT(*) FROM trades WHERE status='closed';"
   ```

2. 检查日期范围是否正确（周报统计上一周，月报统计上一月）

## 报告内容说明

### 周报 (周一 09:00)

- **本周交易汇总**: 交易次数统计
- **胜率 / 盈亏比**: 交易质量指标
- **最佳/最差交易**: 单笔交易表现
- **各策略表现**: 策略维度分析
- **下周展望**: 基于表现的智能建议

### 月报 (每月 1 日 09:00)

- **月度交易汇总**: 月度交易统计
- **收益率曲线**: 资金变化趋势
- **最大回撤**: 风险控制指标
- **夏普比率/索提诺比率**: 风险调整收益
- **策略归因分析**: 各策略贡献度
- **下月计划**: 基于表现的智能建议

## 环境变量说明

| 变量名 | 说明 | 示例 |
|--------|------|------|
| `API_TOKEN` | API 安全令牌 | `abc123...` |
| `DINGTALK_WEBHOOK_URL` | 钉钉机器人 Webhook | `https://oapi.dingtalk.com/...` |
| `DINGTALK_SECRET` | 钉钉加签密钥 | `SEC...` |
| `CALLBACK_EXTERNAL_URL` | 回调服务地址 | `http://localhost:8000` |

---

**文档更新日期**: 2026-03-01  
**版本**: v3.0
