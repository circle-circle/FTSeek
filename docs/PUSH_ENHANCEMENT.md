# FTSeek 推送格式增强文档

**版本**: v3.0  
**更新日期**: 2026-03-01  
**作者**: FTSeek Team

---

## 📋 概述

本次更新增强了 FTSeek 的推送功能，支持三种推送类型：

1. **增强版信号推送** - 在原有信号推送基础上增加新闻链接和 AI 分析
2. **重大新闻独立推送** - 实时推送重大财经新闻
3. **新闻汇总推送** - 每小时汇总所有相关新闻

---

## 🎯 功能特性

### 1. 增强版信号推送

**特点**:
- 📈 显示股票名称和代码
- 💰 显示触发价格
- 📊 显示置信度评分
- 📰 显示相关新闻 (最多 3 条)
- 🤖 显示 AI 分析结果
- 🔗 新闻链接可点击
- ✅ 保留原有确认按钮

**示例**:
```
🔔 FTSeek 买入信号

📈 腾讯控股 (HK.00700)
💰 价格：HK$380.50
📊 置信度：+1.25 (阈值 ±0.8)

📰 相关新闻 (2 条)
1️⃣ 腾讯 Q4 财报超预期
   🟢 影响：利好
   🔗 [链接]

2️⃣ 腾讯游戏业务强劲增长
   🟢 影响：利好
   🔗 [链接]

🤖 AI 分析
综合评估：财报超预期，游戏业务强劲
建议：买入

[✅ 确认买入] [⏰ 延期 15 分钟] [❌ 忽略]
```

**使用方法**:
```python
from app.notifier.dingtalk import DingTalkNotifier

notifier = DingTalkNotifier(webhook_url, secret)

# 发送增强版信号
notifier.send_enhanced_signal(
    signal=signal,
    related_news=[news1, news2],
    analysis_summary={
        'sentiment': 'bullish',
        'suggestion': 'buy',
        'reason': '财报超预期，建议买入'
    }
)
```

---

### 2. 重大新闻独立推送

**特点**:
- 🚨 实时推送重大财经新闻
- 🟢/🔴 显示情绪评分 (利好/利空)
- 📊 显示影响程度 (1-10)
- 🤖 AI 分析建议
- 🔗 原文链接可点击
- @所有人 提醒

**重大新闻判断标准**:
- 情绪评分绝对值 >= 0.6
- 标题包含重大事件关键词 (财报、收购、监管等)

**示例**:
```
🚨 重大新闻

央行宣布降准 0.5 个百分点

---

🟢 影响：利好
📊 情绪评分：+0.85
🕒 时间：2024-03-01T14:00:00
📰 来源：新浪财经

---

🤖 AI 分析
综合评估：央行降准释放流动性，对股市重大利好
建议：买入
影响程度：9/10

---

🔗 [阅读原文]

[@所有人]
```

**使用方法**:
```python
from app.notifier.dingtalk import DingTalkNotifier

notifier = DingTalkNotifier(webhook_url, secret)

# 发送重大新闻
notifier.send_major_news(
    news_item=news,
    analysis={
        'sentiment': 'bullish',
        'impact_score': 9,
        'suggestion': 'buy',
        'reason': '央行降准释放流动性'
    }
)
```

---

### 3. 新闻汇总推送

**特点**:
- 📰 每小时自动汇总
- 🔴 重大新闻优先展示
- 📈 按股票分组展示
- 🔗 所有链接可点击
- 📊 显示情绪评分

**推送时间**:
- 交易日：10:00-15:00 每小时整点
- 周末和节假日：不推送

**示例**:
```
📰 FTSeek 新闻汇总

时段：14:00-15:00
更新时间：2024-03-01 15:00:00

---

新闻总数：15 条

🔴 重大新闻 (2 条)
---
1. 腾讯 Q4 财报超预期
   🟢 情绪：+0.75
   🔗 [查看详情]

2. 美联储维持利率不变
   🔴 情绪：-0.30
   🔗 [查看详情]

---

📈 个股新闻

HK.00700 (1 条)
- 🟢 腾讯游戏业务增长 [🔗]

HK.09988 (1 条)
- 🟢 阿里云发布新产品 [🔗]

---
数据源：新浪财经 | 每小时更新
```

**使用方法**:
```python
from app.notifier.dingtalk import DingTalkNotifier
from app.reporters.news_digest import get_news_digest_reporter

# 获取报告器
reporter = get_news_digest_reporter()

# 获取并分类新闻
digest = reporter.fetch_and_classify(['HK.00700', 'HK.09988'])

# 发送汇总
notifier = DingTalkNotifier(webhook_url, secret)
notifier.send_news_digest(digest.to_dict())
```

---

## 🛠️ 技术实现

### 文件结构

