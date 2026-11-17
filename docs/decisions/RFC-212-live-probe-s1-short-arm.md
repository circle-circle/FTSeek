# RFC-212: LIVE 策略探针 — S1 空头臂 (B23 人群) 预注册

- **日期**: 2026-11-15 (周日, ET)
- **状态**: **待签** — `authorization_id` 未签发。本文档**不改变任何运行时行为**：`ALLOW_REAL_TRADING=false`、`s1_arm_enabled=false`、`data/live_probe_lock_waiver.json` 仍是 10/07 的过期件。
- **类型**: L0 (真钱执行) + L5 (探针授权口径变更)
- **风险**: 🔴 高 — 真钱单可执行，单笔 $4,200、总敞口 $8,400；且账户**已持有 $54k 手动仓**，空头叠加会提高杠杆
- **上游裁决**: 用户 2026-11-15「授权真钱：单笔 $4.2k × ≤2 笔/日，总敞口 ≤$8.4k（全损情景 ≈ −$8.4k），kill switch = 累计 −$300 或前 15 笔净<0 即停」+「没有其他账号，叠加现有账户」
- **人群判据**: `docs/ADJUDICATED-CRITERIA.md` §B23 (S1 = A10 ∧ `direction='SHORT'` ∧ 入场 ET≥09:45)，前瞻窗口自 2026-11-16 起
- **先例**: RFC-131/132 (1 股工程探针) + RFC-135 (四锁破例预注册载体) + RFC-082 (知情破例预注册)

> **本 RFC 与 RFC-131/132 的关系**: 那条探针验证的是"链路能不能下单且不炸"（1 股、LONG、3 仓）。
> 本条验证的是"能不能赚钱"（$4.2k、SHORT、2 仓）。前者是工程就绪，后者是 edge 就绪 —— 两者不是同一件事，
> 因此**不是**把旧探针参数放大，而是一条新臂：人群、方向、量级、判据、停止条件全不同。

## 1. 一句话

把真钱从"1 股 LONG 工程探针"换成"$4,200 SHORT、≤2 笔/日、≤$8,400 敞口"的**可裁决最小实验**，
唯一产出是**真实滑点分布 + 真实净 bps**，不是利润。

## 2. 现状实测 (2026-11-15，逐项实读，不用默认值)

| 项 | 实测值 | 来源 |
|---|---|---|
| `ALLOW_REAL_TRADING` | `false` | `.env.us` |
| `AUTO_TRADE_ACCOUNT` | `sim` | `.env.us` |
| `TRADE_ENV` | `1` (DB 路由 → sim 研究库) | `.env.us` |
| `REAL_ACCOUNT_ID` | `281756480166019917` | `.env.us`（在位） |
| `risk.acb_fail_mode` | `closed` | `config/unified_config.yaml:2160` |
| `playbook.live_manual_arm` | `enabled=true`, `mode=engineering_probe`, `max_order_qty=1`, `max_open_positions=3`, `probe.direction=LONG`, `authorization_id=AUTH-20261007-3POS-1SHARE-LONG-RFC132` | 同上 |
| `data/live_probe_lock_waiver.json` | 存在但 **expires=2026-10-12T16:00-04:00（已过期）** | 实读 |
| `playbook.execution_parameters.s1_arm_enabled` | `false` | `PlaybookConfig().get()` 实读 = `False` |
| REAL 账户持仓 | `US.MU 15股`(≈$11.7k) + 港股 02513/00700/00100 (美股系统不可见) | OpenD 只读 `position_list_query` |
| live 库 `open_positions` | `US.MU LONG owner=manual trader_type=MANUAL` (**MU 已被 manual 标记**) | `data/ftseek_us_live.db` |
| live 库 `PLAYBOOK_LIVE` 未平仓 | `0` 笔（历史 3 笔皆已平） | 同库 |
| sim 库未平仓 | **9 笔 LONG** (BA/HON/ASML/AMZN/LMT/JD/OXY/PDD/XOM) | `data/ftseek_us.db` |

## 3. 为什么不是"改两个 env 键"

两条已落地的机制否掉了这个捷径，两条都实读确认：

1. **Path A 已立墓碑**：`AUTO_TRADE_ACCOUNT=real` 且无显式 `target_env` 的旧执行路径已改 fail-close（`DEB-20261003-sim-live-arm-routing-convergence` §Path A）。真钱唯一入口 = `execute_instruction(target_env='real')`。
2. **`evaluate_live_arm_gate` 五关全过才放行**：`arm.enabled` + 未 halt + `mode=engineering_probe` + (`ALLOW_REAL_TRADING=true` ∧ `REAL_ACCOUNT_ID` 非空 ∧ 写库名 `ftseek_us_live`) + `risk.acb_fail_mode=closed` + **RFC-135 豁免文件 active（有到期日）**。
   ⇒ 当前状态下任何真钱尝试都会以 `LIVE_PROBE_WAIVER_INACTIVE` 被拒 —— 这是设计意图，不是故障。

**勘误留痕**: 我 2026-11-15 上一轮曾答"切真钱 = 改 `AUTO_TRADE_ACCOUNT=real` + `ALLOW_REAL_TRADING=true` 两行"。该答案**错误**，证伪源 = `app/playbook/playbook_service.py:1534-1543`（"未显式传 target_env 的调用只能执行 SIM；请求 real 一律 HALT"）+ `app/playbook/live_arm_gate.py:197-212`。

## 4. 签署日才执行的改动清单 (现在一处都不动)

### 4.0 C0：真钱门是**代码级**三锁，不是配置 (2026-11-15 实读 `live_arm_gate.py`)

在 `config` 里把 `probe.direction` 改成 SHORT、`max_order_qty` 改成股数，**一点用都没有** —— 门内硬编码：

| 行 | 代码事实 | 对 S1 臂的后果 |
|---|---|---|
| `:235` | `if probe_direction != "LONG" or instruction_direction != "BUY": ⇒ LIVE_PROBE_DIRECTION_REJECTED` | **SHORT 一律被拒** |
| `:242-258` | `max_order_qty != 1 ⇒ LIVE_PROBE_LIMIT_INVALID`；且 `1 <= max_open_positions <= 3` | **$4,200/笔 不可表达**（只认 1 股） |
| `:16 / :264` | `_TRADING_PHASE = "TRADING"`，phase 不等于它即拒 | 需 `get_market_phase()` 返回 `TRADING` |

