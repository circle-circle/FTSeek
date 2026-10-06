# RFC-135: LIVE 1股探针的四锁破例预注册 (用户裁决 A)

- **日期**: 2026-10-06 (周一, ET 盘后)
- **裁决**: 用户裁「A」— 在呈报的三个选项 (A=预注册破例使告警在授权期内静默、到期自动复响 / B=容忍红灯不改判据 / C=关探针复原四锁) 中选 A
- **上游裁决**: 用户同批裁「1. 修复明确缺陷和异常，不造轮子，更合理可靠 / 2. …就按照3只股来实现交易链路」, 随后改裁「是否可以不重复造轮子 选择1只股 真实试运行，后续可以根据结果随时关闭开关」
- **类型**: L5 (判据/注册表口径变更) + L4 (运维文件) — 交易行为变更见 §5
- **风险**: 🟡 中 (探针真钱单可执行; 但敞口 1 股 = $33~$1,058, 且四锁告警语义从"必响"改为"精确覆盖才静默")
- **先例**: RFC-082 (2026-09-12 用户知情破例预注册, 用户 9/12 常设裁决要求「知情破例预注册攒样判据须留痕」)

> **2026-10-07 扩展留痕**: 用户裁决「验证实盘多持仓股场景，同时持股数量设为
> 3；目前美股 qty 仍为 1，先跑通整个链路」。破例仍只覆盖
> `ALLOW_REAL_TRADING=true` 这一把锁；授权范围更新为
> `AUTH-20261007-3POS-1SHARE-LONG-RFC132`，即最多 3 个 LIVE 自动持仓、
> 每单 1 股、LONG/BUY、RTH。不放大单笔数量、不放开 SHORT、不改变 SIM 常驻。

## 1. 为什么必须破例 (判据互相矛盾, 非阈值购物)

两处已落地判据对同一个开关提出**相反**要求, 探针在四锁体系下结构性不可运行:

| 判据源 | 对 `ALLOW_REAL_TRADING` 的要求 | 违反后果 |
|---|---|---|
| `app/playbook/live_arm_gate.py:99` | 必须 `true` | `LIVE_PREREQ_INCOMPLETE` ⇒ 探针不放行 |
| `scripts/okr_2026_09_scoring.LOCK_EXPECT` | 必须 `"false"` | `all_closed=False` ⇒ `KR4.alarm=True` |
| `ops/hermes/ftseek_weekly_checkpoint.py::check_locks` | 必须 `"false"` | 周检 `ok=False` ⇒ 每周告警 |

⇒ 只要探针能跑, 四锁就必然报警。这不是"门坏了", 是**四锁判据在 RFC-130/131 引入双臂常驻架构时未同步** — 四锁写于 `AUTO_TRADE_ACCOUNT=sim` 单臂时代, 那时 `ALLOW_REAL_TRADING=true` 确实等价于"实盘已启用"。

**勘误留痕**: 我 2026-10-06 上一轮曾建议「探针只需 `live_manual_arm` + `probe.authorized` 两关, `ALLOW_REAL_TRADING` 可以关回 false」。该建议**错误**, 证伪源 = `live_arm_gate.py:99` 实读。按错误建议执行会直接杀死探针。

## 2. 改什么

| 项 | 旧 | 新 |
|---|---|---|
| `data/live_probe_lock_waiver.json` | 不存在 | 破例载体 (gitignored ⇒ 运维文件), 带 `expires` + `locks_waived` |
| `app/playbook/live_arm_gate.py` | 无到期概念 | 新增 `read_live_probe_waiver()` = 判定 SSOT; gate 新增 `LIVE_PROBE_WAIVER_INACTIVE` 判据 |
| `scripts/okr_2026_09_scoring.py` | `alarm = not all_closed` | `alarm = (not all_closed) and (not alarm_suppressed)`; 新增 `waiver` / `alarm_suppressed` / `loose_locks` 三字段 |
| `ops/hermes/ftseek_weekly_checkpoint.py` | `ok, locks` 两元组 | `ok, locks, waiver, suppressed, uncovered` 五元组; 三态标记 `✓ / 🔕 / ⚠️` |
| `tests/test_okr_2026_09_scoring.py` | 断言 `all_closed is True` | 断言「全闭 **或** 松动锁被完整覆盖 + 授权可追溯 + 有到期日」 |

**判定源单一**: 两个审计器都调 `live_arm_gate.read_live_probe_waiver()`, 不各写一份日期解析。这正是既有契约钉 `test_lock_literals_match_weekly_checkpoint` 所防的分叉病 (一处红一处绿)。

## 3. 关键设计约束 (三条, 每条都有契约钉)

