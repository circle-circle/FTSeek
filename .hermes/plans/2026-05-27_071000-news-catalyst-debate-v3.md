# FTSeek-US 盘前/盘后新闻催化辩论引擎 (v3 — 时效性强化版)

> **类型**: L3 新特性 | **焦点**: F1 + F3
> **核心原则**: 不重复造轮子 + 行情先行新闻确认 + 最强模型辩论
> **调度**: 仅盘前(8:30-9:25 ET) + 盘后(16:05-18:00 ET)

---

## 1. 目标

盘前自动采集过夜重大新闻，通过3个最强模型充分辩论，将催化事件结论注入作战计划；盘后回看准确率形成闭环。**同时建立"行情先行→新闻确认"的反向催化检测机制**。

---

## 2. 时效性保障体系 (核心新增)

### 2.1 问题诊断

| 信息源 | 当前延迟 | 问题 |
|--------|---------|------|
| NewsNow-华尔街见闻 | 3分钟轮询 | 二手信息，编辑加工后发布 |
| NewsNow-财联社电报 | 5分钟轮询 | 相对快但仍延迟 |
| NewsNow-金十数据 | 5分钟轮询 | 宏观数据为主 |
| Finnhub News | 轮询 | 英文延迟更大 |
| RSSHub (WSJ/Reuters) | 3分钟轮询 | RSS天然延迟 |
| SEC EDGAR | 5分钟轮询 | 公告本身是一手但延迟发布 |
| **行情异动** | **30秒** (emergency_interceptor) | **最快信号但当前只告警不溯源** |

**核心洞察**: 对于"富途受证监会监管暴跌"这类事件，**价格异动比新闻早 5-30分钟**。系统已有30秒级行情检测，但当前只触发告警，不会去搜索原因。

### 2.2 三层时效性架构

```
Layer 1: 行情先行 (已有, 30秒级)     ← 最快，价格就是最一手的信息
  └── emergency_interceptor 检测到异动
  └── 🔧 新增: 异动触发 → 立即搜索相关新闻 → 快速辩论(1轮)
  
Layer 2: 一手信息源 (新增, 2-5分钟级) ← 公告/监管文件，信息权威
  └── 🔧 新增: 港交所披露易 (HKEXnews) — 上市公司公告
  └── 🔧 新增: 证监会官网爬取 — 监管处罚/调查
  └── 🔧 新增: 财联社电报轮询缩短到2分钟
  
Layer 3: 二手信息聚合 (已有, 3-5分钟级) ← 补充视角
  └── 华尔街见闻/金十数据/WSJ/Reuters (现有)
```

### 2.3 "行情先行→新闻确认" 反向催化 (关键设计)

**现有链路** (正向):
```
新闻发布 → 系统采集 → 分类 → 辩论 → 信号
延迟: 5-35分钟 (太慢!)
```

**新增链路** (反向):
```
价格异动(30s) → 搜索新闻确认 → 快速辩论(60s) → 催化结论
延迟: 1.5-3分钟 (快10倍!)
```

**实现方式**: 在 `emergency_interceptor` 检测到 PRICE_CRASH/RAPID_DECLINE 时，**新增回调**:

```python
# 现有: 检测到异动 → 告警 + RISK_OFF
# 新增: 检测到异动 → 告警 + 搜索新闻 + 催化辩论

async def _on_price_anomaly(self, symbol, change_pct):
    # ...existing alert logic...
    
    # 🔧 新增: 反向催化检测
    if abs(change_pct) >= 0.03:  # 3%以上异动触发
        news = await self._search_catalyst_news(symbol)
        if news:
            event = self.classifier.classify_breaking(news)
            if event and event.severity in ("CRITICAL", "HIGH"):
                # 盘前/盘后: 走完整辩论
                # 盘中: 仅记录，不交易 (用户要求)
                verdict = self.catalyst_debate.quick_debate(event, rounds=1)
                self._store_catalyst_verdict(verdict)
```

### 2.4 一手信息源新增