⇒ C0 必须新增 `mode: strategy_probe` 分支（`probe.allowed_directions` / `probe.single_notional_cap_usd` / `probe.max_daily_entries` / `probe.max_open_exposure_usd` 全部来自 config），
同时**逐字保留** `engineering_probe` 分支行为（契约测试钉住旧语义，防"改一个模式顺手把 1 股探针弄坏"）。

本节即 §3 的补强：门有 **6 关**（`enabled` / `halted` / `mode` / 三把前置 / `acb_fail_mode` / waiver active），其中 3 关是硬编码。

| # | 文件 | 旧 | 新 |
|---|---|---|---|
| **C0** | **`app/playbook/live_arm_gate.py`** | **三处代码级硬锁**（见 §4.0） | **新增 `mode=strategy_probe` 分支**；`engineering_probe` 分支逐字不变，契约测试钉住 |
| C1 | `config/unified_config.yaml` `playbook.live_manual_arm` | `max_order_qty: 1`<br>`max_open_positions: 3`<br>`probe.direction: LONG`<br>`authorization_id: AUTH-20261007-…-RFC132` | `max_order_qty` 改为按名义帽定股（见 C2 语义）<br>`max_open_positions: 2`<br>`probe.direction: SHORT`<br>`authorization_id: AUTH-20261116-2POS-4200USD-SHORT-RFC212` |
| C2 | 同上 | 无 | 新增 `probe.single_notional_cap_usd: 4200`、`probe.max_daily_entries: 2`、`probe.max_open_exposure_usd: 8400`（与 `execution_parameters.s1_arm_*` **同值双写**，由 `test_s1_arm_gate.py` 钉住一致性） |
| C3 | `data/live_probe_lock_waiver.json` | 10/07 过期件 | 换为 RFC-212 版（见 §4.1），`expires` = 签署日 + 30 天 |
| C4 | `.env.us` | `ALLOW_REAL_TRADING=false` | `true`（`AUTO_TRADE_ACCOUNT` 保持 `sim` —— 真钱走显式 `target_env`，不动全局臂） |
| C5 | `config/unified_config.yaml` `execution_parameters` | `s1_arm_enabled: false` | `true`（热载 ~60s）— **必须最后翻，且以 P8–P11 全绿为前提（见 §5.1）** |

> **顺序红线（2026-11-15 追加，2026-11-16 更新）**：C5 是"让臂闸开始判定"的开关，而 P8/P9/P10/P11 修的正是
> 臂闸自己的账本、失败方向、方向作用域与第三条停止条件。**C5 先翻 = 在敞口帽与 kill switch 静默失效、
> 且 sim 多头被连带停掉的状态下开真钱臂**。故签署日顺序必须是 **P6/P8/P9/P10/P11/P13 全绿 → C0–C4 → 最后 C5**。
> **2026-11-16 进度**：P6/P8/P9/P10 已提前落地并带契约测试（**均为 inert，未动 C0–C5 任何一项**，
> 真钱可达性零变化，理由与清单见 §5.4）；**P11 两段式：第一段采集 ✅ 已落地（§5.11），
> 第二段 S3 接线仍 ❌**（须攒样+裁决，在此之前 S3 不得宣称已上线）；**P13 已裁已落地**（用户 2026-11-16 授权按重设计实施，
> 「一条账本、一个锚点、一个增量」，inert，见 §5.8）；C8/C10 fail-closed 亦已提前落地（§5.7）。
> 本节 C0–C5 各条仍遵守"现在一处都不动"。

### 4.1 豁免文件内容 (签署日写入 `data/live_probe_lock_waiver.json`)

```json
{
  "waiver_id": "WAIVER-20261116-LIVE-S1-SHORT-RFC212",
  "authorization_id": "AUTH-20261116-2POS-4200USD-SHORT-RFC212",
  "granted": "<签署日>",
  "granted_by": "用户裁决：S1 空头最小实盘实验，单笔 $4,200 × ≤2 笔/日，敞口 ≤$8,400",
  "locks_waived": { "ALLOW_REAL_TRADING": "true" },
  "expires": "<签署日 + 30 天>T16:00:00-05:00",
  "scope": "仅 S1 臂真钱单：SHORT ∧ 入场 ET≥09:45 ∧ RTH ∧ ≤2 笔/日 ∧ 单笔名义 ≤$4,200 ∧ 总敞口 ≤$8,400。不覆盖 LONG、不覆盖 EXT 盘前盘后、不覆盖手动通道、不构成毕业打标、不豁免 graduated=False 硬拦。",
  "reason": "RFC-135 载体续用：live_arm_gate 要求 ALLOW_REAL_TRADING=true，而四锁/周检要求 false，判据互斥 ⇒ 需带到期日的精确覆盖。",
  "preregistered_in": "docs/decisions/RFC-212-live-probe-s1-short-arm.md",
  "precedent": "RFC-135 (载体) + RFC-082 (知情破例预注册)",
  "on_expire": "gate 拒 (LIVE_PROBE_WAIVER_EXPIRED) + 四锁/周检告警自动复响，无需人工操作",
  "revoke": "删除本文件即立即恢复四锁全闭 + 探针停用 (fail-close)"
}
```

## 5. P0 前置门 (任一不满足则**不签、不开**)

