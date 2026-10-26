# 议题：DA 双 LLM 的「范围 / 输入 / 一致度 / 置信」四处接反 —— 测量提案（零交易行为变更）

## 状态

- [x] 进行中（首轮实测完成，方案待裁）
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

1. **范围不由信号决定** —— 28 个分析名额里 18 个（64%）给了静态自选池（无上限、无排序），按 conviction 排序的动态信号池只有 7 个（25%）；
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
| E5 | 10/23 语义修正后 AGREE 归零、conf=70 灭绝 | `./venv/bin/python scripts/da_scope_confidence_audit.py debate --since 2026-10-19` | `10-22 | 28 | {'PARTIAL_AGREE': 13, 'ANALYST_SKIP': 9, 'ABSTAIN': 4, 'AGREE': 2} | 16 | {35: 8, 50: 2, 70: 5}` / `10-23 | 31 | {..., 'AGREE': 2} | 2 | {35: 13, 70: 1, 50: 1}` / `10-26 | 28 | {'PARTIAL_AGREE': 13, 'ABSTAIN': 6, 'ANALYST_SKIP': 9} | 0 | {35: 7, 50: 1}` |
| E6 | 生成轮前 18 分钟零 checkpoint | `./venv/bin/python scripts/da_scope_confidence_audit.py capacity --date 2026-10-26` | `checkpoints=28 first=20:19:03 US.PBR.A.json last=20:39:07 US.NU.json` + `heartbeat_analyses_done=[0 ×18, 2, 2, 2, 2, 2, 9, ..., 27]` |
| E7 | 单标分析师侧延迟 p50≈72s | 同上 | `analyst_latency_ms n=28 p50=72273 p90=90665 max=95676` |
| E8 | 空头排列 15 只中 11 只给 BUY | `./venv/bin/python scripts/da_scope_confidence_audit.py trend --date 2026-10-26` | `空头排列 BUY -> 11` / `空头排列 SELL_SHORT -> 3` / `空头排列 SKIP -> 1` |
| E9 | 置信门对 DA 行零效力：6 打印 SKIP / 7 仲裁拒绝 / 8 行存活 | `./venv/bin/python scripts/da_scope_confidence_audit.py confgate --date 2026-10-26 --log logs/ftseek_us.log-20261027` | `db_da_rows=8  conf_gate_skip=6  arbitration_refused=7` + 逐行 `US.VZ db_dir=SKIP conf=50 did=DA-20261026202352 gate_skip=- refused=-` |
| E10 | prompt 全文不含「空头排列」 | `grep -c "空头排列" /root/.hermes/skills/deep-stock-analysis/scripts/llm_synth.py` | `0` |
| E11 | 判据只活在门代码里 | `grep -c "空头排列" /root/.hermes/skills/deep-stock-analysis/scripts/signal_gate.py` | `3` |
| E12 | 门与写入是两条路径（行由 bridge 先写） | `./venv/bin/python -c "import sqlite3;c=sqlite3.connect('file:data/ftseek_us.db?mode=ro',uri=True);[print(r) for r in c.execute(\"select symbol,direction,confidence,decision_id,created_at from playbook_instructions where date='2026-10-26' and model_used!='deterministic' order by symbol\")]"` | 8 行全部 `decision_id LIKE 'DA-%'`、`created_at=2026-10-26T12:39:07`（= 08:39 ET，早于 09:06 生成轮） |

---

## 4. 逻辑推理链（每段：观测 → 推理 → **反例判据**）

### N1 范围：预算层级倒挂

- **观测**：E1/E2/E4。主池 `_select_eligible(_pool_screened, top_n + 2)` ⇒ 7 席（`pipeline.py:1235`）；自选池 extras `_select_eligible(_extra_screened, len(_extra_rows))` ⇒ **无上限**（`pipeline.py:1247`）。
- **推理**：唯一带 conviction 排序的通道配额最小，不做排序的通道配额等于候选数 ⇒ 分析集的构成由**静态名单**决定，信号强度只决定「前 7 名额归谁」。所以「DA 每天分析 28 只」在结构上更接近「把自选池里够波动的都分析一遍 + 扫描器 Top7 附加」，而不是「按当日信号择优」。
- **反例判据**：若跨日分析名单重叠率 < 50%，则「由静态名单决定」不成立（说明扫描器侧贡献了大部分名单）。**该重叠率本提案未实测**（见 §8 自曝），复查者可用 `ls data/da_checkpoint/<date>/` 两日交集复算。

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