# 议题：DA 双 LLM 的「范围 / 输入 / 一致度 / 置信」四处接反 —— 测量提案（零交易行为变更）

> **2026-10-27 复核阅读指引**：§0–§10 保留原提案及执行记录，不应直接作为改门指令。§10.5为原作者追加复核；§11 对因果归因、指标分母和执行权责作更正；§12 记录用户已批准的第1—4项最小修复及第7项只读核查；§13 是未实施Prompt／并发方案的审查；§14 是C-A初步回放（REPLAY_LIMITED）；§15 是fresh Pass1三项验收（OFFLINE_CHECK_PASS_LIMITED）。其余Prompt／门／名单／并发建议仍未批准，不改变既有裁决或交易行为。

## 状态

- [x] 进行中（首轮及 §10 四项观测已落地；全链路复核见 §11；**第1—4项最小修复已实施，见 §12，待下一DA批活体核验**；后续行为方案待裁）
- [ ] 已决策
- [ ] 已验证
- [ ] 已关闭

| 项 | 值 |
|---|---|
| 类型 | 分析提案（纯测量 + 文档；**不改代码、不改 prompt、不改门槛**） |
| 立案日 | 2026-10-27（用户令「将分析和逻辑推理写入 DEB」） |
| 提出者 | Hermes（对 2026-10-26 生成轮全链路只读复核） |
| 待谁裁决 | 用户（§7 frontier F1–F5） |
| 数据窗 | 2026-10-19 ~ 2026-10-26（DA checkpoint / playbook_instructions / candidate_funnel / 生成轮日志） |
| 复跑入口 | `scripts/da_scope_confidence_audit.py`（本批入库，只读） |

---

## 0. 一句话结论

四件事各自都能跑，但**彼此接反了**：

1. **范围不由信号决定（已按 §8-7 降级口径）** —— 28 个分析名额里 18 个（64%）给了静态自选池（无上限、无排序），按 conviction 排序的动态信号池只有 7 个（25%）；分析名单**跨日重叠 19/28（68%）**、近 20 对平均 Jaccard **0.37** ⇒ 范围是「半锚定」：六成来自静态名单，其余由扫描器轮换；
2. **判据不在输入里** —— 下游用「空头排列 + BUY → 丢弃」判单，而 `空头排列` 在 prompt 里出现 **0 次**；当日 15 只空头排列标的 11 只给了 BUY，即 **39% 的分析预算产出被结构性丢弃**（该轴已立案于 DEB-20261008，本提案不重裁）；
3. **一致度不进置信** —— 10/23 的状态机语义修正（正确）之后，「同向 + PARTIAL_AGREE」不再算一致，而挑战者几乎恒给 PARTIAL ⇒ `confidence=70`（唯一能过 0.7 档）从 5 条/日掉到 **0 条/日**，置信阶梯整体下移一档，而门槛 55/70 未重标定；
4. **置信不进决策** —— 生成轮当日打印 6 条 `confidence=35 < 门槛=55 → SKIP`，但 **7 条被写入仲裁拒绝**（`现存行有源, 空 decision_id 写入不覆盖`），8 行 DB 全部存活并按 DA 方向进入盘中。**置信门对 DA 行零效力**，真正把关的是盘中三道门。

子目标（每条可用命令判真假）：

| # | 子目标 | 判据 |
|---|---|---|
| S1 | 能逐口说明「28 只怎么来的」 | E1–E4 复算一致 |
| S2 | 能说明「模型看到什么、没看到什么」 | E10/E11 计数一致 |
| S3 | 能说明「辩论改变了什么」 | E5 跨日状态×conf 表一致 |
| S4 | 能说明「置信门实际拦住了什么」 | E9 三方计数一致（gate_skip / 仲裁拒绝 / DB 存活） |
| S5 | 能说明「还能扩多少」 | E6/E7 + §4 N5 推算（**推算非实测**，已标注） |

---

## 1. 立案前强制检查（P0.5 撞车普查）

命令与原始输出：

```bash
grep -rln "破位硬买\|空头排列" docs/debates docs/decisions | head
# DEB-20260724-da-chain-audit.md / DEB-20261008-model-long-bias-decomposition.md
# DEB-20260907-prompt-contract-role-boundary-review.md / DEB-20260924-premarket-index-caliber-*
grep -rln "挑战者\|弃权率\|辩论一致度" docs/debates | head
# DEB-20260724-da-chain-audit.md / DEB-20260725-llm-failure-semantics.md / ...
grep -rln "min_confidence" docs/debates docs/decisions | head
# DEB-20260629-llm-direction-mode.md / DEB-20260816-fullchain-code-review.md / ...
```

| 既有文件 | 它覆盖什么 | 本提案**不重裁**什么 | 本提案依赖它哪一段 |
|---|---|---|---|
| `DEB-20261008-model-long-bias-decomposition.md` | 77% 多头基本盘分解：bearish→BUY 62%、挑战者无纠偏、四方案（prompt 对称化/空头专线/接受+下游补偿/只观测）待裁、§6 待验证「抄底臂成绩」 | **不重开**「空头排列→BUY 该怎么办」这个议题；不重复提 prompt 对称化方案 | 本提案 N2 只补两条它没有的量：①当日预算成本（11/28 = 39%）②其 §6 的验证需要「按 conf/state 分层」的尺子，而这把尺子已被 N3 打坏 |
| `DEB-20260724-da-chain-audit.md` | DA 链路（提名→bridge→playbook）早期审计 | 链路连通性结论 | 仅作背景 |
| `DEB-20260907-prompt-contract-role-boundary-review.md` | prompt 契约与角色边界 | prompt 文本层 | N2「判据未进 prompt」的证据互补 |
| `DEB-20261023-*`（admission / 根因修复） | DA 执行前 admission、载体/finalize 治理 | 不重裁 | N5 与 finalize 降级有关 |

**唯一增量（本提案的全部主张）**：N1 范围预算倒挂、N3 一致度阶梯失效、N4 置信门被仲裁绕过、N5 容量天花板。N2 只做「预算成本量化」，不提案。

---

## 2. 非目标

- 不改 `signal_gate.py` 任何判据（破位硬买门、择时记录、tier 映射）。
- 不改 prompt（属 N2 既有 DEB 的方案 A/B，需先裁）。
- 不改 `min_confidence` / `supply_conf_gate_*` / `top_n` / `_UE_FEED_CAP` 任何数值。
- 不动 `save_instruction` 的覆盖仲裁优先级（属 N4 方案 A，风险最高）。
- 不追溯重写历史行（历史 `playbook_instructions` 保持原样）。

---

## 3. 证据表（每行作者已实跑；命令可整段复制）

工作目录 `/opt/ftseek/FTSeek-US`，解释器 `./venv/bin/python`。

| 编号 | 声称 | 复现命令 | 实测输出（逐字） |
|---|---|---|---|
| E1 | 分析集由四个入口拼成，规模 67 行 screened | `./venv/bin/python scripts/da_scope_confidence_audit.py funnel` | `stage={'da_main_pool': 16, 'da_extra_watchlist': 43, 'da_ue_feed': 3, 'da_premarket_mover': 5}` |
| E2 | 28 只入选 = 7+18+1+2 | 同上 | `selection={'selected': 28, 'rejected_ineligible': 31, 'budget_cut': 4, 'duplicate': 4}` |
| E3 | 资格门拒了 31 只，主因低 ATR | 同上 | `reasons={'low_atr': 30, 'price_floor': 1}` |
| E4 | 自选池占 18/28 席位且无上限，信号池仅 7/28 | 同上 | `{('da_extra_watchlist','selected'): 18, ('da_main_pool','selected'): 7, ('da_ue_feed','selected'): 1, ('da_premarket_mover','selected'): 2}` |
| E5 | 10/23 语义修正后 AGREE 归零、conf=70 灭绝 | `./venv/bin/python scripts/da_scope_confidence_audit.py debate --since 2026-10-19` | `2026-10-22 n=28 states={'ANALYST_SKIP': 9, 'PARTIAL_AGREE': 13, 'ABSTAIN': 4, 'AGREE': 2} agreed=16 conf={35: 8, 50: 2, 70: 5}` / `2026-10-23 n=31 ... agreed=2 conf={35: 13, 70: 1, 50: 1}` / `2026-10-26 n=28 states={'PARTIAL_AGREE': 13, 'ABSTAIN': 6, 'ANALYST_SKIP': 9} agreed=0 conf={35: 7, 50: 1}` |
| E6 | 生成轮前 18 分钟零 checkpoint | `./venv/bin/python scripts/da_scope_confidence_audit.py capacity --date 2026-10-26` | `checkpoints=28 first=20:19:03 US.PBR.A.json last=20:39:07 US.NU.json` + `heartbeat_analyses_done=[0 ×18, 2, 2, 2, 2, 2, 9, ..., 27]` |
| E7 | 单标分析师侧延迟 p50≈72s | 同上 | `analyst_latency_ms n=28 p50=72273 p90=90665 max=95676` |
| E8 | 空头排列 15 只中 11 只给 BUY | `./venv/bin/python scripts/da_scope_confidence_audit.py trend --date 2026-10-26` | `空头排列 BUY -> 11` / `空头排列 SELL_SHORT -> 3` / `空头排列 SKIP -> 1` |
| E9 | 置信门对 DA 行零效力：6 打印 SKIP / 7 仲裁拒绝 / 8 行存活 | `./venv/bin/python scripts/da_scope_confidence_audit.py confgate --date 2026-10-26 --log logs/ftseek_us.log-20261027` | `db_da_rows=8  conf_gate_skip=6  arbitration_refused=7` + 逐行 `US.VZ db_dir=SKIP conf=50 did=DA-20261026202352 gate_skip=- refused=-` |
| E10 | prompt 全文不含「空头排列」 | `grep -c "空头排列" /root/.hermes/skills/deep-stock-analysis/scripts/llm_synth.py` | `0` |
| E11 | 判据只活在门代码里 | `grep -c "空头排列" /root/.hermes/skills/deep-stock-analysis/scripts/signal_gate.py` | `3` |
| E12 | 门与写入是两条路径（行由 bridge 先写） | `./venv/bin/python -c "import sqlite3;c=sqlite3.connect('file:data/ftseek_us.db?mode=ro',uri=True);[print(r) for r in c.execute(\"select symbol,direction,confidence,decision_id,created_at from playbook_instructions where date='2026-10-26' and model_used!='deterministic' order by symbol\")]"` | 8 行全部 `decision_id LIKE 'DA-%'`、`created_at=2026-10-26T12:39:07`（= 08:39 ET，早于 09:06 生成轮） |
| E13 | 分析名单跨日半锚定（N1 降级依据） | `./venv/bin/python scripts/da_scope_confidence_audit.py overlap` | `2026-10-26 \| 28 \| 64.3% \| 0.47 \| 19/28` + `mean_jaccard_last20=0.37` |
| E14 | 仲裁留痕已接线且不改行为 | `./venv/bin/python -m pytest tests/test_playbook_instruction_arbitration_shadow.py -q` | `3 passed`（含「拒绝仍返回现存行 id」「正常写入零留痕」「四处拒绝点均接管」） |
| E15 | carrier 心跳新增阶段字段且双副本一致 | `sha256sum ops/hermes/da_main_carrier.py /root/.hermes/scripts/da_main_carrier.py` | `2 617bc28c1ed416aa7c0d35f322d3de918c8b2350a48f9026ae456c35d1f82fcc`（两行同值） |
| E16 | 留痕产物尚未产生（落地≠活体） | `./venv/bin/python scripts/da_scope_confidence_audit.py arbitration` | `no arbitration shadow yet: /opt/ftseek/FTSeek-US/data/shadow/instruction_arbitration.jsonl` |

---

## 4. 逻辑推理链（每段：观测 → 推理 → **反例判据**）

### N1 范围：预算层级倒挂

