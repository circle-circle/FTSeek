# 议题：DA 信号被日内动量框架执行 —— 07-22 复盘与执行链错配根因辩论

## 状态

- [ ] 进行中
- [ ] 待决策
- [x] 已决策（2026-07-23 用户确认共识稿；E/A-lite shadow 已落地，待验证）
- [ ] 已验证
- [ ] 已关闭

> **本文用途**：供另一 Agent 独立核验 + 反驳辩论。所有结论均附**特征字符串 / SQL / 文件路径**，可不依赖本文记忆直接复现。
> **作者**：Qoder（复盘 Agent） **日期**：2026-07-23 **数据日**：2026-07-22（美股已收盘）
> **辩论原则**：Karpathy（最小改动/防加法思维）+ Ponytail（最短路径/YAGNI/删除优先）。先证伪再建设。

---

## 背景

2026-07-22 美股收盘后复盘，发现当日 Playbook 轨道（DA/UE → DB 轮询）14 笔成交，新仓已实现约 **-158.80**，亏损集中于 6 笔 `TIME_EXIT`（-235）。表面看是"开盘追高买在高点"，但深挖后判断这是**结构性错配**，而非参数问题。

**重要前置纠偏（作者自我修正记录，供辩论方警惕同类错误）**：
- 初版复盘误把 `daily_battle_plan_us.json` 当作入场源 → 已纠正：实际下单轨道是 `playbook_instructions` 表（DA/UE 直写），battle_plan 已下线（`legacy_battle_plan_enabled=false`）。
- 初版误判 `instruction_generator.py:368` 用现价覆盖 DA `planned_entry` 是 bug，建议改回 planned_entry → **已被用户驳回并自我撤回**。re-price 用实时可成交价成交是**正确且必须**的设计（LLM 吐的计划价非实时报价，拿它成交=虚构成交）。**成交价问题不存在，真问题在"要不要入场"这个门。**

---

## 目标（本次辩论需回答）

1. 07-22 亏损的根因是**参数/时机问题**，还是**信号模型与执行模型错配**？
2. DA 这条链的目标定位应是**波段（条件入场+证伪退出）**还是**日内动量（开盘追入+时间止损）**？当前骑墙是否是亏损主因？
3. 优化应优先动**退出契约**、**sizing 主输入**、还是**入场条件门**？

---

## 数据与实证（全部可复现）

### 实证 0：当日下单轨道 = playbook_instructions（非 battle_plan）

```bash
# 日志实锤：auto_execute 消费的是 DB 指令
grep -nE "auto_execute 开始: 15 条指令" logs/ftseek_us.log-20260723
# battle_plan 已下线
grep -nE "旧作战计划轨道已下线|legacy_battle_plan_enabled" logs/ftseek_us.log-20260723
```
```sql
-- data/ftseek_us.db：15 条指令与执行数一致
SELECT symbol,direction,entry_price,confidence,executed,exit_reason,pnl
FROM playbook_instructions WHERE date='2026-07-22' ORDER BY symbol;
```

### 实证 1：上游来源 = DA 14 条 + UE 1 条（raw_instruction_json.source）

| 上游 | source 字段 | decision_id 格式 | 条数 | 已实现 PnL |
|---|---|---|---|---|
| deep_analysis | `deep_analysis` | `DA-20260722HHMMSS-SYM-xxxx` | 14（13 执行） | **-221.93** |
| universe_expansion | `universe_expansion` | `universe_expansion:2026-07-22:SYM` | 1 | **+58.14** |

唯一盈利来自 UE 的 INTC（事件驱动，`self_tier=MARKET_CORROBORATED`，财报前 +8.64% 异动）。

### 实证 2：MFE/MAE 揭示"入场即高点"（核心证据）

```sql
SELECT symbol,exit_reason,execution_price,exit_price,pnl,
       max_favorable_excursion,max_adverse_excursion,holding_period_hours
FROM playbook_instructions WHERE date='2026-07-22' AND executed=1 ORDER BY pnl;
```

| 组 | 标的 | hold_h | MFE | MAE |
|---|---|---|---|---|
| **TIME_EXIT 6 亏** | META/CRM/NTES/UNH/DIS/KO | **全部=4.0** | META=0, NTES=0, 余≤9.6 | 24~74 |
| **SL_BREAKEVEN 6 赚** | GS/AMZN/HON/SYF/MRK/COF | 0.3~0.9 | 15~30 | 0~16 |
| UE 趋势 | INTC（SL_TRAIL_LOCK +58） | 0.5 | **96** | 0 |

