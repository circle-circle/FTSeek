# REVIEW-20260905 · DA/UE 链路 prompt 多模型评审（第一会话：独立分析）

> 起因（用户令）："根据最近一个月的交易结果和优缺点，要从源头来优化提升目标股和指令能力，
> 分析 DA 和 uE 两方面的链路逻辑，从结果和过程反推检查各自 prompt 是否有值得提升优化调整的点，
> 从结果、性能以及最新的 PE 最佳实践，减少冗余，充分发挥模型能力，多模型会话模式充分分析"。
> 日期：2026-09-05。方法：三模型并行独立分析（防同源盖章），第二会话交叉质询后出最终方案。
> **本文档 = 第一会话记录。全程只读分析，未改任何代码/prompt/配置。**

## 〇、三模型分工与输入

| 代理 | 模型 | 任务 |
|---|---|---|
| qwen-architect | Qwen3.8-Max | DA 链路 prompt 架构评审（结构性偏向/冗余审计/校准注入/职责分工/挑战者设计/说服力 vs 预测力） |
| kimi-reviewer | Kimi-K3 | UE 链路独立审查 + 对主分析方链路声明的反幻觉核查（逐条 file:line） |
| glm-verifier | GLM-5.3 | 实证复算：`data/ftseek_us.db` 只读 SQL，三链路盈亏/conf 分布/tier×结果/模型时代切片/时间窗 |

共享输入：`docs/DA-LLM-PROMPTS-VERBATIM.md`（DA prompt 逐字）、`docs/STRATEGY-BRIEF-FOR-EXTERNAL-REVIEW.md`、
`docs/reviews/REVIEW-20260904-monthly-trading-review.md`、`scripts/llm_universe_scan.py`、`config/unified_config.yaml`。

---

## 一、实证基线（glm-verifier，全部数字实跑 SQL，窗口 8/04–9/05 exit_time 归集，247 笔净 +$182.25）

### Q1 三链路总账（join playbook_instructions via decision_id，命中率 245/247）

| 链路 | n | 净额 | 胜率 | 均赢/均亏 | mfe<0.05% 占比 |
|---|---|---|---|---|---|
| DA | 191 | **+479.13** | 63.4% | +36.76 / −56.71 | 10.5% |
| UE | 30 | **−293.57** | 50.0% | +34.58 / −54.15 | 23.3% |
| REENTRY | 19 | −75.18 | 47.4% | +13.52 / −19.69 | 21.1% |
| TOPUP 池 | 5 | −106.05 | 40.0% | — | 20.0% |

截断到复盘原窗（<9/04）后 REENTRY 17 笔 −111.40 与月报**精确一致**；UE 28 笔 −395.09 ≈ 月报 −397.25。
**月报结论成立**。9/04 当天 UE/REENTRY 合计 +137.74（chase_cap enforce 上线次日，仅时间相邻，无对照不证因果）。

### Q2 UE confidence：cap=50 实锤 + raw_confidence 区分度

- 112 条 UE 指令 confidence **全部=50**（恒等确认）。机制：min_conf=0.5 闸使能进 inject 的 raw 必≥50，cap 后全等——cap 与闸合谋抹掉区分度。
- cap 前 origin.raw_confidence（50–92 连续）× 成交：≤60 桶 9 笔 −118.91 / 61–75 桶 14 笔 **−248.74（最差）** / >75 桶 7 笔 **+74.08（唯一正桶，胜率 71.4%）**。
- 判定：非单调，但支撑"cap 丢掉头部信息"；不支撑"conf 线性有用"（n 小）。

### Q3 UE self_tier × 成交（反预期：出血桶不是 THESIS_ONLY）

| self_tier | 成交 n | 净额 | 均笔 | 胜率 |
|---|---|---|---|---|
| HARD_EVENT | 17 | **+101.54** | +5.97 | 52.9% |
| MARKET_CORROBORATED | 12 | **−379.10** | **−31.59** | 50.0% |
| THESIS_ONLY | 1 | −16.01 | — | 0% |

**UE 的真出血桶是 MARKET_CORROBORATED**（"无具体事件、仅市场异动佐证"的浮现），HARD_EVENT 整体为正。

### Q4 UE 模型时代切片（小样本，仅方向参考）

