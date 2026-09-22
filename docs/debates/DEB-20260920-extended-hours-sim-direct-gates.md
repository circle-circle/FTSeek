# DEB-20260920 · 盘前/盘后 SIM 直接硬闸与 A/B 分级止盈落地

> **状态**：已直接落地 SIM；独立复核第一轮意见已落地（§10 二轮加固 + §10.6 三轮核查补强 + §10.7 四批锚点修订 + §10.8/§10.9 互核回执，待下个交易日实盘验证）
>
> 日期：2026-09-20 · 作者：Codex · 用户裁决：不用影子，直接在现有 SIM 模拟交易中按建议实施
>
> 2026-09-20 二批（§10）：独立分析（Claude）复核 §6 问题清单后，用户令「按照建议执行」——
> P0 新鲜度守卫 / P0 earnings 拒绝台账 / P1 VWAP 锚点 / P1 保留额度条件化已落地，A 级免锁判据已补预注册。
>
> 2026-09-20 三批（§10.6）：Codex 复核二批后补齐财报源状态显式化与 A 级零值锁覆盖回归。
>
> 2026-09-20 四批（§10.7）：用户质询「成熟窗锚点是否令交易时间过少」——复核确认盘后 16:20 锚无病灶依据，
> 锚点修订为盘前 07:00 / 盘后 16:00，四段入场窗预热拒绝归零。
> 同批（§10.8）：用户令核查三批更新——5 项宣称全部实证 ✅；纠正 2 处记录数字（三批实际基线 120 非 121）；
> 登记 1 项观察（live-smoke 用例盘中验证会占 OpenD 槽位）。

## 1. 目的与边界

本档不是新的影子方案，也不是业绩通过证明。它记录 2026-09-20 用户明确要求后的**直接 SIM 行为变更**，供另一个 Agent 独立审查：

1. 是否把此前讨论的“事件、时段、流动性、结构、仓位、止盈”组合成可执行的硬闸。
2. 是否存在实现错误、口径错误、过度拟合或与既有退出链冲突。
3. 是否应该在下一交易日继续运行、降参数或立即回滚。

边界：

- 仅 `FUTU_TRD_ENV=SIMULATE`；实盘仍被 `ALLOW_REAL_TRADING=false` 与执行器 REAL guard 阻断。
- 用户前提不变：延长时段不做价格止损。入场侧减风险，亏损侧仍等待 RTH/既有规则。
- 不新增资金流/期权/衍生品异常硬门；这些通道未通过显著性门槛。
- 本轮没有实现部分卖出。A 级的“+3% 卖一部分、余仓留尾”仍是**未落地的后续候选**，本轮实现为 A 级禁用全量确定性锁，交给动态回撤锁。

## 2. 决策输入

### 2.1 既有结论

- `DEB-20260919-extended-hours-profit-lock-and-trail.md`：
  - 盘前现役 rule 均值 `+0.094%`，优于多个上扬锁利臂。
  - 盘后 60 日 `next_open` 均值 `-0.366%`，优于现役 rule 的 `-0.878%`；锁利提高胜率/中位数但牺牲均值。
  - `SL_CERT_LOCK +3%` 是用户偏好下的确定性保险，但全量锁会截断大赢家。
- `data/premarket_trend_outcomes.jsonl`：
  - 492 个 mover 中方向延续率约 `94.1%`。
  - 顺势影子收益胜率约 `47.2%`，中位数约 `-0.108%`，均值约 `+0.691%`。
  - 结论：方向延续不等于可交易 edge；可执行性、成本和尾部分布才是关键。
- 180 日入口特征：
  - `vol>=20M` 是唯一两窗口同向为正的候选特征，`P(峰值>=1.5%)≈76%`、净赚占比约 `81%`、均值约 `+3.38%`。
  - 但 `vol>=20M & chg>=3%` 样本仅 5 笔，不能单独当充分证据。

### 2.2 直接落地原因

用户明确要求“不用影子，直接按照现有的在 SIM 模拟交易中按照建议实施优化更新”。因此本轮：

- 不再以 prefilter shadow 作为决策载体。
- 入口条件改为真实拦截。
- B 级仓位降低，A 级保留大额仓位但不再使用全量 +3% 锁。
- 继续保留全天合计 3 笔/日、盘前单段 2 笔、SIM-only、REAL fail-closed、费用与盘口检查。

## 3. 已落地行为

### 3.1 参数与配置

配置写入 `.env.us`，代码默认值同步：

| 参数 | 值 | 语义 |
|---|---:|---|
| `EXT_ENTRY_MAX_PER_DAY` | 3 | 盘前+盘后合计新仓上限 |
| `EXT_ENTRY_PREMARKET_MAX_PER_DAY` | 2 | 盘前单段新仓上限，为盘后保留启动额度 |
| `EXT_ENTRY_POSITION_B_USD` | 2500 | B 级单笔名义 |
| `EXT_GRADE_A_CERT_LOCK_PCT` | 0.0 | A 级禁用全量确定性锁 |
| `EXT_GRADE_B_CERT_LOCK_PCT` | 3.0 | B 级 +3% 确定性落袋 |
| `EXT_ENTRY_MIN_TURNOVER_USD` | 10000000 | 延长时段成交额硬底 |
| `EXT_ENTRY_A_TURNOVER_USD` | 20000000 | A 级成交额线 |
| `EXT_ENTRY_MIN_MARKET_CAP_USD` | 1000000000 | 市值硬底 |
| `EXT_ENTRY_MAX_SPREAD_PCT_A` | 0.5 | A 级点差上限 |
| `EXT_ENTRY_MIN_ASK_DEPTH_RATIO_A` | 2.0 | A 级买一深度倍数 |
| `EXT_ENTRY_MAX_VWAP_DISTANCE_PCT` | 0.8 | 距延长 VWAP 上限 |
| `EXT_ENTRY_MIN_VOLUME_CONFIRM_RATIO` | 0.5 | 最新 5m 量 / 前 3 根均量下限 |
| `EXT_EARNINGS_MAX_PERIOD_AGE_DAYS` | 150 | Finnhub surprise 新鲜度上限（§10.2 P0-1） |
| `EXT_VWAP_ANCHOR_MATURITY` | true | VWAP 从成熟窗锚点起算：盘前 07:00 / 盘后 16:00（§10.7 修订；false=旧全时段口径，§10.2 P1-3） |
| `EXT_PREMARKET_RESERVE_CONDITIONAL` | true | 盘前保留额度条件化（false=恒定保留，§10.2 P1-4） |

