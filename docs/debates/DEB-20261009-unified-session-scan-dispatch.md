---
title: 统一市场时段与盘前/盘中/盘后/夜盘扫描调度设计
date: 2026-10-09
status: 整体待决策；P0-1 da_bridge 冬令时热修已执行（§15），未落地 SessionResolver/夜盘/交易行为；多 Agent 只读复查意见已纳入 · §13 Hermes 独立复查已推翻 §1.3 立项依据，建议改采 B′ 先减法后新件 · §14 主线程复核：E 表全中，新增 N1–N5 实证，P0-1 修复配方更正为三件套，裁决建议见 14.3
trigger: 用户提出结合 2026-11-01 冬令时切换与 2026-12-06 起美股核心交易所夜盘，评估盘前、盘中、盘后扫描是否可通过现有服务机制融合并按场景快速配置
---

# DEB-20261009 · 统一市场时段与扫描调度设计

## 0. 结论（待裁决）

**方向可行，但不能直接把现有 cron 或 `MarketPhase.OVERNIGHT` 改成 23 小时轮询。**

推荐采用方案 B：

1. 新增独立的 **US Market Session Resolver** 作为美国市场时段唯一口径；
2. 新增 **Scan Scene Dispatcher** 只统一批处理/一次性扫描任务的 ET 触发、幂等与可观测性；
3. 将“市场开着”、“可以扫描”、“可以订阅”、“可以下单”拆成四层独立配置；
4. 2026-12-06 夜盘初期只允许 **scan-only/shadow**，零订单、零生产交易写入；
5. 自动下单、出场与持仓管理继续唯一走 Playbook，禁止新建 23 小时平行交易主管线。

本 DEB 是设计稿。当前没有代码、配置、crontab 或 Hermes 任务变更；落地前须用户另行裁决。

## 1. 背景

### 1.1 用户提出的外部变化

- 美国冬令时将于 **2026-11-01** 生效。现有主要 UE/DA cron 已采用 CST 双季双挂 + ET 窗口守卫，但并非全表均已免疫。
- 用户给出信息：**2026-12-06 起 Nasdaq、NYSE Arca 等核心交易所增设夜盘，整体进入每天 23 小时交易**。公开报道口径常见为 **21:00 至次日 04:00 ET**，全天保留约 1 小时维护窗口。
- 该边界尚未在本仓库建立官方 primary source。Phase 0 必须确认：准确起止、启用日期、交易所范围、标的范围、周末/节假日/半日市规则、维护窗口和券商/数据源支持。确认前本 DEB 只将其记为“用户输入，待官方确认”，不得写入生产判定。

### 1.2 当前系统事实

现有扫描与批处理有两条形态：

1. **常驻服务循环**：`scheduler_core` 下的 `scan_loop`、LLM 策略循环、持仓监控、数据订阅等按 `MarketPhase` 自适应。
2. **一次性 cron/Hermes 批处理**：UE scan、DA fresh、UE confirm、tripwire、延长时段观察/结算等由 crontab 或 Hermes 定时触发。

关键既有约束：

- `signal_pipeline.scan_loop` 不能整体 gate。它虽在盘中禁用入场执行，但仍驱动 watchdog 心跳、日/周/月复盘、持仓快照、财报刷新等 housekeeping。
- Playbook 是唯一自动下单与持仓管理管线；非 Playbook 新开仓已被 fail-closed。
- 延长时段历史上曾先开放执行后安全返工，相关 DEB 已确认盘前/盘后执行必须治理先行。本议题不能复用旧旁路路径。

### 1.3 已核实的重复口径

| 层 | 当前实现 | 限制 |
|---|---|---|
| 主调度 phase | `app/core/scheduler_triggers.py` `MarketPhase/get_market_phase` | 支持节假日/半日市；`OVERNIGHT` 语义是 20:00–04:00 深度休眠，不能直接重载为夜盘交易 |
| 时区工具 | `app/utils/timezone.py` `is_market_open/is_pre_market/is_after_hours/get_market_session` | 与主调度存在边界差异，不能作为新 SSOT |
| 双市场共享模块 | `app/shared/market_hours.py` | 美国无半日市、无跨午夜；历史上配置驱动分支不可达，现为硬编码默认 |
| 延长时段 gate | `scripts/extended_hours_market_session.py` | 只识别 premarket/afterhours，无夜盘 |
| 日内风险 taxonomy | `app/core/shared_kernel.py` | 描述 opening/morning/midday 等交易内部 phase，不是市场开闭 SSOT |

## 2. 目标

1. 建立一个可测试、可热载、可回滚的市场时段解析器，统一盘前、盘中、盘后、维护窗与夜盘。
2. 将扫描任务按场景配置，而不是按 cron 表达式和 CST/ET 手工换算散落维护。
3. 做到调度可观测：预期触发、实际触发、跳过原因、迟到、重复、失败、回放都可审计。
4. 让 2026-11-01 DST 与 2026-12-06 夜盘切换以配置/profile 切换为主，避免大规模手改 cron。
5. 为后续夜盘 SIM/live 执行预留安全边界，但本阶段不开启任何执行。

## 3. 非目标

- 不改变 Playbook 唯一下单/持仓管理架构。
- 不新增夜盘自动开仓、止损或平仓行为。
- 不将 `signal_pipeline.scan_loop` 改造成 23 小时交易主循环，也不整体关闭它。
- 不在本 DEB 中确认交易所公告细节；官方事实确认是 Phase 0 任务。
- 不一次性迁移全部 cron；只允许影子对照后再逐项迁移。
- 不处理与本议题无关的历史测试失败。

## 4. 立案前检查

### 4.1 同轴既有影子枚举

以下影子与延长时段/UE 扫描相关，但均不解决“市场时段 SSOT + 场景调度”这一基础设施缺口：

