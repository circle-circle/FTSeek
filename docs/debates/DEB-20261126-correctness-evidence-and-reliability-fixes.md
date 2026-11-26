# DEB-20261126 · 正确性、证据口径与工程可靠性修复

- 状态：**已实施，定向回归通过；未提交、未重启、未做生产自然验证**
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
- 未提交、未推送；本文件只记录当前工作树实施状态。