- **观测**：E1/E2/E4。主池 `_select_eligible(_pool_screened, top_n + 2)` ⇒ 7 席（`pipeline.py:1235`）；自选池 extras `_select_eligible(_extra_screened, len(_extra_rows))` ⇒ **无上限**（`pipeline.py:1247`）。
- **推理**：唯一带 conviction 排序的通道配额最小，不做排序的通道配额等于候选数 ⇒ 分析集的构成**主要由静态名单决定**，信号强度只决定「前 7 名额归谁」。
- **实测补正（E13，2026-10-27 落地打点时补）**：分析名单跨日重叠 **19/28（68%）**、近 20 对平均 Jaccard **0.37**（`mean_jaccard_last20=0.37`）。⇒ 「主要由静态名单决定」应降级为「**半锚定**」：当日 2/3 的名字与昨日重复，但整体集合仍有三成以上轮换。**按本 DEB 预注册的反例判据（Jaccard < 0.50 即降级），本条已降级**，见 §8-7。
- **反例判据**：若跨日分析名单重叠率 < 50%，则「由静态名单决定」不成立（说明扫描器侧贡献了大部分名单）。**该判据已实跑，结论为降级**（E13）。

### N2 输入：判据没进 prompt（归 DEB-20261008，本段只量化预算成本）

- **观测**：E10 = 0 vs E11 = 3；E8 显示 15 只空头排列里 11 只给了 BUY；而这 11 只在 DA 侧即被 G1 门丢弃（`da_gate_decisions.jsonl` 当日 28 行，11 行含 `破位硬买拦截`）。
- **推理**：模型看到的是 MA5/10/20/60 与 5/20 日涨跌（`llm_synth.py:597`），足以自己推出空头排列；但 prompt 从不告诉它「空头排列 + BUY 会被丢弃」。信息在、规则不在 ⇒ 模型没有动机去避开该输出 ⇒ 11/28 = 39% 的 LLM 预算产出结构性作废。这不是「模型判断错」，是**输入与判据不对称**。
- **反例判据**：若 SYSTEM_PROMPT 全文存在等价表述（如「趋势向下应给 SKIP 或 SELL_SHORT」）且当日分析引用了它，则「不知情」不成立。复查者 grep `llm_synth.py` 的 `SYSTEM_PROMPT` 段（约 1300–1410 行）逐句核。
- **边界**：本条与 DEB-20261008 的 A/B/C/D 四方案**同一根因**；本提案只补「预算成本」这一量，不提议改 prompt。

### N3 一致度：语义修正后阶梯失效（**本条是本提案最重要的新发现**）

- **观测**：E5。`challenger_agreed=True` 从 10-22 的 16/28 → 10-23 的 2/31 → 10-26 的 **0/28**；DB `confidence=70` 从 5 → 1 → **0**；同期 verdict 分布基本稳定（PARTIAL_AGREE 常年占 60–70%）。
- **推理**（三步）：
  1. 10-23 引入 `debate_state.classify_debate`，把「同向 + PARTIAL_AGREE」从 `agreed` 里剔除（此前 `raw_agreed or verdict=="AGREE"` 会把 16 条算一致）。这是**正确修复**：`PARTIAL_AGREE` 带硬伤，不该给一致加成。
  2. 但 `_compute_confidence` 的 `+0.20` 分支条件仍是「`challenger_agreed` 为真」⇒ 修复后该分支只在 `state == AGREE` 时触发，而 AGREE 要求 **同向 + verdict=AGREE + 分析师非 SKIP**。实测当日 8 条 `verdict=AGREE` **全部是分析师先 SKIP** 的样本（状态机优先级 1 判 `ANALYST_SKIP`），而 13 条真正同向的样本全是 `PARTIAL_AGREE` ⇒ **可用 AGREE = 0**。
  3. 于是 conf 阶梯被整体下移一档（70 → 50/35），而下游门槛未动：`BUY/SHORT_min_confidence=70`（strategy_registry 默认）、夏季放宽的兜底 55 仍在 `pipeline.py:1626`。**阶梯与门槛错配**：结构上 8 条 DA 供给里 7 条落在 35，永远低于门槛。
- **反例判据**：若 10-23 之后仍有 ≥3 条/日的 `state=AGREE`，则「阶梯失效」不成立（只需重标定门槛）；复查者按 E5 命令核对 `AGREE` 列。
- **注意**：`verdict=AGREE` 的 8 条都落在不交易的 SKIP 样本上，所以**对当日供给无直接影响**；影响的是「以 conf 分层的任何统计」和「未来若把门槛当真」。

### N4 决策：置信门被写入仲裁整体绕过（Pfailure：门是装饰）

- **观测**：E9 + E12。
  - 09:06–09:07 生成轮打印 6 条 `confidence=35 < 门槛=55 → SKIP`（AMD/CAT/CRM/PANW/TSLA/UNH），另 1 条（COIN）被「方向条件化 IC 冲突」判 SKIP；
  - 同窗口 **7 条** `覆盖仲裁拒绝: <sym> (2026-10-26) 现存行有源, 空 decision_id 写入不覆盖`；
  - DB 8 行全部存活、方向为 DA 原方向（CAT=SELL_SHORT 实成交）；行由 bridge 在 **08:39 ET** 写入（E12），早于生成轮 27 分钟。
- **推理**：`save_instruction` 的仲裁比较 `_prio(新 did)` 与现存行优先级（`db.py:563`），DA 行 `DA-…` 优先级高于生成轮的空 `decision_id` ⇒ 生成轮的任何 SKIP 都无法落地。**门的判决被写入层否决**，保护完全来自盘中门（VWAP/追价帽/2 分钟确认，当日 7 条被它们拦下）。
- **三个后果**（按危险程度）：
  1. **脆弱**：若哪天仲裁优先级被调高（或生成轮改用高优先级 did），全部 DA 供给会因 conf 35<55 一夜归零；日志上看不出这是「conf 语义错配」，会被误判成「模型变差」。
  2. **误导**：日志写着「SKIP」，复盘会以为门在拦；实际 0/7 生效。这与既有的「死门」家族同型。
  3. **口径污染**：`confidence` 字段同时指三样东西 —— DA 侧 `_compute_confidence`（辩论一致度 0.35/0.50/0.70）、playbook 侧 `compute_confidence`（数据质量分 = ok_count×12 + 条件×28，上限 90）、以及门槛 55/70 假设的「信号质量」。任何按 conf 分层的统计都在这三者之间漂移。
- **反例判据**：若 DB 行的 `created_at` 晚于生成轮时刻、或 `decision_id` 非 `DA-` 前缀，则「bridge 先写 + 仲裁拒绝」不成立。复查者按 E12/E9 核。
- **边界（口径混用的旁证）**：VZ 当日 `confidence=50`，理论上也低于 55，但日志**没有**它的 `门槛=` 行 —— 说明 `_min_conf_for_dir` 按 `strategy_id` 从 registry `direction_filter` 取值（默认 70 / 兜底 55，见 `pipeline.py:1620-1637`），**「55」不是全局阈值**。同一个 `confidence` 数字在不同的 strategy 路由下对着不同门槛，这本身是 N4 第三条后果（三语义混用）的又一层：门槛也不是单一真源。

### N5 容量：天花板 ≈35–41 只，超顶静默降级（**推算，非实测**）

- **观测**：E6（carrier 08:00:42 启动 → 首条 checkpoint 08:19:03 → 末条 08:39:07，28 只）+ E7（p50 72s）。
- **推算**：总耗时 38.4 min / 28 只 ⇒ 1.37 min/只（含采集与 5 并发排队）。deadline 08:57 ⇒ 预算 56.3 min ⇒ 上限 ≈41 只；扣掉启动/pullback 开销，保守 **35–40 只**。今日 28 只 ≈ 75% 占用。
- **第二个风险**：前 18 分钟没有任何持久化 ⇒ 该窗口内崩溃 = 整批归零（checkpoint 原子性只覆盖后半程）。
- **超顶行为**：`_deadline_seconds` 到期 → carrier 调 `pipeline --mode finalize` 从已有 checkpoint 出 bridge，**返回 0 并打印成功**，只是候选变少 ⇒ 静默降级。
- **反例判据**：若 `DA_MAX_WORKERS` 实际生效值 >5 或 pullback 与采集并行，上限上移，本推算需重算（变量在 `pipeline.py:1591` 与 carrier `main()` 顺序）。

---

## 5. 方案（只列，不实施；每项标注是否改变交易行为）

| 编号 | 方案 | 行为变更? | 代价/风险 | 推荐 |
|---|---|---|---|---|
| N1-A | 给 extras 设上限（如 `top_n+2` 同额），让自选池也进入 conviction 排序 | **是** | 自选池是人工 curation，削它=改动策略范围；需影子 + 预注册 | 否（先测 N1-B） |
| N1-B | record-only：每日记录「分析名单跨日重叠率」与「extras 占比」 | 否 | 零 | **是（立即）** |
| N2 | 归 DEB-20261008（A/B/C/D 已列），本提案不重复 | — | — | 由该 DEB 裁 |
| N3-A | 重标定置信阶梯（把 0.35/0.50/0.70 与 55/70 门槛对齐） | **是** | 直接改供给量；须影子 + 预注册 | 待裁 |
| N3-B | 为「同向 + PARTIAL + 无 falsification 命中」开一档中间值（如 0.60） | **是** | 需要「无硬伤」的可判定定义，否则退回 10/22 的老毛病 | 影子先行 |
| N3-C | record-only：报表按 `state` 分列打印 + 标注「conf=70 自 10/23 起为 0」 | 否 | 零 | **是（立即）** |
| N4-A | 让生成轮 SKIP 能覆盖 DA 行 | **是（危险）** | 等于把 DA 供给交给「数据质量分」，供给可能归零 | 否 |
| N4-B | DA 行落库时就写 `debate_conf` 并单列字段，门槛按该语义 | **是** | 需迁移历史行口径 + 门槛重标 | 待裁（与 N3-A 合并处理更合理） |
| N4-C | record-only：日志区分「门判 SKIP / 被仲裁拒绝」，并加计数遥测 | 否 | 零 | **是（立即）** |
| N5-A | 提并发（5→8） | 否（但影响 API 限流与延迟） | 限流 429 风险；须实测 | 待裁 |
| N5-B | 「距 deadline <10min 未完成」告警 + 阶段化进度（采集/分析/落盘） | 否 | 零 | **是（立即）** |
| N5-C | pullback 与 pipeline 并行（当前串行最多 300s） | 否 | 需确认无共享 OpenD 连接竞争 | 待裁 |

**最小可落地集（零行为变更）**：N1-B + N3-C + N4-C + N5-B，四项都是打点/日志/报表，不触碰任何判据。

---

## 6. 复查清单（请第二 agent 逐条打假）

| 编号 | 声称 | 复查命令 | 反例判据（什么输出说明我错） |
|---|---|---|---|
| C1 | 自选池 18 席、信号池 7 席、无上限 | `./venv/bin/python scripts/da_scope_confidence_audit.py funnel` | 若 `da_extra_watchlist selected` ≤7 或存在 extras 上限代码（grep `len(_extra_rows)`）⇒ N1 不成立 |
| C2 | 10/23 后 `state=AGREE` 归零 | `./venv/bin/python scripts/da_scope_confidence_audit.py debate --since 2026-10-19` | 若 10-23/10-26 的 `AGREE` ≥3 ⇒ N3 不成立 |
| C3 | conf=70 从 5 条/日 → 0 条/日 | 同上（最后一列 DB conf） | 若 10-26 存在 `70:` ⇒ N3 的「阶梯灭绝」不成立 |
| C4 | 置信门对 DA 行 0 生效 | `./venv/bin/python scripts/da_scope_confidence_audit.py confgate --date 2026-10-26 --log logs/ftseek_us.log-20261027` | 若某 DA 行 DB 方向被改成 SKIP 且 `did` 为空（即生成轮写入成功）⇒ N4 不成立 |
| C5 | 8 行由 bridge 先写（早 27 分钟） | E12 的 SQL 输出 `created_at` | 若 `created_at` ≥ 09:06 ET ⇒ 顺序假设错 |
| C6 | prompt 不含该判据 | `grep -c "空头排列" /root/.hermes/skills/deep-stock-analysis/scripts/llm_synth.py` | 若非 0 ⇒ N2 的「不知情」不成立 |
| C7 | 前 18 分钟零 checkpoint | `./venv/bin/python scripts/da_scope_confidence_audit.py capacity --date 2026-10-26` | 若 heartbeat 序列前段出现 >0 ⇒ N5 的原子性缺口不成立 |
| C8 | 容量上限 ≈35–41（推算） | 同上 + `grep -n "DA_MAX_WORKERS" /root/.hermes/skills/deep-stock-analysis/scripts/pipeline.py` | 若并发实测 >5 或并行化了 pullback ⇒ 推算需上修（本条本就标注为推算） |