| 信息源 | 类型 | 获取方式 | 延迟 | 一手性 |
|--------|------|---------|------|--------|
| **港交所披露易 (HKEXnews)** | 上市公司公告 | HTTP scraping (已验证HTTP 200可达) | 2分钟轮询 | ★★★★★ 公司直接发布 |
| **证监会官网 (CSRC)** | 监管处罚/调查 | HTTP scraping (已验证HTTP 200可达) | 5分钟轮询 | ★★★★★ 监管直接发布 |
| **财联社电报** | 快讯 | NewsNow API (已有) | 缩短到2分钟 | ★★★★ 电报级速度 |
| **Futu OpenD Push** | 行情异动 | Push handler (已有) | 实时 | ★★★★★ 价格就是一手信息 |
| **SEC EDGAR** | 公告 | RSS (已有) | 5分钟轮询 | ★★★★★ 公司直接提交 |

**新增采集器实现** (复用 SourceAdapter 模式):

```python
# app/catalyst/sources/hkex_source.py (~60行)
class HKEXSource(SourceAdapter):
    """港交所披露易 — 上市公司公告 (一手信息源)
    
    复用: app/news_v2/sources/base.SourceAdapter 接口
    URL: https://www1.hkexnews.hk/search/titlesearch.xhtml
    筛选: 仅 watchlist 中有港股代码的公司
    """

# app/catalyst/sources/csrc_source.py (~60行)  
class CSRCSource(SourceAdapter):
    """证监会官网 — 监管公告 (一手信息源)
    
    URL: http://www.csrc.gov.cn/csrc/c100028/common_list.shtml
    关键词: watchlist 公司名 + "处罚"/"调查"/"监管"
    """
```

---

## 3. 模型选择 (修订)

### 3.1 辩论角色分配

| 角色 | 模型 | 为什么 | 视角 |
|------|------|--------|------|
| **宏观策略师** | qwen3.7-max | 中文推理最强，擅长宏观传导链，对中国监管事件理解深 | "证监会监管→中概券商板块传导→影响范围" |
| **风控质疑官** | deepseek-v4-pro | 推理能力最强，擅长找漏洞和反面论证 | "这个判断有什么盲点？如果只是例行检查？" |
| **行业情绪师** | kimi-k2.6 (百炼) | 长上下文+联网搜索能力，擅长行业分析和历史类比 | "历史上类似监管事件后股价怎么走？" |

**三家族独立**: 阿里(Qwen) + DeepSeek + 月之暗面(Kimi) — 确保真正独立视角。

### 3.2 Kimi K2.6 配置

```yaml
llm:
  models:
    kimi-k2:
      provider: dashscope          # 百炼已配置 kimi
      api_key_env: DASHSCOPE_API_KEY
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
      model_name: kimi-k2.6
      timeout: 60
      cost_in: 0.004
      cost_out: 0.016

  features:
    catalyst_sentiment:  {primary: kimi-k2, fallback: qwen-plus}
```

**注意**: 已有 `BAILIAN_MODEL_KIMI=kimi-k2.5` 配置，kimi-k2.6 是同系列升级版，百炼平台直接可用。

### 3.3 完整 feature 路由

```yaml
llm:
  features:
    # 催化辩论 — 3视角各用最强模型 (不同家族)
    catalyst_macro:      {primary: qwen-max,    fallback: qwen-plus}       # qwen3.7-max
    catalyst_risk:       {primary: deepseek-v4, fallback: deepseek-pro}    # deepseek-v4-pro
    catalyst_sentiment:  {primary: kimi-k2,     fallback: qwen-plus}       # kimi-k2.6
    catalyst_classifier: {primary: qwen-max,    fallback: qwen-plus}       # 分类+翻译
    catalyst_review:     {primary: deepseek-v4, fallback: deepseek-pro}    # 盘后回看
    catalyst_breaking:   {primary: qwen-flash,  fallback: qwen-plus}       # 盘中快速(低延迟)
```

---

## 4. 架构 (修订)

### 4.1 完整数据流

