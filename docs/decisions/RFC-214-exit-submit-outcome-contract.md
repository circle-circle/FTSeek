---
title: RFC-214 — 出场提交结果契约：让被拒的出场腿可达终态（解锁 RFC-157 自愈环）
status: deployed_v1_legacy_repair_complete
created: 2026-11-19
driver: US.ELV 2026-11-18 intent 78076759a08e4f6dacd8f4f91fddbd65（已于 2026-11-19 经 V1 + 审计化历史实例处置后由 RFC-157 释放归档）
related: RFC-157（RTH 零成交自动释放）、RFC-146（2026-10-17 审计批）、INCIDENT-002、INCIDENT-003、RFC-128、RFC-127
deploy_state: **V1 已部署**（R18/R19，2026-11-19 13:28→14:28 CST）；**R20 核查补强（`_error_text` 等）已受控部署**（2026-11-19 17:15:09 CST，MainPID 3488322，冒烟 7/7，H-12 双向 0，drift 归负）；ELV 旧腿已备份审计后标为 FAILED/dealt=0，RFC-157 已释放归档；V2 blocked_exits 上界仍未实施。
---

# RFC-214 — 出场提交结果契约

## 0. 结论

**出场路径没有"提交结果契约"。** 入场侧有（`submit_state` 三态 + `has_fill` 守卫），出场侧在 `exit_lifecycle.send()` 里把券商唯一一次告知"我明确拒了这单"的证据（`place_order` 的 `ret`）当作 `_ret` 丢弃，于是「明确拒单」与「结果未知」塌缩成同一种处理——"保持认领等证据"——而那种处理**只对「结果未知」正确**。

后果不是"平不掉"，而是**一个丢弃的返回码同时废掉三道独立防线**，因为它们全部以"腿能到达 `TERMINAL`"为前提：

| 防线 | 载体 | 为何够不着 |
|---|---|---|
| 自愈环（RFC-157，2026-10-22 已落地，默认开启，19 例契约测试） | `scripts/rth_zero_fill_autorelease.py` | C3 要求所有腿 `TERMINAL` ⇒ 生产实测每分钟 skip，具名原因 `"leg_not_terminal"` |
| 人工释放工具 | `scripts/release_stuck_exit_intent.py` | `_unsafe_legs` 以"非终态 / `dealt_qty` 未知"拒绝 ⇒ `REFUSE_UNSAFE_LEG`，退出码 2，**无 override 旗标** |
| 腿自身 deadline | `exit_lifecycle.py:813-815` | `if leg['kind'] == 'MARKET': return result` ⇒ **不做任何状态转移**，时间解不开 |

⇒ intent 永久 `HELD` ⇒ `blocked_exits` 对该符号**整体冻结** SL/TP/时间退出/因子退出/EOD ⇒ 仓位零保护且**无上界**。

**V1（源头，本 RFC 主体）**：给出场路径建立与入场侧逐字对齐的提交结果三态契约。`DEFINITIVE_REJECT` 走一条新增的窄 CAS 转移进 `TERMINAL`（`dealt_qty=0`），`UNKNOWN` 保持今日保守行为不变。**不新建任何释放机制**——修复的唯一作用是让已上线、已测、可 env 停用的 RFC-157 自愈环重新可达。

**V2（纵深防御，独立批，本 RFC 只预注册）**：`blocked_exits` 加最大生存期与到期安全动作。这是 2026-10-17 审计（RFC-146）故意推迟的项，现在有了真实生产案例可供预注册。**不与 V1 同批上线**（一次一变量）。

---

## 1. 为什么会发生：源头链条（全部带实证读数，非推断）

现场：2026-11-18 美股 RTH。`US.ELV` 9 股 LONG，隔夜跳空止损（`reason=OVERNIGHT_GAP_STOP`，`reference_price=419.765`，`stop_loss=420.28`）在开盘后触发。

