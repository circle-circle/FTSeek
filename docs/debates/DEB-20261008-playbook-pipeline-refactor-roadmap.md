---
title: Playbook pipeline 结构性拆分路线 — 先职责地图与 facade 渐进迁移
date: 2026-10-08
status: 阶段 0 已落地（职责地图 + 路线图，零代码变更）；阶段 1 起才允许搬代码
trigger: 用户问 `pipeline.py` 超过 1 万行是否必须打散、集中/分散是否有绝对优劣、优化难度；随后令「按照推荐建议来」
---

# Playbook pipeline 结构性拆分路线

## 0. 结论

**不按“文件行数”一刀切，也不做一次性大拆。** 当前证据说明它不是单纯物理变大：

- `app/playbook/pipeline.py` = **10,207 行**。
- 顶层函数/类 = **119 个**；声明体合计约 **9,573 行**。
- **5 个超过 500 行的函数合计 4,724 行，约占声明体 49%**：
  - `run_playbook_auto_execute` 1,314 行，`app/playbook/pipeline.py:3991`
  - `_check_intraday_trigger` 1,059 行，`app/playbook/pipeline.py:5921`
  - `monitor_playbook_exits` 882 行，`app/playbook/pipeline.py:9046`
  - `_close_trade_record` 829 行，`app/playbook/pipeline.py:7078`
  - `_build_instructions_from_votes` 640 行，`app/playbook/pipeline.py:1382`
- 16 个超过 100 行的函数合计约 **65%**；82 个不超过 50 行的小函数仅约 **19%**。
- 模块级可变状态/缓存/防重集合众多，外部生产代码和测试大量直接 import 或 monkeypatch
  `app.playbook.pipeline`。

因此判断是：**不必立即打散，但值得进入受控重构队列；必须渐进式拆分。**

集中与分散没有绝对优劣。集中式的优势是全局顺序、共享上下文和交易一致性容易看；分散式的优势
是职责边界、局部测试、review 与并行协作更可控。拆错边界比不拆更糟，所以本路线以
**facade + 逐域迁移**为唯一方案。

## 1. 立案前检查

- 本批 **零代码变更**，不新增交易机制、不改判据、不改退出参数、不新增影子。
- 不改变 AGENT.md 架构约束：Playbook 仍是唯一下单/管理管线；拆分只拆内部模块，
  不拆成平行下单架构。
- 不与 `signal_pipeline.scan_loop` 合并或改变其 housekeeping 职责。
- 不迁移 `auto_trading_enabled` 三处裸 regex 直读；该语义归运维批维护。
- 本 DEB 是阶段 0 路线图，后续每个代码搬移批必须另按实际改动落档/回归。

## 2. 职责地图

行号基于 HEAD `40b4b6a1f` 的 `app/playbook/pipeline.py`。