| # | 前置 | 验证方式 | 现状 |
|---|---|---|---|
| P1 | **sim 台账未平仓 = 0** | `SELECT COUNT(*) FROM trade_records WHERE exit_time IS NULL` on `ftseek_us.db` | ❌ **9 笔** ⇒ 必须等其自然平完 |
| P2 | live 库 `PLAYBOOK_LIVE` 未平仓 = 0 | `count_open_live_positions()` | ✅ 0 |
| P3 | 账户级空头敞口不被手动仓吃光 | 真钱下单前读 `power`（11/15 实测 $34,001） | ✅ 待签署日复核 |
| P4 | MU manual 标记在位 | `_ssot_manual_symbols` 含 `US.MU` | ✅ 实读在位 |
| P5 | `acb_fail_mode=closed` | `grep` 实读 | ✅ |
| P6 | B23 前瞻评估器改读 live 库 | `scripts/s1_short_after_open_cohort.py --db live` | ✅ **已提前落地 2026-11-16**（§5.4）— `--db sim\|live` 走同一 SSOT，默认 sim 数值逐字不变；live 写 `latest_live.json` **不覆盖**注册表锚定的 `latest.json`。⚠️ 签署日仍需**显式改注册表 `data_path`** 才能让看板跟真钱 cohort |
| P7 | 切换时点 = 盘中不重启窗口（ET 09:25 前或收盘后） | 运维纪律 | 待定 |
| P8 | **运行时臂闸改读 live 库**（≠ P6：P6 只覆盖评估器脚本） | `s1_arm.py` 的 `_DB` 常量改走 `app/core/paths.py:92 get_trade_records_db_for_arm(target_env)`；`_connect()`/`open_exposure_usd()`/`cohort_state()`/`cycle_state()` 全部增**无默认值**的 `target_env` 形参（漏传即 TypeError，不静默回落 sim） | ✅ **已提前落地 2026-11-16**（§5.4）— `_ledger()` 实测 `sim→ftseek_us.db` / `real,live→ftseek_us_live.db`；`auto_execute_flow.py:450` 显式传 `"sim"` |
| P9 | **臂闸读库失败改 fail-close** | `open_exposure_usd`（原 `except → return 0.0`）与 `cohort_state`（原 `except → return {n:0,net:0.0}`）不再吞异常；由 `cycle_state` 捕获后置 `st["degraded"]=True` + `logger.critical(exc_info=True)`；`check_entry` 在方向门之后第一道即拒、`cap_size` 返回 0 | ✅ **已提前落地 2026-11-16**（§5.4）— 只改失败处理，**未动任何阈值**，故不触碰 B23 冻结面 |
| P10 | **方向门不得连带停掉 sim 多头**（用户 2026-11-15 裁：采**选项 1**） | `check_entry` 对 `direction != allowed` 改为**放行且不施加任何 S1 约束**（`return True, ""`），并**提到 killed/degraded 之前**；`cap_size` 增**无默认值**的 `direction` 形参，非臂方向原样返回不缩股 | ✅ **已提前落地 2026-11-16**（§5.2/§5.4）— 原实现 `return False` + 调用方 `continue` 会把整条 sim 自动执行流变成只做空 |
| P11 | **停止条件 S3（实盘滑点中位 >15bps）落地** | `kill_reason` 现只实现 S1/S2；S3 需 `exit_slippage_bps` + **入场追价 bps** 两段合成 | ⚠️ **两段式：第一段采集 ✅ 已落地 2026-11-16**（`entry_chase_bps` 落 metadata，见 §5.11）；**第二段 S3 接线仍 ❌**（须前瞻窗攒样 + 中位数口径裁决，在此之前 S3 不得宣称已上线） |
| P13 | **日笔数帽的计数口径**（P10 的连带面，2026-11-16 复查新发现） | `auto_execute_flow.py` 轮内 `_s1_st["today_count"] = _today_opened_count` 用的是**全流向**开仓计数（含多头），而授权语义是「S1 臂 ≤2 笔/日」⇒ 签署日 2 笔 1 股多头工程探针就会吃光空头臂当日额度 | ✅ **已裁已落地 2026-11-16**（用户裁「相信你的能力和授权你，请做好调整和优化实施」，采重设计「一条账本、一个锚点、一个增量」，见 §5.8）：日笔数 = 臂账本今日 SHORT 成交数（谓词即语义），`cycle_state` 删全流向计数器形参，轮内唯一增量只计臂方向；敞口帽维持全方向（保守面不动）。inert（enabled=false），真钱可达性零变化 |

### 5.1 P8 为什么是 🔴：签署日 kill switch 会变成装饰品（2026-11-15 独立复查推演）

`s1_arm.py:24` 把 `_DB` 写死成 `data/ftseek_us.db`（sim 研究库），而真钱空单落
`data/ftseek_us_live.db`。按 §4 的签署日状态（C4 翻 `ALLOW_REAL_TRADING=true`、C5 翻
`s1_arm_enabled=true`、`AUTO_TRADE_ACCOUNT` **保持 `sim`**、真钱只走 LIVE 镜像
`auto_execute_flow.py:2005-2012`）逐步推演：

| 限额 | 签署日实际行为 | 根因 |
|---|---|---|
| 总敞口 ≤$8,400 | **永不绑定** | sim 库不再有新空单 ⇒ `open_exposure_usd()` 恒 0.0 ⇒ `exposure >= 8400` 恒假 |
| kill switch（累计 −$300 / 前 15 笔净<0） | **永不触发** | `cohort_state()` 在 sim 库上恒 `{n:0, net:0.0}` ⇒ `kill_reason` 判 `0.0 <= -300` 假、`0 >= 15` 假 ⇒ 恒 `None` |
| ≤2 笔/日 | 有效 | 走内存计数 `_today_opened_count`，不查库 |
| 单笔名义 ≤$4,200 | 有效（但见 P12） | 纯配置算术，不查库 |

即：用户上游授权的三项安全条件里，**"kill switch = 累计 −$300 或前 15 笔净<0 即停"在代码里不会发生**。
P6 只登记了评估器脚本（`s1_short_after_open_cohort.py --db live`），**运行时闸是另一个文件、另一条路径**，
P6 修完 P8 依然存在。故 C5 不得先于 P8/P9/P10/P11 全绿翻 `true`。

### 5.2 P10 为什么必须现在裁：与 RFC-209 前瞻 cohort 同日相撞

实测（只读 `data/ftseek_us.db`，A9 干净 ∧ `owner_strategy='playbook_sim'`，10-06→11-13）：
**LONG 145 笔 / SHORT 58 笔 = 71% 是多头**；近 30 日 108/44 同比例。

而 RFC-209 前瞻 cohort 起点 = **2026-11-16**，本臂 `s1_arm_start_date` 也 = **2026-11-16**。
若按现状开闸，RFC-209 的 cohort 当场被抽走七成人群，而 RFC-209 §3 明写"N/cohort/阈值窗内禁改"
⇒ 两条预注册互相击穿。

**用户 2026-11-15 裁：采选项 1**（S1 只约束空头、不拦多头）。理由留痕：B23 的**人群判据不受影响**
——`S1_WHERE` 内 `direction='SHORT'` 仍是过滤条件，cohort 照旧只收空头 ⇒ **不构成重新裁决**；
同时 RFC-209 的 sim 多头流量原样保留。
选项 2（保留"开闸即只做空"）被否：须同批重裁 RFC-209 前瞻 cohort，代价大且违反其自身冻结条款。