| model_used | 时代 | 指令数 | 成交 | 净额 | 均笔 | 胜率 |
|---|---|---|---|---|---|---|
| qwen3.7-max | 8/04–8/26 | 61 | 13 | −273.26 | −21.02 | 38.5% |
| qwen3.8-flash | 8/27–8/28 | 6 | 1 | +61.01 | — | （n=1 不可评） |
| glm-5.2 | 8/31–9/04 | 40 | 11 | −58.36 | −5.31 | 63.6% |

降档后肉眼变好而非变差；但 glm-5.2 时段与 chase_cap enforce(9/4) 部分重叠，不可归因。

### Q5 DA confidence 分布 × 成交（无单调性）

指令池：0×767（全部 deterministic 兜底）、35×64、50×19、63×15、70×218、72×3、88×2、90×8（另有 60/65 零星）——"六档塌缩"大体属实但不完整。
成交侧：35 档 26 笔 −94.72（WR 26.9%）｜63 档 13 笔 **+150.86（WR 100%）**｜70 档 144 笔 +390.80（主力利润）｜72 档 3 笔 −69.38｜90 档 3 笔 −27.85。
**confidence 对 DA 成交结果无可用排序力。**

### Q6 DA score_band：库内不存在该键（无法复算，如实记录）；替代口径 score（0-10）分桶

6–6.9 桶 72 笔 **+626.75（WR 75%）**｜7–7.9 桶 92 笔 **−565.26（最大出血桶）**｜≥8 桶 8 笔 +121.85｜null 24 笔 +189.74。
**score 无单调性，中间档反向。**

### Q7 开盘 90 分钟（ET 09:30–11:00）入场

DA 189/191=99.0% 在早窗（净 +490.81）；UE 29/30=96.7%；**REENTRY 是唯一大量落在 11:00 后的链路（11/19 笔，−59.82）**——与月报 D6 方向吻合。
（entry_time 混有 −04:00/+08:00 两种时区偏移，已归一，桶归属不变。）

---

## 二、DA 链路评审（qwen-architect）

### 2.1 三个前提修正（改变优化方向）

1. **DA prompt 不是"开盘追强"的生产者。** 生成层实测遵守"回踩优先"：9/1 后 DA 非 SKIP 指令 42 条中 PULLBACK_LIMIT 33 条、FADE_SHORT 4、BREAKOUT **0**；全月 DA BUY 文本含"低吸/回踩/承接"128 条 vs "突破/追"25 条。意图被丢弃在**执行层**：`app/playbook/entry_routing.py:155`（routing_enabled=false → resolve_route 恒 None）+ `pipeline.py:5446-5447`（_vol_ok 恒 True）+ reprice 重锚 → 成交价均高于模型自报 planned_entry **+1.10%**（n=162），9/1 后高于 confirmation.price_level **+1.35%**（n=28）。
2. **但该偏差本窗口无 PnL 代价**：risk_on LONG 中 chase≥+2% 组 49 笔均笔 +$0.75，非 chase 组 83 笔均笔 −$8.27（追价组反而略好）。
3. **"84% 开盘 90 分钟"是基率伪影**：全体入场 93.9% 都在该窗口，mfe≈0 桶占 84% 反而**低于**基率。

**结论：在 prompt 里加强反追涨措辞的预期 PnL 杠杆≈0。** 软约束失效的真因不是模型不听，而是**听的产物没有消费方**（write-only 字段）。唯一有效的约束形式 = **必填数值字段 + 代码消费 + 违约可计数**。

### 2.2 关键论断

