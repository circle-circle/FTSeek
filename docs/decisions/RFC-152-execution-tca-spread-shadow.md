# RFC-152 执行 TCA 台账 + 点差/报价年龄门只读影子（Phase 0）

**状态**: 2026-10-20 用户裁「同意，按照建议执行」→ Phase 0 实施（**纯只读测量，零交易行为变更**）
**类型**: L1 测量落库（新增 jsonl 台账 + 只读报表；不改下单/仓位/止损/退出/触发任何分支）
**关联**: DEB-20261009 §25–§27（费用/左尾/入场锚）、handoff 2026-10-20 P1（PlanGap / chase）
**OKR 边界**: 本 RFC **不占用** OKR-2026-10 KR3/C1 的「10 月至多一个新入场假设」名额——
Phase 0 不产生任何拦截，只是测量。任何转生产（点差门生效）须另立 RFC + 用户批准，
并届时占用 C1 名额。

---

## 1. 背景（为什么先做“一杆秤”）

近 30 日（DEB-20261009 §26/§27 复核口径）账面：

- 毛利 **+9.64bps/笔**，费用 **16bps/笔**，净 **−6.36bps/笔**；费用单因素把 PF 从 1.244 打到 0.853。
- 10-19：5/8 笔 `entry_path=chase`，4/8 笔成交/执行锚 vs 计划锚偏离 >1%（COIN +2.22%、
  RIVN +4.05%、ARM +2.72%、UNH −2.27%）；reprice 中位劣化约 2.37%。
- 近 30 日 MFE<0.3% 的 24 笔“墓地单”净 −$2,637，承担几乎全部亏损；`SL_INITIAL` 8 笔
  平均仅存活 17 分钟。

结论：当前最能确定改善的是**成交层的 implementation shortfall**，而不是再加一个方向指标。
但在动手前，系统没有一笔一笔的“计划锚 → 到达报价 → 实际成交 → 点差/报价年龄”台账，
无法量化“点差门能省多少、会误杀多少”。本 RFC 先补这杆秤。

## 2. 范围与非目标

**做（Phase 0，全部只读）**

1. 每笔**通过全部既有闸门、真正走到下单调用**的指令，落一行 TCA 记录。
2. 记录到达时刻的双边报价、点差、报价年龄、工程可成交性标记（book proxy）。
3. 记录计划锚（生成侧）、重定价锚、真实成交价、方向归一偏差。
4. 提供一个只读报表：按点差/报价年龄分桶，join `trade_records` 的扣费净 bps，
   按日聚类 t + MDE80。
5. 在 jsonl 中**标记**一个预注册的临时阈值下的 `would_block`（仅标签，不拦截）。

**不做**

- 不拦截、不改单、不改路由、不改仓位、不改 SL/TP/退出、不改触发条件。
- 不新增数据库表/列；不改 `playbook_horizon.yaml`（退出哈希零触碰）。
- 不做实时推送；不做自动转生产。
- 不把 `would_block` 标签当作已生效的门；报表中的阈值扫描只用于 Phase 1 提案。

## 3. 数据契约（`data/shadow/execution_tca.jsonl`，append-only）

一行 = 一次“走到下单调用”的执行尝试。schema = `execution_tca_v1`。

| 字段 | 单位/取值 | 说明 |
|---|---|---|
| `ts_et` / `date` | ET ISO8601 / ET 日期 | 时间统一 ET（宪章/时区纪律） |
| `inst_id` / `decision_id` / `symbol` / `direction` | — | 指令标识；`direction` 归一 LONG/SHORT |
| `source` | `DA` / `UE` / `OTHER` | 由 `decision_id` 前缀判定 |
| `entry_archetype` | 枚举/None | 来自 `raw_instruction_json.entry_archetype` |
| `planned_entry` | 美元绝对价 | 生成侧计划锚：`raw.planned_entry` > `confirmation.price_level`；缺则 None |
| `planned_entry_source` | 枚举 | `raw.planned_entry` / `confirmation.price_level` / `missing` |
| `db_entry_price` | 美元绝对价 | 本轮 reprice 前的 DB 值（可能已被前几轮覆盖，仅作上下文） |
| `execution_entry` | 美元绝对价 | 本轮 reprice/路由后的下单锚 |
| `bid` / `ask` / `mid` | 美元绝对价 | 到达时刻双边报价；`mid` 仅双边有效时给 |
| `book_is_proxy` | bool | LV3 盘口退化标记；True 时 `spread_bps=None` |
| `spread_bps` | bps（1bp=0.01%） | `(ask−bid)/mid×10000`；仅双边有效且非 proxy |
| `quote_age_ms` | 毫秒 | LV3 实测报价年龄；缺失 None（不伪造） |
| `quote_source` | `reprice_ctx` / `quote_fallback` / `missing` | 报价来源 |
| `session_min` | 分钟 | 距 09:30 ET |
| `atr_pct` | 百分数（2.5=2.5%） | 与执行链同口径 |
| `size` / `notional` | 股 / 美元 | 本轮计划数量与名义额 |
| `provisional_would_block` | bool | 临时阈值标签，**仅记录** |
| `provisional_block_reasons` | list[str] | `spread` / `quote_age` |
| `success` / `executed` | bool | 下单调用结果 |
| `fill_price` | 美元绝对价 | `execute_instruction.execution_price`；失败为 None |
| `planned_to_exec_bps` | bps，正=不利 | 计划锚 → 重定价锚，方向归一 |
| `exec_to_fill_bps` | bps，正=不利 | 重定价锚 → 真实成交价，方向归一 |
| `planned_to_fill_bps` | bps，正=不利 | 计划锚 → 真实成交价，方向归一 |
| `message` / `error` | 截断字符串 | 失败原因/异常 |
| `skip_reason` | 字符串/None | 失败时的指令 skip_reason |