### 5.3 P12（签署日必须实证，本 RFC 不下断言）：单笔名义帽是否真能到达真钱单

S1 缩股结果写回 `_reprice_params["position_size"]`（`auto_execute_flow.py:1656-1657`），LIVE 镜像确实把
`custom_params=_reprice_params` 传进 `execute_instruction(target_env="real")`（`:2008-2012`）；
但镜像侧另有 `risk_gates.py:500` 把 `position_size` 写死为 1 的路径。**两者谁最终决定真钱单股数未经实证**。
签署日必须以 1 笔 dry-run 证明 $4,200 帽真的落到订单上，否则 C2 的 `single_notional_cap_usd` 只是配置装饰。

### 5.4 P6/P8/P9/P10 提前落地记录（2026-11-16，用户裁「按推荐」= 棘轮红 + B 全量）

**为什么可以提前，且不违 §4「现在一处都不动」**：§4 约束的是 **C0–C5 这批"让真钱能流"的启用改动**。
P6/P8/P9/P10 相反 —— 它们让臂闸**更关得住**（读对账本、失败即拒、不越界拦无关方向），
且在 `s1_arm_enabled=false` 下**全部 inert**（`cycle_state` 首行即 return，一次库都不查）。
本批**未动 C0–C5 任何一项**：`ALLOW_REAL_TRADING=false`、`AUTO_TRADE_ACCOUNT=sim`、
`live_manual_arm.probe.direction=LONG`、`max_order_qty=1`、waiver 仍是 10/12 过期件、
`s1_arm_enabled=false` —— 真钱可达性零变化。

**为什么必须提前（时效性）**：`s1_arm_start_date` 与 RFC-209 前瞻 cohort 起点同为 **2026-11-16**。
P10 若在 cohort 攒样后才修，届时翻 C5 会当场抽走 RFC-209 七成人群（LONG 145 / SHORT 58）
= **窗内改人群**，可能作废整个 cohort。趁 days=0 修，代价最小。

**落地清单（全部带契约测试，`tests/test_s1_arm_gate.py` 20 → 31 条）**：

| 项 | 文件 | 改动 |
|---|---|---|
| P8 | `app/playbook/s1_arm.py` | 删 `_DB` 常量 → 新增 `_ledger(target_env)` 走 `get_trade_records_db_for_arm`；`_connect`/`open_exposure_usd`/`cohort_state`/`cycle_state` 的 `target_env` **无默认值** |
| P8 | `app/playbook/pipeline_parts/auto_execute_flow.py:450` | `cycle_state(today, _today_opened_count, "sim")` — 显式传臂别（同函数 `:473`/`:596` 已写死 sim，属既有范式） |
| P9 | `app/playbook/s1_arm.py` | 两查询删掉吞异常的 `except`；`cycle_state` 捕获 → `degraded=True` + `logger.critical(exc_info=True)`；`check_entry`/`cap_size` 分别拒与返 0 |
| P10 | `app/playbook/s1_arm.py` | `check_entry` 方向门提到 killed/degraded **之前**且非臂方向 `return True, ""`；`cap_size` 增**无默认值**的 `direction`，非臂方向不缩股 |
| P10 | `app/playbook/pipeline_parts/auto_execute_flow.py:1641` | `cap_size(..., direction=inst.get("direction"))` |
| P6 | `scripts/s1_short_after_open_cohort.py` | 新增 `--db sim\|live`（非法值 rc=2）+ `_resolve_db()` 同一 SSOT；live 写 `latest_live.json`；产物新增 `arm`/`ledger` 出处键 |

**方向口径已实读确认（否则 P10 会把臂整个掏空）**：`playbook_instructions.direction ∈
{BUY 1099, SELL_SHORT 337, SKIP 3725}`（sim 库）⇒ 与 `s1_arm_allowed_direction='SELL_SHORT'`
**同词汇，匹配成立**；`trade_records.direction ∈ {LONG, SHORT}` 是另一套词汇，只用于
`S1_WHERE` 人群谓词，不会到达 `check_entry`。两者不可互换。

**变异验证（本仓纪律：新测试必须能抓住回退）**：把 P10 改回 `return False`、P9 改回
`degraded=False` 后注入，`tests/test_s1_arm_gate.py` **7 failed / 24 passed**（含
`test_non_arm_direction_passes_unconstrained` 全部参数化分支、两条 degraded 用例）；
还原后 **31 passed**。

**未动的语义（故意保守，均登记为上表 P13 或留签署日）**：
- `open_exposure_usd` 谓词**不加方向过滤** —— 与用户授权「总敞口 ≤$8,400」同口径（窗口内全部在仓）；
  不过滤只会让帽**更早绑定 = 更保守**，放宽属改授权上限，须另行裁决。
- 轮内 `today_count` 仍用全流向计数 —— 见 P13。
- `S1_WHERE` 人群谓词、四门阈值、三条停止条件的**数值**一字未动（B23 冻结面）。

### 5.5 P11（S3 滑点）为什么今天做不了：缺的不是判据，是数据

B23 原文要求 S3 = 「实盘逐笔真实滑点中位（`exit_slippage_bps` + **入场追价 bps**）>15bps」，两段合成。
2026-11-16 对 sim 库实读：

| 分量 | 可得性 | 实测 |
|---|---|---|
| `exit_slippage_bps` | ✅ 有 | `trade_records.metadata_json` 顶层键，PLAYBOOK_SIM 已平仓 **727/871 行**命中 |
| 入场追价 bps | ❌ **全库无持久化字段** | 无 `chase_bps`/`entry_chase` 类列或 metadata 键（55 列全扫 + 300 行 metadata 键集穷举）；概念只存在于 `entry_confirm_gate.py:120` RFC-170 确认侧追价影子（record-only）与 `entry_routing.py:141` 禁追线，**均未落 trade_records** |

⇒ 只用退出侧算 S3 会**系统性低估**滑点，让这条停止条件**更难触发** = 弱化 kill switch，
方向错误。**宁缺勿半**：故 `kill_reason` 保持只实现 S1/S2，并把上述实测写进其 docstring
（`test_kill_reason_implements_only_s1_s2` 钉住"不得出现 S3_SLIPPAGE 半成品"）。

