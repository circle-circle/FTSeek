# DEB-20261126 · 正确性、证据口径与工程可靠性修复

- 状态：**已实施并提交推送；未重启、未做生产自然验证**
- 来源：用户对另一 Agent 优化分析的复查意见，及用户「按照建议进行优化修复，有把握的可以直接落地」授权。
- 边界：本批恢复既定契约，不新增策略门、不改退出参数、不调整仓位/方向权限、不碰四锁。

## 1. 复核基准与主要结论

原分析基于 2026-11-02 的 `5bec3843`，实施时当前 HEAD 为 `ca2d32f28`，工作树含其他会话 WIP。复核确认迟到 single 报价、配置重载、复盘库路径、回放毛/费/净与官方保真门、日 K 并发/配额、CI/本地静态门仍存在问题；11 月 OKR 未接入已由此前 `b4ccb3bb3` 修复，不重复实施。

## 2. 证据与测试基础

### 2.1 复盘库显式路径隔离

- `ReviewDatabase` 在构造时规范化 `db_path`；仅与默认 `review` 池路径一致的实例复用池连接。
- 自定义路径直接连接实际文件，提交/回滚后关闭，禁止经全局 `"review"` 槽位串库。
- 删除 `tests/conftest.py` 对 `ftseek_us_review.db` 的已知生产写入豁免。
- 新契约测试覆盖 A/B 文件隔离、`PRAGMA database_list` 实际连接文件、相对路径规范化和默认池路由。

### 2.2 回放毛/费/净与保真机门

生产库只读 coverage（`mode=ro + query_only`）：932 笔候选中 930 笔同时有 `pnl_net`/`fees_est`，且毛−费=净全部一致；2 笔两列均缺。

- `load_trades` 增读 `pnl_net`、`fees_est`、`fees_source`。
- 为兼容历史调用方，`live_pnl` 保持旧含义（毛利），新增 `live_gross`、`live_net`、`live_fee_est`，不一次性改义。
- `_fee_of` 优先使用明确费用列，其次毛−净；费用未知或超过既有 3% 名义上限返回 `None`，不再静默当零。
- 官方入口跳过费用未知样本并报告 `fee_coverage`；输出分列 `live_sum_gross/live_sum_fees/live_sum_net`。
- `reason_match_pct < 0.80` 时仅输出 baseline 验证诊断，`variants={}` 且 `frozen_by_fidelity=true`，不再继续跑变体网格。
- T 臂、ATR/BE/几何回放直接消费方同步处理费用未知，防止共享修复后重新吞成零费用。
- `docs/REPLAY-SCRIPT-LEDGER.md` 同步新费用/保真契约；OKR-2026-10 在 `okr_status.py` 注释与协作宪章中统一标为历史块（11 月判分器沿用既有 `b4ccb3bb3`，未重复开发）。

**历史证据边界**：不覆写旧产物。绝对净额、胜率、杀赢/救亏分类及与 live 的绝对对账，须按新口径重跑后才能继续引用；同笔同费配对差可能抵消费用，不一概判为无效。

## 3. 运行时正确性

### 3.1 迟到 single 报价收编

- `_pb_exit_bounded_fetch` 新增可选 `result_to_quotes`，单标的调用显式补回 symbol；批量旧调用不变。
- 结果盒记录实际完成时刻，缓存按完成时刻计龄，迟到结果收编时不再刷新完整 TTL。
- 保留有界线程、同 label 单飞、超时放弃本轮、缓存 120s 上限和批量兼容。
- 新增迟到 single、超龄、无价及既有批量收编测试。

### 3.2 配置 last-known-good

- `ConfigContainer.reload()` 在独立候选实例中加载；YAML 成功后才一次性替换 `__dict__`。
- 坏 YAML、缺文件、顶层非 mapping 或候选初始化异常均保留现役完整状态并抛错。
- 热重载器失败计数不再虚增成功；回滚逻辑不再向只读 `_raw_config` property 强写造成二次异常。
- 覆盖坏 YAML/缺文件保留旧值、失败计数、修复后重试成功。

## 4. 工程可靠性

