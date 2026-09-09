# PROPOSAL-20260905 · DA/UE 链路 prompt 优化最终方案（多模型双会话 · 经独立实证复核）

> 日期：2026-09-05。状态：**待用户裁决**。（截至 2026-09-06 治理审计时仍未裁决；裁决前不得实施。）
> 方法：两会话三模型。第一会话 = qwen-architect(DA) / kimi-reviewer(UE) / glm-verifier(实证) 独立分析
> （记录：`docs/reviews/REVIEW-20260905-da-ue-prompt-multimodel.md`）；
> 第二会话 = 交叉质询收敛 + **glm 对全部承重声明逐条独立复核**（9 条新声明：7 证实 / 2 部分证实）。
> 本文档只收录**经复核证实**的结论；被推翻的第一会话结论在 §2 明确撤回。
> 纪律：全部行为变更 = 影子先行 + 预注册 + 一次一变量 + n≥30 + 用户裁决；本包不含任何退出参数改动（不触 C1 冻结）。

---

## 〇、一页摘要

**最大的反直觉结论：prompt 的主病灶不是"措辞不够狠"，而是三处机制性故障——**

1. **代码层覆写把唯一单调信号抹平**（DA 根因）：挑战者 AGREE 分支把分析师 HIGH(8.0) 覆写成 6.0，77%(33/43) 的 HIGH 被抹平；score_band 实为全系统**唯一单调**的 LLM 字段（HIGH 均笔 +35.70 > MEDIUM·conf0.9 +9.43 > MEDIUM·conf0.8 −29.93，glm 逐项复核精确命中）。
2. **均值锚校准注入是塌缩成因**："你的历史命中率约 50%"使模型收敛到安全中值（代码注释自认 67% 集中 7.0），且 LOW 结构性=SKIP 逃生门（20/21），负向锚永远不可校准。
3. **write-only 字段**（产出无消费方）：DA 侧 entry_archetype/confirmation/tripwires 路由关闭时纯写入；UE 侧 self_tier 有区分度却零消费于任何闸门。

**UE 侧出血桶精准定位**：MARKET_CORROBORATED 12 笔 −379.10（不是 THESIS_ONLY）；只放行 HARD_EVENT 的反事实 = **+101.54（相对实际 −293.57 改善 +395.11）**，组合闸（MC 需 raw_conf≥0.70）= **+18.17（改善 +311.74）**。n=12 小样本，闸设计为"可撤闸"而非永久封禁。

**落地顺序（依赖图见 §6）**：先修尺子（W0 代码）→ 9/12 B11 与 9/16 蒸馏两个裁决点后动 prompt → UE 渲染侧/删 cap/v2.9/tier 闸影子。

---

## 一、复核证实的事实基线（glm-verifier 逐项复跑）