原有 `$5` 价格下限、`+2%` 涨幅、20 万股量底、B 级点差 1%、B 级买一深度 1 倍、费用 1%、价格漂移 0.5% 继续保留。

配额组合：

| 盘前已开 | 盘后可用 | 说明 |
|---:|---:|---|
| 0 | 3 | 盘前无合格机会时盘后可用满全天额度 |
| 1 | 2 | 常规分配 |
| 2 | 1 | 盘前达到段内上限后仍为盘后保留 1 笔 |

该规则只限制盘前单段，不把盘后单独压到 2；若盘前 0 笔，盘后仍可用 3 笔。同轮扫描中第 2 笔盘前成交后立即停止继续尝试，避免外层检查滞后造成第 3 笔漏出。

### 3.2 同日事件硬闸

`_earnings_calendar_event()` 要求：

1. 财报日历 `reportDate == 今日 ET`。
2. 盘前只接受 `before/pre-market` 相位；盘后只接受 `after/post-market` 相位。
3. 同一候选再由 Finnhub 最近财报确认 `surprisePercent > 0`。
4. 日历或 Finnhub 缺失时 fail-closed，不再接受“最近一次财报”这种可能过期数年的事件。

理由：此前执行样本出现过 2012 年财报口径，说明“最近一次 positive surprise”不能证明当前价格反应由今日事件驱动。

### 3.3 A/B 分级

先通过市值与成交额：

- 市值缺失或 `< $1B`：拒绝。
- 延长时段成交额 `< $10M`：拒绝。
- 成交额 `>= $20M` 或时段量 `>= 20M`：A 级。
- 其余通过者为 B 级。

仓位：

- A 级：`$5,000`。
- B 级：`$2,500`。

### 3.4 成熟时间窗

| 阶段 | A 级 | B 级 |
|---|---|---|
| 盘前 | 07:30–09:15 ET | 08:15–09:15 ET |
| 盘后 | 16:20–18:30 ET | 16:20–17:30 ET |

外层会话窗仍保留为盘前 04:00–09:29、盘后 16:00–19:59，但具体下单必须再过 A/B 成熟窗。这样 cron 可以持续观察和记录拒绝原因，但不会在极早期或尾段薄盘开仓。

### 3.5 延长 VWAP 与量能确认

`request_history_kline(..., extended_time=True)` 拉当日 5m 延长时段 K 线：

1. 至少 4 根有效 bar，否则拒绝。
2. 计算同阶段 VWAP。
3. 可执行 ask 必须高于 VWAP。
4. 可执行 ask 距 VWAP 不超过 0.8%，避免追过远。
5. 最新 5m 量 / 前 3 根均量不低于 0.5。

这不是预测方向，而是把“回踩/贴近 VWAP 且仍有量”作为可执行确认。

### 3.6 盘口与费用

A 级：

- 点差 `<= 0.5%`。
- 买一深度 `>= 2x` 下单量。

B 级：

- 点差 `<= 1.0%`。
- 买一深度 `>= 1x` 下单量。

价格漂移、往返费用、买一深度缺失均 fail-closed。快照里的市值/盘口 NaN 已按 0 处理，避免 NaN 比较意外通过。

### 3.7 止盈分级

成交后写入 `metadata_json`：

- `entry_grade`
- `entry_position_usd`
- `entry_structure`
- `cert_lock_pct`

行为：

- A 级：`cert_lock_pct=0`，不触发全量 `SL_CERT_LOCK`；保留 `SL_TRAIL_LOCK` 动态回撤锁，让尾部收益继续跑。
- B 级：`cert_lock_pct=3`，触及 +3% 后全量确定性落袋。
- 既有 `EXT_CERT_LOCK_PCT=3.0` cron 环境对无分级 metadata 的历史仓仍生效；本轮查得当前延长时段持仓为 0，因此无需迁移旧仓。

### 3.8 影子处理

- `PREFILTER_SHADOW` 默认由 true 改为 false。
- 入场链路删除 `_record_prefilter_shadow(session, movers)` 调用。
- 新条件不再只是记录，而是直接拦截或放行。

## 4. 代码落点

| 内容 | 文件 |
|---|---|
| 参数与 A/B 锁 | `scripts/extended_hours_entry_executor.py` |
| 全天/盘前配额双闸 | `scripts/extended_hours_entry_executor.py` |
| A/B 时间窗与分级 | `scripts/extended_hours_entry_executor.py` |
| 快照市值/盘口与 A 级盘口闸 | `scripts/extended_hours_entry_executor.py` |
| 延长 VWAP/量能确认 | `scripts/extended_hours_entry_executor.py` |
| 同日财报相位 | `scripts/extended_hours_entry_executor.py` |
| dry/live 两路硬闸 | `scripts/extended_hours_entry_executor.py` |
| 成交 metadata 与 cert lock | `scripts/extended_hours_entry_executor.py` |
| 参数配置 | `.env.us` |
| 分级/VWAP/财报/盘口测试 | `tests/test_extended_hours_entry_executor.py` |
| 成交 metadata 测试 | `tests/test_extended_hours_order_execution.py` |

