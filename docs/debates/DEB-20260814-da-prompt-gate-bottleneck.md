# DEB-20260814: DA 出不了指令 — 分析师过度保守 × gate decision_conf 结构性卡死

> **立案日**: 2026-08-14
> **状态**: 提案裁决中 (未实施; 证据全量实测, 供跨模型辩论)
> **立案人**: Hermes (应用户要求, 供另一 Agent 分析辩论; 不预设立场)
> **触发**: 用户连续观察"DA 无法产出合理标的股与指令" (8/13 盘中重跑实验 35 只分析 → 仅 3 条 WARN 小仓落库, 0 PASS); MU(+5.9%)/SNDK(+15.1%) 当日大涨未被识别
> **关联**: DEB-20260811-quality-gate-calibration.md (gate 校准同族), `~/.hermes/skills/deep-stock-analysis/scripts/{llm_synth.py,signal_gate.py}`, `data/da_gate_decisions.jsonl`
> **同日前置事件**: 8/13 盘中重跑发现 OpenD 连接风暴 (35/35 快照采集超时, LLM 看到现价 $0) 已修复 (data_collector.py 信号量+缓存, 验证 18/18)。**本文讨论的是修复数据通道之后仍存在的第二层问题** — 数据健康时 DA 依然出不了指令。
> **分析方增补 (2026-08-14, Qoder 独立核验)**: 见 §九。要点: ①开放问题#1 已用成交净额+信号前向 alpha 双口径实测回答 — WARN 组不劣于 PASS 组, 但两组皆≈零期望, agreed 位无 outcome 区分度; ②§2.3"conf 0.70 唯一路径"已过时 (8/6 弃权补丁 L392-399 实为第二路径, 当前 PASS 率 4.7%/18.5% 而非 1.4%); ③MU/SNDK 压制机制实锤 (自评 conf=0.8 被溯源校验压成 SKIP, 且两只 7/14 起每日在池, 非池子漏检); ④P0 有条件支持 + 三点修正, 新增 P5(机器验证)/P6(口径对齐) 提案。

---

## 一、主张拆解

| # | 主张 | 类型 | 现状 |
|---|---|---|---|
| P0 | gate 分级门槛 decision_conf 结构性卡死: 84% 有方向记录 conf=0.50, PASS 门槛需 ≥0.6/0.7 → 分数再高也只能 WARN 微仓。方向一致但未 agreed 应给 0.60 (良/PASS小仓), 只有真反向才 0.35 | 主提案 (改 signal_gate.py, 可 A/B 可回滚) | 未实施 |
| P1 | SYSTEM/USER prompt 的"KO级深度要求"是及格线而非加分项: 强制每只候选挖出公司独有事件/独有计算, 数据普通的票只能编造(→幻觉BLOCK)或观望(→SKIP)。应降级为加分项 | prompt 修订 | 未实施 |
| P2 | 缺"数据充分性前置路由": 数据缺失时仍逼 LLM 输出完整 execution_plan (具体价位), 借鉴 financial-data-query 的空结果纪律 — 数据不足只能 SKIP+如实说明, 禁止出价位 | prompt 修订 | 未实施 |
| P3 | 挑战者信息不对称: 只看 data_text[:4000]+分析结论[:3000] (截断), 分析师看全文; 截断导致低质量审查 → 低 agreed 率 → conf 卡 0.50 (与 P0 同根) | prompt/代码修订 | 未实施 |
| P4 | 分析师定位错位: prompt 在培养"KO级研报分析师", 系统需要"可执行 setup 识别器"; 83% 观望说明门槛在过滤几乎所有票而非过滤差票 | 结构性主张 (供辩论, 不预设改法) | — |

**争议核心**: P0 是纯代码、杠杆最大、直接命中"出不了 PASS"; 但反对意见可能成立——conf 卡 0.50 也许是**市场真的没有共识机会**的正确反映 (辩论双方应验证: 历史 conf=0.50 WARN 样本的成交结局 vs conf=0.70 PASS 样本, 若 WARN 样本结局不差于 PASS, 说明分级门槛过严; 若明显更差, 说明门槛正确、问题在上游 LLM)。P1-P3 改 prompt 语义, 属"一次一变量", 应在 P0 验证后逐个上。

---

## 二、证据包 (全部实测, 8/14 01:00 核实)

### 2.1 gate 全量分布 (da_gate_decisions.jsonl, n=1128, 可解析 966)

| 维度 | 分布 |
|---|---|
| decision | BLOCK 718 (74%) / WARN 234 (24%) / **PASS 14 (1.4%)** |
| tier | SKIP 666 (69%) / 中 234 / 差 52 / 优 13 / 良 1 |
| decision_conf 均值 | 0.531, 中位数 0.500 |
| conf ≥0.7 (PASS优门槛) | 168/966 = 17% |
| conf =0.50 (弃权/同向未确认) | 752/966 = **78%** |
| conf =0.35 (真反向) | 34/966 = 4% |
| score 均值 | 5.1; score≥7 有 221 条, score≥6 有 378 条 |

### 2.2 死结实锤: 有方向记录中的 conf 分布 (n=300, LLM 真正给出 BUY/SELL_SHORT 的)

| conf | 数量 | 占比 | 结果 |
|---|---|---|---|
| 0.50 | 251 | **84%** | 最高只能 tier=中 → WARN → 落库 conf 压到 35 |
| 0.35 (真反向) | 34 | 11% | tier 降档 → BLOCK |
| 0.70 (真共识) | 14 | 4.7% | PASS |
| 0.60 | 1 | 0.3% | PASS |