| # | 声明 | 复核 | 关键数字 |
|---|---|---|---|
| A1 | 挑战者覆写 bug | **证实**（代码+数据双重） | `llm_synth.py:2074-2077` AGREE 分支覆写；checkpoint HIGH 16/16 落库 6.0 且带 `_score_lowered_by_challenger=True`；gate 侧 33/43=76.7% 被抹平 |
| A2 | score_band 单调（PASS-only n=84） | **证实**（逐项精确命中） | HIGH n=26 均笔 +35.70/中位 +11.62/WR 84.6%；MEDIUM·0.9 n=39 +9.43；MEDIUM·0.8 n=19 −29.93；LOW 47/47 全 BLOCK。限定：HIGH 前 3 笔占净额 77.9%（outlier 依赖，证"有区分度"足够，证"HIGH 盈利"不足） |
| A3 | "7 档反向"是时代伪影 | **证实** | score=7.0 仅存在 8/25 前（n=98, −567.48, WR 46.9%）；6.0 主要在 8/25 后（n=55, +455.16, WR 81.8%）。band 契约 8/25 才上线 → band 无前史可回测 |
| A4 | LOW≈SKIP 逃生门 | **证实** | checkpoint LOW 21 条中 20 条 direction=SKIP → LOW 永远无法用实现 PnL 校准 |
| A5 | 触发器计数塌缩 | **证实** | tripwire_count=2 占 236/237；invalidation_trigger_count=2 占 **237/237** → 依从率 100%、信息量 0（第一会话"依从率 19-43%"作废，判据须改为多样性） |
| A6 | 蒸馏影子 v1 臂 = live prompt | **证实** | `da_prompt_distill_shadow.py:85-97` v1 臂直读生产 checkpoint；9/1-9/4 已采 113 行 → **9/16 裁决前任何 SYSTEM_PROMPT 改动（含纯删减）都会污染对照** |
| A7 | 分桶密度 | **部分证实**（方向全成立，标签修正） | ATR×regime 6 格中 n≥30 仅 3 格（high/risk_on 134、high/cautious 49、medium/risk_on 44）；1-D regime：cautious WR 79.4% 均笔 +16.81 vs risk_on WR 52.6% 均笔 −5.19。**修正：qwen 的"n=255"是全链路口径非 DA-only**（DA-only 全史 290 笔）；方向结论不变 |
| A8 | 均值锚代码自认 | **部分证实** | 注释存在（llm_synth.py:1045-1052）；精确 131/195=67% 不可复现（全窗扫描最近 129/196=65.8%），定性"2/3-4/5 塌缩于 7.0"成立 |
| A9 | DISAGREE 口径矛盾 | **证实且可解释** | gate 口径 DISAGREE=8（其中 5 条真反向），**8 条全部 decision=BLOCK** → 不产生指令行，指令侧口径天然看不到。第一会话"DISAGREE=0"系采样偏差 |
| B1 | THESIS_ONLY 夹逼后仍可漏进 inject | **证实**（机制+数据） | 夹逼上限 0.60 > min_conf 0.5（`llm_universe_scan.py:1363-1367` vs `:1961`）；库内实有 4 条 THESIS_ONLY 指令、3 笔成交。细化：实测为"原生 0.5-0.60 档"漏进，夹逼路径尚无实例 |
| B2 | 负反馈表注入形态 | **代码证实** | `_build_feedback_table`（L1064-1093）+ 注入点 L1256-1263；字面 prompt 文本未持久化（链路不留 prompt），样例为代码等价重建 |
| B3 | UE tier 闸反事实 | **复跑** | CF1 只放行 HARD_EVENT：**+101.54**（Δ+395.11）；CF2 MC 需 raw≥0.70：**+18.17**（Δ+311.74），被拦 8 笔合计 −295.42 恰是出血主体；交叉命中变体数据不可得（MC 标的与 earnings cross_hit 影子零交集） |

## 二、撤回/修正的第一会话结论（防后人引用旧论）

| 第一会话结论 | 处置 | 真相 |
|---|---|---|
| "score_band 三档塌缩、无区分度" | **撤回** | band 单调且是唯一单调字段；塌缩的是合并后 score（覆写所致） |
| "score 7 档反向 = 说服力与预测力脱钩" | **撤回** | 时代伪影（8/25 契约切换），两桶不可比 |
| "debate DISAGREE=0，独立方向设计失败" | **修正** | 真反向 5 次全被 gate BLOCK；指令侧看不到属采样偏差。"PARTIAL 是唯一分离度字段"仍待全史验证（H5） |
| "84% 入场即错单在开盘 90 分钟追入 → 加强反追涨措辞" | **撤回** | 基率伪影（全体 93.9% 在早窗）；措辞杠杆≈0，真消费方在执行层路由开关（ADR-035，影子 n=4/20） |
| "甲类删减零风险" | **修正** | 零行为风险但**高测量风险**（A6）→ 排到 9/16 后 |
| "UE 出血=THESIS_ONLY/事件驱动高波" | **修正** | 真出血桶 = MARKET_CORROBORATED（−379.10/12 笔）；HARD_EVENT 为正（+101.54/17 笔） |