| 影子/机制 | 与本 DEB 的关系 |
|---|---|
| `ue_midday_shadow` | 已按预注册判据 FAIL/KILLED；本 DEB 不重开午间扫描策略 |
| `ue_confirm_gate` | 继续沿用，本 DEB 只调整调度触发方式，不改判据 |
| `eth_premarket_exit_shadow` / `eth_afterhours_exit_shadow` | 继续按现有结算口径运行；本 DEB 不改出场策略 |
| `eth_ext_entry_prefilter` | 延长时段开仓相关影子，不因夜盘扫描自动启用 |
| `eth_relative_volume_gate` / `eth_patient_maker` | 已有独立判据；本 DEB 不复用为全局夜盘闸 |
| `premarket_invalidation` | 继续在开盘后早段运行；不改规则 |
| `premarket_peer_shadow` / `premarket_reprice_shadow` | 已有裁决/退役状态；不重开 |

本 DEB 不新增策略采样影子，不新增交易字段，不改变任何既有判据。

### 4.2 既有裁决与冻结面

- 引用 `DEB-20260917-extended-hours-auto-trading.md`：延长时段执行必须先治理，不能因交易所开放而直接放开。
- 引用 `DEB-20260918-extended-hours-ssot-and-observability.md`：SSOT 与全链路可观测性前置。
- 引用 `DEB-20260919/20260920` 系列延长时段 DEB：价格、费用、盘口、时间窗与 SIM-only 边界不得绕过。
- 不修改已冻结退出参数，不重开已 KILLED 影子。

## 5. 可选方案

### 方案 A：直接修改现有 `MarketPhase.OVERNIGHT` 与 cron

**核心思路**：把 21:00–04:00 映射为 active market，并把相关 cron 频率扩到夜盘。

**否决理由**：

- `OVERNIGHT` 目前被 scan loop、position monitor、订阅、emergency、VIX 降级等多路径解释为休眠/低活动。
- 直接重载会造成大量隐性行为变更，尤其可能影响监控、订阅与风控。
- 周日跨午夜、节假日、半日市与维护窗没有统一 trade date 语义。
- 固定 CST cron 继续漂移，问题不是频率，而是时间口径。

### 方案 B：SessionResolver + 兼容 adapter + 场景 dispatcher（推荐）

**核心思路**：

1. 新建 canonical resolver，不直接改旧 API 行为；
2. 旧 API 通过兼容映射保持原输出；
3. 批处理任务先影子迁移到 ET 场景 dispatcher；
4. 夜盘以独立 profile 启用，只开启 scan/observation。

**优点**：

- 行为变更可分批、可回滚、可测试；
- DST、半日市、节假日与跨午夜集中在一个模块；
- 调度执行与交易执行解耦；
- 12/6 只需切换 profile，不需要大规模手改 cron。

**缺点**：

- 需要先做契约测试和 parity log；
- 短期存在新旧 cron 双轨，需要锁与幂等防双跑；
- 需要逐步迁移消费者，不能一次替换。

### 方案 C：保留双季 cron，逐个补丁

**核心思路**：继续维护 CST 20/21 双季双挂，夜盘再新增双季行。

**适用条件**：仅作为 11 月前的保守兜底或高优先级漏项止血。

**否决为长期方案的原因**：

- cron 表数量随任务数线性膨胀；
- 每个任务仍需自带 ET 守卫和幂等；
- 漏跑、错跑、双跑缺乏统一审计；
- 12/6 周日夜盘与跨午夜场景难以可靠表达。

## 6. 设计

### 6.1 MarketSessionProfile

新增配置示例（仅为设计，不在本 DEB 落地）：

```yaml
market_sessions:
  us:
    resolver_version: 1
    active_profile: legacy
    profiles:
      legacy:
        pre_market: {start: "04:00", end: "09:30"}
        regular: {start: "09:30", end: "dynamic_close"}
        after_hours: {start: "dynamic_close", end: "20:00"}
        maintenance: {start: "20:00", end: "04:00"}
      overnight_v2:
        effective_from: "2026-12-06"   # 待官方 primary source 确认
        pre_market: {start: "04:00", end: "09:30"}
        regular: {start: "09:30", end: "dynamic_close"}
        after_hours: {start: "dynamic_close", end: "20:00"}
        maintenance: {start: "20:00", end: "21:00"}
        overnight_trading:
          start: "21:00"
          end: "04:00"
          crosses_midnight: true
```

`SessionResolver` 必须返回 immutable 结果：

| 字段 | 语义 |
|---|---|
| `canonical_session` | `pre_market` / `regular` / `after_hours` / `maintenance` / `overnight_trading` / `closed` |
| `legacy_phase` | 兼容 `MarketPhase` 的旧值 |
| `session_start` / `session_end` | aware datetime，ET 口径 |
| `trade_date` | 业务归属交易日，不是简单当前 ET 日期 |
| `profile_version` | 配置版本，用于审计 |
| `calendar_source/status` | 官方/Futu/离线 fallback 与降级原因 |
| `minutes_to_end` | 场景降频/收口判断 |

实现约束：

- 只接受 aware datetime；naive 输入必须显式声明时区或拒绝，不得隐式按宿主时区解释。
- 支持注入 clock 和 calendar，测试不得依赖真实当前时间。
- 热路径不得隐式触发 OpenD 网络请求。
- 日历查询失败必须显式降级；执行相关调用必须 fail-closed，扫描相关调用可按配置降级但要留痕。

### 6.2 跨午夜与 trade date

夜盘不能按自然日直接归属：

- `after_hours` 建议归属当日收盘后的交易日；
- `overnight_trading` 建议归属**下一交易日**；
- 示例：周日 21:00 与周一 03:59 均归周一 session；周一 04:00 起转入 pre-market。

该规则须待交易所 primary source 确认，特别是：

1. 周日首夜是否确实从 2026-12-06 开始；
2. 节假日前一晚是否交易；
3. 半日市当日夜盘是否启用；
4. 次日为假日时前夜何时终止；
5. venue 之间是否有差异。