**其中 score≥6 的 282 条**: 真共识(conf=0.70)仅 13 条; 同向未确认(conf=0.50) 235 条; 真反向(conf=0.35) 33 条。最终 PASS 14 / WARN 234 / BLOCK 34。
→ **分数不是瓶颈 (score≥6 占 94%), 辩论一致度是唯一瓶颈。**

### 2.3 门槛结构 (signal_gate.py L407-414, 现行)

```
优(PASS normal): score≥7 且 conf≥0.70
良(PASS small):  score≥6 且 conf≥0.60
中(WARN tiny):   score≥5 且 conf≥0.50
差(BLOCK):       其余
```

conf 达到 0.70 的唯一路径 (L389-391): 挑战者方向一致 **且** agreed=true **且** 非 fallback。三条件叠加。

### 2.4 今晚盘中重跑画像 (8/13 ET 11:05, 数据通道修复前的对照样本)

35 只分析: rating 观望 29 (83%) / 看多 6; direction SKIP 30 / BUY 5。执行层 5 条 BUY → gate 后落库 3 条 (CRM/GS/HON), 全部 WARN conf=35, 0 PASS。

### 2.5 历史 PASS 画像 (14 条全部)

UNH/MRK/NEM/MSI/GS/BWXT/LLY/AAPL/AVGO/TSM/BABA/ORCL/LLY/GOOG — 全部 score=7 + conf=0.70 (AAPL 6/0.60)。**无一条靠低分挤过, 无一条 conf<0.6** — 门槛本身筛选逻辑自洽, 争议在门槛是否过严 (见争议核心)。

### 2.6 prompt 现状摘录 (llm_synth.py, 供辩论方直接引用)

- SYSTEM_PROMPT L92-98 "KO级深度要求——缺一不可": A.公司层面独有事件 B.独有计算 C.反向警示 D.估值锚定 E.风险日历 F.交易计划分批
- USER_PROMPT L597-604 "KO级深度要求——必须满足": bull_case 至少1条独有事件 / bear_case 至少1条独有计算
- 挑战者截断: L811-819 `data_text[:4000]` + `initial_analysis json[:3000]`
- 挑战者强制表态: L786 "禁止 SKIP/观望…无理由的观望视为无效审查"
- SHORT 三问高门槛: L585-587 (2026-07-25 实证 SHORT 近14日命中29%全线负后设立, **此约束有实证依据, 辩论时不应轻易废除**)

### 2.7 做空通道现状

DA 自 07-20 以来 140 BUY / 0 SHORT (pipeline.py L86-87 注释自证)。空方供给唯一来源 UE 注入。今晚 UE 注入 JD 亦被 SKIP (SL 6.1%>6% 准入门)。

### 2.8 对照: financial-data-query 的设计哲学 (用户指定参考, 非照抄)

| 维度 | financial-data-query | DA 现状 |
|---|---|---|
| 职责分离 | 召回→识别→查询, 每步契约清晰 | 单一巨型 prompt 全包 (分析+辩论+挖事件+算价位+自评质量) |
| 空结果处理 | 显式规则 "status=SUCCESS≠业务成功, 必须查 data 是否为空, 空则如实降级" | 数据 12/18 缺失仍逼出完整 execution_plan |
| 禁止编造 | 源头约束, 失败如实告知 | 有约束但靠 gate 事后扣分 (幻觉检测) |
| 路由判定 | 先判数据类型再走对应链路 | 所有票同一套 KO 标准 |

---

## 三、PE 工程师视角诊断 (立案人立场, 供反驳)

1. **单一巨型 prompt 职责混杂**: 一个 3000-token 回复要同时完成研究分析+多空辩论+独有事件挖掘+独有计算+分批交易计划+风险日历+数据质量自评 = 4-5 个岗位的工作, 每个都做不深。
2. **KO 级硬要求制造系统性 SKIP**: KO 是极端优秀样本 (万豪换可乐/印度IPO), 设为所有候选的及格线 = 要求所有股票都是可口可乐。数据不丰富的票只能二选一: 编造数字(→幻觉BLOCK) 或 降级观望(→SKIP)。今晚 MU 8个/SNDK 5个"未溯源数字"被 BLOCK 即此机制产物。
3. **空结果无前置路由**: 数据缺失时被迫硬答 (8/13 现价$0 仍被要求出交易计划, 已随连接风暴修复缓解, 但 prompt 层无规则兜底)。
4. **辩论信息不对称**: 挑战者看截断数据审查全文分析, 低 agreed 率是结构必然 (见 P3)。

## 四、华尔街策略交易师视角诊断 (立案人立场, 供反驳)

1. **定位错位**: prompt 在培养"KO级研报分析师", 系统需要"可执行 setup 识别器"。研究深度与交易执行是两种能力, 前者挤占后者。prop trader 视角: 不需要每只票都有可口可乐级故事, 需要的是"今天这只票有没有正期望 setup"。
2. **83% 观望 = 选股指纹不对**: 真实市场每天都有可交易票。门槛不是"过滤差票"而是"过滤几乎所有票"。华尔街框架是**先定 setup 类型 (动量突破/回调企稳/事件驱动) 再筛票**, 现在反过来了。
3. **约束乘积效应**: SHORT三问 × 禁追涨 × 不接飞刀 × KO级要求, 每条单独合理 (多数有实证依据), 乘积把通道压没。
4. **可保留项**: 结构性约束 (止损2-5%/不接飞刀/回踩优先/盘前锚定) 全部是实证亏出来的, 辩论方不应废除。

---

## 五、建议方案 (按杠杆排序, 全部未实施)

