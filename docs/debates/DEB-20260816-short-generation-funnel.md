# DEB-20260816: 空头生成漏斗断点 (LLM评级层标签-分数分裂)

> **立案日**: 2026-08-16
> **状态**: 共识项1已实施 (2026-08-16 §九: prompt score 语义+record-only一致性校验, 零执行风险); 项2-5 按阶段纪律留待
> **立案人**: Qoder (应用户要求, 与 DEB-20260816-regime-entry-gate 并行; 不预设立场)
> **关联**: DEB-20260816-regime-entry-gate (regime门是"何时拦多", 本案是"如何生空"——一体两面);
> 8/16 Hermes 空头诊断 (llm_synth 三问gate 排除 + short_funnel_shadow 首行读数);
> `scripts/short_funnel_shadow_tag.py` (8/16 挂载, AFTER_HOURS, 每日积累漏斗证据)
---
## 一、主张拆解

| # | 主张 | 类型 | 现状 |
|---|---|---|---|
| P1 | 空头断点在 **LLM 评级层**, 不在扫描层/门禁层/执行层: bridge 候选 6 日方向分布 69 LONG / 2 SHORT (空头 2.8%); 8/15 DA 轮 35 只深度分析 → 看空标签仅 1 (2.9%) | 诊断 | 实测, 见 §2.1/2.2 |
| P2 | 断点机制是**评级标签与分数语义分裂**: skill 代码存在确定性映射 (`deep_analyzer.py:679` score≥6→看多 / ≤4→看空), 但 LLM 自由生成标签架空了它——8/15 出现 看多(4.0)×1 / 看空(7.0)×1 矛盾对, 16 只分数≤4 的标的被贴"观望" | 机制 | 实测, 见 §2.3 (冒烟枪) |
| P3 | 两种互斥假设待判别: **H1 标签压扁** (16 只本是高确信看空, 被错贴观望) vs **H2 分析真空** (LLM 对这 16 只 genuine 中立, 池/分析本身产不出空头论点)。判别方法见辩论点 1 | 假设 | 未判别 |
| P4 | 修复方向依赖 P3 判别: H1 → F1 确定性派生标签 (score→rating 强制映射, 废除 LLM 自由标签); H2 → F2 双轴语义 (direction 与 conviction 分离定义+分离消费) 或 F3 池层空头配额 | 方案 | 待裁决 |
| P5 | 任何修复先影子 OOS (重打标历史 DA 日志得反事实空头产出), 判据预注册后才动生成路径 | 纪律 | — |

**争议核心**: P2 的矛盾对证明"标签≠分数函数"是事实, 但它到底是 bug (H1) 还是语义分裂下的合理产出 (H2) 未定。若 H1, 一行确定性映射即可释放 16/35→46% 的空头候选; 若 H2, 问题在分析 prompt 与候选池的多空结构, 修标签无用。**裁决错误方向的代价**: H2 误判为 H1 → 把 16 只"低确信中立"强转看空 → 垃圾空头指令淹没通道 (7月 SHORT 37笔 胜率仅41% 已是低质证据)。
---
## 二、证据包 (全部实测, 8/16 核实)

### 2.1 bridge 层方向分布 (data/opportunity_scan_*.json 实读)

| 日期 | 候选数 | LONG | SHORT | 空头占比 |
|---|---|---|---|---|
| 8/5 | 15 | 15 | 0 | 0% |
| 8/6 | 16 | 15 | 1 | 6% |
| 8/7 | 10 | 10 | 0 | 0% |
| 8/12 | 6 | 6 | 0 | 0% |
| 8/13 | 6 | 6 | 0 | 0% |
| 8/14 | 18 | 17 | 1 | 6% |
| **合计** | **71** | **69** | **2** | **2.8%** |