## 三、根因链（DA 侧）

```
"命中率约50%"均值锚注入 → 模型收敛安全中值(7.0 占 2/3+)
→ 挑战者 AGREE 分支覆写 score(33/43 HIGH 被抹成 6.0)
→ gate 输入(da_score)退化 → "LLM 自评无信息"假象
→ 结论被误读为"prompt 要加更多约束" → prompt 膨胀 → 更多 write-only 字段
```

**修法层次**：删均值锚 + 修覆写 = 治根因；分桶事实表注入 = 治传导；scenarios 纯攒样 = 治症状（绝不接门）。

---

## 四、DA 最终优化包（批次 W0→W6，一次一变量）

### 丙类 · W0 前置代码修复（非 prompt，9/06 即可启动 —— **尺子不修，一切影子白攒**）

| # | 位置 | 动作 | 判据 |
|---|---|---|---|
| 丙1 | `llm_synth.py:2074-2077` | AGREE 分支删 `initial["score"]=float(_c_sc)` 覆写，挑战者分改记 `_challenger_score_shadow` | 离线反事实（237 行 gate 日志重算 da_score=8.0 的判定翻转率）先行；前向 10 交易日 PASS/BLOCK 分布漂移可解释 |
| 丙2 | 指令投影处 | `_score_band_applied` 写入 `raw_instruction_json.score_band`（修复 glm"库内无 band"的测量盲区） | 落库后 score_band 非空率 100% |
| 丙3 | `tripwire_checker.py:150,225` + `llm_universe_scan.py:1380/2140` | metric `成交量`→`量比`（比值代码算，模型永不写股数 → 单位歧义在契约层消失） | **双链路同改**（共享 SSOT `_IT_METRICS`）；随 W4 批次 |

新增 `scripts/shadow_field_cardinality.py`（只读盘后跑）：对离散字段做基数自检（唯一值数/最大单值占比/归一化熵），DEGEN 判定可计数。

### 甲类 · W1 纯删减（**9/16 蒸馏裁决后**才动，A6）

甲1 SYSTEM L43 核心原则7 + USER L186 首句删（真源留 L162）；甲2 SYSTEM L78 改指针；甲3 USER L215 删；
甲4 字符串版 stop_loss/take_profit **待 H9 grep 验证消费方=0 后删**；变更史段**不动**（蒸馏对照）。
判据：行为等价（direction 一致率≥95%、band 分布 KL 不显著，n≥250）。

### 乙类 · 影子批次（新措辞全文，可直接粘贴）

**乙2 · W2（与 W1 同窗，9/16 后）— scenarios 三情景概率（纯攒样，零消费方由单测钉死）**

USER 输出契约新增（置于 score_band 之后）：
```text
    "scenarios": {{
        "horizon_days": 整数(本情景组的判定持有天数, 与 execution_plan 的持有期一致),
        "p_bull": 整数百分比, "p_base": 整数百分比, "p_bear": 整数百分比,
        "target_bull": 数字价位, "target_base": 数字价位, "target_bear": 数字价位,
        "p_def": "一句话: p_bull 的定义 = 在 horizon_days 内触及 target_bull 的频率估计"
    }},
```
```text
【scenarios 填写规则 — 硬性, 违约可计数】
1. p_bull+p_base+p_bear 必须恰好=100; 每个值必须是 5 的整数倍且落在 [5,80] 闭区间。禁止 0 或 100。
2. target_bear < target_base < target_bull 严格成立, 三者均在【有效现价】±25% 以内。
3. p 是频率声明不是信心声明: 不得因 direction=BUY 就给 p_bull 最大值 — 方向与概率是两件事。
4. 禁止参考任何"你的历史命中率约X%"均值锚(本 prompt 不提供也不允许自行假设);
   唯一历史参照是【历史分桶事实表】, 且仅在本格 n≥30 时用于校准 p_base/p_bear 相对宽度。
5. scenarios 不进入任何门禁/评分/仓位计算, 只被记录用于事后校准。
   写不出三档目标价 = thesis 失效边界不清, 应降 score_band 档位(而不是编造目标价)。
6. 叙事段(bull_case/bear_case/causal_chain/executive_summary)不参与任何门禁评分, 只供人读;
   唯一被机器消费的: direction / score_band / scenarios / invalidation_triggers / 各价位数字字段。
```
防退化（针对"六档塌缩"教训）：闭网格 + 和=100 + 单调 target + **落库基数自检**（唯一值<5 或最大单值占比>60% 或熵<0.60 → DEGEN）+ **冗余检查**（|spearman(p_bull, band 序数)|>0.95 → 判"band 换皮"直接砍字段）。
90 天判据：p_bull 五分桶胜率单调 + Brier<0.25（每桶 n≥30）；退化回退 = **删字段不留常量**。

