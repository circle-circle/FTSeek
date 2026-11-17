---
title: RFC-211 — 孤儿仓根治（方案A：入场记账 Outbox，成交意图与状态同事务持久化）前瞻预注册
status: a2_enabled_sim_forward_validation_observing
date: 2026-11-14
approved_by: 待用户裁（用户 2026-11-14「可以」= 批准**起草本 RFC**，非批准实施）
related: RFC-210（B1 已上线/B2 冻结）、DEB-20261114-da-kpi-backfill-stratification §6、STRATEGY-AUDIT-20261114-external-patterns §1、DEB-20260727-entry-fill-price-reconciliation、app/execution/exit_lifecycle.py（exit_intents 在仓成熟先例）
---

# RFC-211 — 孤儿仓根治（方案A：入场记账 Outbox，record-only 预注册 + 设计）

## 1. 授权边界（起草 ≠ 实施）

用户 2026-11-14「可以」批准**起草本待裁 RFC**。**本 RFC 当前只做设计 + record-only 预注册判据，零代码改动、零生产闸改、零重启、零下单。**

- 方案A 属 **behavior-touching**（改成交→记账写路径时序 + 新增持久化表 + 恢复重放）⇒ **影子先行 + 一次一变量 + 重启前核 + 用户裁后才实施**。
- 归类**流程成熟度/工程根治**（非净期望假设检验、非 C1 新入场假设），类比 RFC-210/DEB-20260727「修尺子 + 连带行为修复」先例。
- 与 RFC-210 关系：**A 是 B1/B2 的根治超集**（A 的意图携带真 regime + 真 ATR/SL）。A 毕业后 B1 成冗余子集（无害保留）、B2 不再需单独改。**A 未毕业前 B1 维持上线、B2 维持冻结。**

## 2. 问题陈述（根因 + 内部证据，已 DB 复算）

**根因 = 经典 dual-write（双写）问题**（DEB-20261114-da-kpi-backfill-stratification §6）：
- 写路径时序：`playbook_service.py:2149` `mark_execution_filled_unbooked`（CAS→FILLED_UNBOOKED）→ 内存构建富 record（真成交价/实时 regime/真 ATR/因子/投票）→ `:2281` `save_trade`（INSERT trade_records）→ `:2472` `mark_execution_booked`（CAS→BOOKED）。
- **双写窗口** = `:2149`→`:2281` 之间。若此处重启/中断，指令停 FILLED_UNBOOKED，`position_sync._reconcile_playbook_execution_states`(:803) 回收时调 `_backfill_playbook_trade_record`(:598/:867) **补录降级 record**（peak=entry、`_proxy_atr_from_stop` 代理 ATR、硬编码 RANGE；B1 已修 regime 继承）并标 BOOKED。
- **规模/指纹**：10 月 11 笔 → 11 月 **52 笔（~5×）**；52 笔**全对应 BOOKED 指令**（no_instruction=0）；**81% 在 09:xx ET 开盘高峰**；**重启相关**（11-12 重启最多=当日补录最多 9 笔）。regime 保真度仅 **3.1%**（RFC-210 探针 n=65），而**指令真 regime/ATR 100% 可得**。
- **性质**：安全网有效（无失控孤儿仓，11 月补录净 −$86 量级小），但 ① 回收 record 度量失真；② 孤儿窗口（成交→回收）暴露于无 playbook 看护；③ 重启越频繁污染越多。**这是流程成熟度缺陷，不是策略 edge 问题。**

## 3. 它山之石 + 在仓成熟先例（不发明新轮子）

