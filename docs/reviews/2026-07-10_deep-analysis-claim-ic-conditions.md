# Deep-Stock-Analysis Skill 修改审查说明

- **日期**：2026-07-10
- **范围**：`/root/.hermes/skills/deep-stock-analysis/scripts/`（skill 侧，#1-#6）+ `/opt/ftseek/FTSeek-US/scripts/factor_ic_backfill.py`（FTSeek 侧，#7-#8）
- **主线**：修复"学习闭环把有效论据反向惩罚 + conditions 靠关键词猜 + 辩论结构性偏空 + 挑战者伪独立 + 执行脑 IC 冻结/两脑词汇断裂"六类断点，让 `信息分析 → 2模型辩论(方向中性) → LLM claim_type → alpha 归因 → conditions → 退出/方向门` 全链路口径一致、方向由证据推导、两脑共用 claim_type 词汇。
- **共 7 组改动**（#1/#2/#4 = IC归因与conditions；#5/#6 = 辩论方向中性化与挑战者fallback；#7/#8 = factor IC 假指标与 backfill 崩溃修复），涉及 `alpha_backfill.py`/`llm_synth.py`/`signal_gate.py`/`adapter.py`/`factor_ic_backfill.py`，改动全部 surgical。
- **策略意图（用户明确）**：方向不预设偏向，做多做空同权，由信息分析链路推导方向与策略。
- **验证方式**：`py_compile` 语法 + 对真实 `signal_snapshots.db`/`ftseek_us.db` 数据行为验证（非理论）+ 只读诊断脚本 `scripts/diag_ic_brains_reconcile.py` 复跑核实。

---

## 改动 #1：claim IC 分类口径 score-IC → avg_alpha 符号

**文件**：`alpha_backfill.py`、`llm_synth.py`、`signal_gate.py`

**根因**：`compute_claim_ic` 的 `ic_t1 = Spearman(strength, alpha)`，而 `strength=综合评分`，同一信号所有 claim 共享同一 x → IC 实际测的是"评分 vs alpha"，非"该 claim 的预测力"。实测缓存中 IC 符号与 avg_alpha 系统性相反（如 volume_expansion IC+0.32 但 α−0.65%）→ gate/prompt 反向惩罚了真正赚钱的论据。

**改法**：
- `alpha_backfill.load_claim_ic`（L127-132）：effective/ineffective 分类改用 `avg_alpha_t1` 符号，非 `ic_t1`。
- 新增常量（L77-78）：`_CLAIM_MIN_N=10`、`_CLAIM_ALPHA_TH=0.002`（滤小样本/噪声）。
- `llm_synth._load_claim_ic_for_prompt`（L580 区域）：prompt 标签"IC>0.05"→"历史平均超额>0"，明细字段改 `avg_alpha_t1`（否则 prompt 自相矛盾）。
- `signal_gate`（reason 文案）："历史IC<0"→"历史超额为负"，与判据一致。

**验证**：`py_compile` OK；真实 342 样本 `SIGN-CONSISTENT: True`（effective/ineffective 列表翻转为与真实超额一致）。

---

## 改动 #2：claim IC 按 strategy_id 隔离（deep_analysis-only）

**文件**：`alpha_backfill.py`（4 处）、`signal_gate.py`（1 处）、`llm_synth.py`（1 处）

**根因**：缓存混入 voting_llm（已禁用）样本，且两套 taxonomy 几乎不相交——deep_analysis 用 `claim_type` 枚举（product_catalyst…），voting_llm 用 `above_ma20` 等 condition 名。导致 deep_analysis 的 prompt/gate 读到自己根本不产出的 legacy 论据名。

