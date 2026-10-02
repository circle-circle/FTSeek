# INCIDENT-002: BOOKED intent 永久占用 close_key — ACN 退出链双通道静默锁死

> **严重程度**: P0
> **状态**: 已解决 (仓位已平 + 工具/文案/探针三维修复 + RFC-128 自动释放语义统一)
> **开始时间**: 2026-10-01 20:27 ET (人工按旧语义释放 intent 起)
> **结束时间**: 2026-10-02 09:21 ET (新 intent FILLED_ALL, 仓位收口)
> **响应人**: Hermes (全链路复查) + 用户拍板 P0-1 / 「一并改造」

---

## 事件摘要

10/1 ACN SHORT 23股 的 ETH 限价腿零成交被撤后, 人工按 `release_stuck_exit_intent.py`
旧语义把 intent 改成 BOOKED。BOOKED 只解除了 symbol busy 与 `list_unsettled()` 枚举,
但 `exit_intents` 的 `UNIQUE(account_id, target_env, close_key)` **无 state 谓词** ⇒
BOOKED 行永久占用 `close_key=entry:8219132` ⇒ 10/2 盘前起 ETH 重放链 (每分钟
request 返回旧 BOOKED 行, reserve_leg=None) 与 RTH SL 兜底 (session 不同 ⇒
exit_session_conflict BLOCKED) **双双发不出单**。该形态零未决意图 ⇒ fast loop /
blocked_exits / orphan watch 全静默, 唯一信号是每分钟一条文案误导的「锁盈待确认」
推送 (实际无任何单在途)。持仓 ~16 小时无退出保护。

---

## 时间线

| 时间 (ET) | 事件 | 操作 |
|------|------|------|
| 10/1 09:25 | ETH flatten 限价腿 8219159 60s 零成交 CANCELLED_ALL | intent 7b149410… 进 exit_eth_partial_fill_held |
| 10/1 全天 | symbol 进 blocked_exits, SL 评估被跳过, 2029 条 ERROR 告警 | (INCIDENT-002 前史, 见 RFC-128 §1.2) |
| 10/1 20:27 | 人工跑旧版 release 脚本, state→BOOKED | 告警停止 — 但换成静默零保护 |
| 10/2 04:00 起 | 盘前影子每分钟正确判出 SL_TRAIL_LOCK, executor 每分钟返回陈旧 BOOKED 结果 | 32 条「锁盈待确认」误导推送, 无一张单发出 |
| 10/2 09:04 | 用户令全链路复查 | DB 副本四项检验 (A/B/C/D) 实证双链死锁 |
| 10/2 09:15 | P0-1: 备份整库 → 归档 intent+legs → 删除 | close_key 空出, 回读验证 |
| 10/2 09:20 | 下一轮 cron 重建 intent 767ee433…, 新腿 8221461 发出 | LIMIT 23@212.35 ETH |
| 10/2 09:21 | FILLED_ALL 23@211.5, trade 1598 收口, open_positions 行删除 | 券商侧 ACN 持仓归零, 三源一致 ✅ |

---

## 影响范围

### 服务影响
- [x] 交易主路径中断 (ACN 退出链 ETH+RTH 双通道, ~13 小时静默)
- [x] 数据不一致 (账本 BOOKED vs 券商持仓仍在)
- 推送文案失真: 「锁盈待确认」×32 条, 实际无任何在途单

### 财务影响
- 受影响交易数: 1 (US.ACN SHORT 23股, 名义 ~$4,867)
- 实际结果: 解锁后盘前窗内成交 211.5, 毛利 +$23.67 (entry 212.529), 锁盈基本兑现
- 潜在损失 (若未处置): RTH 开盘后若上冲穿 SL=223.16, exit_session_conflict ⇒ 零保护, 亏损无上界

---

## 根因分析

### 直接原因
`UNIQUE(account_id, target_env, close_key)` 无 state 谓词 + `request()` 对同
close_key 的旧行**原样返回** (BOOKED 也不例外) + `reserve_leg` 对 BOOKED 直接 None。