⚠️ **复核修正 (2026-08-16 Hermes 独立复算)**: 上表漏列 8/3/8/4/8/10/8/11 四个文件。全量 10 日复算 = **113 候选 (111 LONG / 2 SHORT), 空头占比 1.8%** (非 71/2.8%)。遗漏 4 天全部为纯 LONG 日, 修正后 P1 诊断**更强** (bridge 空头比原文更干旱)。复算命令见 §六。

### 2.2 执行层方向分布 (trade_records 实测)

- 8月 PLAYBOOK_SIM: LONG 69 笔 (-$1,029, WR 28%) / SHORT 9 笔 (-$186, WR 22%) → SHORT 占 11.5%
- SHORT 逐日: 8/3×1, 8/4×2, 8/5×3, 8/6×1, **8/7-8/13 零**, 8/14×2 (JD/LLY, 均平推: -$5.39 费 / -$2.97)
- 历史 SHORT: 6月 n=4 +$1,054 (WR 75%) / 7月 n=37 +$13 (WR 41%) → 7月曾有过量, 8月断崖
- bridge 2.8% < 成交 11.5% → 部分 SHORT 来自 bridge 外路径 (UE/再入场), **bridge 主通道空头更干旱**

### 2.3 标签-分数分裂 (冒烟枪, logs/da_pipeline_stderr.log-20260815 实测)

8/15 DA 轮 35 只, 标签×分数交叉表:

| 标签 \ 分数 | 3.0 | 4.0 | 5.0 | 6.0 | 7.0 | 合计 |
|---|---|---|---|---|---|---|
| 看多 | 0 | **1** ⚠️ | 0 | 4 | 13 | 18 |
| 观望 | 2 | **13** | 1 | 0 | 0 | 16 |
| 看空 | 0 | 0 | 0 | 0 | **1** ⚠️ | 1 |

- 确定性规则 (skill `deep_analyzer.py:679`: `score>=6→看多, score<=4→看空, else 观望`) 若生效 → 应为 **看多18 / 观望1 / 看空16** (空头 46%)
- 实际标签: **看多18 / 观望16 / 看空1** (空头 2.9%)
- ⚠️ 矛盾对: 看多(4.0/10)×1, 看空(7.0/10)×1 — 标签与分数独立生成, 无一致性校验
- ⚠️ **复核修正 (2026-08-16 Hermes 独立复算)**: 交叉表逐格吻合 (看多18=4.0×1+6.0×4+7.0×13 ✓), 但"16 只分数≤4 被贴观望"实为 **15 只 4.0 + 1 只 5.0** (≤4 口径=15)。确定性映射应产出看空 15 (非 16), 不改变结论。
- **关键歧义**: 看空(7.0) 若 score=bullishness 则矛盾; 若 score=分析确信度则自洽 → score 语义本身未文档化, 是 H1/H2 判别的钥匙

### 2.4 已排除的层 (Hermes 8/16 诊断 + 本立案复核确认)

- 扫描层: top_bearish 已按 conviction 合并进池 (pipeline.py L243), 不是断点
- conf 门: 8/9 已放宽 70→55, 不是断点
- 三问gate (逼空/催化剂时效): 8/14 漏斗读数 gate_block_hints=0, 当日零拦截, 不是断点
- 执行门: 8/14 空头指令 2 → 成交 2, 执行层无损耗

### 2.5 二级问题 (生成质量×时机, 随主案跟踪)

8/14 仅有的 2 笔 SHORT 均平推: JD 11:28 入场 (普跌日跌势已过), LLY 12:46。即使漏斗修好, 空头指令的**入场时机**仍依附盘前分析, 缺乏盘中 regime 恶化时的动态放空通道——与 DEB-20260816-regime-entry-gate 辩论点 3 (披露日=空头机会窗) 直接相关。
---
## 三、辩论点 (反方必须攻击的 5 处)

