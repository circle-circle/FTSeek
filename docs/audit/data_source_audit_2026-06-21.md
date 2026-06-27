# FTSeek-US 外部数据源准确性、新鲜度与时效性审计报告

**审计时间**: 2026-06-21
**审计范围**: /opt/ftseek/FTSeek-US/
**审计对象**: 所有外部数据获取点、缓存策略、过期处理、对决策的影响

---

## 执行摘要

系统有 **6 大类外部数据源**，共发现 **40+ 个数据获取点**。整体架构采用了多源降级链路设计，但存在若干时效性风险：

| 严重级别 | 问题数 | 关键发现 |
|---------|-------|---------|
| 🔴 P0   | 3     | 基本面/情绪因子无新鲜度标注；StockTwits 无缓存；数据源 provider 未接入 DegradationMonitor |
| 🟡 P1   | 5     | VIX 缓存硬超时后退化为 None；Finnhub 非交易时段 24h 缓存掩盖盘后异动；SEC Form4 解析无降级路径告警 |
| 🟢 P2   | 4     | 数据源优先级链路不统一；fetch_at 标注不一致；LLM 投票缓存 5min 与行情波动阈值耦合 |

**关键风险**: factor_engine.py 中的 `_fetch_fundamental_score` 和 `_fetch_sentiment_crowding` 是 P2 (2026-06-21) 新增的因子，权重占 23%，但两者 **均无 fetched_at 标注**，且 sentiment (StockTwits) **无缓存**，每次因子评分都发起新请求。

---

## 数据源 1: Finnhub REST API（基本面 + 报价 + 新闻情绪）

### 调用点清单

| # | 文件 | 行号 | 接口 | TTL | 过期行为 |
|---|------|------|------|-----|---------|
| 1 | `app/data/finnhub_provider.py` | L194-273 | `get_quote` | 自适应：交易60s/盘前盘后30min/深夜24h/周末24h | 返回 None，降级到 Futu |
| 2 | `app/data/finnhub_provider.py` | L275-299 | `get_company_news` | 7200s (2h) | 返回 [] |
| 3 | `app/data/finnhub_provider.py` | L301-341 | `get_sentiment_score` | 3600s (1h) | 返回 0.0（中性） |
| 4 | `app/data/finnhub_provider.py` | L343-381 | `get_market_news_summary` | 7200s (2h) | 返回 {sentiment:0.0, count:0} |
| 5 | `app/data/finnhub_provider.py` | L390-460 | `get_kline` | 120s | 返回 None |
| 6 | `app/data/finnhub_provider.py` | L462-495 | `get_market_news` | 3600s (1h) | 返回 [] |
| 7 | `app/data/finnhub_provider.py` | L499-522 | `get_earnings_calendar` | 21600s (6h) | 返回 [] |
| 8 | `app/data/finnhub_provider.py` | L524-541 | `get_company_earnings` | 86400s (24h) | 返回 [] |
| 9 | `app/data/finnhub_provider.py` | L543-560 | `get_recommendation_trends` | 86400s (24h) | 返回 [] |
| 10 | `app/data/finnhub_provider.py` | L562-575 | `get_company_peers` | 86400s (24h) | 返回 [] |
| 11 | `app/data/finnhub_provider.py` | L577-598 | `get_basic_financials` | 86400s (24h) | 返回 {} |
| 12 | `app/data/premarket_enrichment.py` | L256-293 | `_collect_sector_rotation` (client.quote) | 模块级 3600s | 返回 {} |
| 13 | `app/data/premarket_enrichment.py` | L296-347 | `_collect_premarket_movers` (client.quote) | 模块级 3600s | 返回 {} |
| 14 | `app/data/premarket_enrichment.py` | L373-417 | `_collect_analyst_sentiment` (client.recommendation_trends) | 模块级 3600s | 返回 {} |
| 15 | `app/data/premarket_enrichment.py` | L420-452 | `_collect_earnings_calendar` (client.earnings_calendar) | 模块级 3600s | 返回 "Unable to fetch" |
| 16 | `app/data/premarket_enrichment.py` | L455-469 | `_collect_market_news` (client.general_news) | 模块级 3600s | 返回 [] |
| 17 | `app/plan/factor_engine.py` | L494-584 | `_fetch_fundamental_score` (stock/metric) | **300s (5min)** 函数属性缓存 | 返回 (0.5, "no api key"/"error") |
| 18 | `app/plan/factor_engine.py` | L102-208 | `_discover_finnhub_movers` (client.quote) | **无缓存** | 返回 [] |
| 19 | `app/plan/factor_engine.py` | L211-290 | `_apply_quality_gate` (client.quote + profile2) | **无缓存** | 返回原 symbols（全通过） |
| 20 | `app/data/providers/insider_provider.py` | L85-133 | `_fetch_finnhub` (stock/insider-transactions) | 继承 BaseDataProvider 86400s | 返回 failed |