| # | 环节 | 实证读数 |
|---|---|---|
| 1 | 腿在**合法相位**提交 | intent `created_at` = ET 09:48:44（RTH 内，开盘 +18min）；`kind=MARKET`，`session=RTH`，`quantity=9`，`deadline` = ET 09:49:14 |
| 2 | 券商明确拒单，`ret` 被丢弃 | `app/execution/exit_lifecycle.py:739` `_ret, data = ctx.place_order(...)`；全仓 20 处 `place_order` 赋值点中**唯一**以 `_ret` 丢弃返回码的一处（其余 19 处均 `ret, data = ...`） |
| 3 | 腿冻结在 `PREPARED` | `state='PREPARED'`、`order_id=NULL`、`dealt_qty=NULL`、`broker_status=''`、`last_error=''`、**`version=0`**（`updated_at == created_at`，86 分钟从未被触碰） |
| 4 | 证据查询**只能证明订单存在** | `query()`（646-685）按 `order_id` 或 `remark` 匹配，无候选即 `raise ValueError('exit_order_evidence_pending')`；被拒的单永远不会产生候选 ⇒ 该异常**每轮必抛** |
| 5 | intent 空转 | `exit_intents.version = 480`（resume 空转 480 轮）、`last_error='exit_order_evidence_pending'`、`state='HELD'`、`futu_order_id=''`、`order_filled=false`、`broker_qty_before=9.0` |
| 6 | MARKET 腿无时间出口 | `exit_lifecycle.py:813-815`：deadline 已过 86min，MARKET 分支直接 `return result`，无转移（LIMIT 腿才有 `mark_cancel` → 终态路径） |
| 7 | **自愈环看到它但够不着** | `logs/extended_hours_exit_cron.log` 每分钟一条：`"rth_zero_fill": {"ok": true, "enabled": true, "dry": false, "intents": 1, "released": [], "skipped": [{"intent_id": "78076759…", "symbol": "US.ELV", "reason": "leg_not_terminal"}]}` |
| 8 | 人工工具同样拒绝 | `release_stuck_exit_intent.py` dry-run 退出码 2（`REFUSE_UNSAFE_LEG`）；`--intent-id/--reason/--apply/--restore/--db` 之外**无任何 override 旗标** |
| 9 | 零保护冻结生效 | `pipeline.py:3338` 注释即事实：「blocked_exits 是符号级冻结：一个未结算 intent 会让该标的 SL/TP/时间/因子规则整体冻结」；`pipeline.py:3376` 推送原文「仓位无规则保护」 |

券商侧真值（同期）：ELV 仍持有 9 股，价 416.56，**低于 SL 420.28**，浮亏 −$205.74。即：止损条件持续成立，但止损评估被第 9 环整体跳过。

### 1.1 为什么 `send()` 会这么写（不是笔误，是一个**只对一半的**保守决策）

`exit_lifecycle.py:757` 的注释写得很清楚：

```python
# Even explicit rejection stays claimed until order evidence proves its outcome.
```

这条保守原则**对 `UNKNOWN` 是正确的**：响应丢失时，订单可能已在券商侧存在甚至成交，此时释放会让活跃出场单变孤儿、让已成交数量丢账（正是 RFC-157 C4「部分成交绝不可释放」与 `_unsafe_legs` 存在的原因）。

**但它对 `DEFINITIVE_REJECT` 是错的**：`ret != RET_OK` 且响应体无 `order_id` = 券商从未创建订单，**不存在可孤儿的单，也不存在可丢账的成交**。把这两种情形混为一谈，等于用"防孤儿"的锁把"根本没出生"的腿永久锁死。

而 `_ret` 正是区分二者的**唯一信息源**——它被丢弃后，代码在语义上已无从判断，只能一律按最保守分支走。这不是"少写了一个 if"，是**证据在进入判断前就被扔掉了**。

---

## 2. 入场侧已有的正确模型（证明 V1 不是发明新东西，是补齐既有契约）

`app/execution/dual_broker.py:submit_order`（240-362）——docstring 243 行即写明三态：

```python
"""提交订单；保留旧字段，并用 submit_state 区分可否安全重试。
submit_state 为 ACCEPTED / DEFINITIVE_REJECT / UNKNOWN。只要已取得 order id，
后续成交查询异常也仍返回 ACCEPTED 且保留该 id。"""
```

计算处（360）：

```python
state = "ACCEPTED" if _accepted_id else ("UNKNOWN" if _place_attempted else "DEFINITIVE_REJECT")
```

另有两处早退 `DEFINITIVE_REJECT`（265 名义上限、320 `ret != RET_OK and not _accepted_id`），以及一处 `UNKNOWN`（325 `ret` 正常但取不到 id）。