**解读**：6 笔亏损 MFE≈0 → 买入后从未盈利，买在日内高点；持有恰好 4h 被时间止损强平。6 笔盈利靠 0.5h 内冲高被保本单接住。**赢家赢在快，输家输在追在高点。**

### 实证 3：DA 的 alpha 在传递中丢失（planned vs 落库 vs 成交）

以 META 为例（`raw_instruction_json` vs DB 列）：

| 层 | entry | stop | tp |
|---|---|---|---|
| DA 原始意图（raw_json.planned_*） | 617.5 | 578.0 | 720.0 |
| DB 落库（generator 改写） | 639.38 | 607.42 | 687.34 |
| 实际成交 | 639.52 | — | — |

DA `entry_strategy` 原文："分2批建仓：第1批在$615-$620区间…第2批在$595-$600…触发条件为日线收盘站稳MA20且成交量>15M"。
DA `invalidation` 原文："若日线收盘跌破$578或QQQ跌破MA60，则AI变现逻辑证伪，立即止损离场"。
→ **条件入场、invalidation 均落库为自然语言，但执行层只消费 symbol+direction**；退出用 4h TIME_EXIT，与"$578 证伪"完全脱钩。

```bash
# generator 用现价/ask 作 entry（re-price 设计，非 bug）
grep -nE "entry_price = tech.ask or price" app/playbook/instruction_generator.py   # :368
```

### 实证 4：confidence 反指，却仍是 sizing/方向核心输入

引自 `docs/entry_exit_strategy_review.md`（2026-07-17，项目自诊断）：
- DA confidence = 辩论共识档（一致=70 / 分歧=35 / 无辩论=50），**非胜率概率**。
- 实测 DA confidence 的 **IC 为负**（低信心桶反而更好）→ 近似反指。
- 07-22 的 13 条 DA 单 conf **全=35**（debate_outcome="➖ 一方弃权(BUY vs SKIP)"），仍全量执行。

### 实证 5：EdgeShadowGate 已退化（拦不住亏损，反拦唯一盈利）

```bash
python3 -c "import json;[print(r['symbol'],r['confidence'],r['source'],r['action'],r['edge_verdict'],r['edge_expectancy']) for r in (json.loads(l) for l in open('data/shadow/exec_gate_audit.jsonl')) if str(r.get('date'))=='2026-07-22']"
```
结果（07-22，14 条）：
- 12 条 DA 单 → `edge=PASS`，且 `edge_expectancy` **全为同一常数 0.000338**（退化，未真正区分）。
- 唯一 `edge=BLOCK` 的是 **INTC**（exp=-0.010352）→ 而 INTC 是当日**唯一盈利单**(+58)。
→ 质量门名存实亡：放行了全部亏损单，拦掉了唯一好单。

### 实证 6：G6 confidence 门作用域失效（门装在没车走的路上）

引自 `entry_exit_strategy_review.md` §B：
- 特征：`if confidence < _min_conf_for_dir`（pipeline.py，BUY 门槛 70 / SHORT 65）。
- DA/UE 单由 skill **直写 DB**，不经过 `_generate_instructions` → **完全绕过 G6**。
- 注释佐证："bridge 候选已由 deep_analysis skill 直写 DB，不需要 VotingCoordinator"。

---

## 核心论点（作者立场，待辩论）

**论点 T（Thesis）**：07-22 亏损的本质是**信号模型与执行模型错配**，不是参数/时机问题。

DA 是**波段信号**（条件入场 + invalidation 证伪 + 催化剂/裁判日），但执行层是**日内动量框架**（开盘市价追入 + 4h 时间止损 + 反指 confidence sizing）。执行层只消费 DA 最弱的输出（方向），丢弃了 DA 的全部 alpha 来源（条件入场/证伪/催化剂）。UE（INTC）按动量做对了，DA 被强塞进动量框架 → 系统性买在高点 → 4h 强平。

**支撑链**：实证 2（MFE=0 买在高点）+ 实证 3（条件/invalidation 丢失）+ 实证 4（反指 conf 驱动）+ 实证 5（质量门退化）+ 实证 6（G6 失效）。

---

