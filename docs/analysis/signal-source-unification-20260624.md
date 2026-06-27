# 信号源统一分析：重复计算 + 数据源映射 + 漏斗化方案

> **日期**: 2026-06-24
> **目标**: 从OKR盈利/胜率目标倒推，识别两条管线数据采集层的重复、差异、信息损耗，给出漏斗化统一方案
> **方法**: 全量代码级审计，不靠记忆，每个结论有行号证据

---

## 一、重复计算的全景图

### 1.1 原始映射表（用户初始分析 + 代码验证补充）

| 数据维度 | 管线A factor_engine | 管线B Playbook | 重复? | 数据源差异 |
|---------|---------------------|---------------|-------|-----------|
| **日线K线** | `ManualOrderService._quote_ctx.get_cur_kline` (Futu, 60根K_DAY) | `MarketDataRouter.get_full_market_context` (Futu, K_1M 200根) | **❌ 不是重复，是不同K线** | A用日线手算8指标；B用分钟线经LV3TechnicalCalculator算20+指标 |
| **MA5/MA10/MA20** | 自己算 `sum(closes[-5:])/5` (日线收盘价) | MarketDataRouter 的 `ctx.get("ma20")` (LV3TechnicalCalculator, 分钟线) | **⚠️ 数值不一致** | 同一标的的MA20可能不同：日线收盘价 vs 分钟线SMA，方向判断可能相反 |
| **RSI-14** | 自己算 `100-(100/(1+avg_gain/avg_loss))` (日线) | LV3TechnicalCalculator._calc_rsi (分钟线) | **⚠️ 数值不一致** | 同上，日线RSI vs 分钟线RSI数值不同 |
| **MACD** | ❌ 不计算 | LV3TechnicalCalculator._calc_macd (分钟线) | **无重复** | A完全没有MACD，只有B有 |
| **ATR** | ❌ 不计算 | LV3TechnicalCalculator._calc_atr (分钟线) | **无重复** | A没有ATR，止损距离无法自适应 |
| **Bollinger** | ❌ 不计算 | LV3TechnicalCalculator._calc_boll (分钟线) | **无重复** | — |
| **VWAP/Supertrend/KDJ** | ❌ 不计算 | LV3TechnicalCalculator (分钟线) | **无重复** | — |
| **基本面 PE/PB/ROE** | `data_service.get_batch_quotes_detailed()` → Futu snapshot (5min缓存) | `FundamentalProvider.get_data()` → Finnhub/Yahoo | **❌ 不是重复，是不同源** | A用Futu snapshot实时数据(5s)；B用Finnhub(可能24h缓存) |
| **基本面 industry/52w/debt** | ❌ 不获取 | FundamentalProvider | **无重复** | A只有PE/PB/ROE，B有industry/market_cap/revenue_growth/52w_high_low |
| **VIX** | `scheduler.market_data.get_vix()` (Finnhub VIXY + FRED fallback) | `regime_ctx.vix` (MarketRegimeGate注入) | **无重复** | 同一VIX值，不同传递路径 |
| **SPY context** | `scheduler.market_data.get_spy_context()` | `regime_ctx` (MarketRegimeGate) | **无重复** | A用原始数值；B只传regime字符串 |
| **新闻情绪** | ❌ VotingCoordinator._enrich_voting_context() 补 | PlaybookDataCollector._collect_news() → Finnhub | **⚠️ 双轨** | A在Voting层补news_sentiment；B在DataCollector层收集，但_playbook_data_to_market_data不传给Voting |
| **内部人交易** | ❌ 不获取 | PlaybookDataCollector._collect_insider() → SEC EDGAR | **无重复** | B独有维度 |
| **财报日历** | earnings_risk_guard (黑名单过滤) | PlaybookDataCollector._collect_earnings() → Finnhub | **⚠️ 双轨** | A做黑名单拦截；B收集surprise数据但不传给Voting |

### 1.2 核心发现：K线类型不对称

| 管线 | K线类型 | 根数 | 计算方式 | 输出字段数 | 代码位置 |
|------|---------|------|----------|-----------|---------|
| A factor_engine | `K_DAY` 日线 | 60 | **手写** `sum(closes[-5:])/5` | 8个 | factor_engine.py:358-429 |
| A MarketDataRouter | `K_1M` 分钟线 | 200 | **LV3TechnicalCalculator** | 20+个 | market_data_router.py:408-414 |
| B PlaybookDataCollector | 委托MarketDataRouter | 同A的router | LV3TechnicalCalculator | 20+个(只取6个) | data_collector.py:281-306 |