### 详细分析

**1. API 限速**: 60 calls/min（令牌桶实现，L49-79），429 时 sleep 5s 返回 None。
**2. 认证**: Header-based (`X-Finnhub-Token`)，防 token 泄露 (P1-8 修复 L177-178)。
**3. TLS**: `verify=False`（L180），已知 Finnhub 免费 API 的 TLS 问题，已禁用 urllib3 警告 (L23-24)。

**🔴 P0 风险 - 基本面因子无新鲜度标注**:
- `factor_engine.py` L494-584 `_fetch_fundamental_score` 使用函数属性做缓存（`setattr(_fetch_fundamental_score, f"_fund_cache_{symbol}", ...)` L578），**无 fetched_at 字段**，无法追溯数据时间。
- 该因子在 REGIME_WEIGHTS 中权重 0.12-0.13（L58-61），直接影响方向选择。
- 降级返回 0.5（中性分），但 **不记录降级事件到 DegradationMonitor**，系统外部不可见。

**🟡 P1 风险 - 非交易时段 24h 缓存**:
- `get_quote` 在 WEEKEND/OVERNIGHT 时 TTL=86400s（L223-225），这意味着周六凌晨获取的价格可能用到周日全天。
- 注释称"周末价格不变"，但 **未考虑盘后重大新闻（如地缘事件）导致的期货异动**。
- 对决策影响：周一开盘前若仍用周末缓存价格，factor_engine 的 `relative_strength` 因子会失真。

**🟡 P1 风险 - premarket_enrichment 模块级缓存跨进程失效**:
- `_cache: Dict` (L48) 是模块级变量，**进程重启后丢失**，但 `_cache_ts` 也重置，所以会重新获取——这点 OK。
- 真正风险：`collect_premarket_data` 被 factor_engine 通过 `from app.data.premarket_enrichment import _cache as pm_cache` (factor_engine L119) 直接读取，**如果 premarket_enrichment 未被先调用，_cache 为空**，factor_engine 的 Source 1 发现会失败但不报错（L130-131 except 静默）。

### 对决策的影响
- **factor_engine 8 因子中 3 个依赖 Finnhub**：fundamental_quality (13%)、insider_signal (12%)、sector_momentum (8-24%)。
- Finnhub 完全不可用时，这些因子降级为 0.5/0.5/0，**系统仍会出信号**，但质量显著下降。
- **无数据健康检查**针对 Finnhub 可用性（DegradationMonitor 未接入 FinnhubProvider）。

---

## 数据源 2: StockTwits API（情绪数据）

### 调用点清单

| # | 文件 | 行号 | 接口 | TTL | 过期行为 |
|---|------|------|------|-----|---------|
| 1 | `app/plan/factor_engine.py` | L587-636 | `_fetch_sentiment_crowding` | **无缓存** | 返回 (0.5, "no data"/"error") |
| 2 | `app/plan/analysts/sentiment_analyst.py` | L176-223 | `_fetch_stocktwits` | **无缓存**（但有 30s asyncio 超时） | 返回 None |

### 详细分析

**🔴 P0 风险 - 无缓存 + 无新鲜度标注**:
- factor_engine 的 `_fetch_sentiment_crowding` (L587-636) **每次因子评分都直接调用 StockTwits API**，无任何缓存。
- StockTwits 免费 API **无官方限速文档**，但高频调用可能被封 IP。
- 在 `run_factor_screen` 中，对 pool 中每只股票调用两次（LONG + SHORT，L796-797），若 pool=35 只，则 **70 次 API 调用/次评分**。
- 该因子权重 0.08-0.10（L58-61），影响方向冲突仲裁（L832-843 的 `_DIRECTION_DIFF_THRESHOLD` 比较）。

**数据新鲜度**: StockTwits 返回的 `messages` 包含 `created_at` 字段，但代码 **未检查消息时间**，可能用到数天前的旧消息（L612 `messages[:30]` 只取前 30 条，不按时间过滤）。

**降级路径**: API 失败返回 (0.5, "error")（L636），**不记录到 DegradationMonitor**。

### 对决策的影响
- 情绪拥挤度因子在 RISK_OFF regime 权重 0.08（最低），影响较小。
- 但在 NEUTRAL regime 权重 0.11，且参与方向选择仲裁（L823 short_composite 计算）。
- **StockTwits 宕机时，所有股票情绪分=0.5，方向仲裁退化为纯技术面比较**，可能误判。

---

## 数据源 3: Yahoo Finance（价格 + VIX）

### 调用点清单