- 日 K 落盘：实例锁内再以 `flock` 保护完整读—合并—替换；临时文件按 PID/线程/纳秒唯一化；成功后才清 dirty 和推进节流时间，失败保留下轮重试。
- 配额：单标的冷却检查、全局窗口检查与预占合并到同一临界区；日 K 第二次物理请求重新预占全局配额，窗口满则放弃重试并走既有降级。
- CI：显式 `requirements.lock` 缓存依赖、完整 checkout、`git diff` 错误不再隐藏；修改文件同时跑 `py_compile` 与 pyflakes。
- 静态门：锁文件加入 `pyflakes==3.4.0`；工具不可用退出 2，不再静默放行。pyflakes 当前主要由本地 pre-commit 与新增 CI 步骤消费。

## 4A. 独立复查返回的 4 项补修

独立只读复查确认主要实现后，又指出并闭环以下遗漏：

1. **空/假值 YAML**：`safe_load` 后不再 `or {}`；空文件、纯注释、`false`、`[]`、`{}` 均拒绝候选并保留 last-known-good，热重载失败计数正确。
2. **ATR NULL 费用**：`_load_metadata` 保留 SQL NULL，主循环的 `_fee_of` 回退与 `fee_unknown` 跳过因此可达；真实 `0.0` 费用与 NULL 分开。
3. **BE 净对净**：反事实 live 侧改 `live_net`，并列展示 `live_gross/fees/live_net`，`nobe_net > live_net`，不再净反事实对毛实盘。
4. **零可回放/零候选样本诊断**：全部样本费用未知或缺 bar 时，官方入口覆盖旧产物，写 `status=NO_REPLAYABLE_TRADES`；窗口零候选时写 `NO_CANDIDATE_TRADES` 且不连接 OpenD。两者均 `variants={}`、`frozen_by_fidelity=true` 并带排除计数，禁止下游沿用上一轮变体。

同时把迟到收编测试的 50ms 时序余量增至 150ms，降低高负载 CI flake 风险。其余复查区域（迟到收编、复盘库、日 K 并发/配额、CI）未发现高置信问题。

## 5. 验证实录

可复跑主命令：

```bash
PYTHONPATH=. ./venv/bin/python -m pytest \
  tests/playbook/test_exit_price_bounded_fetch.py \
  tests/test_config_container.py tests/test_review_tables.py \
  tests/test_exit_replay_backtest_caliber.py tests/test_atr_trail_replay_c_fee.py \
  tests/test_exit_t_arm_shadow.py tests/test_futu_dailyk_quota.py tests/core/test_hot_reload_snapshot_invalidation.py \
  tests/test_conftest_sqlite_guard.py tests/test_winner_overnight_settle.py \
  tests/playbook/test_entry_context_cache_boundary.py tests/test_atr_trail_replay.py \
  tests/test_weekly_optimizer_contracts.py tests/test_replay_engine_import_smoke.py \
  -q --tb=short -p no:cacheprovider --basetemp=/tmp/ftseek-correctness-complete \
  -k 'not module_level_consumers_import and not lazy_import_consumers_import'
```

结果：**228 passed，7 deselected，2 warnings（既有弃用/安全警告）**。其中独立复查 4 项补修后的核心子集为 **92 passed**（含零候选分支）。提交前进一步运行全部 24 个变更/新增测试文件（含其他会话 WIP），结果 **679 passed，1 warning**。

其他：

- 所有本批 Python 文件 `py_compile` 通过，`git diff --check` 通过。
- `scripts/check_undefined_names.py` 检查本批 Python 文件通过；`python -S` 模拟缺依赖，实测退出 2。
- 棘轮家族运行结果 **96 passed / 2 failed**。失败项：① `.qoder/worktrees` 被 `except:pass` 扫描重复计入，实测总数 1814（主树 907 + `.qoder` 907）超过基线 899；② 其他 WIP 的 5 处 W6 日志栈命中。修复后按本批文件重新调用同一 `_check_log_hygiene`，**owned_hygiene_hits=[]**；未调基线、未改其他 WIP。
- 补充配置治理回归 **73 passed / 1 failed**。唯一失败是既有 `default_method`、`simulated_mode_force_options`、`reasoning_effort` 三个死键普查项，近期 handoff 已记录同组失败；与本批 reload 改动无关，未修改豁免表。
- 补充 Futu provider 相邻回归（channel collect / K线 cache / sick gate / quote ready / snapshot N/A / daemon atexit）**120 passed**。

