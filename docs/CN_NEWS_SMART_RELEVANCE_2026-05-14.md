# 智能新闻相关性引擎 — 替代纯关键词

> **日期**: 2026-05-14
> **背景**: 用户反馈"仅靠关键词获取新闻太死板"
> **目标**: 不引入新依赖, 比关键词智能 5x, 完全可控可审计

## 调研结论

### 项目内已有 (已 grep 验证)
| 模块 | 现状 |
|-----|------|
| `app/news_v2/quality/relevance_filter.py` (211 行) | 已实现关键词 + 板块 + GEO_TRANSMISSION 板块传导. 但**3 个 CN 新闻源各自硬编码关键词没共用** |
| `app/news_v2/quality/dedup_v2.py` | 基于 hash, 不智能 |
| 项目 deps (requirements.txt) | jieba/spacy/sentence-transformers/sklearn 全部**未安装** |

### 国内可达开源方案 (调研结果)
| 方案 | 优点 | 缺点 | 国内可达 |
|------|------|------|---------|
| **jieba** | 纯 Python (~3MB), 中文分词成熟 | 仅分词, 不做实体消歧 | ✅ |
| **spaCy + zh_core_web_sm** | NER 强 | 模型 80MB, 依赖 cython | ✅ pip 国内镜像 |
| **sentence-transformers** | 语义相似度, 支持中英 | 模型 70-430MB | ⚠ HuggingFace 镜像 |
| **m3e-small** | 中英专精, 70MB | 需 sentence-transformers | ✅ hf-mirror.com |
| **百度 LAC** | 国内大厂, 经过验证 | 需 paddlepaddle (~300MB) | ✅ |

**结论**: 用户拒绝引入新依赖, 故采用 **纯 Python 4 层 funnel** + 留 embedding hook (用户后续可选装)。

---

## 新方案 — 4 层智能 funnel

```
原始新闻 (1000+/日)
   ↓
[L0 TickerDictionary] 动态词典 (yaml + watchlist + trade_records 历史 + DEFAULT_ENTITIES)
   ├─ NVDA: names=[NVDA, Nvidia, 英伟达] products=[H100, CUDA, ...] people=[黄仁勋]
   ├─ AAPL: names=[Apple, 苹果] products=[iPhone, Mac, ...] people=[Tim Cook, 库克]
   └─ ... 14 个内置标的, yaml 可扩展
   ↓
[L1 NER + 词典反查] 抽 ticker / 名称 / 中文别名
   ↓
[L2 上下文消歧] 核心改进
   ├─ "苹果发新品" + "iPhone/Cook" → AAPL ✓ 高分
   ├─ "苹果汁价格" → AAPL 命中名字但歧义 + 无信号词 → 降级 0.3x
   └─ "苹果发布Q4财报" → 财经语境词 (财报) → 视为消歧通过
   ↓
[L3 板块传导] (复用现有 GEO_TRANSMISSION)
   ├─ "TSMC 跌" → NVDA, AMD 间接关联
   ├─ "美联储加息" → all_affected
   └─ "OPEC 减产" → XOM, CVX 受益, DAL/UAL 受损
   ↓
[L4 综合评分] (0-1, 含 evidence 链)
   ├─ overall_score = max(ticker_score) + macro_bonus + watchlist_bonus
   └─ rejection_reasons 可审计 (比 LLM 黑盒可控)
   ↓
[Optional: Embedding L5] 留接口未启用
   ↓
[Optional: LLM L6] 已有 (llm_features.news_classifier)
```

---

## 4 层各自职责

### L0 TickerDictionary (`app/news_v2/quality/ticker_dictionary.py`)
**核心**: 每个 ticker 维护完整实体词集 (不仅名字, 含产品/CEO/竞品)

```python
@dataclass
class TickerEntity:
    ticker: str
    names: List[str]           # ["NVDA", "Nvidia", "英伟达"]
    products: List[str]        # ["H100", "CUDA", "Blackwell", "Hopper"]
    people: List[str]          # ["Jensen Huang", "黄仁勋"]
    competitors: List[str]
    sector: str
```