**请复查者重点挑战三点**：①N1 的结论依赖「跨日名单重叠率高」，而该重叠率我**没有实测**（若实测 <50%，N1 应降级为「配额结构不均」而非「范围由静态名单决定」）；②N3 把 `+0.20` 灭绝归因于 10/23 语义修正，需确认同期没有换模型/改挑战者 prompt（若有，属混杂变量）；③N4 的「装饰门」判断只基于单日 7 条仲裁拒绝，应扩到 ≥5 个交易日核 `覆盖仲裁拒绝` 计数是否稳定。

---

## 7. 待裁问题（frontier）

| 编号 | 问题 | 推荐 | 理由 |
|---|---|---|---|
| F1 | 是否先落地零行为四项（N1-B/N3-C/N4-C/N5-B） | **是** | 都是打点/日志，不触碰判据；其中 N4-C 能把「装饰门」变成可见 |
| F2 | conf 字段拆分（`debate_conf` / `data_quality` 分列）是否立项 | **是，但合批** | 与 N3-A 门槛重标定同批做，否则拆了字段门槛还是错的 |
| F3 | N3-B（PARTIAL 中间档）是否进影子 | 待 F2 之后 | 需要「无硬伤」的可判定定义，先定再跑 |
| F4 | N1-A（给自选池设上限）是否立项 | **先测 N1-B 再议** | 重叠率未实测，现在就削等于用假设改策略范围 |
| F5 | N5-A（并发 5→8）是否试验 | 待 N5-B 告警上线后 | 先有可见度，再谈提速；429 是主要风险 |

---

## 8. 自曝 / 更正记录

1. **本提案推翻了本会话早前的一个判断**：上一轮我据日志写「7 条被开盘风控门合规拦下」，隐含着「生成侧置信门也在拦」。实测（E9）**置信门 0 生效**，只有盘中门与 DA 侧 G1 门在拦。原说法作废，更正为「生成侧 conf 门对 DA 行被仲裁否决；实际保护来自盘中三道门」。
2. **同一会话的另一处更正**（已由 RFC-172 落地）：曾把 55 只占位统一归因为「bridge 未提名」，实际 11 只是 DA 有方向但被 G1 门 BLOCK、8 只是 DA 自身 SKIP、36 只从未评分。归因分桶已写入 `pipeline.py:_da_gate_buckets`。
3. **N1 的关键前提未实测**：跨日分析名单重叠率、`extras` 逐日构成变化，本提案只用单日漏斗说明「配额结构不均」。把它读成「范围由静态名单决定」需要 C1 命令之外的重叠率实测 —— 未做，如实标注。
4. **N5 上限是推算**：由「38.4 min / 28 只」外推，非实测；并发实际生效值、pullback 实际耗时尚需实测。
5. **N2 不重裁**：`空头排列→BUY` 的处理方案属 DEB-20261008，本提案不提供第五个方案，避免形成第二套判据。
6. **未做的事**：没有改任何代码/prompt/门槛；没有重启服务；本提案的两个脚本（复算器）只读 DB 与产物。
7. **N1 被自己的实测部分证伪（2026-10-27 落地时补）**：原文写「分析集的构成**主要由静态名单**决定」，并按预注册反例判据承诺「重叠率 < 50% 则降级」。落地打点实测 E13：跨日重叠 19/28、平均 Jaccard **0.37 < 0.50** ⇒ **按预注册判据降级**为「半锚定」（六成名字重复，集合仍有三成以上轮换）。§0-1 与 §4-N1 已就地改并留指针，本条为原说法与降级依据。
8. **落地过程中发现的旁支**：`save_instruction` 内部的 admission 预判会**改写入参**（实测把 `$100×1` 的指令改成 `direction=SKIP` + `skip_reason=admission: NOTIONAL_TOO_SMALL(100<2500)`），即「写库函数」本身也是判据修改点之一。这不在本提案范围（属 admission 域），但会让「测试用的合成指令」被静默降级 —— 本批测试因此改用 ≥$2500 的名义额。登记备查，不提案。

---

## 9. 复跑入口（复查者最小动作）

```bash
cd /opt/ftseek/FTSeek-US
./venv/bin/python scripts/da_scope_confidence_audit.py funnel
./venv/bin/python scripts/da_scope_confidence_audit.py debate --since 2026-10-19
./venv/bin/python scripts/da_scope_confidence_audit.py trend --date 2026-10-26
./venv/bin/python scripts/da_scope_confidence_audit.py capacity --date 2026-10-26
./venv/bin/python scripts/da_scope_confidence_audit.py confgate --date 2026-10-26 --log logs/ftseek_us.log-20261027
```

约束：全部只读（sqlite `mode=ro`、json 读、日志读），零写入零下单；日志路径按日期传入（轮转后文件名会变）。
---

## 10. 执行记录（追加，不改上文提案与判据）

### 10.1 本批落地：四项**零交易行为变更**（2026-10-27）

| 项 | 落地形态 | 文件 | 是否改判据 | 验证 |
|---|---|---|---|---|
| N4-C | 覆盖仲裁留痕：四个拒绝点（`ue_vs_da` / `lower_priority` / `stale_did` / `empty_did_vs_sourced`）各落一行 `data/shadow/instruction_arbitration.jsonl`，字段 `{ts,date,symbol,reason,attempted,kept}`；**返回值与仲裁阶梯逐字不变**，写失败仅 debug | `app/playbook/db.py` | 否 | E14（3 passed，含「拒绝仍返回现存行 id」「正常写入零留痕」）+ 源码契约（四处均接管） |
| N5-B | carrier 心跳追加 `remaining_sec`；新增 `first_checkpoint` 一次性事件（首个 checkpoint 落地时刻）与 `deadline_approaching`（剩余 ≤600s 仍未完成时一次） | `ops/hermes/da_main_carrier.py` + 线上副本 `/root/.hermes/scripts/da_main_carrier.py` | 否（不改任何等待/超时/截止行为） | E15（双副本 sha256 同值）+ `tests/test_ops_scripts_sync.py` 3 passed |
| N1-B | 分析名单跨日重叠率 + 自选池席位占比：以只读子命令实现（**不注入生产热路径**），把「半锚定」变成可日更读数 | `scripts/da_scope_confidence_audit.py overlap` | 否 | E13（实跑得 19/28、Jaccard 0.37），并据此把 N1 降级（§8-7） |
| N3-C | 辩论状态分列读数 + 「conf=70 自 10/23 起为 0」标注：同脚本 `debate` 子命令按 `state` 与 DB conf 分列打印 | `scripts/da_scope_confidence_audit.py debate` | 否 | E5（六日序列，含 10-23 后 AGREE=2→0、70=1→0） |

**为什么不把 N1-B/N3-C 注入生产**：两者都只需要「读取已有产物」，注入生成热路径会扩大 DA 关键路径（当前已 38.4 min/28 只，见 N5），收益为零。只读子命令同样满足「可日更」——由复查者/日报调用即可。

### 10.2 部署与活体状态（Gate 6 三态纪律）

- 服务已重启（`systemctl restart ftseek-us`，2026-10-27 08:01:49 CST 启动，晚于代码 mtime），启动后 **0 ERROR/CRITICAL/Traceback**；持仓对账正常（`US.MSFT` 9 股在册）。
- `instruction_arbitration.jsonl` **当前尚未产生**（E16）⇒ 按 Gate 6 记 **TOO_EARLY（不可判）**，不是「接线死」也不是「已生效」。
  首个可判时点 = 下一次 DA 生成轮（ET 09:0x）之后的 `save_instruction` 仲裁拒绝；届时用
  `./venv/bin/python scripts/da_scope_confidence_audit.py arbitration --date <YYYY-MM-DD>` 判读：
  `rows>0` = LIVE；`rows=0` 且当日确有「生成轮 SKIP + DA 行并存」= 接线死，排查序为
  ①进程启动时刻晚于 db.py mtime ②`grep -c _emit_arbitration app/playbook/db.py` ≥4 ③目录 `data/shadow/` 可写。
- carrier 心跳新字段同理：下一次生成轮后 `grep '"status": "first_checkpoint"' data/shadow/da_main_schedule.jsonl` 应有行。

### 10.3 回滚（逐项独立，均一行级）

| 项 | 回滚法子 | 回滚触发条件 |
|---|---|---|
| N4-C | 删 `db.py` 的 `_emit_arbitration` 定义与四处调用（或 `git revert` 该提交） | 若留痕写失败开始影响入库（日志出现 `仲裁留痕失败` 且有指令未落库） |
| N5-B | 心跳还原本函数（删 3 个事件/字段） | 若 carrier 因新事件写盘异常退出，或 `da_main_schedule.jsonl` 增长过快（>10x） |
| N1-B/N3-C | 删两个子命令（不影响任何生产路径） | 无（只读） |

### 10.4 仍未落地（等裁决）

F2（conf 字段拆分）、F3（PARTIAL 中间档）、F4（自选池上限）、F5（并发 5→8）四项均**改变供给或延迟**，不在本批；其中 F2 是本批四项读数能真正「接上决策」的前提。

---

---

## 10.5 复核后更正（2026-10-27，原作者自查 + 逐条实跑）

§11 的外部复核提出九条更正。逐条复跑后：**五条成立、须更正**，两条口径不成立但结论一致，其余作为边界登记。以下为原作者结论，均附复现命令；§0–§10 原读数不删，供追溯。

| 编号 | 原说法（§位置） | 复核结论 | 更正后口径 |
|---|---|---|---|
| K1 | N1「分析集主要由静态名单决定」，§8-7 称「已按预注册判据（Jaccard<0.50）证伪」（§4-N1 / §8-7） | **原判据未指定指标，不成立**。同一窗口：当日留存 19/28=0.68（>0.50，**支持** N1）、Jaccard 0.37–0.47（<0.50，**反对** N1）。指标是结果出来后选的，等于事后择尺 | N1 判 **UNRESOLVED**：只承认「配额结构已测（自选池 18/28 席）」，不承认「价值更低」，也不据此砍名单 |
| K2 | N2 依据「`空头排列` 在 `llm_synth.py` 出现 0 次」推「模型不知下游规则」（E6 / §0-2） | **字面计数不能证明语义缺失**。`llm_synth.py:244-259` 明写「接飞刀拦截/破位硬买拦截/追涨降级由执行层统一执行…你只需按证据给方向与价位」，并给出飞刀几何一致性约束 | 撤回「模型不知规则」。保留的真问题（须另证）：Prompt 承诺「价格回升出拦截区后自动复评」，而 G1 拦下的 BUY 根本不进 DB，故「复评」是否存在于执行链未有证据 |
| K3 | N4「confidence 一字段三义，DA 行会被生成侧数据质量分接管」（§8 因果链第 3 环） | **错**。`instruction_builder.py:294-305` 对 DA 行直接 `final_confidence×100`，不经 `compute_confidence`。数据质量语义只存在于非 bridge（VOTE）行 | 改为「同名 `confidence` 跨来源两种语义（DA=辩论派生；VOTE=数据质量派生），DA 内部另有两份同算法实现（见 E17）」 |
| K4 | N5「前 18 分钟无任何持久化，该窗口崩溃=整批归零」（§5-N5 与 DEB 结论） | **错**。`US.PBR.A.raw.json.bak` 的 `saved_at=2026-10-26T12:15:56Z`（08:15:56 ET），早于首条 analysis 08:19:03；模型调用前已落 raw | 改为「首个**完整 analysis** 较晚（18 分钟）」，并分列 raw 可恢复性 vs analysis 可发布性 |
| K5 | §0「conf=70 自 10-23 起归零」 | **错**。10-23 仍有 conf=70 一条（见 E5）。归零发生在 10-26 | 改为「conf=70 在 10-26 为 0」 |
| K6 | N4「门槛非单一真源」（VZ 无门槛日志⇒阈值例外） | **推断不成立**（`bridge_adapters.py:129-131` 对非 LONG/SHORT 返回 None，VZ 早已是 SKIP，故无门槛日志属正常），但**结论方向正确且更强**，见 E17 | 保留结论，换证据 |