| # | 文件 | 行号 | 接口 | TTL | 过期行为 |
|---|------|------|------|-----|---------|
| 1 | `app/data/yahoo_provider.py` | L25-69 | `get_quote` (yf.Ticker.fast_info) | 900s (15min) | 返回 None |
| 2 | `app/data/yahoo_provider.py` | L71-134 | `get_market_data` (ticker.history 60d) | 300s (5min) | 返回 None |
| 3 | `app/data/yahoo_provider.py` | L175-218 | `get_spy_context` (SPY+QQQ history) | 900s (15min) | 返回 {} |
| 4 | `app/data/yahoo_provider.py` | L220-271 | `compute_atr` (ticker.history 60d) | 1800s (30min) | 返回 {atr:0,...} |
| 5 | `app/data/yahoo_provider.py` | L165-173 | `get_vix` | **硬编码返回 20.0** | 永远返回 20.0 |
| 6 | `app/risk/vix_monitor.py` | L181-196 | `_fetch_vix_fred` | 180s + 硬超时 3600s | 返回 None |
| 7 | `app/risk/vix_monitor.py` | L198-223 | `_fetch_vix_alphavantage` | 同上 | 返回 None |
| 8 | `app/risk/vix_monitor.py` | L225-251 | `_fetch_vix_finnhub` (VIXY 代理) | 同上 | 返回 None |
| 9 | `app/risk/vix_monitor.py` | L253-269 | `_fetch_vix_fallback` (Yahoo ^VIX) | 同上 | 返回 None |
| 10 | `app/plan/analysts/sentiment_analyst.py` | L142-161 | Yahoo ^VIX (requests) | 无缓存 | 返回 None |

### 详细分析

**🟡 P1 风险 - VIX 多级降级 + 硬超时退化为 None**:
- `vix_monitor.py` 实现 4 级降级：FRED → AlphaVantage → Finnhub(VIXY代理) → Yahoo (L157-164)。
- 缓存 TTL=180s（L51），硬超时 3600s（L52）。
- **硬超时后返回 None**（L150-151），触发 `check_vix_level` 返回 WARNING（L336-342）："VIX 数据源不可用，已启用保守风控模式"。
- 这是**正确的保守降级**，但 conservative 模式仅是建议"降低仓位至50%"，**不硬性阻止交易**。
- 退避策略：指数退避 60s→480s（L55-57, L81-101）。

**🔴 P0 风险 - YahooFinanceProvider.get_vix 硬编码 20.0**:
- `yahoo_provider.py` L165-173 `get_vix` **直接返回 20.0**，注释称"当前网络环境下 yfinance 无法访问 ^VIX"。
- 这意味着 `get_full_market_context` (L273-349) 中的 `market_regime` 判断 (L305-314) **永远基于 VIX=20.0**，除非外部注入。
- 但 `vix_monitor.py` 有独立的 VIX 获取逻辑，**两套 VIX 数据源不统一**，存在一致性风险。

**数据新鲜度标注**: Yahoo provider 的 quote 有 `timestamp` 字段（L57），但 **无 source 标注**（与 Finnhub/Futu 的 `source` 字段不统一）。

### 对决策的影响
- VIX 用于 `market_regime` 判断（HIGH_VOL/TREND_UP/...），影响 REGIME_WEIGHTS 选择（factor_engine L57-62）。
- VIX 数据不可用时，factor_engine 的 `_detect_regime` (L639-658) 仅依赖 sector_momentum 和 spy_change，**VIX 因子缺失**。
- Yahoo 的 15min 延迟数据在实盘交易中**不适合止损/止盈判断**（已在 market_data_router 中降为备选）。

---

## 数据源 4: Futu OpenD（实时行情 + 持仓 + 扩展数据）

### 调用点清单

| # | 文件 | 行号 | 接口 | TTL | 过期行为 |
|---|------|------|------|-----|---------|
| 1 | `app/data/futu_lv3_provider.py` | L646-772 | `get_quote` (get_market_snapshot) | 5s + push 缓存 | 降级到 Finnhub fallback |
| 2 | `app/data/futu_lv3_provider.py` | L806+ | `get_batch_quote` | 5s | 返回 {} |
| 3 | `app/data/futu_lv3_provider.py` | L774+ | `get_full_market_context` | 混合 | 返回 None |
| 4 | `app/data/futu_lv3_provider.py` | K线 | `get_cur_kline` | 60s (M6 修复) | 返回 None |
| 5 | `app/data/futu_lv3_provider.py` | 日线 | daily kline cache | 24h | 返回 None |
| 6 | `app/data/providers/futu_extended_provider.py` | L66-114 | `get_short_interest` | 3600s (1h) | 返回 failed |
| 7 | `app/data/providers/futu_extended_provider.py` | L116-148 | `get_daily_short_volume` | 3600s | 返回 failed |
| 8 | `app/data/providers/futu_extended_provider.py` | L150-206 | `get_analyst_consensus` | 3600s | 返回 failed |
| 9 | `app/data/providers/futu_extended_provider.py` | L208-295 | `get_insider_trades` | 3600s | 返回 failed |
| 10 | `app/data/premarket_enrichment.py` | L179-253 | `_collect_futu_market_data` | 模块级 3600s | 返回 None |
| 11 | `app/plan/factor_engine.py` | L358-430 | `_compute_technicals` (get_cur_kline K_DAY) | 无缓存 | 返回 None |
| 12 | `app/web_dashboard/manual_order_service.py` | L145-196 | `connect` (OpenQuoteContext + OpenSecTradeContext) | 连接级 | 返回 False |

