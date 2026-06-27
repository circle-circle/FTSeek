# 信号源统一方案评审：修正与调整计划

> **日期**: 2026-06-24
> **评审对象**: `docs/analysis/signal-source-unification-20260624.md`（信号源统一方案）
> **方法**: 全量代码级核对原文档每条行号证据，对其改动方案做安全性/优先级复审
> **结论**: 原文档**诊断准确、方向正确**，但改动1 的"零改动"被高估、改动2 存在语义错位、改动3 为伪需求+脚枪。本文给出修正版方案与重排后的优先级。

---

## 一、核实结论：诊断成立，"零改动/低风险"被高估

对原文档核心论断逐条核对真实代码：

| 原文档论断 | 代码核实 | 判定 |
|---|---|---|
| `_playbook_data_to_market_data()` 把 100+ 字段压成 10 个 | ✅ `pipeline.py:219-230` 恰好 10 键（symbol/ticker/price/rsi/macd_signal/ma20/ma50/volume_ratio/vix/regime） | 成立 |
| `vote()` 已支持 dict/dataclass，删适配器零改动 | ⚠️ 分支存在（`voting_coordinator.py:394-397`），但"零改动"不成立（见 §二-1） | 部分成立 |
| LV3 算 20+ 指标，factor_engine 只 8 个 | ✅ LV3 输出 rsi/macd/macd_signal/macd_hist/atr/atr_percent/bb_*/vwap/kdj_*/supertrend_*/ma5/ma10/ma20/ma50/volume_ratio（`lv3_technical_calculator.py:115,152-154,307-330`） | 成立 |
| factor_engine 手算日线 8 指标 | ✅ `factor_engine.py:358-429`，`get_cur_kline(K_DAY,60)`，`ma5=sum(closes[-5:])/5`（383），`rsi14`（413） | 成立 |
| data_collector 基本面走 Finnhub 24h 缓存 | ✅ `data_collector.py:225-232` 委托 `FundamentalProvider.get_data()` | 成立（但"5s 实时性"对基本面是伪需求，见 §二-3） |
| `vote()` 已支持 dataclass+dict 输入 | ✅ `voting_coordinator.py:386-397` 双分支 | 成立 |

**诊断层面原文档无懈可击。问题出在改动方案的安全性论证与优先级排序上。**

---

## 二、原文档漏掉/判断偏的关键问题

### 2.1 改动1 自相矛盾："传 SymbolContext" vs 提议代码传 `raw_ctx` dict

原文档正文称"直接传 SymbolContext"，但提议代码（原文档第 203-213 行）传的是 `data_router.get_full_market_context()` 返回的 **raw dict**，两者不是一回事：

- **Pipeline A** 传 `SymbolContext` dataclass → `vote()` 走 `asdict` 分支 → 键名为 `bollinger_upper` / `atr_pct` / `spread_bps` / `ma200`（`pipeline_contracts.py:49-59`）
- **改动1 原版后的 Pipeline B** 传 raw dict → 走 dict 分支 → 键名为 LV3 原生 `bb_upper` / `atr_percent` / 无 `spread_bps`

**后果**：A/B 键名仍不一致，原文档 Phase 2 验证目标"方向一致率>90%"和 KR4"IC 归因无管线污染"**都不会因改动1 原版达成**。

**核实证据**：
- LLM 消费端读取键名（`voting_coordinator.py:1211-1216`）：`rsi` / `macd_signal` / `ma20` / `ma50` / `volume_ratio` / `price`
- `model_agent.py:170-173`：`atr` / `ma20` / `rsi`
- `voting_coordinator.py:975`：`spy_ma200_ratio`
- SymbolContext 字段名（`pipeline_contracts.py:49-59`）：`bollinger_upper` / `atr_pct` / `spread_bps` / `ma5` / `ma20` / `ma50` / `ma200`
- LV3 / router raw dict 字段名（`lv3_technical_calculator.py` + `market_data_router.py:836-855` field_map）：`bb_upper` / `atr_percent` / 无 `spread_bps`

要让两条管线真正同源，B 必须走 `SymbolContext.from_raw(raw_ctx)`（已存在，`pipeline_contracts.py:142`）再传 dataclass，使 `vote()` 走 `asdict` 分支拿到与 A 完全一致的键名空间。