## 5. 验证记录

2026-09-20 本地验证：

```bash
PYTHONPATH=. venv/bin/python -m pytest \
  tests/test_extended_hours_entry_executor.py \
  tests/test_extended_hours_order_execution.py \
  tests/test_ext_entry_prefilter_shadow.py \
  tests/test_extended_hours_original_profit_rules.py \
  tests/test_afterhours_hold_to_open.py \
  tests/test_ext_take_profit_only.py \
  tests/test_extended_hours_exit_cron.py \
  tests/test_config_version_writers.py -q
```

结果：`105 passed, 1 warning`。

```bash
venv/bin/python -m py_compile scripts/extended_hours_entry_executor.py
git diff --check
```

均通过。

本轮新增配额回归覆盖：

- 盘前已开 2 笔时返回 `premarket_session_limit`。
- 盘前已开 2 笔时，盘后仍能继续进入后续开仓闸。
- 盘前 0 笔、盘后已开 2 笔时，第 3 笔仍可进入后续开仓闸。
- 全天合计已开 3 笔时，任何阶段继续返回 `daily_limit`。
- 同一轮盘前扫描中，第 2 笔成交后立即停止，不尝试第 3 笔。

周日 dry-run：

```bash
PYTHONPATH=. venv/bin/python3.11 scripts/extended_hours_entry_executor.py --session premarket --dry
PYTHONPATH=. venv/bin/python3.11 scripts/extended_hours_entry_executor.py --session afterhours --dry
```

均返回 `outside_trading_window`，符合休市事实。

## 6. 独立 Agent 复核问题

请另一位 Agent 优先回答以下问题，不要先接受本档结论：

1. **事件新鲜度**：`EarningsCalendar.reportDate/timeOfTheDay` 与 Finnhub `surprisePercent` 的组合是否足以证明“今日同相位 beat”？是否存在日历时区、财报滞后或相位字段不一致导致的误拒/误放？
2. **VWAP 口径**：5m bar 的 `time_key` 解析、未完成 bar、阶段过滤和前 3 根均量是否会系统性偏差？是否应该只用已完成 bar？
3. **阈值合理性**：`$10M/$20M`、`$1B`、0.5%/1%、2x 深度、0.8% VWAP 距离、0.5 量比是否有样本外依据，还是仅是工程保守值？
4. **A级止盈**：A 级禁用全量 +3% 锁是否足够接近“保尾部”的目标？是否应优先实现部分卖出，而不是全仓禁锁？
5. **风险暴露**：B 级 `$2.5k` 与 A 级 `$5k` 在无延长时段价格止损前提下是否合适？每日 3 笔是否仍过高？
6. **供给影响**：同日事件 + `$10M` 成交额 + VWAP 确认后，剩余样本是否足够？如果 10 个交易日几乎无成交，是否说明硬闸过窄？
7. **退出链一致性**：`metadata.cert_lock_pct=0` 能否稳定覆盖 cron 的 `EXT_CERT_LOCK_PCT=3.0`？动态回撤锁的线位和执行价格是否仍存在不可成交风险？
8. **统计口径**：新仓应如何单独评估？建议至少拆 A/B、阶段、事件相位、成交/未成交、MFE、净费用后收益、滑点与拒绝原因。
9. **配额分配**：盘前 2 + 全天 3 的保留额度是否会显著牺牲盘前高质量第三笔？是否应改为“存在盘后财报日才保留”或“盘前盈利平仓后释放 1 笔”？

## 7. 已知风险

1. **不是统计毕业**：本轮是用户要求下的直接工程落地，不是 30 笔或双窗口通过后的转正。
2. **阈值偏严可能零交易**：同日事件与 `$10M` 成交额组合会显著缩小机会面。
3. **A 级无确定性锁**：A 级可能把已到 +3% 的浮盈回吐，依赖动态回撤锁；这与“每笔不亏”目标存在张力。
4. **无部分卖出**：当前 durable exit/账本链只支持全额平仓，部分止盈需要新的订单与账本设计，不能仓促改。
5. **最新 5m bar 可能未完成**：量比可能被低估；安全方向是少交易，但会降低供给。
6. **财报源单点**：日历缺失或 Finnhub 未更新时 fail-closed，可能错过真实事件。
7. **盘前第三笔机会成本**：段内 2 笔是风险预算分配，不是预测盘后机会更好；盘前极强第三候选会被主动放弃。

## 8. 回滚与调整

### 立即停止新开仓

```bash
EXT_ENTRY_ALLOW_SIM=false
```

或删除/注释 entry cron。

### 降参数但不回滚整体

- 放宽成交额：`EXT_ENTRY_MIN_TURNOVER_USD`
- 调整 A 级线：`EXT_ENTRY_A_TURNOVER_USD`
- 恢复 A 级全量锁：`EXT_GRADE_A_CERT_LOCK_PCT=3.0`
- 调整 B 级锁：`EXT_GRADE_B_CERT_LOCK_PCT`
- 恢复盘前可占满全天额度：`EXT_ENTRY_PREMARKET_MAX_PER_DAY=3`
- 二批开关（§10）：`EXT_VWAP_ANCHOR_MATURITY=false` 回旧 VWAP 口径；
  `EXT_PREMARKET_RESERVE_CONDITIONAL=false` 回恒定保留额度；
  `EXT_EARNINGS_MAX_PERIOD_AGE_DAYS` 调新鲜度窗（调大 = 放宽 stale 守卫）

### 完整回滚

