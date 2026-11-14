# RFC-203 · 早期风险 record-only 影子（宽限灾难 / 计划锚追价 / 确认后排队）

- 日期: 2026-11-13
- 状态: 已实施 — **只记录候选，生产交易行为零变更**
- 用户裁决依据: 「解决小亏变大亏」+ C4 影子先行（先观测、不动入场/退出/尺寸）
- 关联: RFC-202 S1/S2/S4（只读分析器给出的候选面）、RFC-141（entry_confirm）、RFC-123（双锚）
- 写方: `app/playbook/early_risk_shadow.py`
- 评估器: `scripts/early_risk_shadow_eval.py`
- 单测: `tests/test_early_risk_shadow.py`
- 产物: `data/early_risk_shadow.jsonl`

## 1. 目标与非目标

目标：把「小亏变大亏」的三条可观测路径**先量化出来**，为后续独立裁决提供样本：

1. 宽限期（`held < 5min`）内，extreme 波动仓已越过方向性 2% 灾难线，但生产仍在等紧止损武装/等下一轮；
2. 执行前计划锚（DA 原始 `planned_entry` 或生成锚）到实际 reprice/成交价出现方向性 > 3% 的逆向偏离；
3. `entry_confirm` 确认后到实际执行之间存在多少排队延迟。

非目标（本 RFC 明确不授权）：

- 不提前退出、不改 stop/target、不改尺寸、不 reprice、不下单、不写生产 DB；
- 不把 2% / 3% / 0s 变成生产规则；
- 不计算反事实 PnL，不声称「被跳过的交易一定亏/赚」。

## 2. 三类候选（全部 record-only）

统一落到 `data/early_risk_shadow.jsonl`，每行带 `kind`、`date`、`caveat`，并显式写
`execution_blocked=false` / `would_exit=false` / `would_skip=true`（仅标记）/ `would_defer_order=false`。

### 2.1 `grace_candidate` — 宽限期灾难候选

触发（全部满足）：

- `0 <= held_minutes < 5`（宽限期）；
- 波动桶解析结果**严格等于** `extreme`；
- 方向性逆向幅度 **> 2%**：LONG `(entry-current)/entry`；SHORT `(current-entry)/entry`。

波动桶来源（按序）：`meta.vol_bucket` → `meta.attribution.vol_bucket` →
`meta.reprice_audit.vol_bucket` → 用现有 `short_horizon.vol_buckets` 配置从 ATR 定桶
（`bucket_lineage.select_vol_bucket`，与生产 `_compute_volatility_based_sl_tp` 同口径）。
三者都缺 → `vol_bucket=unknown` / `vol_bucket_source=missing`，**不冒充 extreme、不记录**。

关键字段：`entry_price` / `current_price` / `stop_loss` / `held_minutes` / `atr_pct`（百分数）/
`vol_bucket` / `vol_bucket_source` / `extreme_verified` / `adverse_pct` / `disaster_line_pct` /
`mfe_pct` / `mae_pct` / `current_mfe_pct` / `current_mae_pct` / `trade_id` / `inst_id` / `decision_id`。

不退出：本钩子在 `monitor_playbook_exits` 中只读 `meta` 与价格，返回值被丢弃；后续止损/退出
分支完全不受影响。

### 2.2 `anchor_candidate` — 计划锚追价候选

触发：执行前，`planned_entry`（复用 `_resolve_generated_anchor(inst, is_short)`，与触发/路由/
双锚影子同一解析口径）与 effective（`_reprice_params["entry_price"]`，缺则 `inst["entry_price"]`）
的**方向性逆向偏离 > 3%**：

- LONG: `(effective - planned) / planned * 100`；
- SHORT: `(planned - effective) / planned * 100`。

关键字段：`planned_entry` / `planned_entry_source` / `effective_entry` / `current_price` /
`direction` / `drift_pct` / `threshold_pct` / `would_skip=true` / `execution_blocked=false` /
`inst_id` / `decision_id`。

钩子位置在全部 guardrail/MOM 终态判断**之后**、`service.execute_instruction` **之前**，因此
只对「本轮真会下单」的指令取样；执行返回值、reprice 参数、下单路径均未被修改。

### 2.3 `queue_candidate` — 确认后排队候选

触发：`metadata_json.entry_confirm.confirmed_at`（确认门写入）→ 实际
`playbook_instructions.execution_time`（`mark_execution_booked` 写入）的延迟 **> 0s**。

关键字段：`confirmed_at` / `execution_time` / `execution_time_source` /
`latency_sec` / `arm_delay_actual_sec` / `would_defer_order=false`。

- 确认门未启用 / 未确认 ⇒ 无 `confirmed_at` ⇒ 自然不写；
- 成交行为成功后重读 DB 行取 `execution_time`；重读失败或旧行缺字段时回退「成交后当前时刻」，
  并在 `execution_time_source` 标明 `now_after_execute`，便于离线剔除口径污染样本。

## 3. 工程纪律