## 6. 未验证与部署边界

- 未运行全量 pytest、未运行 GitHub Actions、未做生产自然退出/坏 YAML 运维演练、未重启服务。
- 模块级网络型回放 import 冒烟 7 项被刻意 deselect，避免在本验证中触达 OpenD；不代表这些路径完成网络验证。
- 当前工作树含其他会话 WIP，不能借本批重启把未授权源码一并上线。部署前须重新核在途出场及 `git status --porcelain -- app/ scripts/`，只隔离本批生产文件，再按 `systemctl restart ftseek-us` + 启动冒烟执行。
- 全量归账提交 `567f09cbc`（185 文件）已推送 `rfc-060-phase1-hotfixes`，并已 fast-forward 合并/推送 `main`；两远端同 SHA。合并 main 后两轮变更测试均 678/679，失败分别为 signed-quantity 与 query_error-mom 的既有负载时序问题；所在文件单跑 146 passed、signed-quantity 目标单跑 20 passed，未冒称主线全绿。

## 7. 独立复核（2026-11-26 22:3x CST，只读；零代码/配置/DB 写/订单/重启）

### 7.1 总结论与收益读数

- 复核基准：HEAD `2bfaea477`；`git status --porcelain -- app/ scripts/ config/` 为空。服务 `active/running`，MainPID 80581，启动 2026-11-26 21:04:04 CST，NRestarts=0。运行时 `AUTO_TRADE_ACCOUNT=sim`、`TRADE_ENV=1`、`ALLOW_REAL_TRADING=false`，`playbook.execute_mode=futu_simulate`；LIVE 无未平仓，SIM 未平仓为 BA SHORT 23@160.17、BABA SHORT 47@81.88、GS SHORT 5@789.14。
- 本批大部分属于“修尺子/补护栏”：恢复既定契约、减少错误读数与工程故障，方向合理；但**不等于已证明盈利**。行为相邻项与 RFC-217 实验必须单独归因，不能混入“纯正确性/纯遥测”叙事。
- 官方判分只读复跑：`PYTHONPATH=. ./venv/bin/python scripts/okr_2026_11_scoring.py --as-of 2026-11-25 --json`，日志 `/tmp/ftseek-audit-okr-20261125.json`。DA 整窗 n=146、净 −$1587.23、**−24.8369 bps**，KR1 NOT_MET；pre n=107、−27.0715 bps；post qwen4-max n=39、**−17.4678 bps**，UNDERPOWERED，MDE80=80.0408，CI80=[−45.2366,+27.9932]。KR2 死单率 21.23% MET。post 达标规划日 2026-12-17；11-30 结算不顺延。`as_of` 不是 SQL 上界，当前最大 trade_date 为 11-25。
- 毕业门禁异常 fail-close 经隔离注入异常验证为 true（`/tmp/ftseek-audit-graduation-failclose-20261126.log`）；但 engineering probe 路径刻意豁免毕业门禁，不得夸称为 live-arm 全路径毕业锁。

### 7.2 正确性/测量项：通俗解释与合理性

- **迟到 single 报价收编**（`app/playbook/pipeline.py:3517`、`:3541`）：迟到结果仍补回 symbol，并按取价线程实际完成时刻计龄。通俗说，快递迟到仍写清收件人，且按送达时间算新鲜度，不能冒充刚取的新价。合理，属出场取价正确性，非 alpha。
- **配置 last-known-good**（`app/core/config_container.py:514`、`:521`、`:526`）：先在候选实例加载，成功才一次性替换现役字典；坏 YAML/缺文件/非 mapping 保留旧配置并抛错。通俗说，先试穿再换衣，不能把破衣服直接换上。合理；候选 `__init__` 会重解析环境派生配置，稳定生产环境下副作用低。
- **复盘库显式路径隔离**（`app/storage/review_tables.py:29`、`:47`）：显式 `db_path` 直连规范化后的实际文件，只有默认路径复用生产池。通俗说，写明去 A 仓库就不能被全局路由带去 B 仓库。合理，防复盘/测试串库。
- **回放毛/费/净与保真机门**（`scripts/exit_replay_backtest.py:574`、`:694`、`:706`、`:748`）：费用未知跳过，毛/费/净分列；reason 一致率 <0.80 冻结变体；零候选/零可回放覆盖旧产物。通俗说，尺子不准或样本不可回放时，只出诊断，不出可裁决结论。合理。小口径 caveat：`fee_coverage.known_n=len(runnable)` 排除了 `no_bars`，分母不是全部候选；未见生产 gate 消费该 coverage。
- **日 K 落盘/配额**（`app/data/futu_lv3_provider.py:1600`、`:1652`、`:2057`）：flock 保护读—合并—替换、唯一临时文件、成功后才清 dirty、第二次物理请求重新预占配额。方向合理，但确认存在 lost-dirty 竞态，见 §7.4。
- **CI/静态门**（`.github/workflows/ci.yml:42`）：完整 checkout、lockfile 缓存、pyflakes 缺依赖退出 2。合理，防“工具不在就静默放行”。小 caveat：静态 diff 仍 `HEAD~1`，多 commit 批次可能漏扫中间提交。

