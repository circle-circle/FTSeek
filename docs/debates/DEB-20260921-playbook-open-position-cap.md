# DEB-20260921 Playbook 在仓标的帽与清仓重算

## 状态

- [x] 用户裁决：盘前/盘中共享交易上限提升到 5 个，且清仓后释放槽位继续攒样本。
- [x] 代码落地：`max_new_positions_per_cycle=3→5`，新增动态 `max_open_positions=5`。
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