### 详细分析

**✅ 亮点 - FutuLV3 有完整健康检查**:
- `is_healthy()` (L1188-1230) 检查：连接状态 + 非交易时段 + push 新鲜度（30s stale_threshold）。
- `_last_push_time` 跟踪 (L122)，`_stale_threshold_sec=30.0` (L123)。
- 指数退避重连 (L1264+)：2→4→8→16→32→60s。
- 断线统计 + 日志 (L1232-1262)。

**🟡 P1 风险 - FutuExtendedProvider 缓存键与 BaseDataProvider 不兼容**:
- `futu_extended_provider.py` 使用 `self._is_cache_valid(cache_key)` (L84, L120, L173, L226)，但 `BaseDataProvider._is_cache_valid` (base.py L53-60) 期望 `self._cache[symbol]` 包含 `fetched_at` 字段。
- 然而 `get_short_interest` 等方法缓存的是 `result` dict（含 `fetched_at`），但 key 是 `f"si_{symbol}"` 而非 `symbol`。
- `BaseDataProvider._is_cache_valid` 的参数是 `symbol`，但这里传的是 `cache_key`，**逻辑上能工作**（因为都是字符串 key），但 **`get_with_cache` (base.py L72-82) 不会被子类调用**——子类直接实现了自己的缓存逻辑，绕过了基类。
- 结果：`registry.get()` → `provider.get_with_cache()` → 检查基类缓存（key=symbol）→ 未命中 → 调用 `get_data()` → 内部又检查自己的缓存（key=si_symbol）→ 可能重复获取。

**数据新鲜度标注**: FutuLV3 quote 有 `data_source` (L729) 和 `timestamp` (L730)；FutuExtended 有 `fetched_at` (ISO 格式)。**标注较完整**。

### 对决策的影响
- Futu 是**主数据源**（market_data_router L172-184），5s 缓存 + push 推送，延迟 <10ms。
- factor_engine 的 5 个技术因子（sector_momentum 除外）**全部依赖 Futu K线**（L358-430 `_compute_technicals`）。
- Futu 断连时，降级到 Finnhub（15min 延迟），**技术因子实时性显著下降**，但 factor_engine 不检查 Futu 健康状态，仍会计算因子。
- `premarket_enrichment` 的 Futu analyst consensus 补充了 Finnhub 不提供的**目标价数据**（L472-505）。

---

## 数据源 5: SQLite DB（信号快照 + 缓存）

### 数据库清单

| # | DB 路径 | 表 | 用途 | TTL/过期 |
|---|---------|-----|------|---------|
| 1 | `data/signal_snapshots.db` | signal_snapshots | 信号时刻价格 + 前瞻收益回填 | 无 TTL，永久存储 |
| 2 | `data/social_sentiment.db` | sentiment_cache | StockTwits/Reddit/HN 情绪 | 14400s (4h) |
| 3 | `data/ftseek_us.db` | open_positions, position_snapshots, metrics | 交易记录 + 持仓 | 无 TTL |
| 4 | `data/ftseek_us_live.db` | shadow_trades, signal_lifecycle | 影子交易 + 信号生命周期 | 无 TTL |
| 5 | `data/decisions.db` | decisions, model_votes | LLM 投票记录 | 无 TTL |
| 6 | `data/llm_audit.db` | llm_calls | LLM 调用审计 | 无 TTL |
| 7 | `data/vix_cache/vix_data.json` | (文件) | VIX 缓存 | 180s + 硬超时 3600s |
| 8 | `data/sec_cache/` | (文件) | SEC Form4 缓存 | 3600s (1h) |

### 详细分析

**✅ 亮点 - social_sentiment.db 有 fetched_at 标注**:
- `social_sentiment.py` L42-44: `sentiment_cache` 表含 `fetched_at REAL NOT NULL`。
- L58: `(time.time() - row["fetched_at"]) < CACHE_TTL` 检查过期。
- 但 `sentiment_analyst.py` 的 `_fetch_reddit` (L226-283) 查询的是 `sentiment` 表（非 `sentiment_cache`），**表名不匹配**，可能查询失败返回 None。