```
┌──────────────────────────────────────────────────────────────────┐
│  Layer 1: 行情先行 (反向催化)                                      │
│                                                                   │
│  Futu Push ──→ emergency_interceptor (30s检测)                    │
│                      │                                            │
│                      ├─ 现有: 告警 + RISK_OFF                     │
│                      └─ 🔧新增: 异动触发新闻搜索 → 快速辩论(1轮)    │
│                                                                   │
├──────────────────────────────────────────────────────────────────┤
│  Layer 2: 一手信息源 (新增)                                        │
│                                                                   │
│  HKEXnews ──┐                                                     │
│  CSRC ──────┤──→ SourceAdapter ──→ NewsPipelineV2 (已有门禁)      │
│  财联社(2m) ┘   (新建2个源)     (复用去重+相关性+重要性过滤)       │
│                                                                   │
├──────────────────────────────────────────────────────────────────┤
│  Layer 3: 二手信息聚合 (已有)                                      │
│                                                                   │
│  华尔街见闻 ──┐                                                    │
│  金十数据 ────┤──→ NewsNow ──→ NewsPipelineV2                     │
│  Finnhub ─────┤                                                    │
│  RSSHub ──────┤                                                    │
│  SEC EDGAR ───┘                                                    │
│                                                                   │
├──────────────────────────────────────────────────────────────────┤
│  催化事件分类器 (复用 emergency_interceptor 关键词库)               │
│                                                                   │
│  新闻流 ──→ 关键词匹配(O(n)) ──→ 命中项 LLM确认 ──→ CatalystEvent │
│                                                                   │
├──────────────────────────────────────────────────────────────────┤
│  多模型辩论 (3模型并行, 复用 DebatePlanner 模式)                    │
│                                                                   │
│  qwen3.7-max ──┐                                                  │
│  deepseek-v4 ──┼──→ Round 1 并行 → 分歧度 → Round 2 → Arbiter    │
│  kimi-k2.6 ────┘                                                  │
│                                                                   │
├──────────────────────────────────────────────────────────────────┤
│  注入现有辩论上下文 (仅改 _build_debate_context 加1个字段)          │
│                                                                   │
│  catalyst_verdicts ──→ debate_context ──→ DebatePlanner ──→ Plan  │
│                                                                   │
└──────────────────────────────────────────────────────────────────┘
```

### 4.2 盘前/盘后调度 (不改动盘中)

| 时段 | 触发 | 行为 |
|------|------|------|
| **盘前** 8:30 ET | cron / scheduler | 全量扫描12h新闻 → 分类 → 3模型完整辩论(2轮) → 注入计划 |
| **盘后** 16:05 ET | cron c09e8387406b | 回看催化准确率 → 辩论教训 → 写入lessons DB |
| **盘前异动** | emergency_interceptor | 30s检测 → 搜索新闻 → 快速辩论(1轮60s) → 存储结论 |
| **盘中** | — | ❌ 不接入交易 (用户明确要求) |

---

## 5. 实现步骤

### Phase 1: 模型配置 + 一手信息源 (L1)

**文件**:
- 修改: `config/unified_config.yaml` (+40行)
- 新建: `app/catalyst/__init__.py` (5行)
- 新建: `app/catalyst/models.py` (40行)
- 新建: `app/catalyst/sources/hkex_source.py` (60行)
- 新建: `app/catalyst/sources/csrc_source.py` (60行)
- 修改: `app/core/scheduler_core.py` (+10行 — 注册新源)

**要点**:
- HKEXSource/CSRCSource 继承 `SourceAdapter`，自动接入 `NewsPipelineV2` 质量门禁
- 财联社轮询间隔从5分钟缩短到2分钟 (仅改 NewsNow SOURCES 配置)
- 新增 kimi-k2 模型配置 + 5个 catalyst feature 路由

### Phase 2: 催化事件分类器 (L1)

**文件**:
- 新建: `app/catalyst/event_classifier.py` (120行)

**设计**:
- 复用 `emergency_interceptor.BLACK_SWAN_KEYWORDS` (利空关键词)
- 新增利好关键词 (FDA批准/财报超预期/上调评级等)
- 新增中概股/行业映射 (证监会事件→FUTU+BABA+PDD)
- 关键词命中后 call_llm(feature="catalyst_classifier") 确认+翻译+关联标的