- **外部经典解 Transactional Outbox / WAL**（STRATEGY-AUDIT-20261114-external-patterns §1，microservices.io/AWS/ARIES 已核实）：把「要记的台账意图」与「业务状态」**写进同一事务**，再由 relay 异步幂等投递；崩溃后按意图恢复，不丢不重。
- **在仓已证模板 = 出场意图账本 `exit_intents`**（`app/execution/exit_lifecycle.py`）：
  - schema(:57)：intent_id PK + state 机（PENDING→…→BOOKED）+ version 乐观锁 + UNIQUE 幂等约束 + `transaction()` BEGIN IMMEDIATE(:90)。
  - **`reserve_leg`(:204) 在 SDK I/O 之前先持久化发送闸** = Outbox「副作用前先落意图」的精确同构。
  - `create`(:194) 幂等（close_key 命中返旧意图 + 冲突检测）；`mark BOOKED`(:331) 带 receipt_json。
  - **出场侧已用此模式扛住重启**（durable exit ledger）⇒ 入场侧照搬是**最低风险、最一致**的根治路径。
- **成交真值已持久化**：`db.py:1247` `observe_execution_order` 把 `entry_fill`（attempt/order_id/price/dealt_qty/confirmed）写进指令 metadata_json；`mark_execution_booked`(:1326) CAS 已 gate 在 `entry_fill.confirmed=1`。**A 只需把「富 record 意图」也一并持久化**，无需新造真值来源。
- **投递幂等守卫已存在**：backfill(:598) 以 `futu_order_id + source_type` 查 trade_records，命中且 symbol/direction/qty/entry_price 一致即跳过（防重复记账），冲突 critical 告警。**A 的重放复用此守卫，天然防双簿。**

## 4. 设计（方案A：入场记账 Outbox）

### 4.1 原子性边界（关键，不依赖跨表同事务）
- **outbox 意图 ⟺ 指令状态** 必须同事务（同一 PlaybookDB 连接，一次 COMMIT）。这是 Outbox 的原子性核心。
- **trade_records 写入 = 幂等「投递」**，由 relay（正常路径或恢复重放）完成，**不要求与指令状态跨表同事务**。
- **拓扑诚实声明**：`db.py:_conn` 文档载 P0 lane 隔离问题（实证 SIM/LIVE 当前同落 `data/ftseek_us.db`，但隔离修复后可能分库）；`save_trade` 另开 TradeStore 连接。**故 A 的设计不得假设 instruction-state 与 trade_records 同库同事务**——outbox 与指令状态同库即可，投递跨库由幂等守卫兜底。此即经典 Outbox「outbox 与业务实体同库、消息外部投递」的标准边界。

### 4.2 新增表 `entry_booking_outbox`（镜像 exit_intents）
```
entry_booking_outbox(
  intent_id TEXT PRIMARY KEY,          -- = f"{instruction_id}:{attempt}"
  instruction_id INTEGER NOT NULL, attempt INTEGER NOT NULL,
  target_env TEXT NOT NULL, symbol TEXT NOT NULL,
  futu_order_id TEXT NOT NULL,         -- 投递幂等键（对齐 backfill 守卫）
  intent_json TEXT NOT NULL,           -- 完整 save_trade record dict（真 regime/ATR/因子/投票/metadata）
  state TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING→BOOKED
  version INTEGER NOT NULL DEFAULT 0,
  created_at REAL, updated_at REAL,
  UNIQUE(instruction_id, attempt)
)
```
表与指令状态同库（PlaybookDB），`ensure_tables` IF NOT EXISTS 建表 + 列迁移（沿用 exit_lifecycle 模式）。

### 4.3 写路径改造（一次一变量，独立开关默认 off）
开关 `playbook.entry_booking_outbox_enabled`（默认 false）。开启时：
1. 成交确认（broker deal confirm，现 :2140 前）后，**先构建完整富 record 意图**（把现 :2160–:2280 的上下文抓取/record 构建**前移**到状态标记之前；成交既已由 broker 确认，上下文抓取早晚不改持仓）。
2. **新原子方法** `mark_execution_filled_with_intent(instruction_id, attempt, intent_json, ...)`：BEGIN IMMEDIATE → INSERT entry_booking_outbox(PENDING) → UPDATE 指令 execution_state=FILLED_UNBOOKED → COMMIT（一次提交，意图与状态同生共死）。
3. `save_trade(intent)`（投递，幂等键 futu_order_id）→ `mark_execution_booked`（现 :2472 CAS）→ **同事务标 outbox state=BOOKED（消费）**。
4. **fail-safe**：意图构建/原子写任一失败 → 退回现行 `mark_execution_filled_unbooked`(:2149) + 降级补录 + B1，**绝不阻断孤儿接管**（安全网首要职责不变）。