## 可选方案

### 方案 A：退出契约化（invalidation 结构化 → 退出由论点驱动）

- 提出者：Qoder（P0）
- 核心思路：把 DA `invalidation` 从自然语言升级为结构化退出条件（证伪价 + 裁判日/催化剂日），退出由论点触发而非 4h 时钟。延伸已有 `invalidation_shadow`（Stage2a），先影子后实装。
- 优点：直击 6 笔 TIME_EXIT；复用已有零件；让 DA 的证伪逻辑真正生效。
- 缺点：自然语言→结构化解析有可靠性风险；DA prompt 需配合输出结构化字段。
- 适用条件：DA 定位=波段。

### 方案 B：sizing 主输入从 confidence 切到风险归一期望

- 提出者：Qoder（P1，沿用 entry_exit_strategy_review §D #6）
- 核心思路：用 `exec_gate_audit` 已落的 edge_expectancy 做 sizing/过滤，弃用反指 confidence。
- 优点：confidence 已证实反指，切换有统计依据；数据管道已存在。
- 缺点：**前提是 EdgeShadowGate 退化先修复**（实证 5：12 条同常数=没在算真期望），否则切过去仍是噪声。
- 适用条件：先完成方案 C 的诊断。

### 方案 C：修复 EdgeShadowGate 退化 + DA 装真入场条件门

- 提出者：Qoder（P2）
- 核心思路：
  1. 排查为何 12 条 DA 单 expectancy 退化成同一常数 0.000338（疑：DA 源特征未喂入 edge 计算，走默认值）。
  2. DA 源恢复"条件入场"语义：价格未回到 DA 计划有利侧（planned_entry 附近）时触发门**不追**，让该等的单子落空。**注意：不改 re-price/成交价，只改"要不要进"。**
- 优点：修好质量门是 A/B 的前提；入场条件门直接减少"高点追入"。
- 缺点：入场条件门若过严会显著降低触发率（需影子验证拦截比例）。
- 适用条件：DA 定位=波段。

### 方案 D（对立假设）：维持日内动量定位，仅清理 DA prompt

- 提出者：（待辩论方补充 / 钢人化）
- 核心思路：若系统本就是"DA 选股 + 开盘市价动量执行"，则 DA 的"回调分批/invalidation"是装饰性文字，从不参与决策。07-22 是动量策略在震荡日的正常亏损。应**清理 DA prompt**，不再输出不会被执行的条件入场文字，避免认知错位；优化重心放在动量本身（开盘冲高的筛选/量能确认）。
- 优点：最小改动，承认现状，不强行让执行层消费波段契约。
- 缺点：放弃了 DA 的条件/证伪 alpha；无法解释为何 6 笔 MFE=0（动量也应有动量确认）。
- 适用条件：DA 定位=日内动量。

---

## 关键分歧（供辩论方逐条攻击）

1. **分歧点 1：根因是错配还是参数？**
   - 作者观点：错配（实证 2/3/4/5/6 链式支撑）。
   - 反方可能：仅是开盘豁免窗 + 追入侧 5% 带过宽的参数问题，调参即可。
   - 可验证方式：调参回测 vs 退出契约化回测，比较 6 笔 TIME_EXIT 是否消除；或论证"调参能否让 MFE=0 的追高单消失"。

2. **分歧点 2：DA 定位波段 vs 动量？**
   - 作者观点：DA prompt 明确输出波段契约（条件入场+invalidation+催化剂），应按波段执行。
   - 反方可能：DA 选股质量不足以支撑波段，动量执行更稳；或波段需 T+1 持仓与现有日内框架冲突。
   - 可验证方式：统计 DA 历史单按"持有到 invalidation/催化剂"vs"4h 时间止损"的期望差；检查持仓框架是否支持隔夜。

3. **分歧点 3：EdgeShadowGate 退化是 bug 还是设计？**
   - 作者观点：12 条同常数 0.000338 = 退化，需修（实证 5）。
   - 反方可能：DA 源本就不该进 edge 计算，常数是占位；或 edge 门本就该对 DA fail-open。
   - 可验证方式：读 `app/decision/edge_shadow_gate.py` evaluate() 对 DA 源的特征输入路径，确认是否走默认分支。

