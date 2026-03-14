# Task 11: 风控实时监控 - 实现文档

**状态**: ✅ 已完成  
**完成时间**: 2026-03-01  
**执行者**: 监控子代理 (task11-monitor)

---

## 📋 任务目标

实现每 2 小时推送风险监控面板，监控内容包括：
- 账户总值 / 可用现金 / 持仓市值
- 今日/本周/总回撤
- 持仓数量 / 今日交易次数
- 连续亏损次数
- 风控状态

**推送频率**: 交易日每 2 小时 (10:00/12:00/14:00/16:00)

---

## 📁 实现文件

### 1. 核心模块
- **`/opt/ftseek/FTSeek/app/risk/risk_monitor.py`** (9.9KB)
  - `RiskMonitor` 类：风险监控器主类
  - `RiskMonitorData` 数据类：监控数据结构
  - `collect_monitor_data()`: 收集监控数据
  - `push_monitor()`: 推送监控面板
  - `check_and_push()`: 检查时间并推送

### 2. 执行脚本
- **`/opt/ftseek/FTSeek/scripts/risk_monitor.py`** (6.7KB)
  - 支持 `--force` 强制推送
  - 支持 `--test` 测试模式
  - 支持 `--init-data` 初始化测试数据
  - 自动检查推送时间和交易日

### 3. 测试文件
- **`/opt/ftseek/FTSeek/scripts/test_risk_monitor.py`** (9.4KB)
  - 6 个单元测试覆盖所有功能
  - 测试数据结构、数据收集、风控判断、消息格式化、钉钉集成、时间检查

### 4. 配置文件
- **`/opt/ftseek/FTSeek/scripts/ftseek-risk-monitor.service`** (systemd 服务文件)
- **`/opt/ftseek/FTSeek/scripts/README_RISK_MONITOR.md`** (配置说明)
- **`/opt/ftseek/FTSeek/scripts/setup_risk_monitor.sh`** (安装配置脚本)

---

## 🎯 功能特性

### 1. 监控数据收集
```python
data = monitor.collect_monitor_data()
# 返回 RiskMonitorData 包含:
# - total_value: 账户总值
# - cash: 可用现金
# - position_value: 持仓市值
# - today_pnl/pct: 今日盈亏
# - week_pnl/pct: 本周盈亏
# - drawdown: 总回撤
# - position_count: 持仓数量
# - today_trades: 今日交易次数
# - consecutive_loss: 连续亏损次数
# - risk_ok: 风控状态
```

### 2. 风控状态判断
自动判断风控状态，触发条件：
- 回撤 > max_drawdown (15%)
- 今日交易次数 >= max_daily_trades (10)
- 连续亏损 >= consecutive_loss_limit (5)

### 3. 钉钉推送格式
```
📊 FTSeek 风险监控 (14:00)

账户总值：HK$1,025,340.00
可用现金：HK$512,670.00 (50%)
持仓市值：HK$512,670.00 (50%)

🟢 今日盈亏：HK$+8,450.00 (+0.83%)
🟢 本周盈亏：HK$+32,100.00 (+3.21%)
总回撤：2.10% (安全线 15%)

持仓数量：3 只
今日交易：4/10 次
连续亏损：0 次

风控状态：✅ 正常
```

### 4. 定时推送
- **推送时间**: 10:00, 12:00, 14:00, 16:00
- **推送窗口**: 整点后 0-5 分钟
- **推送日期**: 周一至周五 (交易日)
- **推送方式**: crontab 或 systemd timer

---

## 🧪 测试结果

```bash
$ cd /opt/ftseek/FTSeek && python3 scripts/test_risk_monitor.py

============================================================
FTSeek 风险监控模块测试套件
============================================================
通过：6/6
失败：0/6

🎉 所有测试通过!
```