| 项目 | 做法 |
|---|---|
| 开关 | 代码默认 `pb_cfg.get("early_risk_shadow_enabled", True)`；YAML 未写也观测；置 false 才真停写方（需重载/重启） |
| 产物 | 单文件 JSONL，`O_APPEND` + 单次 `os.write`（进程内加锁），best effort 原子追加 |
| 去重 | 进程内按 `(rule, date, inst_id/trade_id)` 去重，上限 20000；20s 退出快循环不会刷屏；重启后允许同键再落一行（离线按同键去重） |
| TEST 符号 | 一律不写（2026-07-30 遥测污染案军规） |
| 异常 | 钩子层 `logger.warning(..., exc_info=True)`，写方所有失败返回布尔且不抛；无 `except: pass` / except 内 debug |
| 生产影响 | 零 DB 写、零下单、零 reprice、零返回值修改；配置 YAML 未改 |

## 4. 评估器

```bash
./venv/bin/python scripts/early_risk_shadow_eval.py
./venv/bin/python scripts/early_risk_shadow_eval.py --date 2026-11-13 --json
```

只读汇总：按日 / rule / symbol 的候选计数与 `affected_symbols`，状态仅两态
`OK`（≥1 行有效）/ `NO_DATA`（文件缺失、该日无行、全部行不可解析、不可读），退出码 0/1。
输出 `caveats` 字段**明示不是反事实 PnL**，阈值非生产规则。

注册表项 `early_risk_shadow`（`app/playbook/shadow_registry.py`）：

- `status=OBSERVING`、`min_n=30`、`eval_on_demand=True`；
- `no_adjudication_reason` / `graduation_criteria` 均为 `PERMANENT_MEASURE`；
- 停摆检查走 `check_mounting` 的产出缺失/超龄（14 天）哨兵；`eval_on_demand=True`
  表示不要求 cron 挂载，评估器按需手跑。

## 5. 样本门与后续裁决

- 进入后续 DEB 的最低条件：**n ≥ 30 且覆盖 ≥ 10 个 ET 交易日**；
- 这是「可以开始评估」的门，不是自动转正门；
- 任何把候选变成入场/退出/尺寸硬规则的动作（例如宽限期提前减仓、锚带拦截、
  确认后立即派发）都须**另立行为变更 RFC + 用户裁决**；
- 评估时须同时回答：避免了多少尾损、误杀多少赢家、是否只是把分批建仓路径重命名。

## 6. 与 RFC-202 的关系

RFC-202 的 S1（reprice 锚带）、S2（确认后立即派发）、S4（宽限期灾难线分桶）是同日
**只读分析器**给出的候选识别面；本 RFC 把它们装进生产路径的**常驻记录钩子**，让样本
持续积累，而不是只靠单日 `entry_geometry_review.py` 的离线回看。两者口径互相独立：
分析器读历史表，影子读写实时执行/退出循环；交叉核对时以 `inst_id` / `decision_id` /
`trade_id` 对齐。

## 7. 回滚

1. 停写：`early_risk_shadow_enabled=false`（重载/重启生效）；
2. 彻底摘除：删除 `exit_monitor_flow` grace 钩子、`auto_execute_flow` anchor/queue 钩子，
   模块与评估器可留档；
3. 产物 `data/early_risk_shadow.jsonl` 一律保留，不删历史。

## 8. 明确禁止的误读

- `would_skip=true` 只是候选标记，**不等于**该笔交易被跳过，更不能推断其盈亏；
- `would_exit=false` 表示本影子没有退出，**不等于**生产退出逻辑没退出；
- 2% / 3% / 5min / 0s 是本影子的**候选阈值**，不是当前生产参数；
- `vol_bucket=unknown` 的样本不得回填成 extreme 来凑样本；
- 重启后重复行不是数据损坏，离线按 `(rule, date, inst_id/trade_id)` 去重后再计数。

## 9. PositionSync 补录覆盖修复（2026-11-13 用户「请执行优化修复」）

首日实证发现 queue 原生样本只有 AMZN/MRK 2/6：MU/CRM/LLY/COIN 走 SUBMITTED→PositionSync 补录，未经过 `execute_instruction success` 内的 queue 钩子。

- 新增 `position_sync._record_recovered_queue_candidate`，在补录成功后调用同一 `record_queue_candidate`。
- 执行时间只取券商订单 `updated_time/create_time`，`execution_time_source=broker_order_updated_or_create_time`；禁止用 reconcile/补录时刻冒充真实成交，缺时间则不写。
- 保留原去重、TEST 符号排除、异常不影响补录的纪律；历史四条不回填，后续自然补录开始覆盖。
- 契约 `tests/test_position_sync_queue_shadow.py`：券商时间延迟 50.420s、无券商时间不造样本、无 confirmed_at 不写。联合恢复/补偿/快照/edge/queue 回归 **58 passed**，部署启动冒烟 7/7。
- 首日六笔人工只读重算仍为 25.548–52.506s、中位 44.205s，但不混入原生影子；后续评估须先报新钩子覆盖率，不能用旧 2/6 样本代表整体。

## 10. MOM grace 人群隔离（2026-11-14）

`record_grace_candidate` 对 `decision_id` 以 `MOM-ETF-V1-` 开头的仓位直接返回 False。RFC-203 的 grace/early-tight 研究对象是 DA 分钟级退出，不将 MOM 的 21 交易日 swing 仓混入灾难候选人群；MOM 自身仍只走 V1 allowlist。契约 `test_mom_swing_positions_do_not_pollute_da_grace_cohort` 已纳入联合回归。
