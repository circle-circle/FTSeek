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

## 10. 阶段 1 首批落地记录 (P1a, 2026-10-09 凌晨, 用户令「按计划优化+盘中验证」)

### 10.1 改动

- 新建 `app/playbook/pipeline_parts/` 包: `shadow_telemetry.py` (VWAP A/B 纯几何/
  台账去重) + `instruction_builder.py` (thesis 模板/方向与 sector 修剪/DB dict 映射)
  + `exit_classification.py` (BE/GHOST/回撤纯分类)。
- §9.6 十钉名迁移 **9 个** (合计约 278 行本体); `_is_overnight_position` 不迁 —
  它在 P2a (2026-09-06) 已是 `exit_engine` 转发器, 再搬一层是空转。
- pipeline.py 每个函数保留**同名转发器** (懒 import), 内部调用点与既有
  `monkeypatch("app.playbook.pipeline.<name>")` 缝全部不变 — 即 §3 过渡纪律。
- 涉及 pipeline 模块绑定的两处按 §9.4 预案处理: `_vwap_ab_row_exists` 回读
  `pipeline._VWAP_AB_LEDGER_PATH` (conftest 沙箱缝不换), `_classify_ghost_cause`
  回读 `pipeline.datetime/_now_et` (datetime patch ×3 全保)。
- `tests/playbook/test_entry_revalidation.py` 隔离环境按「只载已复核纯模块」原则
  同批登记新包 4 个模块 (否则空包路径下懒 import 找不到包 — 首轮实测 56 假红)。

### 10.2 行数棘轮 (§9.5)

`pipeline.py`: 10,249 (G6 后) → **10,145** (−104; 较 10,207 基线 −62) ⇒
`test_pipeline_size_ratchet` **2/2 绿**。G6 批 +42 行与棘轮的冲突由本批吸收,
无需调基线、无需 DEB 例外。

### 10.3 W456 (§9.3)

实扫三新模块 + `__init__`: debug_swallow=0 / except_stack=0 / bare_logger=0
(迁的都是纯函数, 不含 except 内 logger 违例) ⇒ **无需迁基线键**; pipeline.py
debug 46≤47 / except_stack 71=71, 只降不升维持。

### 10.4 offline full chain 修复 (§9.2 前置债, 18 errors → 18 passed)

driver 腐坏共 6 层, 逐层修: ①SIM 身份读生产 LIVE `.env.us` → workdir 冻结 SIM
副本 + `_ENV_FILE` 改道 + 三身份键**显式置 SIM 值** (只 pop 不够: config 包
`load_dotenv(override=False)` 会在 scan 深层懒导入时把生产值灌回 `os.environ`);
②扫描墙钟窗 → `cmd_scan(force_window=True)`; ③`call_llm` 双调用形 (位置形/关键字
形) 存根两形通吃; ④`get_db` 存根补 `connection(db_name)` 位置参; ⑤冻结材料补齐
per-item 契约字段 (counter_evidence/confidence_basis/event_phase/move_basis/
带三标签 causal_chain/SHORT short_check); ⑥FROZEN_CFG prompt_version v1→v2
对齐生产 yaml — v1 归一化**不透传契约字段** ⇒ 7/7 结构性违约 ⇒ repair 二次
LLM 调用 ⇒「llm exactly once」契约假红 (v1 是生产已弃用路径)。

### 10.5 验收 (全部实跑)

- 域专项 10 文件 **101 passed** (vwap/BE/thesis/da_trace/drawdown/skew/direction/
  batch_e/写隔离/棘轮)。
- datetime 三缝文件: `exit_recovery` + `pulse_freshness` **47 passed**
  (含 `_classify_ghost_cause` 懒绑定 datetime patch 验证)。
- `tests/playbook` 全量 **1027 passed** (含 offline full chain 18)。
- **盘中实弹验证**: 2026-10-09 00:03:42 CST 重启 (SPCX 多/P 空在仓、零在途出场
  意图, AGENT.md 盘中重启条款), 冒烟 `--wait 90` **7/7**; 00:04 与 00:07 两轮
  `auto_execute` (74 指令) 正常执行, VWAP A/B「放行臂+台账已记」成功 — 该路径
  异常时整体保守拦截, 能放行即证明 `_vwap_ab_arm/_vwap_ab_row_exists` 转发链
  生产闭环; 重启后零 ERROR/Traceback, 健康检查 00:10 绿。

### 10.6 发现的既有债 (非本批引入, 未动)

- `tests/test_patient_exit.py` **19 failed**: `8047b494f` 给 `_close_trade_record`
  加 `fee_broker` kwarg 时未同步测试 `_fake_close` 存根签名 (HEAD 静态证明:
  调用点传 `fee_broker=dual` 而存根不收)。与本拆分无关, 留给费账批。
- 阶段 1 尚余: net PnL 纯映射、exit rule 转发层两组 (§4 清单), 下一批继续;
  阶段 2 (bridge/context) 起按路线风险升序推进。