**乙5 · W3 — 删均值锚，换分桶事实表注入（SYSTEM L75-78 整段替换）**
```text
【历史分桶事实表 — 系统按本系统实际成交算好注入, 你只读不算, 禁止自行估计】
口径: 本系统已平仓 n 笔, 按 (波动率桶 × 市场regime) 分格。"均笔"=费后美元/笔。
n<30 的格不给数, 只给 regime 边际。
· 本次标的所在格 = {vol_bucket} × {regime}: n={n_cell} 胜率={wr_cell} 均笔={avg_cell}
· 该格 n<30 → 回退 regime 边际: risk_on n=189 胜率51.9% 均笔−$5.39 / cautious n=66 胜率80.3% 均笔+$16.39
  (数字由代码每次实算填充, 此处为本窗实值示意)
· regime 边际也 n<30 → 回退全局。
使用规则(硬性):
1. 本表描述本系统在该格内的历史成交结果, 分格维度只有波动率与regime — 不含标的、不含方向,
   它不是"本股历史胜负", 与被证伪的标的级先验是两回事。
2. 禁止用它决定 direction, 禁止用它调整 score_band 档位 — 档位只由输出契约三条锚定判据决定。
3. 允许且仅允许两种用途: ① 本格均笔为负时在 risks 如实写一句; ② 本格 n≥30 时校准 scenarios
   p_base/p_bear 相对宽度。
4. 本格 n<30 → 在 data_quality_note 写明"历史参照样本不足"并忽略该格。
```
判据：H_band ≥ 基线−0.02、HIGH 产出率漂移 ≤5pp（基线 18.1%）、契约违约率不升。20 交易日窗。
**禁止写成"多给 HIGH"激励**（HIGH 正桶是 outlier 依赖的弱证据，写入即过拟合）。

**乙3+乙1 · W4（9/12 B11 裁决后）— 挑战者改 kill-condition 制 + 分析师触发器去常量**