1. **score 语义判别 (H1 vs H2 的钥匙)**: 反方要求抽样 8/15 的 16 只 观望(4.0) 标的 obsidian 分析笔记 (obsidian_vault/symbols/), 对比 bear_case vs bull_case 强度与条数: 若 bear>bull 普遍 → H1 标签压扁成立 (F1 修复); 若 bull≈bear 或无强论点 → H2 分析真空成立 (F1 无效, 转 F2/F3)。n≥8 抽样即有判别力。
2. **确定性映射的副作用**: F1 (score→rating 强制映射) 假设 score 可靠。但 7 月 SHORT n=37 胜率仅 41% — 若 score 本身噪声大, F1 会把"低质中立"批量转"低质空头"。反方要求: F1 影子期必须带质量门 (如 score≤4 且 bear_case 含 HIGH 强度论点才转 SELL_SHORT 候选)。
3. **池层结构**: 35 只被分析标的中, 来自 bearish 扫描侧的占多少? 若池本身 90%+ 是 bullish 候选, LLM 评级层再准也产不出空头——反方要求查当日扫描日志的池构成, 区分"池无空"与"评不出空"。
4. **7月 37 笔 SHORT 的来源路径**: 若它们主要来自 bridge 外路径 (UE/再入场/旧 signal_pipeline), 则"修复 bridge 空头"对总产出的边际贡献存疑; 反方要求拆分 7 月 SHORT 的 decision_id 前缀分布, 确认 bridge 通道修复的真实杠杆。
5. **与 regime 门的互斥风险**: DEB-20260816 拟在披露日降速入场; 若本案修好空头生成, 披露日恰是空头高产窗——两案在"披露日该多开还是少开"上可能冲突。反方要求明确: regime 门的方向感知条款 (P3) 是否以本案空头产出可用为前提, 两案裁决顺序如何排。
---
## 四、观察点 (影子持续积累, 裁决日读数)

1. `data/short_funnel_shadow.jsonl` 逐日四层读数: analyzed_n → rated_bearish → short_instructions → executed (8/16 已挂载, 首行 8/14: 35→1→2→2)
2. 逐日标签-分数矛盾对计数 (看多且 score<6 / 看空且 score>4) — 分裂是否逐日稳定存在
3. 观望(≤4) 标的中 bear_case 强度分布 (抽样人工读, 辩论点 1)
4. 空头指令入场时刻 vs 当日 regime 恶化时点 (二级问题, 时机通道缺口的量化)
---
## 五、预注册判据 (若辩论裁决"修复", 分两阶段)

**阶段 0 — 判别 (1-2 交易日, 纯人工/只读)**:
- 按辩论点 1 抽样 ≥8 只 观望(≤4) 标的读 obsidian 笔记, 按辩论点 3 查池构成, 按辩论点 4 拆 7月 SHORT 路径
- 判据: H1 (bear>bull ≥6/8) → 走 F1; H2 → 废 F1, 转 F2/F3 重新设计; 池无空 (bearish 池占比 <20%) → 池层修复并入 F3

**阶段 1 — 反事实影子 (修复实现后, 重打标历史 DA 日志, 零执行改动)**:
- 用选定修复方案重打标最近 10 个 DA 轮的日志 → 反事实空头候选清单 → 用当日价格轨迹估算反事实结局
- 判据: 反事实空头候选 n≥10, 且反事实均笔 PnL > 0 (对齐 8月 LONG 均笔 -$14.9 的机会成本基准) → 进入阶段 2
- 若反事实均笔 ≤ 0 → **不修复** (空头干旱可能是正确的市场判断, 非 bug)

**阶段 2 — 转正 OOS (实施后 n≥30 空头指令)**:
- 判据: 空头占比回升至 ≥20% (对标 7月 37/(37+145)=20%), 且 SHORT 均笔 PnL ≥ LONG 均笔, 且 WR ≥ 40%
- 止损: 修复后 SHORT 均笔亏损 > LONG 均笔亏损 2x → 回滚 (F1: skill 代码恢复 LLM 自由标签 / F3: 池配额置 0)

