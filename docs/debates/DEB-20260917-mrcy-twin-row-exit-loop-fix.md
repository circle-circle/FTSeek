# DEB-20260917 · MRCY/SNDK 孪生行退出死循环根因修复 + 日报回撤单位勘误

> 状态：已执行 · 2026-09-17 | 执行人：Hermes | 用户裁决：**「按建议执行」**（承接 9/16 复盘末三条建议）
> 关联：`ftseek-positionstore-broker-mirror` §「补录孪生重复行 → 身份冲突死循环」

## 1. 故障复现（9/16 美股盘中，全部实测）

| 时刻 (ET) | 事件 |
|---|---|
| 09:45:42 / 09:45:57 | MRCY(68@86.66) / SNDK(3@1529.02) 成交，建仓簿记 `BOOKED CAS rejected` → `BOOKKEEPING_FAILED` |
| 同秒 | PositionSync P3 恢复路径见 broker 有仓、DB 无 open 记录 → **补录孪生行** `1511`/`1513`（无 `decision_id`、无 `close_futu_order_id`） |
| 毫秒后 | playbook 簿记重试成功写入 `1512`/`1514`（带完整身份）→ 同一 broker 单（`8182459`/`8182463`）两条 open 行 |
| 09:52:04 / 09:59:01 | 正常平仓成功（`close_futu_order_id` 8182560/8182733，净 -$5.88 / +$9.91），broker 归零 |
| 09:52–10:24 | **退出快环每 20s 重试平仓孪生行** → `exit_lifecycle` 见同 `close_key` 已 BOOKED、owner 快照不等 → `exit_owner_conflict` → 按终态失败升级 **192 条 ERROR + 飞书 HIGH「仓位可能卡住/无止损保护」**（全为误报：仓已平） |
| 10:26:59 | 服务重启清空内存退出台账，`daily_active_pool` 识别已平并移除；孪生行事后手工标 `P3_DUP_CLEANUP`（pnl=0） |

**规模**：日志实测 `exit_owner_conflict` 192 条（CST 21 时段 30 条 + 22 时段 162 条），告警窗口 32 分钟。

## 2. 根因链（三层，缺一不可）

1. **写入非原子**：建仓簿记 CAS 失败 → P3 补录与 playbook 重试是**两条并发写入链**，各自都成功 → 孪生行。
2. **豁免判定被骗**：ghost 清理的「该仓仍由 playbook 管理」豁免查询用 `exit_time IS NULL`，被孪生悬空行骗过 → open_positions 残留不清理。
3. **退出行源无去重**：退出快环行源只按 `exit_time IS NULL` 选行，生行被当未平仓反复平仓 → 永不收敛的 owner 冲突。

## 3. 处置（全部落地）

### ① 清账（9/16 已完成，止血）
孪生行 `1511`/`1513` 引用同 broker 单已平孪生行的 exit 数据补记（`realized_pnl=0` 仅清账，`exit_reason='P3_DUP_CLEANUP'` 独立标记）；`open_positions` 残留行删除。清账后冲突立即停止。

### ② 豁免判定加固（commit `5b9948d06`，随 P0-P2 批次入库）
`load_playbook_open_symbols()` 补两条件：`decision_id <> ''` **且** 不存在同 `futu_order_id` 的已平孪生行 → 空身份悬空行不再无限延长 ghost 豁免。

### ③ 补录防重 + 退出行源去重（本次）
- **补录防重**（`app/core/position_sync.py`）：补录写入前查 同 symbol+direction+价格(±0.01) 且入场时间差<60s 的 open 记录，命中则跳过并记 INFO。
- **退出行源去重**（`app/playbook/pipeline.py`）：新增 SSOT 谓词 `_PB_CLOSED_TWIN_NOT_EXISTS`（同一 broker 单已有已平行 ⇒ 该行身份已结算，不得入未平集），**ghost 豁免与退出快环行源共用**，消除两处漂移。同步替换 `load_playbook_open_symbols` 内联同款谓词。

### ④ 日报回撤单位勘误（本次，修尺子）
`trade_records.max_drawdown` 为**百分点**口径（`100*(mfe+mae)/(100+mfe)`，例 5.5342 ≡ 5.53%）。旧代码 `> 0.05` + `*100` 把 5.53% 打成 **553.4%**，阈值实为 0.05% ⇒ 每笔都误报「超过 5% 阈值」。
- `app/review/trade_analyzer.py`：`> 5.0`，打印 `{max_dd:.1f}%`
- `app/review/error_pattern_detector.py`：`stop_loss_violation` 条件 `0.05 → 5.0`

## 4. 验证（Gate 5，实测）

**故障复现式验证**（临时 DB 副本把 1511/1513 还原为未平）：

```
[退出快环行源] 旧: [(1518,CRM),(1515,NOK),(1513,SNDK),(1511,MRCY),(1485,UBER)]
[退出快环行源] 新: [(1518,CRM),(1515,NOK),(1485,UBER)]          ← 孪生行排除, 真未平保留
[ghost豁免集] 旧(②前): [UBER, MRCY, SNDK, NOK, CRM]            ← 豁免被骗
[ghost豁免集] 新:      [UBER, NOK, CRM]                        ← 骗不动
8/8 断言 PASS (含"只减不增"超集断言)
```

**补录防重验证**：`julianday()` 能正确解析存量 `+08:00` 偏移格式（`2461300.0734`）→ 防重非静默失效；竞态复现命中跳过 / 正常态放行 / 价差 0.02 与 60s 边界均不误拦 → 4/4 PASS。

**回归**：`test_r9_r13_contracts + test_patient_exit + test_ledger_ts_scale_precision` 116 passed/1 skipped；`test_playbook_pipeline + test_daily_review + test_playbook_service + test_review_tables + test_review_validation_accounting` 106 passed / **2 failed**。

⚠️ **2 个失败为既有红，非本次引入**：`TestCheckIntradayTrigger::test_{long_above,short_below}_band_no_trigger` 断言的是**加宽前**的严格触发语义，而 commit `5b9948d06`（他 agent 的 P0「触发条件宽松化 ±1% `_near_miss_tolerance`」）改变了行为、测试未同步。本次 diff 对 `_check_intraday_trigger` 改动行数 = 0。

**安全性实测**：全库 `futu_order_id` 重复组仅 MRCY/SNDK 两对（均为本故障产物）；代码无「部分平仓新建行」路径 ⇒ 谓词不会误伤合法未平仓。

## 5. 未处置（记录在案，不擅自扩范围）

- `app/notifier/feishu.py:627` `max_drawdown > 0.03` / `{:.1%}`：同为百分点口径可疑，但该参数的主调用方（`order_executor._notify_exit` / `position_monitor`）**均未传** `max_drawdown`（默认 0）⇒ 分支实际不可达，按「死代码不动」处理。
- `app/backtest/weekly_optimizer.py:333` `row.max_drawdown_during_trade >= abs(sl)`：回测优化器内疑似同类单位混用，非生产路径，留待回测专项。
- `exit_owner_conflict` 仍按终态失败即时告警（设计如此：宁可多告不可漏告）。本次是把**触发源**（孪生行）消灭，不改告警语义。

## 6. 影响面与回滚

- 受影响进程：`ftseek-us`（`app/playbook/*` 不走热重载，必须 `systemctl restart ftseek-us`）。
- 回滚：`git revert` 本 commit；DB 侧无需回滚（已平的孪生行清账为审计留痕）。
- 未连带提交：工作区中他 agent 的 WIP（`exit_lifecycle.py` R04 时段语义、`daily_review.py` 净口径去重等）保持未提交。