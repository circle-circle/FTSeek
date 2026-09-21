# DEB-20260921 Playbook 在仓标的帽与清仓重算

> **⚠️ 2026-09-21 16:40 CST 修订裁决**：本文档首版把 5 帽装在了盘中 auto_execute，
> 用户复核后修订口径 —— **见文末「修订裁决」节，首版结论中与之冲突的部分以修订为准**。

## 状态

- [x] 用户裁决（首版）：盘前/盘中共享交易上限提升到 5 个，且清仓后释放槽位继续攒样本。
- [x] 用户裁决（**修订**）：5 帽只针对盘前/盘后；盘中不受在仓帽限制，单轮梯度帽回退 3。
- [x] 代码落地：`max_new_positions_per_cycle=3→5`，新增动态 `max_open_positions=5`。
- [x] 修订落地：盘中 `max_open_positions=999`（等效解除）+ 单轮帽回退 3；EXT 执行器 `EXT_ENTRY_MAX_OPEN=5`。
- [x] 定向验证：`tests/playbook/test_entry_revalidation.py` 等 3 个执行链套件 200 passed。
- [x] 上线验证：2026-09-21 15:27:50 CST 重启生效；启动冒烟 7/7 通过。

## 结论

本批只改 Playbook 入场容量，不改退出参数、判据阈值或实盘开关。新增帽的口径是**未平仓** Playbook 交易：每轮 `auto_execute` 重新查询 `trade_records.exit_time IS NULL`，已平仓的标的不再占用槽位；执行间隔现为 180 秒，因此清仓后的槽位释放最多延迟一轮。

日累计帽 `max_daily_positions=16`、日名义帽、资金预算、单名上限和入场质量门保持原样。5 个在仓标的并不是必然开满；资金或质量门先拦截时仍会少开。

## 归因影响

- 2026-09-21 15:26 CST 起的成交样本属于新容量 cohort，与旧 `max_new_positions_per_cycle=3` 窗口不可直接混合。
- 本批是用户明确要求的直接容量调整，未另做 shadow；后续报表应按 `max_open_positions` 生效时点切分。
- 若存在异常未平仓台账行，在仓帽会保守拦截新仓，符合预算/持仓门 fail-close 语义。

## 验证

```bash
./venv/bin/python -m py_compile app/playbook/pipeline.py app/playbook/config.py tests/playbook/test_entry_revalidation.py
./venv/bin/python -m pytest tests/playbook/test_entry_revalidation.py tests/playbook/test_daily_guardrail_db_contract.py tests/playbook/test_offline_full_chain.py -q
```

实测：200 passed。新增契约测试覆盖“4 个在仓 + 1 个新开 → 第二笔被拦；平掉 1 个旧仓 → 下一轮第二笔可开”。

## 回滚

```yaml
playbook:
  execution_parameters:
    max_new_positions_per_cycle: 3
    max_open_positions: 999
```

改回后重启 `ftseek-us` 生效；`max_open_positions=999` 等效解除动态在仓帽，保留原日累计/资金门。

## 上线记录

- 15:27:50 CST 重启，`MainPID=2252619`，`ActiveState=active`，`NRestarts=0`。
- `PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py --wait 90`：ConfigValidator / 漂移 / 热重载 / SSOT / 模拟盘 / 退出快循环 / 零 Traceback 全部通过。
- 重启前未平仓查询结果为空；重启段 ERROR / CRITICAL / Traceback / Exception 为 0。

## 修订裁决（2026-09-21 16:40 CST，用户复核后）

**用户口径**：`max_open_positions=5` 只针对盘前和盘后交易阶段，盘中不受此限制影响。

首版实现装错了位置——`run_playbook_auto_execute` 是盘中（TRADING 分支）专属路径；
盘前/盘后实际走独立 cron `scripts/extended_hours_entry_executor.py`（每 5 分钟）。
复查还发现两阶段原本就不对称：EXT 执行器自有 `ext_open_position_limit` 闸
（只数 EXT 自有仓位，缺省帽 3），而首版给盘中加了全局在仓帽 5、盘后方向反而无共享约束。

**三项子裁决**（AskUserQuestion）：

| 项 | 裁决 | 落点 |
|---|---|---|
| 盘前/盘后 5 帽计数口径 | 只数 EXT 自有仓位（沿用现闸，与盘中在仓正交） | `.env.us: EXT_ENTRY_MAX_OPEN=5`，零代码改动 |
| EXT 单日新开配额 | 同步 3→5；**追加裁决**：盘前/盘后共享总额、不再单窗限制（盘前配额=总配额=5 → 单窗闸永不先触发，每日剩多少用多少） | `.env.us: EXT_ENTRY_MAX_PER_DAY=5 / EXT_ENTRY_PREMARKET_MAX_PER_DAY=5` |
| 盘中路径 | 在仓帽解除、单轮梯度帽回退原值 | `unified_config.yaml: max_open_positions=999 / max_new_positions_per_cycle=3`；`config.py`、`pipeline.py` 三层默认同步 3/999 防漂移 |

**生效方式**：EXT cron 每次启动重读 `.env.us` → 下一轮（≤5 分钟）即生效，无需重启；
盘中 yaml/pipeline 改动需重启 `ftseek-us`（距 9:35 ET 首个 auto_execute 窗口充裕）。
`MAX_OPEN_NOTIONAL_USD` 未显式设置 → 自动随帽扩至 5×$5000=$25,000。

**机制保留**：盘中在仓帽代码路径与契约测试（显式注入 5/5）原样保留，999 等效解除，
回滚只需改回目标值+重启；`skipped_open_position_cap` 审计动作继续可用。

**归因修订**：新容量 cohort 分界仍为 15:26 CST；16:4x CST 起盘中恢复单轮 3 帽、
EXT 窗进入 5 帽/5 配额 regime，报表切分时以本节点为准。

**验证**：py_compile 通过；配置加载实测 盘中=3/999、EXT=5/5/5/$25k（总配额/盘前配额/在仓帽）；
三套件复跑 200 passed。