### E17（新增实测）：同算法两份实现，在 6/28 分析上给出不同 conf，且全部跨越 55 门槛两侧

```bash
# 对每个 checkpoint 同时按 adapter._compute_confidence 与 signal_gate.py:377-407 复算
# adapter 弃权分支: pass（保持 0.50）;  gate 弃权分支: max(0.50, score/10)
```

| 标的 | 方向/挑战者 | score | adapter | gate | 差 0.55 门槛 |
|---|---|---|---|---|---|
| ARM | BUY/SKIP | 8.0 | 0.50 | **0.80** | 翻转 |
| BWXT | BUY/SKIP | 8.0 | 0.50 | **0.80** | 翻转 |
| T | BUY/SKIP | 8.0 | 0.50 | **0.80** | 翻转 |
| BABA | BUY/SKIP | 6.0 | 0.50 | **0.60** | 翻转 |
| SNDK | BUY/SKIP | 6.0 | 0.50 | **0.60** | 翻转 |
| VZ | SELL_SHORT/SKIP | 6.0 | 0.50 | **0.60** | 翻转 |

**含义**：28 只里有 6 只（21%）的「是否够 55」取决于读哪份实现。今日对 DB 行的实际影响为 0（这 6 只均未成为 DA 行），但这是**结构性暴露**：任何一处把 gate 侧值当门槛输入，这 6 只即从 SKIP 变可执行。两份实现的唯一差别是 gate 的弃权分支多了 `max(conf, score/10)` 抬升（`signal_gate.py:399-401`），adapter 同步注释写着「算法与 adapter 完全一致」——不一致。

### 经复核后**加强**的部分（原 N3/N4 核心成立，因果链更精确）

原 N3「一致度不进置信」、N4「门对 DA 行零效力」成立，且因果链可闭合：

1. `challenger_agreed=True` 今日 **0/28**（`debate` 子命令），AGREE 状态 0/28；
2. `+0.20` 只在「方向一致 且（agreed 或 outcome 含 AGREE）且非 fallback」时给（`adapter.py:210-215`）⇒ 今日从未触发 ⇒ adapter conf 恒 **0.50**；
3. 0.50 → tier「中」→ `WARN` → 落库前 **−0.15**（`pipeline.py:2129-2144`）⇒ **0.35**，与 DB 的 7 行 35 逐字一致；
4. 门槛 55/70 ⇒ 0.35 结构性不可达；唯一到达 0.70 的路径是 AGREE 且 `|Δscore|≤3`（差值 >3 会被衰减到 0.70 以下）。

**结论**：不是「门槛设得太高」，而是 **+0.20 的加成本身不可达**（上游 `challenger_agreed` 恒 False），使全部供给压在同一档 0.35。修门槛属治症；要先回答「AGREE 为何不可达」（挑战者判据冲突已在 §11 指出），本提案不擅自改门。

### 连带更正：§10.1 字段清单

`db.py` 的仲裁留痕记录实际字段为 `{ts,date,symbol,stage,write_outcome,reason,attempted{did,direction,confidence,skip_reason,id},kept{same}}`（§12 批追加 `stage`/`write_outcome`/`skip_reason`/`id`）。§10.1 表格里的四字段描述已过期，以本节为准；相关断言由 `tests/test_playbook_instruction_arbitration_shadow.py` 4 条新增用例覆盖（含「已执行不被改写」「留痕失败不改写入结果」）。

## 11. 全链路专业复核：先修权责与证据，不按低置信读数调门（2026-10-27）

### 11.1 范围、结论与证据等级

**本轮授权：检查 DEB、分析并优化指令与方向。交付仅为本文和 handoff 更新；不修改生产代码、Prompt、配置、数据库、调度或交易行为，不重启、不提交。** 原作者为 Hermes，本轮由 Qoder 审查代码与论证，并分别委托上游只读审查、独立数据复算；不以模型投票决定结论。

**结论：原 DEB 找到了真实的“生成判决与入库结果不一致”，但不足以证明“四处接反”，更不能直接推出“重标置信门、增加 PARTIAL 档、削自选池、提高并发”。** 其中“不知规则”“置信不进决策”“前18分钟无任何持久化”有代码反证；“低置信全由状态修正造成”存在遗漏变量；多个统计量也不属于同一母体。保留这些历史说法供追溯，本节明确更正，不静默改写 §0–§10。

建议采用既有 `docs/specs/da-execution-contract-20261023.md` 的职责划分，不另建协议或执行器：**DA 产出可追溯的研究意图，确定性规则判断当前可执行性，Playbook 承担订单与持仓，反馈只回灌当时可知的事实。** 质量是事实可信、逻辑自洽、执行边界清楚、结果可归因，不是报告更长、AGREE 更多、confidence 更高或成交更多。

本节三种证据严格分开：代码分支能证明机制存在；单批实物能证明当日发生；跨日因果和收益改善必须另有同口径对照，前两类不能代替第三类。

### 11.2 对原提案的关键更正

| 编号 | 原断言／方案 | 复核证据与更正 | 对后续动作的影响 |
|---|---|---|---|
| R1 | N1 按“重叠率<50%”被 Jaccard=0.37 证伪 | `19/28=67.9%` 是交集占当日集合，Jaccard 是交集占并集，两者不是同一指标；单日与多日均值也不可互换。§4-N1 原反例没有明确指定 Jaccard，§8-7 不得称已按同一预注册判据证伪。18/28 只能证明当批来源构成，不能证明自选池劣质或无信息增量。 | 撤回“按原判据已证伪”的判分，改为“配额结构已测，价值与稳定性未证”；不改原50%判据，不据此砍名单。 |
| R2 | N2 无“空头排列”字面，所以模型不知道下游规则 | `llm_synth.py:244-259` 明确列出接飞刀／破位硬买／追涨降级，并要求有错杀证据时照常给方向、时机交执行层。这直接否定“不知情”。真正需核查的是：Prompt 泛称“回升自动复评”，是否与具体 G1 拒绝后不进入 DB 的生命周期相符。 | 审查**等价语义和真实执行能力**，不是补关键词；不得要求模型为了过门改成 SHORT。11/28 是当日被阻断的分析数量占比，不是 token／费用占比，更不是39%已证浪费。 |
| R3 | N3 置信阶梯下移全因10/23状态修正 | 10/22→10/23 分析师 Prompt hash 为 `3ad15c002fd6→c7444d67ce58`，挑战者为 `81b31f18c8a1→bd4f78da4ac7`，不是单变量自然实验。10/26 七条发布时保留方向的 PARTIAL 先得 adapter conf=0.50，再由 WARN **减0.15** 得0.35（`pipeline.py:2129-2144`），不是状态函数直接产出35，也不是固定封顶。 | 逐行拆 `state→gate→adapter conf→WARN adjustment→bridge→DB`；不能通过抬高conf补偿另一层惩罚。10/23仍有conf=70一条，“自10/23起为0”应撤回为“10/26为0”。 |
| R4 | N4 置信不进决策；DA会被生成侧数据质量分接管 | 对 DA，`instruction_builder.py:294-305` 直接把 bridge conf×100；并不在该路径重算数据质量分。`auto_execute_flow.py:164-180` 仍按来源、confidence降序分配执行顺序。当前 sizing=false、分歧乘数=1，只证明没有这两项主动缩放，不证明conf无行为作用。 | 收窄为“该批生成侧conf拒绝未覆盖已有DA行”；调整conf仍可能改变有限预算下谁先成交，不得标零行为。不同来源共用字段的语义债存在，但不要误称每条DA票被重复打数据质量分。 |
| R5 | N4 6次conf SKIP／7次仲裁拒绝／8行均可执行；VZ无55日志意味着阈值例外 | 发布后8行包含VZ原已SKIP；另外7条发布时带方向，生成侧6条低conf、COIN另走IC冲突。`BridgeVotingAdapter.vote()` 对非LONG/SHORT返回None（`:129-131`），所以VZ无conf日志不能用来推断阈值例外。**本轮读取现存DB时已有5行SKIP**，只余CAT／PANW／TSLA为非SKIP方向，其中后二者也有禁追阻断，CAT已成交。 | 分开“记录存在、发布时方向、当前方向、当前可执行、已提交、已成交”；非SKIP方向不等于可新执行。6、7、8不得共享拦截率分母；现存DB不能直接还原09:06生成后状态，更不能证明整个盘中一直保留原方向。 |
| R6 | N4 保护完全来自盘中三道门 | DA发布前有signal_gate，写库有admission，执行时还在真实重定价／数量后复查admission（`auto_execute_flow.py:852-895`），另有预算、账户与订单校验。写入优先级保护来源身份，不等于安全判据的权威顺序。 | 不改仲裁来“使SKIP生效”；先列明每个规则的适用来源、时点、结果及失败语义。不能把全部安全性归功于当日三个拦截理由。 |
| R7 | N5 前18分钟无任何持久化；崩溃必整批归零 | `pipeline.py:1566-1577` 在模型调用前落raw，之后补完整user_prompt；carrier计数排除了raw。10/26 `US.PBR.A.raw.json.bak` 的saved_at为08:15:56 ET，早于首analysis 08:19:03。 | 改称“首个完整analysis较晚”；分别量raw可恢复性与analysis可发布性。raw存在也不等于finalize能直接拿它发布，恢复测试须分阶段。 |
| R8 | N5 超时总是静默成功；容量硬顶35–41 | carrier明确写 `pipeline_timeout_finalized` 及artifacts（`:328-337,358-368`），并非静默。缺口是 `_verify_artifacts():181-207` 只验证同日bridge存在或同日有DA/UE行，不验证本轮覆盖率／身份。单日总时长除标的数再线性外推，没有分离固定开销、采集、并发和尾延迟。 | 报告区分“退出码成功、产物存在、当批完整”，35–41只只能保留为粗估，不能作为扩容依据。 |
| R9 | N5-A/C 提并发、并行pullback不改行为 | 提并发会改变deadline前完成的集合、限流和行情采样时点。更直接的是 `llm_synth.py:1054-1068` 会读取pullback产物并注入Prompt；并行刷新可能让同批读到不同版本，不只是OpenD争用。 | 按可能改变输入／供给的变更管理；先冻结该批输入快照再谈并行，不能以“性能优化”绕过单变量验证。 |

**置信因果的可验证反例**：固定10/26其余已保存字段，按旧 `raw_agreed` 语义重算 gate 与 adapter，七条中只有 AMD／CRM 的原始布尔为真，有恢复0.70的路径；不是七条都会恢复。此为**确定性下游反事实**，不是重跑旧Prompt所得模型答案，也不是收益证据。gate与adapter还存在不同分数语义：VZ的gate confidence=0.60、adapter=0.50；这应记录为两种派生量，不能只写一个“模型置信”。

**更直接的Prompt逻辑冲突**：`llm_synth.py:2555` 要求“找不到≥2条具体反证就AGREE”，但`:2573-2578` 又要求“同向且至少一条实质硬伤就PARTIAL”。恰有一条决定性反证时，两条指令相互冲突。规范状态机只解释方向／verdict，不验证硬伤是否真实，因此不能把PARTIAL标签当成已证错误。合理目标是按反证实质及来源判断，不按条数凑AGREE；对应Prompt纠错仍须独立离线对照，不能与执行能力文案或门槛同批改。