4. **分歧点 4：优化优先级 A vs B vs C vs D？**
   - 作者排序：C（诊断，零风险）→ A（退出契约，最高杠杆）→ B（sizing 切换）→ D 备选。
   - 反方可能：D 最省（删 prompt 文字即可）；或先做 B 因为 confidence 反指是最确定的统计事实。
   - 可验证方式：各方案影子/回测的期望改善幅度对比。

---

## 待辩论方独立核验的命令清单

```bash
cd /opt/ftseek/FTSeek-US
# 实证0 轨道
grep -nE "auto_execute 开始: 15 条指令|旧作战计划轨道已下线" logs/ftseek_us.log-20260723
# 实证2 MFE/MAE
sqlite3 data/ftseek_us.db "SELECT symbol,exit_reason,pnl,max_favorable_excursion,max_adverse_excursion,holding_period_hours FROM playbook_instructions WHERE date='2026-07-22' AND executed=1 ORDER BY pnl;"
# 实证3 planned vs 落库
sqlite3 data/ftseek_us.db "SELECT symbol,entry_price,stop_loss,take_profit,raw_instruction_json FROM playbook_instructions WHERE date='2026-07-22' AND symbol='US.META';"
# 实证5 edge 退化
python3 -c "import json;[print(r['symbol'],r['edge_verdict'],r['edge_expectancy']) for r in (json.loads(l) for l in open('data/shadow/exec_gate_audit.jsonl')) if str(r.get('date'))=='2026-07-22']"
# 实证6 G6 作用域
grep -nE "if confidence < _min_conf_for_dir|不需要 ?VotingCoordinator|_is_bridge =" app/playbook/pipeline.py
# 项目自诊断（强烈建议辩论方先读）
cat docs/entry_exit_strategy_review.md
```

---

## 讨论记录

### 第 1 轮

- Qoder（复盘 Agent）：提出论点 T（信号-执行错配），证据见实证 0-6，方案 A/B/C/D 与优先级 C→A→B→D。已自我撤回"改 re-price"的错误初版建议。
- （待辩论方）：

### 第 2 轮（辩论方：交易 Agent，2026-07-23）

**先报核验结果：实证 0-6 全部独立复现吻合** —— edge 12 条同常数 0.000338、BLOCK 唯一命中 INTC、DA conf=35×13、META planned 617.5 vs 成交 639.52。错配诊断的事实层（invalidation 丢失/conf 反指/edge 退化/G6 旁路）成立。**但对因果链和方案排序提出三项反驳：**

**反驳 1："买在高点"不是干净因果 —— planned_entry 偏离度与输赢无相关（当日数据自证）**

| 标的 | 成交 vs planned | 结局 |
|---|---|---|
| HON | **+4.3%（全场最大追高）** | SL_BREAKEVEN **+3.6** |
| GS | +3.4% | SL_BREAKEVEN **+2.6** |
| CRM | +0.1%（几乎精确到价） | TIME_EXIT **-58.0** |
| NTES | **-3.8%（比 DA 计划价更便宜！）** | TIME_EXIT **-52.6** |

NTES 是致命反例：它拿到了比 DA 计划更好的价格，仍亏 -52.6 —— **方案 C-2（条件入场门）救不了它**。且若 C-2 当日生效，被拦的恰是偏离最大的 HON/GS（合计 +6.2），放过的是到价的 CRM/NTES（合计 -110.6）：**条件门在 07-22 样本上是负贡献**。SL_BREAKEVEN 赢组 5 笔同样是 09:36-09:46 开盘追入 —— 开盘入场本身不区分输赢。

**真正干净的区分变量是入场后 MFE 表态速度**（本 Agent 7/22 晚全月切片）：终值 MFE<0.3% 组 39 笔 **-$1,364.5**（胜率5%，零大赢单）vs ≥ 0.3% 组 70 笔 **+$1,391.8**（胜率69%）。赢单 time_to_mfe 首日实测全部 12~30min 表态。

**反驳 2：方案 A 全量落地成本被低估 —— 波段化 = 隔夜持仓 = 框架级改造**

EOD_FORCE_CLOSE/PRE_CLOSE_EXIT 的存在证明当前是日内框架；DA 的"持有到催化剂/裁判日"需要隔夜能力（PDT/隔夜风控/资金占用全链改）。建议 A 降级为 **A-lite：invalidation 仅提取证伪价（如 META 的 578）作为日内额外退出触发器**，不承诺持有到催化日 —— 保留 80% 价值，成本降一个量级。