| # | 方案 | 改动面 | 杠杆 | 风险 |
|---|---|---|---|---|
| P0 | decision_conf 重校准: 方向一致时即使 agreed=false 也给 0.60 (良/PASS small), 仅真反向 0.35 | signal_gate.py ~3行 | ★★★ 直接命中"出不了PASS" | 放行低质量共识 → 需 OOS 验证 (见§六) |
| P1 | KO级要求降级为加分项: 有独有事件加 confidence, 无不强制; 保留禁止编造 | llm_synth.py prompt 文本 | ★★ | 研报深度下降 |
| P2 | 数据充分性前置路由: prompt 注入"本次覆盖 X/18, 缺[…]", 规则"现价缺失只能 SKIP 禁出价位" | llm_synth.py + 采集层传参 | ★★ | 无 (纯防御) |
| P3 | 挑战者给全量数据 (或至少同等 snapshot/技术面段), 提高真共识率 | llm_synth.py L811-819 | ★ | token 成本上升 |
| P4 | 定位重构讨论: setup 类型前置路由 (动量/回调/事件分道, 各道独立标准) | 大改 | — | 仅辩论, 不预设 |

**实施纪律 (立案人坚持)**: 一次一变量 — P0 先行, 纯代码可 A/B 可回滚, 验证有效后再逐个上 P1-P3。回滚: P0 改回 L389-391 原逻辑。

---

## 六、预注册判据 (P0 若实施, OOS 影子验证)

- 样本: P0 生效后新产生的 gate 记录, n≥30 且覆盖 ≥10 个交易日
- 对照组: 现行逻辑 (conf=0.50→WARN) 历史样本 vs P0 逻辑 (conf=0.60→PASS small) 新样本
- 达标线: P0 放行组的成交净 PnL (pnl_net) 不劣于 WARN 组, 且 PASS 落库率 ≥30% (现行 1.4%)
- 否决线: P0 放行组净 PnL 显著劣于 WARN 组 (单尾 p<0.1) → 回滚, 承认"conf 卡死是市场无共识的正确反映"

---

## 七、留给辩论方的开放问题

1. conf=0.50 的 251 条有方向记录, 其历史成交结局 (含 WARN 落库成交) 是否真的劣于 conf=0.70 组? 这是 P0 成立与否的**唯一决定性证据**, 立案人尚未统计 (数据在 trade_records + playbook_instructions, 可按 decision_id JOIN)。
2. 分析师 83% 观望, 多少是 prompt 门槛问题 (P1), 多少是市场真的没机会? 对照: 同期 UE 通道/盘中异动票的实际走势。
3. P1 降级 KO 要求后, 如何防止滑坡到"通用模板废话" (当初设 KO 线正是为了治模板病, DEB-20260723 系)? 需要给出"反模板"的替代机制。
4. SHORT 通道是否值得独立重开 (绕过 SHORT 三问, 走独立做空 prompt 链)? 07-25 实证 SHORT 29% 命中是旧 prompt 期的数据, 是否已过时。
5. 辩论方若反对 P0, 须给出替代杠杆: 在不动 LLM prompt 的前提下, 还有什么能把 PASS 率从 1.4% 抬到可用水平。

---

## 八、证据文件与复算入口

- gate 全量日志: `/opt/ftseek/FTSeek-US/data/da_gate_decisions.jsonl` (1128 条)
- 今晚重跑产物: `/tmp/da_intraday_shadow/run2_result.json` + `run2_stderr.log`
- 现行 gate 门槛: `~/.hermes/skills/deep-stock-analysis/scripts/signal_gate.py` L381-444
- 现行 prompt: 同目录 `llm_synth.py` L69-114 (SYSTEM), L538-620 (USER), L779-830 (挑战者)
- 同日连接风暴案: `~/.hermes/skills/devops/ftseek-futu-broker-patterns/references/opend-connection-storm-20260813.md`
- 复算命令示例: `python3 -c "import json,re; gs=[json.loads(l) for l in open('data/da_gate_decisions.jsonl') if l.strip()]; ..."` (完整脚本见本文 §二 数字来源, 立案人会话 8/14 00:50)

---

## 九、分析方独立核验与增补证据 (2026-08-14, Qoder; 不修改立案人原文, 仅供裁决)

### 9.1 文档证据复核: 两项修正

**复核一致项**: §2.1/§2.2 数字全量复算一致 (可解析 966 条; PASS 14=1.4%; 有方向记录 conf=0.50 占 84%; score≥6 非瓶颈)。§2.6 prompt 引用逐条核对属实 (KO 要求/挑战者截断 `data_text[:4000]`+`[:3000]`/强制表态/SHORT 三问均在位)。

**修正1 — §2.3 "conf 达到 0.70 的唯一路径"已过时**: 现行 signal_gate.py L392-399 存在 2026-08-06 上线的第二路径 — 挑战者弃权(SKIP)时 `conf = max(0.50, score/10)`, score≥7 即达 0.70。该补丁已部分缓解卡死:

| 窗口 | 有方向记录 | PASS | PASS率(有方向口径) | 全量口径 PASS 率 |
|---|---|---|---|---|
| 8/6 前 | 251 | 2 | **0.8%** | — |
| 8/6 后 | 65 | 12 | **18.5%** | 12/257 = 4.7% |

→ 本文证据包以全历史为主口径, **低估了当前供给**; 残余瓶颈已从"挑战者弃权"转移到"同向未 agreed" (8/6 后 conf=0.50 且 score≥6 共 41 条, 是 P0 的真实受益集)。P0 的立论应针对这 41 条/月量级的增量, 而非文档标题级的 966 条全量。