入场路径消费它（`app/playbook/playbook_service.py:2131`）：

```python
if _submit_state == "DEFINITIVE_REJECT" and not _submit_has_fill:
    ...  # 记 DEFINITIVE_REJECT, dealt_qty=0 + logger.error("下单明确拒绝")
```

注意 `_submit_has_fill` ——入场侧**专门加了一道"报拒绝却成交"的守卫**。V1 必须镜像这道守卫（见 §3.3），否则就是拿入场侧已识别的风险去换出场侧的死锁。

**出场路径没有消费这个分类器**，而是在 `send()` 里内联重写了一遍提交，并丢掉了分类所依赖的返回码。这正是本仓协议 Gate 4 所指的漂移形态：**同一逻辑多处 inline = 漂移根源**。

> 为什么不直接改调 `submit_order`：它自管 `_ctx_and_acc(target_env)` 与锁，而 `send(intent, leg, ctx, scope, lock)` 的 ctx/scope/lock 由调用方线程化传入，且出场链显式区分 `Session.RTH`/`Session.ETH`（INCIDENT-002/003 的处置面）。整体迁移会牵动 ETH 出场相位，超出"消除死锁类"的必要范围，违背一次一变量。**V1 复用其语义与常量，不复用其调用栈。**

---

## 3. V1 源头修：出场提交结果契约

### 3.1 分类（语义与 `dual_broker.submit_order:360` 逐字对齐）

`send()` 内 `place_order` 返回后：

| 情形 | 分类 | 处置 | 与今日行为差异 |
|---|---|---|---|
| 取得合法 `order_id`（且 `identity_matches`） | `ACCEPTED` | `attach_order`（现有逻辑） | **无变化** |
| `ret != RET_OK` 且无 `order_id`，且 §3.3 守卫放行 | `DEFINITIVE_REJECT` | 新窄转移 → `TERMINAL`，`broker_status='FAILED'`，`dealt_qty=0`，`dealt_amount=0.0`，`last_error=<券商原文截断>` | **唯一变化面** |
| `place_order` 抛异常（响应丢失） | `UNKNOWN` | 保持认领，记 `Exit submit response unknown`（现有逻辑） | **无变化** |
| `ret == RET_OK` 但取不到 `order_id` | `UNKNOWN` | 保持认领 | **无变化** |
| `ret != RET_OK` 但响应体**有** `order_id` | `ACCEPTED` | 按现有 `attach_order` 路径（对齐 `dual_broker:319` 的 `and not _accepted_id`） | **无变化** |

`FAILED` / `DISABLED` **已在** `TERMINAL_STATUSES`（`exit_lifecycle.py:17-18`）——拒单语汇早就存在于本模块，只是代码从不写它，因为它从不读 `ret`。V1 不新增状态常量、不改 `LEG_STATES`、不改 `TERMINAL_STATUSES`。

### 3.2 为什么**不能**复用 `observe()`（一条硬闸，已实证）

`observe(leg, row, now)`（243-276）是**券商证据校验器**，第 257 行：

```python
if str(row.get('order_id', '')) != old['order_id']:
    raise ValueError('exit_order_id_conflict')
```

被拒的腿 `old['order_id']` 为 `NULL` ⇒ Python `None`；而左侧恒为 `str` ⇒ **`'' != None` 永真 ⇒ 必抛**。即 `observe()` 结构上无法作用于 `order_id` 为空的腿。

这是**正确的**：`observe()` 的契约是"券商告诉我的"（注释 259：`Late cache snapshots cannot lower cumulative evidence or reopen terminal orders`）。往里喂合成行会污染它的全部单调性守卫。因此 V1 新增一条**语义独立**的窄转移，保持两种认识论分开：

- `observe()` = 券商回报的订单状态；
- `mark_submit_rejected()`（新）= 券商告知**它拒了这次提交**。

### 3.3 新窄转移的设计约束

`ExitLedger.mark_submit_rejected(leg, now, *, error)`，参照同文件 `mark_cancel`（278-283）的 CAS 写法：

