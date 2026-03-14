# FTSeek v3.0 Lite 重构 - 实施计划

**设计文档**: `docs/plans/2026-03-01-ftseek-v3-design.md`  
**创建日期**: 2026-03-01  
**预计时间**: 2-3 天  
**执行模式**: 子代理驱动 (TDD + 双重审查)

---

## 📋 任务列表

### Task 1: 备份现有系统

**目标**: 完整备份 v2.0 代码和数据库，确保可回滚

**操作**:
```bash
# 备份代码
cp -r /opt/ftseek/FTSeek /opt/ftseek/FTSeek.v2.backup

# 备份数据库
cp /opt/ftseek/FTSeek/data/ftseek.db /opt/ftseek/FTSeek.v2.backup.db

# 导出当前配置
cp /opt/ftseek/FTSeek/config/*.yaml /opt/ftseek/FTSeek.v2.backup.config/

# 验证备份
ls -lh /opt/ftseek/FTSeek.v2.backup*
```

**验收**:
- [ ] 代码备份完整
- [ ] 数据库备份完整
- [ ] 配置文件备份完整
- [ ] 备份验证通过

---

### Task 2: 创建新架构骨架

**目标**: 创建 v3.0 Lite 目录结构

**新目录结构**:
```
/opt/ftseek/FTSeek/
├── app/
│   ├── core/           # 新建
│   │   ├── market_data.py
│   │   ├── signal_engine.py
│   │   └── trade_executor.py
│   ├── strategies/     # 保留 + 简化
│   │   ├── fusion_v2.py
│   │   ├── ma_cross.py
│   │   ├── rsi_rebound.py
│   │   └── config.yaml
│   ├── analyzers/      # 新建
│   │   ├── news_analyzer.py
│   │   └── sentiment.py
│   ├── reporters/      # 新建
│   │   ├── daily_stats.py
│   │   ├── weekly_report.py
│   │   └── monthly_report.py
│   ├── risk/           # 新建 (风控增强)
│   │   ├── risk_manager.py
│   │   └── risk_monitor.py
│   ├── notifier/       # 保留 + 增强
│   │   └── dingtalk.py
│   └── storage/        # 保留 + 简化
│       └── database.py
├── config/
│   ├── system.yaml     # 新建
│   ├── risk_config.yaml # 新建
│   └── watchlist.json  # 保留
├── main.py             # 简化
├── requirements.txt    # 精简
└── scripts/
    ├── daily_stats.py
    ├── weekly_report.py
    └── monthly_report.py
```

**验收**:
- [ ] 目录结构创建完成
- [ ] 删除废弃模块 (openclaw_bridge, backtest, server 等)
- [ ] 保留核心文件

---

### Task 3: 创建风控配置 (risk_config.yaml)

**目标**: 创建风控配置文件

**内容**:
```yaml
# config/risk_config.yaml

# 交易模式
trading:
  mode: "paper"             # paper=模拟，real=实盘
  paper_initial_cash: 1000000  # 100 万港币

# 五层风控阈值
risk_limits:
  # 信号级
  min_confidence: 0.60
  
  # 订单级
  max_single_position: 0.20
  max_single_loss: 0.10
  
  # 仓位级
  max_total_position: 0.80
  max_daily_trades: 10
  max_daily_loss: 0.05
  max_weekly_loss: 0.10
  
  # 账户级
  max_drawdown: 0.15
  consecutive_loss_limit: 5
  
  # 策略级
  strategy_min_trades: 20
  strategy_min_winrate: 0.40

# 预警配置
alerts:
  dingtalk_webhook: "${DINGTALK_WEBHOOK_URL}"
  alert_contacts:
    - name: "管理员"
      phone: "${ADMIN_PHONE}"
```

**验收**:
- [ ] 配置文件语法正确
- [ ] 环境变量占位符正确
- [ ] 所有阈值可配置

---

### Task 4: 实现风控管理器 (RiskManager)

**目标**: 实现五层风控检查

**功能**:
- `check_signal()` - 信号级检查 (置信度)
- `check_order()` - 订单级检查 (仓位/止损)
- `check_position()` - 仓位级检查 (总仓位/交易次数)
- `check_account()` - 账户级检查 (亏损/回撤)
- `check_system()` - 系统级检查 (连续亏损/策略失效)
- `send_alert()` - 发送预警到钉钉
- `liquidate_all()` - 紧急清仓

**测试**:
```python
# 测试信号级检查
assert risk_manager.check_signal(low_confidence_signal) == False

# 测试订单级检查
assert risk_manager.check_order(over_size_order).approved == False

# 测试账户级检查
assert risk_manager.check_account(loss_account).can_trade == False
```

**验收**:
- [ ] 五层检查全部实现
- [ ] 单元测试通过
- [ ] 预警推送正常

---

