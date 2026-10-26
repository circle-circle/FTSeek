---
title: "RFC-169: 慢成交 (fill=0 在途) 的链路闭环 + 三态分报"
status: implemented
module: playbook
related_code:
  - app/core/position_sync.py
  - app/playbook/pipeline_parts/auto_execute_flow.py
  - tests/test_rfc169_reconcile_linkage.py
related_okr: OKR-2026-11
tags: [bookkeeping, observability, reconcile, entry-confirm, no-behavior-change]
updated: 2026-10-26
---

# RFC-169: 慢成交 (fill=0 在途) 的链路闭环 + 三态分报

> **状态**: 已实施（用户 2026-10-26「请合理修复」）
> **性质**: 记账/可观测性批。**不改入场/出场判定、不改门禁阈值、不改仓位/止损止盈、不切实盘。**
> 判据: 「这笔改动会不会让某笔本该成交的单不成交，或反之？」→ 不会。
> **日期**: 2026-10-26
> **审批人**: 项目所有者

## 背景（2026-10-26 开盘窗实证，US.CAT）

时间线（ET）：

| 时刻 | 事件 |
|---|---|
| 09:37:51 | `[EntryConfirm] CAT arm 锚=$158…`（armed，120s 后复核） |
| 09:40:26 | `[EntryConfirm] ✅ US.CAT 确认通过 → 下轮执行` |
| 09:41:00.699 | `执行来源 origin=auto:playbook id=4725` → 进入 `execute_instruction` |
| 09:41:02.446 | `[DualBroker] 下单已受理: US.CAT SHORT qty=8 futu_id=8290348 fill=0.0` |
| 09:41:23 | `[US Sync] New position: US.CAT SHORT 8.0 @ 686.105`（券商侧已成） |
| 09:41:32.68 | `自动执行完成: 0 成功, 3 跳过` ← **与上一行矛盾** |
| 09:41:55 | `[US Sync·P3] 已补录未平 PLAYBOOK_SIM trade_record (id=1722)` + `mark_execution_booked` |
| 09:56:31 | 保本线平仓，净 +$11.89 |

两处缺陷：

1. **`_confirm_order_filled` 30s 内未确认 ⇒ `success=False` ⇒ 计入 `skipped_count`**，
   而原因只 append 进返回 dict，**日志零痕迹**。生产读数因此是「0 成功」，
   与「已成交、53s 后 BOOKED」直接冲突；真失败与在途也不可分。
2. **reconcile 补录的 trade_record 不带链路**：缺 `metadata.playbook_instruction_id`
   （`exit_monitor_flow.py:290` 靠它取 `inst_id`）、`decision_id`、`entry_session`。
   `close_flow` 的 outcome 结算在 `if inst_id:` 之后 ⇒ 跳过 ⇒ 该笔已平仓单永久停在
   `outcome='holding'`（近 10 日 15 条 executed 中唯一一条 holding）。
   `decision_id=''` 同时打断「指令 → 成交 → 盈亏」的归因链。

## 修复内容（零交易行为变更）

### A. `app/core/position_sync.py` — 补录带链路

- `_backfill_playbook_trade_record()` 新增可选参 `instruction_id` / `decision_id`
  （legacy 递归同参透传）；`record` 写入 `decision_id`、`entry_session`
  （`get_current_session()`，与 `playbook_service.py:2090` 同源，fail-closed 落 `""`），
  `metadata.playbook_instruction_id` 仅在拿到 id 时写（legacy 路径不伪造链路）。
- `entry_vix` 保持 `0.0`，**不拿 `instruction.vix_at_generation`（生成时刻）冒充入场值**，
  避免口径静默混尺；符合 7/11 约定的「取不到落 0」。
- `_reconcile_playbook_execution_states()` 调用处传 `instruction_id=iid`、
  `decision_id=row['decision_id']`。

### B. `app/playbook/pipeline_parts/auto_execute_flow.py` — 三态分账 + 失败留痕

- 新增 `submitted_pending_count`。`success=False` 且非 TTL 时先读
  `db.get_instruction_by_id(...).execution_state`：
  - `SUBMITTED / FILLED_UNBOOKED / UNKNOWN` ⇒ 计入「待确认」+ WARNING（含原因），
    **不计成功也不计跳过**（成交回执由 position_sync 收口）；
  - 其余 ⇒ 计入「跳过」+ WARNING（含 `execution_state` 与原因）——此前该分支无日志。
- 汇总行改为 `自动执行完成: X 成功[, Y 待确认], Z 跳过`（无待确认时字面量不变），
  `pipeline_update` 事件与返回 dict 增加 `submitted_pending`。

## 不改什么（显式排除）

- `entry_confirm_max_wait_s`（30s）**不动**：加长会拉长本就 ~187s 的阻塞轮，
  且有把慢成交"等到"的同时拖慢全部候选执行的代价；先让在途可观测。
- `open_positions.current_sl` 的镜像偏差（CAT: 699.83 legacy 通用 ATR×4 夹 2% vs
  trade_records 权威 722.95）**本批不动**：退出监控读 trade_records（已核实
  `exit_monitor_flow.py:289/424`），改镜像等于把 REAL 臂 broker 侧止损参考值放宽，
  属行为变更，需单独裁决。
- entry_confirm v2 的弃单阈值（预注册观察期，n=34<60）——不碰。

## 验证

- `tests/test_rfc169_reconcile_linkage.py`（3 条）：链路写入 / legacy 不伪造 / 三态源码契约。
- 回归：`test_position_sync_backfill_clock`、`playbook/test_m2_b1_execution_state`、
  `test_dual_arm_routing`、`test_offline_full_chain`、`test_live_arm_p0_contracts`、
  `test_exit_compensation` → 125 passed。
- 部署后活体验证：**TOO_EARLY**（下一次「受理未确认」或下一次 reconcile 补录才出行为行）；
  给出的替代证据见会话报告（进程启动时刻 > 文件 mtime + 源码/单测契约）。

## 回滚

`git revert <commit>` + `systemctl restart ftseek-us`（两文件独立可回滚，无配置项）。