## 11. 阶段 1 收口 + 阶段 2a 落地 (2026-10-09 凌晨, 用户令「继续」)

### 11.1 改动

- **P1b 收口**: `_net_pnl_pair` / `_playbook_exit_owner` /
  `_playbook_exit_decision` 本体迁入 `pipeline_parts/exit_classification.py`,
  pipeline 保留同名懒转发器; §4 阶段 1 纯函数/转发层清单完成。
- **2a bridge adapters**: `NullVotingAdapter` /
  `TechnicalFallbackAdapter` / `BridgeVotingAdapter` /
  `_validate_bridge_schema` 本体逐字迁入
  `pipeline_parts/bridge_adapters.py`。
- pipeline 顶部 re-export 三类 adapter 与 schema 函数, 保
  `scheduler_core.py` 懒 import、`test_stale_bridge_guard.py` 直引与
  `app.playbook.pipeline._validate_bridge_schema` patch 缝。
- 调度入口不迁: `_bridge_file_state` / `_check_bridge_health_and_alert` /
  `_try_load_bridge` / `_reload_bridge_if_landed` / 开关与重载日期状态仍留
  pipeline, 避免读写/状态分家。
- `tests/playbook/test_entry_revalidation.py` 同批登记
  `bridge_adapters.py` (隔离环境空包路径假红防线)。
- W456 按文件键搬家: `except_stack_baseline` pipeline 71→70,
  `bridge_adapters.py=1` (迁入的 TFA 无栈 warning 存量, 未改行为)。

### 11.2 行数

`pipeline.py`: 10,118 (P1b 后) → **9,933** (2a 后; 较 10,207 基线 −274)。
行数棘轮维持只降不升。

### 11.3 验收

- `py_compile`: pipeline / bridge_adapters / entry_revalidation 通过。
- bridge 契约: `TestBridgeVotingAdapterConfidence` +
  `test_stale_bridge_guard.py` **38 passed**。
- `tests/test_playbook_pipeline.py` + stale bridge + entry revalidation:
  **284 passed, 1 failed**; 失败为
  `test_replacement_entry_order_survives_identical_snapshot` 的
  `sl_broker_order_id` 快照语义, 位于 close record 域, 与 bridge 迁移无交集
  (本批未触碰该路径), 留给 owner 批次。
- 全量主线: `tests/playbook` 1027 + stale bridge + W456 + 行数棘轮 49 =
  **1076 passed** (含 offline full chain)。
- **盘中实弹**: 重启前核 `exit_ledger.db` / `playbook_exits.db` 未 BOOKED 意图
  均 0; 00:32:38 CST 重启 (PID 174171), 冒烟 **7/7**; 启动即
  `bridge加载: 17 candidates (date=2026-10-08)` 且 scheduler 挂
  `BridgeVotingAdapter`; 00:32:55 与 00:36:04 两轮 `auto_execute: 74 条指令`,
  重启后零 ERROR/Traceback/CRITICAL。

## 12. 阶段 2b-1 运行上下文/装配落地 (2026-10-09 凌晨, 用户令「继续」)

### 12.1 改动

- 新建 `pipeline_parts/runtime_context.py`: 迁移 HorizonSelector 单例、
  playbook 配置读取、`_build_data_collector`、watchlist/positions/equity、
  `_resolve_sizing_base` 与 `_get_voting_coordinator` 本体。
- pipeline 保留同名懒转发器, 并新增 `_build_runtime_context` 显式注入这些
  facade 函数 — `monkeypatch("app.playbook.pipeline.<name>")` 缝不变。
- 新增 `RuntimeContext` 懒属性上下文, 生成与补量入口按原顺序取
  watchlist → collector → positions → voting coordinator → data_router →
  total equity; 不提前装配、不重复调用、不改变失败分支。
- **刻意不迁** `_now_et` / `_today_et_str` / `_is_trading_day_today`:
  `pipeline.datetime`、`pipeline._now_et` 是既有测试缝 (§9.4), 时间域待
  调用面与测试缝统一后再迁, 避免假绿。
- `tests/playbook/test_entry_revalidation.py` 同批登记
  `runtime_context.py`; 新增 `tests/playbook/test_runtime_context.py`
  钉懒装配顺序、缓存、facade patch 缝与 Null fallback。
- W456 按文件键搬家: `debug_swallow_baseline` pipeline 47→44,
  `runtime_context.py=3`; except_stack 无新增。

### 12.2 行数

`pipeline.py`: 9,933 (2a 后) → **9,825** (2b-1 后; 较 10,207 基线 −382)。

### 12.3 验收

- 新增 RuntimeContext 契约 + entry revalidation + stale bridge + W456 +
  行数棘轮: **233 passed**。
- `tests/playbook` 全量 **1030 passed** (新增 3 契约, 含 offline full chain)。
- 生成/补量相关根测试 (`premarket_overrun_cancel` / batch E / DA trace /
  horizon) **22 passed**。