禁止把 Sunday 简单标记为常规交易日，否则会污染日级幂等、结算与报表。

### 6.3 兼容映射

初期不改旧语义：

| canonical | legacy `MarketPhase` | `timezone.get_market_session` | 执行权限 |
|---|---|---|---|
| `pre_market` | `PRE_MARKET` | `pre_market` | 按现有盘前策略 |
| `regular` | `TRADING` | `regular` | 仅 Playbook |
| `after_hours` | `AFTER_HOURS` | `after_hours` | 按既有 ETH 机制，不自动扩夜盘 |
| `maintenance` | `OVERNIGHT` | `closed` | 禁止 |
| `overnight_trading` | 初期仍映射 `OVERNIGHT` | `closed` | 禁止；只允许 scan/observation |

后续如果要启用夜盘执行，必须单独开 DEB，新增显式 `overnight_trading` 执行语义，不允许通过兼容映射“借道”。

### 6.4 Scan Scene Dispatcher

Dispatcher 的边界：

- 管理**批处理/一次性任务**：UE scan、DA fresh、confirm、tripwire、观测、结算；
- 不接管常驻实时执行、行情订阅、Playbook 自动执行、出场监控与风控循环；
- 不成为新的交易策略引擎。

配置维度：

```yaml
scan_scenes:
  premarket:
    enabled: true
    mode: scan_only
    jobs:
      - id: ue_scan
        schedule: {minute_of_session: 10}
        timeout_seconds: 1800
        lock_key: "ue_scan:{session_id}"
        execution: scan_only
        stale_policy: fail
  overnight:
    enabled: false
    mode: shadow
    jobs:
      - id: overnight_data_probe
        interval_minutes: 15
        execution: observe_only
```

每个 job 必须声明：

1. `scene` 与 `job_id`；
2. schedule（session 起点偏移、固定 ET 时刻或间隔）；
3. timeout 与并发上限；
4. lock/lease 与幂等键；
5. allowed execution（`observe_only` / `scan_only` / `inject` / `execute`）；
6. 漏跑/迟到/重复/失败策略；
7. 回滚开关。

调度键建议使用 `session_id + scene + job_id`，禁止只用本地日期。市场敏感任务默认不允许自动补跑；统计/结算任务可在幂等键保护下补跑。

### 6.5 可观测性

每次预期任务必须留下：

| 字段 | 要求 |
|---|---|
| `planned_at` | ET，由 SessionResolver 计算 |
| `dispatched_at` / `started_at` / `completed_at` | 实际执行时刻 |
| `skipped_reason` | DST、closed、disabled、duplicate、stale、capacity 等必须可归因 |
| `lateness_ms` | 调度漂移 |
| `attempt` / `lock_status` | 重复与并发控制 |
| `result` / `error` | 成功、失败、超时、部分完成 |

验收必须能区分：

- 预期跳过；
- 窗外拒绝；
- 锁冲突；
- 任务失败；
- 任务漏跑；
- 双季 cron 双挂导致的重复；
- dispatcher 与旧 cron 灰度双跑。

### 6.6 DST 与 cron 迁移

短期（2026-11-01 前）：

- 保留 UE/DA 主链已验证的双季双挂；
- 修复已发现的单挂漏项；
- 做一次 crontab/Hermes 全表 ET 等价审计。

中期：

- 新增 dispatcher 影子运行，不关闭旧 cron；
- 输出计划触发 vs 实际触发对照；
- 连续多日无漏跑、无重复、无错误窗后再迁移；
- 迁移时保留原 cron 备份作为回滚路径。

新任务禁止继续写固定 CST 双季表达式；一律由 ET session gate 决定。

### 6.7 执行安全分层

必须显式分离：

1. **market open**：交易所夜盘是否开启；
2. **scan enabled**：是否扫描；
3. **data capable**：行情、深度、时间戳、延迟、成交流是否可用；
4. **execution allowed**：是否允许下单/管理持仓。

夜盘执行前置能力矩阵必须覆盖：

- 券商是否支持对应 session、订单类型、TIF、撤改单、状态查询；
- 数据源是否支持夜盘 quote/depth/trade stream 和准确时间戳；
- 标的/venue 覆盖范围；
- 费用、点差、深度、滑点、成交概率；
- 配额、重连、错误语义；
- 对账与 in-flight 恢复。

数据年龄、深度、券商状态、配额、异常价差、熔断/暂停等条件在执行层必须 fail-closed。不得因 SDK enum 存在就认定账户能力可用；必须以账户级 probe 为准。

## 7. 多 Agent 只读复查结论

### 7.1 架构视角

- 可以融合，但不应直接重载 `MarketPhase.OVERNIGHT`；
- 应新建 canonical `SessionResolver` / `MarketSessionProfile`，用兼容 adapter 逐步替换；
- `scan_loop` 只能做 job 级 gate，不能整体跳过，否则 housekeeping 断链；
- `position_monitor`、订阅、watchdog、OrderTracker 对 overnight 的语义需逐项独立评估；
- Resolver 必须注入 clock/calendar，并覆盖 DST、半日市、跨午夜、节假日。

### 7.2 运维视角

已核实主链：

| 链路 | 判定 |
|---|---|
| UE DA feed 20:00/21:00 CST 双挂 + ET 07:30–08:05 守卫 | 正确 |
| UE premarket 21:10/22:10 CST 双挂 + ET 09:00–09:29 守卫 | 正确 |
| UE confirm 21:40/22:40 CST 双挂 + ET 09:32–10:30 守卫 | 正确 |
| tripwire 宽窗 + ET 09:30–16:00 自守卫 | DST 可用，但不覆盖夜盘 |

新增 P0/P1 风险：

