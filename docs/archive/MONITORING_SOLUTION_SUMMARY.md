# FTSeek-US 美股行情监控解决方案总结

**创建日期**: 2026-03-24 20:00  
**状态**: ⚠️ 需要网络配置

---

## 一、需求说明

**用户需求**: 当前美股的行情数据查询获取，如何通过富途的接口获取价格，来实现定期的查询并给出止损和止盈。

---

## 二、测试结果

### 1. 富途 OpenD 测试
```bash
python3.8 scripts/test_us_monitor.py
```

**结果**:
```
❌ US.AAPL: 获取失败 - 无权限获取 US.AAPL 的行情
❌ US.MSFT: 获取失败 - 无权限获取 US.MSFT 的行情
❌ US.NVDA: 获取失败 - 无权限获取 US.NVDA 的行情
```

**原因**: 富途账户未开通美股实时行情权限。

### 2. Yahoo Finance 测试
```bash
python3.8 -c "import yfinance as yf; print(yf.Ticker('AAPL').fast_info.last_price)"
```

**结果**:
```
KeyError: 'currentTradingPeriod'
```

**原因**: 可能是网络问题或 Yahoo Finance API 限制。

---

## 三、解决方案

### 方案 1: 开通富途美股行情权限 ⭐⭐⭐⭐⭐

**步骤**:
1. 登录富途牛牛 APP
2. 进入"我的" → "行情权限"
3. 开通"美股实时行情"
4. 重启 OpenD: `systemctl restart futu_opend`

**优势**:
- ✅ 实时行情
- ✅ 数据准确
- ✅ 与 FTSeek 集成良好

**劣势**:
- ⚠️ 可能需要付费
- ⚠️ 需要开通权限

### 方案 2: Yahoo Finance (备用) ⭐⭐⭐

**步骤**:
```bash
pip install yfinance
python3.8 scripts/us_stock_monitor.py
```

**优势**:
- ✅ 免费
- ✅ 无需权限

**劣势**:
- ⚠️ 15 分钟延迟
- ⚠️ 可能受网络影响

### 方案 3: Alpha Vantage ⭐⭐⭐⭐

**步骤**:
1. 注册 https://www.alphavantage.co/support/
2. 获取免费 API Key
3. 配置 `.env.us`:
```bash
DATA_SOURCE=alpha_vantage
ALPHA_VANTAGE_API_KEY=your_key
```

**优势**:
- ✅ 免费 (500 次/天)
- ✅ 实时数据
- ✅ API 稳定

**劣势**:
- ⚠️ 需要注册
- ⚠️ 有调用限制

### 方案 4: Finnhub ⭐⭐⭐⭐

**步骤**:
1. 注册 https://finnhub.io/
2. 获取免费 API Key
3. 配置 `.env.us`:
```bash
DATA_SOURCE=finnhub
FINNHUB_API_KEY=your_key
```

**优势**:
- ✅ 免费 (60 次/分钟)
- ✅ 实时数据
- ✅ 文档完善

**劣势**:
- ⚠️ 需要注册
- ⚠️ 有调用限制

---

## 四、已创建文件

| 文件 | 说明 | 状态 |
|------|------|------|
| `scripts/us_stock_monitor.py` | 监控脚本 (Yahoo Finance) | ✅ 已创建 |
| `scripts/test_us_monitor.py` | 测试脚本 | ✅ 已创建 |
| `docs/US_STOCK_MONITOR_GUIDE.md` | 使用指南 | ✅ 已创建 |
| `docs/US_STOCK_MONITOR_SUMMARY.md` | 解决方案总结 | ✅ 已创建 |
| `docs/MONITORING_SOLUTION_SUMMARY.md` | 本文档 | ✅ 已创建 |

---

## 五、推荐方案

### 短期方案 (今天可用)
**使用现有 FTSeek 港股监控逻辑**

FTSeek 已有成熟的港股监控逻辑，可以借鉴:
- 文件：`/opt/ftseek/FTSeek/app/data_subscriber.py`
- 功能：富途行情订阅 + 止损止盈计算 + 钉钉推送