**14 个内置标的** (NVDA/AAPL/TSLA/MSFT/GOOG/META/AMZN/AMD/INTC/TSM/AVGO/BABA/PDD/NIO), 每个含中英文名 + 5-15 个产品 + CEO + 竞品.

**自动扩展**:
- yaml `news_relevance.entities.<TICKER>` 静态映射 (用户可加)
- 启动从 `trade_records.db` 拉历史交易过的 ticker (即使无静态词集也至少有 ticker 名)
- 反向索引: name→ticker, signal_word→tickers

### L1 NER + 词典反查
**做法** (无 NLP 库, 纯 re):
```python
# (a) 直接 ticker (大写 2-5 字母, word boundary)
re.finditer(r"\b([A-Z]{2,5})\b", text)

# (b) 名称/别名 (各 ticker 的所有 variants)
for variant in entity.all_name_variants():
    if variant in text or variant.upper() in text.upper():
        match!
```

**效果**: 比纯关键词智能, 支持每个 ticker 多名称变体.

### L2 上下文消歧 (核心创新)
**问题**: "苹果"既是公司也是水果. 纯关键词无法区分.

**解法**:
```python
if name_hit and signal_words_hit:
    score = unambiguous (0.6) + 0.15 * count
    is_disambiguated = True

elif name_hit and AMBIGUOUS_NAMES contains name:
    if FINANCIAL_CONTEXT_KEYWORDS in text:
        # "苹果发布Q4财报" 财经语境 → 视为消歧通过
        视为 unambiguous + [financial_context] signal
    else:
        # "苹果汁价格" 无线索 → 降级 0.3x
        score *= 0.3

elif name_hit and not AMBIGUOUS_NAMES:
    # "Nvidia" 不歧义, 直接给 unambiguous 分
    score = unambiguous
```

**财经语境词** (`FINANCIAL_CONTEXT_KEYWORDS`): 财报/EPS/营收/盘前/盘后/股价/期权/上调/下调/做空/纳指/标普/earnings/revenue/...

**例子**:
| 新闻 | 旧关键词 | 新智能 |
|------|---------|-------|
| "苹果汁好喝" | ❌ AAPL 命中 (false positive) | ✅ 歧义降级, score=0.09, 不进 primary |
| "苹果发布iPhone16" | ❌ 命中但无 signal_word 验证 | ✅ iPhone 信号词命中, score=0.75, primary |
| "苹果Q4财报超预期" | ❌ 同上 | ✅ 财经语境消歧, score=0.55, primary |
| "贵州茅台股价创新高" | ❌ 命中 "股价" 噪音 | ✅ 无 ticker 命中且无 GEO, 不相关 |

### L3 板块传导
**复用** `RelevanceFilter.GEO_TRANSMISSION` 字典:
```python
"china_tension": {
    "keywords": ["china tension", "taiwan", "south china sea"],
    "victims": ["BABA", "PDD", "JD", "NIO", "TSM", "BIDU"],
}
```

新闻提到 "China tension over Taiwan" → 自动关联 `related_tickers=[BABA, PDD, TSM, ...]`, 即使新闻文本没直接提这些 ticker.

### L4 综合评分
```python
overall_score = max(per_ticker_score)
              + macro_bonus (0.35 if 命中 CPI/Fed/FOMC)
              + watchlist_bonus (0.20 if primary 含 watchlist)

is_relevant = score >= 0.4 OR macro_hit OR len(primary_tickers) > 0
```

每个 RelevanceResult 含完整 evidence:
- `matches: {ticker: TickerMatch(name_hit, signal_words_hit, is_disambiguated)}`
- `primary_tickers / related_tickers / sectors / macro_hit`
- `rejection_reasons` (失败时人类可读原因)

**可审计** = 比 LLM 黑盒可控.

---

## 接入 3 个新闻源

### Before (旧关键词逻辑)
```python
# cailianpress_source.py
matched_kws = [kw for kw in DEFAULT_KEYWORDS_CN if kw in full_text]
matched_tickers = self._extract_tickers(full_text)
if not matched_kws and not matched_tickers:
    continue  # 丢
```