**🟡 P1 风险 - signal_snapshots 无数据新鲜度机制**:
- `signal_snapshots` 表存储信号时刻价格（`price_at_signal`）用于 IC 回填，但 **无数据源标注**，无法追溯价格来自 Futu 还是 Finnhub。
- 前瞻收益回填（`close_t0/t1/t5/t20`）依赖后续价格获取，**若数据源在回填时不可用，realized 字段保持 0**，IC 计算会遗漏该样本。

**🟢 P2 风险 - VIX 文件缓存无原子写入**:
- `vix_monitor.py` L123-128 `_save_cache` 直接 `json.dump`，**无原子写入**（write-to-temp + rename），进程崩溃可能产生损坏的 JSON。

### 对决策的影响
- social_sentiment.db 4h 缓存意味着 **盘中情绪数据可能滞后 4h**，但该数据用于 deep_research 分析（非实时决策），影响较小。
- signal_snapshots 的 IC 回填质量直接影响 `ic_tracker` 的因子权重调整建议，**数据源不可追溯会污染 IC 计算**。

---

## 数据源 6: 其他免费 API（SEC EDGAR + FRED + OpenInsider + RSS）

### 调用点清单

| # | 文件 | 行号 | 接口 | TTL | 过期行为 |
|---|------|------|------|-----|---------|
| 1 | `app/data/sec_form4_provider.py` | L100-109 | SEC EDGAR (urlopen) | 3600s (1h) | 返回 [] |
| 2 | `app/data/sec_form4_provider.py` | L111-141 | `company_tickers.json` | 7天 (文件 mtime) | 重新获取 |
| 3 | `app/data/premarket_enrichment.py` | L538-601 | FRED Treasury yields (requests) | 模块级 3600s | 返回 {} |
| 4 | `app/data/premarket_enrichment.py` | L604-634 | OpenInsider HTML 抓取 | 模块级 3600s | 返回 {error:..., total_buys_7d:0} |
| 5 | `app/data/news_provider.py` | L97-145 | RSS feeds (feedparser) | 900s (15min) | 返回 [] |
| 6 | `app/plan/analysts/sentiment_analyst.py` | L125-173 | Fear&Greed (alternative.me) + Yahoo VIX | 无缓存 | 返回 None |

### 详细分析

**✅ 亮点 - SEC Form4 有完整的数据建模和集群检测**:
- `sec_form4_provider.py` 实现 Form 4 XML 解析 (L231-304)、集群买入检测 (L383-487)、评分 (L459-465)。
- 限速 10 req/s (L44, L93-98)，符合 SEC 官方限制。
- ticker→CIK 映射缓存 7 天 (L116-117)。

**🟡 P1 风险 - OpenInsider HTML 抓取脆弱**:
- `premarket_enrichment.py` L604-634 `_collect_insider_trading` 通过正则解析 OpenInsider HTML，**网站改版会立即失效**。
- 无降级到 SEC EDGAR（虽然 `insider_provider.py` 有 Finnhub 降级，但 premarket_enrichment 直接调用的是 OpenInsider，不经过 provider registry）。

### 对决策的影响
- SEC Form4 数据通过 `insider_provider.py` → `enrichment_router.get_insider()` → `factor_engine` L756-778 使用，权重 0.12。
- insider 买入是"强看多信号"（历史胜率 60%+，insider_provider.py L8 注释），**数据源失效会降级为 0.5（中性）**。
- FRED Treasury yields 用于 `premarket_enrichment` 的 `curve_shape` 判断（L578-581 倒挂=衰退信号），影响 LLM debate context。

---

## 缓存策略配置汇总

### config/unified_config.yaml 中的 TTL 配置

| 配置路径 | 值 | 实际使用位置 |
|---------|-----|------------|
| `llm_voting.cache_ttl_seconds` | 300 (5min) | LLM 投票缓存（原 1800→300，2026-05-31 优化） |
| `llm_voting.price_change_threshold` | 0.008 (0.8%) | 行情变动超此则强制重新投票 |
| `data_sources.twelvedata.cache_ttl.quote` | 300 | TwelveData 报价 |
| `data_sources.twelvedata.cache_ttl.timeseries` | 1800 | TwelveData 时间序列 |
| `data_sources.twelvedata.cache_ttl.vix` | 60 | VIX（原 600→60，2026-06-10 修复） |
| `data_sources.twelvedata.cache_ttl.spy` | 900 | SPY 数据 |
| `data_sources.finnhub.cache_ttl.quote` | 60 | Finnhub 报价（但实际代码用自适应 TTL） |
| `data_sources.finnhub.cache_ttl.news` | 7200 | Finnhub 新闻 |
| `data_sources.finnhub.cache_ttl.sentiment` | 3600 | Finnhub 情绪 |
| `data_sources.alphavantage.cache_ttl.vix` | 60 | AlphaVantage VIX |