```
UPDATE exit_legs SET state='TERMINAL', broker_status='FAILED', dealt_qty=0, dealt_amount=0.0,
       last_error=?, version=version+1, updated_at=?
 WHERE intent_id=? AND sequence=? AND version=?
   AND state='PREPARED' AND order_id IS NULL AND dealt_qty IS NULL
```

窄在四处，缺一即拒绝（返回 `False`，调用方保持今日行为）：

1. `state='PREPARED'` —— 只处理"从未被券商接住"的腿；`ACTIVE`/`CANCEL_REQUESTED` 意味着已存在订单，绝不适用。
2. `order_id IS NULL` —— 有任何订单标识即不适用（那属于 `ACCEPTED` 或 `UNKNOWN`）。
3. `dealt_qty IS NULL` —— 已有成交证据即不适用（对齐 RFC-157 C4「部分成交绝不可释放」）。
4. `version=?` —— 乐观锁，与并发 resume 竞态时宁可失败也不覆盖。

**`has_fill` 镜像守卫**（对齐 `playbook_service.py:2131` 的 `_submit_has_fill`）：转移前用 `send()` 已持有的同一 `ctx/scope/lock` 做一次券商侧核验——`preflight()`（687-692）已在用 `ctx.position_list_query(**scope)`，故本模块内**已有**券商持仓真值的取得路径，无需新增依赖。判据：

- 券商该 symbol+方向持仓 `>= intent.quantity` ⇒ 无成交迹象 ⇒ 放行转移；
- 持仓**小于** `intent.quantity` ⇒ 可能已（部分）成交 ⇒ **降级为 `UNKNOWN`，保持认领**（保守方向），并记 WARN；
- 查询失败（`ret != RET_OK` 或异常）⇒ **fail-closed，保持认领**。注意方向：RFC-157 C6 对"影子打点"是 fail-open、对"写操作"是 fail-closed（其 §2 明写「写操作与影子打点的 fail-open 方向相反」），本转移是写操作 ⇒ 取 fail-closed。

### 3.4 修复后 ELV 这类会怎样（RFC-157 C1–C7 逐条走查，用现场实数）

| 判据 | 要求 | ELV 实测 | V1 后 |
|---|---|---|---|
| C1 | `state ∉ {BOOKED, FILLED_UNBOOKED}` | `HELD` ✓ | ✓ |
| C2 | `session == 'RTH'` | `RTH` ✓ | ✓ |
| C3 | 腿非空且**全部** `TERMINAL` | `PREPARED` ✗（**唯一硬阻断**） | `TERMINAL` ✓ |
| C4 | `dealt_qty` 全已知且 `Σ == 0` | `None` ✗ | `0` ✓ |
| C5 | `now - created_at ≥ 1800s`（锚必须是 `created_at`） | 5194s ✓ | ✓ |
| C6 | 本地 `open_positions` 仍有该 symbol+方向活仓 | 9 股在 ✓ | ✓ |
| C7 | 当日同 symbol 自动释放 `< 3` | 0 ✓ | ✓ |

⇒ **V1 之后，ELV 这一类由已上线的自愈环在 1800s 后自动释放**（`archive_and_delete`：归档 JSON 含腿证据 + 审计 jsonl + 一条 HIGH 推送，**不静默自愈**），close_key `entry:8382695` 真正空出，退出链下一轮重建意图，SL 回补。V1 自身不释放任何东西、不撤单、不平仓、不改 `trade_records`/`open_positions`。

---

## 4. V2 纵深防御（**独立批，本 RFC 只预注册，不实现**）

V1 消除的是 `DEFINITIVE_REJECT` 这一支。`UNKNOWN` 支（响应真丢了、且券商侧永远查不到）理论上仍可卡死——虽然 §3.3 的持仓核验会消掉其中"其实成交了"的部分，但"券商侧既无单又无仓变化"的残余形态仍无时间出口。

因此 `blocked_exits` 的**无上界符号级冻结**本身需要一个界。这正是 2026-10-17 审计（RFC-146）当年**故意只上告警不上行为变更**的项：

```python
# pipeline.py:3342
_BLOCKED_EXIT_ALERT_TIERS = ((30 * 60.0, "HIGH"), (4 * 3600.0, "CRITICAL"))
```