> **修正**：删适配器是对的，但要补一行 `ctx = SymbolContext.from_raw(raw_ctx)` 才能真正收敛。属于"换一行"而非"零改动"。原文档"删代码不加新代码"的原则在此处需让步。

### 2.2 改动2 语义错误——factor_engine 的因子是日线概念，不该换分钟线 MA

factor_engine 的因子定义（`factor_engine.py:9-10, 378-392`）：

| 因子 | 定义 | 时间框架 |
|---|---|---|
| `relative_strength` | 个股 **20 日涨幅** vs SPY | 日线 |
| `ma_alignment` | **MA5>MA10>MA20** 多空排列 | 日线 |
| `change_5d` / `change_20d` | 日线 5/20 日涨跌（`factor_engine.py:378-379`） | 日线 |

这些都是**日线 bar 概念**。原文档改动2 要 factor_engine 改用 MarketDataRouter 的**分钟线 SMA**，等于用分钟线 MA20 去填日线趋势因子——语义错位，`relative_strength` 这类跨日因子根本无法从分钟线还原。

更关键的是：**"数值不一致"在改动1 之后已经消失**。改动1 让 Pipeline B 全程用 router 的分钟线 ma20，不再有 factor_engine 日线 ma20 与之并列喂给同一 LLM。改动2 解决的是个**已被改动1 消解的问题**，性价比低、且破坏日线因子语义。

> **修正**：不是"factor_engine 改用分钟线"，而是"router 额外暴露一个日线 bar 指标块（K_DAY 派生）"，让日线因子和分钟线因子各取所需、共存。或直接**砍掉改动2**，只保留改动1。

### 2.3 改动3 过度工程 + 基本面不需要 5s 实时

三个问题：

1. **伪需求**：PE/PB/ROE/industry/52w 这些基本面**本质低频**，24h 缓存完全够用。原文档用"5s 实时 vs 24h 过期"框定问题，但基本面本就不该按秒刷新。改动3 的 OKR 收益接近 0。
2. **实现是脚枪**：提议代码（原文档 311-322 行）在同步上下文里用 `ThreadPoolExecutor + asyncio.run + loop.is_running() + RuntimeError fallback` 套娃，是典型的嵌套事件循环陷阱，对 `get_data_service()` 单例的线程安全也无保障。风险评级"中"偏低，应为"中高"。
3. ROE 用 `net_profit/net_asset*100` 从 snapshot 现算，分母/正负号易错，且 Futu snapshot 字段名未经验证（原文档 §八 自己也列为风险，但提议代码已硬编码这些字段名）。

> **修正**：**砍掉或大幅推迟改动3**。Finnhub 路径作为基本面主源没问题；真要补实时性，只补真正高频字段（如 day_high/low 已在 router，不属基本面）。

### 2.4 OKR KR1 估算（54%→57-60%）与既有 IC 实测相悖

依项目 IC 实测记忆（`signal-ic-measurement.md`）：

- **T+1 IC 反预测（-0.114）**，alpha 是周级（T+5 +0.265）
- EARNINGS 反预测，TECHNICAL+SHORT 正收益

含义：方向决策的瓶颈是**预测 horizon 与退出规则**，不是输入维度数量。给 LLM 更多维度能改善 SHORT 质量（原文档这点判断对），但把胜率从 54% 拉到 57-60% 缺乏依据——T+1 反预测下，维度再多也救不了一日内的均值回归噪声。

原文档 §6.2 诚实承认"PnL 不达 $1000 需退出规则配合"，但 KR1 估算没同步下调，存在内部不一致。

> **修正**：KR1 预期改为 **55-56%**（微提升）；真正杠杆放在退出规则（TIME_EXIT→EOD 持有，匹配周级 alpha）与 horizon 治理上。

### 2.5 A/B 收敛悖论——"方向一致率>90%"与"保留两条管线对比"互斥

改动1+改动2 全做完后，A 和 B 喂给 `vote()` 的数据同源，**两条管线退化为一条**，届时"对比 A/B 方向一致率"这个验证项失去意义（必然 100%）。

