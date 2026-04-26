"""
FTSeek 统一新闻管线 V2 — 设计文档

## 核心目标
只让"有效的新闻快讯"进入管线，实现快速识别、快速响应、快速获利。

## 关键事实
- Futu OpenD SDK ❌ 无原生新闻 API（已遍历所有公开接口）
- 当前新闻源: RSSHub (WSJ/Fed/Futu) + 部分失效源 (Reuters/东财)
- 替代数据源: Futu 逐笔异动、公告变化、资金流向（间接信号）

## 架构：三层漏斗

```
┌─────────────────────────────────────────────────────────────┐
│ Layer 0: 多源采集层 (Source Aggregator)                      │
│ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐        │
│ │RSSHub    │ │Futu Niu  │ │SEC EDGAR │ │同花顺/   │        │
│ │(WSJ/Fed) │ │Niu (web) │ │(美股公告) │ │东方财富  │        │
│ └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘        │
│      │            │            │            │                │
│      ▼            ▼            ▼            ▼                │
│ ┌─────────────────────────────────────────────────────┐     │
│ │              UnifiedNewsItem v2                      │     │
│ │  - id, title, content, source, timestamp            │     │
│ │  - market_tags: [HK, US, MACRO]                     │     │
│ │  - urgency: IMMEDIATE/HIGH/NORMAL/LOW               │     │
│ │  - geo_scan: 预筛结果                               │     │
│ │  - raw_score: 初始质量分 (0-1)                      │     │
│ └─────────────────────────────────────────────────────┘     │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│ Layer 1: 质量过滤层 (Quality Gate)                           │
│                                                              │
│  Gate 1: 去重 (SimHash + 语义相似度)                         │
│  Gate 2: 相关性 (watchlist 匹配 + 板块关联)                   │
│  Gate 3: 重要性 (预筛器 + 规则引擎)                          │
│  Gate 4: 时效性 (过期新闻拦截)                                │
│                                                              │
│  通过 Gate 1-4 → 进入 Layer 2                                │
│  未通过 → 归档 (不丢弃，供后续分析)                            │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│ Layer 2: 智能处理层 (Smart Processor)                        │
│                                                              │
│  快速通道 (<5s):                                             │
│  - 预筛器标记的事件 → 直接推送到 LLM                          │
│  - LLM 生成策略 → 注入 SignalStore                           │
│                                                              │
│  深度通道 (10-30s):                                          │
│  - 非事件新闻 → 批量汇总 → 定时 LLM 分析                      │
│  - 输出: 市场情绪报告 + 持仓调整建议                           │
│                                                              │
│  输出:                                                       │
│  - 钉钉/飞书推送 (格式化)                                     │
│  - 交易信号 (通过 LLMNewsStrategy)                            │
│  - 记忆更新 (us.json / hk.json)                              │
└─────────────────────────────────────────────────────────────┘
```

## 新增数据源详解

### 1. Futu NiuNiu 网页 API (非官方)
- URL: https://www.futunn.com/news/live
- 数据: 7x24 快讯 (与 RSSHub 的 futunn/live 同源，但更稳定)
- 频率: 每 30 秒轮询
- 风险: 非官方 API，可能被封 → 独立进程，不影响主系统

### 2. SEC EDGAR API (官方)
- URL: https://data.sec.gov/submissions/
- 数据: 美股公司 8-K/10-K/10-Q  filing
- 频率: 每小时扫描一次
- 风险: 无（官方 API）
- 价值: 财报/重大事件的第一手信息，比新闻早 5-15 分钟

### 3. 同花顺/东方财富快讯 (中国源)
- URL: https://news.10jqka.com.cn/tapp/news/push/stock/
- 数据: A 股快讯 (对港股有联动效应)
- 频率: 每 60 秒轮询
- 风险: 可能需要代理
- 价值: 南向资金联动的 A 股政策/行业新闻

### 4. Futu 逐笔异动 (间接信号)
- API: subscribe(SubType.TICKER) → 大单异动
- 数据: 异常成交量/价格变动
- 频率: 实时推送
- 价值: "新闻在盘面上" — 大单异动往往先于新闻

## UnifiedNewsItem v2 数据结构

```python
@dataclass
class UnifiedNewsItemV2:
    # 核心字段
    id: str                          # 全局唯一 ID (source + hash)
    title: str
    content: str                     # 摘要或全文
    source: str                      # 来源标识: rsshub/futu_niuniu/sec_edgar/10jqka
    source_name: str                 # 人类可读来源: "WSJ", "富途7x24", "SEC"
    timestamp: float                 # 发布时间戳 (epoch)
    collected_at: float              # 采集时间戳 (epoch)
    
    # 分类字段
    market_tags: List[str]           # ["HK", "US", "MACRO", "CRYPTO"]
    sectors: List[str]               # ["能源", "科技", "金融"]
    urgency: str                     # "IMMEDIATE" / "HIGH" / "NORMAL" / "LOW"
    
    # 质量字段
    raw_score: float                 # 初始质量分 (0-1)
    geo_scan: Dict                   # 地缘预筛结果
    related_stocks: List[str]        # 相关股票代码: ["HK.00700", "US.NVDA"]
    
    # 处理字段
    llm_analyzed: bool               # 是否已做 LLM 分析
    llm_result: Dict                 # LLM 分析结果
    pushed: bool                     # 是否已推送
    signal_generated: bool           # 是否已生成交易信号
    
    # 溯源字段
    trace_id: str                    # 追踪 ID (用于全链路归因)
    raw_url: str                     # 原文链接
```

## 去重方案升级

当前: 标题字符串哈希 (有遗漏)
目标: SimHash + 语义相似度

```python
class DedupEngineV2:
    def is_duplicate(self, item: UnifiedNewsItemV2) -> bool:
        # Gate 1: 精确匹配 (标题完全相同)
        if title_hash in self.exact_index:
            return True
        
        # Gate 2: SimHash (相似度 >0.85)
        simhash = compute_simhash(item.title + item.content[:200])
        if self.simhash_index.query(simhash, threshold=0.85):
            return True
        
        return False
```

## 文件结构

```
app/news_v2/
├── __init__.py
├── models.py               # UnifiedNewsItemV2
├── sources/
│   ├── __init__.py
│   ├── base.py             # SourceAdapter 基类
│   ├── rsshub_source.py    # RSSHub 源 (已有逻辑迁移)
│   ├── futu_niuniu.py      # Futu NiuNiu 网页 API
│   ├── sec_edgar.py        # SEC EDGAR 公告
│   └── cn_finance.py       # 同花顺/东方财富
├── quality/
│   ├── __init__.py
│   ├── dedup_v2.py         # SimHash 去重
│   ├── relevance_filter.py # watchlist/板块相关性
│   ├── importance_scorer.py # 重要性评分
│   └── geo_pre_filter.py   # 迁移自 factors/geo_pre_filter
├── processor/
│   ├── __init__.py
│   ├── fast_lane.py        # 快速通道 (<5s LLM)
│   ├── deep_lane.py        # 深度通道 (批量分析)
│   └── notifier.py         # 推送格式化
└── pipeline.py             # 统一管线入口 (orchestrator)
```

## 渐进式实施计划

Phase 1 (1-2 天): 基础设施
  - 创建 app/news_v2/ 目录结构
  - 实现 UnifiedNewsItemV2 数据模型
  - 实现 DedupEngineV2 (SimHash)
  - 迁移现有 RSSHub 逻辑到 SourceAdapter 模式

Phase 2 (2-3 天): 新增数据源
  - 实现 Futu NiuNiu 源 (非官方，独立进程)
  - 实现 SEC EDGAR 源
  - 实现 CN Finance 源 (可选)

Phase 3 (1-2 天): 质量过滤
  - 实现 Quality Gate 四层过滤
  - 集成 GeoPreFilter (已完成的模块)
  - 实现快速/深度双通道

Phase 4 (1 天): 集成与验证
  - 替换现有新闻管线
  - 端到端验证
  - 性能基准测试

## 降级策略

任何数据源失败 → 不影响其他源
LLM 不可用 → 走规则引擎降级
SimHash 不可用 → 降级到精确哈希匹配
整个 V2 管线失败 → 回退到现有 RSSHub 管线

## 不做的 (明确边界)

- ❌ 不改造 Futu OpenD SDK（没有新闻 API 就是没有）
- ❌ 不做实时新闻翻译（源已经是中文或英文）
- ❌ 不做新闻摘要生成（LLM 分析时自然包含）
- ❌ 不做新闻分类模型（LLM 会做分类）