- P0：`da_bridge_dingtalk_push.py` 冬令时后会早于 DA main 执行，可能断链且无告警。
- P0：`deploy_freshness_check.py` 安全窗在冬令时后与夜盘重叠。
- P0：`freeze_sentinel.py` 只在 `MarketPhase.TRADING` 处置，夜盘心跳停滞不会自愈。
- P0：周日 2026-12-06 首夜，现有 weekend 判定和周一至五 cron 会直接漏触发。
- P1：tripwire、业务心跳、error alert 均未覆盖夜盘。
- P1：单挂任务如 macro brief、true reverse shadow 在冬令时后时序漂移，需逐个审计。

### 7.3 交易安全视角

- 可以融合的是扫描、会话识别、调度、配置与影子记录；
- 不能直接融合为 23 小时自动交易主管线；
- Playbook 必须继续唯一负责下单、出场与持仓管理；
- 夜盘初期必须 scan-only/shadow：零订单、零生产交易写入、零真实资金影响；
- Playbook 当前下单语义仍偏 RTH + `DAY`，夜盘执行须单独定义 session、订单类型、TIF 与券商能力；
- 现有 quote age 不可得时存在 fail-open 风险，不能支撑夜盘执行；
- 实盘启用前必须用户明确签署，并预登记标的范围、名义本金、频率、订单类型、日损上限和 kill switch。

## 8. 测试与验收矩阵

落地前必须至少包含以下契约测试：

1. **常规边界半开区间**：04:00、09:30、16:00、20:00、21:00、次日 04:00。
2. **半日市**：2026-11-27 12:59 为 regular，13:00 起 after_hours。
3. **DST**：2026-11-01、2027-03-14，EDT/EST 转换不影响 ET session 判定，不依赖宿主时区。
4. **跨午夜**：周日 21:00 与周一 03:59 为 overnight_trading，`trade_date=周一`；04:00 转 pre_market。
5. **假日**：当日、跨午夜归属日、周日夜盘遇次日假日均显式判定。
6. **scan_loop**：只禁用/降频具体 job，心跳、复盘、快照、财报刷新不被整体跳过。
7. **调度幂等**：同一 `session_id+scene+job_id` 双触发只执行一次。
8. **可观测性**：每个预期任务都有 planned/started/completed/skipped/error 字段。
9. **兼容映射**：overnight_trading 初期不改变旧 API 输出，不开启执行。
10. **执行 fail-closed**：日历降级、quote age 缺失、深度缺失、券商能力未知时不得进入执行。

## 9. 分阶段落地路线（待裁决）

### Phase 0：事实与治理确认（无行为变更）

- [ ] 确认交易所 primary source：23 小时边界、维护窗、日期、venue、标的、节假日规则。
- [ ] 全量登记 cron/systemd/Hermes/git hook/repo import 消费者。
- [ ] 产出券商与数据源能力矩阵。
- [ ] 明确旧 cron/Hermes 备份与回滚剧本。

### Phase 1：SessionResolver 契约与影子

- [ ] 实现 resolver 单测，不改旧调用方。
- [ ] 输出与 `scheduler_triggers`、`timezone`、`market_hours`、extended gate 的 parity log。
- [ ] 连续多日差异为零或差异均有解释。

### Phase 2：Dispatcher 影子运行

- [ ] 只计划，不执行任务；与旧 cron 对照。
- [ ] 验证 planned/actual、漏跑、重复、漂移。
- [ ] 修复 DA bridge 等已发现 DST 漏项。

### Phase 3：批处理任务逐项迁移

- [ ] 先迁移低风险 scan/confirm/observation。
- [ ] 保留旧 cron 回滚路径。
- [ ] 使用锁与幂等避免双跑。

### Phase 4：夜盘 scan-only/shadow

- [ ] 启用 overnight observation，不开启执行。
- [ ] 验证数据覆盖率、quote age、延迟、调度漂移、告警覆盖。
- [ ] 验收：零订单、零生产交易写入、旧行为不变。

### Phase 5：Playbook SIM-only（另行裁决）

- [ ] 仅通过 Playbook 正常 entry/exit 管线接入。
- [ ] 完成订单生命周期、撤改单、成交、对账、恢复演练。
- [ ] 数据与券商能力全 fail-closed。

### Phase 6：窄范围 live pilot（另行签署）

- [ ] 用户明确批准。
- [ ] 极窄标的、低名义本金、低频、限价、显式夜盘 session、独立配额与日损上限。
- [ ] 任何能力缺口、对账差异或回滚不可达立即降回 shadow。

## 10. 回滚设计

按顺序执行：

1. 禁用 dispatcher / overnight scene kill switch；
2. 停止新的夜盘扫描；
3. 如未来已进入执行阶段，先停止新订单；
4. 撤销或对账 in-flight 订单；
5. 恢复原 cron/Hermes 备份；
6. 恢复 legacy market profile；
7. 保留影子数据与审计记录，供复盘。

代码级回滚只在配置/调度开关失效时使用。23 小时交易下重启必须先确认无未平仓/在途出场/在途订单，并在维护空档执行；重启后必须跑 startup smoke。

## 11. 待用户裁决

1. 是否采纳方案 B（SessionResolver + dispatcher + 分阶段迁移）？
2. 是否批准 Phase 0/1 只读与契约测试实现？
3. 是否批准先修复 2026-11-01 前已确认的 DST P0 漏项？
4. 是否确认夜盘初期严格 scan-only/shadow，执行层另行裁决？

## 12. 讨论记录

### 第 1 轮：多 Agent 只读复查（2026-10-09）

- **架构 Agent**：可以融合，但需新建 canonical SessionResolver；不要重载 `OVERNIGHT`；所有调用方按兼容 adapter 迁移；必须注入 clock/calendar 并覆盖跨午夜与半日市。
- **运维 Agent**：UE/DA 主链 DST 双挂基本正确；发现 DA bridge、部署安全窗、freeze sentinel、周日首夜四个 P0；dispatcher 应先影子运行并用锁防双跑。
- **交易安全 Agent**：只融合扫描与调度，不融合交易主管线；夜盘先 scan-only/shadow；Playbook 唯一执行；执行前必须完成券商/数据源能力矩阵和 fail-closed 硬门。