- **F（说服力与预测力脱钩，实证且为负）**：score 7 分档 WR 47.8% 均笔 −$6.14 vs 6 分档 WR 75.0% 均笔 +$8.70（中间档反向，两档均 n≥30）。因果链≥3 层/论据独有/多空各≥2 条只提高可读性。
- **D（校准字段退化）**：score_band LOW 全史 0 次产出、HIGH 仅 2.2% → 三档实为二档常量；signal_gate 的两个 LLM 输入（score 映射 8/6/3 + 恒 0.70 的 decision_conf）**同时退化**——gate 的负向 edge 只能来自 strategy_memory/claim IC/regime。
- **E（挑战者）**：全月 debate_outcome AGREE 187 / PARTIAL 21 / 弃权 53 / **DISAGREE 0** → "独立方向"设计失败；但 **PARTIAL 是全部 LLM 输出中唯一有分离度的字段**（n=13，WR 23.1%、mfe≈0 占 23.1%），且未接任何门。处方：删强制表态/my_direction，改产 ≤3 条机器可核销 kill-condition（并入 invalidation_triggers，走 tripwire_checker 同一核销器），PARTIAL 接影子门。**不移除辩论**（会扔掉唯一信号）。时序约束：B11 观察窗 9/1–9/12 内禁改挑战者。
- **C（校准注入）**：均值型注入（"你约 50%"）的实际效应是中档通胀+负向锚不可达。替代：代码算好的**分桶事实表**注入（本 ATR 桶×regime 下 n/WR/均笔，n<30 注入"样本不足勿调档"），与"数据充分性由系统算好"同一原则。

### 2.3 段落级清单

**甲类·零风险纯删减**（行为等价，换 PROMPT_HASH 一次生效）：

| # | 位置（DA-LLM-PROMPTS-VERBATIM.md） | 动作 |
|---|---|---|
| 甲1 | L43 核心原则7 / L162 / L186 首句 | "论据独有+具体数字"三写 → 唯一真源留 L162，删另两处 |
| 甲2 | L78 括号内复述 HIGH 三判据 | 与 L113-116 双写且措辞不一致 → 改指针"判据见输出契约段" |
| 甲3 | L215 | 与 L211 同义，删 |
| 甲4 | L139-140 字符串版 stop_loss/take_profit | 与 L142-143 数字版双字段 → 确认消费方=0 后删字符串版 |
| 甲6 | 变更史/日期戳段（L45-58 等） | **勿动**——DEB-20260901c 蒸馏 V2 影子在跑（−19%，37/37 等价，9/16 裁决），重复改污染对照 |

**乙类·行为变更（影子+预注册+n≥30）**：

| # | 改法 | 判据 | 杠杆 |
|---|---|---|---|
| 乙1 | 删 tripwires；invalidation_triggers 改"系统列出可判定指标、模型只填 operator+value+source_field"（现 ≥2 条依从率仅 19–43%） | 影子 10 交易日违约率≤20%（现 57%） | 中 |
| 乙2 | score_band 保留不动，**新增 scenarios {bull/base/bear 各 p+target，p 和=100}**；prompt 明示叙事段不进任何门禁评分，只供人读 | 落库基数自检（唯一值≥8）；90 天后 p(bull) 五分桶胜率单调 + Brier<0.25，每桶 n≥30 | **高** |
| 乙3 | 挑战者改 kill-condition 制（见 §2.2-E） | B11 窗后影子：PARTIAL/kill 命中切片 WR 差≥15pp，n≥30 | **高** |
| 乙4 | L67-68 散文"回踩优先" → 三必填数值契约（archetype + pullback_limit_price + no_fill_above_pct），缺失=契约违约计数 | 与 ADR-035 影子并轨；prompt 侧违约率≤10% | 语义高/PnL 低 |
| 乙5 | 校准原则改分桶事实表注入（见 §2.2-C） | 注入前后 score 分布熵不降、HIGH 产出率漂移≤5pp | 中 |
| 乙6 | prompt 内 stop_loss 数值段删（代码 ATR 分桶是真源；收紧已被反事实否决） | 需同步 admission SL_STOP_TOO_WIDE → 中影响面，先影子 | 中 |

### 2.4 qwen 留下的待验证假设

H1. AGREE 率两口径矛盾（debate_outcome 90% vs DEB checkpoint 96–100%）→ B11 裁决前必须锁口径。
H2. chase_cap enforce（9/4 live）可能拦掉中性偏正单（chase≥2% 组均笔 +$0.75）→ trigger_miss_telemetry 前向影子 n≥30。
H3. PARTIAL WR 23.1% 是否 n=13 噪声 → 扩窗全史重算 + 按日聚类 + permutation。
H4. score 7-vs-6 反向是否稳定 → 全史重算剥 regime/通道；若稳定则 score_band 不应作 gate 输入（触 B12 冻结声明）。

---

## 三、UE 链路独立审查（kimi-reviewer）