**落地前置**（签署日批，属数据采集行为变更，须另行影子/落档）：先把入场追价 bps 在成交瞬间
写入 `trade_records.metadata_json`（可复用 RFC-170 影子已在算的量），攒到前瞻窗有值后再接进
`kill_reason` 作第三条判据。在此之前 S3 **不可宣称已上线**，看板与周报不得把"三条停止条件"
写成已生效。
**进度（2026-11-16）**：第一段采集已落地（用户授权，见 §5.11）；第二段 S3 接线仍待攒样+裁决。

### 5.6 C5/C6 真钱侧停止条件影子先行（2026-11-16，DEB-20261116 §10 第 1 步的 record-only 半批）

**来源**：`DEB-20261116-realmoney-s1-gate-cross-model-audit.md` 五模型审计共性 C5（kill switch
只读 sim 账本 ⇒ 真钱亏损永不进停止条件）与 C6（人群混入非本臂空单）。第二轮独立复核全部成立。

**本批落地（全部 record-only，不拦截、不 halt、不写交易库）**：

| 项 | 文件 | 改动 |
|---|---|---|
| C6 读数 | `app/playbook/s1_arm.py` | cohort 查询抽共享实现 `_cohort_state_impl`；新增 `arm_cohort_state(target_env, start_date)` = `S1_WHERE ∧ source_type=本臂来源`（sim→PLAYBOOK_SIM / real,live→PLAYBOOK_LIVE）。**`S1_WHERE` 与 `cohort_state` 语义一字未动**（B23 冻结面，历史参照 n=88/+30.75bps 契约仍绿） |
| C5 影子 | `app/playbook/s1_arm.py` | 新增 `record_real_kill_shadow(today)`：读 real 账本敞口 + 臂属 cohort + `kill_reason`，落 `data/shadow/s1_real_kill_shadow.jsonl` 一行；读库失败落 `degraded=true` 行 + warning(exc_info)，绝不抛进交易路径；**独立于 `s1_arm_enabled`**（关闸期也观测可读性） |
| 接线 | `auto_execute_flow.py:450` 后 | 每轮一次 `_s1_gate.record_real_kill_shadow(today, shadow_path=…)`，不被 enabled 包裹；**路径从 `module_file` 派生**（同 `sizing_delta` 范式）⇒ hermetic 沙箱改写 `__file__` 后影子自动落 tmp |
| 注册 | `shadow_registry.py` | 新项 `s1_real_kill_shadow`（OBSERVING, PERMANENT_MEASURE, config_key=None）；`s1_short_after_open` 仅 `notes=` 加指针 |
| 测试守卫 | `tests/playbook/test_entry_revalidation.py` | `isolated_connect` 补 `file:` URI 解析（对齐 `offline_chain_driver.connect_guarded`）：原实现把整条 URI 当相对路径判 `in_tmp` ⇒ 误拦沙箱内 tmp 库的只读连接。**隔离强度不降**：生产路径照旧改道留痕、tmp 外一律拒。修复前该文件 124 failed/125 errors（全部由影子触发，基线=摘接线 181 passed），修复后 181 passed |

**臂属谓词为什么用 source_type 而不是 dual_arm**：实读 live 库 `PLAYBOOK_LIVE` 3 行
`metadata_json` **均无 dual_arm 标记**（C13 未落，镜像 metadata 只到指令层不到 trade_records）；
live 库 SHORT 仅 2 行且全为 `MANUAL`（MU 手动仓，`S1_WHERE` 本就排除）⇒ 现阶段
`source_type='PLAYBOOK_LIVE' ∧ direction='SHORT'` 即本臂空单的完备谓词。C13 落地后可加严。

**契约测试**：`tests/test_s1_arm_gate.py` 31→38 条（臂属过滤误停反例 / target_env 无默认 /
record-only 不 halt / 独立于 enabled / 失败留痕不抛 / 接线不被门控 / 真库 n=0 实证）。
**变异验证**：①去掉 arm_only 过滤 → 1 failed；②删接线 → 1 failed；还原后 38 passed。

**明确不做（执行接线，归签署日批，须先经影子观测 + 用户裁）**：
- `kill_reason` 非空 → `halt_live_new_positions(...)` 持久化（注意 halt 是全 LIVE 通道语义，
  会连带停 LONG 工程探针 —— 作用域是否限本臂须裁，P10 同族问题）；
- 镜像前建 `cycle_state(today, count, "real")` 并对镜像指令跑 `check_entry`/`cap_size`（C1）；
- 真钱门增 `order_qty/entry_price/arm_open_notional` 入参（C4/X3）。

**真钱可达性零变化**：C0–C5 一项未动（`ALLOW_REAL_TRADING=false`、waiver 仍过期件、
`probe.direction=LONG`、`max_order_qty=1`、`s1_arm_enabled=false`）。本批代码需下次重启生效；
生效前影子不产出（首行观测为手动只读复跑产物）。

### 5.7 C8/C10 fail-open → fail-closed（2026-11-16，P9 族提前批，DEB-20261116 §10 第 5 步）

**为什么可以提前**：与 §5.4 的 P9 同族 —— 只改**失败方向**，不动任何阈值/谓词（B23 冻结面），
且当前 inert 或纯收紧：C8 只在 `s1_arm_enabled=true` 时被调用（现 false）；C10 只在 live
账本**文件缺失**时改变行为（现文件在位 ⇒ 零现行变化），方向是更严不是更松。
**§10 第 2–3 步（C0/C2 strategy_probe 分支、C1 镜像臂闸）属 C0–C5 启用批，遵守 §4 现在不动。**

| 项 | 文件 | 改动 |
|---|---|---|
| C8 | `app/playbook/s1_arm.py::cap_size` | `entry_price <= 0` 从 enabled/size 混合 guard（return size = fail-open）移出，置于**方向判断之后**：臂方向无价 ⇒ `return 0`；非臂方向/关闸原样返回不变（P10 交叉不受影响） |
| C8 落痕 | `auto_execute_flow.py` S1 块 | `_s1_qty <= 0` 的拒绝原因区分 `s1_arm_no_price`（`_entry <= 0`）与 `s1_arm_cap_zero_room`（C14 可观测性前置） |
| C10 | `app/playbook/live_arm_gate.py::count_open_live_positions` | 缺文件 `return 0` → `return None`，对齐 docstring "return None to force a fail-closed gate" ⇒ 门拒 `LIVE_OPEN_COUNT_UNAVAILABLE`。既有测试未钉住旧返 0 语义（`test_m2_c2_live_quote_gate` 注释自述 None=fail-close 契约） |