**① 破例不改写实测真相。** `closed` / `all_closed` 永远记实测值 — 锁松着就记 `False`。授权只决定"要不要吵醒值班人"(`alarm_suppressed`), 不决定"锁是否闭合"。

> 理由: 若把 `closed["ALLOW_REAL_TRADING"]` 翻成 `True`, 那是撒谎, 且会让**下一次真松动**被同一把已消音的门吞掉。
> 钉: `test_locks_closed_records_truth_even_when_waived`。

**② 授权不是万能消音器 — 精确覆盖。** 松动的每一把锁都必须在 `locks_waived` 里, 且**实测值 == 授权值**, 少一条即不抑制。

> 钉: `test_waiver_cannot_silence_uncovered_lock` (`AUTO_TRADE_ACCOUNT=live` 未被授权 ⇒ alarm 仍响)、
> `test_waiver_value_mismatch_does_not_silence` (授权写 `"true"` 但实测是别的值 ⇒ 不算覆盖)、
> `test_execute_mode_lock_not_waivable_by_probe_doc`。

**③ 到期自动双向失效, 无需人工记得关开关。** 到期/文件缺失/坏 JSON/缺字段 ⇒ 探针拒 + 告警复响。fail-close 五态全部有钉。

> 钉: `test_gate_rejects_when_waiver_expired` (探针侧停) +
> `test_kr4_alarm_fires_when_waiver_expired` (告警侧复响) +
> `test_waiver_absent_is_inactive` / `test_waiver_corrupt_json_is_inactive` /
> `test_waiver_non_object_is_inactive` / `test_waiver_missing_required_field_is_inactive` /
> `test_waiver_unparsable_expires_is_inactive`。

**放宽后的生产断言仍会红 (反向验证实证)** — 6 情形对撞:

| 情形 | 断言 | 期望 |
|---|---|---|
| 当前生产 (破例覆盖 `ALLOW_REAL_TRADING`) | PASS | ✅ |
| 破例到期 | **FAIL** | ✅ alarm 复响 |
| `AUTO_TRADE_ACCOUNT` 松动未覆盖 | **FAIL** | ✅ |
| 破例生效但无 `authorization_id` | **FAIL** | ✅ 授权不可追溯 |
| 破例无到期日 | **FAIL** | ✅ 永久消音器被拒 |
| 四锁全闭无破例 | PASS | ✅ |

## 4. 授权范围 (逐条, 不得扩张解释)

```json
{
  "waiver_id": "WAIVER-20261006-LIVE-PROBE-1SHARE-RFC135",
  "authorization_id": "AUTH-20261005-1SHARE-LONG-RFC132",
  "locks_waived": { "ALLOW_REAL_TRADING": "true" },
  "expires": "2026-10-12T16:00:00-04:00"
}
```

**只覆盖**: `ALLOW_REAL_TRADING` 一把锁, 只在其值为 `true` 时。

**身份绑定**: waiver 的 `authorization_id` 必须与
`playbook.live_manual_arm.probe.authorization_id` 精确一致；缺授权 ID、授权 ID
不匹配、或 waiver 范围不是 `ALLOW_REAL_TRADING=true` 时，探针 gate 拒绝。

**不覆盖 / 不改变**:
- `AUTO_TRADE_ACCOUNT=sim`、`execute_mode=futu_simulate` 两把锁照常要求 (松动即响)
- **不构成毕业打标** — `graduated` 仍为 `False`, KR4 `verdict` 仍记 `NOT_MET`, fail-close 硬拦保持
- **不覆盖 SIM 臂** — SIM 侧 1 股小额指令仍被 admission 改写 SKIP (契约钉 `test_apply_still_rewrites_sim_direction`)
- **不覆盖手动实盘平仓通道** — `executor.py:410` 的 `ALLOW_REAL_TRADING` 消费点不在本破例授权内 (见 §6 遗留)
- **不放宽探针自身的其他门** — 方向 (只 LONG/BUY)、时段 (只 RTH)、持仓上限 (`max_open_positions=1`)、`max_order_qty=1` 全部照旧, 破例只解锁那一把锁 (钉: `test_waiver_does_not_loosen_other_probe_gates`)
- **admission 豁免同样收窄** — 只有 `BUY + position_size=1 +
  target_env in {real,live} + mode=engineering_probe` 的指令可豁免
  `NOTIONAL_TOO_SMALL`；标记污染或非 1 股 BUY 均不得放大敞口。

**窗口**: 5 个交易日 (D1=10-06 周二 → D5=10-12 周一), `expires` 取 D5 收盘 ET 16:00。

## 5. 交易行为变更

LIVE 1股工程探针从「结构性不可执行」变为「**可执行**」。链路:

