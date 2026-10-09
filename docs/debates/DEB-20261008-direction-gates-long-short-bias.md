# 议题：多空方向是否被「人为拦截限定」—— 六道方向闸普查、偏向溯源漏斗、与摘闸裁决

## 状态

- [x] 进行中（提案待裁）
- [ ] 待决策
- [ ] 已决策
- [ ] 已验证
- [ ] 已关闭

**状态头**

| 项 | 值 |
|---|---|
| 类型 | 辩论分析提案（闸普查 + 偏向归因 + 摘闸方案待裁；**本批零交易行为变更**） |
| 立案日 | 2026-10-08（问题起于 2026-10-07 盘前推送复查） |
| 提出者 | Hermes Agent（用户令「请确认今日 DA 产出是否正常 / UE 融合是否合理」→「多空不要人为拦截限定」→「请落 deb 文档」） |
| 待谁裁决 | **用户**（frontier F1–F5，见 §10） |
| 目标读者 | ①裁决者=用户（读 §1 结论 + §10 待裁 + §11 自曝）②复查者=第二 agent（读 §5 E 表，全部可独立复跑）③执行者（读 §8，仅在裁决后） |
| 交易行为变更 | **本提案落地部分零变更**。3 个只读普查脚本入库；F1–F5 全部待裁 |
| 已落码 | 3 个复算脚本入库 `scripts/deb20261008_*.py`（rc=0，无 scratch 依赖，任意 cwd + 无 PYTHONPATH 冷跑）；生产代码零改动 |
| 时效性 | 🔴 **G5（执行前组合偏斜门）已随服务 CST 23:10 重启实弹生效**，首次实际拦截 = 下一交易日开盘；F1 若采纳须在开盘前落地 |

---

## 1. 一句话目标与可判定子目标

**目标**：判定「系统是否在人为拦截多空方向」；若是，分清**哪几道闸**在拦、**拦掉的是谁**、以及**摘闸能否真正达成用户诉求**（多空双向、有空头产出）。

五条可判定子目标（每条都能用命令判真假）：

| # | 子目标 | 判定式 | 实测结论 |
|---|---|---|---|
| S1 | 系统里到底有几道方向性拦截闸？ | 逐个 grep 代码锚点实存性 | **6 道**（G1–G6），其中 5 道硬编码无 yaml 开关，仅 G3 可配置关闭 |
| S2 | 多头偏向是「闸造成的」还是「模型本来就只出多头」？ | 闸前 L1 vs 闸后 L3 的方向占比差 | **模型自己就 77.0% 多头**；闸在总量上只加 **+1.6pp**（77.0%→78.6%）⇒ **摘闸不能把 77% 变成 50/50** |
| S3 | 闸有没有造成「空头清零日」？ | 逐日比对「闸前有空头提案」与「闸后空头=0」 | **有，4 天**（9/21、9/22、10/02、10/07）⇒ 闸的破坏力集中在**空头稀缺日**，不是总量 |
| S4 | 这些闸拦掉的是好单还是坏单？ | 被拦行的 `fwd_return` 填充率 | **不可判**——被拦行填充率曾 **0.0%**（n=50, F4 回填前时点快照; 回填后 100% 见 §13），成交行 100.0%（103/103）⇒ 回填以成交为条件，被拦信号**结构性无结局** |
| S5 | 「空头交给 UE 不设限浮现」这个设计假设兑现了吗？ | UE 通道实际产出方向 | **未兑现**——UE 20 日 LONG=11 / SHORT=6，日均空头 **0.4 条**；10/07 当日 UE 的 SHORT 提案（ALKT）**未落库**，当日有效 SHORT=0 |

**头条结论**：用户诉求「多空不要人为拦截限定」**方向成立且证据充分**（4 个空头清零日 + G1 结构性只拦多头 + G2 净压空头存活率 66.1% vs 78.7%）。但**摘闸是必要非充分**——77% 的多头基本盘来自**模型自身提案分布**，闸只贡献 1.6pp。若只摘闸不动上游，结果仍是多头为主。另有一个**决定性数据缺口**（S4）：现有 DB 无法证明这些闸拦对了还是拦错了，所以摘闸目前只能靠方向裁决，**拿不到回测背书**。

---

## 2. 背景与触发事件

### 2.1 触发：2026-10-07 盘前推送与 DB 不一致

用户贴出当日推送（5 条指令：PENG/CEG/NOK/BKV/MRVL，全 BUY LONG，`0股 | 风险 $0`），问是否正常、UE 融合是否合理。复查发现：

| 观测 | 真值 |
|---|---|
| 推送显示 5 条指令 | DB 最终**只有 2 条**非 skip（CEG conf=35 / BKV conf=50） |
| 推送显示 PENG/NOK/MRVL（conf=70） | 三条**已被降级为 SKIP** |
| `0股 \| 风险 $0` | **正常渲染**：PENDING 态 `position_size` 恒 NULL，下单（BOOKED）时才由风险平价填入 |
| 全 5 条 BUY LONG | 当日 3 个做空候选（ITUB/CIEN/ALAB）**全被拦**，最终有效 SHORT=0 |

### 2.2 时序还原（推送 ≠ DB 的根因）

| 时刻（ET） | 事件 | 证据 |
|---|---|---|
| 08:18–08:27 | DA 深度分析 11 标的，产出 5 条直写指令 | `da_checkpoint/2026-10-07` 22 文件 / 11 标的；gate jsonl 时间戳 |
| 08:55（CST 20:55） | **bridge 推送**（crontab `55 20 * * 1-5`） | `logs/da_bridge_dingtalk.log` |
| 09:05:57 | builder 批 + **G4/G6 组合层闸**实跑，砍掉 PENG/NOK/MRVL | `skew_gate_vision.jsonl` ts=`2026-10-07T13:05:57Z`；被砍行 `updated_at=2026-10-07T20:00:19Z` |

⇒ **推送早于砍单约 10 分钟**，且推送读的是 `opportunity_scan_{date}.json` 快照而非 DB 当前态 ⇒ 用户收到的必然与最终 DB 不一致。**这是结构性时序缺陷，非当日偶发。**

（Codex 已于 `f9d4de35a` 部分修复：推送头新增「DB可交易 M/K | guardrail SKIP J」口径拆分。但该修复的 `WHERE decision_id LIKE 'DA-%'` 过滤漏掉 UE 行——见 §7 P1-4。）

### 2.3 与本仓既有治理文档的关系