**问题不是重复查K线，是factor_engine用日线手算8个指标，MarketDataRouter用分钟线算20+个指标——同一标的技术面有两套互相矛盾的数据。**

factor_engine的MA5来自日线收盘价之和，MarketDataRouter的MA5来自分钟线SMA——数值不同，方向判断可能相反。

---

## 二、VotingCoordinator输入维度断崖式缺失（OKR核心瓶颈）

### 2.1 代码证据

`_playbook_data_to_market_data()`（pipeline.py:212-230）只传了**10个字段**给VotingCoordinator：

```python
# pipeline.py:212-230 — Pipeline B → VotingCoordinator 的适配器
def _playbook_data_to_market_data(symbol: str, data, regime_ctx) -> Dict[str, Any]:
    tech = data.technical
    return {
        "symbol": symbol,
        "ticker": symbol,
        "price": tech.price or 0,
        "rsi": tech.rsi_14 or 50,
        "macd_signal": tech.macd_signal or "neutral",
        "ma20": tech.ma20 or 0,
        "ma50": tech.ma50 or 0,
        "volume_ratio": tech.volume_5d_avg_ratio or 1.0,
        "vix": getattr(regime_ctx, "vix", 20),
        "regime": regime_ctx.regime.value if hasattr(regime_ctx.regime, "value") else str(regime_ctx.regime),
    }
```

Pipeline A通过`SymbolContext.from_raw()`传给`voting_coordinator.vote(symbol, ctx)`的是**100+字段**的dataclass（pipeline_contracts.py:26-100）。

`VotingCoordinator.vote()` 第386-402行已支持dataclass自动转换：
```python
def vote(self, symbol: str, ctx) -> Dict[str, Any]:
    if hasattr(ctx, '__dataclass_fields__'):
        market_data = dataclasses.asdict(ctx)  # ← SymbolContext → 100+字段dict
    elif isinstance(ctx, dict):
        market_data = ctx
    ...
```

### 2.2 维度对比表

| 数据维度 | Pipeline A (SymbolContext) | Pipeline B (_playbook_data_to_market_data) | 缺失影响 |
|----------|---------------------------|--------------------------------------------|---------|
| bid/ask/spread_bps | ✅ | ❌ | LLM无法评估流动性→滑点风险盲区 |
| ATR/atr_pct | ✅ | ❌ | LLM无法判断波动率→止损距离无依据 |
| MACD值/hist | ✅(macd+macd_signal+macd_hist) | ⚠️(仅macd_signal字符串) | 金叉/死叉判断缺一半数据 |
| Bollinger upper/lower/middle | ✅ | ❌ | 缺均值回归信号 |
| slope_5m | ✅ | ❌ | 缺短期动量方向 |
| order_flow_imbalance | ✅ | ❌ | 缺买卖力量比 |
| ORB (day_high/low/opening_range) | ✅ | ❌ | Pipeline B完全不做ORB策略 |
| Supertrend/KDJ/VWAP | ✅ | ❌ | 缺趋势确认+超买超卖维度 |
| prev_close/change_pct | ✅ | ❌ | 缺口相对强弱判断 |
| ma5/ma200 | ✅ | ❌(仅ma20/ma50) | 短长期均线排列缺失 |
| spy_price/spy_ma200 | ✅ | ❌(仅regime字符串) | LLM只能看regime标签，看不到原始数值 |
| earnings_blackout | ✅ | ❌(Screener层过滤) | 设计差异，非bug |
| news_sentiment | ✅(enrichment补) | ✅(enrichment补) | 两条管线都靠enrichment |
| volume/avg_volume | ✅ | ❌(仅volume_ratio) | 缺绝对成交量判断 |

### 2.3 Enrichment补偿分析

`_enrich_voting_context()`（voting_coordinator.py:534-613）统一补6个维度：
- news_sentiment_score, position_info, win_rate_7d, spread_bps, recent_trades, battle_plan_context

**但两条管线都跑同一个enrichment——所以B仍然只有10+6=16个维度 vs A的100+6=106个维度。**

### 2.4 对OKR的直接影响

同一VotingCoordinator、同一LLM，在16个维度上做的方向决策 vs 在106个维度上做的方向决策，结果必然不一致。