**契约测试**：`tests/test_s1_arm_gate.py` 38→44 条（无价臂方向返 0 ×2 参数 / 非臂方向不受影响 ×4 /
关闸 passthrough / 落痕可区分）；`tests/playbook/test_live_arm_p0_contracts.py` +2 条
（缺文件返 None / 门拒 LIVE_OPEN_COUNT_UNAVAILABLE）。
**变异验证**：C8 回退混合 guard + C10 回退返 0 同批注入 → **4 failed**（C8×2 + C10×2），还原后全绿。

**真钱可达性零变化**：C0–C5 一项未动；C10 收紧的是异常路径（缺文件），正常路径逐字不变。

### 5.8 P13 日笔数重设计：一条账本、一个锚点、一个增量（2026-11-16，用户裁决授权实施）

**裁决留痕**：用户 2026-11-16 听取选项分析后裁「感觉这个逻辑有点不够简洁和有效，请重新设计」
→ 复核提出重设计 → 用户裁「相信你的能力和授权你，请做好调整和优化实施」。
**裁决语义 = 日笔数只计本臂 SHORT 当日成交（账本口径）；敞口帽维持全方向（已裁保守面不动）。**

**病根（为什么不是在原骨架上选口径）**：原实现三处结构性毛病 —— ①平行记账（预算层全流向
计数器 + 臂态 today_count 两处真相、每指令重置 + 每笔成交同步四处接线）；②锚点错误（锚在
全流向开仓上，方向正确性靠事后守卫，漏一处错一处，DEB-20261116 C7）；③内存态（重启丢计数）。

**重设计落地清单**：

| 项 | 文件 | 改动 |
|---|---|---|
| 锚点 | `app/playbook/s1_arm.py` | 新增 `_today_arm_entries(target_env, today)` = `COUNT(trade_records WHERE trade_date=今日 ∧ direction='SHORT' ∧ source_type=本臂来源)`。**谓词即语义**：方向/归属天然正确，数真成交不数尝试，跨重启不丢 |
| 删参 | `s1_arm.py::cycle_state` | 签名 `(today, today_opened_count, target_env)` → `(today, target_env)`；today_count 改自账本锚定（与 exposure/cohort 同一 try 块，读失败同归 degraded fail-close） |
| 唯一增量 | `auto_execute_flow.py` 成交后 | 臂方向成交成功 ⇒ `_s1_st["today_count"] += 1`（全仓唯一一处增量，兜簿记时滞）；**删**每指令重置行与全流向同步行 |
| 不动 | 同上 | 敞口轮内累加维持**全方向**（与敞口帽谓词同口径，已裁保守面）；`_today_opened_count` 预算层计数本身不动（其他 guardrail 仍用） |

**安全性论证**：日笔数是流量闸不是风险闸；美元边界（单笔 $4,200 / 敞口 $8,400 全方向 /
kill switch −$300）一分未松。簿记时滞最坏差一笔，该笔仍被敞口帽兜住（$8,400=2×$4,200，
第三笔满额单数学上必被敞口拒）。**净效果代码更少、真相唯一**；P13 从"待裁口径题"变为
"谓词写出来即定死"。数据动机：11 月 BUY 133 / SELL_SHORT 33，旧口径下多头先吃光额度 ⇒ 实验零样本。

**契约测试**：`tests/test_s1_arm_gate.py` 44→49 条（账本锚点计 2 拒第三笔 / 多头·非臂来源·昨日
不计 / 旧三参签名 TypeError 防回退 / 接线两参+重置行已删+增量在方向守卫内 / P10 交叉多头不受影响）。
**变异验证**：①锚点谓词删方向过滤 ②增量守卫回退 `if True` 同批注入 → **2 failed**，还原后 49 passed。

**inert 论证与重启纪律**：`s1_arm_enabled=false` 下 `cycle_state` 首行 return（一次库不查），
全部改动零现行行为变化；C0–C5 一项未动。生效需重启，**避开盘中执行窗**（签署日前无时效压力）。

### 5.9 C11/C12 授权值单源 + 配置读取治理（2026-11-16，DEB-20261116 §10 第 6 步）

**C11 单源 fail-close**：`s1_arm.py` 的 `DEFAULTS` 九键同值双写**删除**，授权值唯一真源 =
`unified_config.yaml execution_parameters.s1_arm_*`。`cfg(key)` 缺键/读失败抛 `S1ConfigError`，
消费点全部 fail-close：`cycle_state` → degraded（含 enabled 本身判不出 ⇒ 视为开闸但降级）、
`check_entry` → 拒（`s1_arm_config_missing`）、`cap_size` → 0、`kill_reason` →
`CONFIG_UNREADABLE(fail-close)`。**P10 交叉保护**：配置全断时方向用 B23 冻结谓词
（`S1_WHERE direction='SHORT'`）的**结构身份**兜底 —— 那是实验身份不是第二份配置数值，
阈值仍 fail-close，非臂方向照常放行，不重演"开闸即只做空"。

**C12 修正原提案（重要）**：DEB 原建议"走现成单例 `get_playbook_config()`"——**实读证伪**：
该 R-4 单例只在 `playbook_horizon.yaml` mtime 变更时重建（`config.py:287-314`），
`PlaybookConfig._config` 构造时缓存 unified 拍平字典 ⇒ **unified 键（含 `s1_arm_enabled`）
热载不穿透单例**，盲用会丢掉"回滚=改键热载 ~60s"的授权回滚通道。改用 **10s TTL 缓存构造**：
每指令 6+ 次 `PlaybookConfig()`（每次 yaml.safe_load horizon）降为 ~1 次/10s，
热载延迟 ≤10s 仍远小于 60s 预算。契约钉住"不得改盲单例 + TTL 必须在位"。

**flow 去直接 cfg 化**：`cycle_state` 把 `allowed_direction` / `single_cap_usd` 载入臂态，
flow 的增量守卫与缩股日志改读臂态载值 ⇒ 交易路径零直接配置读取（配置故障不炸轮次）。