- `DEB-20260930-skew-gate-vision-gap.md`：已记录「guardrail 分母只含 builder 本批，DA 直写/UE/reentry 行绕过组合门」（9 月 active 279 条中 195 条绕门）
- `RFC-121-skew-gate-day-scope-preregistration.md`：2026-09-30 提案 day-scope 改法后**撤回**（WITHDRAWN），2026-10-07 由 Codex 追加实施记录，改在**执行前统一收口**（= G5）
- 本 DEB **不重复**上述议题，只回答用户新问题：「方向闸一共有几道、偏向从哪来、摘哪道」

---

## 3. 六道方向闸全清单（E1 实测锚点）

| ID | 闸名 | 类型 | 判据 | 代码锚点 | yaml 开关 | 实测拦截量（近15交易日） |
|---|---|---|---|---|---|---|
| **G1** | DA 破位硬买拦截 | 硬 SKIP | `scanner_direction=="bearish" and direction=="BUY"` | `skill signal_gate.py:428` | ❌无 | **36 次，全 BUY** |
| **G2** | DA 方向冲突降档 | soft：−10 分 + tier 降 2 档 | `dir_conflict and tier in ("优","良","中")` | `skill signal_gate.py:419` | ❌无 | **305 次命中**（BUY 188 / SKIP 61 / SHORT 56） |
| **G3** | UE 动量门 | 硬 SKIP | 空头排列 + `direction=="LONG"` | `scripts/llm_universe_scan.py:588` | ✅`universe_expansion.long_momentum_gate_enabled` | 影子 **n=9**（passed 6 / blocked 3）⇒ 样本不足 |
| **G4** | 生成期方向偏斜门 | 降级 SKIP | builder 本批 `majority_pct > 0.80` → 砍低 conf | `app/playbook/pipeline.py:2019,2179` | ❌常量 `max_direction_skew=0.80` | **43 次** |
| **G5** | 执行前组合偏斜门 | 拒绝执行 + 写 SKIP | 当日全域 `majority_pct > 0.80` | `app/playbook/portfolio_execution_guardrail.py` | ❌常量 `MAX_DIRECTION_SKEW=0.80` | **0 次**（🔴至今零实弹拦截，见 §4.3） |
| **G6** | 行业集中度门 | 降级 SKIP | sector 占比 `> 0.40` → 砍低 conf | `pipeline.py:2189` + `portfolio_execution_guardrail.py` | ❌常量 `MAX_SECTOR_PCT=0.40` | **7 次** |

**分层**：
- **DA 分析内部**（G1/G2）：在 skill 仓 `/root/.hermes/skills/deep-stock-analysis/scripts/signal_gate.py`，属「DA 扫描分析结果」的一部分
- **UE 侧**（G3）：在主仓 `scripts/llm_universe_scan.py`
- **组合层**（G4/G5/G6）：在主仓 `app/playbook/`，在 DA/UE **已定方向之后**二次改写方向

---

## 4. 关键发现（每条带反例判据）

### 4.1 🔴 多头偏向的主因是**模型自身提案分布**，不是闸

```
L1 闸前模型原始提案:  BUY 188 / SHORT 56        ⇒ 多头 77.0%
L2 过完 DA 两道闸:    BUY 148 / SHORT 37        ⇒ 多头 80.0%
L3 过完组合层闸:      BUY  81 / SHORT 22        ⇒ 多头 78.6%
```

**闸在总量上只贡献 +1.6pp**（77.0% → 78.6%）。

> **反例判据（什么输出说明我错）**：若 L1 多头占比 ≈ 50% 而 L3 ≈ 78.6%，则偏向主因是闸，摘闸有效。实测 L1 已 77.0% ⇒ 不成立。
> 复跑：`venv/bin/python scripts/deb20261008_bias_funnel.py`（E5/E6 段）

### 4.2 🔴 但闸在「空头稀缺日」把稀缺直接清成零