### 第 2 轮：主线程综合

- 采纳三路共同结论：不一次性改 `OVERNIGHT`，不做大爆炸 cron 迁移，不开启夜盘执行。
- 将交易所边界标记为“待官方确认”，避免把用户输入直接固化为生产事实。
- 将 12/6 设为 profile 生效候选日，仅在 Phase 0 primary source 确认后启用。

## 13. 第二 Agent 独立复查（Hermes · 2026-10-09 · 只读实测）

> 复查人：Hermes。方法：不信任本文档的描述，对 §1.3 立项依据、§7.2 四个 P0、§8 日期事实逐条活体实测。
> 探针已入库：`scripts/deb20261009_session_caliber_probe.py`（只读，不触发 OpenD，时钟/日历均注入）。
> 本节**只追加**，§1–§12 正文与判据逐字未动（含已被本节推翻的 §1.3 表述，改法见下）。

### 13.1 结论：安全边界全部认可，但 §1.3 立项依据被实测推翻 ⇒ 方案 B 的第一块（SessionResolver）应先做减法

| 维度 | 判定 |
|---|---|
| §7.3 交易安全边界（Playbook 唯一执行 / 夜盘先 scan-only / 执行层另行签署） | ✅ **完全认可，无需改动**，这是本 DEB 最有价值的部分 |
| §7.2 四个 P0 | ✅ **全部实测为真**，且 P0-1/P0-4 比原文更严重（见 13.2） |
| §1.3「五处重复口径存在边界差异」 | ❌ **被推翻**：真边界差异 **0** 处；五处中 **2 处零消费方死代码** |
| §6.1 新建 `SessionResolver` + `MarketSessionProfile` | ⚠️ **前提不成立**：不是"六处口径打架需要第七个统一件"，而是"一处活跃 SSOT + 两处死代码" |
| §9 Phase 1（resolver 契约与 parity log） | ⚠️ 建议**缩范围**：parity 对象从 4 个降到 2 个活口径；先删死代码再谈新件 |
| §8 验收矩阵日期事实 | ✅ 四个日期全部正确（2026-12-06 确为周日） |
| §1.1 夜盘边界「待官方 primary source」 | ✅ 认可；实测仓内确无 2026-12-06 / 23 小时的任何 primary source（`docs/` `config/` grep 命中仅历史文档里"夜盘"作为**既有渠道名**，非交易所公告） |

### 13.2 四个 P0 的实测复核（提案说对了，我补精确读数）

| P0 | 提案声称 | 实测 | 判定 |
|---|---|---|---|
| P0-1 | `da_bridge_dingtalk_push.py` 冬令时后早于 DA main，可能断链且无告警 | crontab **单挂** `55 20 * * 1-5`；夏令时 = ET 08:55、**冬令时 = ET 07:55**；而 da-feed 冬季有效那发是 `00 21` CST = ET 08:00 ⇒ **bridge 比 feed 早 5 分钟** | ✅ 真，且**余量仅 5 分钟**（不是"可能"，是必然先跑） |
| P0-2 | `deploy_freshness_check.py` 安全窗冬令时后与夜盘重叠 | 安全窗 = 北京 09:00–12:00 CST；夏令时→ET 21:00–00:00、冬令时→**ET 20:00–23:00**；夜盘 21:00–04:00 ET ⇒ 重叠 **ET 21:00–23:00（2h）** | ✅ 真，但**截止日应更正为 2026-12-06**（见 13.3） |
| P0-3 | `freeze_sentinel.py` 只在 `MarketPhase.TRADING` 处置 | `scripts/freeze_sentinel.py:81` `return get_market_phase() == MarketPhase.TRADING`（全文件恰 **1** 处），docstring:18 自述「相位 ≠ TRADING → 不处置」 | ✅ 真；夜盘心跳停滞不会自愈 |
| P0-4 | 周日 2026-12-06 首夜，weekend 判定与周一至五 cron 会漏触发 | crontab 有效 **122** 行；按星期字段：`dow=*` 12 行 + `dow=0` 5 行 + `dow=6,0` 1 行 = 18 行周日会跑；**104 行周日不触发**（`1-5` 30 行、`2-6` 60 行、`1` 3 行、`6` 11 行） | ✅ 真，且规模比原文大：**104/122 行漏触发** |

### 13.3 P0-2 的截止日更正（影响 §11 待裁项 3 的修复范围）

提案把 P0-2 归入「2026-11-01 前已确认的 DST P0 漏项」。实测**冬令时单独不制造危险**：

- 冬令时安全窗 = ET 20:00–23:00，而现行 20:00–04:00 ET 是 `OVERNIGHT` 深度休眠（`scheduler_triggers.py:62` 注释、`:75` 分支实证）⇒ **重启不打断任何在途**，脚本自述的「≥9h 缓冲」在冬令时下依然成立。
- 危险只来自**夜盘生效**：ET 21:00 起成为交易时段，安全窗尾段 21:00–23:00 落入交易中 ⇒ 真截止日是 **2026-12-06**，不是 11-01。

**含义**：§11 待裁项 3「是否批准先修复 2026-11-01 前已确认的 DST P0 漏项」的清单应**只含 P0-1**（5 分钟余量断链，冬令时即触发）；P0-2 归 Phase 4（夜盘 scan-only）前置，P0-3/P0-4 归 Phase 4 前置或夜盘专项。混在一起会让 11 月前那批修复背上不必要的范围。

### 13.4 §1.3 被推翻的实测证据

探针 B 段用注入时钟对拍 `timezone.get_market_session()` 与 `scheduler_triggers.get_market_phase()`，覆盖 03:59/04:00/09:29/09:30/12:59/13:00/15:59/16:00/19:59/20:00/23:00 共 11 个边界时刻，日期取**半日市当天 2026-11-27**（13:00 ET 收）：