```
DA BUY 指令 → pipeline C-lite 镜像 (position_size=1, dual_arm 标记)
            → evaluate_live_arm_gate (含新 WAIVER_INACTIVE 判据)
            → admission (NOTIONAL_TOO_SMALL 被豁免, 见 commit 4ed7f6c30)
            → save_instruction 写 LIVE lane (ftseek_us_live.db, 见 commit efecbba00)
            → 券商真钱单 1 股 + STOP 台账落库
```

敞口: 单笔 1 股 = $33~$1,058 (今日价格分布), SL 走券商 STOP 单。
SIM 臂零变更。

**随时关闭开关** (用户明确要求) — 任一条即刻生效, 无需重启:
1. 删 `data/live_probe_lock_waiver.json` ⇒ gate 拒 (`waiver_file_absent`) + 告警复响
2. 删/改 `data/live_arm_halt.json` 落 HALT ⇒ `LIVE_NEW_POSITIONS_HALTED`
3. `config: playbook.live_manual_arm.enabled=false` ⇒ 镜像分支短路
4. `.env.us: ALLOW_REAL_TRADING=false` ⇒ `LIVE_PREREQ_INCOMPLETE` (需重启)
5. 什么都不做 ⇒ 10-12 ET 16:00 自动到期

①②③ 读文件/配置即时生效 (实证 `read_live_arm_halt()` 与 `read_live_probe_waiver()` 均每次调用读盘, 无缓存)。

## 6. 遗留 (不在本 RFC 范围)

- **`executor.py:410` 手动实盘平仓兜底门** 仍被 `ALLOW_REAL_TRADING=true` 连带打开 (`manual_order_service._is_live()` 恒 True)。本破例授权**不覆盖**它 — 但物理上该门现在通着。需独立裁决是否收窄。
- **34 条存量红灯** 未处理 (10 failed / 24 errors): 9 条 RFC-134 他人 WIP、2 条已提交、4 条存量 (`ext_entry`×3、`u1` PROMPT_VERSION 棘轮)、18 ERROR `test_offline_full_chain` + 1 FAILED `test_owner_strategy_persistence` (`no such column: tr.futu_order_id` 迁移在旧库形态抛错)。
- **30+ 文件他人 WIP 未提交**, 含已部署进生产的 `playbook_service.py` STOP receipt 修复 ⇒ 回滚面失控, 需催提交。

## 7. 回滚

```bash
rm data/live_probe_lock_waiver.json     # 立即: 探针停用 + 四锁告警复响 (无需重启)
git revert <本提交>                       # 完整: 判据回到 alarm = not all_closed
```

## 8. 验证

- 新契约钉 23 条: `tests/playbook/test_live_probe_lock_waiver.py` 全绿
- admission 豁免契约钉 8 条: `tests/playbook/test_live_probe_admission_exemption.py` 全绿
- 既有四锁同源钉 `test_lock_literals_match_weekly_checkpoint` 保持绿 (字面值未被破坏)
- 生产实测: `measure_locks()` → `all_closed=False` (诚实) / `alarm_suppressed=True` / `loose_locks=[]` / `days_left=6`; `kr4_hard_gate().alarm=False`, `verdict=NOT_MET`
- 周检实跑: `check_locks()` → `ok=False` / `suppressed=True` / `uncovered=[]`; 渲染 `🔕 四锁: sim/true/futu_simulate`
- 产物重生成: `okr_history.jsonl` 保持 180 行 (`okr_status.py` L539-543 同日幂等去重生效, 非纯追加)

### 2026-10-06 复查补充

- 补齐 waiver `authorization_id` 非空校验、与探针配置的一致性校验、
  `ALLOW_REAL_TRADING=true` 精确范围校验；
- admission 豁免收紧为 1 股 BUY 工程探针形态，防止 `dual_arm` 标记污染；
- 定向回归 `waiver / admission / routing / quote gate / exit compensation /
  LIVE P0` 共 **94 passed**；
- 生产重启后 `startup_smoke_check.py --live-probe` **7/7 通过**，全局仍
  `broker=sim db=sim AUTO_TRADE_ACCOUNT=sim`，LIVE 仅显式探针授权；
- SIM 指令 `3707/3710` 的历史 `dual_arm` metadata 污染已备份并清理，
  交易记录与持仓分臂保持干净。
- 追加 STOP 终检：若 broker STOP 先于规则引擎触发，退出监控用券商成交
  证据补记 `SL_BROKER_STOP`，并按 `entry_broker_order_id` 清理 open position；
  LIVE 手动持仓检查异常时 fail-close。终检定向回归 **146 passed**。
