# DEB-20260723: DA/UE 链路 Prompt 与策略优化空间核验

> 状态: **立即档全部落地+Qoder 复核全绿 (V5.4)** (score 分层切片+回填+cron、P3.9 成文、UE V2.1、hermes 六项: 版本戳/conf_semantic 双写/SKIP 理由/模型回显/三次采样 shadow; 剩: 次交易日运行时验收 + 数据开门档随样本积累)
> 发起: 用户提问 — "DA和UE链路的prompt和对应的策略是否有可能进行优化, 有没有固化模板,
> 深入现象本质, 诚实指出弱点"
> 分析方: Qoder (只读核验 + 数据分析, 未改任何代码)
> 日期: 2026-07-23
> 关联: DEB-20260723-da-execution-mismatch-postmortem (信号-执行错位, E/A-lite shadow 已落地 record-only)

---

## 〇、议题与直接回答

**Q1: 有没有固化模板?** — 有, 两链路均已模板化:
- **UE**: `scripts/llm_universe_scan.py` L57-91 内嵌 V1/V2 双模板; config `universe_expansion.prompt_version: v2`
  (unified_config.yaml:1572) 可切换; 每次 scan 落库 `prompt_version` 可归因。V2 自 07-14 上线,
  含 evidence_ref 强制引证 + self_tier 三级自评 + Track A 代码侧夹逼 (`_normalize_surfaced` L507-523:
  无证据 → 强制 THESIS_ONLY 且 conf≤0.60, 兜底 LLM 不守自律)。
- **DA**: 技能在工作区外 (`/root/.hermes/skills/deep-stock-analysis/`, 工作区内不可读),
  输出契约固定 (execution_plan: entry/SL/TP 数字 + entry_strategy + invalidation,
  证据: `docs/specs/da-prompt-contract-draft-20260710.md` 引 llm_synth.py L509-517),
  带挑战者多模型辩论 (challenger=deepseek-v4-pro, 见 opportunity_scan bridge 的 debate 段)。

**Q2: 能否优化?** — 能, 但主张: **最高杠杆项不是"写更聪明的 prompt", 而是先修三处结构性病灶
(B1 置信度退化 / B2 评估闭环断电 / B4 输出-消费错位)。顺序反了, prompt 改得再好也会在传输中漏掉。**

---

## 一、实证发现 (每条附证据与影响链)

### B1. DA 置信度已退化为常数 — 本轮最重要发现

**证据** (`data/opportunity_scan_2026-07-{13..22}.json` 逐文件统计):

| 日期 | 候选 n | conf=0.35 | conf=0.25 | conf=0.5 | conf=0.7 | 方向 LONG/SHORT | debate_outcome 主体 |
|---|---|---|---|---|---|---|---|
| 07-13 | 4 | 2 | 1 | 0 | 1 | 3/1 | 方向分歧 x3 |
| 07-14 | 8 | 7 | 0 | 1 | 0 | 6/2 | 方向分歧 x7 |
| 07-15 | 10 | 8 | 0 | 0 | 2 | 7/3 | 方向分歧 x8 |
| 07-16 | 8 | 7 | 0 | 1 | 0 | 6/2 | 方向分歧 x7 |
| 07-17 | 5 | 5 | 0 | 0 | 0 | 4/1 | 方向分歧 x5 |
| **07-20** | 18 | **18** | 0 | 0 | 0 | **18/0** | **➖一方弃权 x18** |
| **07-21** | 13 | **13** | 0 | 0 | 0 | **13/0** | **➖一方弃权 x13** |
| **07-22** | 14 | **13** | 0 | 0 | 1 | **14/0** | **➖一方弃权 x13** |

- 07-13~17 (35 候选): 分歧为主, conf∈{0.25,0.35,0.5,0.7} 尚有分布。
- **07-20 起挑战者几乎全部弃权 (44/45)**, 而映射规则 **弃权=分歧=35** → conf 信息量≈0 bit。
- "挑战者没参与" 与 "挑战者反对" 是两个语义完全不同的状态, 被压成同一个数。

**影响链**:
1. EdgeShadowGate 按 conf 分桶 (`edge_shadow_gate.py` evaluate: 4 桶查表) → 所有 DA 单落进同一桶
   (0.3-0.5) → 查出同一个期望常数 0.000338。**这机制上完整解释了 DEB-20260723-postmortem
   第 4 轮"12 笔 DA 单同常数"的观测** — 不全是 gate 的桶级设计问题, 是输入已先退化成常数。
2. conf 驱动的仓位 sizing → 实际均匀 sizing, 区分度为零。
3. 07-20~22 恰恰是全 LONG 大亏时段的起点 (两日 28 单 46% 胜率 -$331, 见前析)。

### B2. UE 的评估闭环没通电 — V2 prompt 从未被 outcome 验证

**证据**:
- `data/shadow/llm_universe_scan.jsonl`: **13 条 scan 记录, 0 条 score 记录**。
- crontab (data/_live_crontab_snapshot.txt L67): 仅 `scan && inject && push` (21:10 CST),
  **`score` 无定时入口**, 只能手动跑。
- `data/shadow/prompt_eval.jsonl`: 仅 4 条, 全部 2026-07-14 — **V1/V2 A/B 只跑了一天**,
  V2 即上 live。升级依据是设计直觉, 不是数据。
- 07-22 新埋 `emergence_streak_days` (llm_universe_scan.py L528-537, 注释明写"两周后判分")
  **无评分器消费** → 将无声烂掉。
- V2 上线后每日仅浮现 2-3 标的 (07-14~22 scan 实测): 瓶颈是 LLM 自律,
  不是门槛 (min_confidence=0.50 / max_targets_per_day=30 均宽)。

**影响**: V2 的核心创新 self_tier 分级 (HARD_EVENT > MARKET_CORROBORATED > THESIS_ONLY)
**从未对过账** — 没有任何数据证明 HARD_EVENT 真的更准。self_tier/evidence_ref 已落库
(commit e12d94b1), 但落库≠验证。整个"意图对齐"改造目前停留在意图层面。

### B3. 两链路 confidence 语义不兼容, 却进同一个 DB 被同一批下游消费

- DA: 离散 {35/50/70}, 由 debate_outcome 映射 (一致70/分歧35/弃权35/无辩论50)。
- UE: 0-1 连续 float, Track A 夹逼 ≤0.60, inject 时 `UE_CONF_CAP=50` 再截断
  (llm_universe_scan.py L786-787, 注释: "低于 DA 一致70, 高于 DA 分歧35")。
- 该排序是**政策选择而非校准概率**; 下游 (edge gate 分桶/sizing) 却按可比较的概率读。
- 且两者 IC 均为负 (docs/entry_exit_strategy_review.md 已证) — 标量 conf 本身就是
  把结构化判定 (debate_outcome / self_tier) 有损压缩成一个数, 再被误读为概率。

### B4. Prompt 输出预算与执行消费的结构性错位 (最深一条)

| DA 输出字段 | 执行端消费现状 |
|---|---|
| symbol / direction | ✅ 真正消费 |
| confidence | ✅ 消费 (但已退化, B1) |
| entry_price / stop_loss / take_profit | ❌ 死字段 — reprice 按设计覆盖 (**此设计正确**, 用户已确认: 以实盘价成交而非 LLM 计划价) |
| invalidation | ⚠️ 07-23 起 A-lite shadow 首次读 (record-only, `_extract_invalidation_price`) |
| entry_strategy / conditions / risks | ❌ 仅拼推送文案, 执行永不读 |
| debate.challenged_claims | ❌ 落库即终 |

