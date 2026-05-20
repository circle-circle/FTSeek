# 中国大陆新闻源接入方案

> **日期**: 2026-05-14
> **场景**: FTSeek-US 在中国大陆环境下获取美股新闻并喂给 LLM (L1 新闻分类器)
> **目标**: 实现高效、及时、稳定的新闻筛选 — 5 分钟内入库, 7×24 可达

---

## 1. 国内可达性现状 (2026 年实测)

### ✅ 完全可达 (无需代理)
| 源 | 类型 | 语言 | 时效 | 国内推荐 |
|-----|-----|------|------|---------|
| **财联社快讯** cls.cn | 中文财经 | 中文 | 实时 (秒级) | ⭐⭐⭐⭐⭐ |
| **华尔街见闻** wallstreetcn.com | 中文财经 | 中文 | 1-5 min | ⭐⭐⭐⭐⭐ |
| **东方财富** eastmoney.com | 中文财经 | 中文 | 1-15 min | ⭐⭐⭐⭐ |
| **新浪财经** finance.sina.com.cn | 中文财经 | 中文 | 5-30 min | ⭐⭐⭐ |
| **雪球美股** xueqiu.com | 中文社交+财经 | 中文 | 1-30 min | ⭐⭐⭐ |
| **Tushare** | API | 中文+英文 | 日级 | ⭐⭐ |
| **AKShare** | API | 中英 | 日级 | ⭐⭐ |
| **Finnhub** finnhub.io | 英文 API | 英文 | 实时 | ⭐⭐⭐⭐ (国内 CDN OK) |
| **TwelveData** | 英文 API | 英文 | 实时 | ⭐⭐⭐ |
| **Alpha Vantage** | 英文 API | 英文 | 5-15 min | ⭐⭐⭐ |
| **Polygon.io** | 英文 API | 英文 | 实时 | ⭐⭐⭐ (CDN 不稳) |

### ⚠️ 部分可达 (CDN 不稳, 时好时坏)
| 源 | 现状 | 应对 |
|-----|-----|------|
| Yahoo Finance | API 限流 + 偶发 timeout | 重试 + 缓存 |
| **SEC EDGAR** | 下午 ET 时段经常 502 | 重试 + 国内云中转 |
| MarketWatch | 慢, ~10s 延迟 | 仅作备份 |
| Investing.com | 偶发 403 | 仅作备份 |

### 🚫 国内不可达 (需 VPN/代理或 RSSHub 桥接)
| 源 | 价值 | 替代方案 |
|-----|-----|---------|
| Twitter API | 极高 (突发新闻) | RSSHub `/twitter/user/...` 或 Nitter 实例 |
| Reuters | 高 (深度) | 财联社 + 华尔街见闻翻译稿 |
| Bloomberg | 高 | 同上 |
| WSJ | 中 | RSSHub `/wsj` (已有, 但实例不稳) |
| CNBC | 中 | 雪球转载 |
| Reddit r/wallstreetbets | 中 (情绪) | RSSHub `/reddit/...` |
| Seeking Alpha | 中 | 雪球转载 |

---

## 2. 三套源组合推荐

### 方案 A: **国产财经 + Finnhub** (推荐, 最稳)
**适用**: 不想折腾代理, 追求稳定可靠

```yaml
news_sources:
  cn:
    cailianpress: { enabled: true, interval_sec: 30 }    # 实时快讯
    wallstreetcn: { enabled: true, interval_sec: 60 }    # 美股专栏
    eastmoney_us: { enabled: true, interval_sec: 120 }   # 个股
  us:
    finnhub:      { enabled: true, interval_sec: 60 }    # 突发新闻
    sec_edgar:    { enabled: true, interval_sec: 300 }   # 公司公告
```

**优势**:
- 财联社快讯比 Reuters/Bloomberg 中文翻译稿更快 (因为它有自己的记者)
- 全部国内可达, 无 GFW 风险
- 中文新闻喂 L12 多语言 LLM 处理质量很好