| 结果 | 读数 |
|---|---|
| 真边界差异 | **0 处**（`closed` vs `overnight` 是命名折叠，语义同为不可交易，归一后一致） |
| 半日市感知 | **两处都已处理**：12:59 双方判 regular/trading 且 `is_market_open=True`；13:00 双方判 after_hours 且 `is_market_open=False` |
| 唯一真差异 | `timezone.is_after_hours` 是**闭区间** `16.0 <= x <= 20.0`（`app/utils/timezone.py:211`），20:00 整同时被判「是盘后」+ `get_market_session()='closed'` ⇒ 同模块自相矛盾 **1 个时刻** |
| 但该差异**无害** | `is_after_hours` 与 `is_pre_market` 的真实调用方 = **0**（import 级 grep 全仓含 `ops/` `tests/` `~/.hermes/scripts/`，命中仅本探针自身，已在探针内自指排除）⇒ 是死函数 |

消费方普查（探针 A 段，已排除定义处自身与本探针）：

| 口径 | 调用点 | 文件 | 判定 |
|---|---|---|---|
| `scheduler_triggers.get_market_phase` | 55 | 29 | 活跃（**事实 SSOT**） |
| `scheduler_triggers.MarketPhase` | 113 | 24 | 活跃 |
| `timezone.is_market_open` | 21 | 10 | 活跃（薄包装） |
| `timezone.get_market_session` | 1 | 1 | 活跃（单点） |
| `timezone.is_pre_market` | **0** | **0** | 💀 死 |
| `timezone.is_after_hours` | **0** | **0** | 💀 死（含上述闭区间矛盾） |
| `shared.market_hours` | 18 | 10 | 活跃（**港股**分支为主；其美国侧覆盖分支源码自述「恒不可达」已删，见 `market_hours.py:126-133`） |
| `scripts.extended_hours_market_session` | 12 | 10 | 活跃（ETH 专用窄口径） |

三套活跃口径的消费方**几乎不重叠**（探针实测交集）：`extended_hours_market_session ∩ get_market_phase` = **0** 文件、`extended_hours_market_session ∩ market_hours` = **0** 文件、`market_hours ∩ get_market_phase` = **2** 文件（`app/data/subscription_manager.py` + 其测试）。三套同用 = **0** 文件。

⇒ **它们是分层而非并列竞争**：主调度口径（get_market_phase）、订阅/HK 口径（market_hours）、ETH 执行窄口径（extended_hours_market_session）各管一层，真并行冲突面只有 1 个生产文件。「五处重复口径互相打架」这个立项叙事不成立。

### 13.5 由此产生的方案修正建议（供裁决，不改本文档 §5/§6 原文）

**建议 B′：先减法，后新件。** 顺序反过来，成本差一个量级：

1. **删两处死代码**（`timezone.is_pre_market` / `is_after_hours`，零消费方 ⇒ 行为逐字不变；顺带消灭 20:00 闭区间矛盾）。这是本 DEB 里唯一"零风险、立即减少口径数"的动作。
2. **Phase 1 的 parity log 对象从 4 个降到 2 个**（`get_market_phase` + `extended_hours_market_session`；`market_hours` 只在 subscription_manager 一处交叉，单独记一笔即可）。B 段实测已给出 parity 基线：**当前 11 个边界时刻零差异** ⇒ Phase 1 的验收不是"发现并解释差异"，而是"夜盘 profile 引入后**仍**零差异"，判据更硬。
3. **`SessionResolver` 降级为「夜盘 profile 加载器」**：不需要 canonical 重写五层语义，只需要在既有 `get_market_phase` 之上加一个 `overnight_trading` 分支 + profile 开关 + `trade_date` 归属。新建第六个 resolver 的风险是**它自己成为第七处口径**（提案 §6.3 兼容映射表就是在为此上保险，但保险成本 = 长期双轨）。
4. **P0-1 单独提前修**（不等 Phase 1）：`da_bridge` 加 21:55 CST 第二挂或改为 ET 守卫驱动，与既有 da-feed 双挂同构（`llm_universe_scan.py:932-933` 注释已写明双挂原理：恒差 1h > 窗宽 58min）。
5. **Phase 4 前置补 P0-2/P0-3/P0-4**：安全窗按 profile 让开 ET 21:00–23:00；`freeze_sentinel` 相位门加夜盘分支；周日首夜的 cron `dow` 覆盖单独立项（104 行漏触发不可能靠逐行改，需要 dispatcher 或显式 `dow` 补丁清单）。

**与 DEB-20260918 的关系（必须写清，否则是第二套账本）**：该 DEB 规划的 `trade_session_policy` SSOT **全仓 grep = 0 命中**（`app/ scripts/ config/ tests/ ops/`），即从未落地，其 Phase 1 复选框仍是 `- [ ]`。⇒ 本 DEB 的 `MarketSessionProfile` **不与已落地的 SSOT 冲突**，但**与 DEB-20260918 §5.1 的设计意图重叠**。建议二选一：要么本 DEB 承接 20260918 的 `trade_session_policy`（同一份配置、同一批落地），要么在 20260918 里显式标注该节由本 DEB 接管。不处理会留下两份"会话策略 SSOT"设计稿。

### 13.6 E 表（可复算证据，命令逐条可跑）

| # | 命令 | 期望输出（程序化实跑提取，非手抄） |
|---|---|---|
| E1 | `venv/bin/python scripts/deb20261009_session_caliber_probe.py` | `真边界差异           : 0 处` |
| E2 | 同上 | `零消费方死口径       : 2 处 → timezone.is_pre_market, timezone.is_after_hours` |
| E3 | 同上 | `活跃口径 SSOT        : get_market_phase (调用点 55 / 文件 29)` |
| E4 | 同上 | `crontab 有效行数 = 122` 与 `周日不触发的 cron 行数 = 104` |
| E5 | 同上 | `2026-12-06 = 周日` |
| E6 | 同上 | `重叠 = ET 21:00 - 23:00  (2h)` |
| E7 | `crontab -l \| grep -cE "^[0-9*].*da_bridge_dingtalk_push"` | `1` |
| E8 | `grep -cE "get_market_phase\(\) == MarketPhase.TRADING" scripts/freeze_sentinel.py` | `1` |
| E9 | `grep -rn "trade_session_policy" app/ scripts/ config/ tests/ ops/ \| grep -v pycache \| wc -l` | `0` |
| E10 | `grep -n "16.0 <= hour_min <= 20.0" app/utils/timezone.py` | `211:    return 16.0 <= hour_min <= 20.0` |
| E11 | `grep -n "OVERNIGHT = " app/core/scheduler_triggers.py` | `OVERNIGHT = "overnight"       # 深夜休市 (20:00-04:00 ET, 工作日)` |