UE 同构: causal_chain / evidence_ref / self_tier 全部 record-only (self_tier 落库但执行端无人读)。

**结论**: 两条链路把大部分 token 预算花在一个**不存在的读者**身上。这决定了 prompt 优化的
天花板 — 在 E / A-lite / per-trade 评分 (DEB-20260723-postmortem 共识) 落地前,
信号端任何改进都会在传输中漏掉 (该 DEB 已定量: DA 波段 alpha 被日内动量执行丢弃)。

### B5. 方向分布漂移, 且无监控

07-13~17: LONG 26 / SHORT 9; **07-20~22: LONG 45 / SHORT 0**。
两种解释无法区分: (a) 行情 regime 确实全多; (b) 挑战者弃权机制削弱了产生 SHORT 的摩擦力
(分歧→弃权的时点恰与全 LONG 起点重合)。**系统没有任何方向分布监控**, 漂移不可归因。

### B6. DA prompt 本体是审计盲区

技能在工作区外; 挑战者行为在 07-18~07-20 间发生明显漂移 (分歧→弃权),
**工作区内零对应 commit** — 证明工作区外 prompt/skill 有变动且仓库内不可见、无版本戳、
无归因字段。UE 每 scan 记 `prompt_version` (如 "v2.0"), DA bridge JSON 无任何等价物。
契约草案早已警示: 改 DA skill "难回退, 带备份" (da-prompt-contract-draft L81)。

---

## 二、提案 (按杠杆排序, 诚实标注前置依赖)

### P0 — 是修复, 不是优化 (零风险, 立即可做)

1. **UE score 接电**: `score` 挂 cron (建议 05:40 CST, backfill_forward_returns 之后),
   report 按 self_tier 切片出命中率/前瞻收益并进推送。没有它一切 prompt 迭代都是盲飞。
   纯运维改动, 零交易行为变更。
2. **DA 弃权与分歧解耦**: 弃权不应=35 (建议: 弃权→单独标志位或映射"未验证"档);
   并排查挑战者 07-18 起集体弃权的根因 (模型端点故障? skill 侧改动? — 需能访问
   /root/.hermes 的一方执行)。**这正在毒害所有下游 conf 消费者, 优先级最高。**

### P1 — prompt 层, 但必须数据开门

3. UE: score 数据积累后验证 tier 排序 (HARD_EVENT 前瞻收益应显著 > THESIS_ONLY)。
   若成立, 再让 self_tier 进 inject 门 (如 THESIS_ONLY 须 corroborated_by_market=true)。
   **用数据不用直觉** — V2 凭 1 天 A/B 上线的教训还热着。
4. DA: 挑战者 prompt 强制表态 ("SKIP 必须给出具体反对理由, 无理由弃权视为无效响应"),
   让分歧重新携带信息; challenged_claims 要么被下游消费, 要么从契约删除 (省 token)。
5. 两链路: 下游改消费结构化判定 (DA=debate_outcome 枚举, UE=self_tier),
   放弃把标量 conf 当概率读 (per-trade 评分重建的前置, 与 postmortem 共识衔接)。

### P2 — 契约重对齐 (依赖 DEB-20260723-postmortem 执行项, 裁决 ≤07-28)

6. **prompt 预算从死字段搬到被消费字段**: DA 的 invalidation 改结构化输出
   (机读: {价格, 条件, 方向}), 直接根治 A-lite 正则提取的痛点 (实测 7/14 正确,
   COF "$155亿" 营收数字误报 — 在自然语言端打补丁不如在源头改契约)。
7. UE 补 invalidation 字段: UE 候选目前只有 thesis 无证伪条件, TIME_EXIT 是唯一失败模式
   (实测 UE 2 笔 TIME_EXIT -$109.08 占其全部亏损); 给 A-lite 在 UE 侧也留咬合点。

### P3 — 治理

8. DA bridge 输出加 prompt/skill 版本戳 (对齐 UE 的 prompt_version 归因能力)。
9. A/B 纪律成文: prompt 版本升级须 ≥10 交易日 + 分层命中率验证, 禁止直觉升级 —
   把 INTC 测试原则 ("任何新门上线前 07-22 回放不得拦 INTC") 推广到 prompt 层。

---

## 三、分析方诚实声明的局限

1. **DA prompt 本体不可读** (工作区外): B1/B5 是从输出工件 (bridge JSON) 反推,
   弃权漂移根因需能访问 skill 的一方核实。分析方无法排除"挑战者模型端点故障"这一
   与 prompt 无关的解释。
2. **UE tier 验证被 B2 自锁**: P1.3 所需数据不存在, 且在本分析之前无法产生 —
   这是本报告最大的证据缺口, 也意味着"self_tier 分级有价值"目前只是假设。
3. **prompt 优化天花板**: 即使信号端修到完美, 执行消费缺口 (DEB-20260723-postmortem)
   不补, 兑现收益仍受限。本报告所有 P1/P2 项的实际收益都受该 DEB 裁决 (≤07-28) 制约。
4. 方向漂移 (B5) 样本仅 8 个交易日, 07-20~22 全 LONG 也可能是真实 regime;
   分析方不主张这是 bug, 只主张**不可归因本身是问题**。
5. 背景绩效数据 (复用前析, 供辩论方核对): DA 62 单 46.8% 胜率 -$255.03 (PF 0.65,
   TIME_EXIT 18 单 -381.37 为主出血点); UE 12 单 66.7% 胜率 +$3.16 (PF 1.01,
   利润全部来自 SL_TRAIL_LOCK 5 单 +$169.66); 浮现连续性: 重开≥3次标的 +$202 vs
   一次性标的 -$331 (7/9-7/21, 含幸存者偏差)。

---

## 四、留给辩论方的具体问题 (请逐条表态: 接受/反驳+证据)

1. B1 的"弃权=分歧=35"映射, 是设计意图 (弃权即不信任) 还是疏忽? 若主张设计意图,
   请论证为何"未参与"应与"反对"同分。
2. 挑战者 07-18 起集体弃权: 请检查 skill 侧 (/root/.hermes) 是否有 prompt/模型端点变动;
   若是端点故障, B1 的定性应从"prompt 设计缺陷"改为"运维事故", P0.2 的修法随之改变。
3. B2: 是否同意"score 未接电"是当前 UE 迭代的最高阻塞? 若否, 请指出更高阻塞项。
4. B4 的消费表: 是否有分析方遗漏的执行端消费者 (例如某处确实读了 entry_strategy/conditions)?
   — 若找到, B4 结论需降级。
5. P0.2 修法选择: (a) 弃权→独立标志位; (b) 弃权→conf=50 ("未验证"中性); (c) 弃权→拒绝该候选。
   各有什么二阶后果 (尤其对 EdgeShadowGate 分桶与 sizing)?
6. P2.6: DA invalidation 结构化是否值得改工作区外 skill (难回退)? 还是应在 A-lite 提取端
   继续打补丁 (如过滤"$数字+亿"营收语境)? 请权衡。
7. P3.9 的 10 日 A/B 门槛是否过重 (UE 每日仅 2-3 浮现, 10 日样本仍薄)? 请给出替代判据。

---

## 五、辩论记录

### 第 1 轮 (分析方 Qoder, 2026-07-23)

即本文第〇~四章。核心立场: **三处结构性病灶 (B1/B2/B4) 先于任何 prompt 文采优化;
P0 两项是修复不是优化, 零风险立即可做; P1 起全部数据开门, 拒绝第二次直觉升级。**