**连带测试治理**：历史参照契约（n=88/+30.75bps）加可选尾界 `end_date`（生产调用不传语义
不变）排除前瞻窗——今日盘中 COIN SHORT 已平仓入前瞻窗，无尾界则参照随直播漂移；
前瞻断言由"恒 0"改"全口径=臂属口径"（前瞻窗 SHORT 应全部本臂来源）。

**契约测试**：`tests/test_s1_arm_gate.py` 51→57 条（单源无回落 / YAML 九键完整 /
配置全断 fail-close 且 P10 不连带 / 运行中丢键三消费点 fail-close / TTL 单次构造 /
不得盲单例）。**变异验证**：①cfg 恢复回落 ②TTL 回退每次构造 同批注入 → **2 failed**，
还原后 57 passed。

**inert 论证**：YAML 九键在位（契约钉住）⇒ 生产路径读数与旧 DEFAULTS 逐字同值；
enabled=false 下 cycle_state 首行 return。C0–C5 一项未动，真钱可达性零变化。

### 5.10 C13/C14 镜像臂归因（2026-11-16，DEB-20261116 §10 第 7 步半批）

**病灶**：镜像 `dual_arm.mode` 硬编码 `engineering_probe`（`risk_gates.py`）⇒ 签署日翻
`strategy_probe` 后标记仍是旧值，真钱单事后无法按臂归因；且该标记**纯写入零消费**
（trade_records 层无 dual_arm，PLAYBOOK_LIVE 3 行实读证实）；LIVE 拒绝日志/推送只有
通用原因码，运维分不清"哪个臂模式的限额拒了单"。

| 项 | 文件 | 改动 |
|---|---|---|
| C13 单源 | `risk_gates.py::_mirror_instruction_to_live` | `dual_arm.mode` 改读 `playbook.live_manual_arm.mode`（读失败诚实标签 `config_unreadable`，审计元数据不阻断镜像写入）；新增 `s1_arm_enabled` 快照（读失败 None）⇒ 事后可辨"镜像时臂闸是否生效" |
| C14 归因 | `risk_gates.py::_live_mirror_gate_decision` | 决策返回附加 `arm_mode` 键（旧消费方 .get 不受影响） |
| C14 落痕 | `auto_execute_flow.py` 拒绝路径 | 拒绝日志与钉钉推送带 `arm_mode=` 上下文 |

**契约测试**：`tests/playbook/test_dual_arm_routing.py` +5 条（mode 跟随配置 /
engineering_probe 现行逐字不变 / 配置断诚实标签 / 源码无硬编码 / 决策带 arm_mode 且
SHORT 方向锁语义不变）。**变异验证**：mode 回退硬编码 → **3 failed**，还原后 15 passed。

**明确不做（归签署日批）**：①~~dual_arm 传播到 trade_records 簿记层~~ **已落地 2026-11-16
（用户排队批准，见 §5.12）**；②真钱侧 S1 拒绝/缩股专属审计与告警（依赖 C1
镜像 real 臂闸落地）。臂属过滤谓词升级（source_type → dual_arm.mode）仍另裁。

**inert 论证**：镜像只在 LIVE 门放行时发生（现 waiver 过期 ⇒ 结构上零镜像）；
现行配置 mode=engineering_probe ⇒ 即便镜像发生标记值亦逐字不变。C0–C5 一项未动。

### 5.11 P11 第一段：入场追价 bps 采集落库（2026-11-16，用户授权，record-only）

**裁决留痕**：用户 2026-11-16 裁「继续实施 P11 入场追价 bps 采集落库，同样影子先行、
一次一变量、带契约和变异验证」。本批 = §5.5 两段式的**第一段（采集）**；第二段（S3 接线）
仍须前瞻窗攒样 + 中位数口径裁决，**S3 不得宣称已上线**。

**口径（预注册，改口径=重新裁决）**：
- 字段：`trade_records.metadata_json.entry_chase_bps`（+ `entry_chase_anchor_price` /
  `entry_chase_anchor_source` 自证锚点），单位 **bps（10000 制，非百分数）**，
  **正=不利追价**（LONG 成交更高 / SHORT 成交更低），符号约定与 `exit_slippage_bps` /
  `execution.slippage_bps` 逐字一致。
- 锚 = **盘前计划入场价**：指令行 `entry_price` 会被 reprice tracking 改写
  （`_TRACKING_FIELDS` 含 entry_price，实读确认）⇒ 优先取 `reprice_from`
  （reprice 审计字段=改写前原值；`_cp` 是 execute_instruction 局部变量，簿记在 inner 层
  ⇒ 经 `_execute_via_dual_broker(_inner)` 两层**可选参**穿透，两个执行调用点传入，
  旧调用方不受影响），非 reprice 路径回落 inst 原值；锚来源落字段自证。
- 与 `execution.slippage_bps` 的区别：后者锚=下单时 arrival（微观结构滑点），
  本值锚=决策时计划（追价成本，含 reprice 一步）——两段合成 B23 S3。
- 锚/价缺失或 ≤0 ⇒ None（判不出不写假值）。

**落地清单**：

| 项 | 文件 | 改动 |
|---|---|---|
| 纯函数 | `playbook_service.py::compute_entry_chase_bps` | 量纲/符号/None 语义单点 SSOT，可直测 |
| 簿记接线 | `playbook_service.py` 入场簿记（_exec_meta 后） | 锚点优先级 + metadata 三键；outbox intent 携带同一 metadata_json ⇒ sim 双路径同覆盖 |
| 留痕 | `s1_arm.py::kill_reason` docstring | 更新为两段式状态（S3 仍未接线，契约钉住） |

**契约测试**：`tests/test_entry_chase_bps.py` 18 条（量纲/符号 ×6 参数化 + SHORT=−LONG
约定 + 非法输入 None ×8 + 锚点优先级接线 + metadata 三键 + record-only S3 未接线）。
**变异验证**：①量纲回退百分数 ②SHORT 符号翻转删除 ③reprice_from 优先级删除 同批注入
→ **7 failed**，还原后 18 passed。