**改法**：
- `compute_claim_ic(strategy_id=_PRODUCTION_STRATEGY)`（L308, L331）：SQL 加 `AND strategy_id = ?`，默认 `"deep_analysis"`（L79）。传 `None` = 全策略池化（仅调试对比）。
- result/cache 带 `strategy_id` 标记（供下游标注 provenance）。
- `signal_gate`（L295-296）：`used_aliases.add(ct)` 保留原生 claim_type 名 + 并入 legacy 别名（缓存改原生名后 gate 仍能匹配，向后兼容池化缓存）。
- `llm_synth`（L580）：prompt header 标注 `{strategy_id}` provenance。

**验证**：样本 342（池化）→ **72（deep_analysis-only）**，`legacy leak: []`；theme 别名（n<10）被 `_CLAIM_MIN_N` 自动滤除；分类结果：
- EFFECTIVE = `[technical_breakdown +1.21%, insider_bearish +0.47%, product_catalyst +0.41%, valuation_expensive +0.37%, earnings_beat +0.27%]`
- INEFFECTIVE = `[valuation_cheap −0.59%]`

prompt 实测输出"基于72条deep_analysis历史信号…"；cron `--claim-ic` → `compute_claim_ic()` 默认 deep_analysis，产出一致。

**⚠️ 审查点**：此改动**已用 deep_analysis-only 覆盖了生产缓存** `claim_ic_cache.json`（72 样本），替代原 342 池化缓存。需确认这是期望行为（判断：是——池化缓存本就污染）。

---

## 改动 #4：conditions 改用 LLM claim_type 直接映射（fallback 关键词）

**文件**：`adapter.py`（2 处）

**根因**：`to_ic_snapshot` 已用 LLM 标注的 `claim_type`（实测 **100% 覆盖**，`llm_labeled=True`），但 `_map_bullish/bearish_conditions` 却在同一文本上重做更差的关键词匹配，大量塌缩到 `above_ma20/below_ma20` 默认，丢弃 claim_type 预测力。

**conditions 的下游消费（价值依据，FTSeek 侧）**：
1. `app/playbook/pipeline.py` L823-849：`factors = dict.fromkeys(_deep_conditions, True)` → `factor_exit_rules` 选退出策略（trailing 让利润跑 vs 快止盈）。
2. `app/playbook/pipeline.py` L907-963：conditions 按 `BULLISH/BEARISH_CONDITIONS` + `factor_ic_latest.json` 加权，方向冲突→SKIP。
   - **约束**：方向冲突门读的是 legacy 27-condition 的 factor IC（非 #2 的 claim IC），故 conditions 必须留在 FTSeek 27 枚举内——#4 只做 `claim_type→condition` 映射，不替换为 claim_type。

**映射表**（各 6 项，语义匹配）：

| claim_type | → bullish condition | claim_type | → bearish condition |
|---|---|---|---|
| product_catalyst | news_positive | valuation_expensive | pe_above_industry |
| earnings_beat | earnings_beat_streak | insider_bearish | insider_net_sell |
| valuation_cheap | pe_below_industry | technical_breakdown | below_ma20 |
| technical_breakout | above_ma20 | macro_headwind | news_negative |
| insider_bullish | insider_net_buy | sentiment_crowded | sentiment_bearish |
| macro_tailwind | news_positive | product_catalyst | news_negative |

**改法**：
- 新增 `_CLAIM_TYPE_TO_BULLISH_COND`（L41）、`_CLAIM_TYPE_TO_BEARISH_COND`（L49）。
- `_map_bullish_conditions`（L59）/ `_map_bearish_conditions`（L119）循环顶部（L68-72 / L127-131）：优先 `r.get("claim_type")` 映射，命中即 `continue`；未命中 fallback 到原关键词逻辑（**原逻辑完整保留**）。

**验证**：`py_compile` OK；12 个映射目标 **全 ∈ 27 合法枚举**（`illegal: NONE`）；**方向安全**——bull 映射无 BEARISH 泄漏、bear 映射无 BULLISH 泄漏（不给方向门引入反向 condition，无误 SKIP 风险）；真实 30 snapshot before/after：

