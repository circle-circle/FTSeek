# RFC-151 一致性校验 Memory 分支排除集改 SSOT (2026-10-20)

**状态**: 已实施（仅可观测性日志口径；未改交易策略/阈值/仓位/退出链）
**类型**: L1 功能修复（改动 ~10 行，净减 5 行）
**触发**: 用户令修复 10-19 复核发现的 P2 误报 —— `⚠️ [US Sync Consistency] Memory 缺少 Broker 持仓` 全天贯穿刷屏。

## 1. 现象（10/19 ET 盘后实测）

主服务 `logs/ftseek_us.log` 每 ~5min 一条（PositionSync 循环）：

```
[US Sync] Done | Futu: 4 | 主线DB: 0 | playbook自管: 4 | 一致性: ✓
WARNING: ⚠️ [US Sync Consistency] 检测到 1 个不一致:
  Memory 缺少 Broker 持仓: {'US.ASML', 'US.BA', 'US.TSM'}
```

同一时刻两条日志自相矛盾：Done 行按 SSOT 判据认定「playbook 自管 4 / 一致性 ✓」，
一致性段却把其中 3 个 EXT 仓报成 Memory 缺失。

当日 DB 实况（`open_positions` 活跃行）：

| symbol | owner_strategy | sync_status | trade_records.source_type | decision_id |
|---|---|---|---|---|
| US.ADSK | playbook_sim | synced | PLAYBOOK_SIM | DA-20261016202948-… |
| US.ASML | **extended_hours_entry** | pending | PLAYBOOK_SIM | EXT-2026-10-19-…-premarket |
| US.TSM | **extended_hours_entry** | pending | PLAYBOOK_SIM | EXT-2026-10-19-…-afterhours |
| US.BA | **extended_hours_entry** | pending | PLAYBOOK_SIM | EXT-2026-10-19-…-afterhours |

## 2. 根因（口径漂移，非新引入）

- 写入面：`_sync_to_position_store_locked` 在 L1701 用
  `playbook_open_check(symbol, _pb_open_syms)` 跳过 playbook 自管仓 ⇒ 它们**故意不写入**
  `position_store`（内存）。判据 = `is_playbook_open_position` /
  `load_playbook_open_symbols`，查 `trade_records.source_type IN
  ('PLAYBOOK_SIM','PLAYBOOK_LIVE')` 且未平。
- 校验面：`_verify_and_sync_capital` 的 Memory 分支用另一套判据构造排除集 ——
  `open_positions.owner_strategy = 'playbook_sim'`（2026-07-14 加入）。
- EXT（盘前/盘后）入场仓的 `owner_strategy` 写的是 `extended_hours_entry`，
  `trade_records` 仍记 `PLAYBOOK_SIM` ⇒ 排除集漏这一级 ⇒ 每轮必报。
- 影响面：`owner_strategy='playbook_sim'` 字面量全仓仅此一处（grep 确认），
  其余消费方早已统一到 `is_playbook_open_position`。**DB-vs-Broker 与 qty 偏差两条校验
  未受影响，退出链未受影响**（`monitor_playbook_exits` 按 `trade_records.source_type` 选仓，
  ASML/TSM/BA 的 SL 均已设）。

## 3. 改法（做减法：删掉第二套判据，接回 SSOT）

`app/core/position_sync.py::USPositionSync._verify_and_sync_capital`

- 删：内联 `open_positions.owner_strategy='playbook_sim'` 查询（16 行）。
- 增：`load_playbook_open_symbols()` 一轮一查（与原内联查询同成本），
  Memory 比较改为 `playbook_open_check(s, _pb_open_syms_mem)` 过滤。
- 失败语义：`load_*` 返回 `None`（trade_records 查询失败）时 `playbook_open_check`
  回退逐 symbol `is_playbook_open_position`，保留其分层 fail-closed ——
  不新增 fail-open 面。
- 附带一行（同一函数、同一失效模式）：整段校验的 `except` 由 `logger.debug`
  升 `logger.warning(…, exc_info=True)`。理由：本段的 debug 吞异常会让
  「误报消失」与「整段校验已死」在日志上完全无法区分 —— 这正是本次误报能存活
  一个交易日、以及常年真缺口可能被掩盖的机制。

**为何不用「补 OR owner_strategy='extended_hours_entry'」**：那是给第二套判据再打一层补丁，
下一个新入场通道（研究臂/新 EXT 变体）会再漏一次；SSOT 化后新增通道只要写
`trade_records.source_type=PLAYBOOK_SIM` 就自动被排除。

## 4. 验证

- 定向回归 `tests/test_sync_consistency_memory_exclusion.py`（新增 5 例）：
  ①EXT 仓不报（不得刷屏）；②非 playbook 仓仍必报（不得顺手静默真缺口）；
  ③预载失败回退逐 symbol；④契约：该函数不得再出现 `owner_strategy = 'playbook_sim'`；
  ⑤整段校验异常必须 WARNING 可见（不得 debug 吞）。
  → **5 passed**。
- 回归面：`tests/coordinator/` + `test_m2_b1_execution_state.py` +
  `test_rfc080_system_order_remark.py` + `test_open_row_pnl_placeholder.py` +
  `test_pending_sync_staleness.py` + `test_position_sync_backfill_clock.py` +
  `test_last_sync_at_writer_scale.py` → **336 passed**。
- 活体验证（盘后，一致性段与时段无关，每轮必跑）：重启后 `US Sync Consistency`
  WARNING 归零，Done 行仍 `playbook自管: 4 | 一致性: ✓`。

## 5. 回滚

`git revert` 本提交（或恢复 `_playbook_symbols` 内联查询块）+ 重启。
零交易行为影响：该检查只发日志，不参与入场/退出/风控决策。