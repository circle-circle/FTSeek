# RFC-157: RTH 零成交卡死意图的自动释放 (自愈环)

> **状态**: 已落地 (2026-10-22)，默认开启 + 环境变量可停
> **类型**: L1 功能修复 (新脚本 + 既有载体接线, 无交易决策变更)
> **驱动**: INCIDENT-003 (2026-10-21 US.TSLA: 盘后 EOD 下 RTH 市价腿被撤 → 意图卡死 → SL 冻结 4h42m)
> **载体**: `scripts/extended_hours_exit_cron.py run_once` (既有每分钟/5 分钟环路, **不新增 cron 行**)
> **契约测试**: `tests/test_rth_zero_fill_autorelease.py` (19 例)

---

## 1. 为什么需要（不是"顺手加个自动化"）

`ExitLifecycle.resume()` 的补腿逻辑只在 `if not legs` 时 `reserve_leg`；而当腿已是
**TERMINAL** 且 `kind != 'LIMIT'` 时直接返回（`exit_lifecycle.py:796-798`）:

```python
if leg['state'] == 'TERMINAL':
    if leg['kind'] != 'LIMIT':
        return self.ledger.result(..., 'exit_terminal_shortfall', 'HELD')
```

⇒ 一条被券商撤掉的 **MARKET** 腿，会让该 intent **永远**停在该结果上。后果不是"平不掉"，
而是 `_resume_playbook_exits` 把该 symbol 放进 `blocked_exits`，退出链对整行 `continue`：
**SL / TP / 时间退出 / 因子退出全部跳过**（SIM 无券商止损 ⇒ 零保护静默期）。

ETH 侧早已有同类出口（`pipeline._release_eth_held_exit_for_rth_takeover` +
`eth_held_exit_shadow` 的 ghost-sample 守卫）；**RTH 侧是缺口**，唯一出口是人工跑
`release_stuck_exit_intent.py`（本例人工耗 4h42m）。本 RFC 补的就是这一只。

## 2. 预注册判据（全满足才释放；任一不满足 → skip + 具名原因）

| 编号 | 判据 | 为何 |
|------|------|------|
| C1 | `intent.state ∉ {BOOKED, FILLED_UNBOOKED}` | 这两个态归簿记所有，删行会掐断 `acknowledge_close` 落账 |
| C2 | `intent.session == 'RTH'` | ETH 归自身 escalation；BOOKED 死锁归 `exit_closekey_health` |
| C3 | `legs` 非空，且**所有** leg `state == 'TERMINAL'` | 非终态腿 = 在途单；释放会让它变孤儿 |
| C4 | 所有 `dealt_qty` 已知，且 `Σ dealt_qty == 0` | **部分成交绝不可释放**：释放会让已成交数量丢账 |
| C5 | `now - created_at ≥ 1800s` | 躲开撤单/成交回流竞态。**锚必须 `created_at`**：`updated_at` 每轮 resume 都被 `result()/observe()` 刷成"刚刚"（实测 TSLA: 建 16:45:52 → updated 21:27:17），锚它 ⇒ 年龄恒 0 ⇒ 判据恒 False（死门） |
| C6 | 本地 `open_positions` 仍有该 symbol+方向活仓 (`True`) | `False` = 仓已不在（归 `external_close` 路径）；`None` = 查询失败 ⇒ **fail-closed 不释放**（写操作与影子打点的 fail-open 方向相反） |
| C7 | 当日同 symbol 自动释放 `< 3` 次（审计台账跨进程计数） | cron 每次是新进程，内存计数无效；上限防"持续零成交"变成无限重试风暴 |

**判据之外的硬边界**：不撤单、不建新意图、不平仓、不改 `trade_records`/`open_positions`。
释放语义 = `archive_and_delete`（INCIDENT-002 订正：归档+删除让 close_key 真正空出；改
BOOKED 会永久占键，10/2 ACN 已证伪）。每次释放：归档 JSON（含腿证据）+ 审计 jsonl + 一条
HIGH 推送（**不静默自愈**）。

## 3. 历史回放（4 例 archive，按归档时点为 now）