**两个遗漏的边界**：G1用至少20根日K、四舍五入后的 `MA5<MA10<MA20 ∧ close<MA5` 判空头排列（`app/playbook/momentum.py:6-36`），不能简化成“任何下跌趋势”或盘前实时价。另 `signal_gate.py:477-510` 已有同向PARTIAL加0.10的 `p0_shadow`；它还带旧方向冲突降两档逻辑，不能直接作为现行G1的忠实反事实，也不能把N3-B当全新影子重复立项。先核既有注册／裁决／消费与反事实保真，不重开已关闭轴。

### 11.3 应建立的最小产出闭环

```text
来源事件／人工研究池
  → 按既有资格与预算入选（保留来源、未选原因）
  → 时点一致的事实输入＋原始Prompt／版本
  → 分析师研究意图＋挑战者原始意见／规范状态
  → 既有signal_gate／adapter（原分数、调整、拒绝原因）
  → DA直接写入指令DB，并产出bridge供后续生成轮读取
  → 生成轮尝试／写入仲裁（尝试结果≠当前DB状态）
  → Playbook实时评估／提交／成交／退出
  → 净额与执行归因／有边界的记忆回灌
```

这里的多条写入路径不等于多套下单管线；Playbook仍是唯一订单与持仓管理入口。问题首先是读者无法区分阶段和权威，而不是必须把所有路径物理合并。

| 环节 | 必须回答的问题 | 最小验收，不新增交易规则 |
|---|---|---|
| 范围 | 为什么看这只？本轮有哪些没看、为什么？ | 固定 `ET date + batch_id`；区分候选行／唯一标的、重复／资格拒绝／预算截断。名单重叠只作描述，不代理收益。 |
| 输入 | 每个关键数字何时发生、来自哪里、单位是什么？ | 价格时点／完整bar／复权口径一致；原稿去重；未知不填0；冻结实际渲染Prompt及动态记忆。不能只存静态Prompt hash。 |
| 推理 | 支持方向的事实、假设与反证分别是什么？ | 利用现有 thesis、risks、evidence_gaps、计划字段表达；无依据则SKIP／缺口，不强凑多空或论据数；挑战者不是独立盲评概率。 |
| 规则 | 为什么PASS/WARN/BLOCK？conf是什么？ | 保留原始verdict／raw_agreed、规范state、gate判决、adapter原分、WARN调整与最终分；标单位0–1／0–100及版本，不额外发明“胜率”。 |
| 写入与执行 | 谁尝试改了什么、谁保留了什么，最终有没有下单？ | 关联decision_id、instruction ID及执行／订单身份；区分写入拒绝、规则拒绝、暂未触发、提交失败与成交。禁止用一次save返回row id证明新票已落库。 |
| 反馈 | 哪个阶段导致结果、模型当时能否知道？ | 原计划／提交参数／成交价分开；DA-only与EXT等分开；费用来源、A9双口径、A10、退出版本明确；描述相关性不自动升为调参教训。 |

**合格交易研究输出的逻辑顺序**：有时间与来源的事实 → 对方向的条件性解释 → 最强反证及证据缺口 → 当前接口支持的单一研究计划 → 既有规则是否允许执行。不能倒过来为了“能下单”挑事实，也不能将事后收益用于解释当时的决策必然正确。被规则阻断的研究方向可以有价值，但须明确不会自动变成待执行订单。

### 11.4 修订 F2–F5 的推荐方向

1. **F2拆成两件事，撤回“必须与重标门槛合批”的推荐。** 先在既有审计载体区分各阶段派生量，沿用现有交易字段及数值；这是可独立完成的可观测性工作。阈值校准／排序／sizing消费是另一项行为提案，必须先说明预测对象及增量价值，不能以“恢复供给”为目标。无需迁移或重写历史行；旧行无法重建则标unknown。
2. **F3暂不推荐立项。** PARTIAL是语义类别，不是已校准概率；“0.60中间档”没有数据依据。没有结构化命中的反证，不等于已经证实“无硬伤”，不应通过散文匹配造新门。
3. **F4不因重叠率或席位比例立项。** 自选池代表人工研究意图，不能默认低于扫描器conviction。先量截止前完成率、来源覆盖和新增事实；任何改池／配额的提案都要说明被替换研究对象与机会成本，并检查既有标的范围裁决，不从成交子集反推全池价值。
4. **F5先解决可测性与快照一致性，不直接5→8。** 先量实际worker、各阶段耗时、429／重试、预计完成集合及尾延迟；pullback必须按批冻结，不能一边更新一边作为该批动态输入。沙箱压测与真实供给变化分开验收。

**治理约束不是附注**：判据字典B12禁止未经裁决将decision_conf接新门；C12禁止以“加深分析质量／confidence等建门或提配额”降低死单，11月OKR §7亦明确继承。语义纠错和度量拆分可以提出，但不能将其包装成已获准重开该轴；收益性重启必须满足既有复活条件并另裁。当前也不得为对称改Prompt（C6）、重开C9触发轴或C13顺势轴。

### 11.5 可交给下一执行者的分批指令（均待批准）

**A批：先修复算器的口径，零生产行为。**

- 修改范围优先限于 `scripts/da_scope_confidence_audit.py` 及其隔离测试，不新建平行报表系统。`funnel` 当前取文件最后一批，必须支持显式日期／批次；历史复跑不得随今日新批漂移。
- `debate` 同时输出全分析、有效方向、过gate、写入、成交五个不同母体；DA归属按真实来源身份解析，不用 `model_used!='deterministic'` 代理。当前分类函数重解释旧checkpoint时标明“按现版本重分类”，与当时已存状态并列。
- overlap同时报交集数、两侧集合大小、当日留存率及Jaccard；日均报告带实际批次／日期对数。confgate按指令身份关联日志和DB；补多批重跑、同标多行、VZ原SKIP、缺日志例，不能只按symbol覆盖字典。
- **验收**：冻结10/22、10/23、10/26已有产物并记录哈希；每个数字有同一母体的明细、每个缺口显式missing／ambiguous，换批不改变旧报告。无LLM调用、无生产DB写入。这里是工程真值验收，不以n≥30为修错前置。

**B批：补齐从“判了”到“生效”的最小证据，不动门。**

- 优先复用 `instruction_arbitration.jsonl`、既有 conflict／exec_gate 审计及raw字段。当前 `_emit_arbitration` 只记attempted方向／did／confidence，不含skip_reason或规则身份（`db.py:421-439`），不足以独立区分conf拒绝与IC拒绝；补必要的阶段、原因和可关联身份，禁止无消费方字段堆积。
- 只读展示区分 `proposed_action / write_outcome / stored_action / execution_outcome`，这是审计视图，不是新建四套运行态状态机；历史未知不补猜测。
- 同批另一个独立观测小件检查carrier的当批完成集合、版本和遗漏项；完整／部分／失败先作审计读数，不改现役返回码。此项是交付真值问题，**不应等到D批扩容才处理**；合法零候选和同日旧产物必须有测试。
- 将来源优先级与安全评估分开讲清楚。若发现某条**已经批准且应覆盖DA**的安全规则在执行路径漏接，另报具体规则及失败样本，不给全部生成SKIP提优先级，也不回写已认领／已成交指令。
- **验收**：沙箱覆盖四类已埋点拒绝、正常写入、已执行不可覆盖、多轮重跑、留痕写失败；断言DB最终字段与返回行为不变。当前“3 passed”只含一类拒绝的行为测试、正常插入和源码锚点检查，不能当四路径全验证。下一次真实拒绝后核对事件和保留行；无产物但无应触发事件为TOO_EARLY，有明确事件却无留痕才判缺口。

**C批：对齐研究输出与执行能力，先离线，不改方向目标。**

- 回到既有执行契约与 `DEB-20261008-model-long-bias-decomposition.md`，逐条核对“被拒后是否自动复评、confirmation是否限价、cap是否有效边界、失效条件是否被执行”四类能力陈述。
- 先在“挑战者一条／两条反证冲突”与“分析师执行能力误述”中选择一个独立子批，另一项排队；每个子批仅提出该问题的最小文案差异，沿用现有字段。不得加入“空头排列必须SHORT”、提高AGREE率或抬高置信度的要求；不得凭空新造撤单、重确认、再入场或自动复评机制。
- **验收**：同一冻结输入、同一母体、仅一个Prompt变量，包含SKIP／失败／BLOCK样本；比较事实错误、能力误述、计划几何矛盾和下游决策差异。无法重建当时动态输入的样本单列，不拿当前记忆补回历史。离线结果通过后才申请既有纪律要求的≥5交易日影子，工程一致性通过不等于收益改善已证。

**D批：资源优化，仅在A/B可测后提出。**

- carrier报告选中／raw完成／analysis完成／可发布／已入库的覆盖及未完成原因；复用现有阶段事件，确认当轮身份，不以同日旧行令新批假绿。完整分析后全部SKIP／BLOCK是合法零交易结果，不应以“bridge条数>0”定义完成；complete／partial／failed须依据该批应完成的工作及交付契约区分。
- 分离固定启动开销与每阶段并发尾延迟，先在隔离环境复现限流、截止和中断恢复。只保存raw的阶段应能恢复采集材料，但不得声称无需LLM即可发布完整指令。
- **验收**：变更前后冻结同批输入，核对完成集合、顺序、429、重试、deadline后果及失败状态；不能只比p50速度。任何输入／供给不同均列为行为差异，另裁后单变量上线。

**执行顺序：A → B；C经授权后独立批；D只在证据显示截止或成本确为瓶颈时启动。** 不四项同时开工，不额外铺四个长期影子；复用现有注册表、消费者与周结算，遵守11月实验数量上限。下一检查点为10/27 ET当批完成并经过生成轮之后；这只是建议的人工验收时点，本轮未创建cron或提醒任务。

### 11.6 反馈闭环不能漏，但不凭代码缺口虚报损失

本轮抽查了现役 `strategy_memory.py:315-476`、`feedback_loop.py:65-113,478-496`：滚动窗口已在底层筛选，但pattern仍按入场 `trade_date` 聚合，日报另按 `date(exit_time)`；净额缺失回退毛额，未统一携带A9/A10及DA来源标签。SQLite对带offset的 `date(exit_time)` 会按UTC日期计算，不能默认等于ET退出日。`get_strategy_hint()` 会初始化／写记忆库，因此只读复核不能直接调用它。

只读SQL核对9/27–10/26现有相关窗口：148笔，缺净额0、质量不在clean/real为0、A9排除0、POSITION_SYNC/MANUAL为0；该窗source_type均为PLAYBOOK_SIM。**这意味着发现的是契约和边界缺口，尚未证明该窗受到这些污染；PLAYBOOK_SIM也不等于DA-only。** 不从这些代码差异编造已发生的收益损失。

后续沿用执行契约 §8：DA来源、入场cohort与退出日报分别建只读视图；跨ET午夜、费用缺失、非DA来源、重复lesson、重跑及未来信息泄漏纳入隔离用例。先报差异，再单独批准记忆输入修正，不在当前批回灌新Prompt。

绩效层同时给出 `mean(10000*pnl_net/notional)` 与 `10000*sum(pnl_net)/sum(notional)`，明确逐笔等权／名义加权；CI与MDE必须围绕所报估计量按日聚类。11月基线−2.25bps对应名义加权，不能给它套另一估计量的CI，亦不能为判达标择优切换。主估计量仍按既有契约的待裁澄清处理，本轮不改OKR。工程逻辑闭环与“DA有正期望”分开验收。

### 11.7 建议采用的下一步任务书

> 仅实施经用户批准的A批；先固定日期／批次／原始产物与版本，再修复算口径。交付完整漏斗、逐行置信变换、生成尝试与入库结果对账、缺失项清单及隔离测试。禁止改Prompt、门槛、来源优先级、名单、并发、仓位、退出或历史行；禁止以成交数、AGREE率或conf恢复为验收目标。A批验收后再提交B批最小差异及其消费方，不自动进入C/D。任何结论必须附原始证据、反例、适用范围和不能推出的结论。