**🟢 P2 风险 - 配置与代码不一致**:
- `config.yaml` 中 `finnhub.cache_ttl.quote: 60`，但 `finnhub_provider.py` 的 `get_quote` 使用自适应 TTL（60s/30min/24h），**配置值仅在 `__init__` 默认参数中使用**（L129 `cache_ttl_seconds=60`），实际调用时被覆盖。
- `FinnhubProvider.__init__` 接受 `cache_ttl_seconds` 参数，但 `MarketDataRouter.__init__` 传入 `min(300, cache_ttl_seconds)`（market_data_router L102），**配置的 60s 被 override 为 min(300,900)=300s**，与 yaml 中的 60s 不一致。

---

## 数据新鲜度标注机制审计

### 有 fetched_at 标注的数据源

| 数据源 | 标注字段 | 格式 | 位置 |
|-------|---------|------|------|
| FutuExtendedProvider | `fetched_at` | ISO 8601 UTC | futu_extended_provider.py L47, L82, L108... |
| FundamentalProvider | `fetched_at` | ISO 8601 UTC | fundamental_provider.py L60, L95, L179... |
| InsiderProvider | `fetched_at` | ISO 8601 UTC | insider_provider.py L28, L60, L78... |
| FinnhubProvider.get_quote | `timestamp` | ISO local | finnhub_provider.py L261 |
| TwelveDataProvider | `timestamp` | ISO local | twelvedata_provider.py L285 |
| FutuLV3Provider | `timestamp` | Unix float | futu_lv3_provider.py L730 |
| social_sentiment.db | `fetched_at` | Unix float | social_sentiment.py L42 |

### 🔴 无 fetched_at 标注的数据源（P0 风险）

| 数据源 | 位置 | 影响 |
|-------|------|------|
| **factor_engine._fetch_fundamental_score** | L494-584 | 无法追溯 PE/ROE/Revenue 数据时间 |
| **factor_engine._fetch_sentiment_crowding** | L587-636 | 无法追溯 StockTwits 情绪数据时间 |
| **factor_engine._discover_finnhub_movers** | L102-208 | 异动标的发现无时间标注 |
| **factor_engine._get_sector_momentum** | L329-355 | 板块动量无时间标注 |
| **premarket_enrichment._collect_treasury_yields** | L538-601 | FRED 数据无 fetched_at（只有模块级 _cache_ts） |
| **premarket_enrichment._collect_insider_trading** | L604-634 | OpenInsider 数据无 fetched_at |
| **news_provider** | L97-145 | RSS 新闻缓存只有 cache 时间，无 article published_at 校验 |
| **vix_monitor** | L169-174 | 缓存有 timestamp，但 source 字段是 "fred/alphavantage/finnhub/fallback" 混合，**无法追溯实际来源** |

---

## 数据健康检查机制审计

### 已有的健康检查

| 机制 | 位置 | 覆盖范围 | 阈值 |
|------|------|---------|------|
| FutuLV3 `is_healthy()` | futu_lv3_provider.py L1188 | Futu OpenD 连接 + push 新鲜度 | 30s stale |
| VIXMonitor 退避 | vix_monitor.py L81-101 | VIX API 连续失败 | 60→480s 退避 |
| SubscriptionManager 数据质量门控 | subscription_manager.py L696-754 | 行情质量评分 | `_quality_threshold` |
| DegradationMonitor | degradation_monitor.py | 模块级降级计数 | 10次/h |
| Watchdog 心跳 | observability/watchdog.py | 后台线程心跳 | monitor_interval × 5 |

### 🔴 缺失的健康检查

| 缺失项 | 影响 |
|-------|------|
| **FinnhubProvider 可用性监控** | API 失败静默返回 None/[]，无 DegradationMonitor 记录 |
| **StockTwits 可用性监控** | 无缓存 + 无健康检查，API 被封后所有情绪因子=0.5 |
| **factor_engine 数据完整性检查** | 因子降级为 0.5 时不记录，candidate 仍进入 Top N |
| **premarket_enrichment 数据新鲜度检查** | 模块级缓存 1h，但**不检查缓存内数据的实际时间**（如 Finnhub quote 的 timestamp） |
| **跨数据源一致性检查** | Futu 和 Finnhub 的价格可能不一致，无对账机制 |

### DegradationMonitor 接入情况

经搜索，DegradationMonitor 目前仅接入 **4 个点**（全部在 risk 模块）：
- `app/risk/engine.py` L284-285: `risk_engine_position_check`
- `app/risk/pre_trade_gate.py` L199-200: `daily_pnl_query`
- `app/risk/pre_trade_check.py` L300-301: `rfc056_vix_filter`
- `app/risk/pre_trade_check.py` L385-386: `rfc058_long_filter`