- `tests/test_playbook_pipeline.py`: 72 passed + 1 既有 close-record
  `sl_broker_order_id` 快照失败 (与 2a 相同, 非 runtime context 域, 本批未触碰)。
- **盘中实弹**: 重启前两库未 BOOKED 出场意图=0; 00:48:06 CST 重启
  (PID 175383), 冒烟 **7/7**; 启动加载 17 个 bridge 候选并挂
  `BridgeVotingAdapter`, 00:48:23 与 00:51:33 两轮 `auto_execute: 74 条指令`,
  零 ERROR/Traceback/CRITICAL。

## 13. 阶段 3a 投票输入/构建前置态落地 (2026-10-09 凌晨, 用户令「继续」)

### 13.1 改动

- `_direction_conditioned_ic_net` / `_normalize_regime_str` /
  `_build_vote_market_data` 本体迁入
  `pipeline_parts/instruction_builder.py`, pipeline 留同名懒转发器。
- `_build_instructions_from_votes` 的循环外前置态拆为四个可测函数:
  `_load_factor_ic_global` (IC 文件一次读取)、`_regime_multiplier`、
  `_new_filter_audit` (漏斗计数键集不变)、`_load_direction_config`。
- 主循环内所有判定、continue 顺序、日志文案与 Instruction 字段不变;
  本批只替换前置态赋值与投票输入构造, 不动组合 guardrail。
- 新增 `tests/playbook/test_instruction_builder_vote_context.py`: regime 归一、
  SymbolContext 构造/异常路径、IC 文件显式路径、漏斗计数初始键集。
- W456 按文件键搬家: `except_stack_baseline` pipeline 70→69,
  `instruction_builder.py=1` (`_build_vote_market_data` 的无栈 warning 存量)。

### 13.2 行数

`pipeline.py`: 9,825 (2b-1 后) → **9,754** (3a 后; 较 10,207 基线 −453)。

### 13.3 验收

- 3a 专项 (新契约 + direction IC + thesis/Batch E/DA trace/horizon +
  entry revalidation + 两棘轮): **275 passed**。
- `tests/playbook` 全量 **1035 passed** (新增 5 契约, 含 offline full chain)。
- `tests/test_playbook_pipeline.py`: 72 passed + 1 既有 close-record
  `sl_broker_order_id` 快照失败 (与 2a/2b 相同, 非候选构建域)。
- **盘中实弹**: 重启前两库未 BOOKED 出场意图=0; 01:06:05 CST 重启
  (PID 177080), 冒烟 **7/7**; 启动加载 17 个 bridge 候选并挂
  `BridgeVotingAdapter`, 01:06:24 与 01:09:52 两轮 `auto_execute: 74 条指令`,
  零 ERROR/Traceback/CRITICAL。

## 14. close-record 旧债修复 (2026-10-09, 用户令「先修复掉」)

### 14.1 根因

- `tests/test_playbook_pipeline.py` 的 `replacement_entry_order_survives_identical_snapshot`
  把 `sl_broker_order_id` 当作入场身份快照; 生产 close-record 语义自 2026-10-06 起
  已改用 `entry_broker_order_id` 判定新旧入场。
- STOP 单号可在换仓/止损重挂中变化, 不能代表 entry order identity; 旧断言因此
  把正确生产行为误判为失败。

### 14.2 修复

- 测试改为显式添加 `entry_broker_order_id` / `sl_broker_order_id`,
  并分别断言两个字段在 close-record 后保持 `entry-2` / `stop-2`。
- 生产代码未修改; 本批只修复测试契约。

### 14.3 验收

- `tests/test_playbook_pipeline.py`: **73/73 passed**。

## 15. 最新策略语义对齐 (2026-10-09, 用户令「按最新策略逻辑更新」)

### 15.1 结论

- 生产策略已没有旧 3 模型选举投票脑: `VotingCoordinator` 不再实例化,
  DI 工厂已删除, scheduler 装配链为 `BridgeVotingAdapter →
  TechnicalFallbackAdapter → NullVotingAdapter`。
- 权威链路为 DA bridge; bridge 缺失时按配置走确定性 technical fallback,
  或 Null 全 SKIP。旧 LLM 热路径不在该链路中。

### 15.2 本批改动

- 仅更新 scheduler / pipeline / adapter / runtime context 的注释与运行日志语义,
  把“VotingCoordinator 多模型投票”改为“决策适配器”。
- 兼容缝保留: `scheduler.voting_coordinator`、`vote()`、
  `_build_instructions_from_votes`、`direction_mode: llm_vote` 均不改名、不改行为。
- 本批不修改 config、方向门、fallback 顺序、Instruction 构造或 close-record 判定。

### 15.3 验收

- 跑 bridge/runtime/pipeline 专项测试, 确认兼容缝不受注释/日志语义影响。