```
app/
├── notifier/
│   └── dingtalk.py          # 钉钉推送 (扩展)
├── reporters/
│   └── news_digest.py       # 新闻汇总报告器 (新建)
└── data/
    └── news_fetcher.py      # 新闻抓取 (已有)

scripts/
├── news_digest.py           # 定时推送脚本 (新建)
└── demo_push_formats.py     # 演示脚本 (新建)

tests/
└── test_push_enhancement.py # 单元测试 (新建)
```

### 核心类

#### DingTalkNotifier (扩展)

新增方法:
- `send_enhanced_signal()` - 增强版信号推送
- `send_major_news()` - 重大新闻推送
- `send_news_digest()` - 新闻汇总推送

#### NewsDigestReporter (新建)

方法:
- `fetch_and_classify()` - 获取并分类新闻
- `format_digest()` - 格式化汇总
- `format_major_news()` - 格式化重大新闻
- `format_signal_with_news()` - 格式化增强信号
- `_quick_sentiment_analysis()` - 快速情绪分析
- `_is_major_news()` - 判断重大新闻

---

## ⚙️ 配置

### 环境变量

```bash
# 钉钉机器人配置
export DINGTALK_WEBHOOK_URL="https://oapi.dingtalk.com/robot/send?access_token=xxx"
export DINGTALK_SECRET="SECxxx"

# FTSeek 配置
export API_TOKEN="your_api_token"
export CALLBACK_EXTERNAL_URL="https://your-domain.com"
```

### 定时任务配置

编辑 crontab:
```bash
crontab -e
```

添加以下行 (交易日每小时推送):
```bash
# FTSeek 每小时新闻汇总 (交易日 10-15 点)
0 10-15 * * 1-5 cd /opt/ftseek/FTSeek && python3 scripts/news_digest.py
```

---

## 🧪 测试

### 运行单元测试

```bash
cd /opt/ftseek/FTSeek

# 设置环境变量
export API_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
export DINGTALK_WEBHOOK_URL="https://oapi.dingtalk.com/robot/send?access_token=test"
export DINGTALK_SECRET="test"
export CALLBACK_EXTERNAL_URL="http://localhost:8000"

# 运行测试
python3 -m unittest tests.test_push_enhancement -v
```

### 测试覆盖

- ✅ 增强版信号推送格式
- ✅ 重大新闻推送格式
- ✅ 新闻汇总推送格式
- ✅ Markdown 链接格式
- ✅ URL 可识别性
- ✅ 新闻获取和分类
- ✅ 汇总格式化
- ✅ 重大新闻判断
- ✅ 快速情绪分析

**测试结果**: 10/10 通过

---

## 📊 验收标准

| 标准 | 状态 | 说明 |
|------|------|------|
| 推送格式美观 | ✅ | 使用 emoji 和 Markdown 格式化 |
| 链接可点击 | ✅ | 使用 Markdown 链接语法 `[文本](URL)` |
| 测试通过 | ✅ | 10 个单元测试全部通过 |

---

## 🔍 使用示例

### 完整流程示例

```python
from app.strategy.base import Signal, SignalAction
from app.data.news_fetcher import get_news_fetcher
from app.analyzers.news_analyzer import get_news_analyzer
from app.notifier.dingtalk import DingTalkNotifier

# 1. 创建信号
signal = Signal(
    code="HK.00700",
    name="腾讯控股",
    action=SignalAction.BUY,
    price=380.50,
    quantity=100,
    reason="技术面突破",
    strategy="fusion_v2",
    extra={"consensus": True, "score": 1.25, "threshold": 0.8}
)

# 2. 获取相关新闻
fetcher = get_news_fetcher()
news_list = fetcher.fetch_news("HK.00700", sources=['sina'])

# 3. 分析新闻
analyzer = get_news_analyzer()
analyses = analyzer.analyze_with_llm(news_list[:3], "HK.00700")

# 4. 生成分析汇总
analysis_summary = {
    'sentiment': 'bullish',
    'suggestion': 'buy',
    'reason': '财报超预期，游戏业务强劲'
}

# 5. 发送增强信号
notifier = DingTalkNotifier(webhook_url, secret)
notifier.send_enhanced_signal(signal, news_list[:3], analysis_summary)
```

---

## 📝 注意事项

1. **限流**: 钉钉机器人限制 20 条/分钟，超限消息会自动合并摘要
2. **链接**: 钉钉支持 Markdown 链接，但在某些客户端可能需要点击才能跳转
3. **@所有人**: 重大新闻推送会@所有人，请谨慎使用
4. **定时任务**: 确保服务器时区正确 (Asia/Shanghai)

---

## 🚀 下一步

- [ ] 支持更多新闻源 (腾讯财经、东方财富等)
- [ ] 优化 AI 分析准确度
- [ ] 添加新闻去重功能
- [ ] 支持自定义推送时间
- [ ] 添加推送历史记录

---

**文档结束**