本轮不宣称全链路已验证安全或盈利；已确认的是上述机制与文档论证缺口。新增遥测的下一真实触发、完整链路故障注入、Prompt A/B、端到端回放与净收益因果验证均仍需后续完成。

### 11.8 独立复算与本轮验证记录

代码路径约定：本节裸名 `pipeline.py / llm_synth.py / signal_gate.py / adapter.py / strategy_memory.py / feedback_loop.py` 均位于 `/root/.hermes/skills/deep-stock-analysis/scripts/`；`app/`、`ops/`、`scripts/` 前缀为本仓路径。原始分析材料为 `data/da_checkpoint/2026-10-26/US.*.json` 及raw／备份。

独立复算实际执行了 §9 的五个子命令、`overlap`、`arbitration`，并交叉读取原始checkpoint、漏斗、日志与只读DB；命令均exit 0。原计数多数可复现，错误主要在解释、阶段和窗口，而非简单加错数：

| 复算项 | 实际读数／范围 | 应采用的解释 |
|---|---|---|
| 10/26漏斗 | 当日1个da_selection批，67条候选记录，28入选；主池7、自选18、UE1、异动2 | 没有发现当日混批；但脚本仅取最后一批且无`--date`，下次运行后历史读数会漂移。 |
| 10/26与前一批重叠 | 交集19、当日28；留存率67.9%、Jaccard约0.47 | 原“重叠率<50%”反例未触发。 |
| 脚本实际近20对 | 9/28–10/26，21个日期目录；平均留存率52.8%、Jaccard0.37 | 超出DEB声明的10/19起窗口，且包含extras记录占比为0的较早时期，不能冒充同一来源结构窗口。 |
| 按DEB声明窗 | 10/19–10/26共5对；平均留存率61.1%、Jaccard0.46 | 仍不触发原留存率判据；也不能据此判自选池好坏。 |
| 10/23高置信行 | BABA一条conf=70，checkpoint为AGREE | 只能说10/26为0，不能声称10/23起连续为0。 |
| 10/26置信变换 | 8份对应材料的adapter原分全0.50；7个WARN→35、1个PASS→50 | 表里的35不是当日真反向语义档，不能代理方向分歧。 |
| 10/26阶段对账 | 6条conf日志、7条写入拒绝、8条DA记录；现存DB5条SKIP；唯一成交CAT | CAT来自extras也不能证明extras更优；仅说明不能把自选池整体标成无效。 |
| 新仲裁产物 | `instruction_arbitration.jsonl` 尚未产生 | 维持TOO_EARLY，等真实应触发事件，不宣称已活体验证。 |

建议重跑 §9 时显式保留日期、日志文件和输出的batch身份；`funnel/overlap/debate` 当前的窗口限制属于A批待修内容，不应谎称已经修好。现存指令的阶段反例可在 `file:data/ftseek_us.db?mode=ro` 连接上复算：

```sql
SELECT symbol, decision_id, direction, confidence, executed, skip_reason, created_at
FROM playbook_instructions
WHERE date='2026-10-26' AND decision_id LIKE 'DA-%'
ORDER BY symbol, id;
```

- 独立复核实跑 `./venv/bin/python -m pytest tests/test_playbook_instruction_arbitration_shadow.py -q`：**3 passed**；测试用临时库并重定向留痕路径。此结果不扩张为四拒绝分支、写失败或全链路都已覆盖。
- `sha256sum ops/hermes/da_main_carrier.py /root/.hermes/scripts/da_main_carrier.py` 两副本一致；一致不等于新阶段事件已有生产样本。
- 上游审查者复读新增 §11：未发现必须修正的上游事实表述；2/7反事实与raw／完整analysis边界已明确。数据复核发现的“现存DB已有5条SKIP”已追加R5，不用当前行冒充发布快照。
- 本轮文档 `git diff --check` 通过；未跑全量回归、未重做服务启动冒烟、未跨≥5交易日复核仲裁稳定性。只读反馈边界SQL与源码检查不是收益验证。

---

## 12. 第1—4项最小修复及第7项只读核查：实施记录（2026-10-27）

### 12.1 用户授权与边界

用户裁「按建议执行修复」。本批落实此前推荐的**第1—4项最小修复**，并完成**第7项反馈只读差异核查**。未改Prompt、置信门槛、PARTIAL档位、名单／配额、并发、仓位、退出、四锁或订单行为；第5／6项仍需各自独立单变量验证，未借本批上线。

本批是度量、审计与观测修复，不是收益实验。`batch_observation` 不改变carrier既有 `status`、`artifacts.ok` 或returncode；仲裁新增字段不改变写入返回值与保留行。只有 `app/playbook/db.py` 的留痕字段需要主进程重载；carrier由下一次cron新进程读取。

### 12.2 已落地的修复

| 项 | 实现 | 直接收益／边界 |
|---|---|---|
| 统计口径 | `scripts/da_scope_confidence_audit.py`：`funnel --date/--batch-id` 固定历史批；同日多批必须显式选identity；候选行与唯一标的分列。`debate` 以 `decision_id LIKE 'DA-%'` 识别来源，分开分析／方向／过发布门／DB行／成交，打印gate、语义conf、WARN调整及最终conf。 | 不再随新批漂移，不用model_used代理DA来源，不把checkpoint母体与DB母体并排当因果证据。 |
| 重叠指标 | `overlap --since/--date` 限定窗口，分别输出当日retention、Jaccard、交集、实际pair数；缺批或同日多批标unknown，不当作零重叠。 | 指标不再换尺；来源占比仍只作描述，不自动推出名单优劣。 |
| 阶段对账 | `confgate` 从raw保留发布方向，同时列当前方向、skip_reason、执行状态及按decision_id的成交数。同日重跑日志只按symbol关联，多行标ambiguous；事件不去重冒充独立指令。 | 区分“生成尝试／保留行／当前状态／成交”，不用当前DB回写发布时历史。 |
| 仲裁留痕 | `app/playbook/db.py` 既有JSONL增加 attempted/kept `skip_reason`、保留instruction id、stage与write_outcome；审计消费端显示原因并按保留指令去重。 | 能独立区分低conf与IC等拒因；事件数不再被当作独立样本数。写失败仍只debug，不影响入库。 |
| 批次完整性 | `ops/hermes/da_main_carrier.py` 终态事件新增 `batch_observation`：当批selected／analysed／publishable、missing/unexpected、gate_unknown、selection policy、raw/analysis进度、bridge新鲜度及 `complete/partial/failed/unknown`；合法零候选须完整分析且本轮bridge核验。 | 同日旧bridge／DB不再单独证明本批完整；raw不冒充完整analysis，未知gate不冒充可发布。旧成功语义与返回码保留，供先行观测。 |
| 反馈只读 | `scripts/da_chain_contract_audit.py` 新增 `feedback_clock`：入场cohort、ET退出日、现役SQLite退出日分开；decision_id来源、缺净额／费用、naive时间、A9 unknown、A10排除及UTC/ET明确跨日均单列。 | 不把entry/exit并集称为单一绩效母体，不把无时区时间或损坏metadata当成已确认；不调用会写记忆库的get_strategy_hint/run_daily。 |

### 12.3 真实历史批复算（只读）

- 指定10/26批次 `DA:639b6fc4d7e22d88a1409962`：候选记录67行、唯一标的61、入选28；主池7／自选18／UE1／盘前异动2，与原计数一致。
- 分层：`analysis=28 / proposed=19 / gate_publishable=8 / db_da_rows=8 / filled_trades=1`。七个WARN均为 `semantic=0.50 → adjustment=-0.15 → final=0.35`；VZ为 `gate=0.60 / semantic=0.50 / final=0.50`。
- 声明窗10/19—10/26实际5对：`mean_retention=0.61 / mean_jaccard=0.46`，不再把9/28起的20对混作该窗。
- 生成轮：6条conf日志、7条旧仲裁日志、8条DA记录；当前DB与发布方向并列展示，CAT成交1笔，其他当前skip／执行原因逐行可见。
- 以真实 `running.started_at`、pipeline输出、checkpoint和bridge只读调用新增观测：`state=complete / selected=28 / analysed=28 / publishable=8 / missing=0 / unexpected=0 / gate_unknown=0 / bridge_fresh=true`。这是**真实产物重算**，不是新生产事件已经产生。

复跑入口：

```bash
./venv/bin/python scripts/da_scope_confidence_audit.py funnel --date 2026-10-26
./venv/bin/python scripts/da_scope_confidence_audit.py debate --date 2026-10-26
./venv/bin/python scripts/da_scope_confidence_audit.py overlap --since 2026-10-19 --date 2026-10-26
./venv/bin/python scripts/da_scope_confidence_audit.py confgate --date 2026-10-26 --log logs/ftseek_us.log-20261027
./venv/bin/python scripts/da_chain_contract_audit.py --date 2026-10-26
```

### 12.4 反馈差异结果（只核查，未改回灌）

- 10/26反馈报告的时钟并集为151笔：DA 129／EXT 17／UE 5。它是entry/exit窗口并集，**不等于只按入场日的148笔，也不是DA-only绩效母体**；本窗缺净额0、A9/A10排除0。
- 以7/3为报告日的只读实跑确认2笔带offset退出的UTC日≠ET日；另有14笔naive时间标unknown，不能把14笔都断言为归日错误。新字段 `exit_utc_et_date_mismatch` 与 `exit_time_unknown` 已分开。
- 独立只读核查lesson_log：506条中 `(date,content)` 完全重复73组、冗余164行；现役注入窗4条无重复。重复历史应进入后续审计，不据此宣称当前Prompt已受重复污染；未调用实际注入函数，故不宣称已证明或排除未来信息泄漏。
- 本批未改 `strategy_memory.py`、`feedback_loop.py` 或任何Prompt。实际回灌修正须另批，先评估差异与消费方。

### 12.5 验证与部署

| 验证 | 实测结果 |
|---|---|
| 审计／仲裁／carrier／反馈定向契约 | **61 passed**（含同日多批、空候选、未知gate、旧bridge、合法零候选、四仲裁原因、已执行保护、留痕写失败、UTC/ET、naive及A9空串／整列缺失unknown） |
| 双副本同步 | `tests/test_ops_scripts_sync.py` **3 passed**；仓内与 `/root/.hermes/scripts/da_main_carrier.py` SHA256相同 |
| Playbook DB邻近回归 | **49 passed** |
| `tests/playbook` 全目录 | **1158 passed / 0 failed**；测试器仍报告已登记豁免的7次存量生产库写入，本批未改豁免清单 |
| 语法／差异 | `py_compile`通过；`git diff --check`通过 |
| 重启前 | 在仓仅既有 `US.MSFT LONG 9`；`exit_intents` 非BOOKED=0；`app/ scripts/` 改动均属本批 |
| 重载 | `systemctl restart ftseek-us`；ActiveEnterTimestamp 2026-10-27 09:35:36 CST，PID 39188 |
| 启动冒烟 | **7/7通过**；重启后在仓仍为MSFT 9股，非BOOKED出场仍为0 |

独立最终差异审查发现1项边界：空字符串 `metadata_json` 被默认成 `{}`、整列缺失只披露而未计unknown。已修为仅真正NULL表示无排除标记，空串／非法JSON／整列不可用均计入A9 unknown，并补隔离测试；其余审查范围未见必须修复项。修复后定向契约61 passed，真实10/26只读审计复跑通过。

未运行全仓 `pytest tests -q`；本次结论不扩张为全仓所有测试通过。无前端改动，未做浏览器验证。

### 12.6 生效边界、回滚与下一步

- 审计脚本立即可用；主进程已加载新仲裁留痕；外部carrier副本已同步。下一次真实DA拒绝或批次终态前，新增JSONL／`batch_observation` 仍是**待活体触发**，不得以单测或历史重算声称生产新事件已落盘。
- 回滚只需恢复本批列出的观测文件及外部carrier副本；不需要回滚交易参数，因为本批未改任何交易参数。不得顺带覆盖其他工作树改动。
- 下一DA批后核对：新仲裁事件的skip_reason与保留id、批次selected/analysed/missing/gate_unknown、合法零候选与实际返回码是否一致；无应触发事件时保持TOO_EARLY。
- 第5／6项仍按单变量离线验证排队；不自动进入Prompt修改、名单调整或提并发。工程可解释性提升不等于DA净bps已转正。