## 16. 阶段 3b/4/5 连续迁移落地 (2026-10-09 盘中, 用户授权「完成全部迁移」)

### 16.1 阶段 3b: 候选构建与组合闸

- **3b-1 单候选阶段**: `_record_vote_snapshot`、technical 方向/仲裁、
  BUY 分歧门、方向条件化 IC 冲突、horizon、decision/数值、confidence、
  Instruction 组装与过滤审计迁入 `instruction_builder.py`。
  pipeline 留同名懒转发器; `continue` 顺序、filter audit 键与 Instruction 字段不变。
- **3b-2 组合闸**: 新建 `portfolio_guardrails.py`, 承接 vault industry、
  guardrail 总开关、G4/G6 判定与 skew gate vision 影子。
  pipeline 转发器显式注入 emit/开关/industry/trim 回调, 保既有 monkeypatch 缝。

### 16.2 阶段 4: 风险闸与 live 镜像

- 新建 `risk_gates.py`, 迁日亏熔断、连亏、毕业门禁、同日硬止损冷却、
  执行漏斗审计、LIVE C-lite context 与双臂镜像。
- 日亏 baseline 路径、连亏回调和 exec audit 的 `now_et/module_file`
  均由 pipeline 显式注入; fail-open/fail-close 语义不变。

### 16.3 阶段 5: 盘中指标首批

- 新建 `intraday_indicators.py`, 迁 VWAP 斜率、资金流背离日缓存、
  低 MFE 连败封禁缓存、生成锚解析与反事实触发几何。
- `now_et/module_file` 显式注入; 触发主体 `_check_intraday_trigger` 本批不动。
- **第二批接线**: p50 量能曲线、盘中量能比、流动性门与借券池门本体迁入
  `intraday_indicators.py`; pipeline 保留同名转发器。`now_et`、
  `module_file`、`_VOL_CURVE_CACHE` 与 `_intraday_vol_curve_p50`
  curve provider 显式注入, 保时间、文件、缓存与 monkeypatch 语义。

### 16.4 行数与卫生

- `pipeline.py`: 9,754 (3a 后) → **8,692**; 较 10,207 基线 **−1,515**。
- W456 按文件键搬家:
  `debug_swallow` pipeline 44→41, instruction_builder=1,
  intraday_indicators=1;
  `except_stack` pipeline 69→60, instruction_builder=3,
  portfolio_guardrails=1, risk_gates=6。

### 16.5 验收

- 3b/组合闸/风险/live/指标专项:
  86 + 29 + 207 + 220 + 264 + 226 passed。
- `tests/playbook` 全量: **1043 passed**。
- 行数棘轮 + W456: **18 passed**。
- 阶段 5 第二批专项: `test_phase1_signal_quality.py` +
  `test_entry_revalidation.py` **226 passed**。
- 盘中实弹: 01:39:47 CST 重启 PID 179603, 冒烟 **7/7**; 新进程装载
  17 个 bridge 候选, 01:40/01:43/01:46 三轮 auto_execute 均处理 74 条指令,
  零 ERROR/Traceback/CRITICAL。该轮验证覆盖 3b/组合闸; 风险与指标批在全量
  测试后另行重启复验。
- 复验: 重启前 `exit_ledger.db` / `playbook_exits.db` 未 BOOKED 意图均为 0;
  02:03:58 CST 重启 PID 181539, 冒烟 **7/7**; 02:04:04 装载 17 个 bridge
  候选并挂 `BridgeVotingAdapter`, 02:04:16 auto_execute 处理 74 条指令
  并于 02:04:19 正常完成, 零 ERROR/Traceback/CRITICAL。该时点 ET 14:04
  已过新触发截止, 量能/流动性/借券门由专项测试覆盖。

## 17. 阶段 6: 生成/追踪流迁移落地 (2026-10-09 盘中)

### 17.1 生成流

- 新建 `pipeline_parts/generation_flow.py`:
  `run_playbook_generate`、`run_playbook_morning_push`、
  `run_playbook_topup_supplement` 与 `run_playbook_normalize_vol_buckets`
  本体迁入。
- pipeline 保留同名转发器。生成/补量/定桶显式注入 today/now、退避缓存、
  runtime/instruction/ATR/SLTP 回调; morning push 显式注入 state path。
- `run_playbook_generate` 的完成标记契约测试同步指向新宿主,
  仍断言 3 处终态标记、异常路径不写标记。

### 17.2 追踪流

- 新建 `pipeline_parts/tracking_flow.py`: `run_playbook_track` 与
  `run_playbook_weekly` 本体迁入。
- pipeline 转发器显式注入 today/now、开关、交易日、IC backfill、
  EOD 强制平仓、总权益与 module_file; monkeypatch 缝不变。

### 17.3 盘中证据

- `_gather_intraday_evidence` 与 sector→pulse 映射迁入
  `pipeline_parts/intraday_indicators.py`。