### Task 5: 实现大模型新闻分析器

**目标**: 使用 Bailian/Qwen 分析新闻影响

**功能**:
- `fetch_news()` - 从新浪财经抓取新闻
- `analyze_with_llm()` - 调用 Bailian API 分析
- `parse_response()` - 解析大模型返回
- `get_sentiment_score()` - 计算情绪评分
- `get_trade_suggestion()` - 生成买卖建议

**Prompt 模板**:
```
分析以下新闻对股票 {code} 的影响:

新闻标题：{title}
新闻内容：{content}

请分析:
1. 利好还是利空？(bullish/bearish/neutral)
2. 影响程度 (1-10)
3. 建议操作 (buy/sell/hold)
4. 理由 (50 字内)

返回 JSON 格式:
{
  "sentiment": "bullish/bearish/neutral",
  "impact_score": 8,
  "suggestion": "buy/sell/hold",
  "reason": "..."
}
```

**降级方案**:
- Bailian API 失败 → 使用关键词分析
- 超时 (>5s) → 返回中性

**验收**:
- [ ] 新闻抓取正常
- [ ] 大模型分析正常
- [ ] 降级方案有效
- [ ] 响应时间<5s

---

### Task 6: 实现三策略引擎

**目标**: 实现 3 个策略的信号生成

**策略 1: 融合 v2** (保留现有代码，简化)
- 技术指标 (RSI/MACD/布林带)
- 情绪评分 (来自 Task 5)
- 融合公式：`score = technical × 0.6 + sentiment × 0.4`

**策略 2: 均线交叉** (新增)
- MA5 上穿 MA20 → 买入
- MA5 下穿 MA20 → 卖出
- 成交量过滤 (>1.5 倍均量)

**策略 3: RSI 反弹** (新增)
- RSI<25 → 买入
- RSI>75 → 卖出
- 持仓时间≤3 天

**统一接口**:
```python
class BaseStrategy:
    def generate_signal(self, market_data: MarketData) -> Optional[Signal]:
        pass
    
    def get_config(self) -> Dict:
        pass
```

**验收**:
- [ ] 3 个策略全部实现
- [ ] 统一接口
- [ ] 配置可调整 (YAML)
- [ ] 单元测试通过

---

### Task 7: 实现钉钉推送增强

**目标**: 增强钉钉推送，支持三按钮确认

**消息类型**:
1. **买卖信号** (带三按钮)
2. **确认提醒** (15 分钟未响应)
3. **执行结果** (确认/拒绝)
4. **风险预警** (五层风控触发)
5. **收盘统计** (16:30)
6. **周报/月报**

**三按钮实现**:
```python
def send_signal_message(self, signal: Signal):
    message = self.format_signal(signal)
    buttons = [
        {"title": "✅ 确认执行", "action": "confirm"},
        {"title": "⏰ 延期", "action": "defer"},
        {"title": "❌ 忽略", "action": "ignore"}
    ]
    msg_id = self.dingtalk.send_markdown(
        title=f"FTSeek {signal.action}信号",
        text=message,
        buttons=buttons
    )
    # 保存 msg_id 用于后续更新
    save_signal_msg_id(signal.id, msg_id)
```

**验收**:
- [ ] 买卖信号推送正常
- [ ] 三按钮可点击
- [ ] 用户回调处理正确
- [ ] 风险预警推送正常

---

### Task 8: 实现交易执行器 (人工确认模式)

**目标**: 实现带人工确认的交易执行

**流程**:
```
信号生成 → 风控检查 → 推送钉钉 → 等待确认 → 执行/跳过
                                    ↓
                            15 分钟超时 → 标记过期
```

**功能**:
- `submit_signal()` - 提交信号到钉钉
- `handle_confirm()` - 处理用户确认回调
- `handle_defer()` - 处理延期
- `handle_ignore()` - 处理忽略
- `execute_order()` - 执行订单 (富途 API)
- `record_trade()` - 记录到数据库

**状态机**:
```
pending → confirmed → executed
        → ignored
        → expired (15 分钟超时)
        → rejected (风控拒绝)
```

**验收**:
- [ ] 状态流转正确
- [ ] 超时处理正常
- [ ] 富途模拟盘下单正常
- [ ] 数据库记录完整

---

### Task 9: 实现收盘统计推送

**目标**: 每日 16:30 自动推送收盘统计

**统计内容**:
- 今日信号数 / 执行数 / 忽略数
- 今日盈亏 (已平仓)
- 当前持仓
- 本周累计
- 风控状态