### 3.1 主分析方声明核验：1–5 全部成立；声明 6（clue 输入源清单）**不完整**——漏三源：
负反馈表（clue_feedback，`llm_universe_scan.py:1256-1263`，**prompt 内唯一绩效闭环**）、regime 摘要（L1286-1294）、人工线索（L1105）。

### 3.2 v2.8 prompt 结构问题（独立发现）

1. **字段表漏 invalidation_triggers**：L134-140 字段清单无此键，JSON 模板（L152）与补述（L158-160）有——按 E3 自证规律"示例里没有≈不存在"，最高危不一致。
2. **模板示例自违 null 规则**：模板示例是 LONG 行却带非空 short_check 对象和 skip_reason 占位串（L148/L156-157），而 L160 要求"非 SHORT 填 null；非 SKIP 填 null"——示例在教错误行为。
3. **第 4 条"充分浮现不设数量上限"+ self_tier 零消费于任何闸门 = 结构性放量偏置成立**：inject 筛选链（L1959-1964）只查 conf/持仓/排重/streak；THESIS_ONLY 夹逼后仍可达 0.60>0.5 门槛。

### 3.3 生产填充率实测（kimi 复算 data/shadow/llm_universe_scan.jsonl，46 次 scan/399 标的）

evidence_checklist 100%（155/155）｜invalidation 82%｜short_check 80%（96/120）｜**invalidation_triggers v2.8/glm-5.2 时代仅 36%（56/155）**——远低于受控 bakeoff 的 78%，**受控实验高估依从度**。

### 3.4 论断

- **B（cap=50）= 信息销毁，且销毁目标已不存在**（排序主键 _src_priority 使防抢占动机失效，代码注释 L2084-2086 自认）。现存效果只剩污染 conf 分桶 + UE 源内次键排序打成平局。修法：①落 raw conf；②用代码侧 rubric_score 替代 LLM 自报；③tier 进闸（HARD_EVENT 才 inject）。
- **C（模型降档）**：纯散文条款（第 4/9/10/11 条）对弱模型不可测不可依从 → 删或转"模板字段+代码闸"；"字段写进 JSON 模板即填充"意味着**模板是唯一可靠的契约面**。
- **D（clue 输入端垃圾进）**：盘前榜段（L1031-1045）/峰群段（L985）本质是涨幅榜；"极端带仅记录不入围"靠模型自觉，与 E3 教训（模型读示例不读禁令）直接冲突。更便宜的输入侧修法：极端带行根本不渲染；gainer 侧行数≤loser 侧；clue 准入要求跨源交叉命中（earnings shadow 的 cross_hit 是现成范式）。

---

## 四、交叉验证一致性矩阵

| 论断 | qwen(DA) | kimi(UE) | glm（实跑） | 一致性 |
|---|---|---|---|---|
| score/confidence 无区分度（常量退化） | ✔ 提出 | ✔ cap 合谋机制 | ✔ Q2/Q5/Q6 实跑证实 | **三方一致** |
| write-only 字段是主病灶 | ✔（DA 侧 6 个） | ✔（UE self_tier 零消费） | ✔ Q3 tier 有区分度却没接门 | **三方一致** |
| 说服力≠预测力 | ✔ score 7 档反向 | — | ✔ Q6 独立复现（7–7.9 桶 −565 vs 6–6.9 桶 +627） | **一致** |
| UE 出血桶定位 | — | （prompt 侧未定位） | MARKET_CORROBORATED −379，非 THESIS_ONLY | glm 新发现，kimi 的"tier 进闸"处方恰好对症 |
| 反追涨措辞杠杆 | ≈0（执行层丢弃） | （UE 侧 clue 诱导才是真源头之一） | Q7 基率证实 | 互补 |

分歧/待裁：U4 落 raw conf vs rubric_score（glm Q2 显示 raw 非单调）→ 第二会话定夺。

---

## 五、新增军规建议（qwen 提出，三方背书）

> **prompt 字段消费方登记制**：每要求一个输出字段，必须登记其 live 消费方 + 落库基数自检（唯一值数，防常量退化）；无消费方的字段一律删除。
> 当前 write-only：DA 侧 tripwires、entry_archetype、confirmation、ttl_min、data_quality_note、score 字符串版；UE 侧 self_tier（有区分度却没接门）、evidence_checklist（100% 填充零消费）。

## 六、时序与纪律约束