- pipeline 转发器注入 today/now、`datetime` 类与 pipeline `__file__`,
  保留既有 `datetime` freeze 测试缝和 data_root 注入口径。

### 17.4 行数与卫生

- `pipeline.py`: 8,692 (阶段 5 第二批后) → **7,904**;
  较 10,207 基线 **−2,303**。
- W456 按文件键搬家后:
  `debug_swallow` pipeline=23, generation_flow=3, tracking_flow=15;
  `except_stack` pipeline=51, generation_flow=8, tracking_flow=1。
  全局阈值未放宽, 仅迁移存量计数。

### 17.5 验收

- 生成/补量/定桶专项与 source contract: **17 passed**。
- 盘中证据 + 卫生/行数棘轮: **25 passed**; 最终专项合并 **34 passed**。
- `tests/playbook` 全量: **1043 passed**。
- `git diff --check` 与 `compileall(pipeline + pipeline_parts)` 通过。
- 生产复验: 重启前两库未 BOOKED 出场意图均为 0; 02:41:02 CST 重启
  PID 184156, 冒烟 **7/7**; 02:41:10 装载 17 个 bridge 候选并挂
  `BridgeVotingAdapter`, 02:41:22 auto_execute 处理 74 条指令并于
  02:41:25 正常完成, 退出快循环启动并检查 1 个未平仓, 零
  ERROR/Traceback/CRITICAL。

## 18. 阶段 7: auto_execute 前置风险闸迁移 (2026-10-09 盘中)

### 18.1 改动

- `_auto_execute_preflight` 本体迁入
  `pipeline_parts/risk_gates.py`: kill switch、regime/risk state
  fail-mode、日亏熔断四段前置闸。
- pipeline 留同名转发器, 显式注入 `_check_daily_loss_circuit`
  与 logger; `run_playbook_auto_execute` 仅在预检返回非 None 时
  原样返回跳口结果。
- 候选加载、预算、DB guardrail、下单、台账与 `continue` 顺序均不动。

### 18.2 行数与卫生

- `pipeline.py`: 7,904 → **7,869**; 较 10,207 基线 **−2,338**。
- W456 按文件键搬家: `except_stack` pipeline 51→47,
  risk_gates 6→10; 全局阈值未放宽。

### 18.3 验收

- entry revalidation + 行数/卫生棘轮: **199 passed** (含 226-class
  entry 套件中的前置闸测试)。
- `tests/playbook + tests/test_playbook_pipeline.py` 合并回归:
  **1116 passed**。
- 生产复验: 重启前两库未 BOOKED 出场意图均为 0; 02:47:51 CST 重启
  PID 184644, 冒烟 **7/7**; 02:47:56 装载 17 个 bridge 候选,
  02:48:06 auto_execute 处理 74 条指令并于 02:48:10 正常完成,
  退出快循环检查 1 个未平仓, 零 ERROR/Traceback/CRITICAL。

## 19. 阶段 8: signal context 存储迁移 (2026-10-09 盘中)

### 19.1 改动

- 新建 `pipeline_parts/signal_context_store.py`, 迁入
  `_compute_key_levels`、`_load_theme_context_cached` 与
  `_persist_signal_context`。
- pipeline 保留同名转发器; `_tc_cache` 与 pipeline `__file__`
  显式注入, 关键位和主题缓存回调保 monkeypatch 缝。
- 新增 `tests/playbook/test_signal_context_store.py`, 覆盖
  关键位排序去重与 mtime 缓存失效。

### 19.2 行数与卫生

- `pipeline.py`: 7,869 → **7,695**; 较 10,207 基线 **−2,512**。
- W456 按文件键搬家: `except_stack` pipeline 46,
  signal_context_store=1。

### 19.3 验收

- signal context 专项 + 行数/卫生棘轮: **20 passed**。
- `tests/playbook + tests/test_playbook_pipeline.py` 合并回归:
  **1118 passed**。
- 生产复验: 重启前两库未 BOOKED 出场意图均为 0; 02:54:22 CST 重启
  PID 185164, 冒烟 **7/7**; 02:54:27 装载 17 个 bridge 候选,
  02:54:38 auto_execute 处理 74 条指令并于 02:54:41 正常完成,
  退出快循环检查 1 个未平仓, 零 ERROR/Traceback/CRITICAL。

## 20. 阶段 9: auto_execute 预算/日帽装配迁移 (2026-10-09 盘中)

### 20.1 改动

- 新建 `pipeline_parts/execution_flow.py`, 迁入
  `load_budget_and_guardrails`。
- 装配覆盖 R1b 未平仓敞口、capital safety 预算、sizing base、
  日名义额度、日开仓数、单轮新开仓、在仓数与 DB 日帽读数。
- pipeline 留 `_load_execution_budget` 转发器, 注入
  `_resolve_sizing_base` 与 logger; 候选排序和后续执行顺序不变。