### 4.4 恢复路径（relay 重放，干净而非降级）
`_reconcile_playbook_execution_states`(:803) / `_backfill_playbook_trade_record`(:598) 回收 FILLED_UNBOOKED 时：
- 查 entry_booking_outbox 是否有 (instruction_id, attempt) 的 **PENDING 全量意图**：
  - **有** → 按 intent_json **干净重放 save_trade**（真 regime/ATR/因子/peak=真成交，非降级），复用 futu_order_id 幂等守卫防双簿 → mark BOOKED → 消费 outbox。
  - **无**（崩溃早于 outbox 写 /  legacy / 开关 off）→ **现行降级补录 + B1**（fail-safe，行为不变）。
- 孤儿窗口暴露缩短：意图在成交瞬间即持久化，重启后 relay 立即干净接管，而非等下一轮 reconcile 降级。

### 4.5 与 B1/B2 的关系（超集，不叠加变量）
- A 的意图携带**真 regime**（⊇ B1）+ **真 ATR/SL 锚**（⊇ B2）。**A 毕业 ⇒ B1 冗余（无害保留）、B2 取消单独实施**。
- A 未毕业 ⇒ B1 维持上线、B2 维持冻结。**A 与 B1/B2 不同时上线**（一次一变量）。

## 5. 预注册判据（阈值先写，前瞻窗内禁改；改=重裁）

- **修复定义**：§4 写路径 + 恢复重放，独立开关默认 off，fail-safe 退回现行降级补录。
- **前瞻 cohort**：A 实施上线日（待裁）起；对照 = 修复前补录基线（RFC-210 探针 10-01→上线日，n=65 保真 3.1%）。
- **样本门**：前瞻 **n≥30 补录/重放事件**（重启驱动稀疏；最长 **60 交易日**，到期无结论自动 KILLED）。
- **毕业判据（同时满足）**：
  1. **保真度=100%**：前瞻重放 record 的 regime == 意图真值 ∧ 真 ATR 采用率=100%（有真 ATR 者）∧ peak=真成交价（非 entry 重置）；
  2. **结局守卫**：前瞻重放仓 `SL_INITIAL` 率与笔均净 bps **不劣于**基线（容差 SL_INITIAL +≤3pp ∧ 净 bps −≤5bps）；
  3. **零回归**：补录/重放成功率不降（fail-safe 不阻断）∧ **零重复记账**（futu_order_id 幂等守卫 0 冲突）∧ 零新增孤儿/两不管仓 ∧ FILLED_UNBOOKED 残留=0；
  4. **窗口缩短**：成交→干净接管时延 ≤ 现行降级补录时延（restart-correlated 探针度量）。
- **决策**：达标 → 提请默认开启（**另立 DEB + 用户裁 + 重启前核**，影子不自动生效）；不达标 → 回退/迭代；到期无结论 → KILLED。
- **必报**：保真度（regime/ATR/peak）、SL_INITIAL 率、笔均净 bps、重放成功率、幂等冲突数、孤儿窗口时延、n、fail-safe 触发数。

## 6. 分阶段（影子先行 → 实施 → 前瞻 → 裁）

1. **A0 影子探针（record-only，零行为变更，可先行）**：新建只读探针（mode=ro）度量 ① 成交瞬间富意图可完整捕获率；② 双写窗口时延分布；③ 历史补录中「若有意图本可干净重放」占比。**不写生产表、不改写路径**——先证「意图可得 + 窗口可闭」，再谈实施。注册表 PERMANENT_MEASURE（不占 KR4 ≤5）。
2. **A1 实施（待 A0 + 用户裁）**：§4 写路径 + 恢复重放，独立开关默认 off + fail-safe + 契约测试。重启前核（在途出场 SQL + git status）。
3. **A2 前瞻验证**：开关 on（sim），攒 n≥30，按 §5 判据。
4. **A3 毕业裁**：达标 → 另立 DEB + 用户裁默认开启；B1 转冗余、B2 取消。