回退 `scripts/extended_hours_entry_executor.py`、`tests/test_extended_hours_entry_executor.py`、`tests/test_extended_hours_order_execution.py` 与 `.env.us` 的本批改动。

## 9. 建议后续观察

前 10 个交易日按以下字段单独归档，不与旧配置样本混算：

- `entry_grade`
- `session`
- `earnings_report_phase`
- 延长时段成交额、市值
- VWAP 距离与量比
- 点差、买一深度、费用、滑点
- 是否成交
- MFE / 峰值 / 实际捕获
- 出场原因（A 动态锁、B 确定性锁、RTH、next_open）
- 净收益与回撤

最低观察要求：

1. 若连续 10 个交易日 0 笔，先检查是市场无事件还是硬闸过窄，不得直接放宽阈值。
2. 每级至少 10 笔后再讨论参数；至少 30 笔后再谈策略表现。
3. A/B 必须分开评估；不能把 B 级高胜率与 A 级尾部收益混成单一均值。

## 10. 二轮加固：独立复核意见落地（2026-09-20 二批，用户令「按照建议执行」）

独立分析（Claude）核验本档 §6 问题清单与 `extended_hours_profit_exit_rules.py` / 本执行器代码后，
按优先级落地以下改动。**先登记核验结论（回答 §6），再登记改动。**

### 10.1 §6 问题清单的复核回答

| # | 问题 | 复核结论 | 处置 |
|---|---|---|---|
| 1 | 事件新鲜度 | **有残留漏洞**：日历只证明"今天发财报"，Finnhub `e[0]` 在今日 actual 未入库时是**上一季**记录——"上季 beat + 今日盘前涨 2%（好 guidance/误读）"可透过，今日实际 miss ⇒ 高开回落（9/17 首跑 4 笔病灶变种） | **已大幅削减**（§10.2 P0-1；四批复核实证仍有残留误放行边界——快速披露/非日历财年上季 age 可 ≤150 天，见 §10.9，收口靠同日 actual 交叉验证，不得单纯调低阈值） |
| 2 | VWAP 口径 | 5m bar 解析/阶段过滤无误；但锚点从 4:00 起算，凌晨薄量 bar 拖锚 ⇒ 平稳爬升的动量延续型（主盈利形态）易被 `vwap_extended` 误拒。未完成 bar 问题（已知风险 5）方向安全，暂不改 | **已修锚点**（§10.2 P1-3，§10.7 修订为盘前 07:00/盘后 16:00），未完成 bar 保留观察 |
| 3 | 阈值合理性 | $10M/$20M/$1B/0.5%/2x/0.8%/0.5 均无样本外依据，属工程保守值；vol≥20M 是唯一双窗口同向特征但叠加同日财报后 n=5 | 维持 + §9 攒样；被拒 metrics 现已落台账供回看（§10.2 P1-3） |
| 4 | A 级止盈 | 免全量锁方向与回放一致（大赢家集中在高量票），但与"每笔不亏"目标存在张力；部分卖出是正解，需订单/账本链支持 | **判据已补预注册**（§10.3）；部分卖出立项（§10.4） |
| 5 | 风险暴露 | A $5k / B $2.5k / 日 3 笔在 MAX_OPEN=3、名义闸 $15k 内，无延长时段止损前提下可接受（观察期口径未变） | 不改 |
| 6 | 供给影响 | 硬闸组合确实可能零交易；**旧实现无法归因**（earnings 闸只有一个计数，"源挂了/无事件/相位错配/surprise 过期"不可区分） | **已修**（§10.2 P0-2 + §10.6 源状态） |
| 7 | 退出链一致性 | **实测无 bug**：`profit_exit_rules.py:155` 用 `metadata.get("cert_lock_pct", env默认)`，default 仅在键缺失时生效 ⇒ A 级写入的 `0.0` 正确覆盖 cron 的 `EXT_CERT_LOCK_PCT=3.0`，无 falsy-zero 陷阱。回撤锁执行价风险沿用 DEB-20260919 既有结论 | 不改；已钉回归测试（§10.6） |
| 8 | 统计口径 | §9 字段清单可行；本批新增 `earnings_period_age_days`、structure 被拒 metrics、earnings 子原因台账 | 已随 §10.2 落地 |
| 9 | 配额分配 | "存在盘后财报日才保留"是零新依赖改进（日历已在手上） | **已修**（§10.2 P1-4） |

### 10.2 已落地改动（本批）

**P0-1 Finnhub surprise 新鲜度守卫**（`_earnings_surprise` / `_surprise_period_age_days`）：

- `e[0].period`（财季末日）距今必须在 `[-7, EXT_EARNINGS_MAX_PERIOD_AGE_DAYS=150]` 天内；
  缺失/不可解析/超龄一律 fail-closed，子原因 `surprise_stale`，并打印显式警告行。
- 150 天依据：财季末→披露常规滞后 30~90 天，150 容纳晚披露者；上一季记录（距今 ~170-270 天）必被拦。
- 放行时 `period_age_days` 随 earnings dict 写入成交 `metadata_json`（审计字段）。

**P0-2 earnings 闸拒绝台账 + 源故障可见化**：

- `_earnings_calendar_rows()`：源异常不再静默缓存 `[]`（旧病灶："源挂了"与"今日无财报"不可区分），
  返回 `(rows, error)` 并打印警告；缓存为进程内，cron 每 tick 新进程 ⇒ 源故障只影响本 tick，下轮自动重试。
- 子原因枚举（全部落台账）：`calendar_source_error` / `calendar_missing` / `calendar_phase_mismatch` /
  `calendar_phase_unknown`（相位字段空或不可识别 = 数据源契约漂移信号）/ `finnhub_client_unavailable` /
  `finnhub_no_data` / `finnhub_error` / `surprise_nonpositive` / `surprise_stale`。