现状：30min 推 HIGH、4h 推 CRITICAL，**但冻结本身永不解除**（`pipeline.py:3428` 注释：「不改恢复/告警行为（10/1 ACN 首例：SL评估被blocked_exits整体跳过）」）。ELV 现场实测：已越过 30min HIGH 档（age 89.9min），距 4h CRITICAL 档 150.1min——**且即便越过 CRITICAL 档，冻结仍不解除**，这正是"无上界"的含义。

预注册要求（V2 上线前必须齐备，缺一项即不启动）：

1. **阈值必须由影子定，不得拍脑袋**——RFC-146 当年推迟的理由就是没有影子数据；现在有了 ELV 这个真实生产案例作为预注册锚点，但仍需先跑影子观测"未结算 intent 的存活时长分布"，取分布尾部的安全分位，而非直接选一个数。
2. **到期动作必须定义且必须是安全动作**——候选：到期后不解除冻结、而是把该符号**降级为"只允许止损、禁止其它出场规则"**（保住最低保护），并强制一条 CRITICAL 推送 + 归档。绝不选"到期即释放意图"（那会绕过 C3/C4 的全部孤儿保护）。
3. **一次一变量**——V2 不与 V1 同批。V1 是"让既有防线可达"，V2 是"给冻结加上界"，两者的失效模式与回滚面完全不同。

---

## 5. 契约测试清单（V1 上线前必须全绿，含反向钉）

沿用 `tests/test_rth_zero_fill_autorelease.py` 的钉法（RFC-157 §4.2 用契约测试钉住"锚必须是 `created_at`"，因为任何把它改回 `updated_at` 的"优化"都会让环静默失效）：

| # | 测试 | 钉住什么 |
|---|---|---|
| T1 | `test_send_classifies_definitive_reject_and_marks_leg_terminal` | `ret != RET_OK` + 无 order_id ⇒ 腿 `TERMINAL`/`FAILED`/`dealt_qty=0` |
| T2 | `test_send_keeps_claim_on_unknown_exception` | `place_order` 抛异常 ⇒ 腿**仍** `PREPARED`，`version` 不变（保守行为未被顺手改掉） |
| T3 | `test_send_keeps_claim_when_ret_ok_but_no_order_id` | `UNKNOWN` 支不误伤 |
| T4 | `test_send_attaches_when_rejected_response_still_carries_order_id` | 对齐 `dual_broker:319` 的 `and not _accepted_id`——有 id 一律按 ACCEPTED |
| T5 | `test_mark_submit_rejected_refuses_non_prepared_leg` | 窄转移约束 1 |
| T6 | `test_mark_submit_rejected_refuses_when_order_id_present` | 约束 2 |
| T7 | `test_mark_submit_rejected_refuses_when_dealt_qty_known` | 约束 3（部分成交绝不可终态化） |
| T8 | `test_mark_submit_rejected_is_version_cas` | 约束 4（并发 resume 竞态下宁可失败） |
| T9 | `test_reject_downgrades_to_unknown_when_broker_qty_dropped` | §3.3 `has_fill` 镜像守卫：持仓变小 ⇒ 不终态化 |
| T10 | `test_reject_fail_closed_when_position_query_fails` | 查询失败 ⇒ fail-closed 保持认领 |
| T11 | `test_observe_cannot_act_on_null_order_id_leg` | **反向钉 §3.2**：断言 `observe()` 对 `order_id=NULL` 的腿必抛 `exit_order_id_conflict`——防止后人"顺手复用 observe()"把证据校验器污染掉 |
| T12 | `test_rfc157_verdict_reaches_release_after_v1` | 端到端：V1 转移后，`rth_zero_fill_autorelease.verdict()` 对该 intent 返回 `("release", …)` 而非 `("skip","leg_not_terminal")` |
| T13 | `test_terminal_statuses_already_contains_failed_and_disabled` | 钉住"V1 未新增状态常量"（防止有人以为需要扩 `TERMINAL_STATUSES`） |
| T14 | `test_exit_submit_reject_emits_push_not_silent` | 对齐 RFC-157「不静默自愈」：终态化必须有可观测输出 |

---

## 6. 部署与回滚

### 6.1 部署前必须知道的三件事

