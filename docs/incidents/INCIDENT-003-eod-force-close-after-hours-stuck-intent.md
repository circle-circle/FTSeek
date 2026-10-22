# INCIDENT-003: 盘后 EOD 兜底下 RTH 市价腿必被撤 → 意图卡死 → SL 冻结 (TSLA)

> **严重程度**: P0 (单仓位零保护 4h42m, 已安全释放)
> **状态**: 已解决 — 仓位无损释放；根因已代码修复 (commit `1b208e4a6`)，首窗活体验证待 10/22 16:00 ET
> **开始时间**: 2026-10-21 16:45:52 ET (intent 477a2289 建)
> **结束时间**: 2026-10-21 21:27:19 ET (人工释放归档)
> **响应人**: Hermes (全链路取证 + 释放 + 根因修复)

---

## 事件摘要

10/21 16:45:23 ET 盘后 EXT 链建仓 TSLA SHORT 12 @390.221 (入场单 8276448 FILLED_ALL)。
30 秒后 EOD 兜底 `_force_close_orphaned_sim_positions` 扫到该仓 (它每日在 AFTER_HOURS
每 ~2min 运行)，以 `reason=EOD_FORCE_CLOSE` 下 **RTH session + MARKET** 平仓腿 8276449。
盘后窗口下 RTH 市价单券商直接撤单 (CANCELLED_ALL, dealt=0)，`close_position` 返回
`exit_order_pending` → 120s 宽限后判真卡单 → P0 告警。

**真正的伤害不是告警，而是意图卡在 PENDING**：`_resume_playbook_exits` 把该标的放进
`blocked_exits`，退出链对整行 `continue` ⇒ SL/TP/时间/因子规则**全部跳过**。该仓
`sl_broker_order_id=None` (SIM 无券商止损)，因此 16:48–21:27 期间处于零保护静默期。

---

## 时间线 (ET)

| 时间 | 事件 |
|------|------|
| 16:45:23 | EXT 建仓 TSLA SHORT 12 @390.221，入场 8276448 FILLED_ALL |
| 16:45:52 | EOD 兜底建 intent 477a2289 (session=RTH, limit=0 → MARKET 腿 8276449) |
| 16:45:53 起 | 券商 CANCELLED_ALL dealt=0；`exit_order_pending` 进入 120s 宽限 |
| 16:48:24 | 宽限超时 (122s) → `[playbook平仓失败] US.TSLA EXIT_PENDING` ERROR + 钉钉/飞书 HIGH |
| 21:27:19 | 券商核实 (仓位 −12 / 无活单) → `release_stuck_exit_intent.py --apply` 归档删除，close_key 空出 |
| 21:27 后 | 未决意图 0，TSLA 仓位完整 (−12, SL 409.73)，下个 TRADING 相位规则链接管 |

---

## 根因

1. **相位与腿型不匹配**：`_force_close_orphaned_sim_positions` 只由 AFTER_HOURS 驱动，
   却始终下 `session=RTH` 的市价腿。RTH 单在盘后窗口必被撤 ⇒ 必然 0 成交 ⇒ 必然卡意图。
   盘中规则链无此问题：`exit_monitor_flow` 有 `phase != TRADING → return` 早退，本函数是唯一缺口。
2. **"收盘孤儿"定义缺失**：EXT 链在收盘后建仓 (9/17 用户裁 EXT 仓可隔夜, cohort=B)，
   30 秒后被"EOD 强平"扫到，语义自相矛盾 (建仓即被扫)。
3. **卡单的后果被低估**：卡住的意图独占 close_key 并使标的进 blocked_exits，
   比"平不掉"更糟 — 它让后续所有规则行跳过。

数据佐证：`trade_records` 中 `EOD_FORCE_CLOSE` 的券商成交 **7/7 落在 16:0x**，
无任何窗口外成交。`data/exit_intents_archive` 已有 4 例人工释放 (ACN 10-01 / PEP 10-08 /
GS 10-13 / TSLA 10-21)。

---

## 修复 (commit `1b208e4a6`)

`app/playbook/pipeline_parts/overnight_cleanup.py` 新增 `eod_close_verdict()`，在交腿前判定：

| 判据 | 动作 |
|------|------|
| 建仓时刻 ≥ 当日收盘 (ET，半日市按 `get_us_close_minutes_et` 取 13:00) | `defer_fresh_ext` — 非收盘孤儿，交回 EXT/次日规则链 |
| 相位为 TRADING，或收盘后 10min 内 | `submit` (原行为) |
| 其余 (盘后 16:10–20:00 / 盘前 / 深夜) | `defer_window` — 不交必被撤的腿 |

延后只跳过"交腿"，不建意图 ⇒ 不进 `blocked_exits` ⇒ 下一 TRADING 相位规则链照常评估
SL/TP/MAX_HOLD (卡死意图反而让规则链整行跳过，故延后严格优于硬下)。守卫置于隔夜豁免与
RFC-079 深水闸影子打点**之后**，豁免台账/影子采样零损失。

契约测试：`tests/playbook/test_eod_close_window_contract.py` (16 例，含真函数 + 假 DB/假券商活体探针)；
回归面 `tests/playbook tests/execution`：1330 passed。

---

## 验证状态

- 已验：判据真值表 (含 naive CST 混存、半日市 13:00 边界)、真函数探针 (新建 EXT 仓/窗口外 → 0 腿 0 告警；
  16:05 窗口内 → 仍交腿)、进程启动时间 (11:40:18 CST) 晚于源文件 mtime (11:40:07) ⇒ 代码已加载。
- **未验 (TOO_EARLY)**：首窗活体行需 10/22 16:00–16:10 ET 的 AFTER_HOURS 才有数据；
  该窗口内日志应出现 `EOD 兜底延后` (若存在盘后新建仓/窗口外仓) 或沿用原 `EOD 强制平仓`。

## 回滚

`git revert 1b208e4a6` + `systemctl restart ftseek-us`。无配置/账本变更，回滚不涉及数据修复。