## 7. 非重叠声明（不重开已闭轴）

A = **流程/工程根治**，不属 C8/C9/C12/C16（入场过滤）或 C14/C15/C17（隔夜/出场）已闭轴；**不改入场决策、不改频率/仓位、不改出场规则**。恒真红线全维持。与 RFC-209（降频）正交。A 携带真 ATR/SL 仅恢复「本应干净记账」的原值，**不是新出场假设**（原值即正常路径 :2281 所写）。

## 8. 独立开关与回滚

- 开关 `playbook.entry_booking_outbox_enabled`（默认 false）；off = 现行写路径 + 降级补录 + B1，**行为完全不变**。
- A0 探针 mode=ro，产物 `data/shadow/entry_outbox_intent_probe_latest.json`；回滚 = 删探针产物 + 注册表项（脚本留档）。
- A1 回滚 = 关开关 + 重启 → 恢复现行路径；outbox 表留存不删（审计），PENDING 残留由 fail-safe 降级补录兜底。

## 9. 验收门（接入验收，非收益裁决）

1. 只读：A0 探针 sqlite mode=ro，零 DB 写/零下单/零生产文件改。
2. 契约测试钉死：① 意图+状态同事务原子性（崩溃注入 → 两者同生共死）；② 恢复重放保真（regime/ATR/peak==意图）；③ **幂等防双簿**（重放两次 → trade_records 唯一）；④ fail-safe（意图写失败 → 降级补录不阻断）；⑤ 开关 off = 旧行为快照一致。
3. 实施前：影子预注册阈值冻结；A0→A1→A2→A3 分阶段；重启前核。

## 10. 风险与反面

- **跨库拓扑**：不依赖 instruction-state 与 trade_records 同事务（§4.1）；outbox 与指令同库即可，投递幂等兜底。
- **写路径时序前移**：上下文抓取前移属行为变更 ⇒ 影子先行 + 开关 + fail-safe；抓取失败退回现行路径，不阻断孤儿接管。
- **幂等**：重放复用 futu_order_id+source_type 守卫；契约测试钉死双簿=0。
- **稀疏样本**：n≥30 可能慢，60 交易日上限自动 KILLED，不无限挂账。
- **不夸大收益**：A 主要收益 = **流程成熟 + 度量保真 + 缩短孤儿窗口暴露**；11 月补录净 −$86 量级小，**不承诺直接盈利提升**。A 不改策略 edge，edge 仍靠 EXT 盘前（DEB-20261114-edge-discovery-premarket）+ RFC-209 降频。

## 11. KR4 分类

流程成熟度/工程根治（A0 PERMANENT_MEASURE 探针 + A1 行为修复另裁），**非 KR4「与净期望直接相关」实验，不占 ≤5 名额**。KR4 净期望名额仍由 DEB-20261114-KR4-consolidation KEEP≤5 约束。

## 12. 待裁决策点（给用户）

1. **是否立项 A0 影子探针**（record-only，零行为变更，零风险）——建议先做，用数据证「意图可得 + 窗口可闭」。
2. **A1 实施授权**（A0 达标后）：behavior-touching，须影子先行 + 一次一变量 + 重启前核 + 用户裁。
3. **设计取舍**：A1 全量 outbox（推荐，镜像 exit_intents，根治彻底）vs A2-lite 扩展 entry_fill metadata（更轻但 JSON blob 并发写风险、不如专表干净）——本 RFC 推荐 A1。
4. **B2 处置**：A 毕业后 B2 取消单独实施（A 已携带真 ATR/SL）；A 未毕业 B2 维持冻结。

## 13. A0 部署记录（2026-11-14 21:00 CST，用户「做」批准 A0 影子先行）

**A0 只读探针已落地；A1（写路径 Outbox）仍冻结待裁。**