| condition | OLD（关键词） | NEW（claim_type） |
|---|---|---|
| earnings_beat_streak | 0 | 11 |
| pe_below_industry | 3 | 6 |
| above_ma20 | 4 | 5 |
| insider_net_buy | 0 | 1 |
| sentiment_bullish | 12 | 5 |
| revenue_growth_high | 7 | 0 |
| volume_expansion / roe_high | 各 2 | 0 |

核心改善：18 条 `earnings_beat` claim（#2 实测 EFFECTIVE +0.27%）原被关键词误塌缩为 `revenue_growth_high`/`roe_high`，现精确映射为 `earnings_beat_streak`（BULLISH，方向门加权正确）。

**⚠️ 审查点**：`product_catalyst`/`macro_*` 无专用 condition 枚举，映射到 `news_positive`/`news_negative`（最近似，非精确）。彻底解法需扩 FTSeek 27 枚举 + factor IC，本次未做。

---

## 改动 #5：辩论方向中性化（去除结构性反做空偏置）

**文件**：`llm_synth.py`（3 处）

**根因（数据实证）**：实跑 160 条 deep_analysis 信号，方向分布 LONG=111(69%) / NEUTRAL=45(28%) / **SHORT=4(2.5%)**；scanner 明确分到 `trend_short` 桶的候选，最终 = 9 SKIP + 1 翻多 + **0 SHORT**。根因在辩论层两处不对称：
1. 分析师 prompt 要求"看多≥3、看空≥2"——结构性看多倾斜。
2. 挑战者被定义成"找被忽视的风险"的风险审查官——对 SHORT 论点系统性去找上行/挤空风险 → 分歧 → SKIP。

**改法**：
- SYSTEM_PROMPT（L80）：多空对等改为"看多、看空各≥2"，并明确"方向由证据权重决定，数据支持看空就坚定 SELL_SHORT，不必强凑看多论据"。
- challenger_system（L681）："审查/找风险" 改为"校准方向而非唱反调；**对称质疑**——看多查过度乐观，看空同样查过度悲观（超跌反弹/挤空）；不预设偏向、不系统性偏空"。
- challenger_user（L714）：检查项 2/3 从"bull 最薄弱 + bear 遗漏风险" 改为"bull/bear 各自最薄弱 + 双向对称查风险与机会"。

**验证**：`py_compile` OK；`SELL_SHORT + 独立一致` 与 BUY 走**完全相同**的 confidence 升档路径（中→中高），做空不再被特殊对待。

**⚠️ 审查点**：改的是 prompt 语义（非硬规则），SHORT 占比是否回升、方向准确率如何，需今晚首批带新字段样本（20-30 笔）验证。未凭空承诺平衡，仅移除已确认的结构性偏置。

---

## 改动 #6：挑战者 fallback 伪独立修复

**文件**：`llm_synth.py`（2 处）、`adapter.py`（1 处）

**根因**：`_run_challenger` 在挑战者模型（deepseek-v4-pro）不可用时 fallback 用主模型（qwen）当挑战者，但 `_merge` 仍硬编码记 `challenger_model="deepseek-v4-pro"`，且"方向一致"照常升 confidence。→ qwen-vs-qwen 的"一致"是**伪独立信号**，confidence 虚高，且 `_debate` 模型名错误（污染 IC 归因）。

**改法**：
- `_run_challenger`（L732）：fallback 结果打标 `_challenger_fallback=True`。
- `_merge_debate_results`（L744）：记录**真实**模型名（fallback 时=MODEL）；`_debate` 增 `challenger_fallback` 字段；**fallback 时"方向一致"不升 confidence**（debate_outcome 标注"降级为主模型(非独立)"）。
- `adapter._compute_confidence`（L146）：数值端同步——fallback 时不 +0.20（否则 label 修了但 numeric 仍虚高）。