1. B11 观察窗 9/1–9/12：挑战者改动排到 9/12 裁决后。
2. DEB-20260901c 蒸馏 V2 影子 9/16 裁决：甲类删减与其重叠部分勿重复改。
3. 全部乙类/U3-U5 = 行为变更：影子先行 + 预注册 + n≥30 + 一次一变量 + 用户裁决。

---

## 七、第二会话计划（交叉质询）

1. glm-verifier：复核 qwen/kimi 的承重经验数字（PULLBACK_LIMIT 33/42、成交价 vs planned_entry +1.10%、debate AGREE/PARTIAL/DISAGREE 分布、PARTIAL 切片 WR、score_band LOW 0 次、UE 填充率 36% 等）+ 扩窗全史重算 H3/H4 + UE tier×raw_conf 交叉。
2. qwen-architect：带着 glm 实证 + kimi 的 UE 发现，收敛 DA 最终方案（含乙2 防退化机制设计、乙3 kill-condition 精确契约、乙5 分桶口径、一次一变量下的排序）。
3. kimi-reviewer：带着 glm 实证 + qwen 的 DA 发现，收敛 UE 最终方案（tier 闸精确规则、raw conf vs rubric_score 定夺、clue 渲染侧具体改法、UE prompt v2.9 全文草稿、与月报 D3 shadow-only 的先后序）。
4. 主会话合成最终优化解决方案 → `docs/proposals/PROPOSAL-20260905-da-ue-prompt-optimization.md`。

---

## 八、第二会话结论分流（2026-09-05 完成，勘误指针）

第二会话（交叉质询 + glm 独立复核）已完成，**最终方案见 `docs/proposals/PROPOSAL-20260905-da-ue-prompt-optimization.md`**。
本报告 §一/§三 的 glm、kimi 结论全部经复核成立；**qwen §二 的以下第一会话结论已被其自己第二会话的新实测推翻并经 glm 复核证实，请勿再引用**：

| 第一会话结论 | 复核后真相 |
|---|---|
| §2.2-D "score_band 三档塌缩、无区分度" | **撤回**：band 存在 `da_gate_decisions.jsonl`（glm 此前查 playbook_instructions 未命中），PASS 单内 HIGH+35.70 > MEDIUM·0.9 +9.43 > MEDIUM·0.8 −29.93 **单调**，是唯一单调字段；真凶是挑战者覆写 bug（33/43 HIGH 被抹成 6.0，`llm_synth.py:2074-2077`） |
| §2.2-F "score 7 档反向 = 说服力≠预测力" | **撤回**：时代伪影（7.0 仅存在 8/25 前、6.0 在 8/25 后，契约切换不可比） |
| §2.2-E "DISAGREE=0，独立方向设计失败" | **修正**：gate 口径 DISAGREE=8（5 条真反向）但全部 decision=BLOCK → 指令侧采样偏差 |
| §2.1-3 "84% 开盘 90 分钟"支持加强反追涨措辞 | **维持撤回**：基率伪影（全体 93.9%），措辞杠杆≈0 |
| 甲类删减"零风险" | **修正**：零行为风险但高测量风险（蒸馏影子 v1 臂=live prompt，9/16 前动 SYSTEM_PROMPT 污染对照） |
| §2.3 乙1 判据"违约率≤20%" | **作废**：触发器实为依从率 100% 但恒写满 2 条（信息量 0），判据已改为多样性口径 |

最终执行方案、排序依赖图与裁决请求清单（P1-P9）以 PROPOSAL 文档为准。

---

## 九、实施记录（2026-09-05，用户裁"如无问题可以实施"）

实施范围 = PROPOSAL 裁决清单 P1-P4 + P7 工具侧；P5（W1/W2 DA prompt 文本）、W4（挑战者契约，B11 窗 9/12 后）、W3/W5/W6、丙3（成交量→量比改名，随 W4 双链）按依赖图冻结未动。**未动任何退出参数（C1 冻结），未做 git 提交（等用户明示）。**

### 9.1 落地清单