### Phase 3: 催化辩论引擎 (L2)

**文件**:
- 新建: `app/catalyst/catalyst_debate.py` (250行)

**设计**:
- 3模型并行调用 (ThreadPoolExecutor, 复用 DebatePlanner 并行模式)
- 分歧度计算 (复用 DebatePlanner._calculate_divergence 逻辑)
- Round 2 交叉验证 (分歧>0.4时触发)
- Arbiter 聚合 (共识度→strength 调整)
- `quick_debate()`: 1轮快速模式 (60s内完成, 用于反向催化)

### Phase 4: 反向催化 + 集成 (L2)

**文件**:
- 新建: `app/catalyst/detector.py` (80行)
- 新建: `app/catalyst/reverse_catalyst.py` (80行)
- 修改: `app/pipeline/llm_pipeline.py` (+15行 — 注入催化结论)
- 修改: `app/llm/prompts/debate.py` (+20行 — ARBITER_PROMPT 催化章节)
- 修改: `app/us_pipeline/emergency_interceptor.py` (+15行 — 异动回调)

**reverse_catalyst.py**:
```python
"""反向催化: 行情异动 → 搜索新闻 → 快速辩论

当 emergency_interceptor 检测到价格异动时:
1. 搜索该标的相关新闻 (NewsNow + Finnhub, 最近30分钟)
2. 如果找到催化事件 → quick_debate(1轮)
3. 存储 CatalystVerdict → 盘前计划/盘后归因使用
"""
```

### Phase 5: 盘后催化回看 (L1)

**文件**:
- 新建: `app/catalyst/postmarket_review.py` (100行)

**设计**:
- 读取今日催化结论 vs 实际涨跌
- call_llm(feature="catalyst_review") 分析预测偏差
- 教训写入 LessonInjector (下次盘前自动注入)

---

## 6. 文件清单

| 操作 | 文件 | 行数 | 说明 |
|------|------|------|------|
| 新建 | `app/catalyst/__init__.py` | 5 | 模块入口 |
| 新建 | `app/catalyst/models.py` | 40 | CatalystEvent + CatalystVerdict |
| 新建 | `app/catalyst/sources/hkex_source.py` | 60 | 港交所披露易(一手) |
| 新建 | `app/catalyst/sources/csrc_source.py` | 60 | 证监会官网(一手) |
| 新建 | `app/catalyst/event_classifier.py` | 120 | 催化事件分类 |
| 新建 | `app/catalyst/catalyst_debate.py` | 250 | 3模型辩论 |
| 新建 | `app/catalyst/detector.py` | 80 | 盘前/盘后调度 |
| 新建 | `app/catalyst/reverse_catalyst.py` | 80 | 行情先行→新闻确认 |
| 新建 | `app/catalyst/postmarket_review.py` | 100 | 盘后回看+教训 |
| 修改 | `config/unified_config.yaml` | +40 | 模型+features+catalyst+财联社2m |
| 修改 | `app/pipeline/llm_pipeline.py` | +15 | 注入催化结论 |
| 修改 | `app/llm/prompts/debate.py` | +20 | ARBITER催化章节 |
| 修改 | `app/core/scheduler_core.py` | +10 | 注册新新闻源 |
| 修改 | `app/us_pipeline/emergency_interceptor.py` | +15 | 异动回调 |

**总计**: ~900行新建 + 100行修改

---

## 7. 时效性对比 (v2 → v3)

| 场景 | v2延迟 | v3延迟 | 改善 |
|------|--------|--------|------|
| 富途被证监会监管 → 系统知道 | 5-35分钟 | **30秒-2分钟** (行情先行) | **10-50倍** |
| NVDA财报超预期 → 系统知道 | 3-10分钟 (NewsNow) | 3-10分钟 (不变) | — |
| 证监会处罚公告 → 系统知道 | 不可检测 | **2-7分钟** (CSRC源) | **从0到1** |
| 港股公司公告 → 系统知道 | 不可检测 | **2-5分钟** (HKEX源) | **从0到1** |
| 盘中突发暴跌 → 系统知道原因 | 只知道暴跌不知道原因 | **1.5分钟** (反向催化) | **从0到1** |