### 7.3 行为相邻与研究项：不能标成纯遥测

- **教训验证闭环**（`app/review/lesson_injector.py:236`；调用 `app/core/scheduler_triggers.py:614`）：只以已平仓净损益验证，盘前窗口按 ET 04:00–09:30；会更新 confidence/expiry/validation_count。活跃教训进入 battle-plan prompt（`app/llm/agent_bridge.py:413`）与交易约束（`app/pipeline/signal_pipeline.py:284`）。当前相关活跃教训：high_signal_loss 32、premarket_loss 5、preclose_loss 1。属行为相邻，收益/风险须单独归因。
- **陈旧 trail-state 丢弃**（`app/risk/trail_state_store.py:30`；消费 `app/pipeline/position_monitor.py:406`、`app/core/scheduler_core.py:1877`）：防止历史仓残留状态恢复进新仓。属正确性/状态卫生，不是 alpha。
- **RFC-216 VIX 平滑**（`app/playbook/market_regime.py:131`）：20–22 线性过渡，端点与区间外不变。通俗说，消除门槛边界的 3 分跳变。合理，但无收益证明。
- **Phase A regime 快照**：record-only，`regime_snapshot_used=False`，供基数/测试/审计，不接入新 gate。合理，属证据采集。
- **FLAT_ALL 空仓段去重**（`app/risk/position_exit_monitor.py:826`，commit `0fc72268f`）：只改日志，每状态段一条 WARNING 替代每轮重复 CRITICAL。测试覆盖；自然 WARNING 路径未被触发，日志中新 warning 计数为 0，旧重复 CRITICAL 可见。收益是降噪，不是交易行为改善。
- **entry_anchor_dual**（`app/playbook/shadow_registry.py:1345`）：阶段 1 已 KILLED，但采集继续，因 W7/replay 脚本仍消费双锚分歧数据。合理；KILLED 否的是立项，不是否记录本身。
- **verdict_outcomes 台账**：record-only，15091 行（instruction 10777 / da_checkpoint 4314），度量选择/判别力含 SKIP，不是生产 gate。合理，属研究尺子。

### 7.4 确认缺口（本轮只记录，不实施修复）