- 新台账 `data/extended_hours_earnings_gate_rejects.jsonl`：每次运行聚合一行
  （counts=子原因计数；details=除 `calendar_missing` 外的逐票明细，防每 5 分钟膨胀）。
  **§9"10 日 0 笔先归因"的判断依据即此文件**：`calendar_missing` 主导 = 市场无事件；
  `calendar_phase_unknown`/`surprise_stale` 显著 = 闸/数据源错配，先修口径再谈放宽。
- 闸计数行新增 `earnings_phase` / `earnings_stale` / `earnings_src_err` 三个观测列（earnings 的子集，
  不参与"逐候选归因求和"不变量）。
- `_earnings_calendar_event` 顺带修一处小病灶：旧版首行相位不符即终判，现扫完所有 symbol+date 匹配行。

**P1-3 VWAP 锚点改成熟窗 + 被拒 metrics 落台账**：

- `EXT_VWAP_ANCHOR_MATURITY=true`（默认）时 VWAP/量比只用锚点窗内 bar；false 回旧全时段口径。
  metrics 新增 `bars`/`anchor` 审计字段。锚点初版为盘前 07:30/盘后 16:20（=入场窗起点），
  **四批已修订为盘前 07:00–09:29、盘后 16:00–19:59**（修订依据与影响见 §10.7）。
- `_entry_structure_gate` 改三元返回 `(metrics|None, reason, metrics|None)`：被拒时也携带实测
  metrics，随 pretrade 台账落盘（`structure_metrics` 键）⇒ 被拒候选的 `vwap_dev`/`volume_ratio`
  影子分布由此积累，攒样后回看 0.8%/0.5 阈值是否误杀，不再盲调。

**P1-4 盘前保留额度条件化**：

- 当日日历存在 after/post 相位财报 ⇒ 维持盘前限 2（旧行为）；不存在 ⇒ 盘前可用满全天 3 笔。
- 保守方向钉死：日历源故障或空日历一律视为"有盘后财报"（继续保留）。
- `premarket_session_limit` 结果新增 `reserved_for_afterhours` 字段供归因。

**代码落点（本批增量）**：均在 `scripts/extended_hours_entry_executor.py` +
`tests/test_extended_hours_entry_executor.py` + `tests/test_extended_hours_order_execution.py` + `.env.us`。

**验证记录（二批）**：

```bash
PYTHONPATH=. venv/bin/python -m pytest \
  tests/test_extended_hours_entry_executor.py \
  tests/test_extended_hours_order_execution.py \
  tests/test_ext_entry_prefilter_shadow.py \
  tests/test_extended_hours_original_profit_rules.py \
  tests/test_afterhours_hold_to_open.py \
  tests/test_ext_take_profit_only.py \
  tests/test_extended_hours_exit_cron.py \
  tests/test_config_version_writers.py \
  tests/test_earnings_calendar_source_order.py -q
# 121 passed, 2 warnings（一批为 105；二批新增 7 例；三批新增 3 例并纳入 6 个源序用例
#   ——核查更正: 6 例中 1 例系三批新增, HEAD 既有为 5 例, 三批实际基线 120, 见 §10.8）
```

新增回归覆盖：stale period 拒绝/新鲜 period 放行/period 缺失 fail-closed；
子原因四态区分（源故障/无事件/相位不符/相位不可识别）；同 symbol 多行日历须扫完；
`_afterhours_earnings_expected` 三态（有/无/源故障保守）；盘前第 3 笔释放与源故障保留；
VWAP 成熟窗锚点排除凌晨 bar + 回滚开关恢复旧口径；structure 被拒携带 metrics；
earnings 台账聚合行（空轮不写）。
周日 dry-run 两段均 `outside_trading_window`，符合休市事实；`py_compile` / `git diff --check` 通过。

### 10.3 A 级免全量锁的判据预注册（P1-5，补 DEB-20260919 §7.1 的口径缺口）

9/19 §7.1 预注册的是"**全样本** +3% 落袋"的破例判据；9/20 起 A 级被豁免 ⇒ 若不分级出数，
§7.1 判据会被 A/B 混合样本悄悄稀释。本条为 A 级免锁行为的独立预注册：

| 项 | 内容 |
|---|---|
| 预注册对象 | A 级 `cert_lock_pct=0`（禁用全量 +3% 锁，仅留 `SL_TRAIL_LOCK` 动态回撤锁） |
| 判据（复查日 2026-10-17 或 A 级累计 10 笔已结算，先到者） | ① A 级"峰值曾 ≥3%"笔的**峰值回吐均值 ≤ 2pp**（回撤锁设计捕获 60%，MFE 3% 的期望让出 ≈1.2pp+滑点，2pp 为容忍上限）；② A 级全样本净均值 ≥ B 级净均值 −0.5pp（证明尾部收益没有被免锁吃掉）；③ A 级"峰值曾 ≥2% 且净出场 <0"（浮盈回吐成亏）占比 ≤ 20% |
| 数据源 | `trade_records`（`metadata_json.entry_grade='A'`）+ 出场原因；口径沿用 §15.3（DEB-20260919）预注册谓词，测试笔不入分母 |
| 不达标处置 | 二选一交用户裁：恢复 `EXT_GRADE_A_CERT_LOCK_PCT=3.0`，或等 §10.4 部分卖出落地 |
| 回滚 | `EXT_GRADE_A_CERT_LOCK_PCT=3.0`（env 级即时生效，零代码） |

**10/17 复查必须 A/B 分级分别对 §7.1 判据与本条判据出数，禁止混合评估。**

### 10.4 部分卖出立项登记（P2-6，本批不实现）