### 第 2 轮 (辩论方 Qoder — 盲区核验: 读到了 /root/.hermes, 2026-07-23)

**核验总评**: B2/B3/B4/B6 全部独立复现成立; **B1/B5 现象属实但归因需重大修正** — 辩论方具备分析方没有的 `/root/.hermes` 读权限, 完成了第 1 轮声明的最大盲区 (局限 1) 的实地核验。以下 N1-N3 为本轮新证据。

#### N1. B1 归因修正: "弃权=分歧=35 映射"已于 07-18 修复, live 的 0.35 是**三层规则叠加的涌现常数**

证据链 (全部在 `/root/.hermes/skills/deep-stock-analysis/scripts/`):
1. `adapter.py:222` (07-18, 注释标注用户批准): 弃权 ≠ 真反向, **中性不罚保持 0.50** — 分析方指控的映射缺陷已不存在。
2. 但 `signal_gate.py:349-357` tier 阶梯: 优需 decision_conf≥0.7 / 良需 ≥0.6, 而 +0.20 只奖励"独立一致+AGREE" → **弃权单永远停在 0.50, 结构性地只能落"中"档** → `_tier_map` "中"→WARN。
3. `pipeline.py:444` WARN 降权: `max(0.25, 0.50-0.15) = 0.35`。

**链条**: 弃权 0.50 → tier 恒"中" → WARN -0.15 → **0.35**。三层各自合理, 叠加后把"弃权不罚"的修复意图完全抵消, 最终数字与修复前一模一样。**对下游而言 B1 的症状 (conf 常数 0 bit) 完全成立, 但 P0.2 的修法必须重定义: 不是"解耦映射"(已做), 是"让解耦在全链生效"。** 这也是一个新型病理样本: 单点修复被下游规则栈静默吹零, 无端到端验证则无人发现。

#### N2. "07-18 起集体弃权"部分是**标签假象**; 真实漂移是挑战者 SELL_SHORT 归零

逐日挑战者行为 (bridge JSON `debate.challenger_direction` 实数):

| 日期 | 挑战者 SKIP | 挑战者 SELL_SHORT | 挑战者 BUY | fallback |
|---|---|---|---|---|
| 07-13 | 2/4 | 2 | 0 | False |
| 07-14 | 7/8 | 1 | 0 | False |
| 07-15 | 3/10 | 6 | 1 | False |
| 07-16 | 5/8 | 2 | 1 | False |
| 07-17 | 4/5 | 1 | 0 | False |
| 07-20 | **18/18** | **0** | 0 | False |
| 07-21 | **13/13** | **0** | 0 | False |
| 07-22 | 13/14 | **0** | 1 | False |

- **弃权早已是常态** (07-14: 7/8, 07-17: 4/5) — 07-18 前它被标为"⚠️方向分歧(BUY vs SKIP)", 07-18 改动把同一行为重标为"➖一方弃权" → 第 1 轮表格里的"07-20 断崖"相当部分是**重标签产物**, 非行为突变。
- **真实信号**: 挑战者 SELL_SHORT 6→2→1→0 归零, 且分析师侧 SHORT 候选同步归零 (B5 的 45/0)。两个模型同时转向, 指向共同输入变更或真实 regime。
- **排除端点故障** (问题 2 答案之一): 全部 `challenger_fallback=False`、`challenger_model=deepseek-v4-pro` 在场。不是运维事故。

#### N3. 漂移窗口内找到 3 处工作区外变更, 其中一处直接改了 LLM 输入 (B6 实锤 + 问题 2 主假设)

| 变更 | 时间戳 (文件系统实证) | 内容 |
|---|---|---|
| `llm_synth.py.bak-20260718_124329` | **07-18 (周六)** | ① 弃权重分类 (N2 标签变更); ② **关闭 claim IC 注入** — "论据有效性反馈"段从 data_text 移除 (注释标注用户批准) |
| `SKILL.md` | 07-20 22:35 | v0.60 交易哲学定调 ("不预测,识别→趋势明朗抓多赚") |
| `signal_gate.py.bak-20260722_preB` | 07-22 | 破位硬买拦截 (bearish+BUY→SKIP) |

**关键**: `data_text` 同时喂分析师与挑战者。claim IC 注入 07-18 (周六) 被关, 首个交易日 07-20 (周一) 两个模型行为同步突变 — 时序精确吻合。**无法与 regime 解释完全区分** (07-20~22 可能确实全多), 但这正是 B6 的实锤: 三处变更全部落在漂移窗口, 工作区 commit 零可见、无版本戳、无归因字段。P3.8 从"治理建议"升级为**已被实证需要的基础设施**。

> ⚠️ **第 3 轮深挖订正 (见 N6) + 第 4 轮实验定论 (见 N7)**: "claim IC 关闭→弃权"作为问题 2 的主假设**已被离线双臂反事实正式洗清** — 机制方向与观测相反 (看空论据 alpha 全负, 关闭本应增加 SELL_SHORT); 且同输入多轮重跑下 claim IC 效应≤温度噪声 (挑战者方向 BUY/SKIP/SELL_SHORT 随机翻转)。真因更可能是**挑战者模型版本漂移** (deepseek-v4-pro 无版本锁定) 叠加挑战机制本身的随机不稳定 (N7)。B6 的实锤 (工作区外变更不可见) 仍然成立, 但具体到 claim IC 这一处的因果归因已定为"非原因"。

#### N4. B4 消费表核验: 成立, 无遗漏消费者 (问题 4 答案)

逐一追踪了疑似消费点, 全部是落库/文案, 无执行决策消费:
- `entry_strategy`: pipeline.py:447 仅入库 dict; :1832 仅 dashboard brief 提取; playbook_service:453-474 仅拼推送文案。
- `conditions`: 仅 `condition_taxonomy.validate_conditions` 做 schema 校验 — **有校验器但无求值器**, 执行层无任何 evaluate/trigger 分支读它 (死字段配了活门卫, 讽刺性佐证 B4)。
- `emergence_streak`: playbook_service:1380 → 推送文案; 无评分器 (与 B2 一致)。

#### N5. B2 完全复现

scan 13 条 / score 0 条; crontab L67 仅 `scan && inject && push`; prompt_eval 4 条全在 07-14。无异议。

#### N6. claim IC 因果链深挖 (第 3 轮, 2026-07-23): 原 N3 主假设被削弱, 真因更可能是挑战者模型版本漂移

针对用户追问"弃权是否因关闭 claim IC 注入导致", 逐层取证后结论: **很可能不是主因, 机制方向与观测相反。**

**证据 1 — 机制方向矛盾**: `claim_ic_cache.json` 中所有看空类论据的方向调整 alpha 全为负 (insider_bearish -0.27% / technical_breakdown -0.34% / valuation_expensive -0.39% / earnings_beat -0.28%)。注入文本若生效, 是在告诉模型"看空论据历史亏钱" → **抑制** SELL_SHORT。关闭它本应让 SELL_SHORT **增加**, 但观测是归零 — 与预测相反。

**证据 2 — 注入内容是弱元信号**: `effective_claims`/`ineffective_claims` 列表均为空, 注入函数只产出"超额收益明细"表; 且 n≥10 的 top/bottom 3 全是 META 字段 (quality_score / gate_decision / theme_momentum / debate_outcome / macro_tailwind), **看空论据类型根本不在注入文本里** — 挑战者读不到"看空论据无效"的直接指令。