---

## 13. 未实施的 Prompt 与并发方案审查（2026-10-27，仅审查）

### 13.1 范围与结论

用户要求「审查未实施的Prompt与并发优化方案」。本轮只读核对现行文本、代码、既有裁决及历史产物；**未修改Prompt／代码／配置，未调用LLM或外部API，未启动影子、重启或扩容**。

| 候选项 | 审查结论 | 推荐顺序与理由 |
|---|---|---|
| C-A：挑战者反证数量与严重性冲突 | **值得做单变量离线修复验证，不可直接上线。** 一条真实硬伤可能被“不足两条即AGREE”反向升级，属于全局逻辑矛盾。 | 若只先做一个Prompt子批，优先此项；只统一裁决语义，不改方向、模型、conf公式或门。 |
| C-B：执行能力描述 | **值得做下一独立文案子批。** confirmation、cap等描述大体正确，不能整段重写；需修的是把发布前BLOCK也承诺成同指令自动复评。 | C-A验收后另批；仅修不真实的生命周期承诺，不新增复评机制。 |
| D1：`DA_MAX_WORKERS 5→8` | **暂缓。** 截止压力存在，但前置扫描、OpenD并发与外层worker共同构成瓶颈；不能把单标LLM延迟线性外推成worker收益。 | 先定位慢段与真实限流；只有证明外层排队是主要瓶颈时才另裁，不能以deadline紧张直接扩容。 |
| D2：pullback与pipeline并行 | **当前形态不推荐。** 实际可省约100秒，且pullback产物是Prompt输入；并行更新可造成同批新旧快照混用及额外OpenD竞争。 | 不进入现况试验；只有先实现并验证批次不可变快照、且串行耗时确为瓶颈时才可重议。 |

**总方向：Prompt先修语义真值，资源优化后置；任何Prompt换版不得与worker或pullback调度变更同批。** PROMPT_HASH变化还会影响缓存复用和首批耗时，若同时提并发，将无法归因供给与延迟差异。

### 13.2 C-A：只统一“严重性优先于条数”，不追求AGREE或conf

**已证冲突**：`llm_synth.py:2555` 要求“找不到≥2条有数据支撑的反证就AGREE”；`:2573-2578` 又规定“同向但至少一条实质硬伤就PARTIAL”。恰有一条决定性反证时，模型收到互斥指令。user侧 `:2635-2637` 还把“≥2条”用于DISAGREE/AGREE，因此“一条决定性反证且独立方向相反”也可能同时命中AGREE与DISAGREE；不能只改一行留下第二处数量要求。

**建议稿（仅审查文本，未生效）**：

> 反证按实质严重性而非条数判断。找不到任何有数据支撑的实质反证时，可以在满足同向等下述方向条件后给AGREE，并如实记录证伪失败；一般风险补充、措辞瑕疵不构成PARTIAL。方向一致但存在至少一条有出处、足以改变确信度或入场条件的实质硬伤时，给PARTIAL_AGREE，并指明论据及与其冲突的数据。反向证据是否足以支持DISAGREE，仍按现行方向与证据契约判断。

该建议删除的是“凑两条才承认问题”的错误激励，**不是**“每条风险都降级”、强迫挑战者反向、提高AGREE率或恢复conf=70。规范状态机、raw_agreed保留、adapter、WARN调整、PARTIAL 0.60影子及执行排序均不随文案顺手修改。

**既有裁决边界**：DEB-20260831 B11曾列“≥1条强反证”作为仪式化成立后的候选之一，但其观察窗未找到正式收口，不能把旧候选当成当前自动授权；B12/C12对confidence新门与提配额的限制不因本次文案纠错解除。`signal_gate.py:477-510` 的既有p0_shadow不得当作本方案证据或与新旧窗口混算。

**验收分两层**：

1. 无模型静态／契约层：所有挑战者指令不再含“至少两条才AGREE”的冲突；构造零反证、一条决定性硬伤、两条一般补充、真反向、无出处数字、技术失败，检查预期语义与现有字段，不新增LLM字段。
2. 隔离固定输入回放：冻结分析师输入与实际输出、挑战者可见材料、模型身份／参数、渲染器和动态记忆；只改C-A一段。包含SKIP／BLOCK／失败、T1–T4身份、同向一条硬伤及反向一条决定性反证样本，逐条列verdict、state、gate、conf及供给差异。无法重建当时输入的样本标missing，不用今天的数据补齐。

验收目标是消除互斥指令、保持证据引用与输出契约，**不是**方向翻转数、AGREE率、成交数或conf上升。单条硬伤仍因不足两条被AGREE、或同向单条硬伤被强制DISAGREE，均为反例。模型回放可能发生下游排序变化，必须如实列为行为影响，并记录每候选调用数、token、时延、重试／fallback及折算成本；离线通过不等于盈利改善，更不自动进入生产。后续活体仍需独立批准、版本隔离及既有≥5交易日影子纪律。

### 13.3 C-B：只修“自动复评”承诺，不重写整个执行接口

**已证边界**：`llm_synth.py:244-259` 已正确告诉模型研究方向与执行时机分离，但把接飞刀、破位硬买、追涨统一描述为“价格回升出拦截区后自动复评”。破位硬买实际在bridge发布前被BLOCK（`pipeline.py:2110-2126`），本轮不形成可执行指令，不能承诺同一指令稍后自动恢复。`app/playbook/momentum.py:6-36` 的G1也不是任何下跌趋势或实时盘前价判断。相反，**已入库指令的B门与chase_cap确实会按后续执行轮和当时行情复评**（`auto_execute_flow.py:314-429,1058-1101`）；这两类描述本身正确，不能整段删除。

**建议稿（仅审查文本，未生效）**：

> 你仍按证据给出研究方向与计划。部分方向性规则会在指令发布前拒绝；被拒绝的本轮不生成等待复评的执行指令，不承诺价格回升后同一指令自动恢复。已进入Playbook的B门与追价约束按后续执行轮和当时行情复评，其它终态不自动复活。confirmation是研究确认位，不是限价单或现役entry-confirm的回升触发器；现役确认使用自己的执行锚与窗口。

**明确不动**：单一首批、第二腿不保证执行、confirmation非限价单、cap的方向／null及飞刀几何、tripwire不自动撤单／平仓等已有正确描述。不得借C-B增加“空头排列必须SHORT”、自动复评、重确认、撤单或再入场机制。

验收先逐条核对“发布前拒绝／执行时拒绝／终态跳过／可复评”的真实生命周期，再做同类固定输入回放；主要指标是能力误述与不可执行承诺减少，不能拿G1阻断数量或方向分布当文案正确性的替代指标。

### 13.4 并发：截止压力属实，但不能据此直接提worker或并行pullback

**多日实测**：独立复算carrier事件得到完成时刻及08:57 ET余量：10/20余16.1分钟、10/21仅**1.7分钟（约100秒）**、10/22余14.0、10/23余6.8、10/26余17.9分钟。说明deadline压力不能否认；但10/21首份raw约08:27，较10/26的08:18晚约9分钟，慢日有显著前置扫描开销，worker无法修复这一段。10/26批内raw首末跨度约1234秒；按单标raw到analysis墙钟估算的有效并发约3.8，也低于标称5，进一步说明不能把5→8换算成1.6倍吞吐。该有效并发是间接估算，不是直接worker测量。

**D1边界**：每worker包含采集与两次串行LLM调用；OpenD另有全局并发上限4，worker增加主要抬高模型请求突发。历史stderr复核未见真实HTTP 429，但这既不证明5并发绝无风险，也不提供8并发或账户QPS上限证据；429重试后仍失败可令该标的降级／SKIP。提worker还会前移部分标的的行情采样，并改变慢日deadline前完成集合，属于潜在输入与供给变更。当前按代码默认及cron无覆盖推定worker=5，未把该推定冒充逐项环境实测。

**D2边界**：pullback自身已做4路并行，10/26实际阻塞pipeline约90–110秒，原“串行最多300秒”不是当前收益实测。`llm_synth.py:1054-1068` 会逐标读取pullback产物并注入Prompt，`pullback_stats.py:135-145` 在全批结束后原子替换文件。若与pipeline并行，文件虽不会半写，不同标的仍可能分别读到旧／新完整版本；其4路K线连接还可能与DA采集竞争，OpenD合计8路未测。原子写解决文件完整性，不解决批次一致性。

**推荐**：

1. 当前维持worker=5与串行pullback。理由不是“没有deadline压力”，而是**尚未证明提worker能消除主要慢段，且D2只能省约100秒却引入输入与连接风险**。
2. 先用已落地的 `batch_observation`、deadline事件和现有日志观察，不新造同轴长期影子；这些新事件在10/26后仍待下一次活体触发，保持TOO_EARLY。
3. 若重复出现deadline相关partial／未完成，先分辨前置scanner、OpenD等待、模型fallback或外层排队；只有外层排队被证明是主瓶颈，才另裁D1。
4. D1若获批，只允许“worker 5→8”一个变量，Prompt／模型／名单／pullback调度保持不变；冻结输入做行为对照，工程性能另验，预注册真实429／重试失败、timeout、raw/analysis缺失、完成集合、p50/p95及deadline余量。覆盖率下降、确认限流恶化或数据质量退化即停止并恢复5；p90没有改善属于“无性能收益”，不应包装成行为测试通过。
5. D2重议前置是批次不可变快照及逐标一致性测试，不是先并行再观察；即使实现快照，也须证明串行pullback约100秒确为值得承担改造风险的瓶颈。

这些方案只能承诺“可能减少等待或漏分析”，不能在实测前写成确定收益，更不能以提速后的成交增加或DA净bps作为工程验收。DashScope账户QPS、08:00 ET其他进程OpenD占用及8并发安全性均未实测，不得写成已排除风险。

### 13.5 推荐执行门禁

- **顺序**：C-A离线单变量 → 独立验收／批准 → C-B离线单变量；Prompt首批缓存与供给稳定后，才根据真实瓶颈决定是否讨论D1。D2现况不做。
- **不变量**：不换模型、不改方向目标、不改score/conf公式或门、不改名单／配额／仓位／退出；不为对称改Prompt，不重开C9/C12/C13等已裁轴。
- **隔离**：离线回放不写生产checkpoint、bridge、指令或记忆；模型调用与费用须另行授权。任何后续影子必须登记，跨11月还需满足实验数量上限。
- **证据边界**：文本一致性和调度正确性可逐例验收；收益假设仍需同口径前瞻样本、日聚类与MDE。不得用工程通过宣称DA转正。

本节仅形成候选指令与停止条件，未批准或实施任何上述变更。

---

## 14. C-A 隔离变体与最小回放：REPLAY_LIMITED（2026-10-27）

### 14.1 范围与生产边界

用户在前述审查后指示「继续」，按 §13.5 仅开展 **C-A 挑战者“反证条数→实质”隔离验证**。未进入C-B、worker、pullback、门槛、名单、仓位或退出；未修改生产 `llm_synth.py`、配置、DB、checkpoint、bridge或记忆，未重启服务。

全部变体、冻结输入、捕获请求和结果仅写 `/tmp/da_ca_replay_20261027_qoder/`。生产源码SHA256在回放前后均为 `97367403caac2a27e53e0879d743202e474232ad780899aecd257d803f037ba1`。该目录是临时审计产物，不是新增生产影子或注册表项目。

### 14.2 单变量与输入保真