**🔴 所有数据源 provider 模块（finnhub_provider, futu_lv3_provider, twelvedata_provider, alphavantage_provider, sec_form4_provider, insider_provider, fundamental_provider, futu_extended_provider, news_provider, social_sentiment, sentiment_analyst）均未接入 DegradationMonitor**。

这意味着数据源静默降级时，系统外部（Dashboard / PushHub 告警）完全不可见，违反了 DegradationMonitor 设计文档注释中宣称的"每个 Provider 独立文件，失败不抛异常，返回 failed"原则——虽然返回了 failed，但**无人记录**。

---

## 关键数据流路径总结

### 路径 1: factor_engine 因子评分（影响交易候选选择）

```
run_factor_screen()
  ├─ _get_stock_pool()
  │   ├─ _load_active_pool() → data/daily_active_pool.json (无 TTL)
  │   ├─ _discover_finnhub_movers() → Finnhub quote (无缓存!) + Finnhub news
  │   └─ _apply_quality_gate() → Finnhub quote + profile2 (无缓存!)
  ├─ _get_sector_momentum() → Finnhub quote (无缓存)
  ├─ _compute_technicals() → Futu K_DAY K线 (无缓存, 直接 subscribe+get_cur_kline)
  ├─ _detect_regime() → 纯计算
  └─ 每只股票评分:
      ├─ insider_signal → enrichment_router.get_insider() → SEC EDGAR / Finnhub (24h TTL)
      ├─ fundamental_quality → _fetch_fundamental_score() → Finnhub stock/metric (5min 函数属性缓存, 无 fetched_at)
      └─ sentiment_crowding → _fetch_sentiment_crowding() → StockTwits API (无缓存! 无 fetched_at)
```

**关键风险**: factor_engine 中有 **4 个数据获取点无缓存**（_discover_finnhub_movers, _apply_quality_gate, _get_sector_momentum, _fetch_sentiment_crowding），每次 `run_factor_screen` 调用都会产生大量 API 请求。

### 路径 2: market_data_router 实时报价（影响止损/止盈）

```
MarketDataRouter.get_quote()
  ├─ FutuLV3Provider.get_quote() → push 缓存 / get_market_snapshot (5s TTL)
  │   └─ fallback → FinnhubProvider.get_quote() (自适应 60s-24h TTL)
  ├─ FinnhubProvider.get_quote() (如果 Futu 失败)
  └─ TwelveDataProvider.get_quote() (如果 Finnhub 也失败, 5min TTL)
```

**数据新鲜度**: Futu < 10ms（实时），Finnhub 15min 延迟（免费计划），TwelveData 实时但限速 8/min。

### 路径 3: premarket_enrichment 盘前数据（影响 LLM debate context）

```
collect_premarket_data() (模块级 1h TTL)
  ├─ _collect_futu_market_data() → Futu get_market_snapshot
  ├─ _collect_sector_rotation() → Finnhub client.quote (11 ETF)
  ├─ _collect_premarket_movers() → Finnhub client.quote (MOVER_POOL 20只)
  ├─ _collect_analyst_sentiment() → Finnhub client.recommendation_trends
  ├─ _collect_futu_analyst_consensus() → FutuExtendedProvider
  ├─ _collect_earnings_calendar() → Finnhub client.earnings_calendar
  ├─ _collect_market_news() → Finnhub client.general_news
  ├─ _get_economic_calendar() → 硬编码 FOMC 日期 (2026 年)
  ├─ _collect_treasury_yields() → FRED CSV API (免费)
  └─ _collect_insider_trading() → OpenInsider HTML 抓取
```

**关键风险**: 模块级缓存 1h，但 **缓存内不标注各子数据源的 fetched_at**，无法判断哪些子项是新鲜的、哪些是过期的。

---

## 改进建议（按优先级排序）

### 🔴 P0 - 立即修复

1. **为 factor_engine 的 `_fetch_fundamental_score` 和 `_fetch_sentiment_crowding` 添加 fetched_at 标注**
   - 在缓存值中增加 `fetched_at` 字段。
   - 在 candidate 输出中包含数据时间，供下游判断。

2. **为 `_fetch_sentiment_crowding` 添加缓存（至少 300s）**
   - StockTwits 无缓存导致每次评分 70 次 API 调用，有被封 IP 风险。
   - 建议 TTL=300s，与 fundamental_score 一致。

3. **将所有数据源 provider 接入 DegradationMonitor**
   - 在每个 provider 的 except 块中调用 `get_degradation_monitor().record("finnhub_quote", str(e))`。
   - 确保 Dashboard /system 页面可见数据源降级状态。