**修正2 — 口径陷阱警示 (修尺子)**: `playbook_instructions.confidence` ≠ gate `decision_conf`。实证: 8/7 的 9 条 gate=优/良 指令落库 conf=50 (写入的是 LLM 语义 conf), 8/10-8/11 两条落库 70。同 tier 落库 conf 不一致 → 下游任何按 confidence 列的分桶分析都有测量误差 (见 9.5-P6)。

### 9.2 开放问题#1 的决定性证据 (本文核心增量, 双口径实测)

**口径A — 成交净额** (trade_records ⋈ playbook_instructions 按 decision_id JOIN, 再按 gate tier 精确分层, 费用净额 pnl_net / 仓位归一 pnl_pct 双列):

| gate tier | n | 净PnL | 单均净 | pct均 | 胜率(pct) |
|---|---|---|---|---|---|
| 中 (WARN, conf=0.50) | 107 | $-780.94 | $-7.30 | -0.091% | 53% |
| 优 (PASS, conf=0.70) | 8 | $-57.84 | $-7.23 | -0.072% | 50% |
| 良 | 1 | $-0.52 | — | — | — |
| 近窗口 8/1+: 中 | 31 | $-34.98 | $-1.13 | **+0.165%** | 52% |
| 近窗口 8/1+: 优 | 6 | $-37.08 | $-6.18 | -0.043% | 50% |

**口径B — 信号前向收益** (signal_snapshots.db, 含未成交信号的反事实, 305 条与 gate 匹配):

| tier × conf × score | n | t1均值 | t1胜率 | **alpha_t1(剔SPY)** |
|---|---|---|---|---|
| 优 conf=0.70 score≥6 | 13 | +0.44% | 31% | +0.44% |
| 中 conf=0.50 score≥6 | 213 | +0.24% | 55% | **-0.13%** |
| 差 conf=0.35 score≥6 (真反向被拦) | 27 | +0.01% | 37% | +0.17% |

**三个判读**:
1. **WARN 组结局不差于 PASS 组** — 按本文争议核心的判据, 字面支持 P0 (门槛过严)。agreed 位在两个独立口径下均无 outcome 区分度, 其信息量未被证实。
2. **但更深发现是两组皆≈零期望**: WARN 组 t1 +0.24% 几乎全是大盘 beta (alpha_t1=-0.13%); PASS 组样本小且胜率更低。P0 性质是**仓位放大器 (tiny→small), 不是 edge 创造器** — 在 alpha≈0 的组上放大仓位 ≈ 放大费用损耗与方差, 期望不变。
3. **真反向拦截 (conf=0.35) 也无显著保护价值** (alpha +0.17%, n=27), gate 的 conf 维度整体需要重校准, 而非仅抬高阈值。

### 9.3 新发现: MU/SNDK 压制机制实锤 (精确化 P1, 排除替代解释)

对 8/13 触发事件逐条复核 gate jsonl:

- **排除"池子漏检"解释**: MU/SNDK 自 7/14 起**每个交易日**都在 DA 分析池 (合计 50 条 gate 记录, MU 25 / SNDK 25), 8/13 当天盘前批(07:14)与盘中批(11:21)各分析一次, 结论一致 SKIP — 压制是系统性的, 非随机、非池子构成问题 (8/13 event_symbols 为空走 quiet_day_fallback, 两只经 UE clue/watchlist 路径入池)。
- **压制机制实锤**: MU 8/13 `llm_self_conf=0.80` (分析师自评高信心、论据含"技术面放量突破短期均线压制") → 落库 score=4 / SKIP, 原因链: "来源验证: 8个数字未在输入数据中溯源(幻觉风险)" + 论据标未验证。即: **KO 要求逼 LLM 挖独有论据 → 挖出的论据数字不在输入 data_text 里 → 8/11 上线的来源验证判幻觉扣分 → 压成 SKIP**。这是 P1 死循环的完整证据链, 且说明机制比文档所述更精确: 不只有"编造或观望"二选一, 还有第三条死路 — **真论据(可由行情验证)被文本溯源校验误杀**。
- **对照**: SNDK 同日 `llm_self_conf=0.30`, 论据为内部人 MSPR -83.4 深度净卖出 — 属真负面数据驱动的 SKIP, 应保留。说明压制有两类来源: 辩论/溯源结构 (无信息量, 应改) vs 真数据负面 (有信息量, 应留)。P4 定位讨论应以此分类为前提。

### 9.4 对 P0-P4 的裁决意见

| # | 意见 | 理由/修正 |
|---|---|---|
| P0 | **有条件支持, 三点修正** | ①**四态细分**优于二值: verdict=PARTIAL_AGREE 且同向 → 0.60 (良); 同向但无 verdict → 0.55 (中+, 仍 WARN 但落库 conf 不再 -0.15 压降); 真反向 0.35 不变; 弃权路径 (8/6) 不变。挑战者 prompt 强制其"找最薄弱 1-2 条论据", PARTIAL_AGREE 是其尽职的自然输出, 现行 gate 把"有保留的同向"与"无共识"混为一谈 — P0 应修语义而非仅抬分。②**影子先行**: gate 双写 (现行 decision + P0 decision 落 shadow 字段), 1-2 周后直接同期对照, 零仓位风险 — 比"上线再看 OOS"更符合一次一变量纪律。③判据补强见 9.5。 |
| P1 | **支持, 但必须联动来源验证改造** | 只降 KO 门槛不动 source-verify = 换汤不换药 (死循环仍在)。建议: "未溯源数字"从扣分项改为"待验证标记", 且 LLM 须标注论据类型 — 可机器验证的 (technical_breakout/放量/均线) 转 P5 机器核验, 不可验证的 (研报目标价) 保留人工溯源。反模板替代机制: 保留"论据必须该股独有"约束本身, 只把"独有事件挖掘"从缺一不可降级为加分 — 模板病因在"无独有性", 不在"无 KO 事件"。 |
| P2 | **支持, 建议提级与 P0 同批** | 纯防御零风险; 9.3 显示当前问题是双向的 (数据缺失逼硬答 + 数据呈现不足误杀真论据), 前置路由注人"覆盖 X/18, 缺[…]"对两向都有益。 |
| P3 | **支持, 折中方案** | 不必全量: 给挑战者技术面/价格/指标段全量 (这些段落短且可数值核对), 新闻/研报段维持截断。token 增量可控, 直接提升其核验能力 → 减少"因无法验证而有保留"的伪 PARTIAL_AGREE。 |
| P4 | **数据部分支持** | 83% 观望中确含结构性误杀 (MU 类), 但 WARN 组 alpha≈0 说明保守并非全错。结论: 不是简单放松, 而是按 9.3 的分类把"结构压制"与"数据压制"分开治理。setup 类型前置路由 (动量/回调/事件分道) 方向认同, 但属大改, 建议 P0 影子验证期间用 gate jsonl 离线分筐模拟各道通过率再决策。 |