**推送格式**:
```
📊 FTSeek 收盘统计 (2026-03-01)

今日信号：8 次
已执行：5 次
忽略/延期：3 次

今日盈亏：+HK$12,450 (+1.23%) 🟢
本周盈亏：+HK$45,600 (+4.56%)

当前持仓：3 只
- HK.00700 腾讯控股 +2.3%
- HK.09988 阿里巴巴 +1.8%
- HK.09992 泡泡玛特 -0.5%

风控状态：✅ 正常
连续亏损：0 次
总回撤：-2.1% (安全线 15%)
```

**定时任务**:
```bash
# Crontab
30 16 * * 1-5 cd /opt/ftseek/FTSeek && python3 scripts/daily_stats.py
```

**验收**:
- [ ] 统计数据准确
- [ ] 定时推送正常
- [ ] 格式美观
- [ ] 无数据时不报错

---

### Task 10: 实现周报/月报生成器

**目标**: 生成周度和月度绩效报告

**周报内容** (周一 09:00):
- 本周交易汇总
- 胜率 / 盈亏比
- 最佳/最差交易
- 各策略表现
- 下周展望

**月报内容** (每月 1 日 09:00):
- 月度交易汇总
- 收益率曲线
- 最大回撤
- 夏普比率/索提诺比率
- 策略归因分析
- 下月计划

**推送格式**:
```
📈 FTSeek 周报 (2026-W09)

本周交易：25 次
胜率：64% (16/25) 🎯
盈亏比：2.1

收益：+HK$85,300 (+8.53%)
最大回撤：-3.2%

策略表现:
- 融合 v2: 68% 胜率 (15 次) ⭐
- 均线交叉：60% 胜率 (7 次)
- RSI 反弹：50% 胜率 (3 次)

最佳交易：HK.00700 +12.5%
最差交易：HK.09992 -4.2%

下周展望：关注财报季，谨慎交易
```

**验收**:
- [ ] 周报/月报生成正常
- [ ] 统计数据准确
- [ ] 定时推送正常
- [ ] 格式美观

---

### Task 11: 实现风控实时监控

**目标**: 每 2 小时推送风险监控面板

**监控内容**:
- 账户总值 / 可用现金 / 持仓市值
- 今日/本周/总回撤
- 持仓数量 / 今日交易次数
- 连续亏损次数
- 风控状态

**推送频率**: 交易日每 2 小时 (10:00/12:00/14:00/16:00)

**推送格式**:
```
📊 FTSeek 风险监控 (14:00)

账户总值：HK$1,025,340 (+2.53%)
可用现金：HK$512,670 (50%)
持仓市值：HK$512,670 (50%)

今日盈亏：+HK$8,450 (+0.83%) 🟢
本周盈亏：+HK$32,100 (+3.21%) 🟢
总回撤：-2.1% (安全线 15%)

持仓数量：3 只
今日交易：4/10 次
连续亏损：0 次

风控状态：✅ 正常
```

**验收**:
- [ ] 定时推送正常
- [ ] 数据实时准确
- [ ] 预警触发正常

---

### Task 12: 最终测试 + 文档

**目标**: 端到端测试 + 更新文档

**测试场景**:
```bash
# 1. 信号生成测试
python3 scripts/test_signal_generation.py

# 2. 风控检查测试
python3 scripts/test_risk_manager.py

# 3. 钉钉推送测试
python3 scripts/test_dingtalk.py

# 4. 模拟交易测试
python3 scripts/test_paper_trading.py

# 5. 收盘统计测试
python3 scripts/test_daily_stats.py

# 6. 周报/月报测试
python3 scripts/test_reports.py
```

**文档更新**:
- `docs/UPGRADE_TO_V3.md` - 升级指南
- `docs/RISK_CONFIG.md` - 风控配置说明
- `docs/STRATEGY_CONFIG.md` - 策略配置说明
- `QUICKSTART_V3.md` - 快速启动指南

**验收**:
- [ ] 所有测试通过
- [ ] 文档完整
- [ ] 代码提交 git
- [ ] 可部署

---

## 📅 执行计划

| 阶段 | 任务 | 预计时间 | 说明 |
|------|------|----------|------|
| **Phase 1** | Task 1-3 | 2h | 备份 + 骨架 + 配置 |
| **Phase 2** | Task 4-6 | 4h | 风控 + 新闻分析 + 策略 |
| **Phase 3** | Task 7-9 | 4h | 钉钉 + 执行 + 收盘统计 |
| **Phase 4** | Task 10-12 | 3h | 报告 + 监控 + 测试 |
| **总计** | 12 任务 | ~13h | 2-3 天 |

---

## 🎯 执行模式选择

### 选项 A: 子代理驱动 (推荐) ⭐
- 每个任务由独立子代理实现
- 每个任务完成后经过 spec review + code quality review
- TDD 强制：先写测试，再实现
- 你只需要等待完成通知

### 选项 B: 手动执行
- 你自己按任务列表逐个实现
- AI 提供指导和代码审查

---

**准备就绪。请选择执行模式。** (A 或 B)