| 域 | 行段/规模 | 代表函数 | 状态与依赖 | 建议落点 |
|---|---|---|---|---|
| Shadow/遥测/防重 | `28-542`，515 行 | `_shadow_dedup_*`, `_fetch_l1_micro*`, `_vwap_ab_*`, `_shadow_portfolio_emit`, `_log_playbook_direction_audit`, `_short_chase_shadow_row` | 多个进程内防重 set、缓存、文件 ledger；I/O 与 scheduler 弱耦合 | `shadow_telemetry.py` |
| 运行上下文/装配 | `545-854`，310 行 | `_now_et`, `_playbook_cfg`, `_build_data_collector`, `_get_watchlist`, `_get_positions_as_dicts`, `_get_total_equity`, `_get_voting_coordinator` | scheduler/config/db/collector 装配，含模块单例 | `runtime_context.py` |
| Bridge voting | `855-1287`，433 行 | `NullVotingAdapter`, `TechnicalFallbackAdapter`, `BridgeVotingAdapter`, `_validate_bridge_schema`, `_check_bridge_health_and_alert`, `_try_load_bridge` | bridge 文件状态、健康告警、reload 日期、scheduler 注入 | `bridge_adapters.py` |
| 候选构建/组合闸 | `1288-2365`，1,078 行 | `_build_vote_market_data`, `_build_instructions_from_votes`, `_select_thesis_template`, `_apply_portfolio_guardrails`, `_emit_skew_gate_vision_shadow`, `_trim_majority_*` | 640 行巨型构建函数；行业缓存、方向/sector 修剪、影子输出 | `instruction_builder.py` + `portfolio_guardrails.py` |
| 生成/补入/追踪 | `2366-3568`，1,203 行 | `run_playbook_generate`, `run_playbook_morning_push`, `run_playbook_topup_supplement`, `run_playbook_normalize_vol_buckets`, `_persist_signal_context`, `run_playbook_track`, `run_playbook_weekly` | scheduler 入口编排、幂等 marker、主题缓存、IC/DB 写入 | `generation_flow.py` / `tracking_flow.py` / `signal_context_store.py` |
| 风险闸/live 镜像 | `3569-3988`，420 行 | `_check_daily_loss_circuit`, `_check_graduation_gate`, `_check_same_day_hard_stop_cooldown`, `_emit_exec_gate_audit`, `_mirror_instruction_to_live_if_allowed` | 交易行为相关，live 镜像与工程探针上下文 | `risk_gates.py` / `live_mirror.py` |
| 自动执行主流程 | `3989-5304`，1,316 行 | `run_playbook_auto_execute` | 1,314 行单一函数；调用多层闸、broker、台账、shadow；**高风险** | `auto_execute_flow.py`，后续再拆步骤 |
| 盘中指标/触发 | `5305-6979`，1,675 行 | `_calc_intraday_vol_ratio`, `_capital_flow_divergence`, `_symbol_streak_banned`, `_check_liquidity_gate`, `_check_borrow_pool_gate`, `_check_intraday_trigger` | 多个 TTL 缓存/防重；1,059 行触发函数；**高风险** | `intraday_indicators.py` / `intraday_trigger.py` |
| 出场决策/落账/对账 | `6980-8916`，1,937 行 | `_close_trade_record`, `is_playbook_open_position`, `load_playbook_open_symbols`, `playbook_open_check`, `patient_exit_*`, `_check_playbook_exit_rules`, `_reconcile_broker_stop_exit`, `_release_live_broker_stop_for_rule_exit`, `_book_playbook_exit_result` | DB 台账、broker stop、live/sim 双臂、失败告警；**最高风险** | `exit_decision.py` / `exit_booking.py` / `broker_reconciliation.py` |
| 出场监控 | `8917-9933`，1,017 行 | `_pb_exit_price_cache_*`, `_pb_exit_bounded_fetch`, `monitor_playbook_exits` | quote cache、inflight box、882 行监控主循环 | `exit_monitor.py` |
| 隔夜/孤儿清理 | `9934-10207`，274 行 | `_mark_overnight_arm`, `_force_close_orphaned_sim_positions` | DB 状态标记与强平路径，行为敏感 | `overnight_cleanup.py` |

模块级状态需要按域封装，不能散搬：

- Shadow/防重：`_SHADOW_SEEN`, `_LIMIT_SHADOW_EMITTED`, `_OB_PULL_WARNED`,
  `_L1_MICRO_CACHE`, `_VWAP_AB_LOGGED`, `_SP_EMITTED`, `_low_atr_logged`,
  `_scb_scan_logged` 等。
- Bridge：`_bridge_alert_sent`, `_bridge_reloaded_date`。
- 组合/候选：`_VAULT_INDUSTRY_CACHE`, `_VAULT_INDUSTRY_CACHE_TS`。
- 生成/追踪：`_MORNING_PUSH_STATE`, `_TOPUP_ATTEMPTS`, `_tc_cache`。
- 盘中指标：`_VOL_CURVE_CACHE`, `_CAPITAL_FLOW_CACHE`, `_STREAK_BAN_CACHE`,
  `_LIQUIDITY_CACHE`, `_BORROW_POOL_CACHE`。
- 出场监控：`_quote_fail_streak`, `_quote_fail_alerted`, `_sl_burst_alerted`,
  `_close_fail_alerted`, `_close_fail_first_seen`, `_PB_EXIT_PRICE_CACHE*`,
  `_PB_EXIT_FETCH_*`。
- Chase/观测：`_CHASE_CAP_ROUNDS`, `_CC_NORM_LOGGED`。

## 3. 现有测试锚点

本阶段只做静态扫描，未改测试。已有测试证明以下域有直接契约：

- Shadow/防重：`tests/test_pullback_limit_shadow_telemetry.py`,
  `tests/test_chase_cap_round_counter.py`, `tests/test_chase_cap_inversion_fix.py`,
  `tests/test_vwap_gate_ab.py`, `tests/test_short_chase_shadow_scan_rows.py`。