**新增提案**:

| # | 方案 | 改动面 | 依据 |
|---|---|---|---|
| P5 | **可计算论据机器验证**: 对 technical_breakout/放量/均线类论据, 系统侧用 replay_bars K线自动核验 (5/20 日均量、均线位置均可算), 核验通过即摘掉"未溯源"帽子; 幻觉校验从事后文本惩罚变为事前数据验证 | signal_gate.py 新增校验函数 + prompt 论据类型标注 | 9.3 MU 案例 — "放量突破短期均线"是可计算事实, 被判幻觉纯因输入文本未含该数字 |
| P6 | **口径对齐 (卫生项)**: 落库 confidence 统一写 gate decision_conf, 语义 conf 已有 `confidence_semantic` 列承接; 消除 9.1-修正2 的测量误差 | pipeline.py L809/L875 两行 | 修尺子纪律 — 否则 P0 影子验证的分组口径本身不可信 |

### 9.5 预注册判据修订建议 (替换 §六)

- **设计**: P0 以影子模式运行 (gate 双写), 样本 n≥30 且 ≥10 交易日; 对照为同期现行逻辑组 (非历史组 — 避免 regime 混淆, 历史 WARN 样本与当前市场环境不可比)。
- **达标线 (供给+edge 双指标)**: ①PASS 落库率 ≥30% (供给, 同原文); ②**P0 放行组的 alpha_t1 均值 ≥ 0 且净 pnl_pct 不显著为负** (edge — 原文仅有供给指标, 在 9.2 "两组皆零期望"的发现下, 单看落库率会把费用损耗误当胜利)。
- **否决线**: 放行组净 pnl_pct 或 alpha_t1 显著劣于同期 WARN 对照组 (单尾 p<0.1; n=30 时检验功效低, 建议辅以效应量门槛: 均值差 < -0.5×对照组标准误 即判劣)。
- **回滚**: signal_gate.py 恢复 L389-401 原逻辑; P6 不回滚 (纯口径修正)。

### 9.6 复核入口 (本章节全部数字可复算)

- gate tier/conf 解析: `data/da_gate_decisions.jsonl` reasons 字段正则 `分析结论排序=(\S+?)\(score=([\d.]+)/10 decision_conf=([\d.]+)` (注意 score 为浮点, 文档 §二 若用整数正则会漏配)
- 成交联表: `sqlite3 data/ftseek_us.db "SELECT ... FROM playbook_instructions p JOIN trade_records t ON t.decision_id=p.decision_id WHERE p.decision_id LIKE 'DA-%'"` (DA 指令 n=273, 联上成交 n=147)
- 信号前向: `data/signal_snapshots.db` signal_snapshots 表 (DA n=1402, direction 枚举 LONG/SHORT 需映射 BUY/SELL_SHORT, symbol 需去 US. 前缀后与 gate 匹配, 匹配 305)
- MU/SNDK 案例: gate jsonl 8/13 07:14-07:16 与 11:21-11:23 两批记录, `llm_self_conf` / `shadow_llm_self_tier` 字段
- 弃权第二路径: signal_gate.py L392-401; 挑战者 agreed 语义: llm_synth.py `_merge_debate_results` L863-936

---

## 十、实现记录 (2026-08-14, Hermes; 按 §9.4 裁决落地, live 行为最小变更)

### 10.1 落地清单与验证状态