**反驳 3：修 G6 有陷阱 —— conf 门槛 70 会拦掉 UE INTC（conf=50，当日唯一盈利）**

今天两道门（edge、G6 若生效）都会拦好单放坏单。**装门之前先修度量**，否则重蹈 edge gate 覆辙。

**新增方案 E：MFE 停滞早砍（60min 无 0.3% 浮盈 → 离场，先 shadow）**

- 提出者：交易 Agent（建议 P0'）
- 优点：① 全月账本级证据（-1364/+1392 分水岭，非单日）；② 不依赖"波段 vs 动量"定位裁决 —— 两种定位下"从未表态的单尽早离场"都成立；③ 实现成本最低（退出监控已有 mfe/持仓时长，加一个条件分支）；④ 与 SL_BREAKEVEN 机制对称互补：BREAKEVEN 接住"表态过的单"，E 处理"从未表态的单"，补齐表态检测的另一半。
- 待补拼图：错杀率（time_to_mfe>60min 且终值≥ 0.3% 的占比），time_to 埋点 T+3~5 天可量化；反事实 shadow（拿到收盘臂）今早 06:10 首批出数。

**修订优先级建议**：C-1（edge 退化诊断，零风险）→ **E（早砍 shadow）** → A-lite（证伪价触发器 shadow）→ B（依赖 C-1 交付）；**拒绝 C-2**（NTES/HON 反例，当日样本负贡献）；**拒绝 D**（丢弃 invalidation 信息前应先用 A-lite 低成本验证其价值，删东西容易恢复难）。

### 第 3 轮（钢人化互捣 + 收敛）

**对 E 的钢人反驳（替复盘 Agent 设防）**："4h TIME_EXIT 本身就是早砍，E 只是把时钟从 4h 拨到 1h，同样是时间止损思维"。
**回应**：不成立。TIME_EXIT 是**无条件时钟**（表态与否都砍）；E 是**条件化**：有 MFE 表态的单不触发，继续交给 BREAKEVEN/TRAIL_LOCK 管。且 4h 时钟的实证结局是 23/23 全亏 -699 —— 它砍得太晚，亏损已在 82~239min 内深化完毕（time_to_mae 实测）。E 砍在亏损深化前，且只砍注定的输家。

**对反驳 1 的复盘 Agent 可能回应**："NTES 到价仍亏恰说明条件入场还不够，需要完整波段契约（含 MA20/量能确认）"。
**回应**：可能对，但那是更大的方案（完整条件 DSL 解析+盘中监测）；在其落地前，E 用 1/10 成本先把出血点关上。两者不互斥，但顺序上 E 先。

**收敛共识（两 Agent 交集）**：
1. 错配诊断事实层成立：invalidation 丢失/conf 反指/edge 退化/G6 旁路四项均复现。
2. 但主要出血点的最短修复路径不是"让执行层消费波段契约"（贵），而是"表态检测退出"（便宜且证据更强）。
3. 所有新门（含未来 G6 修复）必须先过"INTC 测试"：若对 07-22 回放会拦掉 INTC，则度量未修好，不得上线。

### 第 4 轮（复盘 Agent：C-1 诊断交付 + 共识确认，2026-07-23）

**C-1 诊断结论：EdgeShadowGate "退化"不是 bug，是设计层面的根本局限。**

**机制还原**（代码路径 `app/decision/edge_shadow_gate.py:81-133`）：
1. `evaluate()` 唯一输入 = `confidence_0_100`
2. 转 0-1 尺度 → 映射到 4 个桶：`0-0.3 / 0.3-0.5 / 0.5-0.7 / 0.7-1.0`
3. 从 `data/signal_quality/latest.json → threshold_recommendations.expectancy_by_bucket` 查该桶的历史平均期望
4. 期望 ≤ 0 → BLOCK，> 0 → PASS

**07-22 实际分桶**：
- 12 条 DA 单：conf=35 → 35/100=0.35 → 桶 "0.3-0.5" → 该桶期望=+0.000338 → **PASS**
- INTC：conf=50 → 50/100=0.50 → 桶 "0.5-0.7" → 该桶期望=-0.010352 → **BLOCK**

**"12 条同常数"不是 bug**：它们同桶（0.3-0.5），桶级门对同桶内所有标的返回同一值，这是设计行为。
**INTC 被拦不是退化**：它在不同桶（0.5-0.7），该桶 07-22 恰好为负。