**缺陷**:
- 缺 Twitter/Reddit 情绪信号
- 翻译稿存在 5-30 min 滞后

**成本**: $0/月 (Finnhub free tier 60 calls/min 够用)

---

### 方案 B: **方案 A + 自建 RSSHub** (推荐, 覆盖最全)
**适用**: 有阿里云/腾讯云 ECS, 想要 Twitter/Reddit 信号

部署 RSSHub:
```bash
# 阿里云 ECS (4 核 8G, ~¥200/月)
docker run -d --name rsshub --restart=always -p 1200:1200 diygod/rsshub
```

订阅关键路由:
```yaml
news_sources:
  rsshub_self:
    enabled: true
    base_url: "http://your-ecs.cn:1200"
    routes:
      # 高价值 Twitter 账号
      - path: "/twitter/user/elonmusk"        # Elon
      - path: "/twitter/user/jimcramer"       # Jim Cramer
      - path: "/twitter/user/zerohedge"       # 新闻聚合
      - path: "/twitter/user/business"        # Bloomberg Twitter
      # SEC + 央行
      - path: "/federalreserve/press"
      - path: "/sec/press"
      # Reddit
      - path: "/reddit/subreddit/wallstreetbets/hot"
      - path: "/reddit/subreddit/stocks/hot"
      # 财报电话会议
      - path: "/seekingalpha/transcript"
```

**优势**: 覆盖 Twitter / Reddit / 央行原文; 一份 ECS 同时供港股 + 美股
**成本**: ¥200/月 ECS + $0 RSSHub (开源)

---

### 方案 C: **方案 B + 商业 API** (终极)
**适用**: 实盘对接, 不允许任何延迟

加 Polygon.io ($199/月) 提供 SEC 文件 + 期权链数据.

```yaml
news_sources:
  polygon:
    enabled: true
    api_key: ${POLYGON_API_KEY}
    fast_news_endpoint: true
```

**适用范围**: 实盘 > $100k 资金时再考虑.

---

## 3. 已实现的国产源 (本次新增)

### 3.1 财联社快讯 (`cailianpress_source.py`)
- API: `https://www.cls.cn/nodeapi/telegraphList`
- 频率: 30s 一次拉最新 N 条
- **优势**: 财联社记者 24h 蹲守, 央行新闻常常比英文媒体早 1-3 分钟
- **关键字过滤**: 内置 "美股 / NVDA / AAPL / 美联储 / 鲍威尔 / SEC..." 等
- 国内 CDN, 200ms 延迟

### 3.2 华尔街见闻 (`wallstreetcn_source.py`)
- API: `https://api-prod.wallstreetcn.com/apiv1/content/articles`
- 频率: 60s
- **优势**: 美股专题质量高, 翻译及时准确, 含分析
- 国内可达, 500ms 延迟

### 3.3 东方财富美股 (`eastmoney_us_source.py`)
- API: `https://np-listapi.eastmoney.com/...` (个股新闻接口)
- 频率: 120s
- **优势**: 个股层面新闻最全 (财报/评级/重大事项)
- 缺陷: 需按 watchlist 逐 symbol 拉

### 3.4 NewsSourceDashboard (`scripts/news_sources_health.py`)
- 实时显示各源延迟 / 可用率 / 当日新闻数
- `python scripts/news_sources_health.py --live` 持续监控

---

## 4. 配置 yaml 接入