### 测试覆盖
1. ✅ 数据结构测试 - 验证 RiskMonitorData 正确性
2. ✅ 数据收集测试 - 验证从 RiskManager 收集数据
3. ✅ 风控状态判断 - 验证 4 种风控场景
4. ✅ 消息格式化 - 验证钉钉推送格式
5. ✅ 钉钉推送集成 - 验证 send_risk_monitor 调用
6. ✅ 推送时间检查 - 验证时间窗口逻辑

---

## 🚀 部署指南

### 方式 1: 使用安装脚本 (推荐)
```bash
cd /opt/ftseek/FTSeek/scripts
./setup_risk_monitor.sh
```

### 方式 2: 手动配置 crontab
```bash
# 编辑 crontab
crontab -e

# 添加以下行
0 10,12,14,16 * * 1-5 cd /opt/ftseek/FTSeek && /usr/bin/python3 /opt/ftseek/FTSeek/scripts/risk_monitor.py >> /opt/ftseek/FTSeek/logs/risk_monitor.log 2>&1
```

### 方式 3: 使用 systemd timer
```bash
# 复制服务文件
sudo cp /opt/ftseek/FTSeek/scripts/ftseek-risk-monitor.service /etc/systemd/system/

# 创建 timer 文件
sudo nano /etc/systemd/system/ftseek-risk-monitor.timer
# 内容见 README_RISK_MONITOR.md

# 启用并启动
sudo systemctl daemon-reload
sudo systemctl enable ftseek-risk-monitor.timer
sudo systemctl start ftseek-risk-monitor.timer

# 查看状态
systemctl list-timers | grep ftseek
```

---

## 📖 使用示例

### 测试模式 (不实际推送)
```bash
cd /opt/ftseek/FTSeek
python3 scripts/risk_monitor.py --test
```

### 强制推送 (忽略时间检查)
```bash
cd /opt/ftseek/FTSeek
python3 scripts/risk_monitor.py --force
```

### 查看日志
```bash
tail -f /opt/ftseek/FTSeek/logs/risk_monitor.log
```

### 初始化测试数据
```bash
cd /opt/ftseek/FTSeek
python3 scripts/risk_monitor.py --init-data --force
```

---

## ⚙️ 配置说明

### 环境变量
```bash
export API_TOKEN="your_api_token"
export DINGTALK_WEBHOOK_URL="https://oapi.dingtalk.com/robot/send?access_token=xxx"
export DINGTALK_SECRET="your_secret"
```

### 风控配置文件
编辑 `/opt/ftseek/FTSeek/config/risk_config.yaml`:

```yaml
risk_limits:
  max_drawdown: 0.15          # 最大回撤 15%
  max_daily_trades: 10        # 每日最大交易 10 次
  consecutive_loss_limit: 5   # 连续亏损限制 5 次
  max_total_position: 0.80    # 最大总仓位 80%

schedule:
  risk_monitor_interval: 120  # 监控间隔 120 分钟
```

---

## ✅ 验收标准

- [x] **定时推送正常**: crontab 配置完成，交易日自动推送
- [x] **数据实时准确**: 从 RiskManager 实时获取账户和交易数据
- [x] **预警触发正常**: 风控状态异常时自动标记并推送

---

## 📝 后续优化建议

1. **添加持仓详情**: 在监控面板中显示具体持仓盈亏
2. **支持自定义推送时间**: 通过配置文件调整推送时间
3. **添加图表支持**: 钉钉卡片支持资金曲线图
4. **异常重试机制**: 推送失败时自动重试
5. **推送频率限制**: 避免短时间内重复推送

---

## 🔗 相关文件

- 计划文件：`/opt/ftseek/FTSeek/docs/plans/2026-03-01-ftseek-v3-refactor-plan.md`
- 风控管理器：`/opt/ftseek/FTSeek/app/risk/risk_manager.py`
- 钉钉推送：`/opt/ftseek/FTSeek/app/notifier/dingtalk.py`
- 配置文件：`/opt/ftseek/FTSeek/config/risk_config.yaml`

---

**Task 11 完成 ✅**