**验证**：`py_compile` OK；行为矩阵：
| 场景 | conf label | numeric | challenger_model |
|---|---|---|---|
| 独立挑战者 + 一致 | 中→中高 | 0.70 | deepseek-v4-pro |
| fallback 自辩 + 一致 | 中（不升） | 0.50 | qwen3.7-max（真实名） |

---

## 改动 #7：factor IC 假指标 `trade_ic` 修复（FTSeek 侧）

**文件**：`scripts/factor_ic_backfill.py`

**根因（真 bug）**：section 3 计算 `trade_ic` 时，`signed` 被直接赋值为 `pnl_pct`，随后 `calc_ic` 又拿 `pnl_pct` 与自身求相关 → `trade_ic` **恒等于 ±1**（自相关无意义）。诊断脚本里所有 factor 都被标 `DEGEN-trade_ic=±1` 即源于此。且该字段**无任何决策逻辑消费**（screener/方向门读的是信号级 `ic`），纯属误导。

**改法**：删除自相关 `trade_ic`，改用真实盈利度量（`trade_wr` / `trade_avg_pnl_pct` / `trade_avg_pnl_usd` / `trade_avg_mfe_pct`，本就在算）；verdict 由 `avgPnL%` 定；把无意义的"IC vs TradeIC 分歧"检查换成有意义的**"预测IC vs 实际成交盈亏 分歧"**。

**验证**：重跑 backfill，输出 0 个 `trade_ic` 字段；新分歧检查产出真信号（`volume_expansion` 预测IC=+0.34 但实际 avgPnL=−0.07% → 预测未兑现）。

---

## 改动 #8：backfill 崩溃修复 + 两脑 claim_type 词汇打通（FTSeek 侧）

**文件**：`scripts/factor_ic_backfill.py`

**根因（真 bug，影响最大）**：`signal_snapshots.factors_json` 有三种历史形态——`list[str]`（旧27条件，276条）/ `list[dict]`（结构化 claim，51条）/ `dict`（deep_analysis 完整输出，15条）。旧代码 `for f in factors` 遇到 dict 元素直接 `TypeError: unhashable type: 'dict'` 崩溃。含义：**deep_analysis 开始产出结构化 claim 后，factor IC 的 cron 每天跑崩、`factor_ic_latest.json` 一直是旧的**（执行脑 IC 冻结）；且 claim_type 数据一直躺在 snapshot 里从未进入 factor IC。

**改法**：新增 `_extract_factors(raw)` 归一化器，把三种形态统一拍平为因子名字符串列表——`list[str]` 原样、`list[dict]`/完整输出取 `claim_type`、`{cond:bool}` 取真键，记录内去重。

**验证**：重跑 backfill 跑通（342 条全部纳入，旧条件 n 大增如 `above_ma20` 39→120）；**10 个 claim_type 首次进入 factor IC**（technical_breakdown ic=+0.88/n=17 等）；双脑对账从"UNREACHABLE=4 断裂"变为 **12 AGREE / 1 CONFLICT / 4 N/A**，两脑首次共用 claim_type 词汇。

**⚠️ 审查点**：`_extract_factors` 让 claim_type 与旧 condition 混入同一 `factor_ic_latest.json`；已确认二者命名无重叠、且方向门只查 27-condition 分类集，claim_type 键会被忽略（对方向门无副作用）。唯一真冲突 `valuation_cheap`（claim −0.59% vs factor +0.79%）需人工复核 n=21 样本。

---

## 附：A/B/C 排查结论（纠正上一轮误判）

本轮排查确认，上一轮全链路审查中我提出的 A/B/C 三项，其中两项是**误判**（诚实记录）：