挑战者 SYSTEM 输出契约替换：
```text
铁律(反仪式化):
- kill_conditions 是你唯一的实质输出。verdict=AGREE 时它必须是空数组 [] —
  承认"证伪失败"是合法输出, 且此时不得在 missed_risks 里塞入任何可量化阈值来变相表达异议。
- verdict=DISAGREE 时必须给出至少 1 条通过全部合法性检查的 kill_condition, 否则系统自动降级 PARTIAL_AGREE。
- verdict=PARTIAL_AGREE 时给 1-3 条, 每条必须指明它 kills 的是 thesis / level / risk 中哪一个。

kill_conditions 契约(与分析师 invalidation_triggers 同 schema, 同一消费方 tripwire_checker):
  metric ∈ {收盘价, 量比, 5日涨跌, 盘前涨跌幅}   ← 白名单是单一真源, 不在本 prompt 内扩展
  operator ∈ {<, >}     value = 纯数字     deadline = YYYY-MM-DD
  basis   = 该阈值在上方原始数据中的出处原文(≤40字), 例: "支撑位186.5(K线20日均线)"
  origin  = "challenger"
合法性(代码逐条校验, 不合法条目静默丢弃并计入违约计数):
  · 价格类(收盘价) value 必须落在本股【有效现价】[0.70, 1.30] 倍区间内 —
    永真/永假阈值或把 VIX/SPY/其他标的数值套到本股, 一律判无效。
  · 量比 value ∈ [0.3, 10.0]; 量比=当日成交量/20日均量由系统计算, 禁止写成交量股数。
  · basis 必须在上方原始数据中子串命中; 命不中 = 幻觉, 会被记录。
```
分析师侧（乙1）：删 tripwires 字段；invalidation_triggers 改"恰好 2-4 条 + 任意两条 (metric,operator) 组合不重复 + 必须覆盖价格类与量能/时间类两个维度"。
判据（**已替换失效的"违约率≤20%"**，A5）：20 交易日窗内 distinct(metric,operator) 组合 ≥5 且"恰好 2 条"占比 ≤70%（现 100%）。
合并裁决：AGREE→kill 必须空、**不覆写分析师分**；PARTIAL→1-3 条记 `shadow_kill_partial=1`；DISAGREE→无有效 kill 自动降级。
影子门：`data/shadow/kill_condition_shadow.jsonl`，命中 n≥30 且命中切片 WR − 未命中切片 ≤ −15pp → 才可议 enforce。攒样估算：≈0.26 命中/日 → **60-115 交易日**（按 n 不按日历裁决）。

**乙6（W5）/ 乙4（W6）不出终稿**：乙6（删 prompt 内 SL 数值段）触 admission SL_STOP_TOO_WIDE 需联动；乙4（回踩三数值契约）依赖 ADR-035 影子（n=4/20 远未达标）。证据到位前不写死文本。

## 五、UE 最终优化包（批次 U1→U6）

### U1 · clue 渲染侧三处（输入变更，与 U4 同窗影子）
1. **极端带行不渲染**：`_gather_premarket_rank_clues._fmt_side` L1039-1042 extreme 循环整段删（采集端 candidates json 保留，攒样不丢）。回滚开关 `clue_render_extreme_band: false`。
2. **gainer≤loser 行数对称**：同函数 L1044-1045，gainer 行截断至 ≤loser 行数（losers=0 则 gainers 全不渲染——纯涨幅日=追榜日，预注册声明此特性）。回滚 `clue_side_symmetry: false`。
3. **交叉命中集落盘**：`_corro_syms` 落 `data/ue_corro_syms_{ET}.json`（scan 写、inject 读），供 U3 tier 闸消费。

### U2 · 删 confidence cap（存储语义恢复，近零行为风险）
删 `_UE_CONF_CAP`（`llm_universe_scan.py:2088-2089`），指令落 raw_conf（50-92 连续）。
依据：防抢占动机已不存在（排序主键 `_src_priority`，代码注释 L2084-2086 自认）；cap 现存效果只剩污染分桶 + UE 源内次键排序打成平局；max_n=30 几乎不截断 → 排序行为变化≈0。
**护栏**：conf 只用于归因分桶与源内次键，**禁止**线性加权/连续门（raw 非单调：61-75 桶最差 −248.74、>75 桶唯一正 +74.08）。rubric_score 不作主键（0-4 更粗且自评可游戏化），只作闸的布尔补强（≥3）。

### U3 · tier 闸（先影子-only，后 enforce；**先于月报 D3 整体影子化执行**）

| self_tier | 处置 | 条件 |
|---|---|---|
| HARD_EVENT | 直进 | 仍过既有 conf/streak/持仓/价格地板/低 ATR 闸 |
| MARKET_CORROBORATED | 需补强 | symbol ∈ 当日交叉命中集，**或** raw_conf≥0.70 且 rubric_score≥3；否则影子-only |
| THESIS_ONLY | 影子-only | 现状漏洞：夹逼上限 0.60 > min_conf 0.5 必漏（B1 证实，库内已有 4 条指令 3 笔成交） |