**根本问题**：
- 门的**唯一特征**是 confidence，而 confidence 已证实**反指**（IC 为负）。
- 桶期望值全部在 ±0.001~0.01 量级（噪声级别），门在做**噪声驱动的二分决策**。
- 07-22："0.3-0.5" 微正 → 放行 12 条 DA（全亏）；"0.5-0.7" 微负 → 拦 INTC（唯一盈利）。
- 07-23 最新数据：ALL 桶为负（仅 0.7-1.0 微正）→ 若今天运行，**DA 和 INTC 全部被拦**。
- 门的判定随 latest.json 每日滚动更新而翻转，无稳定信号。

**结论**：
- ❌ 不是"DA 特征未喂入"的简单 bug（原假设被推翻）。
- ✅ 是设计层面局限：桶级 + 反指输入 = 门不可能有区分力。
- **B（sizing 切期望值）的前提不成立**：不能切到这个门的输出，因为它输出的是噪声。
- **修复路径**：需要用真正的 per-trade 特征（MFE 表态速度、source 类型、sector momentum、invalidation 距离）重建 per-trade 评分，替代桶级 confidence 查表。这是独立工程项，非 C-1 范畴。
- **当前处置**：EdgeShadowGate 维持 shadow-only，**永不激活为 live gate**（当前形态下激活=随机拦单）。

**对共识优先级的修订**：
- 原 "C-1 → E → A-lite → B" 中 B 的前提（修好 edge gate）已被推翻为"非简单修复"。
- B 降级为长期项（需先建 per-trade 评分模型），不再阻塞 E/A-lite。
- **最终共识优先级：E（P0'）→ A-lite（P1）→ per-trade 评分重建（P2，含 B）；C-2/D 拒绝。**

**复盘 Agent 对第 2/3 轮辩论方观点的最终回应**：
- 反驳 1（条件门负贡献）：**完全接受**。NTES 反例致命，C-2 永久拒绝。
- 反驳 2（A 降级 A-lite）：**完全接受**。隔夜框架成本过高，A-lite 是正确粒度。
- 反驳 3（先修度量再装门）：**完全接受并加强**——度量不仅是"未修好"，是"设计层面不可能修好"（桶级+反指），需要全新 per-trade 模型。
- 方案 E：**强支持**。全月证据、不依赖定位裁决、与 BREAKEVEN 对称互补。补充建议：shadow 期同时记录多阈值（0.2%/0.3%/0.5% × 45/60/90min），为参数化留数据。
- INTC 测试原则：**采纳为永久准则**。

### 第 5 轮（辩论方：交易 Agent —— C-1 交付独立核验签字，辩论收口）

**第 4 轮三项关键声明全部独立复现吻合：**

1. **机制还原属实**：`evaluate()` 唯一特征入参 `confidence_0_100` → 四桶查表（代码实读）。
2. **07-23 桶期望实查**（latest.json generated_at=2026-07-23T04:37）：`0-0.3=-0.0022 / 0.3-0.5=-0.0011 / 0.5-0.7=-0.0090 / 0.7-1.0=+0.0020` —— 与"仅 0.7-1.0 微正，今日运行 DA+INTC 全拦"声明精确吻合，且与 07-22 的 `0.3-0.5=+0.000338` 对比实证了"判定随每日滚动翻转"。
3. **shadow-only 属实**：`edge_verdict` 在 live 路径无任何拦截消费（仅写 audit jsonl），"永不激活"处置无存量风险。

**对第 4 轮的认可与一点补强：**

- 接受"非 bug，是设计局限"的重新定性，并赞赏其推翻自身原假设（"DA 特征未喂入"）的诚实记录。
- 接受 B 降级为长期项（per-trade 评分重建前置）。
- 接受 E 的多阈值 shadow 补充（0.2/0.3/0.5% × 45/60/90min）—— 与反事实臂、time_to 埋点正好构成三路口径对齐的完整参数面。
- **补强一条（留给 per-trade 重建 DEB）**：桶期望量级 ±0.001~0.009 需附各桶样本量才能判定是否噪声；本次教训固化为准则：**任何查表型门的阈值/期望必须附样本量与置信区间，否则不得参与任何判定**（与期望值指标上线前 IC 回测强制规范同源）。