6/23的"SHORT是唯一alpha源"可能不是方向偏差，而是 **Pipeline B给LLM的数据太薄，LLM只能用RSI+MA20+MACD_signal三个指标做判断，等于退化成最简单的技术分析**。

---

## 三、基本面数据源双轨

| | factor_engine | PlaybookDataCollector |
|--|--------------|----------------------|
| **数据源** | `data_service.get_batch_quotes_detailed()` → Futu snapshot | `FundamentalProvider.get_data()` → Finnhub/Yahoo |
| **字段** | pe_ttm, pb_ratio, eps, net_asset, net_profit | pe_ttm, market_cap, industry, revenue_growth, roe, debt_to_equity, 52w_high/low |
| **缓存** | 5分钟函数级缓存 | 无缓存（每次API调用） |
| **评分** | 自己的PE/PB/ROE加权公式 (0-1分) | 不评分（传原始值给LLM） |
| **实时性** | 5s (Futu snapshot) | 24h (Finnhub缓存可能过期) |
| **API限频** | 无 | Finnhub 60次/分钟 |

**不是"重复"——是两个完全不同的基本面数据源，字段不重叠，评分逻辑不统一。**

factor_engine看PE/PB/ROE给0-1分，Playbook给LLM原始PE/market_cap/industry让LLM自己判断。

---

## 四、漏斗化方案（做减法，不新建抽象）

### 4.1 设计原则

1. **不新建抽象** — 系统已有SymbolContext(100+字段)、MarketDataRouter、PipelineStage Protocol，不造新的
2. **删代码消除根因** — `_playbook_data_to_market_data()`是有损适配器，删掉它让Pipeline B直接用SymbolContext
3. **一个数据入口 → 一个上下文 → 多个消费者** — 漏斗的核心是收敛，不是加层

### 4.2 漏斗三层架构

```
┌─────────────────────────────────────────────────┐
│  Layer 1: 统一数据采集 (已存在，没被Pipeline B复用) │
│  MarketDataRouter.get_full_market_context()      │
│  → Futu LV3 K_1M(200根) → LV3TechnicalCalculator│
│  → 20+技术指标 + 实时报价 + ORB + 深度数据         │
│  输出: raw dict (SymbolContext.from_raw的输入)    │
└──────────────────┬──────────────────────────────┘
                   │
          ┌────────┴────────┐
          │                 │
          ▼                 ▼
┌─────────────────┐  ┌──────────────────────┐
│ Pipeline A      │  │ Pipeline B            │
│ market_data_    │  │ 删掉_playbook_data_   │
│ stage.py        │  │ to_market_data()      │
│ → SymbolContext │  │ 直接用SymbolContext   │
│ → signal_stage  │  │ → VotingCoordinator   │
│ → VotingCoord   │  │ → _build_instructions │
└─────────────────┘  └──────────────────────┘
```

### 4.3 具体改动（3处，都是删代码/改路径，不加新代码）

---

#### 改动1：删 `_playbook_data_to_market_data()`，直接传SymbolContext

**当前流程（Pipeline B）**:
```
MarketDataRouter → PlaybookDataCollector._collect_technical()
→ PlaybookData.technical (取6个字段)
→ _playbook_data_to_market_data() [丢90%字段]
→ market_data dict (10字段)
→ VotingCoordinator.vote()
```

**目标流程**:
```
MarketDataRouter → market_data_stage.process() (已存在)
→ SymbolContext (100+字段)
→ VotingCoordinator.vote(symbol, ctx)  [vote()已支持dataclass→dict]
→ _build_instructions_from_votes()
```

**代码变更**:

`pipeline.py` — `_build_instructions_from_votes()` 函数:

```python
# 当前 (pipeline.py:268-274):
for sr in screen_results:
    symbol = sr.symbol
    data = sr.data
    factors = sr.factors
    market_data = _playbook_data_to_market_data(symbol, data, regime_context)

# 改为:
for sr in screen_results:
    symbol = sr.symbol
    factors = sr.factors
    # 直接从MarketDataRouter获取SymbolContext (与Pipeline A同路径)
    raw_ctx = data_router.get_full_market_context(symbol)
    if not raw_ctx or raw_ctx.get("price", 0) <= 0:
        instructions.append(Instruction(
            symbol=symbol, direction="SKIP", skip_reason="行情不可用"
        ))
        continue
    raw_ctx["symbol"] = symbol
    raw_ctx["vix"] = getattr(regime_context, "vix", 20)
    raw_ctx["regime"] = regime_context.regime.value if hasattr(regime_context, "regime") else "CAUTIOUS"
    # VotingCoordinator.vote() 已支持 dict 输入 (line 396-397)
    vote_result = voting_coordinator.vote(symbol, raw_ctx)
```