预注册判据：攒样 ≥15 交易日且被影子化桶 fwd 回填 n≥30。毕业：被拦桶方向性前瞻均值 ≤0 且放行桶均笔 >0。撤闸认错：被拦桶前瞻 >+0.3% 且 n≥30。
**先后序**：tier 闸是 D3 的精确子集（D3 会连 +101.54 的 HARD_EVENT 一起关）→ 先 tier 闸；上线后 UE 残余净额仍 <0 再观察 ≥15 日，才升级 D3 整体影子化作兜底。
**诚实声明**：MC 桶 n=12，均笔 −31.59 可能由少数大亏主导，未做 permutation/聚类 → 闸设计为可撤而非永久封禁。

### U4 · prompt v2.9 全文（kimi 收敛稿，55 行→26 行 −53%）

```python
SYSTEM_PROMPT_V2 = (
    "你是资深事件驱动型美股分析师。职责: 从给定材料浮现受事件/市场驱动的可交易美股标的, 多空对等, 质量分层由下游代码执行。\n"
    "输出契约(逐字段, 缺字段=违约):\n"
    "1. symbol: 美股代码不带前缀, 只输出真实可交易标的; direction: LONG/SHORT/SKIP, 不确定=SKIP。\n"
    "2. evidence_ref: 引用给定材料中的具体证据原文(新闻标题/异动数据/人工线索); 仅凭行业常识或板块联想则留空字符串。"
    "代码将核验引证真实性, 空引证将被强制降级。\n"
    "3. self_tier: HARD_EVENT=因果链锚定evidence_ref里的具体事件(封锁/战争/财报/监管/事故); "
    "MARKET_CORROBORATED=无具体事件但evidence_ref是真实市场异动/板块轮动数据; THESIS_ONLY=evidence_ref为空仅逻辑联想。\n"
    "4. causal_chain: 事件→传导→标的一句话; thesis: 交易论点≤60字(THESIS_ONLY必须以'板块联想:'或'逻辑推演:'开头)。\n"
    "5. confidence: 0~1小数, 如实反映证据强度(HARD_EVENT>MARKET_CORROBORATED>THESIS_ONLY; 空引证者将被代码封顶0.60)。\n"
    "6. invalidation: 一句话失效条件≤40字(非SKIP必填); invalidation_triggers: 代码可判定失效条件数组, "
    "每条{metric,operator,value}, metric限收盘价/成交量/5日涨跌/盘前涨跌幅, operator限<或>, value纯数字, 无可判定条件填[]。\n"
    "7. evidence_checklist: {catalyst_specific,catalyst_within_24h,price_volume_confirmed,crowding_low}均bool, 如实勾选不得美化。\n"
    "8. horizon_hours: 预期兑现时效整数小时(日内动量4-8); expected_move_pct: 预期方向幅度%小数; "
    "is_universe_external: bool; winner_loser: WINNER/LOSER; corroborated_by_market: bool。\n"
    "9. 仅SHORT填short_check对象{borrow_feasible(bool),downside_catalyst_expiry_hours(int),squeeze_risk(1-5)}, 非SHORT填null; "
    "无具体下跌催化剂(仅'估值高'/'涨多了')的SHORT不得浮现。仅SKIP填skip_reason一句话, 非SKIP填null。\n"
    "不输出任何价格数字(entry/止损/止盈由下游代码计算)。严格只输出JSON, 字段与顺序与示例完全一致:\n"
    "{\"surfaced\": [\n"
    " {\"symbol\": \"AAPL\", \"direction\": \"LONG\", \"evidence_ref\": \"材料中具体证据原文\", \"self_tier\": \"HARD_EVENT\", "
    "\"causal_chain\": \"事件→传导→标的\", \"thesis\": \"交易论点≤60字\", \"confidence\": 0.72, "
    "\"invalidation\": \"一句话失效条件\", \"invalidation_triggers\": [{\"metric\": \"收盘价\", \"operator\": \"<\", \"value\": 185.5}], "
    "\"evidence_checklist\": {\"catalyst_specific\": true, \"catalyst_within_24h\": true, \"price_volume_confirmed\": true, \"crowding_low\": true}, "
    "\"horizon_hours\": 6, \"expected_move_pct\": 2.5, \"is_universe_external\": false, \"winner_loser\": \"WINNER\", "
    "\"corroborated_by_market\": true, \"short_check\": null, \"skip_reason\": null},\n"
    " {\"symbol\": \"XYZ\", \"direction\": \"SHORT\", \"evidence_ref\": \"材料中具体证据原文\", \"self_tier\": \"MARKET_CORROBORATED\", "
    "\"causal_chain\": \"事件→传导→标的\", \"thesis\": \"交易论点≤60字\", \"confidence\": 0.65, "
    "\"invalidation\": \"一句话失效条件\", \"invalidation_triggers\": [], "
    "\"evidence_checklist\": {\"catalyst_specific\": true, \"catalyst_within_24h\": true, \"price_volume_confirmed\": true, \"crowding_low\": false}, "
    "\"horizon_hours\": 8, \"expected_move_pct\": 3.0, \"is_universe_external\": true, \"winner_loser\": \"LOSER\", "
    "\"corroborated_by_market\": true, \"short_check\": {\"borrow_feasible\": true, \"downside_catalyst_expiry_hours\": 24, \"squeeze_risk\": 2}, \"skip_reason\": null}\n"
    "]}"
)
```