### 13.1 交付物
- `scripts/entry_outbox_intent_probe.py`（mode=ro，206 行）：三指标（意图可得性 / 孤儿暴露窗口 / 可干净重放占比）。
- `tests/test_entry_outbox_intent_probe.py`（8 契约用例：核心意图齐全 / 时区归一窗口 / ATR 修正 / voting DA 不同源 / 全意图需 extras / 缺指令跳过但窗口保留 / 缺 voting 表不崩 / 信号行排除）。
- 注册表 `entry_outbox_intent_probe`（PERMANENT_MEASURE，eval_on_demand，stale_expected，min_n=30，不占 KR4 ≤5）。
- 产物 `data/shadow/entry_outbox_intent_probe_latest.json`（record-only）。

### 13.2 事前读数（10-01+，实跑）
- n=65 补录（exit_price>0），joined=65（100% 有关联指令）。
- **核心意图（真成交价 / regime / 真 ATR / 止损）100% 可得**；全意图（+conditions/thesis/horizon）96.9%。
- **可干净重放占比=100%** ⇒ A1 对全部历史补录均可干净重放，事前可行性成立。
- **真 ATR 可修正代理 ATR 63 笔**（中位绝对差 4.34）⇒ A 修 B1 未触的 ATR 降级。
- 孤儿暴露窗口（created_at−entry_time，时区归一）：p50=35.7s / p90=83.1s / max=8.9h（>60s 13 笔）。
- voting DA 不同源（DA- vs DEC-）恒 0，不计核心（正常路径亦取不到，非降级）。

### 13.3 验证 / 未验证
- 验证：8 契约测试过（+RFC-210 探针 3，共 11 passed）；py_compile OK；注册表 fresh import REGISTRY=117 dup_ids={} 新项在、stop_invalidation_audit 完整；探针 mode=ro 实跑零 DB 写。
- **零交易行为变更**：不改写路径、不建 outbox 表、不下单、不写库（唯一写产物 record-only shadow JSON）。**无需重启**（record-only 测量尺，零交易消费方；eval 脚本 fresh import 即生效，运行服务下次自然重启载入）。
- 未验证：A1 未实施；前瞻保真 / 结局守卫 / 窗口缩短均未验（n<30）；任何收益 / live readiness。

### 13.4 诚实边界
- 孤儿暴露窗口是「成交→降级回收」时延（上下文），**A 主要修干净度（全意图重放），不承诺必然缩短该时延**（reconcile 仍按调度跑）；§5 判据 4 为「不劣于」守卫。
- created_at=datetime.now() 朴素系统本地时（trade_store.py:858），entry_time ET 带时区；探针 .astimezone(UTC) 归一（勿假设 naive=UTC，否则虚增 8h——首轮探查曾踩此坑，已修）。

### 13.5 回滚
删注册表项 + 产物（探针脚本留档）；A0 零行为，回滚无交易影响。

## 14. A1 实施记录（2026-11-14 21:16 CST，用户「继续」批准 A1 实施，开关默认 off inert）

**A1 写路径 Outbox 已实施并部署（开关 off，零交易行为变更）；A2 启用仍待前瞻 + 用户裁。**

### 14.1 交付物
- config `playbook.entry_booking_outbox_enabled: false`（默认 off = inert）。
- db.py：`entry_booking_outbox` 表（_SCHEMA_SQL，与指令同库 ftseek_us.db）+ `persist_booking_intent`（INSERT OR IGNORE 幂等）/ `consume_booking_intent`（PENDING→BOOKED）/ `get_pending_booking_intent`。
- playbook_service.py：富 record 提取为 `_booking_intent`（:2281，行为中性）；save_trade 前 guarded persist（:2368）；BOOKED 后 guarded consume（:2504）。
- position_sync.py：`_replay_pending_booking_intent`（:598）恢复干净重放 + backfill 内 guarded 调用（:763）。
- tests/test_entry_booking_outbox.py（7 契约用例）。