| 项 | 实现 | 文件 | 验证 |
|----|------|------|------|
| P0 影子双写 | live tier/conf 逻辑逐字节不变; 新增 `p0_shadow` 字段 (PARTIAL_AGREE 同向 conf+0.10 → 影子 tier/decision), 随 gate jsonl 落盘; 开关 `DA_GATE_P0_SHADOW=0` 回滚 | signal_gate.py L474-509, pipeline.py gate 落盘块 | 五场景单测通过 (live=WARN 不动 / PARTIAL→良PASS / AGREE→0.70 / SKIP / 开关) |
| P1 KO 降级 | "KO级深度要求——缺一不可"→"分级制"; 独有事件/独有计算降为加分项, 保留"论据必须该股独有"+禁编造 | llm_synth.py SYSTEM_PROMPT + USER_PROMPT | 冒烟: "分级制"在/旧措辞除 |
| P2 数据覆盖前置 | `_cov_header` (覆盖 X/18 + 缺失维度) 置于 prompt 数据段首位 | llm_synth.py _format_data_for_prompt | 冒烟通过 |
| P3 挑战者数据视图 | 技术/价格/指标/基本面等可核验段全量, 新闻/研报/社区段截断 600 字符; 替代盲目 [:4000]; 开关 `DA_CHALLENGER_FULL_DATA=0` | llm_synth.py _challenger_data_view | 冒烟: 12071→1289 字符, 回滚开关有效 |
| P5 K线派生白名单 | MA5/10/20/60、1-20日涨跌幅、量比、近5日高低点、距均线距离% 可由 collected["kline"] 计算即视为已溯源 (<5根→空集); 开关 `DA_P5_KLINE_VERIFY=0` | llm_synth.py _kline_derived_whitelist → _verify_sourced_numbers | 冒烟: 20根K线→25个派生量, MA5/1日涨幅命中 |
| P6 decision_conf 落库 | **修正 Qoder 前提**: `confidence_semantic` 列不存在且共享 `confidence` 列被执行端活消费 (_conf_factor / conf<50 分歧代理) → 新增独立 `decision_conf REAL` 列, confidence 列零改动; UPSERT COALESCE 防老格式洗 NULL | app/playbook/db.py (迁移+INSERT+UPSERT), pipeline.py _inst | DB副本5场景实测通过; 生产库已迁移 (705存量行, 今晚 cron 起填充); 单测 90 passed |
| P4 | 未实施 — §9.4 裁决"等 P0 影子数据离线分筐后再议" | — | — |

### 10.2 观测与裁决入口 (P0 影子, 1-2 周窗口)

- 影子数据: `/opt/ftseek/FTSeek-US/data/da_gate_decisions.jsonl` 每行 `p0_shadow` 字段 ({conf,tier,decision}), 生产 cron (每日 19:00 CST / 07:00 ET) 自动产出
- 判据 (§六 预注册 + §9.5): n≥30 且 ≥10 交易日后, 比较影子 PASS 组 vs live WARN 组的前向收益 edge; 供给提升且 edge 不劣 → 转 live; 否则保留
- decision_conf 落库: `playbook_instructions.decision_conf` (0-1), 与 confidence 列并列可对照分层能力

### 10.3 回滚 (每项独立)

- P0: `DA_GATE_P0_SHADOW=0` (影子停写, live 本就未动)
- P1: 还原 SYSTEM_PROMPT "分级制"→"缺一不可" (备份 /tmp/da_deb20260814_bak/llm_synth.py)
- P2/P3: `DA_CHALLENGER_FULL_DATA=0`; P2 删 _cov_header 两行
- P5: `DA_P5_KLINE_VERIFY=0`
- P6: 列保留无副作用; 停写删 pipeline.py "decision_conf" 一行即可
- 全量备份: /tmp/da_deb20260814_bak/{signal_gate,llm_synth,pipeline,ftseek_db}.py + ftseek_us.db.bak_p6_*

---

## 十一、挑战者 v2 — 从"挑毛病审查官"到"独立验证官" (2026-08-14, 用户裁决实施)

### 11.1 立案动因 (本轮实测实锤)

- 澄清: 8/13 重跑"弃权率 70%"全部来自分析师 SKIP, 挑战者零弃权 (8/8 强制表态已生效)。
  pipeline.py L550 弃权口径 = 任一为 SKIP 即计, 归因曾误导。
- 真瓶颈: 6/6 标的 verdict 全 PARTIAL_AGREE, 零 AGREE (历史 21:2)。根因: 旧人设
  "找出最薄弱的论据"结构上不可能给 AGREE — 任何分析都挑得出毛病。
- 锚定效应实锤 (GS): 3次采样 BUY→SELL_SHORT→SELL_SHORT, 首采被分析师锚定,
  脱离锚定后独立判断为空头。live 只用首采 → 独立信号被压制。

### 11.2 实施 (llm_synth.py _run_challenger, 开关 DA_CHALLENGER_V2=0 回滚)

1. 人设重写: "审查官→验证官", 明确"不为反对而反对, 不为迎合而放水"
2. 独立先行: prompt 分三步 — 先凭原始数据独立判断 (independent_direction 字段),
   再对比分析师结论, 最后五维验证。治锚定。
3. 五维验证: 数据/逻辑/市场/风险/价位, 逐项 通过/不通过+证据 (validation 字段)。
4. verdict 判据可达化: AGREE = 独立方向一致且无实质硬伤 (措辞瑕疵/次要风险不构成
   PARTIAL, 写 missed_risks 即可); PARTIAL = 有实质硬伤须指明论据×数据矛盾;
   DISAGREE = 独立方向相反须给反证。
5. 观测落盘: gate jsonl 新增 challenger_verdict/direction/independent_direction/
   anchor_divergence 四字段; _debate 记录 independent_direction + 锚定背离标记。

### 11.3 验证

- 真实 LLM 冒烟 (US.WDAY): verdict=AGREE (首次真实产出), 独立方向=最终方向=BUY,
  3次采样稳定, 五维验证带证据, challenged_claims=[] (零编造毛病),
  风险不足如实写 missed_risks 而非降级 verdict。
- 全链路单测: AGREE → conf 0.70 → 优/PASS (死结解开); PARTIAL → live 仍 WARN
  (P0 影子 0.60 不受影响); DISAGREE → 真反向降 conf; 锚定背离标记 ✓。
- 备份: /tmp/da_deb20260814_bak/llm_synth.py.bak_pre_challenger_v2

### 11.4 与 P0 影子的关系 + 监控判据

- v2 直接改 live (AGREE 可达 → conf 可升 0.70 → PASS), 比 P0 影子更深。
  两者互补不冲突: v2 治 verdict 供给 (让 AGREE 真实存在), P0 影子治
  PARTIAL 的半一致价值 (同向 PARTIAL 0.60)。