### 🟡 P1 - 近期修复

4. **统一 VIX 数据源**
   - 删除 `YahooFinanceProvider.get_vix` 的硬编码 20.0，改为调用 `VIXMonitor.get_vix()`。
   - 确保 `market_data_router.get_full_market_context` 使用统一的 VIX 数据。

5. **为 factor_engine 的 `_discover_finnhub_movers` 和 `_get_sector_momentum` 添加缓存**
   - 这两个函数每次评分都调用 Finnhub quote，但异动标的和板块动量在分钟级变化不大。
   - 建议 TTL=300s。

6. **为 premarket_enrichment 的缓存增加子项 fetched_at 标注**
   - 每个子数据源（sector_rotation, premarket_movers, analyst_sentiment 等）独立标注获取时间。
   - 供 `format_for_debate` 输出时标注数据新鲜度。

7. **修复 config.yaml 与代码的 TTL 不一致**
   - `finnhub.cache_ttl.quote: 60` 被 MarketDataRouter override 为 300s。
   - 建议：代码从 config 读取 TTL，或在 config 中更新为实际使用的值。

### 🟢 P2 - 中期优化

8. **实现跨数据源价格一致性检查**
   - Futu 和 Finnhub 的价格差异 >1% 时告警，防止用错误价格做止损。

9. **为 signal_snapshots 添加数据源标注**
   - `price_at_signal` 增加 `price_source` 字段（futu_lv3/finnhub/twelvedata）。

10. **VIX 文件缓存实现原子写入**
    - `vix_monitor.py._save_cache` 使用 write-to-temp + rename。

11. **统一所有 provider 的缓存实现**
    - FutuExtendedProvider 绕过了 BaseDataProvider.get_with_cache，应统一为基类缓存逻辑。

---

## 附录: 数据源优先级与降级链路图

```
┌─────────────────────────────────────────────────────────────────┐
│                    实时报价 (get_quote)                          │
├─────────────────────────────────────────────────────────────────┤
│  Futu LV3 (5s TTL, <10ms)                                      │
│    └─ fallback → Finnhub (60s-24h TTL, 15min delay)             │
│         └─ fallback → TwelveData (300s TTL, real-time, 8/min)   │
│              └─ (AlphaVantage 已禁用 2026-04-24)                │
├─────────────────────────────────────────────────────────────────┤
│                    VIX 指数                                     │
├─────────────────────────────────────────────────────────────────┤
│  VIXMonitor: FRED CSV (180s TTL)                               │
│    └─ AlphaVantage (180s TTL)                                   │
│         └─ Finnhub VIXY 代理 (180s TTL)                        │
│              └─ Yahoo ^VIX (180s TTL)                           │
│                   └─ 硬超时 3600s → 返回 None → 保守风控模式    │
│  YahooFinanceProvider.get_vix: 硬编码 20.0 (⚠️ 未使用 VIXMonitor)│
├─────────────────────────────────────────────────────────────────┤
│                    基本面 (PE/ROE/Revenue)                      │
├─────────────────────────────────────────────────────────────────┤
│  FundamentalProvider: Futu snapshot (1h TTL)                    │
│    └─ Finnhub stock/metric (1h TTL)                            │
│         └─ Yahoo (1h TTL, 仅 PE/EPS)                           │
│              └─ AlphaVantage (无基本面 API, 返回 None)           │
│  factor_engine._fetch_fundamental_score: Finnhub (5min TTL) ⚠️  │
├─────────────────────────────────────────────────────────────────┤
│                    内部人交易                                    │
├─────────────────────────────────────────────────────────────────┤
│  InsiderProvider: SEC EDGAR Form4 (24h TTL)                    │
│    └─ Finnhub insider-transactions (24h TTL)                   │
│  FutuExtendedProvider.get_insider_trades: Futu OpenD (1h TTL)   │
│  premarket_enrichment._collect_insider_trading: OpenInsider (1h)│
├─────────────────────────────────────────────────────────────────┤
│                    情绪数据                                      │
├─────────────────────────────────────────────────────────────────┤
│  factor_engine._fetch_sentiment_crowding: StockTwits (无缓存!) ⚠️│
│  sentiment_analyst._fetch_stocktwits: StockTwits (无缓存)        │
│  sentiment_analyst._fetch_fear_greed: alternative.me (无缓存)    │
│  social_sentiment.collect_sentiment: last30days skill (4h TTL)  │
│  FinnhubProvider.get_sentiment_score: Finnhub news (1h TTL)     │
└─────────────────────────────────────────────────────────────────┘
```

---

**审计完成**。报告已保存至 `/opt/ftseek/FTSeek-US/docs/audit/data_source_audit_2026-06-21.md`。