1. **日 K lost-dirty 竞态（中危，已隔离复现）**：`_daily_disk_save` 在 `_daily_disk_lock` 内合并写盘并于 `app/data/futu_lv3_provider.py:1652` 清 dirty；生产者 `:2057`/`:2060` 更新内存缓存与 dirty 时不持同一锁。隔离临时路径复现：`/tmp/ftseek-audit-dailyk-race-20261126.log`，输出 `{"dirty_after_flush":false,"lost_update_reproduced":true,"memory_symbols":["US.NEW","US.OLD"],"persisted_symbols":["US.OLD"]}`。影响是磁盘缓存更新丢失，不是内存即时丢失；下一进程可能缺缓存并多耗 OpenD 配额。建议同锁保护生产侧更新，或 snapshot+version 校验；本轮未改代码。
2. **RFC-217 SIM-only 闸口径过强（治理/未来 live-arm 缺口，当前无 LIVE 暴露）**：`no_trade_short_sim_experiment_active()`（`app/playbook/pipeline_parts/risk_gates.py:193`）用 `is_live_capable_env()`（`app/core/live_readiness.py:50`），后者只查 `AUTO_TRADE_ACCOUNT=real AND ALLOW_REAL_TRADING=true`。live_manual_arm 镜像路径可 `target_env="real"`（`app/playbook/pipeline_parts/auto_execute_flow.py:2185`、`:2223`；镜像元数据/1 股见 `risk_gates.py:541`、`:569`、`:577`）。隔离复现 `/tmp/ftseek-audit-sim-only-gate-20261126.log`：`ALLOW_REAL_TRADING=true`、`AUTO_TRADE_ACCOUNT=sim` 时 `experiment_active=true`、`is_live_capable_env=false`。当前生产 ALLOW=false、probe LONG，故无当前 LIVE 暴露；但“LIVE-capable 恒不生效”若被解释为“任何 real-target 路径恒不生效”则过强。建议把闸口径显式限定到当前环境定义，或覆盖 live-arm 镜像能力；本轮未改代码/配置。
3. **RFC-217 cohort 标记与收益归因不完整**：只读 DB 日志 `/tmp/ftseek-audit-db-readonly-20261126.log`。11-25 标记指令 10 条，执行 3 条（CAT5765/GS5766/BABA5774），其中 MSFT5771 方向为 SKIP，不能写“全部 SHORT”。已平实验仅 CAT1949 净 +$9.96（毛 14.07−费 4.11，SL_BREAKEVEN）；BABA1950/GS1951 未平。trade metadata 标记计数 0，outbox 仅 GS BOOKED，CAT/BABA 靠 decision_id 关联。n=1 已平远低于 n≥30/10 交易日。11-25 总已平 +$215 中 +$205.04 来自旧仓，不能归给实验。建议 record-only 补齐标记链路；本轮未写库。

### 7.5 验证凭据

- 正确性主命令见 §5，独立复跑 **228 passed，7 deselected，2 warnings**：`/tmp/ftseek-audit-correctness-20261126.log`。
- 行为/研究相邻子集复跑 **236 passed**：`/tmp/ftseek-audit-behavior-20261126.log`。命令：

```bash
PYTHONPATH=. ./venv/bin/python -m pytest \
  tests/playbook/test_no_trade_short_sim_experiment.py \
  tests/review/test_lesson_outcome_validation.py \
  tests/risk/test_exit_observability_and_state_hygiene.py \
  tests/test_verdict_outcomes_ledger.py \
  tests/test_market_regime.py \
  tests/test_da_chain_contract_audit.py \
  tests/test_da_input_freshness_manifest.py \
  tests/test_da_event_context_shadow.py \
  -q --tb=short -p no:cacheprovider \
  --basetemp=/tmp/ftseek-audit-behavior-20261126
```

- 毕业门禁研究库 pin：**6 passed**，`/tmp/ftseek-audit-graduation-pin-20261126.log`。命令：

```bash
PYTHONPATH=. ./venv/bin/python -m pytest tests/test_rfc080_research_pin.py \
  -q --tb=short -p no:cacheprovider \
  --basetemp=/tmp/ftseek-audit-graduation-pin-20261126
```

- 官方判分：`/tmp/ftseek-audit-okr-20261125.json`；只读 DB：`/tmp/ftseek-audit-db-readonly-20261126.log`；补充只读计数/服务边界：`/tmp/ftseek-audit-supplement-20261126.log`；两项隔离复现见 §7.4。

### 7.6 未验证与下一步

- 未跑全量 pytest/GitHub Actions；未做生产自然退出取价、坏 YAML 运维演练、未来 live-arm 路径自然验证、完整 tick/订单簿回放、收益显著性检验；未触发自然 FLAT_ALL 新 WARNING；未补齐实验标记写库。
- 下一步优先把日 K lost-dirty 竞态与 SIM-only 闸口径作为独立正确性补丁评审/修复，各带契约测试；RFC-217 维持原 cohort/n≥30 或 10 交易日/硬停，不据 n=1 宣称空头 edge；教训闭环与 VIX 平滑等行为相邻项单独归因。任何行为变更另案、一次一变量，不新增默认 gate。

## 8. 按建议实施两项独立正确性补丁（2026-11-26 22:4x CST，本地；未重启/未部署）