| 项 | 文件 | 内容 |
|---|---|---|
| 丙1 修覆写 | `deep-stock-analysis/scripts/llm_synth.py`（同向分支） | 挑战者同向低分**不再覆写** analyst score；改记 `_challenger_score_shadow` + `_score_lowered_by_challenger` 影子字段。反向 SKIP 的 `min(orig, chall)` 不动（真决策变更） |
| 丙2 落 band | `deep-stock-analysis/scripts/pipeline.py` | raw_instruction 增 `score_band` / `challenger_score_shadow` / `score_lowered_by_challenger` 三字段持久化 |
| 基数自检 | `scripts/shadow_field_cardinality.py`（新） | 只读；DEGEN 判据 unique<5 或 top_share>60% 或 entropy<0.60；FIELD_RULES 豁免设计性低基数字段；n<30→INSUFFICIENT；n≥30 DEGEN→exit 1 |
| 丙1 反事实 | `scripts/da_score_overwrite_counterfactual.py`（新） | 回放 gate 日志中 da_score < band 映射值的行，重算 decision_conf/tier/择时闸，含保真校验 |
| U2 删 cap | `scripts/llm_universe_scan.py` | `_UE_CONF_CAP` 删除，落 raw_conf（含 price_floor/low_atr SKIP 行）；留"禁止线性 conf 门"护栏注释 |
| U1 渲染侧 | 同上 + `config/unified_config.yaml` | 极端涨跌带渲染默认关（`clue_render_extreme_band: false`）；多空对称截断默认开（`clue_side_symmetry: true`）；反馈表跳过"待回填"行 + max_rows 截断 |
| U4 v2.9 | 同上 | `PROMPT_VERSION = "v2.9"`，kimi 26 行契约文本（1-9 条 + LONG null 示例 + 完整 SHORT 示例），`PROMPT_HASH_V2=8964bcae8322`；V1 原样保留作 A/B 基线 |
| U3 tier 闸影子 | 同上 | `_tier_gate_verdict`（HARD_EVENT→pass；MC→cross_hit 或 raw_conf≥0.70 且 rubric≥3 才 reinforced，否则 would_block；THESIS_ONLY→shadow_block）+ `_log_tier_gate_shadow` 落 `data/shadow/ue_tier_gate_shadow.jsonl`；`_corro_syms` 持久化 `data/ue_corro_syms_{date}.json` |
| 配置键 ×4 | `config/unified_config.yaml` | `clue_render_extreme_band` / `clue_side_symmetry` / `tier_gate_shadow`（默认如上）+ `tier_gate_enabled: false`（enforce 未实现，置 true 只触发误配告警，已在 cmd_inject 消费——非死键） |
| 测试同步 | `tests/test_da_output_contract.py` | 3 用例改影子语义：保守例 score 保持 7.0 + shadow 5.0 + 标记 True；激例 shadow 8.0 无标记；band 映射例 score 7.0 + shadow 3.0（LOW 映射）+ 标记 True |

### 9.2 丙1 反事实回放（verbatim，`da_score_overwrite_counterfactual.py`）

```
回放器保真: 32/33 行 orig 重放 tier == logged tier
  不一致样本: 2026-09-03 US.MRK logged=良 replay=中 (dc 近似偏差)

── 若 score 未被覆写 (回放) ──
tier 翻转: 29/33   (全部 HIGH 行 score 6.0→8.0: 良→优, 即 PASS/small → PASS/normal)
  2026-08-25 US.ORCL ... 2026-09-04 US.MRK (29 行, 明细见脚本输出)
decision 翻转 (PASS/WARN/BLOCK 变): 0
仓位档翻转 (如 small→normal): 29
```

**爆炸半径诚实记录：修覆写对历史 33 个被覆写行（全为 HIGH 带）只放大仓位档（small→normal），零准入翻转。** 与提案 §八-1 一致：区分度恢复的价值在前向，不在历史重放。

### 9.3 字段基数自检基线（verbatim，`shadow_field_cardinality.py`，exit 1 为设计行为）