- 日帽 DB 契约测试同步指向新宿主, 继续锁死 routed DB 与
  `daily_guardrail_query_failed` fail-close。

### 20.2 行数与卫生

- `pipeline.py`: 7,695 → **7,605**; 较 10,207 基线 **−2,602**。
- W456 按文件键搬家: `except_stack` pipeline 43,
  execution_flow=3。

### 20.3 验收

- entry revalidation + 行数/卫生棘轮: **199 passed**。
- `tests/playbook + tests/test_playbook_pipeline.py` 合并回归:
  **1118 passed**。
- 生产复验: 重启前两库未 BOOKED 出场意图均为 0; 03:05:33 CST 重启
  PID 186189, 冒烟 **7/7**; 03:05:39 装载 17 个 bridge 候选,
  03:05:50 auto_execute 处理 74 条指令并输出预算/单日 guardrail,
  03:05:54 正常完成, 零 ERROR/Traceback/CRITICAL。

## 21. 阶段 10: 盘中触发流迁移 (2026-10-09 盘中)

### 21.1 改动

- 新建 `pipeline_parts/trigger_flow.py`, 迁入 `_check_intraday_trigger` 本体。
- pipeline 留同名转发器, 显式注入时间、当日、module_file、logger、
  触发/量能/流动性/借券/双锚/影子去重回调与模块级降噪状态。
- `_now_et`、`_today_et_str`、pipeline `__file__` 对应路径与 monkeypatch 缝
  保持不变; 判定顺序、continue 顺序、审计键与 JSONL 文件均不动。

### 21.2 行数与卫生

- `pipeline.py`: 7,605 → **6,581**; 较 10,207 基线 **−2,626**。
- W456 按文件键搬家: `except_stack` pipeline 43→38, trigger_flow=5。

### 21.3 验收

- 触发/低 ATR/pipeline 源契约: **109 passed**。
- `tests/playbook + tests/test_playbook_pipeline.py`: **1118 passed**。

## 22. 阶段 11: auto_execute 主流程迁移 (2026-10-09 盘中)

### 22.1 改动

- 新建 `pipeline_parts/auto_execute_flow.py`, 迁入
  `run_playbook_auto_execute` 本体。
- pipeline 留同名转发器, 注入 datetime、时间/交易日、module_file、logger、
  risk/budget/trigger/gate/audit/shadow 回调与 `INVALIDATED_SKIP_REASONS`、
  chase cap 状态。
- 候选排序、闸门、DB 写入、下单与 continue 顺序保持原样。

### 22.2 行数与卫生

- `pipeline.py`: 6,581 → **5,451**; 较基线 **−4,756**。
- W456 按文件键搬家: debug pipeline 23→20, auto_execute_flow=3;
  except pipeline 38→25, auto_execute_flow=13。

### 22.3 验收

- entry revalidation + pipeline: **254 passed**。
- same-day cooldown / dual arm / reprice source 契约: **52 passed**。

## 23. 阶段 12–14: 出场落账/监控/隔夜清理迁移 (2026-10-09 盘中)

### 23.1 改动

- 新建 `pipeline_parts/close_flow.py`, 迁入 `_close_trade_record`;
  注入 datetime、`_now_et`、分类/净值/observed extremes 回调与止损连发状态。
- 新建 `pipeline_parts/exit_monitor_flow.py`, 迁入
  `monitor_playbook_exits`; 注入函数属性、时间、快循环缓存/超时、
  逐仓规则、portfolio backstop、broker stop、落账与恢复回调。
- 新建 `pipeline_parts/overnight_cleanup.py`, 迁入
  `_force_close_orphaned_sim_positions`; 注入出场决策、释放 broker stop、
  落账、恢复与当日回调。
- pipeline 均保留同名转发器, monkeypatch 缝、函数属性状态与调用顺序不变。

### 23.2 行数与卫生

- `pipeline.py`: 5,451 → **3,577**; 较基线 **−6,630**。
- W456 按文件键搬家: debug pipeline 20→7, close_flow=9,
  exit_monitor_flow=3, overnight_cleanup=1;
  except pipeline 25→15, close_flow=3, exit_monitor_flow=5,
  overnight_cleanup=2。全局阈值未放宽。

### 23.3 验收

- close/exit recovery/gap/bounded/pipeline: **118 passed**。
- portfolio backstop + bounded fetch: **53 passed**。
- exit recovery / live arm / naive datetime: **92 passed**。
- `tests/playbook + tests/test_playbook_pipeline.py`: 阶段 12–13 后
  **1118 passed**; 阶段 14 后唯一源码宿主契约已同步, 专项 **49 passed**。
- 最终合并回归 (阶段 14 + 源码宿主同步后):
  `tests/playbook + tests/test_playbook_pipeline.py` **1118 passed**。