### 根本原因
旧版释放工具的语义假设错误: docstring 声称「改成 BOOKED 是唯一能同时做到解除
symbol busy + 退出 pending 枚举 ⇒ SL 兜底重新接管的态」。前半句真, 后半句假 —
**SL 兜底恢复评估 ≠ 恢复执行能力**: RTH 侧 `close_position` 默认 `session='RTH'`
撞上旧行 `session='ETH'` ⇒ `exit_session_conflict`。检验 C 证明改 session 也不够
(仍 BOOKED ⇒ reserve_leg=None)。正确释放 = 归档 + 删除, 让 close_key 真正空出。

### Contributing factors
1. 该形态**零未决意图** ⇒ 所有既有哨兵 (fast loop / blocked_exits / orphan watch)
   的触发源都是未决态, 全部失明;
2. executor 推送把陈旧 BOOKED 结果包装成「锁盈待确认」, 文案掩盖「无可用退出路径」;
3. 推送无跨进程节流, 每分钟重放同文。

---

## 止血措施

```bash
# P0-1 (已执行): 备份 → 归档 → 删除 → 回读验证
cp data/playbook_exits.db /opt/ftseek/backups/playbook_exits_pre_unlock_20261002_211519.db
# python: 归档 intent 7b149410… + legs → data/exit_intents_archive/<id>_<ts>_<unique>.json, 后 DELETE
```

**验证**: 09:21 ET 下一轮 cron 重建 intent 767ee433… → 腿 8221461 FILLED_ALL
23@211.5 → trade_records 1598 收口 (exit_reason=SL_TRAIL_LOCK) → open_positions
行删除 → 券商 position_list_query ACN qty=0。三源一致。

---

## 根因修复

### 已落地 (本批, [hotfix] INCIDENT-002 处置批)
1. **P0-2 工具修正**: `scripts/release_stuck_exit_intent.py` 释放语义从「改 BOOKED」
   改为「归档并删除」(证据保全到 `data/exit_intents_archive/`, `--restore` 可回插);
   docstring 订正被证伪的论断; 新增 FILLED_UNBOOKED 拒删守卫 (删行会掐断
   acknowledge_close 落账)。契约测试 `tests/test_release_stuck_exit_intent.py`
   钉住「释放后同 close_key + session='RTH'/'ETH' 的 request() 能重建并拿腿」+
   旧 BOOKED 死锁形态复现 (防回退)。
2. **P1-3 推送修正**: `scripts/extended_hours_exit_executor.py` ①exit_state=BOOKED
   且未成交 ⇒ 文案改「账本已终结但仓位仍在 → 无可用退出路径」+ 可粘贴释放命令;
   ②跨进程节流 (审计文件尾部连续同态行数, 首行必推 + 每 30 行提醒, awaiting 瞬态
   重复不推)。契约测试 5 例。
3. **P2-5 探针**: `scripts/exit_closekey_health.py` — 活仓 close_key 被 BOOKED
   intent 占用 (年龄≥180s) ⇒ P0 告警, 每 intent 一次; 挂在
   `extended_hours_exit_cron.run_once` 会话门控**之前** (死锁不分时段); 台账已并入
   crontab-ext-exit-cron 行。契约测试 12 例 (含放行安全 + 载体接线源级钉)。

### 第二批 (同日晚, 用户裁「一并改造」): RFC-128 自动释放语义统一
4. **释放语义下沉为账本层 SSOT**: `ExitLedger.archive_and_delete(intent_id, *,
   reason, now, evidence=None, kind='manual_release')` + `restore_archived(intent_id)` —
   归档文件路径从 DB 路径派生 (`data/exit_intents_archive/`, 不新增配置键),
   落盘 tmp+`Path.replace` 原子替换, 删行在 `BEGIN IMMEDIATE` 事务内, 返回归档路径。
   守卫 fail-closed: 非终态腿 ⇒ `exit_release_unsafe_leg`; `FILLED_UNBOOKED` ⇒
   `exit_release_unbooked` (删行会掐断 `acknowledge_close` 落账); 读档与删除之间
   意图变化 ⇒ 回滚 + 撤销归档; 删后回读不成立 ⇒ `exit_release_verification_failed`。
