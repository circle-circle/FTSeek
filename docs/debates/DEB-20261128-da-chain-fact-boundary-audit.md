# DEB-20261128 · DA 链路事实边界审计（Batch A · record-only · 零行为变更）

- **状态**：已落地（record-only）。用户裁「同意，港股数据现阶段更适合候选发现与背景输入，按照建议执行」。
- **范围**：外部审计指控「把设计目标／已实现／已激活／有交易价值混述」，本批逐项对照**正式源码**核实并落档，**不改任何规则、不激活任何开关、不写生产代码**。
- **凭据**：探针 `/tmp/rfc218_s29_{truth_table,view_manifest,funnel_audit}.py`；产物 `data/shadow/da_chain_audit/2026-11-28/{truth_table,view_manifest,funnel_audit}.json`；契约 `tests/test_da_chain_truth_table.py`（41 绿，`/tmp/rfc218_s29_truthtests.log`）。

## 1. 链路表述修正（A1）

| 旧表述（过满） | 修正后（与源码一致） |
|---|---|
| 「共享同一真源，杜绝两人事实不同」 | 同一 `data_text` 变量；挑战者看 `_challenger_data_view` 的**过滤/截断子集**（`_TRUNC`=新闻/研报/社区截断 600，未识别段丢弃，回退 `[:4000]`）。§28 ON 只补「数据源状态」一段。一致性合同应定义为：关键事件 ID/版本/价格/时点/单位/缺失状态一致＋**显式列出挑战者未见的材料** |
| 「一致 +1／反向 −1／分差>3→SKIP」 | 三层分离：① score_band→{HIGH:8,MEDIUM:6,LOW:3}；② merge 中文五档 confidence ±1 档（仅独立模型 AGREE 升，self_debate 不升，同向不覆写 score）；③ gate `decision_conf`（0.50±0.20/−0.15，弃权 max(0.50,score/10)）→ tier；④ adapter conf（另有同向分差衰减、弃权保持 0.50）。「+1」是**中文档位**，非胜率/非最终 confidence |
| 「缺失≥6→SKIP；$0 只能 SKIP（靠 prompt）」 | 充分性标记由代码算注入 prompt；LOW→score3→tier 差→BLOCK（代码兜底）。**$0/无效价已有代码硬拒**：`candidate_eligibility.positive_number`（isfinite&&>0）＋min_price 5.0＋scanner 质量闸 price>$5＋admission entry_price≤0。缺失计数仍粗（19 维等权），§26/§27 已改输入语义 |
| 「全市场扫描，risk_off 数量×0.3」 | **0.3 是仓位乘数**（instruction_builder:89 / planned_executability:81 / risk_gates:236），**不是候选数量**。DA 候选预算=主池 top_n+2(=7)＋extras＋UE≤cap＋异动≤2（pipeline:1284），无 regime 乘数。「全市场」实为多渠道发现→质量闸(>$2B/>$5/非杠杆)→四信号评分 |

## 2. 逐变量真值表（A3 · 567 行 · 真实函数）

被测：`_apply_score_band`/`_merge_debate_results`/`evaluate_signal`/`_compute_confidence`。I/O 加载器中性化（只喂 pattern/quality_score，不喂 decision_conf/tier），`_regime` 注入避免盘中网络。

**钉死结论**（`tests/test_da_chain_truth_table.py` 41 绿）：
- MEDIUM tier ∈ {良,中,差}，**恒不「优」**（score 6<7）；LOW 恒「差」/BLOCK。
- HIGH+独立 AGREE → 优/0.70；**HIGH+PARTIAL_AGREE 或明确反对 → 中/0.50**（HIGH 不一定优）。
- 同向 merge **永不改 SKIP**、**不覆写 score**（丙1）；`|分差|>3→SKIP` **仅真反向分支**（36/162 真反向行 SKIP，全部 |diff|>3；非 SKIP 全部 |diff|≤3）。
- 一方 SKIP=合法弃权，保持有观点方向，不罚。

### 2.1 新发现：独立性判据分歧（钉死待裁，本批不修）

- **merge** 用 `_self_debate` 判独立；**gate/adapter** 用 `_fallback`（2026-09-18 三级链后未同步）。
- 后果：T2/T3 换家族独立挑战者（`fallback_independent`）AGREE 同向 → merge 中文 confidence 升「中高」，但 gate/adapter **不 +0.20** → HIGH 落「中」(WARN/tiny) 而非「优」(PASS/normal)。
- 合成量化：567 行中 **18 行** merge 升档而 gate 平（decision_conf 0.50），**全部 fallback_independent**。
- 历史发生数：现有 `da_gate_decisions.jsonl` 仅 n=10 且**缺 challenger_tier 字段**，无法定量 → 需 bridge/raw_instruction 台账 join，列后续。
- **处置**：本批只钉死现状（契约测试）。修复属行为变更，须先影子量化拦截分布＋用户单独裁决，不抬分不降门。

## 3. 挑战者视图差异清单（A2 · 744 真实 checkpoint）

- OFF 段落统计：full=13933 / trunc=1448 / dropped=87。
- 常截断段＝最新新闻、社区讨论（_TRUNC）；「数据源状态」段出现 103 次，OFF dropped/full（子串碰撞）→ ON 全 full。
- 无「恒整段丢弃」的常规段（dropped 主要为 OFF 态数据源状态）。
- 产物逐段标 full/trunc/dropped＋字节数，供「挑战者未见材料」显式清单。

## 4. 候选漏斗覆盖率审计（A4 · 72 批 / 4025 候选）

- by_status：UNKNOWN 2041 / REJECT 1972 / PASS 12。
- top_reasons：low_atr 1583 / leveraged_etf 316 / stale_quote 57 / **price_floor 40** / unsupported_security_shape 2。
- top_missing：source_asof 3620（扫描快照非实时）/ price 734 / bid_ask 41 / shortable 11。
- 结论：拒绝有显式 accounting（low_atr 主导），price_floor 代码硬拒确实触发；「全市场」覆盖率须以漏斗计数表述，而非字面全量。

## 5. 港股联动定位（用户已裁）

- 现阶段＝**候选发现/背景输入**，非方向信号。美股腿代码自标 `EXCHANGE_LISTED_DEFERRED`/`OTC_UNAVAILABLE`（未采集）。
- 时间领先≠未消化（美股盘前 16:00/17:00 CST＋期货更早）；同公司 ADR 与跨公司行业映射分开；3/3 非独立证据。
- 采集器三处硬化（有限值/成分时点状态/字段语义）合成证实 → 见 B1 批（DEB-20261128-hk-collector-finite-guard）。

## 6. 边界 / 未验证 / 回滚

- **零行为变更**：未改生产代码、未激活开关、未触 DB（只读）、零订单。
- 未验证：独立性分歧的历史发生数（台账缺字段）；真值表为合成枚举，非收益证据；A2/A4 为描述性审计。
- 回滚：本批纯文档/影子产物/测试，删除即回滚，无生产影响。
- 后续单变量：B1 有限值入口（HK 采集器）；A3 分歧修复、B2/B3、C 美股腿、D 研究预注册、E DA 注入各自独立批，激活须 §10.4 A/B＋用户裁决。