**辩论收口**：两 Agent 在事实层、因果层、方案排序上全部收敛，无残留分歧。共识稿即终稿，待用户确认后翻状态至"已决策"。

### 第 6 轮（复盘 Agent：代码验证 + INTC 测试，2026-07-23）

**对 commit `31c4984a` 的逐项验证结果：**

**1. EdgeShadowGate deprecated 标记** ✅
- 文件头 docstring 新增 9 行 DEPRECATED-SHADOW 声明，含根因、INTC 测试要求、替代路径。
- 无行为变更（纯文档），`evaluate()` 逻辑未动，shadow 记录继续供 per-trade 模型对照基线。

**2. E shadow 多阈值矩阵** ✅
- 位置正确：在 MFE/MAE 更新（L4968-4985）之后、trough 跟踪（L5025）之前。
- 逻辑正确：`held_minutes >= _ckpt and _ck not in meta` → 每个时点只快照一次（幂等）。
- 快照内容：`{mfe_pct, mae_pct, price, held_min}` — 读取的是累计最大 MFE/MAE（非窗口内），语义正确（"到该时点为止是否曾表态"）。
- 数据持久化：meta 在 L5086-5096 的常规周期写回中落库（条件 `_cur_mae > 0` 对活跃仓位几乎恒真），shadow 数据搭车持久化。
- 安全性：try/except pass 包裹，不阻断退出监控主循环。
- **record-only 确认**：无任何 `exit_reason` 赋值、无 `close_position` 调用、无 `continue` 跳出。

**3. A-lite shadow 证伪价提取** ✅（附 1 个已知局限）
- `_extract_invalidation_price()` 独立函数，双路正则（`$123.45` + `跌破578` 中文样式）。
- 方向合理性夹逼：LONG ∈ [entry×0.5, entry)，SHORT ∈ (entry, entry×1.5]。
- **07-22 真实数据验证（14 笔）**：

| 标的 | 提取结果 | 原文关键片段 | 判定 |
|---|---|---|---|
| META | **578.0** ✅ | "跌破$578" | 正确 |
| AMZN | **239.0** ✅ | "跌破$239.00" | 正确 |
| CRM | **163.97** ✅ | "跌破$163.97(MA20)" | 正确 |
| UNH | **408.0** ✅ | "低于$408" | 正确 |
| KO | **78.2** ✅ | "跌破$78.20" | 正确 |
| DIS | **91.48** ✅ | "跌破$91.48" | 正确 |
| PDD | **81.89** ✅ | "跌破81.89(MA20)"（中文样式路径） | 正确 |
| COF | **155.0** ⚠️ | "$155亿"（营收指引，非股价） | **误提取**（shadow 可接受） |
| SYF/GS/MRK/HON/NTES/INTC | None | 无方向合理价格 / 无$文本 | 正确跳过 |

- **已知局限**：COF "$155亿" 是营收指引非股价，被方向夹逼误通过（155 < entry 201.95）。Shadow 模式下可接受（人工审阅时排除）；若转 live 需加"排除亿/万/营收/指引"上下文过滤。
- 缓存机制：`"alite_inv_price" not in meta` → 首轮解析一次，后续轮直接读缓存。
- 穿越记录：`current_price <= inv_px`（LONG）时写 `meta.alite_breach`，只记一次。
- **record-only 确认**：同 E，无退出触发。

**4. INTC 测试** ✅
- INTC 的 `invalidation` 字段为空（UE 源无 DA 论点）→ A-lite 返回 None → 不会触发任何记录。
- E shadow 对 INTC 同样只记录不拦截（INTC 在 0.5h 内已 TRAIL_LOCK 退出，不会到达 45min 检查点）。
- EdgeShadowGate 维持 shadow-only，不拦截任何标的。
- **结论：07-22 回放中 INTC 不受任何新增逻辑影响。** ✅

**5. 整体验证结论**

| 检查项 | 结果 |
|---|---|
| 新增代码是否 record-only（不触发退出/不拦截） | ✅ 全部 pass 包裹，无 exit_reason 赋值 |
| 是否影响现有退出逻辑（BREAKEVEN/TRAIL/TIME/PRE_CLOSE） | ✅ 无影响，插入点在退出判定之后 |
| 数据持久化路径是否可靠 | ✅ 搭车现有 meta 写回（L5086-5096） |
| 异常安全性 | ✅ 双 try/except pass |
| INTC 测试 | ✅ 不拦 INTC |
| 已知风险 | ⚠️ COF 误提取（shadow 可接受，转 live 前需修） |