| 标的 | session | 状态 | 存活 | 判定 |
|------|---------|------|------|------|
| US.TSLA | RTH | PENDING | 281min | **release / all_legs_terminal_zero_fill**（本可早 4h11m 自愈） |
| US.PEP | ETH | HELD | 59min | skip / session_not_rth（归 ETH escalation） |
| US.GS | ETH | HELD | 4min | skip / session_not_rth |
| US.ACN | ETH | BOOKED | 1436min | skip / state_booked_owned_by_booking（归 closekey health） |

即：本环**只**命中它该命中的那一类，不与他环争夺处置权。

## 4. 失效模式与已知局限（诚实登记）

1. **只做本地持仓守卫，不做券商侧二次核验**：开业前提依赖 `open_positions` 与券商一致
   （PositionStore = broker 镜像）。若本地陈旧（券商已平/同步滞后）⇒ 释放后下一次
   `preflight` 抛 `exit_position_ownership_conflict`，且 C7 上限封顶 3 次/日；
   这类"仓已不在"的形态本应由 `external_close` 路径释放（RFC-128），两条路并存不冲突。
2. **判据锚点死门**（已规避）：见 C5。任何后续把锚改回 `updated_at` 的"优化"都会让本环
   静默失效 —— 契约测试 `test_age_anchor_is_created_at_because_updated_at_is_bumped_by_resume` 钉住。
3. **零样本观测**：无未决意图时 `scan()` 零动作零日志（`intents=0`），与"接线死了"在日志上
   同形 ⇒ 载体返回值里带 `rth_zero_fill` 字段（`enabled/intents/released/skipped/errors`），
   以"字段存在且 enabled=true"判活，不以零动作判死。
4. **cap 达到后只告警不重试**：持续零成交（如限价永远够不到）应人工看腿价与流动性，
   不是继续重试。

## 5. 同族扫描 (为什么只动这两处 — 实测读数, 2026-10-22)

同类缺陷 = "在当前相位下提交一个不可能成交的腿"。全仓按证据扫了一遍:

| 面 | 读数 | 判读 |
|---|---|---|
| 出场腿时相 | `exit_legs` 全量 350 条 (RTH 336 / ETH 14); **RTH 腿创建时刻最早 570 (09:30:00)、最晚 959 (15:59)**, 窗口外 0 条 | 除已归档的 TSLA 那条外, 退出链无第二道门 |
| 入场相位 | 2026-09-01 起 `trade_records` 入场: PLAYBOOK_SIM RTH 272 / PRE 17 / AH 7; 非 PLAYBOOK_SIM 的盘前/盘后入场 **0** | 盘前/盘后入场全是 EXT 链的 ETH 单 (设计如此), 无 RTH 腿混入 |
| ETH 侧自动释放 | 生产实证 1 次: `10-13 09:30:13 ET US.GS` `ETH HELD出场已移交盘中策略` → 归档 `3f429eac…` | ETH 自愈环**活着**, 不是纸面 |
| `position_sync._execute_trims` (OVER_LIMIT_TRIM) | 直连 `ctx.place_order(order_type=MARKET)`, **无显式 session/无相位门**; 保留日志内 `[减仓]` / `OVER_LIMIT_TRIM` 命中 **0 次** | 结构性同类, 但 ~6 周零实例 ⇒ 记为**潜伏门**, 本批不改 (无证据的改动=引入风险), 触发再按同一谓词收口 |

## 6. 验收与回滚

- 验收（已跑）：19 例契约（真 `ExitLedger` 上造 TSLA 形态 → 归档+删除 → 同 close_key 可重建
  意图；dry-run 不写库；ETH/BOOKED/部分成交/仓位不明/超上限全部 skip；载体接线契约）+
  `test_release_stuck_exit_intent` / `test_exit_intent_external_release` 回归绿 +
  生产探针 `intents=0`（当前无未决意图）+ cron 载体实测已输出
  `"rth_zero_fill": {"ok": true, "enabled": true, "intents": 0, ...}`。
- 回滚：`EXIT_RTH_ZERO_FILL_AUTORELEASE=false`（跳过并留审计行）→ 或删
  `extended_hours_exit_cron.run_once` 里那 4 行调用 → 已释放的意图可用
  `release_stuck_exit_intent.py --restore` 从归档回插。