**回滚**: 全部修复均在 skill 层 (deep-stock-analysis) 或扫描池层, config 键控制, 不动执行路径。
---
## 六、裁决请求

请辩论双方就 P1-P5 逐条表态, 重点回应辩论点 1 (H1/H2 判别) 与 3 (池层结构)。
禁止只引用本文件结论——交叉表可自行复算:
`grep "评级:" logs/da_pipeline_stderr.log-20260815 | sort | uniq -c`;
bridge 方向分布: `python3 -c "import json; [print(f, __import__('collections').Counter(c['direction'] for c in json.load(open(f))['candidates'])) for f in __import__('glob').glob('data/opportunity_scan_2026-08-*.json')]"`。
---
## 七、独立裁定 (Hermes, 2026-08-16, score 语义一锤定音)

应用户要求, 对辩论点 1 (score 语义判别) 做实证裁决。**结论: P2 机制叙事需修正, F1 修复方案在实证语义下不成立。**

### 7.1 决定性证据: 看空(7.0) 标的 = US.JD 全链路

```
评级: 看空 (7.0/10)          ← score=7.0, 方向=看空
Gate: PASS score=55
IC Writer: SHORT strength=70  ← 7.0×10=70, score 直接作为空头信号强度
成交: US.JD SHORT pnl_net=-5.39 (8/14, 平推)
```

adapter.py L313/L389: `"strength": int(score * 10)`——**下游把 score 当方向无关的信号强度消费** (看空 7.0 → SHORT strength 70)。若 score=bullishness (多空程度), 看空标的应该低分, 7.0 自相矛盾; 实证链路证明 score 语义 = **方向无关的确信度/综合评分**。

### 7.2 交叉表语义复核 (全部自洽于 conviction 语义)

| 现象 | bullishness 语义 | conviction 语义 |
|---|---|---|
| 看空(7.0)×1 (JD) | ❌ 矛盾 | ✅ 高确信看空 |
| 看多(4.0)×1 (RDTL) | ❌ 矛盾 | ✅ 低确信看多 |
| 观望(≤4)×15 | — | ✅ 低确信无方向 |

score≥6 同时出现于看多(17)与看空(1)——**bullishness 语义被证伪**。原文 §2.3 "关键歧义" 裁决: **score = conviction (方向无关), 非 bullishness**。

### 7.3 冒烟枪修正: L679 确定性映射在生产路径是死代码

- 生产入口 pipeline.py L379: `DeepAnalyzer(_data)` 不注入 synth_module → `_synthesize` 默认加载 llm_synth → **走 `_apply_llm_result` (L580)**: rating/score 均直接取 LLM 输出, 零一致性约束
- L679 的 `score≥6→看多/≤4→看空` 映射在 `_rule_synthesize` (L627) 内, **仅 CLI `--synth rule` 可达, 生产从不经过**
- **修正 P2 叙事**: 不是"LLM 自由标签架空了确定性映射"——是**生产路径从来没有确定性映射**。标签-分数分裂是 LLM 双字段自由生成的自然产物, 无 bug 可言, 但也无一致性保障

### 7.4 对 P3/P4 的连锁影响

1. **H1 假设削弱**: conviction 语义下, 观望(4.0) = "低确信无方向", 不等于"本是高确信看空被错贴"。H1 仍需 obsidian 抽样验证 (辩论点 1 方法保留), 但先验概率下降。
2. **F1 方案危险**: `score≤4→强制看空` 在 conviction 语义下 = 把"低确信"批量转成"看空方向"——正是辩论点 2 担忧的垃圾空头量产机。**F1 若要做, 前提是先改 prompt 把 score 重定义为 bullishness** (llm_synth.py L619 现仅写 "0-10的数字", 无语义定义——这是病根)。
3. **修复方向修正**: 真修复 = F2 (direction 与 conviction 分离定义+分离消费) 或 rating/score 方向一致性校验, 而非 F1 强制映射。