修复点：①字段表补 invalidation_triggers（v2.8 漏，E3 规律"字段表没有≈不存在"，实测填充率仅 36%）；②LONG 示例 short_check/skip_reason 改 null + 新增完整 SHORT 示例（v2.8 示例自违 null 规则，在教错误行为）；③删"充分浮现不设数量上限"（放量偏置，质量分层已由 tier 闸接管）；④删板块级联/结构约束/回调优先三散文（不可测不依从，clue 渲染侧与 tier 闸已对冲）；⑤夹逼写成可预期事实（"代码将核验/封顶"）替代"自律约束"。
**联动注意**：第 6 条白名单 `成交量` 将在 W4 随丙3 双链路同步改 `量比`，v2.9 落稿时先保持现值，丙3 上线时一并改。
行为风险（预注册声明）：浮现量下降是预期效果（监控 HARD_EVENT 桶量不塌缩）；SHORT 示例可能轻微抬 SHORT 产出率（第 9 条门槛保留）。

### U5 · 负反馈表小修（一行级）
注入前滤掉"待回填"行（`_build_feedback_table` L1084）；其余不改（fwd 单日噪声风险有界，已有"除非新增量证据"豁免 + same_symbol_cooldown_days=3 硬兜底）。

### U6 · 模型维持 glm-5.2
不回 max、不做在线 A/B（隔天切换=日期混淆；受控 bakeoff 已有契约依从证据 glm 14/18 vs qwen 0/x）。
干净归因设计：用**浮现级 fwd 回放**（按 scan jsonl 的 model_used 分组比方向性前瞻）而非成交级 PnL —— chase_cap enforce 拦的是执行入场，不进入该口径；9/4 起成交样本单标 enforce 期做交互切片。

---

## 六、总排序与依赖图

```
立即(纯代码/存储语义, 不动 prompt 文本):
  W0 丙1-2 (修覆写+落band) + 基数自检脚本      ← 尺子优先, 9/06 可启动
  U2 删 cap 落 raw_conf                        ← 近零行为风险
  U5 负反馈滤"待回填"行                         ← 一行级

9/12 B11 裁决点 ── 解锁挑战者类变更 (W4)
9/16 蒸馏裁决点 ── 解锁 SYSTEM_PROMPT 任何改动 (A6)

9/16 后:  W1 甲类删减 + W2 乙2 scenarios (同窗, 判据正交)         [10 交易日 / 90 天+n≥100]
          U1 渲染侧 + U4 v2.9 prompt (同窗影子, UE 侧一次一变量)   [≥15 交易日]
W1/W2 后: W3 乙5 分桶注入 (串行: 同被测量 band 分布)              [20 交易日]
W0 后:    W4 乙3+乙1+丙3 (挑战者+触发器, 双链路同改)              [60-115 交易日按命中数]
W4 后:    W5 乙6 (admission 联动, 单独窗)
最后:     W6 乙4 (等 ADR-035 影子 n≥20)
UE 影子链: U3 tier 闸影子 ≥15 日+n≥30 → 毕业裁 enforce → 残余<0 再 15 日 → 才议 D3 整体影子化
```