```
⚠️  6 个字段 DEGEN (n≥30)
da_gate         da_score               237  唯一3  6.0=75.9%  DEGEN   ← 丙1 诊断实证
da_gate         challenger_score       237  唯一3  6.0=75.9%  DEGEN   ← 同上(覆写同源)
da_gate         invalidation_trigger_count 237 唯一1  2=100%  DEGEN   ← 乙1 已诊断(恒写满2条)
da_gate         tripwire_count         237  唯一2  2=99.6%   DEGEN   ← 同上
da_instruction  score                  234  唯一3  7.0=55.1%  DEGEN   ← 丙1 下游实证
ue_instruction  confidence             100  唯一1  50=100%    DEGEN   ← U2 诊断实证(cap=50)
da_instruction  score_band               0  INSUFFICIENT              ← 丙2 刚落地, 待攒样
ue_instruction  raw_confidence         100  唯一18 55=14.0%   OK      ← 盖帽下 raw 数据一直在
da_gate         score_band             237  MEDIUM=62.0%      OK      ← 丙2 已生效
ue_scan         confidence/rubric/self_tier 全部 OK
```

6 个 DEGEN 全部是提案已诊断字段（丙1×3、乙1×2、U2×1），零意外；前向攒样后同一脚本即毕业判据工具（n≥30 且 entropy/top_share 回正常区间）。

### 9.4 回归证据

- DA 契约测试 35 通过（含 3 个改写用例）；UE 测试 82 通过。
- 全量套件 **2784 passed / 2 failed**：两例均为本分支（rfc-060-phase1-hotfixes）既有 live-DB 状态用例（`test_live_positions_rule4_no_longer_misfires`、`test_live_rows_aligned_with_trade_records`），与本批改动无关（`git stash -u` 隔离验证：本批改动 stash 后同样两例失败）。
- 治理门禁通过：`test_config_key_census`（4 个新键全部被消费，无死键）、`test_except_pass_ratchet`（`_load_corro_syms` 无静默 except，失败响warn+空集兜底）。
- tier 闸影子写路径冒烟：临时文件 n=3，n_would_block=2，passed_chain 标记正确。
- U1 渲染冒烟：合成候选文件验证 gainers 3→1 截断、极端带行消失；v2.9 JSON 示例经 `_normalize_surfaced` 往返无损。
- 丙1 合并行为冒烟：同向低分 score 8.0 保留 + shadow 6.0 落字段。

### 9.5 回滚开关

| 开关 | 默认 | 回滚动作 |
|---|---|---|
| `universe_expansion.clue_render_extreme_band` | false | 置 true 恢复极端带渲染 |
| `universe_expansion.clue_side_symmetry` | true | 置 false 恢复多空不对称 |
| `universe_expansion.tier_gate_shadow` | true | 置 false 停影子日志 |
| `universe_expansion.tier_gate_enabled` | false | enforce 未实现；置 true 仅告警（防误配） |
| 丙1 | 代码 | revert `llm_synth.py` 同向分支即恢复覆写（影子字段随之消失，无副作用） |
| U2 | 代码 | 恢复 `_UE_CONF_CAP` 一处即回 |

### 9.6 冻结未动（按依赖图）

- **W1/W2**（DA prompt 甲类删减 + scenarios）：9/16 蒸馏影子裁决前不动 SYSTEM_PROMPT（v1 臂=live prompt，动则污染对照）。
- **W4**（挑战者契约）+ **丙3**（成交量→量比）：B11 窗 9/1-9/12 不动挑战者，9/12 后启动。
- **W3/W5/W6**、P9（月报 D3 升级）：按提案 §五/§六 判据，待 tier 闸影子 ≥15 日数据。
- 全部改动**未提交 git**，等用户明示。

---

## 十、追加：UE 模型 thinking 模式探针 + 影子 A/B（2026-09-05 夜，用户令"要"）

**背景发现**：UE 主模型 glm-5.2 的 `enable_thinking` 现被 `model_call.py:159-160` 全局关闭——该开关是 2026-07-06 P0 为 800-tok 投票/评分小调用设计（thinking 吃光预算 → empty_response，78/210 失败根因），UE 的 16000-tok 大调用系**误伤**。路由级 `extra_body.enable_thinking` 灰度基建已在（2026-07-25 E3），开生产 = yaml 一行，回滚 = 删 key。

### 10.1 探针（`scripts/probe_glm_thinking.py`，生产同路径 call_llm，verbatim）

```
P1 glm OFF mt=16000 [生产基线]  → OK   3.1s  in=116 out=111  finish=stop schema✓
P2 glm ON  mt=16000             → OK  17.7s  in=122 out=987  finish=stop schema✓
P3 glm ON  mt=32000 [上限探测]  → OK   6.1s  in=122 out=243  finish=stop schema✓
P4 deepseek ON mt=16000 [fallback 本体] → OK 8.5s out=414 json_valid✓
```