回放中 `so50_lock4pct`（+4% 卖一半、余半持到开盘）是唯一同时改善中位数（+2.0%）与胜率（54.5%）、
均值代价可接受（-0.14pp/笔）的臂，也是"A 级保尾部"与"每笔不亏"两个目标的唯一兼容路径。
阻塞项：durable exit / 账本链只支持全额平仓（`playbook_exits` intent 语义、POSITION_SYNC 对账、
`open_positions` 部分减仓）。**登记为独立变更立项**，不与本批硬闸混做；落地前 A 级按 §10.3 判据观察。

### 10.5 本批后的 §7 已知风险状态更新

- 风险 5（未完成 5m bar 低估量比）：保留，方向安全（少交易）。
  **留痕自纠（四批）**：本条初版称「B 级盘前 08:15–08:35 段可能因 `bars<4` 拒绝」——**该说法有误**：
  盘前锚点（初版 07:30）在 B 窗起点 08:15 之前 45 分钟，届时已有 ≥9 根可用 bar，不存在预热拒绝。
  初版锚点下真实受预热影响的段是：盘前 A 07:30–07:50、盘后 A/B 16:20–16:40（锚点=窗起点，前 20 分钟
  bar 不足 4 根）。§10.7 锚点修订（盘前 07:00/盘后 16:00）后**四段入场窗预热拒绝全部归零**，
  该风险面收窄回「未完成 bar 低估量比」本身 ⇒ 台账观察面不变。
- 风险 6（财报源单点 fail-closed 错过真事件）：fail-closed 语义不变，但**已可归因**
  （`calendar_source_error`/`finnhub_error` 子原因 + 显式警告行），不再是静默丢单。
- 风险 7（盘前第三笔机会成本）：已条件化（P1-4）——仅在"当日确有盘后财报"时支付该成本。
- 新增已知项：`surprise_stale` 守卫在**晚披露财季**（period 距今 150 天边缘）存在误拒可能；
  方向安全（少交易），台账可观测，误拒显著时再议放宽 `EXT_EARNINGS_MAX_PERIOD_AGE_DAYS`。

### 10.6 三轮核查补强（Codex，2026-09-20）

**财报源状态显式化**：

- `EarningsCalendar` 新增 `get_earnings_calendar_with_status()`，返回 `(rows, error)`；旧 `get_earnings_calendar()` 契约不变。
- 富途主源与 AV 备源皆空时返回 `futu_and_av_empty`，入口 `_earnings_calendar_rows()` 将其传播为 `calendar_source_error`，
  不再把普通底层断流误归因为 `calendar_missing`。
- 保守边界：若 3 个月窗口真实无财报，也会被标记为源不可用；方向是少交易且可归因，不改变 fail-closed 行为。

**A 级零值锁覆盖回归**：

- 新增测试钉住 `EXT_CERT_LOCK_PCT=3.0` 且 `metadata.cert_lock_pct=0.0` 时必须 `hold`、`cert_lock_pct=None`，
  防未来重构把 falsy-zero 当成“键缺失”回退到 cron 默认。

三批新增回归：

- 双源皆空时状态 API 返回 `futu_and_av_empty`。
- 入口传播底层状态错误，不降级为 `calendar_missing`。
- A 级零值 metadata 覆盖 cron 环境默认。

### 10.7 四批：用户质询「交易时间是否过少」与锚点修订（2026-09-20）

**质询原文**：VWAP 锚点改成熟窗——盘前从 07:30、盘后从 16:20 起算——交易时间是否过少？

**复核结论：质询部分成立，且挖出一处真设计错误。**

1. 有效入场窗损失量化（旧锚 = 入场窗起点，前 20 分钟 bar<4 预热拒绝）：

| 窗 | 入场窗 | 旧锚下有效窗 | 损失 |
|---|---|---|---|
| 盘前 A | 07:30–09:15（105min） | 07:50–09:15（85min） | 20min |
| 盘前 B | 08:15–09:15（60min） | 无损 | 0 |
| 盘后 A | 16:20–18:30（130min） | 16:40–18:30（110min） | 20min |
| 盘后 B | 16:20–17:30（70min） | 16:40–17:30（50min） | 20min（**占窗 29%**） |

2. **盘后 16:20 锚无病灶依据（设计错误）**：
   - 成熟窗锚的病灶依据是「凌晨 4:00–7:00 薄量 bar 拖锚」——那是**盘前特有**病灶；
     盘后时段 16:00 才开始，不存在凌晨死区，无病灶 bar 可排。
   - 16:00–16:20 恰是盘后财报发布后**量最重、价格发现最真**的 20 分钟；排除它 VWAP 反而失代表性。
   - 副作用即在 A/B 窗开头制造 bars<4 预热拒绝，把盘后 B 有效窗压掉 29%。
3. 盘前 07:30 锚有病灶依据（排凌晨死区），但切得比必要更狠：对今日有财报的候选，
   07:00+ 的 bar 普遍已有真实事件量，07:00–07:30 不应排除。

**修订（已落地，`VWAP_ANCHOR_WINDOWS`）**：盘前 `(700, 929)` / 盘后 `(1600, 1959)`。

- 盘前 07:00：仍排除 4:00–7:00 凌晨死区（P1-3 原意保留），纳入 7:00+ 事件量。
- 盘后 16:00：锚 = 全时段起点，不排除任何 bar（无病灶可排）。
- 效果：四段入场窗预热拒绝**归零**——盘前 A 07:30 开窗时已有 ≥6 根 bar，盘后 16:20 时 ≥4 根。
- `ENTRY_WINDOWS`（交易窗本身）不变，只改 VWAP/量比的 bar 参与范围 ⇒ 不新增入场面，
  §9 攒样协议与判据零影响。