- 若目标是收敛成单管线：应明说"删 Pipeline B 的独立数据路径"；
- 若想保留 A/B 对照测量改动收益：必须**保留 shadow 开关**跑旧适配器输出做 baseline。

原文档同时要两者，逻辑互斥。

> **修正**：改动1 实施**保留 legacy 适配器于 config flag 后**，shadow 跑 1 交易日对照，量化收益后再删。

### 2.6 缺 baseline + 回滚阈值

原文档 Phase 2 验证只有"方向一致率>90%"，没有：

- 改动前 N 标的 × M 天的 vote 决策快照基线；
- "若 SHORT 占比偏移 >X% 或 SKIP 率 >Y% 则回滚"的明确阈值。

对 sim-live 系统，这是必须补的 gate。

### 2.7（预存隐患）regime 取值空间不一致

`_build_instructions_from_votes` 用 `regime_mults = {"RISK_ON","CAUTIOUS","RISK_OFF","NO_TRADE"}`（`pipeline.py:265`），但适配器写 `regime = regime_ctx.regime.value`。两套枚举值空间，改动1 注入 regime 时要确保落到 `RISK_ON/CAUTIOUS/...` 这套，否则 `regime_mult` 默认 0.5。预存隐患，改动时顺手对齐。

---

## 三、修正版调整计划（重排优先级 + 补门槛）

### 3.1 重新排序的三项改动

| 优先级 | 改动 | 调整 | 理由 |
|---|---|---|---|
| **P0** | 改动1（删适配器） | **改为**：`ctx = SymbolContext.from_raw(raw_ctx)` → `vote(symbol, ctx)`，而非传 raw dict；注入 `vix`/`regime` 时对齐 `RISK_ON` 枚举 | 真正收敛 A/B 键名，KR4 IC 归因才无污染；增量仅 1 行 |
| **P1** | 新增 measurement gate | 改动前跑 1 交易日 shadow baseline，保留旧适配器分支于 config flag 后；定义回滚阈值 | 量化改动收益，防方向分布突变 |
| **defer** | 改动2（factor_engine 改用 router） | **砍掉或重定义为**：router 增设日线 bar 指标块，日线/分钟线共存 | 改动1 已消解"数值不一致"；原方案破坏日线因子语义 |
| **defer** | 改动3（基本面统一 Futu） | **推迟**，保留 Finnhub 主源 | 基本面低频，5s 是伪需求；asyncio-in-sync 脚枪 |

### 3.2 修正版改动1 的目标代码

`app/playbook/pipeline.py` — `_build_instructions_from_votes()`（第 268-274 行区域）：

```python
# 当前 (pipeline.py:268-274):
for sr in screen_results:
    symbol = sr.symbol
    data = sr.data
    factors = sr.factors
    market_data = _playbook_data_to_market_data(symbol, data, regime_context)

# 修正版改为:
from app.core.pipeline_contracts import SymbolContext
from app.playbook.data_collector import PlaybookDataCollector  # 或直接取全局 router

for sr in screen_results:
    symbol = sr.symbol
    factors = sr.factors
    # 与 Pipeline A 同路径：router raw dict → SymbolContext dataclass
    raw_ctx = data_router.get_full_market_context(symbol)
    if not raw_ctx or raw_ctx.get("price", 0) <= 0:
        instructions.append(Instruction(
            symbol=symbol, direction="SKIP", skip_reason="行情不可用"
        ))
        continue
    raw_ctx["symbol"] = symbol
    raw_ctx["vix"] = getattr(regime_context, "vix", 20)
    # 对齐 RISK_ON/CAUTIOUS/RISK_OFF/NO_TRADE 枚举空间 (见 §2.7)
    raw_ctx["regime"] = _normalize_regime(regime_context)
    ctx = SymbolContext.from_raw(raw_ctx)   # ← 关键：走 asdict 分支, 与 Pipeline A 键名一致
    vote_result = voting_coordinator.vote(symbol, ctx)  # vote() line 394-395 走 dataclass 分支
```

**删除**：`_playbook_data_to_market_data()` 整个函数（`pipeline.py:212-230`）——但先置于 `--legacy-adapter` flag 后跑 shadow（见 §3.3）。