四问全有答：**① thinking+json_mode+schema 校验链兼容（glm 与 fallback deepseek 均通）；② reasoning token 增量小任务 ≈876；③ 时延增量小任务 +14.6s；④ max_tokens=32000 DashScope 接受**（16000 旧上限有扩容空间）。注意 thinking 时长逐轮波动大（P2 17.7s vs P3 6.1s 同 prompt）。

### 10.2 影子 A/B（`llm_universe_scan.py thinking-eval` 新仪器，真实 clue brief 4202 字符，verbatim）

| 指标 | OFF（生产现状） | ON |
|---|---|---|
| schema 通过 | ✓ | ✓ |
| 时延 | 29.6s | **538.1s（18x）** |
| tokens_out（含 reasoning） | 1,392 | 6,306（4.5x） |
| 浮现数 L/S | 3/2 全 MARKET_CORROBORATED | 2/2，**HARD_EVENT×2 + MC×2** |
| invalidation/checklist/short_check/skip_reason/引证 填充 | 全 100% | 全 100% |
| triggers 非空 | 4/5 | 0/4（键在=合规，内容空） |
| conf 均值 | 0.628 | 0.700 |
| 标的差异 | FICO-S/GS-L/USAR-L 仅 OFF；IOT-L、LULU-S 共有 | **CCK-S(0.65)、XLF-L(0.75) 仅 ON 且均为 HARD_EVENT** |

记录落 `data/shadow/thinking_eval.jsonl`（含双臂原始浮现清单，可复查）。

### 10.3 判读（n=1 方向性信号，非结论）

- **质量面方向性利好**：ON 臂把浮现从"全 MC 桶"移向"HARD_EVENT+MC 混合"——与历史盈亏结构同向（HARD_EVENT 17 笔 +$101.54 / MC 12 笔 −$379.10）。ON 独有的 CCK-SHORT 锚定简报里真实存在的 8-K CEO 离任事件，非幻觉。
- **工程面两个硬约束**：① 时延 538s——外层 cron `timeout 1800` 可容纳单轮，但 repair 调用同路由也会变慢，链路总时长需观测；② token 4.5x，若复现 9/1 内容侧 8000 触顶 + reasoning ≈5000 → 13000 贴近 16000 上限，配套动作=抬 32000（探针已证可收）。
- **triggers 内容回退**（4/5→0/4 非空）需观察是否稳定出现——该字段是盘中失效核销器唯一结构化输入。
- **结论：暂不裁生产切换。** ① 单轮 n=1 不足为凭；② v2.9 观察窗（≥15 交易日，至 ~9/26）内动模型 = 双变量污染。路径：每交易日跑一轮 `thinking-eval` 攒样（影子、零生产影响），≥10 轮后按 §十预注册判读（schema 通过率不降 / 时延分布 / tier 结构偏移稳定性 / triggers 回退是否持续）再裁。
- **落地（2026-09-05 用户裁"同意建议"）**：① cron 已挂 ~~22:30~~ → **21:12 CST 工作日**（用户裁定"都在盘前执行的才有可比性"：紧随 21:10 主 scan 建情报板，同盘前数据快照；brief 采集 ~1min + OFF ~0.5-1min + ON ~9min ≈ 21:23 前完成，21:30 开盘前收尾；与主 scan 并发 2-3 个 glm 调用，DashScope 容量无虞；外层 timeout 1500；日志 logs/thinking_eval.log；回滚=删该行，原 crontab 备份 /tmp/crontab_backup_20260905.txt）。⚠️ 口径备注：第 1 轮（9/5 晚）采于 22:28 CST=开盘后 10:28 ET，臂内对比（on/off 同 brief）有效，但与生产盘前条件的对齐自 9/8 轮起；② 攒样裁决提醒已设 9/22 09:30（届时 9/5 首轮 + 9/8-9/19 盘前 10 轮共 11 轮）；③ 即使届时裁"开"，生产灰度仍等 v2.9 窗口满（~9/26 后）才执行，且配套 max_tokens 16000→32000（探针已证 DashScope 可收）+ 观测 repair 调用链总时长。