- Bridge：`tests/test_stale_bridge_guard.py`, `tests/test_playbook_pipeline.py`。
- 候选/组合闸：`tests/playbook/test_direction_conditioned_ic_contract.py`,
  `tests/test_thesis_direction_consistency.py`, `tests/test_market_regime_f5_shadow.py`,
  `tests/test_skew_gate_vision_shadow.py`, `tests/test_portfolio_execution_guardrail.py`。
- 生成/追踪：`tests/test_pipeline_batch_e_contracts.py`,
  `tests/test_da_trace_preservation.py`, `tests/test_pipeline_horizon.py`,
  `tests/playbook/test_entry_revalidation.py`, `tests/test_premarket_overrun_cancel.py`。
- 自动执行/触发：`tests/test_playbook_pipeline.py`,
  `tests/playbook/test_offline_full_chain.py`,
  `tests/playbook/test_low_atr_gate_screening_basis.py`,
  `tests/test_phase1_signal_quality.py`, `tests/test_entry_anchor_dual_shadow.py`。
- 出场：`tests/test_patient_exit.py`, `tests/playbook/test_sl_fallback_gap_attribution.py`,
  `tests/playbook/test_exit_recovery.py`, `tests/playbook/test_live_arm_p0_contracts.py`,
  `tests/playbook/test_portfolio_backstop.py`,
  `tests/playbook/test_exit_price_bounded_fetch.py`,
  `tests/playbook/test_close_failure_transient_grace.py`。

迁移时有一个关键陷阱：许多测试通过 `monkeypatch.setattr("app.playbook.pipeline.<name>", ...)`
替换函数或状态。若直接把实现移走，pipeline facade 的 monkeypatch 可能不再影响新模块内部调用，
造成假绿或行为漂移。**过渡期必须保持 pipeline wrapper 是内部调用点**，直到测试与生产调用全部迁到新模块。

## 4. 渐进拆分方案

### 阶段 0（本 DEB，已完成）

- 只建职责地图与风险顺序，**不改 `pipeline.py`**。
- 输出本 DEB，作为后续拆分批的 SSOT。

### 阶段 1：建 facade，不搬巨型函数

- 新建 `app/playbook/pipeline_parts/`（或等价包）。
- `pipeline.py` 保持原路径与原公开名，内部调用 wrapper。
- 先迁移最小、纯函数、无交易副作用的一组：
  - overnight exemption 纯判定；
  - VWAP A/B 纯几何/ledger 判定；
  - thesis template 选择；
  - direction/sector trim；
  - instruction DB dict 转换；
  - ghost/drawdown/net PnL/BE exit 纯分类；
  - exit rule 转发层。
- 每迁一组，`pipeline.py` 保留同名 wrapper，内部调用 wrapper，测试先继续 patch pipeline。

验收：对应专项测试全绿；`rg` 证明生产入口仍从 `app.playbook.pipeline` 调用；无行为/配置变更。

### 阶段 2：拆 adapter 与上下文

- 迁 bridge voting adapters，但保留 `_try_load_bridge` 等调度入口 wrapper。
- 运行上下文抽为显式 `RuntimeContext`/装配函数，scheduler/db/collector 依赖注入。
- 先不改变任何调用时序。

验收：`test_stale_bridge_guard.py`, `test_playbook_pipeline.py`, scheduler 相关测试全绿。

### 阶段 3：拆生成/追踪/组合闸

- `_build_instructions_from_votes` 先拆成阶段函数：
  1. vote/market data 归一化；
  2. 方向与 horizon 判定；
  3. thesis/模板与 risk 构造；
  4. 组合 guardrail；
  5. shadow/审计输出；
  6. DB instruction 落库。
- 每阶段输出显式 dataclass 或 dict 契约，不改变过滤顺序。
- `run_playbook_generate/topup/track/weekly` 只保留编排，不夹业务细节。

验收：候选构建、组合闸、生成/追踪专项全绿；过滤计数与 shadow 行数不变。

### 阶段 4：拆盘中触发与自动执行

- 先拆可测指标：vol curve/ratio、VWAP slope、capital flow、liquidity、borrow、anchor。
- `_check_intraday_trigger` 拆成“指标采集 → 触发几何 → 防重/审计 → 决策”。
- `run_playbook_auto_execute` 拆成“加载候选 → 闸门 → 组合约束 → 下单 → 台账/审计”。
- 不改变候选顺序、闸门顺序、失败分支、DB 写入顺序。

验收：offline full chain、entry revalidation、low ATR、signal quality、anchor、
portfolio guardrail、live arm P0 契约全绿。