**删除的代码**: `_playbook_data_to_market_data()` 整个函数 (pipeline.py:212-230)

**影响**: Pipeline B的VotingCoordinator输入从10字段→100+字段

**验证**: `VotingCoordinator.vote()` line 386-402 已支持dict输入，零改动

---

#### 改动2：删 `factor_engine._compute_technicals()`，复用MarketDataRouter

**当前流程（factor_engine）**:
```
ManualOrderService._quote_ctx → get_cur_kline(K_DAY, 60)
→ 手算 MA5/MA10/MA20/RSI14/vol_ratio/change_5d/change_20d/high_20d/low_20d
→ 8个指标
```

**目标流程**:
```
MarketDataRouter.get_full_market_context()
→ LV3TechnicalCalculator → 20+指标
→ factor_engine 消费 ctx.get("rsi14") / ctx.get("ma20") 等
```

**代码变更**:

`factor_engine.py` — `_compute_technicals()` 函数 (line 358-429):

```python
# 当前: 手动开连接 + get_cur_kline + 手算8指标
def _compute_technicals(svc, symbol: str) -> Optional[Dict]:
    from futu import RET_OK, SubType
    ret_sub, _ = svc._quote_ctx.subscribe([symbol], [SubType.K_DAY])
    ...
    ret, data = svc._quote_ctx.get_cur_kline(symbol, 60, ktype="K_DAY")
    closes = [float(r["close"]) for _, r in data.iterrows()]
    result["ma5"] = sum(closes[-5:]) / 5
    ...

# 改为: 从MarketDataRouter获取 (与Pipeline B同源)
def _compute_technicals(svc, symbol: str, data_router=None) -> Optional[Dict]:
    if data_router:
        ctx = data_router.get_full_market_context(symbol)
        if ctx and ctx.get("price", 0) > 0:
            return {
                "symbol": symbol,
                "cur_price": ctx.get("price", 0),
                "ma5": ctx.get("ma5", 0),
                "ma10": ctx.get("ma10", 0),
                "ma20": ctx.get("ma20", 0),
                "rsi14": ctx.get("rsi", 0),
                "vol_ratio": ctx.get("volume_ratio", 1.0),
                "change_5d": ctx.get("change_5d_pct", 0),
                "change_20d": ctx.get("change_20d_pct", 0),
                "macd_hist": ctx.get("macd_hist", 0),
                "atr": ctx.get("atr", 0),
                "boll_upper": ctx.get("boll_upper", 0),
                "boll_lower": ctx.get("boll_lower", 0),
            }
    # 降级: 保留原逻辑 (ManualOrderService手算)
    ...原代码...
```

**影响**: 
- 消除日线/分钟线MA20数值不一致
- factor_engine获得MACD/ATR/Bollinger（原来没有）
- 仍保留降级路径（data_router不可用时手算）

---

#### 改动3：统一基本面数据源到Futu snapshot

**当前**:
- factor_engine: `data_service.get_batch_quotes_detailed()` → Futu snapshot (pe_ttm, pb_ratio, eps, net_asset, net_profit)
- PlaybookDataCollector: `FundamentalProvider.get_data()` → Finnhub (pe_ttm, market_cap, industry, revenue_growth, roe, debt_to_equity, 52w_high/low)

**目标**: 都用Futu snapshot（实时5s、无API限频、130+字段），FundamentalProvider降级为fallback

**代码变更**:

`data_collector.py` — `_collect_fundamental()` (line 225-272):