- 最终生产复验: 重启前两库未 BOOKED 出场意图均为 0; 03:51:43 CST
  重启 PID **189747**, 冒烟 **7/7**; 03:51:48 装载 17 个 bridge
  候选并挂 `BridgeVotingAdapter`; 03:51:58 退出快循环启动并检查
  1 个未平仓; 03:52:14 auto_execute 处理 74 条指令、输出预算/单日
  guardrail, 03:52:17 正常完成, 零 ERROR/Traceback/CRITICAL。

## 24. 复查修复闭环 + D-1/D-2 归因与登记 (2026-10-09, resume 会话, 用户裁「继续」)

### 24.1 复查修复批 (06:43–08:07 复查会话, 用户令「请修复」)

- **P0 修复**: `trigger_flow.py` 资金流背离影子写盘块裸 `Path/json/datetime`
  NameError 哑弹 (运行时必抛被 except 吞 ⇒ 影子静默死亡) — 按文件内局部 import
  惯例修复 + 补 `tests/playbook/test_falsification_capital_flow_shadow.py` 真写盘钉。
- **锚点测试重钉**: 约 48 条根目录源码锚点测试 `_SRC` 重指新宿主 (chase_cap/
  premarket/pullback/push/r8/be_passive/penetration/entry_anchor/horizon 白名单等)。
- **except:pass 净增清剿**: 棘轮 921→922 回落 (就地清剿非抬基线)。

### 24.2 resume 会话全量终验 (10-09 上午)

- 全量套件 **6911 passed / 39 failed / 70 skipped**; 39 红逐条归因, **零迁移/修复批
  新引入**: patient_exit ×21 既有债 (8047b494f `fee_broker` 存根, 本批已顺手修复
  → 40/40 绿) + D-1 ×2 + D-2 ×1 + ue_quote 污染 ×1 + 14 条 HEAD 隔离 worktree
  实证同红的既有债族。五条棘轮全绿; 生产 PID 221600 冒烟 7/7。

### 24.3 D-1 退出指纹漂移归因 + 登记

- **现象**: 现役指纹 `exit-ca4487172a6f` ≠ 9/26 裁决钉值 `exit-34c7153088a1`;
  2026-10-09 05:05 不变量自检已推 W1_exit_version_drift (未登记) +
  W1_oct_cohort_broken (oct 窗 9/17→ 出现 2 哈希)。
- **归因** (隔离 worktree 逐提交二分, 9/26 钉值精确复现): 唯一直接跳变点 =
  `410297295` (10-08 22:04, RFC-106 §5.2) — `short_horizon.overnight_gap_stop_pct=3.0`
  由代码默认同值显式写入 yaml, **零行为变更**, 整文件哈希副作用 (config.py R-4b
  自述的「过宽代价」)。
- **登记**: `exit-ca4487172a6f` 入 `config_freeze_baseline.json` exit_versions
  (含行为等价声明) + `test_kr2_freeze_exemption.py` 键集钉同步 ⇒
  **W1_exit_version_drift 一维解除**。
- **维持 BROKEN 不动**: 主 cohort (9/3→) 与 oct cohort (9/17→) 两维判分维豁免
  通道关闭, 按预注册回滚线须开跑会级裁决, **不得静默移窗**; era 模块按 RFC-138
  只标注不降级, `test_era_block_live_no_drift` 转阳性对照钉 (检出 applies=False
  = 守卫有牙齿; 行为等价声明在豁免台账)。

### 24.4 D-2 live 库墓碑前提归因

- 8 行 6 月 `owner='manual'` open_positions 墓碑于 **10-03→10-04 备份窗口**消失
  (RFC-130/131 双臂收口期); 主不变量 (实盘行不得误标 sim) `bad=0` 仍成立;
  历史在 trade_records (17 行含 6 月期) / live_round_trips (68) /
  live_deal_records (122) 完好。测试前提更新见 §24.2 同批。

### 24.5 ue_quote_freshness 影子到期裁决 (逾期 11 天补跑)

- 影子 9/28 已达预注册 10 轮门槛未被触发; 机械裁决器 18 轮 → **NOT_READY**
  (genuine 龄 P95=1329.2s >> 90s, 激活=系统性误杀; 维持 9/15 用户裁 C 现状,
  禁止事后改阈值)。注册条目记裁决 + 挂账 (`no_adjudication_reason`) 阻断
  refresh_counts 重复提升 (该提升即全量套件「单跑绿/全量红」污染源)。

## 24. 独立复查 + 修复批 (2026-10-09 晚, 用户令「请复查」→「请修复」→「请优化调整」)

### 24.1 复查方法与结论

第二 agent 独立复查 (6 个保真核对 agent 按模块组逐语句对照 HEAD + 本地实证):
迁移本体**保真通过** — auto_execute 主循环 1079 行仅 3 处注入替换; close/monitor/
overnight 20 条 SQL 逐字相同; 状态容器全部按引用注入无重绑; 函数属性回环作用于
pipeline 转发器本体; `_build_instructions_from_votes` 拆分后 continue 链与
filter_audit 键集逐项等价; 45 个外部 import 面完整; 零顶层循环导入。