- 基线为生产源码的隔离副本；变体只改两个文本块：system的“≥2条才AGREE”与user的DISAGREE/PARTIAL/AGREE条数规则。方向、模型、score/conf公式、merge、gate及其它文本不变。
- 基线挑战者指纹 `bd4f78da4ac7`，C-A指纹 `40e9681c9661`；另存双臂完整system/user SHA256，避免仅凭不含user模板的现役指纹判断等价。
- 静态契约全部通过：仅两个差异块、旧数量规则移除、方向条件保留、单反证规则存在、下游五个规范状态真值不变、生产hash未变。
- 冻结四个样本的raw与analysis及来源hash。无网络stub捕获证明：双臂data_text、分析师视图、模型参数完全一致，system/user各仅一个差异块。参数均为 `deepseek-v4.1-flash / temperature=0 / max_tokens=16000 / enable_thinking=true / timeout=300`；关闭blind shadow，不走fallback。
- data_text可从raw analyst user_prompt的v2尾部精确恢复，包含动态经验，不重新用当前数据渲染。**Pass1只能按merge的文档化逆变换重建**：恢复原confidence、剔除debate_outcome、后续symbol/name、挑战者追加risks与claim标记；原挑战请求未保存，不能称为原始请求字节级回放。
- 10/26样本的当前基线hash与历史相同；10/08样本是“历史输入＋当前基线”，历史hash不同，**不是历史Prompt复现**。

### 14.3 实模双臂结果

只执行2个核心样本×双臂，串行primary调用；SKIP与弃权先由确定性契约覆盖，未为凑样本扩大调用。

| 样本 | 臂 | verdict / my_direction | challenged | 规范state | 纯公式gate / 最终conf | 可发布 |
|---|---|---|---:|---|---|---|
| 10/08 AMD（历史single-challenged） | baseline | AGREE / BUY | 0 | AGREE | 良 / PASS / 0.70 | 是 |
| 同上 | C-A | PARTIAL_AGREE / BUY | 2 | PARTIAL_AGREE | 中 / WARN / 0.35 | 是 |
| 10/26 AMD（同向PARTIAL） | baseline | PARTIAL_AGREE / SELL_SHORT | 3 | PARTIAL_AGREE | 中 / WARN / 0.35 | 是 |
| 同上 | C-A | PARTIAL_AGREE / SELL_SHORT | 3 | PARTIAL_AGREE | 中 / WARN / 0.35 | 是 |

下游由现有 `_merge_debate_results`、adapter与gate公式只读重算；未写DB或执行。**单一样本虽两臂都可发布，但PASS/0.70→WARN/0.35说明C-A可能改变执行排序，不能称为零行为变更。** 第二组两臂raw `agreed=true` 但规范state仍为PARTIAL，既有状态机未被bool升级。

运行实测：逻辑调用4次、HTTP尝试4次、错误0、重试0、fallback0，模型回显均为 `deepseek-v4.1-flash`，finish均为 `stop`；总 **82,751 tokens**（prompt 34,300 / completion 48,451），累计延迟 **661,135ms**。baseline/variant总tokens分别41,926/40,825，样本太少且有随机性，不据此宣称变体更快或更便宜。

关键反证做了输入字符串抽查：单一样本的 `May Be Fully Valued`、5.732%、5.8%/6.0%阈值，以及第二样本的成交量、MA与振幅均存在于冻结输入。该检查只证明引用字符串存在，**不等于全量数字和事实核验**；模型自报“全部可溯源”未被当作独立真值。

### 14.4 裁决：REPLAY_LIMITED，不进入生产影子

**已通过的部分**：静态契约；两组实模均成功；方向未因文案被强制翻转；单一样本出现预期的AGREE→PARTIAL，同向PARTIAL样本保持稳定；已观察样本未触发“单条硬伤因不足两条仍AGREE”或“同向单条硬伤被强制DISAGREE”的反例；下游gate/conf/发布差异已补算。

**未闭合的验收**：

1. 原挑战请求未存，Pass1是逆变换重建；10/08还混用历史输入与当前基线，不能称完整历史冻结回放。
2. 仅两对、无重复、仅T1 primary；SKIP/BLOCK/技术失败、T2–T4及“反向一条决定性反证”未做实模回放，静态真值表不能替代这些行为覆盖。
3. 证据仅抽查字符串，未做全量事实核验；gate/conf是公式重算，不是实际入库、执行或收益结果。
4. 单一样本的C-A输出有2条challenged，不应将其叙述成“实模已证明恰一条硬伤的所有情形”。

因此结论为 **REPLAY_LIMITED**：支持继续离线验证，**不具备申请生产影子的验收闭环，更不能直接上线**。这不是FAIL，也不是盈利证据。独立只读复核与主判断一致。

### 14.5 下一次验证的最小正确路径

1. 用同一份冻结raw data_text重新生成一次Pass1，并在merge前立即冻结该次 `initial_analysis`、完整挑战者system/user、模型参数和身份；双臂共用这**同一次新生成的Pass1**，不再逆推历史答案。全程仍写隔离目录，不触生产。
2. 覆盖零反证、同向一条硬伤、反向一条决定性反证、分析师/挑战者SKIP、BLOCK、技术失败与T1–T4身份；关键样本重复，逐例保存JSON失败、重试、fallback、token和时延。新增模型调用及成本另批，不由本次自动扩展。
3. 对关键反证做完整事实核验，并列state、gate、conf、发布与排序差异；全部预注册反例未触发后，才可另行申请≥5交易日生产影子。即便届时获准，也不是转正或盈利证明。

本轮不再调用模型，不进入C-B或并发。复跑入口（临时产物仍在时）：

```bash
./venv/bin/python /tmp/da_ca_replay_20261027_qoder/replay.py --capture
./venv/bin/python /tmp/da_ca_replay_20261027_qoder/replay.py --live --label single_challenged --arm baseline
./venv/bin/python /tmp/da_ca_replay_20261027_qoder/replay.py --live --label single_challenged --arm variant
./venv/bin/python /tmp/da_ca_replay_20261027_qoder/replay.py --live --label same_direction_partial --arm baseline
./venv/bin/python /tmp/da_ca_replay_20261027_qoder/replay.py --live --label same_direction_partial --arm variant
```

结果汇总：`static_contract.json`、`capture_contract.json`、`evaluation.json`；完整逐臂响应在 `results/`。临时目录清理后，以上命令需先按本节hash与差异重新构建夹具，不得从生产最新数据补造历史输入。

---

## 15. C-A fresh Pass1证据／JSON／gate-conf验收：OFFLINE_CHECK_PASS_LIMITED（2026-10-27）

### 15.1 范围与有效母体

用户指定「核验证据真实性、JSON稳定性及gate/conf变化」。本轮仅执行这三项的离线验收，不进入生产影子、C-B或并发。有效母体仅为 `/tmp/da_ca_full_replay_20261027_qoder/` 中2个10/26样本及10次逻辑调用；其他并行目录、方案或扩样提议均不纳入。发现额外扩样倾向后已停止相关协作任务，未把其内容计入本验收。

生产 `llm_synth.py`、配置、DB、checkpoint、bridge和记忆均未修改，未重启。生产源码SHA256仍与 §14 相同。临时的runner与结果不是生产影子。

### 15.2 fresh Pass1与单变量保真

- 每样本只生成一次Pass1并保存原始应答及完整请求，随后双臂各2次挑战共用**同一份fresh Pass1**；不再使用 §14 的逆变换重建。
- AMD fresh Pass1为 `SKIP`、score=6，JSON契约零违规，数字溯源22/22；CRM为 `BUY`、score=8，契约零违规，数字溯源33/33。fresh方向与历史可能不同，不与历史结果混算。
- 双臂data_text、分析师视图、模型参数完全一致；只有C-A文本不同。挑战者均固定primary `deepseek-v4.1-flash / temp=0 / max_tokens=16000 / thinking=true / timeout=300`，不走fallback。
- 本轮实际模型调用 = 2次Pass1＋8次挑战 = **10次逻辑调用/10次HTTP尝试**，错误0、重试0、fallback0；合计 **161,951 tokens**、**907,138ms**。不与 §14 的4次旧回放混合计数。

### 15.3 三项验收结果

| 验收项 | 实测 | 判定 |
|---|---|---|
| 证据真实性 | 全部20条 `challenged_claims.counter_evidence`：13条直接来源、4条推导复算正确、2条为确认原值不存在的有效负证据、1条有出处但证明力有限；所审范围未发现编造。 | **PASS（限定字段）** |
| JSON与重复稳定性 | 2份Pass1契约零违规；8次挑战均满足必填字段、枚举、claim结构及finish=stop。每臂repeat的verdict与direction均稳定。 | **PASS（结构/verdict/方向）** |
| gate/conf变化 | AMD两臂均 `ANALYST_SKIP / BLOCK / 0.50 / 不可发布`；CRM两臂均 `PARTIAL_AGREE / WARN / 0.35 / 可发布`，各repeat一致。 | **PASS（纯公式一致）** |

**C-A在这两份fresh样本中没有改变verdict、方向、gate/conf或发布结果。** 这支持“未观察到意外的下游变化”，但不能推出它一定优于baseline或能改善股票选择。gate/conf由现有merge/adapter/gate公式重算，未写库或执行，不是实际排序与成交。

证据分类只覆盖 `challenged_claims.counter_evidence`，没有穷举所有 `validation/missed_risks` 散文；“数字有源”与推导正确也不等于整段因果证明成立。1条证据虽有出处，但证明力有限，已单独保留，未强行判为强反证。

### 15.4 重复内容与故障路由的边界

- AMD基线的challenged条数为 **3→0**、C-A为 **2→3**；CRM两臂均3→3。说明JSON、verdict和方向稳定，**不等于论据内容或推理完全一致**。
- 另以无网络故障注入测试T1→T2→T3→T4：模型顺序、16000/2000上限、thinking开关、fallback标记、primary_error和self_debate标记均通过。**这只是stub路由测试**，不是T2–T4实模质量、真实HTTP失败或生产fallback证据。
- 本轮没有实模“反向一条决定性反证”对照，也没有真实执行、生产排序或净收益数据。

因此总评为 **OFFLINE_CHECK_PASS_LIMITED**：用户指定的三项离线验收在所述范围内通过，独立只读复核一致；但不自动获得生产影子资格，不宣称行为收益、泛化能力或盈利改善。若继续扩大验证，反向单反证、T2–T4实模与真实故障仍须单独批准，不进入C-B或并发。

### 15.5 产物与复核入口

- `acceptance_summary.json`：输入、三项验收、总量与剩余限制。
- `evaluation.json`：逐次JSON、verdict、方向、下游公式、重复内容和usage。
- `manual_evidence_review.json`：20条反证逐条分类及4项推导复算。
- `fallback_contract.json`：T1–T4 stub路由结果，明确非实模覆盖。
- 每样本 `pass1/` 保存fresh原文、请求和数字溯源；`captured/` 保存双臂完整请求；`challenge/` 保存每臂两次完整响应。

本轮不再调用模型。临时目录不是长期数据SSOT；关键结论已在本DEB与handoff留档。

**归档（2026-10-27 提交批）**：上述结论层与逐次响应已按 sha256 锚定精选归档至本仓 `docs/offline/ca-20261027/`（34 文件：acceptance_summary / evaluation / manual_evidence_review / fallback_contract / manifest / runner 源、每样本 fresh Pass1 与双臂捕获请求、8 份挑战原始响应；`ARCHIVE_INDEX.json` 逐文件哈希）。两个大体积 `*.raw.json` 与 `*.historical.json` 未复制——归档时实测其与生产 `data/da_checkpoint/2026-10-26/` 逐字节相同（checkpoint 属 gitignored data/），由 `manifest.json` 的 `raw_sha256/historical_sha256` 指纹锚定，须复验时从 checkpoint 比对哈希。归档后完整性回验 34/34 通过；10 次调用 161,951 tokens（prompt 94,398 / completion 67,553）与 verdict/downstream 均从归档件独立复算一致。`/tmp/da_ca_full_replay_20261027_qoder/` 清理后以本归档为唯一复核入口；§14 的 `/tmp/da_ca_replay_20261027_qoder/`（旧逆变换回放）不在本归档范围，其结论仍以 §14 文内记录为准。

---