### 阶段 5：拆出场链

- `monitor_playbook_exits` 拆价格获取、持仓选择、出场规则、portfolio backstop、
  broker 对账、台账落账、失败恢复。
- `_close_trade_record` 拆费用、成交明细、PnL/extremes、source_type、DB 事务、
  broker stop release、通知/告警。
- 必须最后拆，且每步都有生产冒烟与离线 replay。

验收：patient exit、exit recovery、live arm P0、broker stop reconciliation、
portfolio backstop、bounded fetch、close failure grace 全绿。

## 5. 难度与风险评级

| 批次 | 难度 | 主要风险 | 必须保护点 |
|---|---|---|---|
| 纯函数迁移 | 低-中 | monkeypatch 落点漂移 | pipeline wrapper 仍被内部调用 |
| Bridge/context | 中 | 模块单例和 scheduler 生命周期 | 不改变 reload/alert 时序 |
| 候选/组合闸 | 中-高 | 过滤顺序与影子断言 | skip reason、方向、sector 口径不变 |
| 自动执行/触发 | 高 | 下单行为和 gate 顺序 | 候选顺序、失败分支、台账字段 |
| 出场/对账/落账 | 最高 | 账实不一致、重复平仓、live stop 泄漏 | durable ledger、broker stop、双臂一致性 |

## 6. 验证纪律

每个代码搬移批必须满足：

1. 一批只拆一个域；禁止与行为修复混批。
2. 先跑该域专项，再跑 Playbook 相关合并回归。
3. 生产代码改动后按 AGENT.md 重启并跑 `startup_smoke_check.py --wait 90`。
4. 若涉及 live/broker/DB 出场链，必须加离线 replay 或 golden fixture，不能只靠 unit mock。
5. 迁移前后比较关键日志/DB 字段：候选数、skip reason 分布、下单数、出场数、
   broker order id、fees、PnL、source_type、exit_time。
6. `pipeline.py` facade 在外部调用全部迁完前不得删除。

## 7. 回滚

- 阶段 0 无代码回滚需求，回滚即删除本 DEB/handoff 条目。
- 后续代码批按域 revert；facade 允许逐步回退到旧 `pipeline.py` 实现。
- 禁止把未完成的 `pipeline_parts` 作为第二入口并行上线。

## 8. 复核命令

```bash
wc -l app/playbook/pipeline.py
rg -n "^(class |def |async def )" app/playbook/pipeline.py
rg -n "from app\\.playbook\\.pipeline import|from app\\.playbook import pipeline|app\\.playbook\\.pipeline\\." app scripts tests
PYTHONPATH=. ./venv/bin/python -m pytest tests/playbook -q
```

阶段 0 未改代码，因此本批验收为文档结构检查与 `git diff --check`，不跑全量交易测试。

## 9. 复查增补 (2026-10-08 第二 agent 复核: 数字全对, 补 4 个未覆盖风险点)

复核方式: 独立实跑复核命令 + 全仓普查, 不引用原文数字。

### 9.1 原文数字复核 — 全部精确 ✓

- `pipeline.py` = 10,207 行 / 顶层 119 个 def/class ✓
- 5 巨函数行号逐一吻合 (`_build_instructions_from_votes` L1382 /
  `run_playbook_auto_execute` L3991 / `_check_intraday_trigger` L5921 /
  `_close_trade_record` L7078 / `monitor_playbook_exits` L9046) ✓
- 生产侧 import pipeline 的文件 = 21 个 ✓ (「大量直接 import」成立)

### 9.2 🔴 新增前置债 (阶段 0.5, 必须在阶段 4 之前清): 验收尺是坏的

**`tests/playbook/test_offline_full_chain.py` 当前 18 errors** — 驱动子进程
`require_auto_sim()` 读生产 LIVE-capable 配置抛错 (`candidate_eligibility.py:61`),
driver 缝表/私有清单缺该开关的打桩 (与 offline-chain-driver-private-patch-list
记忆同型债)。而本路线阶段 4/5 的验收**都依赖 offline full chain 全绿** — 尺子不修,
最高风险的两个阶段等于盲拆。(另: handoff 10-08 晨批提到的 `test_window_isolation`
失败现测已通过, 实指 `tests/playbook/test_da_candidate_selection.py:441`, 不再欠账。)

### 9.3 🔴 新增迁移纪律: W456 日志卫生基线是**按文件键**的, 搬码必须同批迁基线