| 日期 | 闸前 B/S | 闸前多头% | 最终 B/S | 最终多头% | 空头清零 |
|---|---|---|---|---|---|
| 2026-09-21 | 14/1 | 93% | 7/**0** | 100% | ⚠️是 |
| 2026-09-22 | 7/2 | 78% | 3/**0** | 100% | ⚠️是 |
| 2026-10-02 | 14/3 | 82% | 5/**0** | 100% | ⚠️是 |
| **2026-10-07** | 6/2 | 75% | 2/**0** | 100% | ⚠️是 |

15 个交易日中 **4 天**「闸前明明有空头提案、闸后空头归零」。

> **反例判据**：若这 4 天闸前 SHORT 提案数=0，则清零与闸无关。实测 4 天闸前分别有 1/2/3/2 条 SHORT 提案 ⇒ 清零由闸造成。

### 4.3 🔴 自我推翻：今日 5→2 **不是** Codex 新增的 G5 砍的

我在口头汇报中曾称「执行前组合偏斜门（Codex 今日新增）今天把 5 条砍到 2 条」。**实测推翻**：

| 核验项 | 实测 |
|---|---|
| 今日被砍 3 条的 `skip_reason` | `方向偏斜超限 guardrail 降级 (多数方 BUY, 原size=88/564)` + `sector 集中度 guardrail 降级 (Semiconductors)` ⇒ **G4/G6 旧门措辞** |
| G5 专属文案「组合方向偏斜 guardrail 拒绝执行」全库计数 | **n = 0** |
| G5 审计留痕 `data/shadow/portfolio_execution_guardrail.jsonl` | **不存在** |
| G5 提交时刻 / 服务加载时刻 | 提交 CST 22:58:35 / 加载 CST 23:10:46 |
| G5 加载后是否有执行轮次 | 有 5 次 `auto_execute`（ET 15:47–15:59），但 68 条中 2 BOOKED + 66 SKIP ⇒ **无可执行指令到达该门** |
| `execute_instruction` 全库日志调用次数 | **0** |

⇒ **今日 5→2 由 G4（生成期偏斜）+ G6（行业集中度）造成，G5 至今零实弹拦截**，其首次实际检验 = 下一交易日开盘。按 Gate 6 时段依赖埋点三态纪律，G5 当前状态为 **TOO_EARLY（不可判）**，不得称其「已生效」或「接线死」。

> **反例判据**：若 G5 审计文件存在且有行，或被砍行 skip_reason 含「组合方向偏斜」，则 G5 已实弹。实测两项皆否 ⇒ 归因更正成立。
> 复跑：`venv/bin/python scripts/deb20261008_gate_attribution.py`（④⑥ 段）

### 4.4 🔴 G5 存在**未覆盖的断崖副作用**（明日开盘即生效）

G5 判据 `majority_pct > 0.80` 配合 `while len(active) > 1`，在**全域 0 空头**时 `majority_pct` 恒 = 1.0 > 0.80 ⇒ 一路砍到只剩 1 条。

重放实测（还原今日 5 条为全 PENDING）：

| 场景 | 构成 | 放行 |
|---|---|---|
| A（今日同型） | 5 条全 LONG / 0 空头 | **1 条**（砍 4） |
| B | 4B / 1S = 80% | **5 条全放行** |

**少 1 条空头 = 从放行全部塌到只放 1 条**，是断崖不是斜坡。同向条数 N=2..8 重放均恒留 1 条。

RFC-121 明写「不调整 80%/40% 阈值，也不给 confidence 豁免」⇒ 是设计意图，但**文档未写这个后果**，且 Codex 6 个测试的 `direction_counts` 全是 `{BUY:5, SELL_SHORT:1}` / `{BUY:2, SELL_SHORT:1}`，**0 空头日零覆盖**（关键词 `0.80`/`max_direction_skew`/`while` 在测试文件中 0 次匹配）。近 15 交易日 0 空头日 = 2 天（13%）。

> **反例判据**：若测试文件含 0 空头日用例，或重放场景 A 放行 >1 条，则断崖不存在。实测两项皆否。

### 4.5 🟡 两道 DA 侧闸方向**相反**，不可合并叙述

| 闸 | 拦谁 | 实测 |
|---|---|---|
| G1 破位硬买 | **只拦 BUY**（判据结构决定） | 36 次全 BUY |
| G2 方向冲突降档 | 双向，但**净压空头** | SHORT 存活率 **66.1%**（37/56）vs BUY **78.7%**（148/188） |

⇒ 「系统只压多头」的说法**不成立**：G1 压多头、G2 压空头。我在口头汇报中曾把 G1 单独特征概括为全局，已在本节更正。

### 4.6 🟡 G1 的设计假设「空头交给 UE」未兑现

`signal_gate.py:426` 注释原文：「DA 定位优质多头选股器，遇到『动量已明确转空』应观望等回稳…做空机会交由 UE 不设限浮现负责。」

实测 UE 通道近 20 日：LONG=11 / SHORT=6（空头占比 35%），**日均空头 0.4 条**；2026-10-07 当日 UE 浮现的 SHORT（ALKT conf=0.50）与 LONG（RARE）**均未落库** ⇒ 当日有效 SHORT=0。

⇒ DA 放弃的空头，UE 没有接住。**这是「纯单向多头」的结构性根因，与闸无关。**

### 4.7 🔴 数据缺口：无法回测证明闸的好坏

| 人群 | n | `fwd_return_intraday` 有值 | 填充率 |
|---|---|---|---|
| 被组合层闸拦掉的行 | 50 | **0** | **0.0%**（F4 回填前时点快照, 回填后 100% 见 §13） |
| 未拦可交易行 | 103 | 103 | 100.0% |

回填以成交为条件（`executed=1` 组 103/103（`fwd_return_intraday`）；`triggered=0` 组仅 18/939），本地无 K 线表（`cb_shadow_daily` n=29，非价格源）。

⇒ **「这些闸拦掉的是好单还是坏单」在现有 DB 里不可判**（2026-10-08 F4 已解决: 回填后填充率 100%, 首读见 §13）。摘闸决定目前只能靠方向裁决，拿不到回测背书。这一条必须写给裁决者，不得粉饰。

---

## 5. E 表（可执行核验，每行可独立复跑）

复跑前提：`cd /opt/ftseek/FTSeek-US`，用 `venv/bin/python`；三个脚本均**只读**、无 scratch 依赖、任意 cwd + 无 PYTHONPATH 冷跑 rc=0。

| # | 核验项 | 命令 | 预期关键输出 |
|---|---|---|---|
| E1 | 六道闸代码锚点实存性（G1–G6 逐个给行号） | `venv/bin/python scripts/deb20261008_direction_gate_census.py` | `G1 … ✅ signal_gate.py:428` / `G4 … ✅ pipeline.py:2114` / `G5 … ✅ portfolio_execution_guardrail.py:11` / `G6 … ✅ portfolio_execution_guardrail.py:12`（6 行全 ✅） |
| E2 | 各闸实测拦截量 | 同上（E2 段） | `G4 生成期偏斜 43 次` / `G5 执行前偏斜 0 次` / `G6 行业集中度 7 次` / `G1 破位硬买 (硬BLOCK) 36 次 原方向分布={'BUY': 36}` / `G3 … n=9 … ⚠️ n<20` |
| E3 | 下游消费者（Guard-First）+ 开关可得性 | 同上（E3 段） | `skew_gate_vision.jsonl … 消费者 n=4` / `ue_long_momentum_gate.jsonl … n=6` / `G3 long_momentum_gate_enabled yaml开关=✅有` / `G4/G5/G6 yaml开关=❌无` |
| E4 | 今日 5→2 归因（G4/G6 vs G5） | `venv/bin/python scripts/deb20261008_gate_attribution.py` | `skip_reason='方向偏斜超限 guardrail 降级 (多数方 BUY, 原size=88)'`（旧门措辞）/ `G5 专属文案…全库 n = 0` / `审计留痕…不存在` / `🔴 G5 至今零实弹拦截` |
| E5 | L1 闸前模型提案方向占比 | `venv/bin/python scripts/deb20261008_bias_funnel.py` | `L1 闸前原始提案 n=305: {'BUY': 188, 'SKIP': 61, 'SELL_SHORT': 56}` / `模型自己提的多头占比 = 77.0%` |
| E6 | 三层漏斗 + 存活率对比 | 同上（E6 段 + E5 尾） | `L1 77.0% / L2 80.0% / L3 78.6%`；`BUY 存活率 = 148/188 = 78.7%`；`SHORT 存活率 = 37/56 = 66.1%` |
| E7 | 逐日漏斗 + 空头清零日 | 同上（E7 段） | `「闸前有空头提案、闸后空头归零」的日子 = 4 天: ['2026-09-21', '2026-09-22', '2026-10-02', '2026-10-07']` |
| E8 | 被拦行事后结局填充率（数据缺口） | 同上（E8 段） | `被组合层闸拦掉的行: n=50 fwd_return_intraday 有值=0` / `对照 (未拦可交易行): n=103 … 有值=103` / `0.0% vs 100.0%` |
| E9 | G5 断崖重放（0 空头日恒留 1 条） | 见 §9 C4（scratch 重放，未入库） | 场景 A `存活 1` / 场景 B `存活 5`；N=2..8 同向均 `放行 1` |
| E10 | G5 加载时刻 vs 收盘 + 执行轮次 | `deb20261008_gate_attribution.py`（⑤⑥ 段） | `服务启动: ActiveEnterTimestamp=Wed 2026-10-07 23:10:46 CST` / `auto_execute 开始: 68 条指令` ×5 / `BOOKED n=2, SKIP n=66` |
| E11 | UE 通道实际空头产出 | `deb20261008_bias_funnel.py`（E7 尾） | `UE 融合落库 LONG=11 SHORT=6 ⇒ 空头占比 35.3%` / `日均空头 0.40 条` |
| E12 | 今日推送 ≠ DB 的时序证据 | `grep -c '非当前通道候选' …` 见 §9 C6 | builder 批 63 行 skip_reason=`非当前通道候选(bridge未提名/无方向)`；bridge crontab `55 20 * * 1-5` |
| E13 | **本 DEB 全部数字的 claim↔实测自动对账 + 结构自检**（文档/数据任一漂移即红） | `venv/bin/python scripts/deb20261008_verify_claims.py` | 7 个 claim 全 `✅`（C1 L3=78.6 / C2 +1.6pp / C3 35.3 / C4 5条 / C5 0.0 / C6 100.0 / C7 4天）+ `表格列数不一致行: 0` + `引用不存在章节: (无)` ⇒ `✅ claim ↔ 实测全部一致` 且 **rc=0**。反向能力已验：人为把 `+1.6pp` 改成 `+9.9pp` ⇒ `🔴 C2 … 文档=9.9 实测=1.6` 且 **rc=1** |

---

## 6. 方案对比（Gate 2）

| 方案 | 内容 | 优点 | 缺点 | 判定 |
|---|---|---|---|---|
| **A（不做）** | 维持现状 | 零风险 | 4 天空头清零持续；G5 断崖明日开盘即生效；违反用户 8/16「禁纯单向多头」 | ❌ 否决 |
| **B（只摘组合层 G4/G6）** | 删/关生成期偏斜门 + 行业集中度门 | 直接消除 4 个空头清零日的成因；G4/G6 无测试纠纷 | 不动 G5 ⇒ 明日断崖仍在；不解决 77% 基本盘 | 🟡 不完整 |
| **C（摘 G4/G5/G6 + 对称化 G2）** | 组合层三闸全摘，G2 降档改为纯记录 | 最贴合用户「不人为拦截」；同时修掉断崖 | G5 是 Codex 刚提交、6 测试钉住 ⇒ 需同步改测试 + 跨 agent 协调；G1 未动 | ✅ **推荐** |
| **D（C + 摘 G1）** | 连「空头排列不硬买」也放开 | 完全尊重 LLM 方向 | 放弃防接飞刀，与用户 8/16「耐心不硬扛」张力 | ⚠️ 需用户明确表态 |
| **E（全摘 G1–G6）** | 系统内不再有任何方向性拦截 | 最彻底 | G3 有 yaml 开关可直接关；G2 属 DA 分析内部，摘了等于改 DA 语义；风险面最大 | ❌ 过度 |
| **F（只补数据不改闸）** | 先给被拦行回填 `fwd_return`，攒样后回测裁决 | 零交易行为变更；解决 S4 数据缺口 | 不解决 4 天空头清零与明日断崖；用户诉求延后 | 🟡 应作为**配套**而非替代 |

**推荐 C + F 配套**，理由：
1. C 精确对应用户诉求（组合层是「DA/UE 已定方向之后的人为二次改写」）
2. F 补上 S4 缺口，使「闸拦对没拦对」在下一次变成可回测问题
3. G1（D）单独留给用户表态——它不对称但防接飞刀，属价值判断非事实判断
4. G2 建议**降为纯记录**而非删除：它在 DA 分析内部，属「扫描分析结果」的一部分，但降 2 档的实际效果是硬拦（净压空头 66.1%）

---

## 7. 复查 Codex 两提交的遗留问题（本 DEB 范围外但需登记）

Codex `f9d4de35a` + `10adea0c8` 精准修对了两个真问题（推送口径不一致 + guardrail 绕门/反向选择），工程质量高、测试规范、未碰 KR1。遗留：

| 级别 | 问题 | 实测证据 |
|---|---|---|
| 🔴 P0-1 | G5 在 0 空头日恒留 1 条（断崖），测试零覆盖 | §4.4；6 个测试 `direction_counts` 全含 SELL_SHORT≥1 |
| 🔴 P0-2 | G5 fail-close：DB 读失败 ⇒ 拒绝**所有**单 | 注入 `database is locked` → `allowed=False, reason='[PORTFOLIO_GUARDRAIL_FAIL_CLOSE]…'`；有测试钉住（有意设计），但影响面从生成期挪到执行路径未评估 |
| 🟡 P1-3 | 双门叠加：G4（生成期，批内分母）未移除，G5（执行期，全域分母）追加 | `pipeline.py:2019` 仍在；两门分母不同 ⇒ 重复设防 |
| 🟡 P1-4 | 推送口径漏计 UE 行 | `_instruction_summary` 用 `WHERE decision_id LIKE 'DA-%'`，UE 前缀是 `universe_expansion:` ⇒ 近 14 日漏计 **9 条可交易行，其中 5 条 SELL_SHORT**（CORT/EPC/ARM/TDOC/MDB；另 4 条 BUY=ALK/QCOM/STX/WDC） |

（P1-4 恰好漏掉的是空头 ⇒ 与用户诉求直接冲突，建议与 F1 同批修。）

---

## 8. 实施步骤（仅在用户裁决后执行）

**F1 摘组合层三闸（G4/G5/G6）**
1. `pipeline.py:2019` — 移除 `_apply_portfolio_guardrails` 调用，或加 `portfolio_guardrail_enabled: false`（yaml，可一键回滚）
2. `portfolio_execution_guardrail.py` — 同上开关；`playbook_service.py:959` 调用点保留但短路
3. 同步改 Codex 6 个测试（`test_portfolio_execution_guardrail.py`）：从「断言砍单」改为「断言开关关闭时零砍单 + 开关打开时行为不变」
4. **保留影子采集**：`skew_gate_vision.jsonl` / `portfolio_execution_guardrail.jsonl` 照写（判据退役 ≠ 采集退役，Guard-First）
5. 跨 agent 协调：G5 属 Codex `10adea0c8`，改前在其 RFC-121 文档追加变更记录

**F2 对称化 G2（DA 方向冲突降档）**
- 改为纯记录：`dir_conflict` 只写 `risk_notes` 与影子，不扣分不降档；或双向都降（含 bullish+BUY）
- 位置在 skill 仓 ⇒ 需按 `multi-agent-project-collaboration` 的 Skill-Edit Impact Ledger 留 diff + 接受/拒绝台账

**F3 G1 破位硬买门（待用户表态）**
- 选项 a：保留（承认多头侧有一道保护）
- 选项 b：对称化（bullish+SELL_SHORT 也硬 SKIP）
- 选项 c：降为纯记录
- 任一选项都需同步改 `signal_gate.py:428` 与 3 个消费该文案的脚本 + 2 个测试

**F4 补被拦行事后结局回填（配套，零交易行为变更）**
- 定位现有回填逻辑，去掉「以成交为条件」，改为「凡有 entry_price + 方向即可回填」
- 验收：被拦行 `fwd_return_intraday` 填充率从 0.0% → ≥80%

**F5 修推送口径漏计（P1-4）**
- `da_bridge_dingtalk_push.py` 的 `_instruction_summary`：`LIKE 'DA-%'` → 去掉前缀过滤或补 `OR decision_id LIKE 'universe_expansion%'`
- 验收：`DB可交易 M/K` 与全量非 skip 行数一致

**部署**：改 `app/playbook/*` ⇒ HotReloader 穿不透 ⇒ **必须 `systemctl restart ftseek-us`**；重启前核未平仓 + 改动归属；重启后按 Gate 6 时段三态判定，不得以「重启成功 + 测试全绿」冒充生效。

---

## 9. C 表（复查者的反例判据）

| # | 我的结论 | 反例判据（什么输出说明我错） |
|---|---|---|
| C1 | 系统有 6 道方向闸，5 道无 yaml 开关 | E1 任一锚点报 🔴缺失，或某闸实为可配置关闭 |
| C2 | 多头偏向主因是模型自身（闸前已 77.0%） | E5 输出 L1 多头占比 < 60%，或 L1 SHORT > 100 |
| C3 | 闸造成 4 个空头清零日 | E7 输出的清零日 ≠ 4 天，或那些日子闸前 SHORT=0 |
| C4 | G5 在 0 空头日恒留 1 条 | 重放场景 A 放行 > 1 条；或 N=5 同向放行 > 1 |
| C5 | G5 至今零实弹拦截（今日 5→2 是 G4/G6） | E4 输出 G5 专属文案 n > 0，或审计文件存在 |
| C6 | 推送 ≠ DB 因推送早于砍单 ~10min | bridge 日志 mtime 早于 skew 影子 ts，或推送读的是 DB |
| C7 | G2 净压空头（存活率 66.1% < 78.7%） | E6 存活率对比反向，或 SHORT 提案 n=0 |
| C8 | 「空头交给 UE」未兑现 | E11 输出 UE 日均空头 ≥ 2 条，或 10-07 UE SHORT 已落库 |
| C9 | 被拦行结局不可判（填充率曾 0.0%, F4 后 100%） | E8 输出被拦行填充率 > 50%，或本地存在可用 K 线源 |
| C10 | G1 结构上只可能拦 BUY | `signal_gate.py:428` 判据含 SELL_SHORT 分支，或 gate jsonl 中 G1 命中过 SHORT |

**点名挑战 3 处**（复查者请优先打这三点）：
1. **C4 的重放是否可信**——我的重放脚本在 scratch（未入库），且第一版有浅拷贝 bug（`scen2 = [dict(x) for x in scen[:4]]` 复用了场景 A 已就地改成 SKIP 的行，导致「存活1+被砍0=1≠5」）。第二版从原始定义重建后才得到场景 B 全放行。**请独立重写重放，不要用我的脚本**。
2. **E8 的「本地无 K 线源」是否成立**——我只查了表名含 `kline/bar/price/quote/daily/ohlc` 的表与 `data/` 下同名文件。若 futu OpenD 可拉历史 K 线（`futuapi` skill），则 S4 缺口可当场补，「不可判」应降级为「未做」。
3. **§4.1 的 +1.6pp 是否掩盖了结构效应**——总量 1.6pp 很小，但 §4.2 的 4 个清零日是真实破坏。请判断我是否在用「总量小」淡化「结构伤害」，或反之用「4 天」夸大了一个 13% 频率的边缘情形。
4. **§4.7 的「不可判」是否只是我没做**——E8 谓词踩过一次坑（自曝 #5：按代码中文描述写 LIKE，漏掉实际文案是英文 `sector` 的 7 行）。请独立重写谓词（建议用 `SELECT DISTINCT skip_reason` 先枚举全部真值再决定匹配），确认「被拦行 n=50 / 填充率 0.0%」不是又一个口径遗漏；同时按 §9 挑战 #2 核实是否真的无法从 futu OpenD 拉历史 K 线补上结局。

---

## 10. 待裁（frontier）

| # | 事项 | 我的建议 | 需用户表态 |
|---|---|---|---|
| **F1** | 摘组合层三闸 G4/G5/G6 | ✅ 摘（含 yaml 开关 + 保留影子采集 + 同步改 Codex 测试） | 是否同意 |
| **F2** | G2 方向冲突降档 | ✅ 降为纯记录（它净压空头 66.1% vs 78.7%） | 是否同意；或改「双向对称降档」 |
| **F3** | G1 破位硬买门（只拦 BUY，n=36） | ⚠️ **需你定**：保留 / 对称化 / 降为纯记录 | 三选一 |
| **F4** | 补被拦行事后结局回填 | ✅ 做（零交易行为变更，解决 S4） | 是否同意 |
| **F5** | 修推送口径漏计 UE（漏 6 条空头） | ✅ 做（一行修复） | 是否同意 |
| **F6** | 上游 77% 多头基本盘 | 🟡 **另案**：这是 DA prompt / 选股器自我定位问题（代码注释自称「优质多头选股器」），不属本 DEB 范围 | 是否另开 DEB |

**时效提醒**：G5 已实弹加载，**明日开盘就会按新逻辑砍单**。若采纳 F1，建议在开盘前落地；否则明日可能再现「0 空头日只放行 1 条」。

---

## 11. 自曝 / 更正记录

**纪律**：被证伪的写成「部分证伪」（原说法 + 实测值 + 改成什么），不悄悄改题。本节 8 条：

- **#1/#2 是我本轮先说错、后被自己的数据推翻的实质结论**（G5 归因、两道闸方向相反）
- **#3/#4/#5 是脚本层 bug**（浅拷贝污染重放、锚点凭记忆写、LIKE 谓词按代码描述而非落库真值）
- **#6/#7/#8 是守卫层教训**（期望值硬编码⇒假红、自指检测三版、按日期 glob 卷入并行 agent 文件）

其中 **#5 与 #6 是同一类错误的两面**：先按「我以为的文案」写谓词漏了 7 行，再用「我以为的数字」硬编码守卫——两次都是把**假设当既成事实**。

| # | 我先说的 | 实测推翻 | 根因 | 处置 |
|---|---|---|---|---|
| 1 | 「执行前组合偏斜门（Codex 今日新增 G5）今天把 5 条砍到 2 条」 | G5 专属文案全库 **n=0**、审计文件**不存在**、`execute_instruction` 日志 **0 次调用**；今日被砍 3 条的 skip_reason 是**旧门措辞**「方向偏斜超限 guardrail 降级」/「sector 集中度 guardrail 降级」，`updated_at=2026-10-07T20:00:19Z` | 看到「Codex 刚提交了 guardrail」+「今日指令被砍」就做了因果关联，**没有核 skip_reason 文案归属**，也没查 G5 的审计产物是否存在。属「声称必核」纪律的自我违反 | §4.3 就地更正；G5 状态判为 **TOO_EARLY**（有 5 次 auto_execute 但 68 条中 2 BOOKED + 66 SKIP ⇒ 无可执行指令到达该门）；E4/E10 入库钉死；**明日开盘才是 G5 首次实弹检验** |
| 2 | 「破位硬买门只拦 BUY ⇒ 事实上的单向限流（只压多头）」 | G2 方向冲突降档**净压空头**：SHORT 存活率 **66.1%**（37/56）< BUY **78.7%**（148/188）；被 BLOCK 的 SHORT=19 > BUY=4 | 只看了 G1 一道的方向特征就概括全局，没做**全部闸的方向×判定交叉表**。G1 压多头、G2 压空头，两道方向相反 | §4.5 就地更正并显式标注「不可合并叙述」；E6 段把存活率对比印成脚本固定输出，防再犯 |
| 3 | 场景 B 重放「存活 1 + 被砍 0 = 1 ≠ 5」 | `scen2 = [dict(x) for x in scen[:4]]` 取的是**场景 A 已就地改成 SKIP 的** 4 条 ⇒ 4 条被 `continue` 跳过 | 浅拷贝 + 就地 mutate 的复用；重放脚本内部状态污染 | 改为从 `_ORIG5` 原始定义重建；修正后场景 B（4B/1S=80%）**5 条全放行**，与场景 A（0 空头）**只放 1 条**形成断崖对照。**该脚本仍在 scratch 未入库**，已在 §9 点名挑战 #1 要求复查者独立重写 |
| 4 | E1 中 G3 锚点串 `_LONG_GATE_REASON_PREFIX` | 实跑报 🔴缺失（该常量名不存在） | 凭记忆写锚点串，没先 grep 验证（MEMORY 铁律「假设值验证=自证陷阱」的自我违反） | 改为 `def _long_momentum_gate`（函数定义，必然实存），重跑 ✅ |
| 5 | E8 谓词 `skip_reason LIKE '%行业集中度%'` | 实测命中 **n=43**，与 E2 的 G4(43)+G6(7)=**50** 对不上 | G6 的**实际落库文案是英文 `sector 集中度 guardrail 降级`**，不含「行业集中度」⇒ 谓词漏掉全部 G6 行。我按代码里的变量名/中文描述写 LIKE，没先看落库真值 | 谓词改为同时匹配 `%方向偏斜%`/`%sector%`/`%行业集中度%`/`%组合方向偏斜%`，并在脚本内注释说明「必须与 E2 的 cut 计数同一谓词」；修正后 **n=50 与 E2 一致** |
| 6 | 首版对账脚本把期望值**硬编码**（`if abs(p3 - 79.0) > 0.05`） | 文档改成真值 78.6% 后，脚本反而报「🔴 L3 应为 78.6 不是 79.0」⇒ **红的是脚本陈旧，不是文档错** | 对账守卫不该内嵌某一次的期望值快照；那等于把「今天的实测」冻成「明天的判据」，文档一改就假红 | 重写为 `deb20261008_verify_claims.py`：从 DEB 正文 **regex 提取 claim** 再与 DB 实测比对 ⇒ 文档/数据任一漂移都抓，且不需要同步维护期望值；入库为 E13 |
| 7 | 该守卫的 scratch 依赖检查**连续三版自指误报** | 检测行里写 needle ⇒ needle 命中检测行自己；第二版改拼接仍自命中（源码里 `"/tmp/"` 本身含 needle） | 守卫检测「源码不得含某字面量」时，它**自身必然含该字面量**——这是定义处的自指，不是 bug 可修掉 | 承认自指：needle 提为模块级 `_NEEDLES` 并**拼接构造**，检测时跳过守卫自身文件（它是定义处），其余 3 个附录脚本照检不排除。三版迭代过程全部留痕，不藏 |
| 8 | 我按日期前缀 glob `deb20261008_*.py` 当「本 DEB 的脚本」 | glob 到 **7 个**，其中 `deb20261008_scaleout_{arith,binding,cf}.py` 3 个是**另一 agent 并行新建**（`git status` 均 `??` 未提交） | 同日期前缀 ≠ 同议题；并行 repo 下 glob 会把他人的文件当自己的检查对象，既可能因他人文件漂移误报红，也可能在 `git add` 时卷入他人改动 | 守卫改为**只查本 DEB 附录明确列名的 4 个脚本**（白名单，不 glob），并主动打印 `ℹ️ 同日期前缀下另有他人/他议题脚本 (本守卫不检查、不提交)`；本批提交逐文件 `git add`，不用 `.`/`-A` |

---

## 12. 复查者入口

```bash
cd /opt/ftseek/FTSeek-US
venv/bin/python scripts/deb20261008_direction_gate_census.py    # E1–E3 闸普查 + 下游 + 开关
venv/bin/python scripts/deb20261008_gate_attribution.py         # E4/E10 归因核验 (G4/G6 vs G5)
venv/bin/python scripts/deb20261008_bias_funnel.py              # E5–E8/E11 偏向漏斗 + 数据缺口
venv/bin/python scripts/deb20261008_verify_claims.py            # E13 本文档数字对账守卫 (rc=0=绿)
```

四个脚本均只读、rc=0、无 scratch 依赖，可用 `env -u PYTHONPATH` + 任意 cwd 冷跑验证（复查者视角）。

**复查者最省力的入口是 E13**：它从本文档正文 regex 提取全部 7 个数字 claim 重算比对，
文档或数据任一漂移都会红；且已验证反向能力（改坏数字必报 rc=1，不是恒绿的死门）。

---

## 附录：复现材料

| 文件 | 用途 |
|---|---|
| `scripts/deb20261008_direction_gate_census.py` | E1–E3：六道闸锚点实存性 + 实测拦截量 + 下游消费者 + 开关可得性 |
| `scripts/deb20261008_gate_attribution.py` | E4/E10：今日 5→2 归因（G4/G6 vs G5）+ G5 实弹状态三态判定 |
| `scripts/deb20261008_bias_funnel.py` | E5–E8/E11：L1→L2→L3 偏向漏斗 + 空头清零日 + UE 兑现度 + 数据缺口 |
| `scripts/deb20261008_verify_claims.py` | E13：本文档 7 个数字 claim ↔ DB 实测自动对账 + 表格列数/章节引用结构自检（可作 CI 守卫） |

**相关既有文档**：
- `DEB-20260930-skew-gate-vision-gap.md` — guardrail 分母盲区（195/279 绕门）原始记录
- `RFC-121-skew-gate-day-scope-preregistration.md` — day-scope 提案撤回 + 2026-10-07 执行前收口实施记录
- `RFC-136-*fresh-event-da-batch-and-window-visibility.md` — DA 主链窗可见化（v0.93）
- `DEB-20261007-entry-pricing-reprice-mfe-capture.md` — 同日入场定价轴（与本 DEB 正交，结论互不依赖）

---

## 13. 裁决与落地记录 (2026-10-08 用户裁决, 当日执行完毕)

**裁决**: F1 摘 / F2 降为纯记录 / **F3 保留 G1 (防接飞刀, 用户明示)** / F4 做 / F5 做 /
F6 另案分析 (当日完成首轮, 见 docs/debates/DEB-20261008-model-long-bias-decomposition.md)。

**落地清单**:
- F1: `playbook.portfolio_guardrail_enabled=false` (yaml 单键, 热重载生效, 代码读失败
  fail-safe 保持开启) — G4 (pipeline._apply_portfolio_guardrails 偏斜+sector 两段) 与
  G5 (playbook_service 执行前) 砍单停用; **影子照算照写** (skew_gate_vision 保留,
  G5 审计行新增 `enforced` 字段, would-skip 落 `shadow_would_skip`)。
  min_rr 过滤与 sector 回填不受影响 (非方向闸)。测试: 砍单断言全部显式开开关,
  新增关闸零砍单 + 影子照写用例。
- F2: skill `signal_gate.py` G2 择时闸降纯记录 (dir_conflict 只留 reasons 不降 tier);
  skill v0.94→v0.95 (SKILL.md/CHANGELOG 同步, PROMPT_HASH 不变)。
- F3: G1 破位硬买门保留 (零改动)。
- F4: `scripts/deb20261008_blocked_outcome_backfill.py` — 被拦行方向取证
  (G4 从 skip_reason 多数方解析 / G6 查 DA checkpoint→bridge 兜底) + ic_tracker
  同口径四列回填 (entry/open 双锚), NULL-only 幂等。**实测: n=78 填充率
  0.0%→100%** (判据 ≥80% 达成), 取证链落 data/shadow/blocked_outcome_backfill.jsonl。
  验证器锚: F4 后被拦行填充率实测口径 **100.0%** (n=78, `fwd_return_intraday`;
  当日开盘锚同 78/78。T+1/T+1 open 当时为 75/78, 3 条 10-07 行等待 10-08 收盘)。
- F5: `da_bridge_dingtalk_push.py` 推送口径补 `OR decision_id LIKE 'universe_expansion%'`
  + 契约测试 (UE SHORT 行不再被漏计)。
- RFC-121 §变动记录 追加 (跨 agent 协调项, 见该文档)。

**F4 回填后的 S4 首读** (回答「闸拦对没拦对」, open 锚=诚实尺子):
被拦 78 笔若按原计划成交 — pooled 当日均值 +0.92% (胜率 53%), pooled T+1 均值
+1.36% (n=75, 胜率 60%)。按日聚类后分别为 +0.638% (t=1.26) 与 +0.443%
(t=0.48), 均与 0 不可区分; 因此本读数**只说明 pooled 首读为正, 不能作为
「闸净拦盈利单」的统计背书**。纸面收益未含费用, entry 锚含未成交限价优势。

---

## 14. 独立复查轮（Codex, 2026-10-08, 复查提交 `40b4b6a1f`）

### 14.1 代码与测试复验

```bash
PYTHONPATH=. ./venv/bin/python -m pytest \
  tests/test_deb20261008_blocked_backfill.py \
  tests/test_da_bridge_dingtalk_push.py \
  tests/test_direction_skew_guardrail.py \
  tests/test_portfolio_execution_guardrail.py \
  tests/test_skew_gate_vision_shadow.py -q

PYTHONPATH=. ./venv/bin/python -m pytest \
  tests/test_except_pass_ratchet.py \
  tests/test_naive_datetime_now_ratchet.py \
  tests/test_w456_log_hygiene_ratchet.py -q

PYTHONPATH=. ./venv/bin/python -m pytest tests/playbook -q
./venv/bin/python scripts/deb20261008_verify_claims.py
```

结果：
- 相关五文件：**40 passed**。
- 三条日志/时间棘轮：**29 passed**。
- `tests/playbook`：**1004 passed + 18 errors**。18 errors 全部集中在
  `test_offline_full_chain.py`，根因仍是驱动子进程 `require_auto_sim()` 读生产
  LIVE 配置后拒绝 SIM；属既有环境债，非本提交引入（本提交未改 driver/env 路由）。
- E13：7 个数字 claim 与 DB 实测一致，结构自检 0 问题。
- F6 §2 分解漏斗独立复跑：scanner `bullish 113 / bearish 68`；LLM
  `BUY 224 / SELL_SHORT 68 / SKIP 72`；交叉计数与文档逐项一致。
- F2 仓外 skill 实物存在：`SKILL.md version 0.95`，`signal_gate.py` 已移除
  tier 降档，`references/CHANGELOG.md` 有 v0.95 条目。

### 14.2 复查发现（不推翻落地，但必须修正解读）

#### F4「闸净拦盈利单」目前只是 pooled 读数，不满足按日聚类 + MDE 军规

§13 的 `+0.92% / +1.36%` 是逐行 pooled 均值。同日信号高度相关，按宪章数据口径
第 9 律必须报按日聚类值 + MDE。独立复算（open 锚）：

| 口径 | pooled n | 聚类天数 | pooled 均值 | 日均均值 | 日均 SE | t | 95% MDE |
|---|---:|---:|---:|---:|---:|---:|---:|
| 当日 | 78 | 19 | +0.919% | +0.638% | 0.506pp | 1.26 | ~0.99pp |
| T+1 | 75 | 18 | +1.359% | +0.443% | 0.928pp | 0.48 | ~1.82pp |

结论修正：
- **当日与 T+1 的按日聚类均值均与 0 不可区分**（t=1.26 / 0.48），MDE 分别约
  0.99pp / 1.82pp。
- §13 「闸净拦的是盈利单 ⇒ 摘闸裁决获回填证据支持」表述过强，应降级为：
  「pooled 首读为正；按日聚类后尚不显著，不能作为摘闸的统计背书。」
- F1 落地本身不因此回滚：摘闸另有点头、空头清零日与用户裁决；但 F4 不能继续被
  引用为「已证明拦错」。

#### F4 的 100% 是 `fwd_return_intraday` 口径，不是四列全量 100%

当前 DB：n=78 中当日/当日 open 均 78/78；T+1/T+1 open 均 75/78。缺的 3 行全部是
2026-10-07 的 PENG/NOK/MRVL，T+1 对应 2026-10-08，当时美股尚未收盘，属窗口未到期。
§13 已写明验证器锚是 `fwd_return_intraday`，这点成立；但读者不得把「四列回填」
读成「四列均 78/78」。今晚收盘后重跑可补 T+1，届时需重算聚类表。

#### G6 影子「照算照写」不是事件级全覆盖

`skew_gate_vision.jsonl` 只记录方向偏斜（G4），不记录 sector 集中度 would-skip；
`portfolio_execution_guardrail.jsonl` 只在执行前 G5 检查时写，且当前文件尚不存在。
因此 F1 对 G4 的影子是自动事件级；对 G6 只能事后从保留的 sector 字志重放，不是
「照算照写」的自动影子。文档应把 G6 标为「可重放、未自动落影子」，或补一个
sector would-skip shadow record。

#### E13 守卫白名单漏了 F4 新脚本

`deb20261008_verify_claims.py` 的 `OWN` 仍只列 4 个旧附录脚本，未把本提交新增的
`deb20261008_blocked_outcome_backfill.py` 加进去；实跑输出还把它列在
「他人/他议题脚本」里。这不影响 DB 对账，但 scratch/代码行守卫没有覆盖 F4 脚本。
建议下批把它加入 `OWN` 并把注释/输出从 4 个改为 5 个。

#### F2「纯记录」仍保留 `quality_score -= 10`

仓外 `signal_gate.py` 已不再降 tier，但 `dir_conflict` 仍扣 `quality_score` 10 分。
当前 tier 判定不消费 quality_score，因此交易判定确实只剩记录；但如果「纯记录」被
理解为「不再产生任何方向性扣分」，则尚未做到。文档应明确：**F2 = 不降 tier，仍保留
既有 quality_score 扣分**；若用户要求彻底纯记录，需另删该扣分并另立验证。

#### F4 `--dry` 会改写取证台账状态

脚本在 dry 模式仍重写 `blocked_outcome_backfill.jsonl`，并把既有 `status=filled`
覆盖成 `dry`。DB 不受影响，但「只算不写」的语义不严格，且会污染取证链。建议：
dry 模式不写 provenance，或 status 单独标 `dry_preview` 且不覆盖既有 filled 记录。

### 14.3 复查裁决

- **代码可保留，不建议回滚 `40b4b6a1f`**：F1/F5 的实现与测试成立，F4 数据回填
  本身可复算，F6 §2 漏斗可复现。
- **统计结论须降级**：F4 不构成「拦错盈利单」的显著性证据；后续引用必须带
  按日聚类 + MDE。
- **下批小修清单**：E13 `OWN` 加 F4 脚本；G6 影子语义补自动记录或改文档；
  F2 扣分语义改文档或按用户裁决删除；F4 dry-run 不覆盖 provenance。

---

## 15. 复查修复批（用户令「请修复」, 2026-10-08）

### 15.1 修复项

| 问题 | 修复 | 锚点 |
|---|---|---|
| G6 摘闸后无事件级 would-skip 影子 | `skew_gate_vision.jsonl` 同记录新增 `sector_vision`: sector counts、violations、would_skip_count、低置信 victim symbols；不新增第二影子产物、不改判定 | `app/playbook/pipeline.py:2230` |
| E13 白名单漏 F4 脚本 | `OWN` 加入 `deb20261008_blocked_outcome_backfill.py`，守卫覆盖 5 个本 DEB 脚本 | `scripts/deb20261008_verify_claims.py:204` |
| F4 `--dry` 覆盖取证台账 | 抽出 `_write_provenance(..., dry=True)`；dry-run 零写盘，不覆盖 filled 状态 | `scripts/deb20261008_blocked_outcome_backfill.py:135` |
| F2 仍保留 `quality_score -= 10` | 仓外 skill 删除该扣分；「纯记录」现在同时无扣分、无降档，仅 reasons/risk_notes 留痕 | `/root/.hermes/skills/deep-stock-analysis/scripts/signal_gate.py:120` |
| F4 统计表述过强 | §13 降级为 pooled 首读；按日聚类 D0 +0.638% (t=1.26)、T+1 +0.443% (t=0.48)，不作显著性背书 | 本文 §13/§14 |

### 15.2 验证

```bash
PYTHONPATH=. ./venv/bin/python -m pytest \
  tests/test_skew_gate_vision_shadow.py \
  tests/test_deb20261008_blocked_backfill.py \
  tests/test_direction_skew_guardrail.py \
  tests/test_portfolio_execution_guardrail.py -q

./venv/bin/python scripts/deb20261008_verify_claims.py
./venv/bin/python -m py_compile \
  app/playbook/pipeline.py \
  scripts/deb20261008_blocked_outcome_backfill.py \
  scripts/deb20261008_verify_claims.py
/root/.hermes/skills/deep-stock-analysis/scripts/signal_gate.py
```

实测结果：专项 **38 passed**；E13 **7/7 claim 一致 + 结构 0 问题 + 5 个附录脚本
全覆盖**；py_compile 全过；F4 `--dry` 前后 provenance SHA-256 均为
`fe4ce03510c496d74dd4778160c79602a4d2a5dd0e2ac48f11ba0d687ef3bec8`；
`tests/playbook --deselect tests/playbook/test_offline_full_chain.py` **1004 passed,
18 deselected**（既有 offline-chain 环境债另行处理）。

部署状态：`pipeline.py` 代码需下一合法重启窗口生效；F2 仓外 skill 由下次 DA
进程读取即生效；F4/E13/文档即时生效。本轮未重启。