**证据 3 — 挑战者看空能力完好, 只是改了方向标签**: 07-20 SKIP 样例 (TTE/E) 仍输出完整看空分析 — challenged_claims 2 条 + missed_risks 4 条 (财报博弈、QQQ 5日-4.2%下行、买盘/卖盘比-33.333 抛压、股息可持续性、缩量反弹假信号), challenger_score=4-5, agreed=False。**挑战者依然看空, 只是把 SELL_SHORT 标签换成 SKIP** (对比 07-15 GS: score=4 却标 SELL_SHORT)。这是**方向承诺阈值上移**, 非看空能力丧失。

**证据 4 — 挑战者 prompt 未变**: 07-18 diff 仅 31 行 4 块, 完全不触及挑战者 system/user prompt (llm_synth.py 683-723 行)。挑战者收到的唯一输入变化是 data_text 少了 claim IC 段。

**证据 5 — regime 也不能干净解释**: 07-20 QQQ 5日 -4.2% (下行), 并非纯多头 regime; 但分析师仍 100% LONG。纯 regime 解释力不足。

**更可能的主因 — 挑战者模型版本漂移**: `CHALLENGER_MODEL="deepseek-v4-pro"` 硬编码、**无版本锁定**。若供应商在 07-18/20 前后更新了该端点背后的模型 (变得更谨慎、更不愿承诺方向), 会精确解释"看空分析完好但 SELL_SHORT→SKIP"且本地零变更记录。此假设当前不可 locally 证伪。

**诚实边界**: claim_ic_cache.json 无备份 (现版本生成于 07-23 06:35), 07-18 注入时的真实内容不可恢复, 故无法 100% 排除"旧缓存含 populated 看空论据指令"的可能; 但可用证据与假设方向相反。

**决定性检验 (可证伪设计)**: 离线反事实双臂重跑**同一批 07-20 候选** — (A) 重开 claim IC 注入; (B) 保持关闭。若 A 臂 SELL_SHORT 恢复 → claim IC 是主因 (本分析错); 若 A 臂仍 0 SELL_SHORT → claim IC 洗清, 模型/regime 成立。**附加**: 记录 API 返回的真实模型版本并 pin `CHALLENGER_MODEL` 版本号, 把"上游模型漂移"从不可观测变为可观测 (与 P3.8 版本戳同源)。

> ✅ **本检验已于第 4 轮执行, 结果见 N7。结论: claim IC 正式洗清, 且暴露出比 claim IC 更严重的挑战者随机不稳定问题。**

#### N7. 离线双臂反事实已执行 (第 4 轮, 2026-07-23): claim IC 正式洗清 + 新发现挑战者温度随机不稳定

用户授权后, 按 N6 的可证伪设计跑了 record-only 双臂实验 (`scripts/_tmp_claim_ic_counterfactual.py` + `_tmp_claim_ic_robustness.py`, **全程零交易**: 仅 `collect_all` + 分析师/挑战者 LLM 调用, 不触 pipeline Stage 6b、不写指令、不下单)。

**实验设计**: 选 07-20 挑战者 SKIP 样例 (TTE/E), 两臂**共用同一份当前采集数据**以隔离 claim IC 单一变量。A 臂 = 分析师读 `data_text + claim_IC` → 挑战者; B 臂 = 分析师读 `data_text` → 挑战者。挑战者走真实代码路径 `_run_challenger` (读 `data_text[:4000]`)。

**发现 1 — 截断假设被证伪 (诚实订正)**: 实测 `len(data_text)` = TTE 3735 / E 3690 字符, **均 < 4000**, claim IC 追加位置落在挑战者 `[:4000]` 视野内 → 挑战者**能**直接看到 claim IC。第 3 轮曾推测"claim IC 被 [:4000] 截断、挑战者根本读不到" — **该推测对这两个标的证伪**, 予以订正。(注: data_text 长度随新闻/K线条数浮动, 长标的仍可能被截断, 但本批样例不成立。)

**发现 2 — 首轮双臂 (单轮)**:

| 标的 | 臂 | 分析师 | 挑战者方向 | score | 模型 |
|---|---|---|---|---|---|
| TTE | A 含IC | SKIP | **SKIP** | 5 | deepseek-v4-pro |
| TTE | B 无IC | SKIP | **SELL_SHORT** | 3 | deepseek-v4-pro |
| E | A 含IC | BUY | SKIP | 4 | deepseek-v4-pro |
| E | B 无IC | BUY | SKIP | 4 | deepseek-v4-pro |

表面看 TTE 含IC→SKIP、无IC→SELL_SHORT, 似与 N6 证据 1 (claim IC 抑制做空) 同向。但单轮不可靠, 故做稳健性。

**发现 3 — 稳健性 (TTE 双臂各 3 轮, 输入完全相同、分析师均 SKIP)**:

| 臂 | 3 轮挑战者方向 | 分布 |
|---|---|---|
| A 含IC | BUY(7), SKIP(5), SKIP(5) | {BUY:1, SKIP:2} |
| B 无IC | SKIP(5), SELL_SHORT(7), SKIP(5) | {SKIP:2, SELL_SHORT:1} |

**相同输入下挑战者在 BUY / SKIP / SELL_SHORT 之间随机翻转** — claim IC 的效应**完全淹没在温度 (0.3) 噪声里**, 无法与噪声分离。首轮的 SKIP-vs-SELL_SHORT 差异主要是噪声, 非 claim IC 的确定性驱动。

**裁决 — claim IC 正式洗清 (双重理由)**:
1. **效应量 < 噪声**: 同输入多轮下挑战者方向随机翻转, claim IC 不构成可复现的方向驱动。
2. **方向相反**: 即便存在微弱效应, 也是"含IC 更保守 (抑制做空)", 与"关闭 claim IC 导致 SELL_SHORT 归零"所需的因果方向相反。

→ **关闭 claim IC 不是 07-20 起挑战者集体弃权 / SELL_SHORT 归零的原因。** N6 的机制推理获直接实验佐证。

**新发现 (比 claim IC 更严重) — 挑战者温度随机不稳定**: 同一份数据 + 同一份分析师输出, 挑战者可给出 BUY(7) / SKIP(5) / SELL_SHORT(7) 三种截然相反的方向。这意味着**挑战机制本身不可复现**: 单次 debate 的方向承诺带有很大的随机性, 07-20~22 "~100% SKIP" 的持续塌方因此更不可能是 claim IC 噪声造成 (噪声不会多日多候选持续同向), 而指向一个**系统性偏移** — 与 N6 的模型版本漂移假设一致。

**模型版本核验的局限**: DashScope 响应 `model` 字段仅回显**别名** `deepseek-v4-pro` / `qwen3.7-max`, 不返回带日期的快照版本号 → **无法从 API 直接证实/证伪上游模型漂移**。这恰恰强化了执行项"挑战者模型版本锁定 + 落库 API 返回版本"的必要性 (把不可观测变为可观测)。

**诚实边界**: 实验用当前 (07-23) 数据而非 07-20 历史数据 (历史 data_text 未落库、不可恢复); 样本仅 2 标的、稳健性 N=3。但"claim IC 效应 ≤ 温度噪声"与"挑战者同输入随机翻转"两点不依赖 07-20 原数据, 结论稳健。

#### 对留给辩论方的 7 个问题逐条答复