### 7.5 辩论点 4 实证 (7月 SHORT 来源, 杠杆评估)

7月 38 笔 SHORT decision_id 前缀: **DEC×18 + universe_expansion×12 + DA×5 + TFB×2 + NULL×1**。
8月 9 笔: UE×3 + TFB×3 + DA×2 + intraday_reentry×1。
**DA 深度分析通道对空头总产出的贡献: 7月 13%, 8月 22%**——空头指令主力来源是 DEC/UE 通道。修复 DA 评级层对总产出的边际杠杆有限, 辩论时须同时评估 DEC/UE 空头通道的状态与质量。

### 7.6 裁决建议

- P1 成立 (bridge 空头 1.8%, 修正后更强); P2 机制叙事按 §7.3 修正
- 阶段 0 判别保留 obsidian 抽样, 但 H1 先验降级; **新增前置动作**: 修改 llm_synth prompt, 为 score 写明语义定义 (bullishness 或 conviction 二选一) + 加 rating/score 方向一致性校验——此动作零执行风险 (仅 prompt 文本), 可先行
- F1 在 prompt 语义未改前**禁止实施**
---
## 八、复核裁定 (Qoder, 2026-08-16): §7 逐条核验 + DEC 通道死因新证据

### 8.1 §7 核验结论 (Qoder 独立复算)

| §7 主张 | 裁定 | 复核证据 |
|---|---|---|
| §2.1 修正 (10日=113候选, 111L/2S, 1.8%) | **接受** | 复算确认漏列 4 日全为纯 LONG。补充: glob 全量含 1 个 undated deepseek 变体文件则为 130/128L/2S=1.5%——排除 undated 文件方法合理, 但须在文中写明口径 |
| §7.1 score=conviction (JD 链路) | **接受** | 独立复验: 8/14 bridge JD 记录 score=7.0 → confidence=0.7, direction=SHORT。bullishness 语义证伪成立 |
| §7.3 L679 生产死代码 | **接受** (经验口径) | 调用链在 workspace 外未能逐行复核; 但 看多(4.0)×1 的存在即经验证明生产无强制映射, 结论自洽 |
| §7.4 F1 禁令 (prompt 语义未改前) | **接受** | conviction 语义下 score≤4→强制看空 = 低确信批量转方向, 垃圾量产机, 与本立案 P4 原警示一致 |
| §7.5 通道杠杆 (DA 仅 13%/22%) | **接受并加强** | 独立复算: 7月 DA=5/37 (13.5%), 8月 DA=2/9 (22%) ✓。July 总数 Hermes 38 vs 本复核 37 — 差 1 为 NULL decision_id 行, 非实质 |
| §2.3 微修正 ("看空15非16") | **部分驳回** | 该修正本身不准: 对全表 35 只应用确定性规则, 看空=16 (看多(4.0)×1 的 score 同样 ≤4)。准确表述应为: 16 只观望标签中 15 只 ≤4 + 1 只 5.0。不影响任何结论 |

### 8.2 新决定性证据: DEC 通道的死因与死亡时点 (杠杆排序因此改写)