### After (智能 4 层)
```python
try:
    rel = get_smart_relevance().evaluate(title, content)
    if not rel.is_relevant:
        continue
    matched_tickers = rel.primary_tickers
    matched_kws = ["smart_relevant"] if rel.macro_hit else []
except Exception:
    # 降级: 旧关键词逻辑 (向后兼容)
    ...
```

3 个源 (cailianpress / wallstreetcn / eastmoney_us) 全部接入, 失败自动降级旧逻辑。

---

## L5 Embedding (可选, 留 hook)

`app/news_v2/quality/embedding_matcher.py` 已实现接口框架, 默认 disabled.

**启用步骤**:
```bash
# 1. 装 sentence-transformers
pip install sentence-transformers

# 2. 配 yaml
yq -i '.news_relevance.embedding.enabled = true' config/unified_config.yaml

# 3. 重启 — 首次自动从 hf-mirror.com 下 m3e-small (70MB)
systemctl restart ftseek-us
```

**国内镜像**: 默认走 `https://hf-mirror.com` (国内可达), 启动后离线使用.

**何时用**: SmartRelevance 评分边界 0.3-0.5 时, embedding 二次确认 (Phase B 可加).

---

## 测试结果

`tests/coordinator/test_smart_relevance.py`: **17/17 全绿**
全 coordinator 回归: **124/124 全绿** (124 = 107 之前 + 17 智能新闻)

**关键测试覆盖**:
- ✅ 中文别名反查 (英伟达 → NVDA)
- ✅ 信号词反查 (iPhone → AAPL)
- ✅ 上下文消歧 (Apple+iPhone 高分 vs 苹果汁低分)
- ✅ 财经语境消歧 (苹果Q4财报 → AAPL primary)
- ✅ 中文宏观词 (美联储/鲍威尔)
- ✅ 板块传导 (China tension → BABA/PDD/TSM)
- ✅ Embedding lazy load 不崩 (无 sentence-transformers 时)
- ✅ 3 个新闻源接入后过滤逻辑

---

## 与原方案对比

| 能力 | 旧关键词方案 | 新智能方案 |
|------|-----------|---------|
| 名字识别 | ✅ 静态 list | ✅ 动态 14+ 标的 + yaml 扩展 |
| 中英文支持 | ⚠ 各源各自定义 | ✅ 统一引擎 |
| 上下文消歧 | ❌ "苹果汁" 误报 | ✅ AMBIGUOUS_NAMES + signal words |
| 财经语境理解 | ❌ | ✅ FINANCIAL_CONTEXT_KEYWORDS |
| 间接关联 | ⚠ 仅 RelevanceFilter 内用 | ✅ GEO_TRANSMISSION 复用到所有源 |
| 可审计 | ❌ 命中即放行 | ✅ evidence 链 + rejection_reasons |
| 可扩展 | ❌ 改代码 | ✅ yaml + DB 自动汇入 |
| 引入依赖 | 0 | 0 (核心层) |
| Embedding 升级路径 | 无 | ✅ 留 hook + 国内镜像配好 |

---

## 后续路线 (Phase B)

1. **从 Tushare/AKShare 自动构建词典** — 扩到全美股市场 (1000+ 标的)
2. **启用 Embedding** — 装 sentence-transformers + m3e-small (70MB), 解决"间接相关"
3. **接 LLM L1 (新闻分类器)** — 已有 (yaml `llm_features.news_classifier.enabled=true`)
4. **跨语言新闻去重** — 中文/英文同事件合并 (用 embedding 余弦相似度)
5. **NER 升级** — 装 spaCy `zh_core_web_sm` 替代纯 re (更精准识别公司名/人名)

---

**结论**: 4 层 funnel 在不引入新依赖前提下, 用上下文消歧 + 板块传导 + 财经语境 3 个机制, 大幅提升新闻筛选智能度. 124 单测全绿. 用户可后续按需启用 Embedding / LLM 增强层.
