# 金融新闻推送系统

基于 [Situation-aggregation-monitoring](https://github.com/MidaTechnology/Situation-aggregation-monitoring) 项目改造的金融新闻信息推送系统。

## 📋 功能特性

### 核心功能
- ✅ **多源新闻采集**: RSS/API/爬虫，支持财联社、BlockBeats、华尔街见闻、新浪财经等
- ✅ **规则引擎过滤**: 关键词匹配、来源优先级、分类开关、股票代码匹配
- ✅ **频率控制**: 每小时≤5 条、每日≤30 条、冷却时间 5 分钟、消息合并
- ✅ **Qwen 大模型分析**: 智能摘要、情绪分析、影响评估、操作建议
- ✅ **钉钉机器人推送**: Markdown 格式、来源标识、股票关联

### 数据源
| 类型 | 来源 | 优先级 |
|------|------|--------|
| 快讯 | 财联社电报、BlockBeats | ⭐⭐⭐ |
| 财经 | 华尔街见闻、新浪财经、CNBC | ⭐⭐ |
| 宏观 | 美联储、SEC 公告 | ⭐⭐ |
| 港美股 | 港交所披露易 | ⭐⭐ |

### 监控股票
默认监控股票 (可配置):
- 港股：腾讯 (00700)、阿里 (9988)、泡泡玛特 (9992)
- 美股：苹果 (AAPL)、特斯拉 (TSLA)、英伟达 (NVDA)、微软 (MSFT)

---

## 🚀 快速启动

### 1. 环境变量配置

```bash
# 钉钉推送 (必需)
export DINGTALK_WEBHOOK_URL="https://oapi.dingtalk.com/robot/send?access_token=XXX"
export DINGTALK_SECRET="SECXXX"

# Qwen 大模型 (可选，用于智能分析)
export QWEN_ENABLED="true"
export QWEN_API_KEY="sk-xxx"
export QWEN_MODEL="qwen-max"

# 监控股票 (可选)
export NEWS_WATCH_STOCKS='[
  {"code": "HK.00700", "name": "腾讯控股", "enabled": true},
  {"code": "US.AAPL", "name": "苹果", "enabled": true}
]'

# 推送频率 (可选)
export NEWS_MAX_PER_HOUR=5
export NEWS_MAX_PER_DAY=30
export NEWS_COOLDOWN_SECONDS=300
```

### 2. 安装依赖

```bash
cd /opt/ftseek/FTSeek
pip3.8 install feedparser requests
```

### 3. 测试运行

```bash
# 执行一次采集推送
python3.8 app/news_push/service.py

# 或作为模块运行
python3.8 -m app.news_push.service
```

### 4. 系统服务 (systemd)

```bash
# 创建服务文件
sudo cp scripts/news_push.service /etc/systemd/system/

# 启动服务
sudo systemctl daemon-reload
sudo systemctl enable news-push
sudo systemctl start news-push

# 查看状态
sudo systemctl status news-push
```

---

## 📁 目录结构

```
app/news_push/
├── __init__.py           # 模块导出
├── config.py             # 配置管理
├── collector.py          # 新闻采集器
├── rule_engine.py        # 规则引擎
├── rate_limiter.py       # 频率控制器
├── qwen_analyzer.py      # Qwen 分析器
├── notifier.py           # 钉钉推送器
├── service.py            # 主服务
└── README.md             # 本文档
```

---

## ⚙️ 配置说明

### config.py 主要配置项

```python
# 新闻源开关
CATEGORY_ENABLED = {
    "finance": True,    # 财经类
    "flash": True,      # 快讯
    "macro": True,      # 宏观
    "tech": False,      # 科技 (默认关闭)
    "crypto": False,    # 加密货币 (默认关闭)
}

# 关键词过滤
PUSH_KEYWORDS = {
    "must_include": ["财报", "业绩", "盈利", "收购", ...],
    "must_exclude": ["广告", "推广", "赞助", ...],
}

# 频率控制
RATE_LIMIT = {
    "max_per_hour": 5,      # 每小时最多 5 条
    "max_per_day": 30,      # 每日最多 30 条
    "cooldown_seconds": 300, # 冷却 5 分钟
}

# 推送时间窗口
PUSH_TIME_WINDOW = {
    "start_hour": 9,    # 9:00 开始
    "end_hour": 22,     # 22:00 结束
    "weekend_push": False,  # 周末不推送
}
```

---

## 📊 推送示例

### 单条新闻推送

```
🔴 [财联社] 10:30

**腾讯控股 Q4 净利润同比增长 30% 超预期**

📝 腾讯控股发布 2025 年第四季度财报，净利润达到 XXX 亿元...

---

🟢 情绪：positive (+0.65)
🔥 影响：high
👀 建议：关注

**关键点**:
  1. 净利润同比增长 30%
  2. 游戏业务表现强劲
  3. 广告收入超预期

📈 相关股票：HK.00700

[阅读原文](https://...)
```

### 合并摘要推送

```
### 📊 新闻摘要

**过去 1 分钟内 5 条重要新闻**

1. 🔴 **腾讯控股 Q4 净利润同比增长 30% 超预期**
2. 🟠 **特斯拉宣布新工厂选址**
3. 🔵 **美联储维持利率不变**
4. 🟢 **阿里回购计划加码**
5. 📄 **SEC 发布新规草案**
```

---

## 🔧 API 使用

### Python API

```python
from app.news_push import get_service

# 获取服务
service = get_service()

# 执行一次
service.run_once()

# 持续运行 (60 秒间隔)
service.start(interval=60)

# 获取统计
stats = service.get_stats()
print(stats)

# 停止
service.stop()
```

### 手动采集

```python
from app.news_push import get_collector, get_rule_engine

collector = get_collector()
rule_engine = get_rule_engine()

# 采集
news_list = collector.fetch_all()

# 过滤
filtered = rule_engine.batch_evaluate(news_list)

for news, result in filtered:
    print(f"{news.title} - 分数：{result.score}")
```

---

## 📈 监控与日志

### 日志文件
```
/opt/ftseek/FTSeek/logs/news_push.log
```

### 状态文件
```
/opt/ftseek/FTSeek/data/news_push_state.json
```

### 查看实时日志
```bash
tail -f /opt/ftseek/FTSeek/logs/news_push.log
```

---

## 🛠️ 故障排查

### 钉钉推送失败
1. 检查 Webhook URL 是否正确
2. 检查 Secret 是否匹配
3. 确认机器人已启用

### 新闻采集失败
1. 检查网络连接
2. 检查是否需要代理
3. 查看日志确认具体错误

### Qwen API 失败
1. 检查 API Key 是否正确
2. 检查额度是否充足
3. 降级为规则分析 (自动)

---

## 📝 更新日志

### v1.0.0 (2026-02-28)
- ✅ 初始版本
- ✅ 多源新闻采集
- ✅ 规则引擎过滤
- ✅ 频率控制
- ✅ Qwen 智能分析
- ✅ 钉钉推送

---

## 📄 许可证

基于 Situation-aggregation-monitoring 项目改造