- **DEC 最后成交 2026-07-08 15:55** (trade_records 实测); 7月 18 笔 DEC SHORT **全部在 7/8 前**, 此后 5+ 周全静默 (多空皆零)
- **死因**: 7/9-7/11 bridge 模式改造移除投票 fallback — scheduler_core.py:248-252 注释实锤: "3模型投票方向准确率47%≈随机 (DeepSeek 88%偏空/qwen 88%偏多), bridge不存在时fallback到投票=用噪声替代信号" → **DEC 是被故意废弃的噪声通道, 不是故障**
- **推论 1 (基线修正)**: 7月 "37 笔 SHORT" 的繁荣含 18 笔已证伪的噪声产出。有效空头产能基线应剔除 DEC ≈ **19 笔** (UE12+DA5+TFB2)。阶段 2 转正判据"空头占比≥20%"的分母基准须改用有效口径
- **推论 2 (杠杆重排)**: 真实空头杠杆 = 修 DA 评级层 (现占 13-22%) + **扩 UE 通道** (7月12→8月3, 唯一活的主通道也在衰减)。DEC 复活不应进入方案空间 (死因是质量, 不是断链)
- **推论 3 (干旱全貌)**: 8月 SHORT 断崖 = DEC 故意废弃 (7/9) + UE 衰减 + DA 评级层干旱 三重叠加, 本案 (DA) 只覆盖其中一环

### 8.3 合并共识 (Qoder × Hermes, 待用户终裁)

1. **前置动作 (零风险, 可立即做)**: llm_synth prompt 写明 score 语义 (建议 conviction) + rating/score 一致性校验 — 双方一致
2. **阶段 0 增补**: obsidian 抽样保留 (H1 先验已降级); 增补 **UE 通道空头衰减诊断** (7月12→8月3 的原因: 池? 门? 候选质量?)
3. **基线口径修正**: 所有占比/转正判据改用有效基线 (剔除 DEC 噪声): 7月有效 SHORT 19 笔 / (19+184) ≈ 9.4% — 原"对标 7月 20%"判据失效, 重定为 **≥有效基线 2 倍 (≈19%) 或用户另裁**
4. **方案空间收敛**: F1 禁 (prompt 语义未改前) / DEC 复活禁; 可行域 = F2 (双轴语义) + F3 (池配额) + UE 通道扩产, 全部走预注册影子
5. **与 regime 门协同**: 维持原判 — regime 门 P3 方向感知条款以本案空头产出可用为前提, 两案并行推进

---
## 九、实施前独立验证 + 共识落地 (Hermes, 2026-08-16, 用户批准"要")

用户裁决: 共识方案先解释验证、确认无异常后再实施。本节为逐项验证记录与落地结果。

### 9.1 代码路径主张复核 (全部通过)

| 主张 | 验证方法 | 结果 |
|---|---|---|
| score 语义=conviction (§7.1) | 重读 adapter.py L313/L389 `strength=int(score*10)` | ✅ 确认: 看空 7.0→strength 70, 下游方向无关消费 |
| L679 映射生产死代码 (§7.3) | pipeline.py L379 `DeepAnalyzer(_data)` 无 synth_module → `_synthesize` L423-429 默认 import llm_synth → `_apply_llm_result`; `_rule_synthesize` 仅 `synth=='rule'` 可达 (L1084-1085 CLI 专用) | ✅ 确认生产路径零确定性映射 |
| llm_synth.py L619 prompt 无语义定义 (§7.4) | 实读: `"score": 0-10的数字` | ✅ 确认病根 |
| 8/15 交叉表 (§2.3) | 重解析 da_pipeline_stderr.log-20260815: n=35, 看多18/观望16/看空1 | ✅ 逐字吻合 (含 观望≤4 实为 15只4.0+1只5.0 的 §7 修正) |
| DA 服务不 import skill (§回滚隔离) | grep app/ 零引用 llm_synth/deep_analyzer | ✅ DA pipeline 由 hermes cron 独立运行, 改动不触 ftseek-us 服务 |

### 9.2 DB 主张复核 (5 项通过, 1 项异常修正)