`pipeline.py` 在 data/*_baseline.json 里挂着 **118 条按文件登记的存量豁免**
(debug_swallow=47 / except_stack=71)。搬一个含存量违规的函数到新文件 = 旧文件计数
下降 + 新文件「基线外新增」⇒ 棘轮当场红。纪律:
- 每个搬移批同批迁移 `data/{debug_swallow,except_stack}_baseline.json` 的对应键
  (计数搬家, 只降不升; 基线 10-03 起已 git 入库 ⇒ 迁移是可评审 diff);
- except:pass 棘轮是全局总数, 搬码天然中性, 无需处理;
- 不迁基线的搬移批会被 W456 拦红 — 这是**特性**: 强制每批显式对账卫生存量。

### 9.4 monkeypatch 爆炸半径实测 (比「大量」温和, 但有 2 个暗坑)

全仓普查 (字符串形 + 别名形): **25 个测试文件 / 39 个 patch 点 / 22 个不同属性**。
Top: `portfolio_guardrail_enabled`(4+2) `load_playbook_open_symbols`(3)
`is_playbook_open_position`(3) `_try_load_bridge`(3) `_today_et_str`(3)。
两个暗坑原文未列:
- **`datetime` 被 patch 2 次** — 测试 patch 的是 pipeline 模块的 `datetime` import
  绑定; 迁移涉及时钟的函数时, 新模块必须 `import datetime` 同形 (而非
  `from datetime import ...`), 否则这两处 patch 静默失效 = 时间相关测试假绿。
- `__file__` 被 patch 5 次 (走离线全链 driver 的文件定位), 新模块的文件相对定位
  (如 `Path(__file__).resolve().parents[N]`) 在包深一层后 N 要 +1 — 建议统一改用
  `app.core.paths` 类 SSOT 而非 parents 算术。

⇒ facade 纪律 (原文 §3) 方向正确且实测可控; 每批迁移后跑该域 patch 点对应的
测试文件即可闭包 (清单可由本普查复跑生成)。

### 9.5 新增建议: pipeline.py 行数棘轮 (只降不升)

迁移是数周工程, 期间新代码会持续回流 pipeline.py (今天 F1 就加了 26 行)。
建议加 `tests/test_pipeline_size_ratchet.py`: 基线 10,207 行, 只允许下降;
新增逻辑一律进新模块。与本仓 except:pass/W456/naive-now 棘轮同纪律 — 否则
阶段 1 还没拆完, 文件又涨回去了。

### 9.6 阶段 1 候选钉名 (实测行数, 全部 ≤60 行的纯映射/判定)

| 函数 | 行段 | 行数 | 备注 |
|---|---|---|---|
| `_vwap_ab_arm` | L371-377 | 7 | 最适首迁 |
| `_is_overnight_position` | L677-686 | 10 | |
| `classify_be_exit` | L7050-7064 | 15 | |
| `_max_drawdown_from_mfe_mae` | L7013-7028 | 16 | |
| `_vwap_ab_row_exists` | L347-370 | 24 | 读 ledger, 近纯 |
| `_trim_majority_sector` | L2345-2368 | 24 | F1 后仅影子路径调用 |
| `_classify_ghost_cause` | L6982-7012 | 31 | |
| `_trim_majority_direction` | L2313-2344 | 32 | 同上 |
| `_select_thesis_template` | L2024-2082 | 59 | |
| `_instruction_to_db_dict` | L3204-3263 | 60 | 含 9b087ae46 DA trace 逻辑 |

首批合计 ≈278 行 (~2.7%), 全部有既有专项测试锚点 (§3 表)。

### 9.7 config 读取路径提醒

pipeline 内存在三类配置读取 (ConfigContainer.get_dotted / get_playbook_config
单例 / 函数级开关如今日新增 `portfolio_guardrail_enabled()`)。迁移时逐域保持
读取路径不变 — 热重载失效链 (hot_reloader.invalidate_config_snapshots) 只覆盖
已登记的消费者, 新模块若自建缓存会脱离失效链 (8/28「日志说重载了但链路读旧值」
事故同型)。

### 9.8 总体评价

方案成立: facade+逐域、风险升序、禁双入口并行、replay/golden 要求 — 与本文
（及本仓军规）一致, 无需改向。补 9.2/9.3 两条纪律后, 阶段 1 可开工;
建议阶段 1 与 9.2 的 offline 链修复**同批或先行**, 否则阶段 4 的验收尺不存在。