**与原文档差异**：多了 `SymbolContext.from_raw(raw_ctx)` 这一行。这是收敛 A/B 键名的命门，不可省。

### 3.3 执行顺序（含 baseline 与回滚门槛）

| 步骤 | 动作 | 产出/门槛 |
|---|---|---|
| 1 | **基线采集**（先于一切）：选 20 个候选标的，连续 1 交易日，落盘每标的的：旧适配器 10 字段输出、router raw_ctx 全字段、`vote()` 方向/置信度/decision_id | SHORT/LONG/SKIP 分布 + 置信度直方图 = baseline |
| 2 | **改动1 修正版**：删 `_playbook_data_to_market_data`，改走 `SymbolContext.from_raw`；旧适配器函数保留于 `--legacy-adapter` flag 后做 shadow 对照 | 代码 diff |
| 3 | **1 交易日 shadow 对照**：新路径与 legacy 同时跑，比方向一致率、字段维度数（`len(asdict(ctx))`）、SHORT 占比偏移 | 见回滚阈值 |
| 4 | **回滚阈值**：SHORT 占比偏移 >15pp 或 SKIP 率 >20% 或方向一致率 <70% → 回滚，查 enrichment 是否漏读新增键名（`bb_upper` vs `bollinger_upper` 等），补键名映射再试 | gate |
| 5 | 通过则删 legacy 适配器与 flag；不通过则回到步骤 2 调键名映射 | — |
| 6 | **退出规则**（PnL 真正杠杆，与数据统一并行推进，另文跟踪）：TIME_EXIT 2h/-0.5% → EOD 持有，匹配周级 alpha（T+5 IC +0.265） | KR2 达 $1000 的必要条件 |

### 3.4 对 OKR 预期的修正

| KR | 原文档预期 | 修正预期 | 依据 |
|---|---|---|---|
| KR1 胜率 55% | 57-60% | **55-56%** | T+1 IC 反预测，维度改善 SHORT 质量但救不了 horizon 噪声 |
| KR2 周PnL $1000 | $400-600 | $300-500（数据）+ 退出规则才达 $1000 | 原文档已承认，此处强化 |
| KR3 样本 30 笔 | 不变 | 不变 | 取决于执行频率 |
| KR4 IC T+3 >0.05 | "有数据可算" | 成立，但**需先过改动1 修正版**才能按 decision_id 干净分组 | A/B 键名不收敛则 IC 仍被管线差异污染 |
| KR5 触发率 50% | 不受影响 | 不受影响 | — |

---

## 四、不改动的部分（沿用原文档 §五，确认仍成立）

| 组件 | 理由 |
|---|---|
| PlaybookDataCollector 的 insider 维度 | factor_engine 无 SEC EDGAR，B 独有增量价值 |
| PlaybookDataCollector 的 news 维度 | DataCollector 层收集更详细（含标题/摘要/来源） |
| PlaybookDataCollector 的 earnings 维度 | Finnhub earnings calendar 有 surprise 数据 |
| factor_engine 的 Finnhub 异动发现 | 标的发现逻辑，非数据采集重复 |
| VotingCoordinator `_enrich_voting_context()` | 两条管线共用；改动后 B 的 base 从 10 → 50+ 字段（SymbolContext），enrichment 后 56+ |
| PlaybookDataCollector 的 sentiment 维度 | VIX + news_sentiment 组合，与 factor_engine 的 VIX 路径互补 |

---

## 五、风险登记册（修正版）