```python
# 当前: 委托 FundamentalProvider
def _collect_fundamental(self, symbol: str) -> FundamentalData:
    raw = self.fundamental.get_data(symbol)
    ...

# 改为: 优先Futu snapshot, 降级FundamentalProvider
def _collect_fundamental(self, symbol: str) -> FundamentalData:
    # P0 (2026-06-24): 统一基本面数据源到Futu snapshot
    try:
        from app.web_dashboard.data_service import get_data_service
        ds = get_data_service()
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    quotes = pool.submit(
                        asyncio.run, ds.get_batch_quotes_detailed([symbol])
                    ).result(timeout=10)
            else:
                quotes = loop.run_until_complete(ds.get_batch_quotes_detailed([symbol]))
        except RuntimeError:
            quotes = asyncio.run(ds.get_batch_quotes_detailed([symbol]))

        q = quotes.get(symbol, {})
        pe = q.get("pe_ttm") or q.get("pe_ratio") or 0
        pb = q.get("pb_ratio") or 0
        market_cap = q.get("market_cap") or 0
        roe = (q.get("net_profit", 0) / q.get("net_asset", 1) * 100) if q.get("net_asset") else 0
        # Futu snapshot有industry字段
        industry = q.get("industry") or ""
        # 52w high/low from snapshot
        high_52w = q.get("high_52w") or 0
        low_52w = q.get("low_52w") or 0

        key_fields = [pe, market_cap, industry]
        quality = "ok" if all(f for f in key_fields) else ("partial" if any(f for f in key_fields) else "missing")

        return FundamentalData(
            pe=pe, market_cap=market_cap, industry=industry,
            roe=roe, high_52w=high_52w, low_52w=low_52w,
            pb=pb, quality=quality,
        )
    except Exception:
        pass

    # 降级: FundamentalProvider (原逻辑)
    raw = self.fundamental.get_data(symbol)
    ...
```

**影响**: 基本面数据从24h缓存→5s实时，消除API限频风险

---

## 五、不改动的部分

| 组件 | 理由 |
|------|------|
| PlaybookDataCollector的insider维度 | factor_engine没有SEC EDGAR，是B独有增量价值 |
| PlaybookDataCollector的news维度 | 虽然VotingCoordinator.enrichment也补news，但DataCollector层收集更详细（含标题/摘要/来源） |
| PlaybookDataCollector的earnings维度 | Finnhub earnings calendar有surprise数据，factor_engine只有黑名单 |
| factor_engine的Finnhub异动发现 | 标的发现逻辑，不是数据采集重复 |
| VotingCoordinator._enrich_voting_context() | 两条管线都需要，改动后B的base从10→100+字段，enrichment后106+ |
| PlaybookDataCollector的sentiment维度 | VIX+news_sentiment组合，与factor_engine的VIX路径不同但互补 |

---

## 六、OKR影响估算

### 6.1 KR级影响

| KR | 当前值 | 目标 | 改动后预期 | 逻辑 |
|----|--------|------|-----------|------|
| KR1 胜率 55% | 54.2% | 55% | 57-60% | LLM在100+维度做决策 vs 16维度，方向判断准确率提升；SHORT信号质量改善（当前SHORT是唯一alpha源，更多维度→更多有效SHORT） |
| KR2 周PnL $1000 | $242 | $1000 | $400-600 | 同置信度下信号质量更高→持仓盈亏比改善；但主要PnL瓶颈在退出规则（TIME_EXIT 2h/-0.5%），数据统一是前置条件不是直接解 |
| KR3 样本 30笔 | 24 | 30 | 不变 | 样本量取决于执行频率，不取决于数据维度 |
| KR4 IC T+3 >0.05 | 0% | 0.05 | 有数据可算 | 两条管线给VotingCoordinator相同数据→IC归因可按decision_id分组，不再有"管线差异"污染 |
| KR5 触发率 50% | 100% | 50% | 100% | 不受影响 |

### 6.2 PnL分解

当前: `PnL = (WR × avg_win) - (LR × avg_loss)` = 0.591×$103 - 0.409×$90 = $24/笔

改动后模拟（假设LLM在更多维度下方向准确率从54%→58%）:
- `PnL = 0.58×$110 - 0.42×$85 = $31/笔` (avg_win提升因为LLM能看到ATR→止损更合理)
- 周PnL: $31×15笔 = $465 (仍不达$1000，需要退出规则配合)

**结论**: 数据统一是OKR的**必要前置条件**，不是充分条件。PnL要达标还需要退出规则优化（TIME_EXIT→EOD持有，已单独跟踪）。

---

## 七、实施计划

### Phase 1: 数据收敛（本周，最小改动）

| 步骤 | 改动 | 代码量 | 风险 | 验证方法 |
|------|------|--------|------|---------|
| 1.1 | 删`_playbook_data_to_market_data()`, 直接传raw_ctx | -20行+10行 | 低 | 对比改动前后VotingCoordinator输入字段数 |
| 1.2 | factor_engine._compute_technicals()加data_router参数 | +15行 | 低 | 对比MA20数值一致性 |
| 1.3 | data_collector._collect_fundamental()优先Futu | +30行 | 中 | 对比PE值实时性 |