**修改为美股**:
1. 修改代码中的港股代码为美股代码
2. 调整交易时间判断 (美东时间 vs 北京时间)
3. 需要开通富途美股行情权限

### 中期方案 (1-2 天)
**开通富途美股行情权限**

这是最稳定可靠的方案:
1. 开通权限
2. 使用现有的 FTSeek 监控逻辑
3. 调整参数适配美股

### 长期方案 (1 周)
**集成多个数据源**

实现数据源自动 fallback:
```
富途 OpenD (实时) → Alpha Vantage (实时) → Yahoo Finance (15 分钟延迟)
```

---

## 六、止损止盈实现逻辑

### 核心代码
```python
# 计算止损止盈价格
def calculate_levels(cost_price):
    stop_loss = cost_price * (1 - 0.10)        # -10%
    take_profit_1 = cost_price * (1 + 0.15)    # +15%
    take_profit_2 = cost_price * (1 + 0.25)    # +25%
    take_profit_3 = cost_price * (1 + 0.40)    # +40%
    return stop_loss, take_profit_1, take_profit_2, take_profit_3

# 检查是否触发
if current_price <= stop_loss:
    send_alert("STOP_LOSS", "CRITICAL")
elif current_price >= take_profit_3:
    send_alert("TAKE_PROFIT_3", "INFO")
elif current_price >= take_profit_2:
    send_alert("TAKE_PROFIT_2", "INFO")
elif current_price >= take_profit_1:
    send_alert("TAKE_PROFIT_1", "INFO")
```

### 钉钉推送
```python
from app.observability.alert_manager import AlertManager

manager = AlertManager(
    provider="dingtalk",
    dingtalk_webhook=webhook,
    dingtalk_secret=secret,
)

manager.send_info(
    title="⚠️ STOP_LOSS - AAPL",
    content="AAPL 触发止损！当前价 $152.00, 止损价 $153.00"
)
```

---

## 七、下一步行动

### 立即行动 (今天)
1. **确认富途账户是否有美股行情权限**
   - 登录富途牛牛 APP 查看
   - 如果没有，考虑开通

2. **测试网络连接**
   ```bash
   # 测试 Yahoo Finance
   ping finance.yahoo.com
   
   # 测试 Alpha Vantage
   curl https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=AAPL&apikey=demo
   ```

### 明天行动
1. **开通富途美股行情权限** (如果决定使用富途)
2. **注册 Alpha Vantage 或 Finnhub** (作为备用)
3. **修改监控脚本** (根据选择的数据源)

### 本周行动
1. **部署定时任务** (systemd 或 cron)
2. **配置持仓和止损止盈**
3. **测试告警推送**
4. **监控运行日志**

---

## 八、文档索引

| 文档 | 路径 |
|------|------|
| 监控解决方案总结 | `docs/MONITORING_SOLUTION_SUMMARY.md` |
| 监控指南 | `docs/US_STOCK_MONITOR_GUIDE.md` |
| 监控总结 | `docs/US_STOCK_MONITOR_SUMMARY.md` |
| LLM API 修复报告 | `docs/LLM_API_FIX_REPORT_20260324.md` |

---

## 九、总结

### 当前状态
| 功能 | 状态 | 说明 |
|------|------|------|
| 富途行情获取 | ❌ | 无权限 |
| Yahoo Finance | ⚠️ | 网络问题 |
| 止损止盈计算 | ✅ | 逻辑已实现 |
| 钉钉告警推送 | ✅ | 已测试通过 |
| 监控脚本 | ✅ | 已创建 |

### 核心问题
1. **富途无美股行情权限** - 需要开通
2. **Yahoo Finance 网络问题** - 需要检查网络或代理

### 推荐方案
**开通富途美股行情权限** → 使用现有 FTSeek 监控逻辑 → 最稳定可靠

### 备用方案
**注册 Alpha Vantage/Finnhub** → 修改监控脚本 → 免费但有限制

---

**报告生成时间**: 2026-03-24 20:00  
**创建人**: AI Assistant  
**审核状态**: ⚠️ 需要用户决策