1. **弃权=分歧是设计还是疏忽?** — 前提已过时: 是疏忽, 且 07-18 已修 (用户批准)。但修复被 tier 阶梯 + WARN 叠加静默吹零 (N1), **结果层面"未参与"仍与"反对"同分**。需端到端修复 + 验收断言 (弃权单落库 conf 必须 ≠ 真反向单)。
2. **弃权漂移根因?** — 排除端点故障 (fallback=False)。部分是重标签假象 (N2); 真实漂移 (双模型 SHORT 归零) 与 claim IC 注入关闭时序精确吻合 (N3), 但与 regime 不可区分。B1 定性从"prompt 设计缺陷"改为**"规则叠加涌现 + 输入变更无归因"复合病理**。建议离线反事实: 抽样重跑含 claim IC 的 prompt (不交易), 看挑战者是否恢复表态。
3. **score 未接电是 UE 最高阻塞?** — **接受**。零风险纯运维, 无更高阻塞项。emergence_streak 无评分器同根同源, 应一并接入。
4. **B4 有遗漏消费者?** — 无 (N4)。B4 维持原级别。
5. **P0.2 修法选择?** — 三选项都错了靶子: (a) 独立标志位已存在 (debate_outcome 已区分"➖一方弃权"), (b) 弃权→中性 0.50 已在 adapter 层实现。真正的修点在下游叠加: 方案 (b') **弃权单豁免 WARN 的 -0.15** (或 WARN 降权改乘法), 使落库 conf=0.50 ≠ 真反向 0.35。二阶后果警告: 弃权 cohort 当前实绩 46% 胜率 -$331, **conf 35→50 会直接放大 _conf_factor 仓位** — 在无 edge 证据前给亏损 cohort 加仓是反向操作。故建议: **语义修复先 record-only shadow** (落库双写 conf_semantic/conf_live), 测量两周后再切 (与测量优先原则一致); (c) 弃权→拒绝是过度杀 (弃权 cohort 含盈利单, 且会把日候选从 14 砍到 ~1)。EdgeShadowGate 已 deprecated-shadow, 分桶影响不构成阻塞。
6. **P2.6 改 skill 还是补 A-lite 提取?** — **改 skill, 但用加法式契约**: 保留 invalidation 自然语言字段不动, 新增 `invalidation_price` 结构化字段; 提取端优先读结构化, 无则回退正则。加法式变更回滚=忽略新字段, 风险可控; 同时根治 COF 误报与 27% 覆盖两个痛点 (与 postmortem DEB 执行项对齐)。前提: 先落 P3.8 版本戳, 否则又一次不可归因变更。
7. **10 日 A/B 门槛过重?** — 日历门槛确实不适配 UE 低吞吐。替代判据: **样本计数制** — ≥30 浮现候选且全部有 T+1 outcome (约 10-15 交易日自然达成); 另建议 V1/V2 并行 shadow (同日双跑只 live 一个) 替代串行 A/B, 同 regime 配对比较消除日间干扰。

#### 对提案的总体表态

| 提案 | 表态 | 修正 |
|---|---|---|
| P0.1 UE score 接电 | ✅ 接受 | +emergence_streak 评分器一并 |
| P0.2 弃权解耦 | ✅ 接受但重定义 | 映射已修; 真修点=WARN/tier 叠加; 先 shadow 后切 (仓位二阶风险) |
| P1.3 tier 排序验证 | ✅ 接受 | 样本计数制替代日历制 |
| P1.4 挑战者强制理由 | ✅ 接受 | 保留 SKIP 权利, 只强制理由; 勿强制表态 (会造假反对) |
| P1.5 消费结构化判定 | ✅ 接受 | 与 postmortem per-trade 重建合并, 勿另开工程 |
| P2.6 invalidation 结构化 | ✅ 接受 | 加法式契约; 前置 P3.8 |
| P2.7 UE 补 invalidation | ✅ 接受 | 同上 |
| P3.8 DA 版本戳 | ✅ 接受并升级 P0 | N3 实证: 漂移窗口 3 处不可见变更, 这是归因能力的地基 |
| P3.9 A/B 纪律 | ✅ 接受 | 样本计数制 + 并行 shadow |

**收敛判断**: 两轮无根本分歧 — 分析方的方向排序 (先修结构性病灶后改 prompt 文采) 被核验加强; 唯一实质修正是 B1 归因与 P0.2 修点位置。共识稿见下方决策段, 待用户确认。

### 第 5 轮 (Qoder 验证 + 执行, 2026-07-23)

用户确认共识, 指定 Qoder 执行落地。先验证后执行。

#### V1. 辩论方关键声明独立复现 — 全部吻合

| 声明 | 复现方式 | 结果 |
|---|---|---|
| N1 三层叠加 (adapter:222 弃权→0.50 中性) | 直读 hermes adapter.py L221-225 | ✅ 注释/日期/逻辑精确吻合 |
| N1 (signal_gate tier 阶梯: 弃权恒"中") | 直读 signal_gate.py L340-357 + `_tier_map` L377 "中"→("WARN","tiny") | ✅ |
| N1 (pipeline.py:444 WARN -0.15 → 0.35) | 直读 pipeline.py L442-445 `max(0.25, conf-0.15)` | ✅ |
| N2 逐日挑战者方向表 + fallback 全 False | 独立统计 bridge JSON `debate.challenger_direction` | ✅ 8 天逐格吻合 |
| N4 B4 消费表无遗漏消费者 | grep entry_strategy/conditions 全部消费点 | ✅ 仅 dashboard/推送/schema 校验; condition_taxonomy.py:61 注释自证"无视 conditions 语义" |
| N5 B2 (scan 13/score 0) | 与分析方第 1 轮一致 | ✅ |

补充实锤 (新): SKILL.md frontmatter `version: 0.58`, 但 description  changelog 自述最新为 v0.60 (07-20 修改) — **版本字段本身就未被维护**, P3.8 版本戳必要性又一直接证据。

#### V2. 已执行落地 (工作区内, 全部 record-only / 零交易行为变更)

1. **P0.1-b score 分层字段**: `llm_universe_scan.py` cmd_score 的 results 行补
   `self_tier / emergence_streak_days / evidence_ref` (随行落库, V1 旧记录为 None 兼容)。
2. **P0.1-b report 切片**: cmd_report 新增两个切片块 — 按 self_tier (P1.3 判据:
   预期 HARD_EVENT > MARKET_CORROBORATED > THESIS_ONLY) + 按浮现连续天数 (首日/2日/≥3日)。
3. **历史 score 回填**: 手动补跑一次 `score`, 13 条历史 scan 全部产出 score 记录
   (此前 0 条)。明日起 cron 接续 (见 H0 移交项)。
4. **P3.9 纪律成文**: `docs/specs/prompt-change-discipline-20260723.md` —
   版本戳前置硬门槛 / 样本计数制 (≥30+全T+1) / 并行 shadow 替代串行 A/B /
   INTC 原则推广到 prompt 层 / 置信度语义与 shadow 双写规则。

#### V3. 首份 self_tier 分层数据 (进一步分析, 此前从未存在)

| self_tier | n | 命中率 | 平均前瞻 |
|---|---|---|---|
| HARD_EVENT | 1 | 1.000 | +3.77% |
| MARKET_CORROBORATED | 14 | 0.714 | +2.29% |
| THESIS_ONLY | 0 | — | — |

解读与 caveats (诚实):
- **THESIS_ONLY n=0**: Track A 夹逼 (conf≤0.60) 叠自律后, 此类候选几乎不以 conf≥0.50 浮现 —
  V2 的克制设计在起作用, 但"THESIS_ONLY 更差"因此**永远无法被证伪** (幸存者结构);
  tier 验证实际只在 HARD_EVENT vs MARKET_CORROBORATED 间进行。
- **n 有水分**: 07-14 有 5 条同日重复 scan (V1 期测试残留), 同标的 (EOG 等) 跨 scan 重复计分;
  P1.3 的 ≥30 样本判据应按**去重 symbol+date** 口径计。
- **horizon 混合**: 回填的历史 score 用当前价, 07-14=9 日持有 vs 07-22=1 日;
  明日起 cron 每日评分统一为 T+0 收盘口径, 切片随时间自然净化。
- **近端趋势与 B5 互证**: 07-20 +1.7% → 07-21 -0.8% → 07-22 -2.68%, 全 LONG 漂移段
  UE 前瞻同步恶化 — 两链路输入端 (共同的新闻/regime 原料) 同源漂移的旁证。

#### V4. 移交项 (沙箱写权限阻挡: crontab 与 /root/.hermes 只读可写禁)

以下规格已精确到行, 由有写权限的一方 (用户/辩论方 agent) 应用, Qoder 复核:

**H0. P0.1-a score cron** ( consensus 立即档; Auto 模式 `crontab -` 写被挡):
```cron
# UE score 接电 (P0.1-a, DEB-20260723-da-ue-prompt-optimization): 美东收盘后评分前一日 scan; 回滚=删除本行
40 5 * * 2-6 cd /opt/ftseek/FTSeek-US && PYTHONPATH=. /opt/ftseek/FTSeek-US/venv/bin/python scripts/llm_universe_scan.py score >> /opt/ftseek/FTSeek-US/logs/universe_scan.log 2>&1
```

**H1. P0.2-版本戳** (`/root/.hermes/skills/deep-stock-analysis/scripts/pipeline.py`):
Stage 6b 函数内 `_today_str` 赋值后插入:
```python
        # [P0.2-版本戳 2026-07-23, DEB-20260723-da-ue-prompt-optimization] 归因地基, 前置一切 skill 侧变更
        _skill_version = "unknown"
        try:
            import re as _re_sv, pathlib as _pl_sv
            _sk = _pl_sv.Path(__file__).resolve().parents[1] / "SKILL.md"
            _m = _re_sv.search(r"(?m)^version:\s*(\S+)", _sk.read_text(encoding="utf-8"))
            if _m:
                _skill_version = _m.group(1)
        except Exception:
            pass
```
`bridge_data` dict (现 L576-582) 增键: `"skill_version": _skill_version,`。

**H2. SKILL.md 版本对齐**: frontmatter `version: 0.58` → `0.61`, description changelog 前置一行
`v0.61: skill_version桥戳+conf_semantic双写+挑战者SKIP理由+模型回显落库 (DEB-20260723)`。
(现版本字段滞后于自述 changelog v0.60, 一并修正。)

**H3. P0.2-弃权语义 shadow 双写** (hermes pipeline.py, record-only):
- WARN 分支 (现 L442-445) 内增: `a["_semantic_confidence"] = _vr_pre.get("confidence", 0.5)`
- cand dict (现 L461-482) 增键: `"confidence_semantic": a.get("_semantic_confidence", vr["confidence"]),`
- 验收断言: 弃权单 `confidence_semantic=0.50` ≠ 真反向单 `0.35`; live `confidence` 不变 (仍 0.35),
  仓位零影响; 离线按 `decision_id` join 交易结果测量两周后再议切换。

**H4. 挑战者模型回显落库 + P1.4 SKIP 理由** (`llm_synth.py`):
- `_call_llm` (现 L56-59) `return json.loads(text)` 改为:
  ```python
  out = json.loads(text)
  if isinstance(out, dict):
      out.setdefault("_model_reported", data.get("model"))  # 信封回显, 上游漂移可观测
  return out
  ```
- `_run_challenger` system prompt JSON 模板增字段:
  `"skip_reason": "仅当 my_direction=SKIP 时必填: 具体观望理由 (缺哪条关键数据/哪个风险不可定价); 无理由的 SKIP 视为无效审查"`
  (保留 SKIP 权利, 勿强制表态 — 防造假反对。)
- `_merge_debate_results` 的 `_debate` dict (现 L765-774) 增两键:
  `"challenger_model_reported": challenge.get("_model_reported")`,
  `"challenger_skip_reason": challenge.get("skip_reason")`。

**H5. 挑战者采样稳定性 (N7, 需用户设计决策后再实施)**: 同输入下挑战者方向
BUY/SKIP/SELL_SHORT 随机翻转 (温度 0.4)。两方案待选: (a) 挑战者降温至 0.0-0.1
(确定化, 但可能固化偏见); (b) 3 次采样取众数 (成本 3x, 保留多样性)。
Qoder 倾向 (b) 先 shadow: 落库 `challenge_direction_distribution` 一周看翻转率再定。

#### V5. 用户两项批准已执行 (2026-07-23)

**V5.1 UE V2.1 "邀请 THESIS_ONLY" — 已由 Qoder 直接落地** (工作区内, 当晚 21:10 scan 生效):
- 动因: V3 首份 tier 数据实证 THESIS_ONLY n=0 (幸存者结构, 该层永远无法被证伪)
  + 用户目标"模拟期每日足够交易量"。V2 prompt "绝不为凑数做板块联想"是绑定约束。
- 改动 (`scripts/llm_universe_scan.py`, 加法式, 回滚=git revert):
  SYSTEM_PROMPT_V2 改双通道 — 主通道 (HARD_EVENT/MARKET_CORROBORATED) 纪律一字未动;
  新增 THESIS_ONLY 通道允许板块联想/逻辑推演浮现, 强制 self_tier=THESIS_ONLY +
  conf≤0.60 + evidence_ref 空 + thesis 以"板块联想:/逻辑推演:"开头。
  `PROMPT_VERSION` → "v2.1" (P3.9 版本戳前置, v2.0/v2.1 可在 tier 切片中分离对比)。
- 不变的安全网: Track A 代码夹逼 (`_normalize_surfaced` 无证据→强制 THESIS_ONLY+≤0.60)、
  inject min_conf=0.50、UE_CONF_CAP=50、strategy_id 物理隔离 — 全部原样。
- 验证计划 (P3.9 纪律首次实战): 今晚起 tier 切片将首次出现 THESIS_ONLY 样本;
  判据 = ① THESIS_ONLY 样本流形成 (日 3-8 个) ② 主通道两 tier 命中率不退化
  (对照 v2.0 基线: MARKET_CORROBORATED 0.714/+2.29%) ③ ≥30 去重样本后符号检验。
- 预期效果: UE 日浮现 2-3 → 5-10; 质量分层由 score 数据判定而非 prompt 自律压制。

**V5.2 H5 挑战者采样稳定性 — 用户批准方案 (b) 三次采样, 先 shadow; 规格待移交** (hermes 侧):
`_run_challenger` 首个成功结果返回前插入 (record-only, live 仍用第 1 次方向, 行为零变更):
```python
        if result and "_error" not in result:
            # [H5 2026-07-23 用户批准, DEB-20260723] 采样稳定性 shadow:
            #   同输入再采样 2 次, 方向序列落库测翻转率; live 用第 1 次结果, 零行为变更。
            _samples = [str(result.get("my_direction", "?")).upper()]
            for _i in range(2):
                _r2 = _call_llm(challenger_system, challenger_user,
                                temperature=0.4, max_tokens=2000, model=CHALLENGER_MODEL)
                if _r2 and "_error" not in _r2:
                    _samples.append(str(_r2.get("my_direction", "?")).upper())
            result["_direction_samples"] = _samples
            return result
```
`_merge_debate_results` 的 `_debate` dict 增键:
`"challenge_direction_samples": challenge.get("_direction_samples")`。
- 成本: 每候选 +2 次挑战者调用 (~14 候选/日 ≈ 28 次/日, 可忽略)。
- 一周后测量: 翻转率 = len(set(samples))>1 的候选占比; 若高 (N7 提示 ≥50%),
  再提案 live 切换为 3 取众数 (属行为变更, 届时单独批准)。

**裁决日纪律 (方案 B, DEB-20260723-da-pipeline-efficiency 第3轮共识, 防墓地)**:
三次采样是**临时测量**, 非永久成本。写死裁决日 **≤2026-07-30**, 到期二选一:
- **翻转率低** (稳定) → **回退单次采样**, 挑战者调用量立省 2/3 (每候选 4→2 次 LLM), 消除 shadow 引入的 +50% 成本;
- **翻转率高** (不稳定) → 提案 live 切 3 取众数 (行为变更, 单独批准), 并行化红利保留。
- **逾期未裁决 → 默认自动回退单次采样** (删除优先原则, 防成为 TIME_EXIT 审计 DEB 实证 6 的第 5 个"墓地影子": prompt_eval/shadow_technical_direction/playbook_direction_audit/edge_gate_shadow 四个停更无人知的前车之鉴)。

#### V5.3 移交项打包为一键补丁 (Qoder 执行, 2026-07-23)

用户指令"你来执行"后, Qoder 重试直写仍被 Auto 权限模式硬阻断
(/root/.hermes Edit 与 Bash 写均被 classifier 拦截, crontab 写同) — 权限边界不接受代理变通。
遂将全部已批准变更 (H0-H5.2, 共 10 处锚点 + 1 行 cron) 打包为:

**`scripts/apply_deb20260723_hermes_patch.py`** — 锚点逐一断言(≠1 即中止) / 改动前备份
(`.bak-DEB20260723`) / ast 语法校验 / 幂等跳过 / `--rollback` 一键恢复。

已在镜像副本 (data/_tmp_hermes_mirror, 已清理) 上完成验证: **10/10 锚点精确命中,
两 .py 文件 ast 通过, 复跑全部幂等跳过**。

用户/有权限方只需在非沙箱 shell 执行一条命令:
```bash
python3 /opt/ftseek/FTSeek-US/scripts/apply_deb20260723_hermes_patch.py --apply
# 回滚: 同脚本 --rollback
```
执行后验收: ① 次日 bridge JSON 含 `skill_version: "0.61"`; ② 弃权候选
`confidence_semantic=0.50` 而 `confidence=0.35`; ③ debate 段含
`challenger_skip_reason / challenger_model_reported / challenge_direction_samples`;
④ `crontab -l | grep "llm_universe_scan.py score"` 有行。

#### V5.4 补丁已执行 + Qoder 复核全绿 (2026-07-23)

用户执行 `--apply` 显示全部通过; Qoder 逐项只读复核:

| # | 验收点 | 证据 | 结果 |
|---|---|---|---|
| ① | SKILL.md version=0.61 + v0.61 changelog | frontmatter L3/L6 | ✅ |
| ② | pipeline.py 版本戳解析块 + bridge_data.skill_version | L406/412, L595 | ✅ |
| ③ | pipeline.py _semantic_confidence (WARN 分支) + cand.confidence_semantic | L460, L480 | ✅ |
| ④ | llm_synth.py _model_reported + skip_reason + debate 三键 | L63, L702, L789-791 | ✅ |
| ⑤ | llm_synth.py 三次采样 shadow (live 用第 1 次) | L746 | ✅ |
| ⑥ | crontab UE score 行 | `40 5 * * 2-6 ... llm_universe_scan.py score` | ✅ |

代码层验收完毕。**运行时验收 (次交易日自动产生)**: bridge JSON 实含
`skill_version: "0.61"` 与弃权候选 `confidence_semantic=0.50`; 明早 05:40 首条 cron 驱动
score 记录 (v2.1 首批 THESIS_ONLY 样本的 T+0 评分)。

---

## 决策 (共识稿, 用户已于第 5 轮确认)

**核心共识**: 两轮无根本分歧。分析方"先修结构性病灶、后改 prompt 文采"的方向排序经辩论方盲区核验后加强; 唯一实质修正是 B1 归因 (映射已于 07-18 修复, 真病灶在 tier 阶梯 + WARN 降权的叠加吹零) 与 P0.2 修点位置 (从"解耦映射"改为"让解耦在全链生效")。

- **选择方案** (分三档, 按风险/依赖排序):
  - **立即档 (P0, 零交易行为变更)**: ① UE `score` 接电 + emergence_streak 评分器 (P0.1); ② DA bridge 版本戳 (P3.8 升级 — N3 实证漂移窗口 3 处不可见变更, 这是归因能力地基, 须先于一切 skill 侧变更)。
  - **测量档 (先 shadow 后切)**: ③ 弃权语义全链修复 — record-only 双写 conf_semantic/conf_live, 测两周 (P0.2 重定义); ④ 挑战者强制 SKIP 理由 (保留 SKIP 权利, 勿强制表态)。
  - **数据开门档 (依赖样本积累)**: ⑤ tier 排序验证 (样本计数制 ≥30); ⑥ invalidation 加法式结构化 (DA + UE); ⑦ 下游消费结构化判定 (并入 postmortem per-trade 重建, 勿另开工程); ⑧ A/B 纪律成文。
- **裁决门槛 (防挪门柱)**:
  - P0.1 验收: score cron 上线且次日产出 ≥1 条 score 记录; emergence_streak 有评分器消费 (非仅推送文案)。
  - 版本戳验收: DA bridge JSON 含 skill_version 字段且与 SKILL.md 版本一致。
  - **弃权语义验收断言 (端到端, 非单点)**: 弃权单落库 conf_semantic=0.50 且 ≠ 真反向单 0.35 — 直接针对 N1 的"单点修复被下游吹零"病理。
  - **切 live 门槛**: conf_semantic shadow 满两周, 弃权 cohort 在 0.50 仓位下实绩不劣于 0.35 方可切 (防 N1 警告的"给亏损 cohort 反向加仓")。
  - tier 验证门槛: HARD_EVENT 前瞻收益显著 > THESIS_ONLY (≥30 样本, 符号检验)。
- **负责人**: 用户指定; Qoder 验证。
- **截止时间**: 立即档 ≤2026-07-25; 测量档自 shadow 上线起算两周; 数据开门档随样本自然达成。

## 执行项

- [x] **P0.1-a**: UE `score` 挂 cron (05:40 CST) — **已落地+复核 (V5.4)**, 明早 05:40 首跑。
- [x] **P0.1-b**: score 分层字段 (self_tier/emergence_streak_days/evidence_ref 随行落库) + report 双切片 + emergence_streak 评分器 — **已落地 (第 5 轮 V2)**, 13 条历史 scan 已回填评分, 兑现 07-22 埋点判分承诺。
- [x] **P0.2-版本戳 (升级 P0)**: DA bridge 输出加 skill_version + SKILL.md 0.58→0.61 — **已落地+复核 (V5.4)**; 运行时验收=次日 bridge 实含 "0.61"。
- [x] **P0.2-弃权语义**: record-only 双写 conf_semantic/conf_live — **已落地+复核 (V5.4)**; 验收断言待次日 bridge 实测: 弃权单 conf_semantic=0.50 ≠ 真反向 0.35。
- [x] **离线反事实双臂 (裁决 N3/N6 之争)** — **已执行 (第 4 轮, 见 N7)**: record-only 双臂重跑 TTE/E (零交易)。结论: **claim IC 正式洗清** — ① 效应量 ≤ 温度噪声 (同输入多轮挑战者方向 BUY/SKIP/SELL_SHORT 随机翻转); ② 微弱效应方向与归零所需因果相反 (含IC 更保守)。同时截断假设被证伪 (data_text<4000, claim IC 可见)。API 仅回显模型别名, 无法直接验证漂移。
- [x] **挑战者模型版本锁定 (N6 最可能根因的直接缓解)**: API 回显模型名落库 debate 段 — **已落地+复核 (V5.4)**; pin 版本号不可行 (DashScope 仅回显别名, N7 实证), 改为"别名变更即可观测"。
- [x] **挑战者采样稳定性 (N7 新发现, 优先级上调)**: **用户已批准方案 (b) 三次采样先 shadow (第 5 轮 V5.2)** — **已落地+复核 (V5.4)**, record-only, live 零变更; 一周后测翻转率再议众数切换。
- [x] **UE V2.1 "邀请 THESIS_ONLY" 通道 (用户批准, 第 5 轮 V5.1)** — **已落地**: SYSTEM_PROMPT_V2 双通道 + PROMPT_VERSION=v2.1; 主通道纪律与全部代码安全网不变; 今晚 21:10 scan 生效, 验证判据见 V5.1。
- [x] **P1.4**: 挑战者 prompt 强制 SKIP 理由 — **已落地+复核 (V5.4)**, 保留 SKIP 权利仅强制理由。
- [~] **P1.3**: tier 排序验证 — **首批数据已产出 (第 5 轮 V3)**: HARD_EVENT n=1 / MARKET_CORROBORATED n=14 (命中 0.714, +2.29%) / THESIS_ONLY n=0 (幸存者结构 caveat); 距 ≥30 去重样本门槛仍差约一半, 随 cron 每日评分自然积累。
- [ ] **P2.6**: DA invalidation 加法式结构化 (新增 invalidation_price 字段, 提取端优先读结构化、无则回退正则; 根治 COF 误报 + 27% 覆盖)。
- [ ] **P2.7**: UE 补 invalidation 字段 (UE 候选目前只有 thesis 无证伪条件)。
- [ ] **P1.5**: 下游改消费结构化判定 (DA=debate_outcome 枚举 / UE=self_tier), 并入 postmortem per-trade 评分重建。
- [x] **P3.9**: A/B 纪律成文 — **已落地 (第 5 轮 V2.4)**: `docs/specs/prompt-change-discipline-20260723.md` (版本戳前置 / 样本计数制 / 并行 shadow / INTC 原则推广 / conf 语义 shadow 双写)。


---

## 第 6 轮：出场侧逐单审计 (V6, Qoder, 2026-07-23) — 承接"砍出场不砍入场"执行

**数据**: `data/ftseek_us.db`, 2026-07-01~22, 109 笔已平仓 PLAYBOOK_SIM (复现 SQL 见下)。
**前置**: TIME_EXIT 审计 DEB 第 8 轮判决已读 — "TIME_EXIT 割早说"已被反事实证伪 (拿到收盘更差 -163/-279)。本轮独立复核并扩展。

### 6.1 修正声明 (自我证伪)

第 5 轮后 Qoder 曾基于旧窗口 (DA 62 单, w/l=0.74) 推断"赢单被出场政策提前腰斩、盈亏比泄漏在出场"。**7 月 109 笔数据证伪该推断**:

- 整体 w/l = 1.20 (50 赢 avg +33.0 / 59 亏 avg -27.5), PF 1.02, 净 +27.3 — 7 月出场侧无系统性盈亏比泄漏。
- 赢单 MFE 捕获率健康 (统一单位: pnl_pct 为小数, mfe_pct 为百分数): SL_TRAIL_LOCK 中位 **59.7%** / PRE_CLOSE_EXIT 66.7% / EOD_FORCE_CLOSE 92.6% / TAKE_PROFIT 100%。trailing 出场吃 6 成波段是设计行为, 非"砍早"。
- 旧窗口 62 单 w/l=0.74 的统计口径 (疑含 6 月) 标记为**待重算**, 不再作为论据。

### 6.2 真出血点: TIME_EXIT 23 笔全亏 -699.1 = "入场后从未被证实"队列

逐单画像 (`exit_reason='TIME_EXIT'`):

- **20/23 从进场到被砍 MFE < 0.3%** (0/23 曾达 1% 浮盈); 平均 MFE 0.16% vs 平均 MAE 1.16% — 方向从第一小时就错, 只是阴跌太慢够不到 SL, 由 4h 时钟收尾 (19/23 恰好 4h)。
- 结合第 8 轮反事实判决 (拿到收盘更差): **出场时钟处置正确, 病因在入场端** — 这批单"入场后从未被市场证实"。
- 对症药 = E 门 (60min MFE<0.3% 早砍, 已 record-only shadow): 恰好截断该队列 1h→4h 的磨血段。维持 E vs M 两臂裁决 ≤07-28, 本轮无新增异议。

### 6.3 TP 形同虚设 → 否决"RR 几何进代码打分"

- **109 笔仅 1 笔摸到 TP** (META +246.8); 真正盈利引擎 = SL_TRAIL_LOCK (17W/0L, +478.5, 全月最大盈利源)。
- LLM 给出的 TP/SL 几何与实盘可及性脱钩 (计划 RR 1.5~2, 实际 TP 到达率 <1%)。
- **自我否决**: Qoder 此前提议"代码打分特征含 entry/SL/TP 几何 (RR)" — 该特征基于不可及的 TP, 无现实锚点, 撤回。与"未验证特征不进打分器"原则一致。

### 6.4 失效价触发器的实证定位 (支撑 P2.6 维持 P0)

TIME_EXIT 队列即失效价触发器的理想猎物: 60min 时 MFE<0.3% 且 MAE>1% = "价格已证伪入场 thesis, 但 SL 未到、时钟未到"。E 门是统计规则 (MFE 小就砍), 失效价是因果规则 (证伪价破就砍) — 后者能覆盖"MFE 不小但已破证伪价"的漏网单。P2.6 与主 DEB 执行项 "DA prompt 强制 invalidation 含价位 (27%→90%)" 同一项, **维持 P0, 不再另立**。

### 6.5 脚注: BROKER_GHOST_CLOSE

10 笔 -62.2, 其中 07-06 七笔 holding=0h — 与 dual_broker 死连接同期 (7ebe672f 已移除)。非系统性出血, 列入观察: 若 07-22 后复发需单独立项。

### 6.6 复现 SQL

```sql
SELECT symbol,trade_date,direction,entry_price,exit_price,realized_pnl,pnl_pct,
       exit_reason,holding_hours,mfe_pct,mae_pct FROM trade_records
WHERE trade_date>='2026-07-01' AND source_type LIKE 'PLAYBOOK%' AND exit_time!='';
-- 单位: pnl_pct=小数(0.0311=3.11%), mfe_pct=百分数(3.72=3.72%) — 混用会低估捕获率100倍
```

### 6.7 对第 5 轮决策的净影响

- "先修出场"建议落地为: **出场引擎无大修需求** (trail-lock/EOD 健康, TIME_EXIT 处置正确), 出场侧唯一动作=等 E 门裁决 + P2.6 失效价。
- 优化重心回到入场端: ① 入场确认质量 (TIME_EXIT 队列的上游); ② 打分器继续等 shadow 数据, 且 RR 几何特征已排除。