### Phase 2: 验证（改动后第一个交易日）

| 验证项 | 方法 | 预期 |
|--------|------|------|
| 维度对齐 | `len(market_data.keys())` 对比 | A和B都≥100 |
| 方向一致性 | 同一标的两条管线Voting结果 | 方向一致率>90% |
| 数值一致性 | MA20/RSI对比 | 差异<1% |
| OKR影响 | 跑一周后看胜率 | >55% |

### Phase 3: 清理（验证通过后）

| 清理项 | 方法 |
|--------|------|
| 删PlaybookDataCollector._collect_technical() | 已被market_data_stage替代 |
| 删factor_engine手算MA/RSI代码 | 已被MarketDataRouter替代 |
| 删FundamentalProvider的Finnhub路径 | 已被Futu snapshot替代 |
| 删_CallLLMAdapter死代码 | 已被VotingCoordinator替代 |

---

## 八、风险分析

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| MarketDataRouter K_1M在非交易时段返回None | 高(已知Pitfall) | Pipeline B无法生成指令 | 保留降级路径：data_router不可用时回退到原逻辑 |
| Futu snapshot字段名不统一 | 中 | 基本面数据缺失 | 验证脚本：`ds.get_batch_quotes_detailed(['US.PG'])` 检查字段 |
| 改动后LLM决策突变 | 中 | 方向分布变化 | 先shadow跑1天对比，确认无异常方向偏移 |
| Pipeline B失去PlaybookData的insider/news/earnings维度 | 低 | LLM上下文减少 | 这些维度不进VotingCoordinator（设计如此），不受影响 |

---

## 九、附录：代码位置索引

| 文件 | 行号 | 内容 |
|------|------|------|
| `app/playbook/pipeline.py` | 212-230 | `_playbook_data_to_market_data()` — **待删除的有损适配器** |
| `app/playbook/pipeline.py` | 233-311 | `_build_instructions_from_votes()` — **改动1的目标** |
| `app/plan/factor_engine.py` | 358-429 | `_compute_technicals()` — **改动2的目标** |
| `app/plan/factor_engine.py` | 494-593 | `_fetch_fundamental_score()` — **已用Futu snapshot，保持** |
| `app/playbook/data_collector.py` | 281-306 | `_collect_technical()` — **委托MarketDataRouter，正确** |
| `app/playbook/data_collector.py` | 225-272 | `_collect_fundamental()` — **改动3的目标** |
| `app/data/market_data_router.py` | 361-480 | `get_full_market_context()` — **统一数据入口** |
| `app/data/lv3_technical_calculator.py` | 30-92 | `LV3TechnicalCalculator` — **统一技术指标计算** |
| `app/core/pipeline_contracts.py` | 26-100 | `SymbolContext` — **100+字段统一上下文** |
| `app/agents/voting/voting_coordinator.py` | 386-402 | `vote()` — **已支持dataclass+dict输入** |
| `app/agents/voting/voting_coordinator.py` | 534-613 | `_enrich_voting_context()` — **两条管线共用** |
| `app/pipeline/stages/market_data_stage.py` | 25-105 | `process()` — **Pipeline A数据入口，Pipeline B应复用** |
| `app/pipeline/signal_pipeline.py` | 999-1047 | `_process_symbol_new_pipeline()` — **Pipeline A入口** |
| `app/pipeline/stages/signal_stage.py` | 216-295 | `process()` — **Pipeline A策略编排** |

---

## 十、总结

**一句话**: 当前两条管线的核心问题不是"重复查K线"，而是 `_playbook_data_to_market_data()` 这个有损适配器把100+字段的SymbolContext压缩成10个字段喂给LLM，让Pipeline B的每次投票都在信息饥饿状态下做决策。删掉这个适配器、直接传SymbolContext给VotingCoordinator，是投入最小、OKR收益最高的改动。

**三个改动的本质**:
1. **改动1** = 删有损适配器，让Pipeline B的LLM看到和Pipeline A一样的100+维度数据
2. **改动2** = 让factor_engine不再手算日线指标，复用分钟线20+指标（消除数值不一致）
3. **改动3** = 统一基本面数据源到Futu snapshot（消除24h缓存过期风险）

**OKR影响**: 数据统一是KR1(胜率)和KR4(IC)的必要前置条件。KR2(PnL)要达标还需要退出规则配合。