- 回滚：`EXT_VWAP_ANCHOR_MATURITY=false` 回旧全时段口径（env 级，零代码）。

**自纠留痕**：§10.5 风险 5 初版「B 级盘前 08:15–08:35 可能因 bars<4 拒绝」为**错误陈述**
（盘前锚在其前 45 分钟，届时 ≥9 根 bar），已在 §10.5 原位更正。

**验证（四批）**：121 passed（executor 用例 34→35：新增盘后 16:00 锚边界一例；
盘前锚用例补钉 06:55 排除/07:05 纳入边界 bar）。三批记录「121」与可复现基线 120 相差 1 的疑点
**已由 §10.8 核查收口**：三批把 1 个新增用例误计入既有用例（重复计数），实际基线 120，四批 120+1=121 完全自洽。
`py_compile` / `git diff --check` 通过。

### 10.8 三批（§10.6）独立核查回执（四批，2026-09-20）

用户令核查另一 Agent（Codex）三批更新。对 §10.6 全部宣称逐条与代码比对：

| 宣称 | 核验证据 | 结论 |
|---|---|---|
| `get_earnings_calendar_with_status()` 返回 `(rows, error)`；旧契约不变 | `earnings_calendar.py:254/307`：旧方法委托新 API 取 `[0]`；旧契约唯一生产消费方 `llm_universe_scan.py:1230` 不受影响；`intraday_reentry` 用的是 `get_watchlist_earnings`，不相干 | ✅ |
| 双源皆空 → `futu_and_av_empty`，入口传播 `calendar_source_error` | `:293` 置错并与 `_cache_error` 成对缓存；executor `:1128` 消费 with_status、`:1160` error→`calendar_source_error`；传播测试 patch `app.data.earnings_calendar.EarningsCalendar`（executor 为函数内 import ⇒ patch 生效，测试无网络/无 futu） | ✅ |
| 保守边界：3 个月窗口真实无财报也标源不可用 | 属实——判据只有 rows 为空。后果：台账主子原因记 `calendar_source_error` 而非 `calendar_missing`，方向 fail-closed 且可归因，与 §9「先归因再放宽」协议一致；交易日富途主源 7 天窗正常非空，误标概率低 | ✅（如实记录） |
| A 级零值锁回归钉住 falsy-zero | `extended_hours_profit_exit_rules.py:155-156`：**调用时** `os.getenv`（非模块加载期常量）⇒ 测试 `setenv("EXT_CERT_LOCK_PCT","3.0")` 真实生效；metadata 键存在时不看 env ⇒ 钉的正是「0.0 被当键缺失回退 3.0」的回归面，**非空跑** | ✅ |
| 缓存语义 | `force_refresh` 被尊重；error 随缓存 rows 成对返回；executor cron 每 tick 新进程 ⇒ 源故障缓存不跨 tick（§10.2 P0-2 语义未被破坏） | ✅ |

**核查发现两处三批记录错误（仅数字，无行为影响，已原位标注）**：

1. 「纳入 6 个**既有**源序用例」——实测 HEAD 该文件既有 5 例，第 6 例
   （`test_status_api_distinguishes_dual_source_empty`）系三批自己新增，被重复计入既有。
2. 「121 passed」因此虚高 1：三批实际基线 = 112 + 3 新增 + 5 既有 = **120**。
   四批实测 collected=121（120+1）与之完全自洽；§10.7 登记的「无法复现」疑点由此收口。

**新增观察项（非三批缺陷，登记）**：标准验证命令纳入 `tests/test_earnings_calendar_source_order.py`
后，其中 `test_futu_primary_live_smoke`（`@pytest.mark.live`，HEAD 既有）在 OpenD 可达时
**会真实打开 futu 行情上下文**（本次核查运行即 PASSED 而非 SKIPPED）。盘中跑全量验证会
多占一个 OpenD 连接槽（与「非交易路径免 futu」台账原则同族）。建议：盘中验证加
`-m "not live"` 或临时剔除该文件；非盘中运行无影响。

### 10.9 四批独立复核回执（Codex，2026-09-20）

**复核结论**：§10.7 锚点修订实现与测试属实，可继续运行；§10.2 P0-1 的「已修」表述偏强，
新鲜度守卫仍存在可实证的残留误放行边界。

**锚点核验**：

- `VWAP_ANCHOR_WINDOWS` 为盘前 `(700, 929)`、盘后 `(1600, 1959)`；`ENTRY_WINDOWS` 未变。
- 最早入场起点到锚点的预热 bar：盘前 `07:30-07:00=6` 根、盘后 `16:20-16:00=4` 根，
  均满足 `bars>=4`，「预热拒绝归零」在正常有量 bar 前提下成立。
- 回归覆盖 `06:55` 排除、`07:05` 纳入、盘后 `16:05-16:20` 纳入；`EXT_VWAP_ANCHOR_MATURITY=false`
  仍可回旧口径。

**验证**：

```bash
PYTHONPATH=. venv/bin/python -m pytest -q \
  tests/test_extended_hours_entry_executor.py \
  tests/test_extended_hours_order_execution.py \
  tests/test_ext_entry_prefilter_shadow.py \
  tests/test_extended_hours_original_profit_rules.py \
  tests/test_afterhours_hold_to_open.py \
  tests/test_ext_take_profit_only.py \
  tests/test_extended_hours_exit_cron.py \
  tests/test_config_version_writers.py \
  tests/test_earnings_calendar_source_order.py
# 121 passed, 2 warnings

PYTHONPATH=. venv/bin/python -m pytest -q -m "not live" <同上文件集>
# 120 passed, 1 deselected, 2 warnings
```

`py_compile` / `git diff --check` 通过；周日两段 dry-run 均为 `outside_trading_window`。

**P0-1 残留实证**：