- 状态：用户「按照建议执行优化逻辑」后，仅实施 §7.4 第 1/2 项；第 3 项 cohort 标记完整性仍 record-only 另案。HEAD `2bfaea477`，未提交/未推送；保留其他会话既有 WIP。服务仍 active/running，MainPID 80581，21:04:04 CST 启动，NRestarts=0。
- 边界：不新增策略门，不改 RFC-217 cohort、n≥30 或 10 交易日、提前裁停/硬停、SHORT 0.3、LONG 拦截、方向权限、阈值、peak/tripped/UNKNOWN、四锁；不写交易库、不下单、不改配置。

### 8.1 日 K lost-dirty：生产侧更新与落盘清 dirty 同锁

- 通俗逻辑：旧实现落盘线程拿着旧内存快照写文件，并在写完后把“有待写数据”的 dirty 清掉；生产者却可在锁外把新标的塞进内存并置 dirty。新数据若插在旧快照写出与清 dirty 之间，就会被旧快照误清，只留在内存，重启后磁盘缺条目。
- 修复：`app/data/futu_lv3_provider.py` 将 `_daily_disk_lock` 改为 `threading.RLock()`，并把 `get_daily_kline` 生产侧的缓存/time/dirty 更新与 `_daily_disk_save()` 包进同一锁。落盘/load/save 已用同锁，RLock 允许生产侧持锁后重入 save；未引入反向 flock→实例锁顺序。
- 收益与代价：收益是跨进程/重启日 K 缓存完整，降低冷启动重复拉取和 OpenD 配额打穿/回退风险；不是 alpha。代价是落盘 I/O 期间同实例新生产者短暂串行，属正确性优先的小延迟。
- 契约测试：`tests/test_futu_dailyk_quota.py::TestGetDailyKlineGate::test_daily_disk_producer_during_flush_keeps_dirty` 暂停 `json.dump`，让真实 `get_daily_kline` 生产者在旧快照写出期间新增标的；断言 flush 后 dirty 仍为 true，强制落盘后 OLD/NEW 均在磁盘。

### 8.2 RFC-217 SIM-only：复用 LIVE 臂共享门探测 SHORT real-target 能力

- 通俗逻辑：旧闸只把 `AUTO=real AND ALLOW=true` 当 LIVE-capable；但 `live_manual_arm` 可在 `AUTO=sim` 下走 `target_env=real` 镜像。若未来共享 LIVE 门接受 SHORT，SIM 实验指令可能镜像到真钱。
- 修复：`app/playbook/pipeline_parts/risk_gates.py::no_trade_short_sim_experiment_active()` 保留原 `is_live_capable_env()` 闸；当 `ALLOW=true` 且 live-arm enabled 时，复用 `evaluate_live_arm_gate(..., instruction_direction="SELL_SHORT")` 做结构能力探测。共享门若可接受 SHORT real-target 镜像，实验恒 False；能力/配置读取异常也 False。探测用 best-case transient 参数（TRADING/0 仓/未 halt），不把临时 halt/休市当成 SIM 许可。
- 当前生产不变：`ALLOW=false` 时跳过 live-arm 探测；当前 probe=LONG，共享门拒绝 SELL_SHORT，实验仍 active。隔离当前配置读数 `/tmp/ftseek-opt-two-fixes-20261126-currentlike.log`：`experiment_flag=true, experiment_active=true, live_arm_enabled=true, probe_direction=LONG`。
- 收益与代价：收益是堵住未来 SHORT live-arm 镜像绕过 SIM-only 的口径缺口，且方向能力单源复用、不复制一份方向规则；不是新增交易门。代价是 ALLOW=true 且 arm enabled 时多一次轻量配置/waiver/共享门读取；当前 ALLOW=false 无额外探测。
- 契约测试：`tests/playbook/test_no_trade_short_sim_experiment.py` 新增 ALLOW=false 跳过探测、LONG-only armed 仍 active、共享门接受 SHORT 时 inactive、能力读取异常 fail-close 四例。

### 8.3 验证凭据