### 13.7 复查自曝（本轮我自己犯的错，登记防后人采信）

1. **首次对拍全 DIFF = 我的假阳**：`get_market_phase()` 实测**无参**（签名 `() -> MarketPhase`），我却传入时刻参数 ⇒ 参数被忽略，11 个时刻全部返回"当前真实时刻"的相位，得出"14 处差异"。这是 `review-system-design` 记过的「错相位陈旧=假阳」坑的又一次实例。**修正方式**：注入模块时钟而非传参。若未自查，我会把"零差异"写成"处处差异"，方向完全相反。
2. **第二次对拍仍假阳（大小写）**：归一映射表用了大写枚举名而 `.value` 是小写 ⇒ 14 处 DIFF 里 10 处是我的 bug。修正后才得到真值 0。
3. **探针自指**：探针为做 C 段对拍自己调用了 `is_after_hours`，A 段普查把它计成"生产消费方"，导致死函数被判成活跃（首跑汇总显示"零消费方死口径 = 1 处"，与精确 grep 的 2 处矛盾）。已在探针内显式排除自身文件，重跑得 2 处 ⇒ 与 import 级 grep 一致。
4. **awk 解析 crontab 丢行**：我用 awk 提取字段时输出被截断，一度误判"da-feed 无双挂"，进而怀疑 §7.2 主链表有误。改用原始行 `cut -c1-125` 复核后确认**提案是对的**（`00 20` + `00 21` 双挂确实存在）。这条差点让我把一个正确结论报成错误。

> 上述 4 条全部是"我的测量错"而非"提案错"。提案 §7.2 运维节的每一条声称，在我正确测量后**全部成立**。

## 14. 第三轮独立复核与裁决建议（主线程 · 2026-10-09 · 只读实测复核）

> 复核人：主线程（Claude）。方法：对 §13 E 表逐条重跑 + 对 §13.5 修复方案做前向验证（"不轻信复查者自身结论"同样适用于复查者）。
> 本节**只追加**，§1–§13 正文逐字未动。

### 14.1 §13 E 表重跑结果（全部命中）

| 重跑项 | 读数 | 判定 |
|---|---|---|
| 探针全量重跑（E1–E6） | 真边界差异 0 处；死口径 2 处；SSOT = get_market_phase（55 调用点 / 29 文件）；crontab 122 行、周日漏触发 104 行；2026-12-06 = 周日；P0-2 重叠 = ET 21:00–23:00 | ✅ 与 E 表一致 |
| E7 da_bridge 单挂 | `55 20 * * 1-5`，全表恰 1 行 | ✅ |
| E8 freeze_sentinel 相位门 | `get_market_phase() == MarketPhase.TRADING` 恰 1 处 | ✅ |
| E9 trade_session_policy 全仓 grep | 0 命中（含 ops/ tests/） | ✅ |
| E10 timezone.py:211 闭区间 | `return 16.0 <= hour_min <= 20.0` 原文命中 | ✅ |

§13 结论（含 13.7 四条自曝）经独立重跑后**全部认可**。

### 14.2 新增实证点（§13 未覆盖，均只读实测）

| # | 发现 | 证据 | 影响 |
|---|---|---|---|
| N1 | `get_market_phase()` **无参**（`def get_market_phase() -> MarketPhase`，内部 `datetime.now(ET)`） | scheduler_triggers.py:65-66 | §13.7-1 假阳的根源坐实。B′ 落地第一步必须是 clock 注入重构（无参默认 = `now(ET)` ⇒ 55 个调用点零迁移），契约测试先行 |
| N2 | `MarketPhase.AFTER_HOURS` docstring 已占中文名"**盘后夜盘** (16:00-20:00 ET)" | scheduler_triggers.py:61 | 12/6 真夜盘进来后"夜盘"一词两义，日志/告警/文档必混。建议：新 session 中文固定为"**隔夜盘**"，枚举 `overnight_trading` 不变；profile 落地时同步把 AFTER_HOURS 注释改为"盘后" |
| N3 | DEB-20260918 Phase 1 复选框 `- [ ] 新增 trade_session_policy` 至今未勾 | DEB-20260918:387-389 | §13.5"双账本"风险坐实：该 SSOT 设计稿自立项起 3 周未动。本 DEB 裁决时必须显式二选一（本 DEB 承接同槽位，或在 20260918 标注接管），否则两份"会话策略 SSOT"设计稿并存 |
| N4 | da_bridge 有新鲜度守卫但**无 push-once 幂等** | da_bridge_dingtalk_push.py:313-340：文件存在且当日 ⇒ 每次调用都推；`_append_summary_record(...,"pushed")`（:338）只写不读 | §13.5-4"加 21:55 CST 第二挂"配方**不完整**：直接双挂 ⇒ 夏令时 08:55/09:55 ET 两发都过新鲜度守卫 ⇒ 每日双推钉钉（新故障）。正确配方 = **三件套**：① 脚本内 ET 窗口守卫（候选 08:50–09:25 ET）② push-once 幂等（推前查当日 summary 记录）③ `55 21 * * 1-5` 第二挂 |
| N5 | da_bridge skip 路径只 `print` 无任何告警；全仓 grep 无任何 watchdog 监控该推送 | skip 分支仅 print（:315-324）；grep `da_bridge` 命中全为下游数据消费方（playbook 归因 playbook_service.py:527、仪表盘徽标 battle_plan.html:421），无一监控推送本身 | P0-1 真实爆炸半径比 §13.2 更大：**11-01 起整个冬令时（~4.5 个月）每个交易日盘前钉钉摘要静默缺失**，唯一信号是"没人收到"。P0-1 从"排序错位"升级为"静默失效"；修复紧迫性不变（今天距 11-01 仅 23 天），但验收必须含"skip 可观测"（至少落一条 expected-but-skipped 记录） |