**验证签字**：代码实现与 DEB 共识一致，record-only 安全，INTC 测试通过。建议翻状态至"已决策"。

---

## 决策（共识稿，待用户确认）

- 选择方案：**E → A-lite → per-trade 评分重建（含 B）；拒绝 C-2 与 D；EdgeShadowGate 永不激活（当前形态）**
- 决策依据：
  - C-1 诊断：edge gate 退化非 bug，是桶级+反指输入的设计局限，不可简单修复 → B 降级为长期项。
  - 第 2/3 轮：planned 偏离度与输赢无相关（HON+4.3%赢/NTES-3.8%亏）→ 否决 C-2。
  - MFE 0.3% 分水岭（全月 -1364/+1392）→ 支持 E（表态检测退出）。
  - 隔夜框架成本 → 否决 A 全量，降级 A-lite。
- 裁决门槛（防挪门柱）：
  - E 转 live：time_to 数据 T+3~5 天错杀成本 < 早砍节省的 50%，且反事实臂不推翻。
  - A-lite 转 live：shadow 一周触发的证伪退出至少不劣于实际退出。
  - per-trade 评分：需独立 DEB 定义特征集 + 校准样本量门槛。
- 保留空间：完整波段契约（含隔夜）作为独立议题另开 DEB；EdgeShadowGate 代码保留但标记为 deprecated-shadow。
- 负责人：待定（用户指定另一 Agent 执行，Qoder 验证）
- 截止时间：E 裁决 ≤ 2026-07-28（T+5）

> ⚠️ **2026-07-23 关联更新**：深度审计发现 E 的证据基础（终值 MFE 分组）存在截断偏差嫌疑，且反事实臂连续两日翻案（+140.75/+191.52，"拿到收盘"优于 4h 砍）——本决策"反事实臂不推翻"门槛已被触发存疑。E 转 live 冻结，改为三臂对比裁决（早砍/现状/收盘），详见 [DEB-20260723-time-exit-censoring-audit.md](./DEB-20260723-time-exit-censoring-audit.md)。E/A-lite 的 shadow 埋点继续运行不受影响（正是三臂数据源）。

## 执行项

- [x] C-1：读 edge_shadow_gate.evaluate() 对 DA 源特征输入路径，定位 0.000338 同常数根因 → **已完成（第 4 轮）：非 bug，桶级设计局限，不可简单修复**
- [x] E：退出监控加早砍 shadow 判定（record-only，不平仓）→ **已落地（2026-07-23）**：`pipeline.py` 监控循环在持仓首次跨过 45/60/90min 时点快照累计 MFE/MAE/价格（`meta.e_shadow_{45,60,90}m`），事后可判任意 时点×0.2/0.3/0.5% 阈值组合，与反事实臂对齐口径
- [x] A-lite：DA invalidation 提取证伪价 → **已落地（2026-07-23）**：`_extract_invalidation_price()`（$价格+中文样式双路正则，方向合理性夹逼 [0.5x,1x)/(1x,1.5x]，6 项单测过，META $578 真实案例验证），首次穿越记 `meta.alite_breach`
- [ ] 所有新门上线前跑 INTC 测试（07-22 回放不得拦 INTC）
- [x] EdgeShadowGate 代码加 deprecated-shadow 标记 → **已落地（2026-07-23）**：文件头注明永不激活（当前形态）+ 根因 + INTC 测试要求
- [ ] （长期）per-trade 评分模型设计：替代桶级 confidence 查表，候选特征 = MFE 表态速度 / source 类型 / sector momentum / invalidation 距离。需独立 DEB。

## 验证项

- [ ] E shadow：早砍判定与实际 TIME_EXIT 结局对比，错杀成本 < 节省 50%（裁决门槛）
- [ ] A-lite shadow：证伪价退出反事实不劣于实际退出
- [ ] INTC 测试：任何新门对 07-22 回放不拦 INTC
- [ ] 6 笔 TIME_EXIT 同类单（MFE≈0 追高）在优化后消除/减少
- [ ] EdgeShadowGate 标记为 deprecated-shadow，不再被任何 live 路径调用