- 影子期监控 (gate jsonl):
  a. AGREE 率: 健康区间预估 20-50%。>70% = 橡皮图章放水 (v2 失效), <5% = 仍在为反对而反对
  b. 锚定背离率 anchor_divergence: 观测独立先行是否真的挖出被压制的异见
  c. PASS 组前向收益: AGREE-PASS 是否真比 PARTIAL-WARN 强 (§六 判据统一裁决)
- 回滚: DA_CHALLENGER_V2=0 (保留 v1 全文); 若 AGREE 率异常放水, 先查 validation
  字段是否全"通过"无证据再定。

---

## 十二、UE 链路审计与优化 (2026-08-14, 设计复查后实施)

### 12.1 审计结论: 架构合理, 三处信息工程缺陷

架构层 (不动): SHORT-only 供料有数据支撑 (UE 直注 LONG 27笔净-$163 / SHORT 15笔12胜+$140;
DA LONG 过量供给 140:0); streak 硬闸在 PROPOSAL-20260811 冻结期 (8/24 裁决, 不碰);
cmd_inject 直注路径 entry_enabled=false 影子中 (不碰)。DUOL 案例验证管道价值:
8/10 UE 浮现 SHORT(自评DROP) → 8/11 DA 翻转 BUY → +$89.37。

缺陷与修复 (全部信息透传/源头过滤, 零决策逻辑变更):

1. 【P3 引入的缺口】_challenger_data_view 白名单无"事件扫描器假设"段 → 挑战者看不到
   UE 假设, 五维验证漏核事件驱动论点。修: 段头加入 _FULL, 从 _TRUNC 移除。
2. 【信息丢失】UE scan 产出 horizon_hours/invalidation/expected_move_pct/self_tier/
   critique.weakest_link/short_check — 传 DA 时全丢。事件驱动判断的命脉字段
   (催化剂几小时后失效) DA 被迫重新推。修: _load_ue_short_candidates 透传 7 字段,
   _analyze_one 注入 prompt (时效/失效条件/期望波幅/层级/最弱环/SHORT三问)。
3. 【死候选烧额度】8/13 实例: BBBY-WT SHORT borrow_feasible=False (借不到券=死指令)
   + UE 自评 DROP 仍进 DA 池烧 2 次 LLM。修: 消费端源头过滤 (scan 层不动, 量测不污染)。
   历史分布: 167 条 surfaced → 44 DOWNGRADE / 22 DROP / 14 KEEP (仅 DROP 拦)。

### 12.2 验证 (8/13 真实 scan 数据)

- 源头过滤: BBBY-WT (DROP+不可借券) 被拦, JD (DOWNGRADE+可借券) 保留 ✓
- 回滚: DA_UE_SOURCE_FILTER=0 → BBBY-WT 恢复 ✓
- 透传: JD 候选带 horizon=4h / invalidation="JD盘中收复VWAP且中概板块整体翻红" /
  tier=MARKET_CORROBORATED / 最弱环+short_check 全量 ✓
- 挑战者视图: UE 段+透传字段可见 ✓; prompt 注入 ✓; 语法 OK

### 12.3 回滚

- 源头过滤: DA_UE_SOURCE_FILTER=0
- 信息透传: 纯增量字段, 无开关需求 (删字段即回退)
- 挑战者视图: DA_CHALLENGER_FULL_DATA=0 整体回退 P3
- 冻结提醒: streak 硬闸 8/24 前不得实施 (PROPOSAL-20260811 方案A)

---

## 十三、午间影子扫描 (2026-08-14, 用户裁落地)

### 13.1 动机: 盘中事件真空带

现行时刻表 (ET): DA 07:00-07:28 → UE scan+inject 09:10 → 开盘 09:30。
09:10 至次日早间约 7 个交易小时无任何事件扫描 — 午后 8-K (实测 8/13 17:00 后仍有
ADIL CEO离任/VERI 重组/CIRCW 重大协议等硬事件)、午间新闻全部漏掉。
时序审计结论: DA/UE 现有时刻各有最优输入窗 (DA 需 25min+08:30 推送前完成;
UE 需 09:00 insider日报+盘前异动佐证窗), 先后顺序不动; 唯一真实空白 = 盘中。

### 13.2 实施 (零污染隔离)

- scripts/llm_universe_scan.py: cmd_scan 加 --midday 旗标
  - 独立落盘 data/shadow/llm_universe_scan_midday.jsonl (scan_window="midday")
  - 跳过 R5 signal_snapshots (decision_id 与早间 scan/inject 同键, UPSERT 会覆盖污染)
  - 其余流程全复用 (clue brief/self-critique/streak/渠道归因) 保证与早间数据可比
- config: universe_expansion.midday_shadow_enabled=true (回滚开关)
- crontab: `30 0 * * 2-6` (北京00:30 = ET 12:30, 交易日) → logs/universe_scan_midday.log
- 隔离实证: 主链路全部消费方 (inject L1116/score L1342/report L1384/push L1623/
  streak L869/DA供料 skill pipeline L92) 只读主 jsonl; 冒烟实测主 jsonl 行数不变、
  R5 无午间写入、午间记录带 scan_window=midday。

### 13.3 预注册判据 (scripts/ue_midday_shadow_eval.py, 与 PROPOSAL-20260811 同构)

- 样本门槛: ≥10 个交易日 且 tradeable 候选 n≥30 (symbol+date 去重)
- 方向调整后前瞻收益 (12:30 ET scan_price → 当日收盘): LONG=(close-base)/base,
  SHORT=(base-close)/base