| 主张 | 复算 (data/ftseek_us.db, source_type='PLAYBOOK_SIM') | 结果 |
|---|---|---|
| §7.5 7月SHORT DEC18/UE12/DA5/TFB2 | 复算 DEC:18 UE:12 DA:5 TFB:2 合计37 | ✅ 逐字吻合 |
| §7.5 8月 UE3/TFB3/DA2/IR1 | 复算 UE:3 TFB:3 DA:2 IR:1 合计9 | ✅ 逐字吻合 |
| §8.2 DEC 最后成交 7/8, 此后全静默 | MAX(trade_date)=2026-07-08; ≥7/9 n=0 | ✅ 确认 |
| §2.1 bridge 空头占比 | 8月 opportunity_scan 全量: 128L/2S=1.5% (含 undated deepseek 变体文件, §8.1 已声明口径) | ✅ 与 §8.1 口径一致 |
| §8.2 推论1 有效基线 SHORT 19 | 剔DEC后 SHORT=19, LONG=128 | ✅ 分子正确 |
| **§8.3 项3 "19/(19+184)≈9.4%"** | **184 = 全7月总笔数(含DEC); 19 = 剔DEC分子 → 口径混用** | ❌ **异常: 正确口径 = 19/(19+128) = 12.9%** |

**基线异常修正**: 共识 §8.3 项3 的 9.4% 把剔除 DEC 的分子 (19) 放在含 DEC 的分母 (184) 上。同口径有效基线 = **19/147 = 12.9%**, 对应 2x 转正判据 = **25.9%** (非 ≈19%)。原"对标7月 20%"同样失效 (该 20% 也含 DEC 噪声: 37/182)。阶段 2 判据据此修订: **空头占比 ≥ 有效基线 2x ≈ 25.9%, 或用户另裁**。

### 9.3 阶段 0 增补诊断结果 (共识项 2)

**obsidian 抽样 (辩论点 1, H1 判别)**: 8/15 的 16 只观望标的笔记全部存在 (US_X.md 命名, mtime 8/14-15)。bear_case 关键词计数: NVDA bear6/bull0, COIN bear4/bull1, GOOG bear5/bull2, TSM bear3/bull2, BA bear3/bull0, NIO bear3/bull0, AMD bear2/bull0, ARM bear2/bull0 — **bear>bull 的标的 ≥ 9/16**。粗筛方向偏 H1 (标签压扁), 但关键词计数非论点强度精读, 判级为"H1 先验上调, 待影子数据终审"。与 §7.4 "H1 先验下降"合并: 先验中性, F1 仍禁, F2/影子先行。

**UE 通道空头衰减诊断 (共识项 2)**: 断点在**候选生成量**, 不在执行门:
- UE SELL_SHORT 指令 7月 25 笔 → 8月 9 笔 (-64%), 同期 UE BUY 32→20 (-37%)
- 触发/执行率无衰减: 7月 12/25 triggered=executed, 8月 3/9 triggered=executed (100% 转化, 未 trigger 的是 entry_band 未到, 非门拦截)
- 8月新增 SKIP 9 笔, skip_reason 几乎全是 `SL_STOP_TOO_WIDE(6.1%>6%)` — SL 宽度准入门在拦 UE 候选, 但只影响 9 笔 SKIP, 不是 -16 笔的主因
- **结论**: UE 空头衰减主因 = llm_universe_scan 生成的空头候选量本身下降 (7月 25→8月 9), 候选池/扫描层问题, 非 DA 评级层可修。UE 扩产若立项, 靶点是 universe_scan 的空头召回

### 9.4 实施记录 (共识项 1, 唯一代码变更, 已验证)

**变更文件**: `/root/.hermes/skills/deep-stock-analysis/scripts/llm_synth.py` (skill repo, 非 FTSeek-US 服务代码)