**军规配套**：单位/量纲声明 + 契约断言 ✅（bps 10000 制钉在参数化用例）；基数自检——
本字段连续值无塌缩风险，**判分消费端（S3）尚未接线**，接线批须带 distinct 计数体检
（W2 棘轮届时覆盖）。补偿/修复簿记路径（compensation_repair）不经本组装点 ⇒ 该路径
暂无本字段（record-only 可接受，登记为已知局限）。

**行为变更面**：仅 metadata 新增三键（零 schema 迁移），不改任何下单/拦截/退出判定；
生效需重启（盘中不重启，建议收盘后；生效前成交无本字段）。

### 5.12 C13/C14 后半：dual_arm 标记传播到 trade_records 簿记层（2026-11-16，用户排队批准）

**病灶**：镜像 `dual_arm` 标记仅落在**指令层** metadata_json，trade_records 簿记层无
（PLAYBOOK_LIVE 历史 3 行实读证实）⇒ 真钱成交事后无法按臂归因，臂属过滤只能靠
source_type 粗口径。

**落地**：`playbook_service.extract_dual_arm_mark(inst)` 纯函数（兼容 metadata dict /
metadata_json str 两形态——行适配器 `_row_to_dict` 不解析 metadata_json，此乃关键陷阱；
metadata 键优先；缺失/损坏/非 dict ⇒ None）+ 入场簿记 metadata_json 新增 `dual_arm` 键
（sim 行无标记 ⇒ None，不得伪造归因）。sim/live 共用同一簿记组装点 ⇒ LIVE 镜像单
自动携带 mode/源指令 id/臂闸快照。

**契约测试**：`tests/test_dual_arm_booking_propagation.py` 5 条（两形态/优先级/非法 None/
接线）+ `tests/playbook/test_exit_compensation.py` +2 条**簿记级集成**（真实服务层
execute_instruction：带标记指令 → 成交行 metadata.dual_arm 逐字相等；无标记 sim → None）。
**变异验证**：①接线硬编码 None ②helper 删 metadata_json 回退 同批注入 → **3 failed**
（含簿记级集成），还原后 72 passed（受影响套件）。

**行为变更面**：仅 metadata 新增一键（零 schema 迁移）；sim 行恒 None；不改任何判定。
**仍另裁**：臂属过滤谓词从 source_type 升级为 dual_arm.mode（涉及 kill switch 人群口径，
属 B23 相邻面，须裁决）；真钱侧 S1 专属审计（依赖 C1）。生效需重启（盘中不重启）。

## 6. 预注册判据 = B23 原文，不在此重定义

四门（前瞻 `n≥40` 时同时成立才转正）与三条停止条件（S1/S2/S3）全部沿用 `docs/ADJUDICATED-CRITERIA.md` §B23，
**本 RFC 不新增、不放宽、不改写任何阈值**。历史 88 笔只作参照，不入判据。

## 7. 实验算术 (授权上限下)

| 项 | 值 | 算式 |
|---|---|---|
| 单笔名义帽 | $4,200 | 用户授权 |
| 日上限 | 2 笔 ⇒ $8,400 | 用户授权 |
| 总敞口帽 | $8,400 | 用户授权 |
| 全损情景 | −$8,400 | = 总敞口（最坏下界，非预期） |
| kill switch | 累计 −$300 或前 15 笔净<0 | 触发即停新开仓 |
| 名义周转 | ≈ $67k/月 | 16 笔/月 × $4,200 |
| 月费 | ≈ $75 | 11.19bps × $67k |
| 攒到 `n=40` | ≈ 2.5 个月 | 16 笔/月 |

**为什么 30 天到期**: 到期即自动失效，防止"忘了关"变成常态真钱臂；续期需重签新 `authorization_id`。

## 8. 三条证伪留痕 (旧数字不可复现，已按实测更正)

1. **`A10 n=832 / 毛+8.6bps / 费11.3bps / 净−2.7bps` 不可复现**：20+ 种 A10 谓词变体穷举零命中。实测 **`n=860 / +6.28 / 11.19 / −4.91bps`（−$2,037，日聚类 t=−1.03）**；`−2.7` 源自 `DEB-20261114` 中"本周 76 笔"的笔均。
2. **"一多一空更合理"被证伪**：同窗口同配额重放，`空1+多1` 净 bps 15.01（剔最大单笔 12.33 **低于 13.4 门槛**）、最差单日 −$308、回撤 −$1,547、负日 26/87、月正 3/5；对照 `空2` 为 44.10bps、−$92、−$440、9/50、5/5。两腿日净相关 `rho=−0.12`（不相关≠对冲）。根因：LONG 腿剔尾部 +23.69bps（t=3.16）但带 **20 笔**尾部，SHORT 只带 **3 笔**。
3. **`EXT +74bps(n=23,WR96%)` / `11月 +18.2bps(n=106)` 已更正**：实测 `EXT +2.73bps(n=71，费率26.44bps)`、`11月 −1.48bps(n=134)`。两者**均不进入**本实验人群。

## 9. 回滚 (≈2 分钟，四条命令级动作)

1. `ALLOW_REAL_TRADING=false`（`.env.us`）
2. `s1_arm_enabled: false`（热载 ~60s，无需重启）
3. 删除 `data/live_probe_lock_waiver.json`（fail-close 立即停探针）
4. 若已有真钱仓：走正常退出链平掉（**不用**手工市价单改账本）；必要时 `halt_live_new_positions()` 先停新仓

## 10. 明确不做 / 未解决

- **不做多腿**：见 §8.2；LONG 腿要单独一条臂，前置是"事前识别那 20 笔尾部"，目前 C11/C14/C16/C17 四轴已证伪 ⇒ 暂不可得。
- **不做 EXT / 盘前盘后**：费率 26.44bps 吞掉全部毛利。
- **不做加仓/网格/回补**。
- **未量化**: REAL 账户 $54k 手动仓与 $8.4k 空头叠加后的**组合保证金压力**（只读到单时点 `power=$34,001`，未做压力情景）；以及手动仓暴跌触发 `FLAT_ALL` 连坐 auto 仓的政策归属（RFC-135 缺口3 仍未裁）。
- **遥测可信度**: VIX 双真相(17.9 vs 28.18)、`holding_hours` 偏 13h、`reconcile` 误报 —— 三者未修前，"真实净 bps" 的度量误差可能大于被测量本身；本实验同时是这些遥测缺陷的放大器，须在首周末复盘时一并核对。