---

## 8. 配置 (完整)

```yaml
llm:
  models:
    # 新增模型
    qwen-max:
      provider: dashscope
      api_key_env: DASHSCOPE_API_KEY
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
      model_name: qwen3.7-max
      timeout: 60
    deepseek-v4:
      provider: deepseek
      api_key_env: DEEPSEEK_API_KEY
      base_url: https://api.deepseek.com
      model_name: deepseek-v4-pro
      timeout: 60
    kimi-k2:
      provider: dashscope
      api_key_env: DASHSCOPE_API_KEY
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
      model_name: kimi-k2.6
      timeout: 60

  features:
    catalyst_macro:      {primary: qwen-max,    fallback: qwen-plus}
    catalyst_risk:       {primary: deepseek-v4, fallback: deepseek-pro}
    catalyst_sentiment:  {primary: kimi-k2,     fallback: qwen-plus}
    catalyst_classifier: {primary: qwen-max,    fallback: qwen-plus}
    catalyst_review:     {primary: deepseek-v4, fallback: deepseek-pro}
    catalyst_breaking:   {primary: qwen-flash,  fallback: qwen-plus}

catalyst:
  enabled: true
  premarket:
    scan_news_hours: 12
    min_severity: HIGH
    debate_timeout_seconds: 180
    consensus_threshold: 0.4
  postmarket:
    enabled: true
    generate_lessons: true
  reverse_catalyst:
    enabled: true
    min_change_pct: 0.03         # 3%异动触发反向催化
    quick_debate_timeout: 60     # 快速辩论60秒
  sources:
    hkex:
      enabled: true
      interval_seconds: 120      # 2分钟轮询
    csrc:
      enabled: true
      interval_seconds: 300      # 5分钟轮询
    cls_interval_minutes: 2      # 财联社从5分钟缩短到2分钟
  sector_mapping:
    chinese_adr: ["FUTU","BABA","PDD","BIDU","JD","NIO","LI","XPEV"]
    ai_chip: ["NVDA","AMD","AVGO","MU","ARM","QCOM"]
    ev: ["TSLA","RIVN","LCID","NIO","XPEV","LI"]
    big_tech: ["AAPL","MSFT","GOOGL","AMZN","META"]
    chinese_broker: ["FUTU","TIGR","HLBZ"]
```

---

## 9. 风险与对策

| 风险 | 对策 |
|------|------|
| 行情先行误触发 (正常波动被当异动) | min_change_pct=3%门槛 + quick_debate需找到新闻才生成结论 |
| HKEX/CSRC 网站结构变化导致爬取失败 | SourceAdapter 异常安全, 失败不影响主流程 |
| kimi-k2.6 API 不可用 | fallback: qwen-plus (已配置) |
| 催化事件过多导致辩论耗时 | 只对severity≥HIGH辩论, MEDIUM直接评分 |
| 反向催化在盘中生成信号 | 盘中只存储结论, 不注入交易管线 (用户要求) |

---

## 10. 验证计划

### Gate 5 清单
- [ ] HKEXSource 能爬取港交所公告并正确过滤 watchlist 标的
- [ ] CSRCSource 能爬取证监会公告并匹配关键词
- [ ] 财联社轮询间隔已缩短到2分钟
- [ ] event_classifier 正确识别 "证监会对富途监管" → REGULATORY, CRITICAL, [US.FUTU]
- [ ] catalyst_debate 3模型(qwen3.7-max + deepseek-v4-pro + kimi-k2.6)全部返回有效JSON
- [ ] 反向催化: 模拟3%异动 → 搜索新闻 → 快速辩论 → 存储结论 (≤90秒)
- [ ] 盘后回看正确计算命中率并写入 lessons
- [ ] 无催化事件时盘前计划不受影响

### 影子测试
- 部署后 `dry_run: true` 运行1周
- 启用条件: 辩论方向准确率 ≥ 55% + 反向催化响应时间 ≤ 90秒