- **A（归因账本"缺失"）= 误判**。真账本 `trade_records`（394行，exit_reason 391/394、strategy_id 391/394、realized_pnl 394/394）+ `trade_attribution`（215行）一直在填。上轮我误读了陈旧的 `data/closed_trades.json`（9条遗留）。
- **B（噪声驱动加权）= 已守住**。`screener._load_factor_ic_adjustments` 与 `pipeline.py` 方向门（L932）**均已有 `n>=30` 门槛**，退化维度不进决策。无需改。
- **C（空侧因子缺失）= 已由 #4/#5 从上游解决**。factor IC 计算是通用的，空侧因子缺失只因上游长期无空单；#8 修复后 backfill 已能纳入空侧 claim_type。
- 真正的缺陷是 #7/#8 两个 backfill bug，已修复。

---

## 待办（本轮未做）

- **#3 三重确认门**：入场端 confluence 检查（claim_type 数量/一致度达标才放行）。建议**先不做**——#1/#2/#4 已改变 prompt 输入、IC 归因口径、conditions 三个关键面，应先跑一晚积累 20-30 笔干净样本，再用真实数据定阈值，避免拍脑袋。

---

## 审查建议聚焦点

1. **#2 覆盖生产缓存**（72 vs 342）是否为期望行为——需确认。
2. **#4 近似映射** `product_catalyst→news_positive` 是否可接受，还是应扩 27 枚举。
3. 五组改动**只影响 skill 输出，未改 FTSeek 运行代码**；需确认 FTSeek `factor_exit_rules` 对新 condition 分布（earnings_beat_streak 增多）的退出行为符合预期。
4. **#1 avg_alpha 分类口径**：方向调整是否已在 alpha 回填阶段正确处理（SHORT 的 alpha 符号）。
5. **#5 方向中性**：分析师/挑战者 prompt 语义改动是否引入分析质量回退（如深度下降、论据凑数）——需今晚样本人工抽查。
6. **#6 fallback 门**：`challenger_fallback` 字段是否被下游其它消费方读取（除 confidence 外，如 IC 归因是否应剔除 fallback 样本）——待确认。

---

## 变更位置索引（供快速定位）

| 文件 | 关键行 | 内容 |
|---|---|---|
| `scripts/alpha_backfill.py` | L77-79 | `_CLAIM_MIN_N` / `_CLAIM_ALPHA_TH` / `_PRODUCTION_STRATEGY` |
| `scripts/alpha_backfill.py` | L109-138 | `load_claim_ic`（avg_alpha 分类，读时派生 effective/ineffective） |
| `scripts/alpha_backfill.py` | L308-331 | `compute_claim_ic(strategy_id=...)` + SQL 过滤 |
| `scripts/signal_gate.py` | L295-296 | 原生 claim_type + legacy 别名并集 |
| `scripts/llm_synth.py` | L80 | #5 分析师多空对等（各≥2，方向由证据决定） |
| `scripts/llm_synth.py` | L580 | prompt provenance 标注 |
| `scripts/llm_synth.py` | L681 / L714 | #5 挑战者 system/user 对称化（不系统性偏空） |
| `scripts/llm_synth.py` | L732 | #6 `_run_challenger` fallback 打标 |
| `scripts/llm_synth.py` | L744 | #6 `_merge` 真实模型名 + fallback 不升 confidence |
| `scripts/adapter.py` | L41-57 | 两张 claim_type→condition 映射表 |
| `scripts/adapter.py` | L59-116, L119-176 | `_map_bullish/bearish_conditions`（claim_type 优先 + 关键词 fallback） |
| `scripts/adapter.py` | L146 | #6 `_compute_confidence` 尊重 fallback（不 +0.20） |
| `scripts/factor_ic_backfill.py`（FTSeek） | section 3 | #7 删除自相关 `trade_ic`，改真实盈利度量 + 预测IC vs 实际盈亏分歧 |
| `scripts/factor_ic_backfill.py`（FTSeek） | section 2 | #8 `_extract_factors` 归一化 3 种 factors_json 形态，修崩溃 + claim_type 入 factor IC |
| `scripts/diag_ic_brains_reconcile.py`（FTSeek） | 全文 | 只读诊断工具：两脑对账 + PnL 归因账本体检（可复跑） |