1. **G-02 会自动上线它**。`scripts/deploy_freshness_check.py`（crontab `9,19,29,39,49,59 * * * *`）在北京 09:00–12:00 安全窗内，若 `drift = max(app/**/*.py, main.py mtime) − proc_start > 120s` ⇒ **自动 `systemctl restart ftseek-us` + CRITICAL 推送**（6h 冷却）。因此在本仓改 `app/` **不是本地可逆动作，而是一次延迟 ≤10 分钟的部署**。本 RFC 在批准前不落盘 `app/`，即为此故。
2. **盘中重启有已知代价**（H-12 登记）：`trail_state.highest_price` 被重置回成本、`ExitMonitor._exit_cooldowns` 被清空。当前另有 7 个持仓会受影响。**建议部署窗口选在收盘后**（ET 16:00 之后），除非用户裁定 ELV 的零保护不可接受到必须盘中上。
3. **军规重启前核**（AGENT.md）：在途出场 SQL + `git status --porcelain -- app/ scripts/`。**ELV 这条 HELD intent 本身就是在途出场**，必须在重启前显式处置或显式记录；且不得顺带上线别的 agent 的在途 WIP（当前工作区有 `app/data/futu_lv3_provider.py` 未提交改动、`ops/hermes/*`、`obsidian_vault/*`、`scripts/verdict_outcomes_ledger.py` 等他人在途文件）。

### 6.2 回滚

V1 是**纯增量**：新增一个账本方法 + `send()` 内一段分类。回滚 = 还原 `send()` 的 `ret` 捕获与分类分支，删除 `mark_submit_rejected`。**不需要数据迁移**：被终态化的腿带 `broker_status='FAILED'` 与 `last_error=<券商原文>`，可事后按该标识审计；已释放的 intent 在 RFC-157 归档目录中，可用既有 `--restore` 回滚（`restore_archived`，463）。

### 6.3 落档

- 本 RFC（V1 设计 + V2 预注册）；
- `handoff.md` 头部条目（进度队列 SSOT）；
- RFC-157 §4「失效模式与已知局限」应**补第 5 条**（见 §7）；
- commit tag：`[RFC-214]`。注意 `commit-msg` 钩子对 `app/` 暂存 diff 的高危正则（`exit_policy|position_monitor|order_executor|…`）⇒ 标题必须带 `[RFC-214]` 或 `[hotfix]`。

---

## 7. 应登记进 RFC-157 §4 的第 5 条局限（本 RFC 的副产品）

RFC-157 §5「同族扫描」当年按**相位轴**扫（同族缺陷定义为"在当前相位下提交一个不可能成交的腿"），读数「RTH 腿创建时刻最早 570、最晚 959，窗口外 0 条」⇒ 结论「除已归档的 TSLA 那条外，退出链无第二道门」。

**该结论在其所选轴上是对的**——ELV 的腿创建于 ET 09:48:44，完全在合法相位内。但同族还有**第二个轴**：按**提交结果**分类——"腿在合法相位提交、被券商明确拒绝、然后永不解析"。这一轴当年没扫，所以漏了。

建议补入 RFC-157 §4：

> **5. C3/C4 以"腿能到达 TERMINAL"为前提，而 `send()` 曾无条件保持认领**（本 RFC V1 修复）。即：自愈环覆盖"券商撤掉的腿"，但不覆盖"券商从未接住的腿"——后者在 V1 前根本到不了 `TERMINAL`，C3 结构性不可达。同族扫描（§5）按相位轴扫，未按提交结果轴扫，故当时未发现。

这条登记的价值不在 ELV 一例，而在于它指出：**RFC-157 的判据体系是"腿终态"的下游消费者**，任何让腿无法终态化的上游缺陷都会静默地让整条自愈链失效，而日志上表现为"环活着、enabled=true、每分钟 skip"——与"环正常工作"同形。

---

## 8. 明确不做