1. **score 语义写明 (3 处 prompt)**: L619 分析师 score 字段、L934 挑战者 v1 my_score、L984 挑战者 v2 my_score — 全部注明"语义=conviction确信度: 方向无关, 高确信看空=高分, 禁止用低分表达看空, 方向只由 direction 字段表达"。选 conviction 而非 bullishness 的依据 = §7.1 JD 全链路实证 + adapter strength 消费方式, 改 bullishness 需重写下游 strength 语义, 超出零风险边界
2. **一致性校验 (record-only)**: 新增 `_check_rating_score_consistency()` — 不修改任何字段, 矛盾记录进 `result["_rating_consistency"]` + stderr `[ScoreConsistency]` 行 (short_funnel_shadow 可解析)。判定规则对齐 §7.2 conviction 语义: 看多(4.0)/看空(7.0) 判自洽 (不误伤), 仅 flag 真矛盾 (rating↔direction 反向 / 中性rating有方向 / 方向rating却SKIP / 强烈档低确信)
3. **回滚**: `DA_SCORE_CONSISTENCY=0` 关校验; prompt 文本 git revert

**验证证据**:
- py_compile + import ✅
- `USER_PROMPT_TEMPLATE.format(data=...)` 冒烟通过 (新增文本含 `{{`/`}}` 之外的普通括号不影响 .format) ✅
- 9 组单测全过: 8/15 三个真实案例 (RDTL/JD/观望) 判自洽 + 5 组构造矛盾判 flag + 回滚开关 ✅
- 下游兼容性: `_rating_consistency` 全仓仅 llm_synth 自引用, 无消费者, 无 schema 校验点 ✅
- 执行风险: 零 — 不改 score/rating/direction 任一字段值, 不改 gate/adapter/ic_writer

**未实施项 (共识中非"前置动作"的部分, 按阶段纪律留待)**:
- 项 2 obsidian 抽样 → 已在 §9.3 完成粗筛 (H1 先验上调, 不终审)
- 项 3 基线口径 → 已在 §9.2 修正落文
- 项 4 F2/F3/UE 扩产 → 全部需先过阶段 1 反事实影子, 本次不动
- 项 5 regime 门协同 → 并行案, 不受本次影响

**生效时点**: hermes cron 下次 DA 盘前轮 (北京 ~19:00) 自动加载新 prompt; 一致性数据从次日起进 stderr, short_funnel_shadow n≥10 交易日读矛盾率裁决下一步。



---
## 十一、Hermes 补充 (2026-08-16): §10.2 的 pending_direction 证伪 + 8/8 全空头实锤

### 11.1 R6 (pending_direction 增补) 前提证伪, 关闭

§10.2 担心 admission SKIP 行 direction 被覆写后"原方向不可考"——**不成立**:
`app/playbook/admission.py` L227 在覆写 `inst["direction"]="SKIP"` **之前**已落
`raw["admission"]["original_direction"]`, 随 raw_instruction_json 持久化。无需改码, R6 关闭。

### 11.2 实测实锤: 8 月 admission 拦截 8/8 全是空头

```
date        symbol   original_direction
2026-08-07  US.INDA  SELL_SHORT
2026-08-10  US.WERN  SELL_SHORT
2026-08-10  US.DUOL  SELL_SHORT
2026-08-11  US.JD    SELL_SHORT
2026-08-12  US.HD    SELL_SHORT
2026-08-12  US.RMNI  SELL_SHORT
2026-08-13  US.JD    SELL_SHORT
2026-08-14  US.LH    SELL_SHORT
```

**SL_STOP_TOO_WIDE(6.1%>6%) 门 8 月新增拦截的 8 笔, 8/8 全部是 SELL_SHORT。**
机制: 空头天然 SL 更宽 (止损在入场价上方, 波动率高的标的更宽), 6% 统一上限对空头
构成**不对称伤害**。这是"8 月空头断崖"的一个机械性凶手 (非 LLM 分析层), 与 §八
UE 候选生成量衰减、LLM 转化率低共同构成空头供给的三重压缩。

**处置归属本案裁决** (不并案 no-trigger): 候选方向感知 SL 宽度基线 (按方向×波动率分桶),
或 8/29 验证期后再议。预注册纪律不变: 任何门参数调整先影子复算拦截集反事实
(工具已就绪: `scripts/gate_block_counterfactual.py`, 同族口径)。