### 14.2 设计取舍（诚实，对 §4.1/§4.3 的工程化）
- 采用「**投递前持久化意图**」而非「mark⟺intent 严格同事务」：mark FILLED_UNBOOKED 仍在 :2149（保证 off=字节级一致），意图在富 record 构建后、save_trade 前持久化（symbol 锁内）。
- **干净重放覆盖等价**：重启发生在「意图已持久化、save_trade 未完成」→ 干净重放；重启发生在「构建中（意图未持久化）」→ 降级补录+B1（与严格设计同——严格设计在构建中崩溃亦无意图）。mark→intent 间隙仅含上下文抓取 I/O（symbol 锁内）。
- 恢复重放复用 futu_order_id+source_type 幂等守卫；重放 record 加 metadata.booking_outbox_replay=True 审计标记（entry_reason 保持正常 playbook_sim，度量上等同干净记账）。
- 指令状态机不变：reconcile 在 backfill 返回 True 后照旧 mark BOOKED + 对齐止损（:895）。

### 14.3 部署凭据 / 验证
- 凭据：HEAD 12607e995；改 config/unified_config.yaml（switch=false）、app/playbook/db.py（表+3 方法）、app/playbook/playbook_service.py（提取+persist+consume）、app/core/position_sync.py（replay+调用）、tests/test_entry_booking_outbox.py（7）；未提交。
- 验证：
  - 重启前核：在途 9 仓（4 DA+5 EXT 全 LONG 周末持仓）exit_time IS NULL=9；FILLED_UNBOOKED=0；git status app/scripts/config 全本次 WIP；switch=false。
  - 契约测试 7 passed；相关套件 96 passed（A1+B1+queue_shadow+A0+backfill_fidelity+exit_compensation）；端到端 test_mom_etf_v1_flow 22 passed。
  - systemctl restart → MainPID 2967178→2969986；startup_smoke_check 7/7；重启后 9 仓不变 + FILLED_UNBOOKED=0 + switch flat-merge get()=False + REGISTRY=117 dup_ids={} + 零 Traceback/CRITICAL。
  - entry_booking_outbox 表已建（ensure_tables，ftseek_us.db；启动时 PlaybookService init/generation_flow 懒创建，验证时显式 ensure_tables 加速）。
- **off=identical 保证**：switch off ⇒ _outbox_enabled=False ⇒ persist/consume/replay 全 no-op；富 record 提取为变量再 save_trade 字节级等价。契约 test_replay_switch_off_is_inert 钉死。

### 14.4 未验证 / 待裁
- A2 启用（switch on）未做：须前瞻 n≥30 重放事件 + 结局守卫（§5）+ 用户裁。
- 前瞻保真度=100% / 零双簿 / 窗口不劣于 均未验（n<30）。
- 任何收益 / live readiness 不承诺。

### 14.5 回滚
switch=false（已是默认）+ 重启 ⇒ 现行写路径+降级补录+B1，行为完全不变；outbox 表留存（审计），PENDING 残留由 fail-safe 降级补录兜底。

## 15. A2 启用记录（2026-11-14 21:24 CST，用户「启用」批准 sim 前瞻观察）

**A2 已启用（switch on，sim）；前瞻验证启动，cohort 2026-11-16 起攒样；A3 毕业默认开启仍待裁。**

### 15.1 启用凭据 / 验证
- 凭据：HEAD 12607e995；config entry_booking_outbox_enabled: false→true（:1452，A2 启用注释）；本条 + RFC status；未提交。
- 重启前核：在途 9 仓（4 DA+5 EXT 周末持仓）exit_time IS NULL=9；FILLED_UNBOOKED=0；outbox 0 行；git status 全本次 WIP。
- systemctl restart → MainPID 2969986→2970469；startup_smoke_check 7/7。
- 重启后：switch flat-merge get()=True；9 仓不变；FILLED_UNBOOKED=0；outbox 表在 0 行（armed，周末无新入场）；REGISTRY=117 dup_ids={}；零 Traceback/CRITICAL。