```yaml
news_sources:
  # 国产 (推荐主路径, 默认开)
  cn:
    cailianpress:
      enabled: true
      interval_sec: 30
      keywords: ["美股", "美联储", "纳指", "标普", "鲍威尔", "SEC"]
      watchlist_symbols_zh:
        NVDA: ["英伟达", "Nvidia"]
        AAPL: ["苹果", "Apple"]
        TSLA: ["特斯拉", "Tesla"]
    wallstreetcn:
      enabled: true
      interval_sec: 60
      categories: ["us-stock", "fed", "earnings"]
    eastmoney_us:
      enabled: false             # 默认 off, 需 watchlist 配置
      interval_sec: 120
      symbols: []                # 空 → 用主 watchlist

  # 国际 (英文)
  us:
    finnhub:
      enabled: true
      interval_sec: 60
    sec_edgar:
      enabled: true
      interval_sec: 300
    rsshub_self:
      enabled: false
      base_url: "http://localhost:1200"   # 自建 RSSHub
      routes: []

  # 代理配置 (可选, RSSHub 自建后通常不需要)
  proxy:
    twitter_proxy: ""              # 空 = 不走代理
    bloomberg_proxy: ""
```

---

## 5. 与 L1-L12 LLM 的衔接

```
[财联社] ─┐
[华尔街见闻] ─┤ 中文新闻 ─→ NewsPipelineV2 ─→ 去重 + relevance
[东方财富] ─┘                                      │
                                                    ▼
[Finnhub]  ─┐                                  L12 多语言 LLM
[SEC EDGAR]─┤ 英文新闻 ─→ NewsPipelineV2 ─→  L1 LLM 分类器
[RSSHub]   ─┘                                      │
                                                    ▼
                                           NewsClassification
                                          (sentiment/credibility/
                                           action_hint/horizon)
                                                    │
                                                    ▼
                                           L9 信号生成 (可选)
                                                    │
                                                    ▼
                                          SignalArbitrator
                                          (与 IntradayScorer 同级竞争)
```

中英文新闻各自分类后, **同事件去重**由 dedup_v2 + L1 LLM 的 primary_tickers 字段共同完成.

---

## 6. 验证 + 监控

### 启动后 30 min 检查
```bash
# 1. 各源新闻数 (今日)
sqlite3 data/rsshub_us/news_us.db \
  "SELECT source_id, COUNT(*) FROM news_items WHERE timestamp > strftime('%s','now','-1 hour') GROUP BY source_id"
# 期望: cailianpress > 100, wallstreetcn > 30, finnhub > 10

# 2. 健康看板
python scripts/news_sources_health.py
# 各源应显示 success_rate > 0.9

# 3. L1 分类落库 (启用后)
sqlite3 data/news_quality.db \
  "SELECT raw_lang, COUNT(*) FROM news_classifications GROUP BY raw_lang"
# 期望: zh + en 都有
```

### 异常处理
- 单源 5 次连续失败 → SourceHealthMonitor 自动暂停 30 min
- 国产源全部不可达 → 检查 DNS/防火墙
- LLM 分类失败率 > 30% → 关 L1 用 yaml flag

---

## 7. 推荐启用顺序 (模拟盘)

| 阶段 | 启用 | 验证 |
|-----|------|------|
| 第 1 天 | 财联社 + 华尔街见闻 | 各源每小时 > 30 条 |
| 第 2 天 | 加 L1 (news_classifier) | 中文新闻能正确分类 |
| 第 3-7 天 | 加 L12 (multilang) | 中文情绪/事件抽取准确率 > 70% |
| 第 2 周 | 加 L9 (news_signal_gen) | 突发事件 5min 内出 owner=llm_news 信号 |
| 第 3 周 | 加东方财富个股新闻 | 持仓 symbol 公司新闻覆盖率 > 80% |
| 第 4 周 | (可选) 自建 RSSHub | 接入 Twitter / Reddit |

---

## 8. 成本估算

| 方案 | 月成本 | 覆盖 |
|------|-------|-----|
| A 仅国产 + Finnhub | ¥0 | 中英文 90% 重大事件 |
| B + 自建 RSSHub | ¥200 (ECS) | 95% + Twitter 情绪 |
| C + Polygon.io | ¥1500 ($199) | 99% + 实盘级延迟 |

**模拟盘阶段方案 A 足够**, 实盘上 100k+ 时再考虑 B 或 C.