5. **RFC-128 `release_unsettled()` 改为委托同一 SSOT**: 释放 = 归档 + 删行
   (原 `state→BOOKED` 写法废弃)。证据从 `receipt_json` 移到归档文件的
   `broker_qty_evidence` / `external_close_release` 字段; 时间戳从 now 派生
   (原 `datetime.now()` 独立取时可与 SQL `updated_at` 发散)。
   ⚠️ 该分支的**部分平仓拒删前置仍保留** — 有腿未 BOOKED 时不释放,
   因为删行会掐断 `acknowledge_close` 的落账路径 (与人工工具同一守卫)。
6. **人工工具改为委托 SSOT**: `scripts/release_stuck_exit_intent.py` 不再内联
   删除/回插 SQL (两处各写一份释放语义正是本事故的教训); CLI 面不变
   (`--intent-id` 必填 / `--reason` / `--apply` 真写库, 默认 dry-run / `--restore` 回滚 / `--db`)。
7. **RFC-128 文档标注语义已订正**: 头部加醒目段落, 正文保留存设计史但注明
   「禁止按其实施」— 避免下一个操作者照已废弃的 §3.1 伪码重演同一病灶。

### 未落地 (需另裁)
- **P1-4 RFC-127 阶段1** (ETH HELD → RTH 会话升级补腿): 预注册判据攒样中
  (影子现有 1 行, 门限 ≥5 交易日 ∧ ≥3 独立意图)。它是「ETH 腿被撤、仓位仍在」
  的自动正解 — 落地后本类事件不再需要人工释放。**本批未动**: 拿 1 个样本凑门槛
  属阈值购物, 且这次是人工 5 分钟解掉的, 不构成破例理由。
- ~~**RFC-128 `release_unsettled()` 同源缺陷**~~ → **已修 (本批第 4-7 项)**。

---

## 复盘 (Post-Mortem)

### 做得好的
- DB 副本四项检验 (A/B/C/D) 先于任何生产写, 把「改 session 够不够」这类分支全部实测排除;
- 解锁后一个 cron 周期内完成「重建→发单→成交→簿记→对账」全链, 三源回读一致才算完成;
- 归档先于删除 (原子写 tmp+replace), `--restore` 回滚路径当批落地。

### 需要改进的
- **释放工具的 docstring 论断未经代码路径验证就写死** — 「BOOKED 是唯一」是推断
  不是实证; 处置工具的每个后果声明都应有契约测试钉住 (本批已补);
- **哨兵全依赖「未决态」触发源** — 终态行占用身份键的形态在监控面上不可见,
  探针 (P2-5) 补上; 同型自查: 还有哪些 UNIQUE 键无 state 谓词?
- **推送文案与真实语义脱节** — 「待确认」字样让操作者以为系统在正常工作。

### 行动项

| 行动项 | 负责人 | 截止日期 | 状态 |
|--------|--------|----------|------|
| P0-1 解锁 ACN (归档+删除+回读) | Hermes | 2026-10-02 | ✅ 完成 |
| P0-2 release 工具改归档语义 + 契约测试 | Hermes | 2026-10-02 | ✅ 完成 |
| P1-3 executor 推送文案 + 节流 | Hermes | 2026-10-02 | ✅ 完成 |
| P2-5 closekey 死锁探针 + 载体接线 + 台账 | Hermes | 2026-10-02 | ✅ 完成 |
| P1-4 RFC-127 阶段1 (ETH→RTH 升级补腿) | 影子攒样 → 用户裁决 | 判据达标日 | 攒样中 |
| RFC-128 release_unsettled 同型风险评估 | 用户裁决 | — | 待裁 |

---

## 相关文档

- 日志: `logs/extended_hours_exit_cron.log` (10/2 09:20-09:21 ET 重建+成交轮)
- 审计: `data/extended_hours_exit_executions.jsonl` (32 条陈旧重放 + 13:21Z 新腿)
- 归档: `data/exit_intents_archive/7b149410fb274548b209659b822b9ba7_1790947259.json`
- 备份: `/opt/ftseek/backups/playbook_exits_pre_unlock_20261002_211519.db`
- 相关: RFC-127 (影子), RFC-128 (external_close 释放), RFC-129 (缺口可见化),
  INCIDENT-001 (同载体前史)
- Commit: `[hotfix]` INCIDENT-002 处置批 (工具语义 + 推送文案 + 探针)
