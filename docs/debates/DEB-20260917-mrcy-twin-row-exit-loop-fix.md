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

## 7. 附录 A · 日报「归因未填充」误报 (第 4 类瑕疵, 同日修复)

**症状**：日报 `improvement_suggestions` 每日固定产出「trade_attribution 记录存在但评分数据全空，请修复归因写入管线」。

**取证**：
- `trade_attribution` 表**停写于 2026-06-26**（223 行全部 `overall_score` 非空，9/16 当日 0 行）——平仓路径已改为写 `trade_records.metadata_json.attribution`（playbook 时代每笔必写，9/16 实测 `sources=trade_records.metadata`, `total_records=10`, `dimension_scores=[direction,duration,efficiency,entry,exit]`）。
- `daily_review.py:1195` 的判据读 `attribution['data_available']` —— **该键 `_analyze_attribution` 从无处写入** ⇒ `not None` 恒真 ⇒ 只要有归因记录就报。
- `review_loop.py::_check_attribution_empty` 读 `dims['attribution_learning']` —— **该维度在 report v2.0 的 dimensions 里根本不存在** ⇒ `{}` ⇒ 恒真（该规则已 2026-08-27 `enabled:False` 退休，故未外溢成 Issue，但判定逻辑仍是错的）。

**修法（判定改读真实载体，双向可验）**：两处都改为「`total_records > 0` 且**未提取到任何带分数据**（`overall_scores`/`dimension_scores`/旧表五分/`slippage_values` 全空）才报」。

**验证**：9/16 报告重生成 → `improvement_suggestions` 只剩「今日交易执行正常，继续保持」（断言 1 PASS）；`attribution_data` 实测有 10 记录 + 5 维评分（断言 2 PASS）；`IssueDetector.detect` 零 Issue（断言 3 PASS）；反向后（伪造 `{'total_records':5}` 无评分）→ 报告侧报、规则侧（临时启用 `enabled=True` 验证）报「归因数据未填充」，有评分时不报（断言 4/5 PASS）。

## 8. 附录 B · 触发门测试对齐 P0 宽松化 (消除既有红)

**背景**：commit `5b9948d06`（他 agent 的 P0「触发条件宽松化」）给 `_check_intraday_trigger` 加了 `_near_miss_tolerance = 0.01`（距 `chase_zone` 边界 ±1% 内也触发），但**未同步测试** → `TestCheckIntradayTrigger` 2 条红：

| 用例 | 原断言 | 原价位 | 新语义下实际 |
|---|---|---|---|
| `test_long_price_above_upper_band_no_trigger` | `is False` | 106.0（+6%） | 落进新闭区间上界 **+6%** → 触发 ⇒ 挂 |
| `test_short_price_below_lower_band_no_trigger` | `is False` | 94.0（-6%） | 落进新闭区间下界 **-6%** → 触发 ⇒ 挂 |

**修法（保留用例语义，不翻转断言）**：用例名是"…no_trigger"，其意图是"明确出带不触发" ⇒ 把价位外移到 **+7% / -7%**（真出带），并补 2 条边界用例把新契约钉死（**±6% 闭区间边缘含 → 触发**）。边界用例在用例内显式密封 `entry_open_blackout_enabled`（断言 True 的用例会被 ET 00:00-09:35 禁触窗随挂钟拦截）。

**验证**：worktree 版（HEAD + 本改动，不含他 agent 未提交的 +207 行禁触窗测试类）`pytest tests/test_playbook_pipeline.py` → **56 passed**（该类 16 passed）；工作区版（含他 WIP）同类 16 passed。

**提交隔离**：`tests/test_playbook_pipeline.py` 当时含他 agent 未提交改动（新增 `TestEntryOpenBlackoutGate` 类 + 夹具密封键）⇒ 用 worktree + `hash-object` 注入自己的 hunk（staged 校验：`TestEntryOpenBlackoutGate` 出现 0 次），**并把同一改动也写进工作区文件**，防止他后续提交时覆盖回退本改动。