### 14.3 裁决建议（供用户裁决，不预设结论）

1. **采纳 B′（先减法后新件）**，但补一条 §13 未写明的边界：**降级的是实现位置，不是返回契约**。§6.1 返回字段（尤其 `trade_date` / `profile_version`——§6.4 dispatcher 的 `session_id+scene+job_id` 幂等键依赖它）不能随 resolver 降级一起砍。B′ 落地形态 = 在既有 `get_market_phase` 上扩 `overnight_trading` 分支 + clock 注入 + profile 开关；§6.3 兼容映射表随之从"保险"降级为"不需要"（没有第六个口径，就无需兼容它）。
2. **P0-1 立即单独成批修复**，不等本 DEB 整体裁决（23 天窗口，成本极低）。三件套缺一不可：只加第二挂 → 夏季双推；只加守卫不加第二挂 → 冬季单挂 07:55 ET 在窗外永远被拒 ⇒ 断链照旧。回滚 = 删第二挂 + 关守卫开关。
3. **§11 待裁项按 §13.3 重排**（裁决时以本表替代 §11 阅读）：
   - 项 1（方案 B）→ 改裁"方案 B′（§13.5 + 本节 14.3-1 边界）"；
   - 项 3（11-01 前 DST P0 清单）→ **只含 P0-1**；P0-2 真截止日 2026-12-06，归 Phase 4 前置；P0-3/P0-4 归夜盘专项；
   - 新增项 5：`trade_session_policy` 承接关系（N3：承接 DEB-20260918 同槽位，或在 20260918 显式标注本 DEB 接管）；
   - 新增项 6：批准删除 `timezone.is_pre_market` / `is_after_hours` 两处死码（零消费方，行为逐字不变，顺带消灭 20:00 闭区间自相矛盾）。
4. **P0-4 修复范围收窄**：104 行周日漏触发 ≠ 104 行都要补。绝大多数 cron 任务在周日夜盘**本无业务义务**（盘前 scan/confirm/score 均锚定常规交易日）。正确顺序：先列"夜盘/周日有业务义务"任务清单（observation 探针、心跳、监控类，预计 <10 项），仅清单内任务进 dispatcher 或补 dow ⇒ 进一步支持声明式 dispatcher 而非逐行补丁。
5. **实现注意点（供 Phase 1 承接者）**：现行 `get_market_phase` 第一道分支是 weekend/holiday ⇒ 周日 21:00 永远先落 `WEEKEND`。夜盘 profile 的 `overnight_trading` 判定必须插入 weekend 判定**之前**（或与日历判定合并改写），否则跨午夜首夜死锁在 WEEKEND。这是 §6.2 跨午夜语义落到具体代码的第一个分叉口。

### 14.4 与 §13 的分歧登记

- **无实质分歧**。
- 一处补强（非分歧）：§13.5-4 的 P0-1 修复配方缺 push-once（N4）。按原配方直接落地会引入"夏令时每日双推"新故障 ⇒ 修复包必须含 N4 三件套，否则不如不修。

## 15. P0-1 执行记录（2026-10-09 · 用户裁“按建议执行”）

> 范围：只修 `da_bridge` 冬令时断链；不落地 SessionResolver、不开启夜盘、不改 Playbook、不改交易行为。

### 15.1 已落地

1. `scripts/da_bridge_dingtalk_push.py`
   - 新增 ET 推送窗 `08:50–09:25`（09:25 含边界）。
   - 新增当日 `pushed` 幂等读取；`push_failed` 不拦截下一挂重试。
   - 新增 skip 台账：`outside_window`、`bridge_missing`、`bridge_parse_failed`、`trade_date_mismatch`、`already_pushed`。
   - 新增 `--force-window` 仅供人工补跑/诊断；生产 cron 不使用。
2. `tests/test_da_bridge_dingtalk_push.py`
   - 新增窗口边界、窗外 skip 留痕、缺文件 skip 留痕、当日成功只推一次 4 组契约。
3. crontab
   - 保留夏令时触发：`55 20 * * 1-5`。
   - 新增冬令时触发：`55 21 * * 1-5`。
   - 备份：`data/crontab.backup.20261009_da_bridge_p0.txt`。

### 15.2 回滚

1. 删除 crontab 中 `55 21 * * 1-5` 的 `da_bridge_dingtalk_push.py` 行，或直接从备份恢复。
2. Revert `scripts/da_bridge_dingtalk_push.py` 与 `tests/test_da_bridge_dingtalk_push.py`。
3. 本批为 cron 脚本变更，`ftseek-us` 服务不需要重启；下一 cron tick 直接使用新脚本。

### 15.3 验证

- [x] `PYTHONPATH=. venv/bin/python -m pytest tests/test_da_bridge_dingtalk_push.py -q`
- [x] `PYTHONPATH=. venv/bin/python -m pytest tests/test_da_bridge_dingtalk_push.py tests/test_r8_timezone_contracts.py -q`
- [x] `PYTHONPATH=. venv/bin/python -m py_compile scripts/da_bridge_dingtalk_push.py`
- [x] `git diff --check`
- [x] crontab diff：仅新增一行 `55 21 * * 1-5 ... da_bridge_dingtalk_push.py`
- [x] `scripts/verify_deb20261009.py` 已识别 §15 后当前态：crontab 有效行 `123`、周日漏触发 `105`、`da_bridge` 行数 `2`