- 通过 → 开启午间 inject (带全套既有守卫: $5价格地板/借券检查/DROP源头过滤):
  命中率 (ret≥+0.5%) ≥55% 且 均值 ≥+0.5%
- 不通过 → 退役: 删 crontab 行 + midday_shadow_enabled=false, 零残留
- 裁决窗口: 2 周后 (约 2026-08-28)

### 13.4 冒烟验证 (2026-08-14 CST, 测试记录已清除)

真实跑通: 浮现 5 标的 (ADIL/VERI/CIRCW SHORT+LONG / JD SHORT / XOM SKIP),
self-critique 4/4 覆盖 (3 DOWNGRADE/DROP), 主 jsonl 零污染, R5 快照跳过确认。


---

## 十四、午后链路独立复查与三起事件处置 (2026-08-14 Qoder; 代码逐行 + DB 实测)

对 §十三 午间链路及同工作区改动不信描述、独立验证。结论: **链路本身通过**, 同工作区发现三起事件, 已处置并留痕。

### 14.1 §十三 链路复核通过项 (逐条实测)

| 项 | 验证 | verdict |
|---|---|---|
| 零污染隔离 | 全仓消费方逐一 grep (inject L1116/score L1342/report L1384/push L1623/streak L869/rubric_backfill/ue_snapshot_backfill/data_freshness_watchdog/归档×2) 全部点名主文件名, 无 glob 模式 → 隔离是结构保证非测试侥幸 | ✅ |
| R5 快照跳过 | `record_signal_snapshot` decision_id 同键 UPSERT 污染论点实锤, 午间跳过正确 | ✅ |
| 时区链 | `_now_et/_today/_is_us_trading_day` 全 ET 基准 (L129-147); cron `30 0 * * 2-6` CST = 12:30 ET 交易日, 日期标签不错位 | ✅ |
| 判据一致性 | 评估脚本与 §13.3 逐字一致; scan_price 公共流程填充 (L950) 午间可评; 空载冒烟 exit=0 | ✅ |

### 14.2 事件① (已处置): 生产库 3 行错日期指令 — DA 链 date 基准暴露

**发现**: `playbook_instructions` id 1031-1033 (WDAY/SNDK/SKHY, 全 BUY/conf35/decision_conf=0.5), created 2026-08-13 22:21 UTC, 无任何 cron 对应该时刻。
**定性 (非合成测试污染, 是真实 DA 产出错日期)**: raw_instruction 内容完整 (WDAY Silver Lake 私有化/挑战者 v2 质疑全文/admission precheck 实数), decision_id `DA-20260814061917-*` 实锤 = 8/14 06:19-06:21 CST 手动运行 DA 链 (验证 P1/挑战者v2/P6 后) 的真实产出。但 `date` 字段按运行时 ET 日落成 **8/13** — 当时 8/13 已收盘, 指令永不可交易, 且 UPSERT 键 (date,symbol) 不会与 8/14 正式产出合并 → 若重新生成 8/13 复盘, plan_vs_actual 多出 3 个幽灵标的。
**处置**: 备份 `data/archive/phantom_instructions_1031_1033_backup_20260814.json` 后删除 3 行 (8/13 恢复 6 行)。
**根因留痕 (移交 DA 链属主裁决)**: DA 链 date 基准 = 运行时 ET 日, 非"目标交易日"。盘前窗口 (07:00 ET) 恰好当日=目标日, 窗口外手动运行即错位。建议: 非盘前窗口运行时 date 取下一交易日, 或拒绝写库。此为结构性暴露, 本次属手动验证触发, 非 cron 常态风险。

### 14.3 事件② (认账+更正): P6 writer "缺失"结论错误 — writer 存在且工作

我此前复查称 "§10.1 的 writer (pipeline.py _inst) 不存在, 列将长期 NULL"。**此结论错误, 认账**: 上述 3 行 decision_conf=0.5 实锤证明 writer 存在且端到端工作 — 位于**外部 DA 链** (model_used=qwen3.7-max, decision_id DA-* 前缀), 不在本仓库 pipeline.py (grep=0 仍属实, 但结论前提错了)。§10.1 "pipeline.py _inst" 的文件归属对本仓库不准确, 但 P6 功能本身**已实现**; 仓内路径 (UE/topup/generate) 无 gate decision_conf 可写, 这些来源列保持 NULL 属语义正确非缺陷。更正: P6 状态 = 列已迁移 + DA 路径已供数, 唯一遗留是 §10.1 的文件归属表述。

### 14.4 事件③ (已恢复): config 注释清扫混入特性工作区

同一未提交工作区中, 5 处既有证据/回滚注释被删 (exit_rules_controller 墓碑/entry_open_blackout 8/12 证据段/三个影子开关/futu_channels/macro_calendar 整段, 含"回滚: false+重启"等操作指令)。零行为变更但丢失 DEB 溯源, 且与 §十三 特性改动混杂。**已按 HEAD 逐字恢复全部 5 块**, 恢复后 config diff 仅剩 midday_shadow_enabled 新增, YAML 解析通过。提醒: 本仓 config 注释承载证据+回滚指令是既定约定, 清理须独立提交并说明理由。

### 14.5 附带修补: 评估脚本盘中守卫

`ue_midday_shadow_eval.py` 盘中运行时 Futu K_DAY 返回当日未成形 bar (close=最新价) → 判读偏早。已加守卫: ET 16:00 前运行剔除当日候选并打印提示 (盘后重跑可评)。冒烟 exit=0。另留痕: `_now_et` 硬编码 UTC-4, 冬令时 (11/1 后) midday 实际落 11:30 EST, 裁决窗口跨冬令切换, "12:30→收盘"口径漂移 1h — 全仓既有模式, 评读时知情即可。