- 针对性：`PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 ./venv/bin/python -m pytest tests/test_futu_dailyk_quota.py tests/playbook/test_no_trade_short_sim_experiment.py -q --tb=short -p no:cacheprovider --basetemp=/tmp/ftseek-opt-two-fixes-20261126-targeted` → **40 passed**，日志 `/tmp/ftseek-opt-two-fixes-20261126-targeted.log`。
- 正确性相邻：§5 主命令复跑 → **229 passed / 7 deselected / 2 warnings**，日志 `/tmp/ftseek-opt-two-fixes-20261126-correctness.log`。
- 行为/LIVE 相邻：§7.5 行为子集 + daily_loss/live_arm P0/waiver/admission/entry_data_bounded_fetch → **455 passed**，日志 `/tmp/ftseek-opt-two-fixes-20261126-behavior.log`。
- 隔离复现：日 K 修复后 `/tmp/ftseek-opt-two-fixes-20261126-dailyk-fixed.log` 输出 `dirty_after_flush=true, lost_update_reproduced=false, persisted_symbols=["US.NEW","US.OLD"]`；当前配置 SIM-only 读数见 §8.2。
- 静态：本批 2 源 + 2 测试 `py_compile` 与 `scripts/check_undefined_names.py` exit0；`git diff --check` exit0。

### 8.4 未验证与部署前置

- 未跑全量 pytest/GitHub Actions；未做生产自然日 K 并发落盘、未来 SHORT live-arm 自然镜像、完整 tick/订单簿回放或收益显著性检验；未补齐 RFC-217 trade metadata/outbox 标记。
- 本批仅本地源码/测试/文档，**未重启、未部署、未提交、未推送**。Python 源码须重启才生效；后续若用户明确授权部署，先核在途出场 SQL 与 `git status --porcelain -- app/ scripts/`，只隔离本批生产文件，不夹带其他 WIP，再 `systemctl restart ftseek-us` + `PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py --wait 90`。
- 回滚：revert 本批源码补丁；若只需停实验，配置 `no_trade_short_sim_experiment_enabled=false` 热载即恢复 NO_TRADE 全禁旧门，已开仓位仍由原退出链管理。

## 9. 部署、重启与推送凭据（2026-11-26 22:54 CST）

- 授权：用户明确「请重启、部署、提交、推送」。本批仅上线 §8 两项正确性修复；DEB-20261125 既有 WIP 保留未提交，`app/ scripts/` 重启前无其他待上线改动。
- 提交/推送：代码提交 `29c4a757dd0ec7b70eda771b44bd0401668a9bfd`（7 文件，337+/8-），`git push origin main` 为 `2bfaea477..29c4a757d`；远端 `refs/heads/main` 同 SHA。两处生产源码工作树 SHA256 与 HEAD blob 一致：`futu_lv3_provider.py=5b07a0bd21c1d1fa0c9e8d2635936fe19dbe58a428e5bc47fc1a687e742dda17`，`risk_gates.py=22f6803d53467984acee7f5aa1649c1783014535182eaf222f73d3c08b19d377`。
- 重启前只读核：SIM `trade_records` 未平 BA/BABA/GS（id 1939/1950/1951），LIVE 未平 0；`playbook_exits.db` 与 `exit_ledger.db` 非 BOOKED intent 均 0、非 TERMINAL leg 均 0。凭据 `/tmp/ftseek-opt-two-fixes-20261126-pre-restart-ledger.json`、`/tmp/ftseek-opt-two-fixes-20261126-pre-restart-final.json`。
- 重启：22:53:04 CST 执行 `systemctl restart ftseek-us`；22:53:22 CST 新 MainPID `109261`，active/running，NRestarts=0。重启后只读复查持仓与出场账本同前。
- 启动冒烟：`PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py --wait 90` 于 22:53:42 CST **7/7 exit0**：ConfigValidator、漂移检测、热重载、SSOT 审计 0 BLOCKING、双臂安全确认、退出快循环、零 Traceback/CRITICAL。日志 `/tmp/ftseek-opt-two-fixes-20261126-startup-smoke.log`。
- 未验证：全量 pytest/GitHub Actions、生产自然日 K 并发落盘、未来 SHORT live-arm 自然镜像、完整 tick/订单簿回放、收益显著性检验仍未做；RFC-217 标记完整性仍 record-only 另案。下一步仅观察自然链路，不人工造单、不清 UNKNOWN 锁、不重置峰值/累计止损。