1. **不动 `_unsafe_legs` 的保守性**。不放宽"非终态 / `dealt_qty` 未知 ⇒ 拒绝释放"。V1 是让腿**合法地**到达终态，不是让工具**无视**非终态。
2. **不改 `observe()`**。它是券商证据校验器，其单调性守卫（不得降低累计证据、不得重开终态单）必须原样保留；T11 反向钉住。
3. **不新建释放机制、不新增 cron 行**。RFC-157 已上线且已测；V1 只让它可达。
4. **不整体迁移 `send()` 到 `dual_broker.submit_order`**（理由见 §2 末）。
5. **不给 `release_stuck_exit_intent.py` 加 override 旗标**。人工强制释放绕过 C3/C4 的全部孤儿保护，那是把死锁换成丢账。
6. **V2 不与 V1 同批**，且 V2 阈值不得在影子数据到位前选定。
7. **不对触发源做因果主张**。ELV 拒单发生在 22:48 CST，与当日 22:30–23:01 券商通道降级时段相邻，也与 22:09:50 盘前人工重启部署了未提交的 `app/data/futu_lv3_provider.py`（另一 agent 在途 WIP，mtime 21:29:55）时间相邻。**仅时间相邻，无因果证据**，记为环境观察，不在本 RFC 处置。
8. **不把 MU 归账缺陷并入本批**（P3 recovery 路径 `legacy order receipt unavailable` ⇒ 无 trade_record ⇒ 合成止损 + 归属误标 `playbook_sim`）。那是另一个变量，另行预注册。

---

## 9. 实施后核查补记（R20，2026-11-19 16:5x CST）

V1 由另一 agent 于 R18（13:28–13:49 CST 四次重启）+ R19（14:28:07 CST，MainPID=3477060）实施并部署。本节是**对其实现的对抗性核查结果**，不采信自述。

### 9.1 与本 RFC 设计的一致性：合格

| 设计要求 | 实现 | 判定 |
|---|---|---|
| §3.3 四处窄约束 | `WHERE ... AND version=? AND state='PREPARED' AND order_id IS NULL AND dealt_qty IS NULL` | **逐字一致** ✓ |
| §3.3 `has_fill` 镜像守卫 | `_position_qty_or_unknown`：查询失败/持仓下降/多行歧义/side·acc_id·trd_env 冲突 ⇒ 返回 `None` ⇒ 降级 UNKNOWN | ✓ |
| §3.3 fail-closed 方向 | `if broker_qty is not None and broker_qty >= intent['quantity']` 才终态化，否则保持认领 | ✓ |
| §3.2 不改 `observe()` | 未改；且 T11（`test_observe_cannot_act_on_null_order_id_leg`）已钉住 | ✓ |
| §3.1 不新增状态常量 | 复用既有 `FAILED`；T13 已钉住 | ✓ |
| §8 明确不做 1/2/3/5 | 未放宽 `_unsafe_legs`、未新建释放机制、未加 override 旗标 | ✓ |
| §5 T1–T14 | **十四条全部实现**，另加 2 条（多行响应、持仓证据冲突） | ✓ |

结果面：MU 已对齐指令真值 `669.47/757.56`（原合成 `690.9/972.64`），`_proxy_atr_from_stop` 被**正确移到真值覆盖之后**（ATR 代理应从真 SL 推）；ELV 释放归档、`intents=0`、释放后 blocked 告警 **0 条**；两份备份 sha256 与审计记录**逐字符匹配**。

回归：直接相关六文件 **184 passed**；广域（exit·broker·position_sync·playbook·intent·lifecycle·thesis·zero_fill·blocked·push）**2908 passed / 1 skipped / 0 failed**（305s）。

### 9.2 核查中发现并已修的两项（本 RFC 的补强）

**A. `str(error or '')` 是潜在崩溃点，且会把修复本身变成死锁源（已修）**

`mark_submit_rejected` 与 `_push_submit_rejected` 原写 `str(error or '')[:N]`。`or` 要对 `error` 求 `bool()`，而 **`bool(DataFrame)` 抛 `ValueError: The truth value of a DataFrame is ambiguous`**。

- **当前不可达**：futu SDK `open_trade_context.py:617-618` 失败路径 `return RET_ERROR, msg`，`msg` 是 str；DataFrame 只在成功路径构造（`order_list=[order_item]`）。
- **但后果极重**：一旦 SDK 在 `RET_ERROR` 下回 DataFrame，异常会在**拒单处置内部**抛出 ⇒ 腿退回永久 `PREPARED` ⇒ **精确重建本 RFC 要消除的死锁**。
- **原测试无法发现**：契约文件默认 payload 是字符串 `"integrated_us_order_new_err"`，三个 DataFrame 用例全是 `ret=0` 或带 `order_id`，**没有一个走拒单分支**。
- **修法**：新增模块级 `_error_text(error, limit)`（`error is None → ''`，否则 `str(error)[:limit]`，**不求 bool**），两个调用点统一走它；补 3 条钉子（`_error_text` 契约、端到端 DataFrame 拒单终态化、推送面 DataFrame 存活）。
- **变异验证（内存内 monkeypatch，未写 `app/`，规避 G-02 盲部署）**：把 `_error_text` 退回旧写法 ⇒ `send()` 抛 `ValueError` ⇒ 腿最终 `state=PREPARED` / `version=0` ⇒ **死锁类精确复现，证明钉子有承重**。

