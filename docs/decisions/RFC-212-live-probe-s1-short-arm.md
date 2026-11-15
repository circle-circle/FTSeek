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
| C5 | `config/unified_config.yaml` `execution_parameters` | `s1_arm_enabled: false` | `true`（热载 ~60s） |

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
| P6 | B23 前瞻评估器改读 live 库 | `scripts/s1_short_after_open_cohort.py --db live` | ❌ 待改（签署日一并提交，否则前瞻样本读空） |
| P7 | 切换时点 = 盘中不重启窗口（ET 09:25 前或收盘后） | 运维纪律 | 待定 |

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