### 15.2 前瞻验证（§5 判据，阈值冻结禁改）
- cohort：2026-11-16（下一交易日）起新入场产生的重放事件；对照=修复前补录基线（RFC-210 探针 n=65 保真 3.1%）。
- 样本门：n≥30 重放/补录事件（重启驱动稀疏；最长 60 交易日，到期无结论自动 KILLED）。
- 跟踪口径：clean replay 标记 = trade_records.metadata_json.booking_outbox_replay=1；
  计数 SQL：SELECT COUNT(*) FROM trade_records WHERE json_extract(metadata_json,'$.booking_outbox_replay')=1 AND trade_date>='2026-11-16'；
  保真度：重放 record regime/ATR == 关联指令真值（join metadata.playbook_instruction_id）；
  幂等冲突：同 futu_order_id 重复行=0；fail-safe 触发：journalctl grep "outbox 干净重放失败"/"booking intent 持久化失败"。
- 毕业（A3）：保真度=100% ∧ 零双簿 ∧ 结局守卫（SL_INITIAL +≤3pp ∧ 净 bps −≤5bps）∧ 窗口不劣于 → 另立 DEB + 用户裁默认开启；B1 转冗余、B2 取消。

### 15.3 诚实边界
- 周末无新入场，outbox 0 行属正常（armed 未触发）；首批重放须待 11-16 起新入场 + 重启中断才产生。
- n<30 不下结论；不承诺收益（A2 修流程成熟+度量保真，非策略 edge）。
- fail-safe 全程兜底：任何 outbox 异常退回降级补录+B1，绝不阻断孤儿接管/下单。

### 15.4 回滚
switch=false + 重启 ⇒ 现行写路径+降级补录+B1（行为完全不变）；outbox 表留存审计。

**当前状态：a2_enabled_sim_forward_validation_observing。A0 探针 + A1 写路径 Outbox + A2 启用（sim）已落地；前瞻 cohort 11-16 起攒 n≥30；A3 毕业默认开启待裁。**

## 16. A2 开始前复查修复（2026-11-14，用户裁「基于合理性和必要性进行优化修复」）

独立复查发现并已修正四类实现/治理缺口；cohort 尚未开始，无窗内样本或判据混算：

1. **已投递未消费**：旧恢复在既有 trade_record 的提前返回处绕过 replay，导致 PENDING 永久残留且 A2 漏计。现对既有记录复核 symbol/direction/qty/entry_price 后补 `booking_outbox_replay=True` 并消费精确意图；不修改价格、数量或盈亏。
2. **订单身份**：`get_pending_booking_intent` 恢复调用强制带 `futu_order_id`，并核对意图/record 的订单、标的、source_type 与 SIM lane；冲突拒绝重放并留 critical，退回原降级补录，禁止跨 attempt/订单写错台账。
3. **发现持仓路径**：从 `pb_{instruction_id}_{env}_{attempt}` remark 精确还原 instruction_id，使 Outbox/B1 在 broker holdings 发现路径也可恢复；其他系统单/异常 remark 不认领。
4. **SIM 授权硬边界**：`app.playbook.config.entry_booking_outbox_enabled(target_env)` 为唯一开关入口，real/live 即使全局 true 也返回 false；看门狗将 Outbox 登记为事件驱动表，零行/长期无事件不再误报。
5. **正常路径消费原子性**：`mark_execution_booked` 与 PENDING→BOOKED 在同一 PlaybookDB 事务提交；消费异常回滚指令 BOOKED，避免已 BOOKED 指令离开恢复扫描后残留永久 PENDING。旧最小 schema 无 Outbox 表时保持兼容。

同时补齐恢复异常的堆栈日志，清除本批新增的 except-pass/W6 棘轮回归。

**设计边界不变**：本实现仍是 §14.2 的“投递前持久化意图”，不是 §4.3 的 mark⟺intent 严格同事务；`mark_execution_filled_unbooked` 到意图持久化之间的中断仍由降级补录+B1 兜底。因此当前定位为**增强部分崩溃窗口的干净恢复**，不得把 A2 结果表述为已消除全部 dual-write 窗口。若未来要严格原子化，须另立行为 RFC、故障点测试和用户裁，不与本批小修捆绑。