| 风险 | 概率 | 影响 | 缓解 | 原文档是否覆盖 |
|---|---|---|---|---|
| raw dict 与 SymbolContext 键名不一致（`bb_upper` vs `bollinger_upper` 等） | 高 | enrichment/LLM 漏读字段 | 走 `SymbolContext.from_raw`，且 shadow 阶段比对 `len(asdict(ctx))` 与逐键 diff | **未覆盖（本文新增）** |
| `SymbolContext.from_raw` 未映射的 router 字段丢失（如 `order_flow_imbalance`/`slope_5m`/`opening_range`） | 中 | 部分增量维度未进 LLM | 核对 `from_raw`（`pipeline_contracts.py:142+`）字段映射表，必要时扩 `from_raw` | 部分覆盖 |
| `regime` 枚举空间不匹配 → `regime_mult` 默认 0.5 | 中 | 仓位系统性偏差 | 改动1 注入时调 `_normalize_regime`（§2.7） | **未覆盖（本文新增）** |
| 改动后 LLM 方向分布突变（尤其 SHORT 占比） | 中 | 策略风格漂移 | shadow 1 交易日 + 回滚阈值（§3.3 步骤4） | 覆盖但无量化阈值 |
| MarketDataRouter K_1M 非交易时段返回 None | 高 | Pipeline B 无法生成指令 | 保留降级路径 | 覆盖 |
| 基本面 asyncio-in-sync 脚枪（仅若启改动3） | 中高 | 嵌套事件循环异常 | 推迟改动3 即规避 | 风险评级偏低 |

---

## 六、附录：代码位置索引（核实版）

| 文件 | 行号 | 内容 | 核实状态 |
|---|---|---|---|
| `app/playbook/pipeline.py` | 212-230 | `_playbook_data_to_market_data()` — 待删的有损适配器 | ✅ 10 字段确认 |
| `app/playbook/pipeline.py` | 233-311 | `_build_instructions_from_votes()` — 改动1 目标 | ✅ |
| `app/playbook/pipeline.py` | 265 | `regime_mults` 枚举空间 — §2.7 隐患点 | ✅ |
| `app/core/pipeline_contracts.py` | 26-100 | `SymbolContext` — 50+ 真实字段（原文档"100+"略高估） | ✅ |
| `app/core/pipeline_contracts.py` | 142+ | `SymbolContext.from_raw()` — 改动1 修正版命门 | ✅ 存在 |
| `app/agents/voting/voting_coordinator.py` | 386-397 | `vote()` dataclass/dict 双分支 | ✅ |
| `app/agents/voting/voting_coordinator.py` | 1211-1216 | LLM 消费的 market_data 键名 | ✅ |
| `app/agents/voting/voting_coordinator.py` | 975 | `spy_ma200_ratio` 读取（raw_ctx 不含，预存缺口） | ✅ |
| `app/agents/voting/model_agent.py` | 170-173 | `atr`/`ma20`/`rsi` 读取 | ✅ |
| `app/data/lv3_technical_calculator.py` | 115 | `result[f"ma{period}"]` — LV3 确算 ma5/10/20/50 | ✅（原文档未指明行号，本文补） |
| `app/data/market_data_router.py` | 836-855 | field_map（`rsi`→`rsi_14` 等键名映射，另一方法） | ✅ 注意：与 get_full_market_context 的 ts_ctx 键名不同 |
| `app/data/market_data_router.py` | 361-480 | `get_full_market_context()` — 统一数据入口 | ✅ |
| `app/plan/factor_engine.py` | 358-429 | `_compute_technicals()` 日线手算（改动2 目标，建议砍） | ✅ |
| `app/plan/factor_engine.py` | 378-392 | `change_5d/20d`、`ma_alignment` — 日线因子语义证据 | ✅ |
| `app/playbook/data_collector.py` | 225-272 | `_collect_fundamental()` Finnhub 路径（改动3 目标，建议推迟） | ✅ |
| `app/playbook/data_collector.py` | 281-306 | `_collect_technical()` 委托 router | ✅ |

---

## 七、一句话总结

原文档**诊断准、方向对**，但三处需修正：

1. **改动1** 必须传 `SymbolContext.from_raw()` 而非 raw dict，否则 A/B 键名仍不收敛、KR4 IC 归因仍受污染——这是本文最核心的修正；
2. **改动2** 用分钟线 MA 替日线因子是语义错位，且问题已被改动1 消解，应砍掉或重定义为"日线/分钟线指标共存"；
3. **改动3** 基本面不需要 5s 实时、asyncio-in-sync 是脚枪，应推迟。

真正拉 OKR 的是**退出规则匹配周级 alpha（T+5 IC +0.265）**，数据统一只是必要前置。实施时务必先采 baseline、保留 legacy shadow、设回滚阈值，再删代码。