串行约束：W3↔W1/W2（同被测量）；W5↔W4（同触 admission）；丙3 必须 DA/UE 双链路同窗（共享 `_IT_METRICS` SSOT）。

## 七、裁决请求清单（逐条独立可裁）

| # | 提案 | 性质 | 关键判据 |
|---|---|---|---|
| P1 | W0：丙1 修覆写 + 丙2 落 score_band + 基数自检脚本 | 代码修复（生产行为变更：gate 输入变准） | 离线反事实先行 + 前向 10 日 |
| P2 | U2：删 _UE_CONF_CAP 落 raw_conf | 存储语义恢复 | 源内排序行为漂移≈0 确认 |
| P3 | U1+U4：渲染侧三处 + v2.9 prompt 同窗影子 | 行为变更（影子） | ≥15 日；HARD_EVENT 桶量不塌缩；填充率升（基线 36%） |
| P4 | U3：tier 闸影子-only（含 _corro_syms 落盘） | 行为变更（影子） | 毕业/撤闸判据见 §五-U3 |
| P5 | W1+W2：甲类删减 + scenarios（9/16 后启动） | 行为变更（影子/等价性） | 等价性 + 基数自检 + 90 天 Brier |
| P6 | W3/W4/W5/W6 按依赖图立项 | 行为变更（影子） | 各批次判据见 §四 |
| P7 | 军规入库：**prompt 字段消费方登记制**（无消费方字段一律删；新字段须登记消费方+基数自检） | 治理 | — |
| P8 | UE 维持 glm-5.2；模型归因走浮现级 fwd 口径 | 选型 | — |
| P9 | 月报 D3（UE 整体影子化）**暂搁**：以 tier 闸为先行精确子集，tier 闸 enforce 后残余净额仍 <0 且再观察 ≥15 日才升级 | 排序裁决 | — |

## 八、诚实边界与遗留

1. MC 桶 n=12、HIGH 桶前 3 笔占净额 77.9%（outlier 依赖）——足以证"有区分度"，不足证"稳定盈利"；全部反事实为同窗回放，非未来承诺（MDE ±$335/日）。
2. A7 口径标签已修正（255=全链路非 DA-only）；A8 精确 131/195 未复现（定性成立）；B2 prompt 字面文本未持久化（代码等价重建）；B3 交叉命中组合闸数据不可得（MC 标的与 earnings cross_hit 影子零交集）。
3. 待验证假设（各附验证法，见第二会话记录）：H5 HIGH outlier 稳定性（按日聚类 bootstrap）、H6 8/25 前后 WR 跳变不可归因、H7 修覆写对 PASS 集的翻转率（离线反事实）、H8 量比数据源可得性、H9 字符串版 SL/TP 消费方=0（grep）、H10 DISAGREE 两口径锁死（B11 裁决前置）。
4. 本包不改任何退出参数（不触 C1 冻结）；乙6 触 admission 校验故单独成窗。
5. 全部数字可在 `data/ftseek_us.db` / `data/da_gate_decisions.jsonl` / `data/da_checkpoint/` / `data/shadow/llm_universe_scan.jsonl` 复现（glm 复核轮全部只读 mode=ro）。

---
*三模型双会话：qwen-architect(DA) × kimi-reviewer(UE) × glm-verifier(实证复核) · 主会话合成 · 2026-09-05*
