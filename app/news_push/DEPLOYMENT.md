# 金融新闻推送系统 - 部署指南

## 📋 系统概述

本系统基于 [Situation-aggregation-monitoring](https://github.com/MidaTechnology/Situation-aggregation-monitoring) 项目改造，专注于金融新闻信息的实时采集、规则过滤、频率控制和钉钉推送。

### 核心功能
- ✅ 多源新闻采集（RSS/API/爬虫）
- ✅ 规则引擎过滤（关键词/来源/分类/股票匹配）
- ✅ 频率控制（限流/冷却/合并推送）
- ✅ Qwen 大模型智能分析（可选）
- ✅ 钉钉机器人推送

---

## 🚀 部署步骤

### 1. 环境准备

```bash
# 确认 Python 版本
python3.8 --version

# 进入项目目录
cd /opt/ftseek/FTSeek

# 安装依赖
pip3.8 install feedparser requests -q
```

### 2. 配置环境变量

创建或编辑 `/etc/environment` 或 `~/.bashrc`:

```bash
# ==================== 必需配置 ====================

# 钉钉机器人 Webhook URL
# 获取方式：钉钉群 → 群设置 → 智能群助手 → 添加机器人 → 自定义
export DINGTALK_WEBHOOK_URL="https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN"

# 钉钉机器人加签 Secret（如果启用了加签）
export DINGTALK_SECRET="SECxxxxxxxxxx"

# ==================== 可选配置 ====================

# Qwen 大模型配置（用于智能分析）
# 获取 API Key: https://dashscope.console.aliyun.com/
export QWEN_ENABLED="true"
export QWEN_API_KEY="sk-xxxxxxxxxx"
export QWEN_MODEL="qwen-max"  # qwen-max / qwen-plus / qwen-turbo

# 监控股票列表（JSON 格式）
export NEWS_WATCH_STOCKS='[
  {"code": "HK.00700", "name": "腾讯控股", "enabled": true},
  {"code": "HK.09988", "name": "阿里巴巴", "enabled": true},
  {"code": "HK.09992", "name": "泡泡玛特", "enabled": true},
  {"code": "US.AAPL", "name": "苹果", "enabled": true},
  {"code": "US.TSLA", "name": "特斯拉", "enabled": true},
  {"code": "US.NVDA", "name": "英伟达", "enabled": true},
  {"code": "US.MSFT", "name": "微软", "enabled": true}
]'

# 推送频率控制
export NEWS_MAX_PER_HOUR=5        # 每小时最多推送条数
export NEWS_MAX_PER_DAY=30        # 每日最多推送条数
export NEWS_COOLDOWN_SECONDS=300  # 推送冷却时间（秒）

# 采集间隔
export NEWS_PUSH_INTERVAL=60      # 采集间隔（秒）

# 推送时间窗口
export PUSH_START_HOUR=9          # 开始推送时间（9:00）
export PUSH_END_HOUR=22           # 结束推送时间（22:00）
export WEEKEND_PUSH="false"       # 周末是否推送
```

使环境变量生效:
```bash
source ~/.bashrc
```

### 3. 测试运行

```bash
# 运行测试脚本
python3.8 test_news_push.py

# 或手动执行一次采集推送
python3.8 -m app.news_push.service
```

### 4. 系统服务部署（systemd）

```bash
# 复制服务文件
sudo cp scripts/news_push.service /etc/systemd/system/

# 重新加载 systemd
sudo systemctl daemon-reload

# 启用服务（开机自启）
sudo systemctl enable news-push

# 启动服务
sudo systemctl start news-push

# 查看状态
sudo systemctl status news-push

# 查看日志
journalctl -u news-push -f
```

### 5. 验证部署

```bash
# 检查服务状态
systemctl is-active news-push

# 查看实时日志
tail -f /opt/ftseek/FTSeek/logs/news_push.log

# 检查钉钉群是否收到消息
```

---

## ⚙️ 配置详解

### 新闻源配置

编辑 `app/news_push/config.py`:

```python
# 启用的分类
CATEGORY_ENABLED = {
    "finance": True,    # 财经新闻
    "flash": True,      # 快讯（财联社、BlockBeats）
    "macro": True,      # 宏观政策
    "tech": False,      # 科技新闻（默认关闭）
    "crypto": False,    # 加密货币（默认关闭）
}

# 来源优先级（数字越小优先级越高）
SOURCE_PRIORITY = {
    "cls": 1,           # 财联社（最高）
    "blockbeats": 2,    # BlockBeats
    "wallstreetcn": 3,  # 华尔街见闻
    "sina": 4,          # 新浪财经
    "cnbc": 5,          # CNBC
}
```

### 关键词过滤

```python
PUSH_KEYWORDS = {
    # 必须包含的关键词（任一匹配即推送）
    "must_include": [
        "财报", "业绩", "盈利", "亏损", "营收",
        "收购", "重组", "IPO", "回购",
        "监管", "调查", "诉讼", "处罚",
        # ... 更多关键词
    ],
    
    # 必须排除的关键词（任一匹配即过滤）
    "must_exclude": [
        "广告", "推广", "赞助",
        # ... 更多关键词
    ],
}
```

### 监控股票

```python
# 股票代码别名映射（用于新闻内容匹配）
STOCK_ALIASES = {
    "HK.00700": ["腾讯", "Tencent", "0700", "00700.HK"],
    "HK.09988": ["阿里", "Alibaba", "9988", "阿里巴巴"],
    "US.AAPL": ["苹果", "Apple", "AAPL"],
    # ... 更多股票
}
```

---

## 📊 监控与维护

### 日志文件

```
/opt/ftseek/FTSeek/logs/news_push.log
```

### 状态文件

```
/opt/ftseek/FTSeek/data/news_push_state.json
```

### 缓存目录

```
/opt/ftseek/FTSeek/data/news_cache/
```

### 常用命令

```bash
# 重启服务
sudo systemctl restart news-push

# 停止服务
sudo systemctl stop news-push

# 查看服务状态
sudo systemctl status news-push

# 查看实时日志
journalctl -u news-push -f

# 查看今日推送统计
cat /opt/ftseek/FTSeek/data/news_push_state.json | python3.8 -m json.tool
```

---

## 🛠️ 故障排查

### 钉钉推送失败

1. **检查 Webhook URL**
   ```bash
   echo $DINGTALK_WEBHOOK_URL
   ```

2. **检查机器人配置**
   - 确认机器人已启用
   - 确认加签 Secret 正确
   - 确认 IP 白名单（如果配置了）

3. **测试推送**
   ```bash
   curl "https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"msgtype":"text","text":{"content":"测试"}}'
   ```

### 新闻采集失败

1. **检查网络连接**
   ```bash
   curl -I https://www.cnbc.com/id/100003114/device/rss/rss.html
   ```

2. **检查是否需要代理**
   ```bash
   # 如果需要代理，设置环境变量
   export HTTP_PROXY="http://proxy:port"
   export HTTPS_PROXY="http://proxy:port"
   ```

3. **查看具体错误**
   ```bash
   tail -100 /opt/ftseek/FTSeek/logs/news_push.log
   ```

### Qwen API 失败

1. **检查 API Key**
   ```bash
   echo $QWEN_API_KEY
   ```

2. **检查额度**
   登录 https://dashscope.console.aliyun.com/ 查看余额

3. **降级处理**
   系统会自动降级为规则分析，不影响推送

---

## 📈 性能优化

### 调整采集频率

```bash
# 降低频率（减少 API 调用）
export NEWS_PUSH_INTERVAL=120  # 2 分钟采集一次

# 提高频率（更及时）
export NEWS_PUSH_INTERVAL=30   # 30 秒采集一次
```

### 调整推送限制

```bash
# 增加推送量
export NEWS_MAX_PER_HOUR=10
export NEWS_MAX_PER_DAY=50

# 减少推送量（避免打扰）
export NEWS_MAX_PER_HOUR=3
export NEWS_MAX_PER_DAY=20
```

### 调整时间窗口

```bash
# 仅工作时间推送
export PUSH_START_HOUR=9
export PUSH_END_HOUR=18
export WEEKEND_PUSH="false"

# 全天推送
export PUSH_START_HOUR=0
export PUSH_END_HOUR=24
export WEEKEND_PUSH="true"
```

---

## 🔐 安全建议

1. **环境变量**
   - 不要将 API Key 硬编码到代码中
   - 使用系统环境变量或加密配置

2. **访问控制**
   - 限制日志文件访问权限
   - 定期清理缓存文件

3. **钉钉机器人**
   - 启用加签验证
   - 配置 IP 白名单
   - 定期更换 Token

---

## 📝 更新日志

### v1.0.0 (2026-02-28)
- ✅ 初始版本发布
- ✅ 多源新闻采集
- ✅ 规则引擎过滤
- ✅ 频率控制
- ✅ Qwen 智能分析
- ✅ 钉钉推送

---

## 📞 支持

如有问题，请查看:
- 日志文件：`/opt/ftseek/FTSeek/logs/news_push.log`
- 项目文档：`/opt/ftseek/FTSeek/app/news_push/README.md`