### 24.2 抓出并修复

| # | 级 | 问题 | 修复 |
|---|---|---|---|
| 1 | P0 | trigger_flow.py 资金流背离影子块漏带 `Path/json/datetime` (迁移时唯一漏网站点, AST 全包扫描确认), NameError 被 except 吞成 warning ⇒ 影子静默死亡 (十月重裁数据底座; 实证 jsonl 自 9/25 无新行属门未触发, 暂未丢数据) | 按文件内 `_Path/_json/_dt` 惯例补局部 import; 新增 `tests/playbook/test_falsification_capital_flow_shadow.py` 钉真落盘 + record-only 不拦单 + monkeypatch 缝, **变异测试实证** (buggy 态红/修复态绿) |
| 2 | P1 | 48 条根目录源码锚点测试断裂 (chase_cap×15/premarket_inv×4/pullback×6/phase1×3/push×4/r8×3/be×2 等) — 迁移回归口径只含 tests/playbook + test_playbook_pipeline, 漏根目录契约族 | 3 agent 并行重钉 (只改源指针/拼接, 断言零改动); horizon_confidence 白名单显式登记 instruction_builder.py |
| 3 | P1 | except:pass 棘轮 921→922 (迁移本体 −53/+53 此消彼平; 净增来自夹带批 data_retention/gate_attribution, 及当日新提交 probe/insider) | 4 处 R5 留痕清剿 (计数进输出行/stderr), 棘轮复绿 |
| 4 | P2 | intraday_indicators 迁移剥损设计理由 (vwap_slope 累计VWAP不可求导论证/vol_ratio P1-3 U型分布+count=800 实证/gather_evidence P1-2 新鲜度审计 21 条注释) | 从 HEAD 逐字补回 (适配新变量名) |
| 5 | P2 | `_evaluate_technical_direction` 日志丢 `{symbol}` | 补 symbol 参数并接线 (转发器+调用点), 消息对齐 HEAD 原文 |
| 6 | P2 | pipeline.py `_LIQUIDITY_CACHE`/`_BORROW_POOL_CACHE` 死声明 (活体已在新宿主) | 删除+留指针注释, 防直引分裂 |

### 24.3 优化批 (开盘体检后用户裁「请优化调整」)

- **watchdog 跨开盘告警两阶段化**: 跨界 WARNING+停 ping 交回 watchdog 不变;
  宽限 `scheduler.watchdog_cross_open_grace_sec` (默认 120s, yaml 已登记) 未收轮
  才 ERROR+critical。缘由: 10-09 实证 121s 慢轮跨开盘 0.2s 自解仍 🚨 推送。
  真卡死检出延迟 0s→grace, watchdog 600s stale 兜底不变; 回滚: 键置 0。
  测试钉更新 (grace=0 钉升级路径 / grace=∞ 钉宽限内零推送) + 修出
  `0 or 120` falsy 陷阱一处 (显式 None 判定)。
- **exit_lifecycle HELD 日志两级分流**: `*_pending` 瞬态族降 INFO 不打栈
  (playbook 层宽限/升级不动), 非瞬态守卫拒绝保留 WARNING+exc_info
  (9/13 留栈教训); 新增 `tests/execution/test_exit_lifecycle_held_logging.py`
  钉归一先于分流/瞬态无栈/非瞬态带栈; W456 口径 INFO 不计违例, 棘轮零扰动。
- **test_extended_hours_exit_cron 锁隔 tmp** (与每分钟 cron 共锁假红, 实证一次)。

### 24.4 流程教训 (制度化)

① 「N passed」子集验证不等于全量 — 迁移/重构批的回归口径必须含 tests/ 根目录
源码锚点契约族; ② 棘轮不得自行圈定「四件」— 本批迁移方与复查方各自漏跑
except_pass, 唯全量套件抓到; ③ 全量跑测期间禁并行 pytest (共享生产 data/ ⇒
活体数据测试假红, 本批三例误报实证); ④ 语句级对照 + AST 裸名扫描是迁移批的
必要复查面 (P0 哑弹只有这层能抓 — 1118 绿没抓住因为它零测试覆盖)。

### 24.5 验收

- 棘轮家族 5 件 34 passed; 受影响面 (watchdog/execution/playbook/pipeline/ext_cron/
  premarket_overrun) 1402 passed; P0 变异测试双向实证 (buggy 态红/修复态绿)。
- **全量回归终跑 (串行无并行污染, 含 patient_exit 已修复 40 条): 6987 passed /
  16 failed / 70 skipped** — 16 红全部经 HEAD 隔离 worktree 实证为既有债
  (chokepoint 祖父名单 / manual_hold SSOT / ops_scripts_sync / EXT 臂等,
  与本批零交集); 修复前对照为 67 failed (48 锚点 + 1 棘轮 + 17 既有 + 1 污染)。