- 2026-09-20、日历同日 pre-market、Finnhub `period=2026-05-31`、`surprisePercent=12` 时，
  当前守卫计算 `period_age_days=112` 并放行。
- 该场景可对应「当前财季末 2026-08-31、今日披露，但 Finnhub 今日 actual 未入库，最新记录仍是
  上一季 2026-05-31」的非日历财年/快速披露公司；上一季 age 并不总是 170–270 天。
- 因此 150 天阈值是**风险削减启发式**，不是「今日 actual = 最新记录」的严格证明；§10.1 中
  「已修」应读作「已大幅削减」，未完全关闭误放行。

**下一轮加固候选（不在本批改行为）**：

1. 用 Finnhub `earnings_calendar(from=today,to=today,symbol=ticker)` 的同日 `epsActual/epsEstimate`
   或 Futu beat rank 的 `released_date + detail_post_period` 做同日 actual 精确交叉验证。
2. 若同日 actual 尚未入库，继续 fail-closed 并落 `same_day_actual_missing` 子原因，不得用
   `period_age` 推断放行。
3. 在新闸落地前，不得单纯调低 `EXT_EARNINGS_MAX_PERIOD_AGE_DAYS`；那会把快速披露的真人
   beat 一并误拒。

## 11. 零成交归因批独立复查回执（2026-09-22，用户裁「按照建议执行」）

**复查范围**：工作区未提交改动（near-miss 台账 + 测试迁移）、已提交批（488a41847 券商写口挡板 /
87ad952c4 coverage 台账 / b58f24080 ETF 滤除 / 129c93243 carry 影子）、生产面（cron、.env.us、
9/21-9/22 ext_entry.log、entry_state、券商订单痕迹）。

**结论**：机制健康可交易；9/21-9/22 零成交 = 财报季空窗 ×「当日同相位事件」门的结构性无供给
（9/22 实测候选 42-46 只 × 日历同日仅 6 行，交集恒 ∅，三源同证），非执行故障。归因批
（台账/滤除/影子/near-miss）符合 §9.1「先归因不得直接放宽」纪律，全部核收。

**验证记录**：

```bash
PYTHONPATH=. venv/bin/python3.11 -m pytest -q \
  tests/test_extended_hours_entry_executor.py tests/test_extended_hours_order_execution.py
# 76 passed

PYTHONPATH=. venv/bin/python3.11 -m pytest -q \
  tests/test_ext_entry_test_symbol_guard.py tests/test_cancel_orphan_ext_order.py
# 28 passed
```

本次复查全程 pytest 零真实下单：conftest `_block_live_broker_orders` 封的
`futu.OpenSecTradeContext` 写口与 `_open_trade_context()` 实用类精确同类。配额用例已钉死
常量（MAX_PER_DAY/PREMARKET/MAX_OPEN/敞口帽），不再随 `.env.us`（现 10/10/5）漂移。

**NVDA 泄漏事故残留裁决**：state 中 `2026-09-22:US.NVDA`(failed) 系 16:59 清理后对账按
当日 ET 泄漏单 8197135 合法回填——无害（failed 不占配额、NVDA 当日无财报事件本就不入选、
attempt 键次日过期），**不重复人工清理**。全局 active-order 闸的"外来活单压制入场"风险
随写口挡板落地而源头封闭；外来单处置工具仍为 `cancel_orphan_ext_order`（exit cron 只告警不撤单）。

**DST 免疫扩段（本批落地，crontab 4 行）**：冬令时 2026-11-01 起 ET 窗右移 1h，原固定 CST
时段将丢盘前 09:00-09:29 ET（流动性最好段，含 A/B 级窗尾 9:15）与盘后 19:00-19:59 ET。
处置：entry premarket `16-21→16-22`、entry afterhours `4-7→4-8`、carry shadow 两行同步扩段
（备份 `/tmp/crontab.bak_20260922`）。窗外 tick 在 `_in_session_window` /
`get_extended_hours_session` 判定即返回（先于任何 futu 连接），夏令时行为零变化；
此举同时取代 carry shadow 脚本头「11 月两行各推后 1h」的手工操作。回滚=时段改回原值。

**10 月财报季观察项（登记，零代码变更）**：开季（约 10 月中旬）后盯
`extended_hours_earnings_gate_rejects.jsonl` 子原因迁移——`calendar_missing` 退潮后
`finnhub_no_data`/`surprise_stale` 不得成为新瓶颈（Finnhub 免费版 surprise 覆盖 6-18 只/日
是已知第二道 fail-closed；§10.9 的 period_age 残留边界在高披露密度下暴露面变大）。

**已知小漂移（本批不处置）**：`config/watchlist_us.json` 缺失 → earnings_calendar 走静态
KEY_US_SYMBOLS fallback。不影响 EXT 入场闸（该表仅用于 enrichment 行业映射与
watchlist/beat_rank 备选源，两源当前均关）；若开 `EXT_ENTRY_SOURCE_WATCHLIST` 须先补文件，
且注意文件路径的 sector 经 `_SECTOR_MAPPING` 再映射，直接倒灌静态表值会落 Unknown。

**数据源定版**：near-miss 台账（`ext_entry_earnings_near_miss.jsonl`，record-only、
按 (trade_date, session, symbol) 去重、env `EXT_ENTRY_EARNINGS_NEAR_MISS` 可关）随本回执
提交入库，作为 carry 影子 9/25 前后裁决（n≥3 交易日 ∧ 上界≥2 只/日 → 立 DEB 提请放宽
「同日」语义；<0.5 只/日 → 停采）的并行证据面。文件内 9/22 早段存在旧 schema 行
（`base_gate=price_momentum_volume_passed`，无 turnover 字段），分析时按 base_gate 过滤。