**B. `send()` 拒单分支的两处整洁问题（已修）**：块体缩进 16 空格（8 空格 `if` 之下）归 12；else 分支 `logger.warning(..., exc_info=True)` 在无异常上下文会多打一行 `NoneType: None`（已实测）⇒ 去掉 `exc_info`。

### 9.3 核查中发现、登记但**不改**的一项

**R19 的多行改动移除了一个歧义闸**：本 RFC 实施前 `send()` 用 `if len(rows) == 1` 拒绝在歧义响应上猜测；R19 改为遍历取首个合法 `order_id`。futu SDK 成功路径恒 1 行，R19 自己承认「真实券商自然形态未遇」⇒ 这是为不会发生的形态改动，且**移除了一个守卫**。

**不改的理由**：R19 的动机成立——若保留 `len(rows)==1`，多行响应会落到新增的 `ret != RET_OK and not order_id` 分支被**误判为拒单**，那比"猜首个"更危险。且 `identity_matches` 对每个被接受的行校验 `code / trd_side / qty / acc_id / trd_env`，错行会抛 `exit_submit_identity_conflict` ⇒ 残余风险已被覆盖。登记为**已知的守卫弱化**，不属缺陷。

### 9.4 证据归因更正（已追加审计）

ELV 归档 `last_error` 中的 `integrated_us_order_new_err` **不是 ELV 出场提交的捕获证据**。该串在生产日志 14 次命中**全部**来自 `[LLMStrategyLoop][playbook_service]` 的**入场**拒单（BA 22:51:37 / SPCX 22:53:49 / APH 22:53:52 / ARM 22:53:56 CST），ELV 出场拒单 **0 条**——其原文正因 `_ret` 被丢弃而**结构上不可追回**（这正是 §1 第 2 环）。

归因**合理但属推断**：ELV 提交于 22:48:44 CST，落在同一 22:30–23:01 通道降级窗内、同一 sim 通道、同一错误族。

**终态化的实质判据仍然成立**，且有三份捕获证据支撑：①`broker_position_evidence = {qty: 9.0, LONG, cost: 439.42}` ⇒ 券商侧仍持满 9 股，与 `dealt_qty=0` 一致；②`Exit submit response unknown` 在日志中命中 **0** ⇒ `send()` 的 `except` 分支从未触发 ⇒ `place_order` 正常返回 ⇒ 属 `DEFINITIVE_REJECT` 而非 `UNKNOWN`；③腿 `version=0`、`created_at == updated_at`、891min 未被触碰 ⇒ 券商从未接住。

已在 `data/audits/rfc214_elv_legacy_20261119T003510.jsonl` **追加**一行 `rfc214_legacy_error_string_provenance`（纯追加，原行 sha256 记入 `prior_record_sha256`，原内容完好性已验）。**结论：该 `last_error` 文本应读作标注，不是券商原文。**

### 9.5 剩余暴露

1. **V2 未实施**（§4）：`blocked_exits` 仍无上界。V1 只堵 `DEFINITIVE_REJECT` 一支；`UNKNOWN` 支（响应真丢且券商侧永查不到）理论上仍可卡死。需先有"未结算 intent 存活时长分布"的影子数据才能定阈值。
2. **军规「重启前核在途出场」在 13:28 / 13:38 两次重启未满足**：当时 ELV 的 HELD intent 仍在（13:40:03 才被 RFC-157 释放），它就是在途出场。缓解：ET 00:28 休市、该 intent 已死锁非活跃执行。
3. **V1 的前向生效尚未自然触发**：部署后休市，无新拒单 ⇒ 三条新日志/推送路径命中均为 0。首个检验点是下一次 RTH 出现拒单时。