方向归一：LONG 偏差 = `(later − earlier)/earlier×10000`；SHORT 偏差 =
`(earlier − later)/earlier×10000`；正数恒表示“对交易更不利”。

临时阈值（仅标签，写死在配置、Phase 1 前不得当作生效门）：
`execution_tca_spread_block_bps=20`、`execution_tca_quote_age_block_ms=2000`。

## 4. 实现落点（零行为变更）

- `app/playbook/execution_tca_shadow.py`：纯计算 + append 落盘；所有异常只 warn。
- `app/playbook/pipeline_parts/auto_execute_flow.py`：在 **entry_confirm 门之后、**
  `service.execute_instruction()` 之前快照报价；执行结束后收口写一行。
  - 只覆盖“通过全部既有闸门、真正会下单”的尝试；被预算/门拦下的指令不在本台账，
    避免把“没走到执行”误算成成交成本。
  - 全块包在 try/except 中，任何度量异常不得改变执行分支。
- 开关：`playbook.execution_parameters.execution_tca_shadow_enabled`（回滚一行）。
- `TEST` 标的守卫：与既有埋点一致，测试直调不得写生产 jsonl。

## 5. 预注册读数与判据（先写死，读数前不改）

**样本门**：≥10 个 ET 交易日 **且** ≥30 笔 `success=True` 的成交行。
未达门只报进度，不作任何裁决（宪章第 5 律）。

**必报读数**（`scripts/execution_tca_report.py`）：

1. 点差分布：p50/p80/p90/max；报价年龄 p50/p90；book proxy 行占比。
2. 实现缺口：`planned_to_exec_bps`、`exec_to_fill_bps`、`planned_to_fill_bps`
   的均值/中位，按 `source` / `entry_archetype` / `session_min` 分层。
3. 点差分桶 × 扣费净 bps：桶 `[0,5) [5,10) [10,20) [20,50) >=50` bps；
   每桶 n、均值、中位、胜率、按日聚类 t、MDE80。
4. 报价年龄分桶 × 扣费净 bps：`<500ms / 500–2000ms / >=2000ms / missing`。
5. 临时 `would_block` 组 vs 放行组的扣费净 bps 与误杀计数（正 PnL 笔数）。

**Phase 1 转生产提案门（仅提案，不是自动生效）**：

- 宽点差组（阈值待定，先用数据分桶确定）扣费净 bps 显著为负：按日聚类 **t≤−2.0**；
- 宽点差组本身 n≥30 笔成交，且跨 ≥10 个交易日；
- 误杀成本可量化：放行组中“宽点差但净为正”的笔数与净额必须同时报告；
- 阈值必须在**前半窗定、后半窗验**（或等价 OOS），不得全窗扫参后直接上线；
- 转生产须另立 RFC + 用户批准，并占用 OKR-2026-10 C1 的新入场假设名额。

**Phase 0 不设“失败即退役”判据**：若读数显示点差与净收益无单调关系，则本项转为
`PERMANENT_MEASURE`（保留台账、不提案），或由用户裁决退役；不得静默停采。

## 6. 风险与边界（显式披露）

1. **SIM 成交 ≠ 真实成交**：当前为 Futu SIM，点差/滑点关系可迁移但绝对值不可直接外推实盘。
2. **book proxy 行不可算点差**：LV3 盘口退化时只记标记，不伪造 `spread_bps`；
   报表按 proxy 行单列，不混入分桶。
3. **选择偏差**：台账只覆盖“已经过全部闸门”的尝试，不能回答“更早的候选被门拦下是否更差”。
4. **计划锚缺失**：部分 UE/EXT 指令无 `raw.planned_entry`/`confirmation`，此时
   `planned_entry=None`，只用 `db_entry_price`/`execution_entry` 做上下文，不反推计划价。
5. **临时阈值标签不是门**：`provisional_would_block` 仅用于报表分组；生产行为零变化。
6. **不碰退出哈希**：本批不新增 `playbook_horizon.yaml` 键、不改 `get_exit_config_version`
   纳入的任何键；KR1/post_ca4487 观测窗不受影响。

## 7. 回滚

- 行为回滚：`playbook.execution_parameters.execution_tca_shadow_enabled: false` + 重启
  （停止写 jsonl；不删数据）。
- 代码回滚：`git revert` 本批 + 重启。
- 数据保留：`data/shadow/execution_tca.jsonl` 一律保留，供后续裁决/审计。

## 8. 变更记录

- 2026-10-20 v1：初版并实施 Phase 0（用户裁「同意，按照建议执行」）。
  同日同批只做“一杆秤 + 点差标签 + 只读报表”；RS / ORB / 事件避雷针均未开工。
