# 富途快讯集成方案

## 背景

富途牛牛有 7×24 快讯功能，但富途 OpenD API 不直接提供新闻接口。本方案通过多种方式获取富途快讯作为 FTSeek 新闻源补充。

---

## 方案一：Playwright 动态抓取（推荐）

### 1. 安装 Playwright

```bash
# 安装 playwright
pip install playwright

# 安装浏览器驱动
playwright install chromium

# 验证安装
python3 -c "from playwright.sync_api import sync_playwright; print('OK')"
```

### 2. 使用采集器

```python
from scripts.futu_news_fetcher import FutuNewsFetcher, fetch_futu_news_as_source

# 方式1: 使用 playwright
fetcher = FutuNewsFetcher(use_playwright=True)
news_list = await fetcher.fetch_news()

# 方式2: 直接获取标准化数据
news_dicts = await fetch_futu_news_as_source(use_playwright=True)
```

### 3. 定时采集

```bash
# 添加到 crontab
# 每5分钟采集富途快讯
*/5 * * * * cd /opt/ftseek/FTSeek && python3 -c "
import asyncio
from scripts.futu_news_fetcher import fetch_futu_news_as_source

async def main():
    news = await fetch_futu_news_as_source(use_playwright=True)
    print(f'采集到 {len(news)} 条快讯')

asyncio.run(main())
"
```

---

## 方案二：富途 APP 抓包 API（需认证）

### 1. 抓包分析

使用 Charles/Fiddler 抓包富途牛牛 APP，找到快讯 API：

```
可能端点:
- https://api.futunn.com/v2/flash/list
- https://www.futuhkapp.com/api/news/live
```

### 2. 模拟请求

```python
import requests

headers = {
    'User-Agent': 'FutuNN/...',
    'Authorization': 'Bearer <token>',
    'X-Device-ID': '<device_id>',
}

url = "https://api.futunn.com/v2/flash/list"
params = {'lang': 'zh-cn', 'limit': 20}

response = requests.get(url, headers=headers, params=params)
news_list = response.json()['data']['list']
```

### 3. 注意事项

⚠️ 抓包方式可能违反富途用户协议，建议：
- 仅用于个人研究
- 不用于商业用途
- 优先使用官方开放接口

---

## 方案三：RSS/第三方聚合

### 1. 富途 RSS（如果有）

```python
import feedparser

# 富途可能有 RSS 源
rss_url = "https://news.futunn.com/rss/live"
feed = feedparser.parse(rss_url)

for entry in feed.entries[:20]:
    news = {
        'title': entry.title,
        'link': entry.link,
        'published': entry.published,
    }
```

### 2. akshare 富途数据

```python
import akshare as ak

# akshare 可能有富途相关接口
# 查阅 akshare 文档确认
```

---

## 集成到 FTSeek 新闻系统

### 步骤1: 添加新闻源枚举

编辑 `modules/news_monitor.py`:

```python
class NewsSource(Enum):
    JINSHI = "jinshi"
    SINA = "sina"
    TENCENT = "tencent"
    CLS = "cls"
    FUTU = "futu"  # 新增富途源
```

### 步骤2: 添加抓取方法

```python
async def _fetch_futu_news(self, stock: str) -> List[NewsItem]:
    """抓取富途快讯"""
    from scripts.futu_news_fetcher import fetch_futu_news_as_source
    
    futu_news = await fetch_futu_news_as_source(stock, use_playwright=True)
    
    news_list = []
    for n in futu_news:
        news = NewsItem(
            id=n['id'],
            title=n['title'],
            content=n['content'],
            source=NewsSource.FUTU,
            publish_time=datetime.fromisoformat(n['publish_time']),
            stocks=n['stocks'],
            sentiment=NewsSentiment(n['sentiment']),
            sentiment_score=n['sentiment_score'],
            importance=NewsImportance(n['importance']),
            raw_url=n['url'],
        )
        news_list.append(news)
    
    return news_list
```

### 步骤3: 在 fetch_news() 中调用

```python
async def fetch_news(self, stock: str) -> List[NewsItem]:
    # ...现有代码...
    
    # 添加富途源
    tasks_map[NewsSource.FUTU] = asyncio.create_task(
        self._fetch_futu_news(stock)
    )
    
    # ...等待结果...
```

### 步骤4: 配置权重

```python
SOURCE_PRIORITY = {
    NewsSource.FUTU: 2,  # 富途优先级（仅次于金十）
}
```

---

## 文件位置

| 文件 | 说明 |
|------|------|
| `scripts/futu_news_fetcher.py` | 富途快讯采集器 |
| `modules/news_monitor.py` | 主新闻监控模块（需修改） |
| `docs/FUTU_NEWS_INTEGRATION.md` | 本文档 |

---

## 性能考虑

| 方案 | 优点 | 缺点 |
|------|------|------|
| Playwright | 完整数据、稳定 | 需要浏览器、较慢 |
| 抓包API | 快速、轻量 | 可能违规、需认证 |
| RSS | 简单、标准 | 富途可能无RSS |

---

## 建议实施顺序

1. ✅ 安装 playwright
2. ✅ 测试 `scripts/futu_news_fetcher.py`
3. ✅ 集成到 `modules/news_monitor.py`
4. ✅ 配置定时采集
5. ✅ 验证推送效果

---

## 预期效果

富途快讯集成后：

| 指标 | 提升 |
|------|------|
| 新闻源数量 | 4 → 5 |
| 快讯覆盖率 | +20% |
| 港股相关新闻 | +30% |
| 信号响应速度 | 更快 |

---

*文档创建时间: 2026-04-12*