# FTSeek-US 全链路项目画像（2026-10-04）

> 口径：全部实读代码 / 配置 / DB / crontab / systemd / Hermes，不引用记忆。
> 用途：给"整个链路"一张总图 — 策略、逻辑、数据流、执行、风控、运维、治理。
> 事实基线：`AUTO_TRADE_ACCOUNT=sim`，`ALLOW_REAL_TRADING=false`，Playbook 为唯一下单管线。

---

## 0. 一句话定位

**FTSeek-US 是一个"证据优先"的美股交易系统，现役 SIM、LIVE 在备**：当前不追求盈利
（毛期望=0 已证），而是把每一笔交易当成一次受控实验 — 预注册判据 → 单版本功效窗 →
影子对照 → 日级不变量自检 → OKR 结算。**Playbook 是 SIM 与 LIVE 的唯一交界线**：
SIM 主车道现役成交，LIVE C-lite 工程探针臂已结构就绪但被四锁 + live_arm_gate
fail-close 关闭（详见 §5.3）。技术形态 = Python 单机常驻进程 + Futu OpenD 券商网关 +
SQLite 数据湖 + cron/Hermes 双调度 + 钉钉/飞书推送。

---

## 1. 系统形态

### 1.1 运行载体

| 载体 | 职责 | 关键入口 |
|---|---|---|
| systemd `ftseek-us` | 主交易常驻进程（自愈 20次/600s） | `main.py --start` via `start.sh` |
| crontab | ~80 条业务 cron（采集/结算/报告/守卫） | `crontab -l` |
| Hermes | LLM 类定时任务（DA 扫描、复盘、复盘判决书等） | `/root/.hermes/cron/jobs.json` |
| Futu OpenD | 券商 + 行情网关（sim/real 双 ctx） | `127.0.0.1:11111` |
| Web Dashboard | 人工看板 + 手动单（恒 REAL 路由） | `app/web_dashboard/` |

### 1.2 主进程装配链

```
systemd → start.sh → main.py:main()
  → _run_new_scheduler()
     ├─ fcntl 双实例锁（/run/ftseek-us.lock, fail-closed）
     ├─ _validate_startup_mode_or_exit()   # 启动模式硬校验
     ├─ OpenD 预检（socket 5s）
     └─ DIContainer.build_scheduler()
          ├─ EventStore (SQLite)
          ├─ ExecutionEngine (Futu)
          ├─ RiskEngine
          ├─ HubTextAdapter(DingTalk)      # 通知收口进 PushHub
          └─ TradingScheduler
               ├─ 恢复持仓 _restore_positions_from_db()
               ├─ startup_reconcile()       # 内存↔DB 双向对账
               └─ 启动 9 条后台线程（见 §2）
```

---

## 2. 核心链路总图

```
                         ┌──────────────────────────────────────────────┐
                         │              数据与情报层                      │
                         │ Futu L1/L3 · 十二数据 · Finnhub · AV          │
                         │ 新闻V2(退役) · 内部人Form4 · 财报日历 · VIX    │
                         └──────────────┬───────────────────────────────┘
                                        │
              ┌─────────────────────────┼──────────────────────────┐
              │                         │                          │
              ▼                         ▼                          ▼
     ┌────────────────┐       ┌──────────────────┐       ┌──────────────────┐
     │  盘前决策生成  │       │  UE 池扩展(独立) │       │  延长时段通道    │
     │  DA/Hermes     │       │  llm_universe_scan│       │  pre/after hours │
     │  → bridge json │       │  → 直写指令表    │       │  (独立脚本/cron) │
     └───────┬────────┘       └────────┬─────────┘       └────────┬─────────┘
             │                         │                          │
             └────────────┬────────────┘                          │
                          ▼                                       │
              ┌──────────────────────────┐                        │
              │  playbook_instructions   │◀──── intraday_reentry │
              │  (SQLite, SSOT 指令表)   │       (盘中再武装)     │
              └────────────┬─────────────┘                        │
                           ▼                                      │
              ┌──────────────────────────┐                        │
              │  run_playbook_auto_execute│                        │
              │  每 180s 一轮 (盘中)     │                        │
              │  ~30 道入场门 + 风控      │                        │
              └────────────┬─────────────┘                        │
                           ▼                                      │
              ┌──────────────────────────┐       ┌────────────────────────┐
              │  PlaybookService          │       │ extended_hours_entry   │
              │  .execute_instruction()   │       │ _executor (cron */5)   │
              │  → ManualOrderService     │       │ 只做多·当日财报票      │
              │  → DualBrokerAdapter      │       │ → Futu SIM 直接下单    │
              │  → Futu OpenD (SIM)       │       └───────────┬────────────┘
              │   └→ LIVE mirror (关)     │                    │
              └────────────┬─────────────┘                   │
                           ▼                                   │
              ┌──────────────────────────┐                    │
              │  trade_records (成交台账) │◀──────────────────┘
              └────────────┬─────────────┘
                           ▼
              ┌──────────────────────────────────────────────┐
              │  出场双宿主                                  │
              │  A. PlaybookExitFastLoop (10s) → exit_engine │
              │  B. extended_hours_exit_cron (每分钟)         │
              │  → 平仓 → trade_records 结算 → 路径湖/影子    │
              └────────────┬─────────────────────────────────┘
                           ▼
              ┌──────────────────────────────────────────────┐
              │  证据回路 (盘后)                              │
              │ IC回填 · 影子结算 · 不变量自检 · 复盘 · OKR    │
              └──────────────────────────────────────────────┘
```

### 2.1 主进程 9 条后台线程（`scheduler_core.run_forever`）

| 线程 | 循环 | 现状 |
|---|---|---|
| SignalScanLoop | `signal_pipeline.scan_loop`（5min） | LIVE 但**只扫描不下单**（双重冻结） |
| PositionMonitorLoop | `position_monitor.monitor_loop`（15s） | LIVE（管遗留/手动仓） |
| LLMStrategyLoop | 按市场相位分发（盘前生成/盘中执行/盘后复盘） | LIVE |
| NewsV2Loop | `news_pipeline_v2` | 已退役（`news_v2.enabled=false`） |
| VIXMonitorLoop | VIX 告警 | 条件启动 |
| ManualGuardLoop | 手动仓风控 | LIVE |
| InsiderScanLoop | SEC Form4 日扫 | LIVE |
| PlaybookExitFastLoop | `monitor_playbook_exits`（10s 配置） | LIVE（出场主宿主） |
| SubscriptionManager | 分时订阅/退订 Futu 推送 | LIVE |

> SignalScanLoop 不可 gate 掉 — 它独家驱动日/周/月复盘、财报日历刷新、看门狗心跳。

---

## 3. 交易日时间轴（ET）

| 时段 | 时间 | 主链路 |
|---|---|---|
| 数据预热 | 04:00–08:00 | UE DA 供料早跑（CST 20:00=ET 08:00）· 内部人日报 · 板块轮动 |
| **盘前生成** | ~09:00 | Hermes DA 扫描 → `opportunity_scan_YYYYMMDD.json`（bridge）→ 指令入库 |
| **盘前入场**（独立通道） | 04:00–09:29 | `extended_hours_entry_executor --session premarket`（cron */5） |
| 开盘缓冲 | 09:30–09:35 | Playbook `auto_execute` 刻意跳过（开盘噪声） |
| **盘中执行** | 09:35–16:00 | `run_playbook_auto_execute` 每 180s · intraday_reentry 每 300s |
| **盘中出场** | 09:30–16:00 | `PlaybookExitFastLoop` 每 10s · EOD 前 8min PRE_CLOSE |
| **盘后入场**（独立通道） | 16:00–20:00 | `extended_hours_entry_executor --session afterhours` |
| **盘后证据** | 16:00–20:45 | 影子结算 · 路径湖采集 · 复盘 · 记忆更新 |
| **夜间治理** | 20:00–04:00 | cron 报告链 · 不变量自检（CST 05:05）· OKR 判分 |

---

## 4. 策略与决策逻辑

### 4.1 市场状态门（Layer 0）

`MarketRegimeGate.assess()` — 每日盘前评估一次（TTL 缓存）：

| 信号 | 评分 |
|---|---|
| SPY >/< MA20 | ±2 |
| MA20 >/< MA50 | ±2 |
| SPY >/< MA200 | ±1 |
| VIX <15 / 15-20 / 20-30 / >30 | +3 / +1 / −2 / −5 |

映射：`score≥5→RISK_ON(top 10, 1.0×)`；`0-4→CAUTIOUS(8, 0.5×)`；
`-5~-1→RISK_OFF(3, 0.3×)`；`<-5→NO_TRADE(0, 0×)`。VIX 不确定时 RISK_ON 降级 CAUTIOUS。

### 4.2 候选源（谁生产指令）

| 源 | 前缀 | 写入方式 | 方向 | 特征 |
|---|---|---|---|---|
| **DA（deep_analysis）** | `DA-` | Hermes → bridge json → 指令表 | BUY/SELL_SHORT | 主供源，LLM 逐单生成 SL/TP/confidence |
| **UE（universe_expansion）** | `universe_expansion` | cron 直写指令表 | 双向 | 池扩展 + 确认门影子，独立 strategy_id |
| **盘中再入场** | `intraday_reentry` | `intraday_reentry.run_scan` 直写 | 同向限制 | 当日非硬亏平仓 + VWAP 回踩 + slope 转正 |
| **延长时段** | `extended_hours_entry` | 独立脚本独立台账 | 只做多 | 当日财报票 + 温和涨 + 财报超预期 |
| 主管线投票 | 无前缀 | `run_playbook_generate` | — | bridge 缺失时 TechnicalFallback 兜底 |

指令表 = `playbook_instructions`（SQLite）。覆盖仲裁在 SQL 层原子完成：
DA-/UE 深度行拒绝 SKIP/TFB 票覆盖；陈旧 did 守卫拒绝 T-1 决策覆盖当日决策。

### 4.3 方向与参数逻辑（盘中通道）

- 方向：`direction_mode: llm_vote` — 由 DA/UE 按标的逐单给出 BUY / SELL_SHORT / SKIP。
- SL/TP：**DA 逐单生成**（非固定百分比），近 30 日 BUY 平均 SL −4.69% / TP +7.67%，
  SHORT −5.30% / +8.27%；TP 另有硬门 RR≥1.5。
- 置信度：分级（UE tier 门 / min_confidence 档位 0.45~0.7），且与仓位缩放联动。
- 仓位：regime 乘数 × capital_safety 水位 × 熔断观察期缩放 × 单名帽 0.08。

### 4.4 入场门（auto_execute，按序）

```
auto_execute 开关 → 交易日门 → 开盘 5min 缓冲 → bridge 三态告警
→ Kill Switch(文件哨兵, fail-close) → 风险状态预检(preflight_fail_mode)
→ 日亏熔断 → 预算总量检查(capital_safety, fail-close)
→ 同日硬止损冷却(同向拒重开) → B门(接飞刀 -5% 拒入场)
→ VWAP 方向硬门(容差 0.003) → chase_cap 禁追线(enforce=true)
→ 费用门(费用≤预期毛边际25%) → 报价健康门 → 报价新鲜度门
→ UE tier/confidence 门 → 单轮梯度帽(3) → 在仓帽 → 日帽(16)
→ PlaybookService.execute_instruction() 内 ~15 道执行级门
```

### 4.5 出场规则栈（exit_engine，10s 快循环）

```
[0]   开盘冷却 15min，SL 放宽至 4%
[0.5] 30min 无盈利 → SL 收紧至 1%
[1]   SL_INITIAL / SL_BREAKEVEN(0.4%) / SL_TRAIL_LOCK(峰值回撤 0.8%)
[1.5] TAKE_PROFIT（RR≥1.5）
[2]   OVERNIGHT_GAP_STOP（隔夜深亏快平，>3%）
[3]   PRE_CLOSE_EXIT（收盘前 8min）
[3.4] MFE_STALL（当前 disabled）
[4]   TIME_EXIT（>6h 且仅亏损时）
[5]   MAX_HOLD（default 1 日 / insider 5 日 / 趋势类 3 日）
```

因子感知：`_get_factor_exit_config` 按 conditions 匹配
`config/playbook_horizon.yaml::factor_exit_rules`（趋势类 trailing、快证伪类 profit_override）。

---

## 5. 执行链路（唯一管线）

### 5.1 Playbook 唯一下单铁律

两道硬锁冻结主管线：

1. **扫描侧**：`signal_pipeline.scan_loop` 硬编码 `_skip_execution_in_trading=True`
   （2026-10-04 实读复核仍在，消费点 `signal_pipeline.py:622` 命中即跳过组合下单）；
2. **执行侧**：`order_executor._submit_order` source_type 白名单只放行
   `PLAYBOOK_SIM / PLAYBOOK_LIVE`（2026-10-04 实读复核仍在，白名单外的候选
   source_type 一律拦截返回 None）。

第三层配置静默：`strategy.main_scan_enabled=false`（实测读取），扫描本体不发信号，
scan_loop 线程仍跑 housekeeping（复盘/财报刷新/watchdog 心跳）。

**复核结论（2026-10-04）**：双重冻结 + 配置静默三层全部在位 — 主管线**仍空扫描不下单**，
候选只做研究与 IC 采集。ORB 引擎已于 2026-09-20 整体退役删除（`app/orb/` 不存在，
最后成交 2026-06-22）。`order_executor`/`position_monitor` 的退出侧与共享设施仍服务
遗留仓与紧急平仓，不可删除。

### 5.2 下单调用链

```
run_playbook_auto_execute(scheduler)
  → PlaybookService.execute_instruction(instruction_id)
     → ManualOrderService facade
        → _execute_via_dual_broker(order, target_env)
           → DualBrokerAdapter.submit_order(order, env)
              → Futu OpenSecTradeContext.place_order
                 (sim: TrdEnv.SIMULATE, real: TrdEnv.REAL)
  → 成交回报 → trade_records 记账 → ManualHoldRegistry 注册
```

`DualBrokerAdapter` 双 ctx 双锁；real 解锁失败不阻断 sim（real_ctx=None，实盘 fail-close）。
durable exit ledger 跨重启续上在途出场。

### 5.3 SIM/LIVE 双臂：Playbook 是唯一交界线

LIVE 不是另起一条管线，而是**同一 Playbook 管线上的受控镜像臂**：

```
SIM 指令成交成功
  → _mirror_instruction_to_live_if_allowed(inst, live_db)
     → evaluate_live_arm_gate(...)   # 先判门，拒绝即不写 LIVE 行
     → 通过才 mirror 写入 ftseek_us_live.db（同 decision_id, qty=1）
     → PlaybookService.execute_instruction(target_env="real")
```

**当前实测状态（2026-10-04）**：

| 项 | 值 | 含义 |
|---|---|---|
| `playbook.live_manual_arm.enabled` | `false` | LIVE 臂未武装 |
| `probe.authorized` | `false` | 工程探针未授权 |
| `ALLOW_REAL_TRADING` | `false` | 环境级实盘闸关闭 |
| `AUTO_TRADE_ACCOUNT` | `sim` | 账户路由在 SIM |
| `ftseek_us_live.db::PLAYBOOK_LIVE` | 0 行 | LIVE 车道结构就绪、零自动成交 |
| `FTSEEK_LIVE_DEAL_PUSH` | `1` | LIVE 成交实时推送订阅在跑（record-only） |

LIVE 启动前置（就绪但关闭）：`REAL_ACCOUNT_ID` 已配置、live 库路由存在、
`DualBrokerAdapter` real ctx 解锁逻辑就绪、LIVE 启动断言要求
`risk.acb_fail_mode=closed` + `risk.preflight_fail_mode=closed`。放行仍需：
四锁解除 + `live_manual_arm.enabled=true` + probe 授权 + 毕业门禁 + 用户签字。

### 5.4 延长时段通道（独立于 Playbook）

| 维度 | 盘前 | 盘后 |
|---|---|---|
| 触发 | cron */5（CST 16-22） | cron */5（CST 4-8） |
| 人群 | 当日 bmo 财报票 ∪ 异动榜 | 当日 amc 财报票 ∪ 异动榜 |
| 方向 | 只做多 | 只做多 |
| 关键闸 | 财报超预期(epsActual>epsEstimate) · 价≥$5 · 市值≥$10亿 · 成交≥$1000万 · 涨幅带 0.5~2.0% · 量比·深度·价差 | 同左 |
| 出场 | 只止盈不止损 · 09:25 收口强平(仅当日仓) | 同左 + 持夜/次日 09:30-09:45 离场窗 |
| 台账 | trade_records (source_type=extended_hours_entry) | 同左 |

---

## 6. 风控与安全体系

### 6.1 六层熔断（正交，禁合一）

| 层 | 模块 | 管什么 | 触发 |
|---|---|---|---|
| L0 | `absolute_circuit_breaker` | 账户生死线 | 净值绝对回撤硬线 |
| L1 | `capital_safety` | 资金水位 GREEN→BLACKOUT | 回撤分档，仓位/预算乘数 |
| L1.5 | `account_circuit_breaker` | 递减式（REDUCE_50/HALT/LIQUIDATE） | 日亏分级 |
| L2 | `market_circuit_breaker` | 大盘异动（VIX/SPY 急变） | 市场级 |
| L3 | `circuit_breaker` | 策略连亏 + 数据源失败率 | 策略/数据源 |
| L4 | `drawdown_control` | 单仓回撤 | 单仓阈值 |

另有：`KILL_SWITCH.flag` 文件哨兵（人工急停，三处裸 regex 读磁盘是刻意设计，
绕过热重载 60s 延迟）；`live_arm_halt.json`（LIVE 新仓持久化 HALT）。

### 6.2 四锁（切实盘前零松动）

```
1. AUTO_TRADE_ACCOUNT=sim
2. ALLOW_REAL_TRADING=false
3. playbook.execute_mode=futu_simulate
4. 毕业门禁 fail-close (graduated=False)
```

LIVE arm 额外要求（`live_arm_gate.evaluate`）：arm.enabled + probe.authorized +
REAL_ACCOUNT_ID + live 库路由 + LONG/BUY only + max_order_qty=1 + max_open=1 + RTH。
任一不满足 fail-close。启动时 LIVE 模式还要求 `risk.acb_fail_mode` 与
`risk.preflight_fail_mode` 均 closed，否则拒绝启动。

### 6.3 资金水位

GREEN(0~-5%)=1.0× → YELLOW(-5~-10%)=0.5× → ORANGE(-10~-20%)=0.25× →
RED(-20~-35%)=0× → BLACKOUT(>-35%) 熔断锁仓。水位回升自动解锁。

---

## 7. 数据与证据回路

### 7.1 存储 SSOT

| 存储 | 粒度 | 写入方 | 消费方 |
|---|---|---|---|
| `playbook_instructions` | 指令/日 | DA bridge / UE / reentry | auto_execute · decision_view · scorecard |
| `trade_records` | 成交笔 | playbook_service | 一切判分/回放/看板 |
| `signal_snapshots.db` | 信号（含被 SKIP） | signal_ic_tracker | IC 回填 |
| `trade_attribution` | 笔 | 复盘归因 | 看板 |
| `data/position_paths/` | 1min bar 路径 | position_path_collector | 精确反事实回放 |
| JSONL 遥测 | 各守卫 | 各模块 | 影子裁决 |
| `data/shadow_registry.py` | 影子注册表 | 手工+脚本 | 影子生命周期 SSOT（69 项） |

串联键 = `decision_id`（instruction→trade→path 全链）。

### 7.2 盘后证据链（cron 时间序，CST）

```
05:05 daily_invariant_check      # W1/W2 棘轮: 退出哈希冻结 · 字段基数
05:15 position_path_collector    # 路径湖 1min bar
05:30 backfill_forward_returns   # IC T+N 前瞻收益
05:35 ic_health_check
05:40 factor_ic_backfill
05:42 condition_expectancy_report
05:50 pangan_daily_rebuild
05:55 unit_cell_daily_readout
06:05 da_chain_health_report
06:10 kr3_runup_shadow / early_dead_signal_report
06:15 shadow_board --push         # 影子组合每日结算
06:20 daily_llm_token_report
06:30 settle_extended_hours
06:44 short_supply_funnel_report
07:00 capital_flow_daily_aggregate
09:05 周六周检（四锁+参数冻结+证伪门）
```

### 7.3 治理文件体系

| 文档 | 作用 |
|---|---|
| `AGENT.md` | 运维/架构规矩（最高纪律） |
| `handoff.md` | 进度队列（交接真源） |
| `docs/specs/multi-agent-collaboration-charter.md` | 协作宪章（数据口径七律） |
| `docs/debates/` | DEB 决策档案 |
| `docs/adr/`、`docs/decisions/` | 架构/机制决议 |
| `docs/OKR-2026-10.md` | 当前 OKR |
| `docs/circuit-breaker-map.md` | 熔断分层地图 |
| `docs/data-map.md` | 数据资产索引 |

---

## 8. 可观测与运维

| 机制 | 位置 | 语义 |
|---|---|---|
| PushHub | `app/push/` | 所有新增推送必经（去重/聚合/健康监控），禁直连 notifier |
| Watchdog | `app/observability/watchdog.py` | 线程级 stale 检测，heal=整服重启 |
| freeze_sentinel | cron 每分钟 | `/run/ftseek-heartbeat` mtime + systemd reset-failed |
| startup_smoke_check | 重启后必跑 | 七条机器断言（ConfigValidator/漂移/SSOT/零 Traceback 等） |
| daily_invariant_check | 每日 CST 05:05 | W1 退出版本冻结 · W2 字段基数棘轮，违反即推钉钉 |
| error_alert.py | 每 15min 四窗 | 相对基线错误激增告警（走 push hub） |
| business_heartbeat | 每 30min | 业务侧静默失败检测 |
| restart 纪律 | `systemctl restart ftseek-us` | 禁 `python main.py --stop/--start`；盘中可重启 |

---

## 9. 当前治理状态（OKR-2026-10）

窗口：2026-09-17 → 2026-10-31，cohort 起点 = `exit-34c7153088a1`。

| KR | 判据 | 状态 |
|---|---|---|
| KR1 功效窗冻结 | oct cohort 退出哈希唯一值=1，攒 200 笔 | in_track（9/17→10/2 已 81 笔，预计 10/24 满额） |
| KR2 edge 判定 | 200 笔时出「净 bps/笔 ± MDE vs 费用线」三态结论 | 采集期 |
| KR3 SHORT 腿 | LONG 已干净证伪；SHORT n≥30 后重跑同脚本 | 采集期（n=24，预计 ~10/15） |
| KR4 实盘硬门 | 四锁零松动 + 毕业门禁 fail-close | 维持 NOT_MET（安全侧） |
| KR5 学费明码 | fee bps/笔 + 周学费外推 | 非门禁，继续公示 |

继承裁决：不切实盘、不设盈利目标、阈值预注册期间禁改、禁用仓位/频率追样本。
9 月三段哈希样本（de49/13f6/34c7）只作跨版本对照，不作单版本期望结论。

---

## 10. 实测画像（近 30 日 PLAYBOOK_SIM）

### 10.1 供给结构

近 7 日指令构成（10/2 为例）：BUY 7 / SELL_SHORT 1 / **SKIP 69** —
供给瓶颈在触发率与覆盖仲裁，不在日帽（16）。

### 10.2 成交与净额（近 14 交易日）

| 日期 | 笔数 | 净 PnL |
|---|---|---|
| 10/02 | 5 | +$70 |
| 10/01 | 6 | +$324 |
| 09/30 | 6 | +$115 |
| 09/29 | 11 | +$390 |
| 09/28 | 7 | −$268 |
| 09/25 | 6 | −$230 |
| 09/24 | 4 | +$46 |
| 09/23 | 9 | −$430 |

### 10.3 出场原因（近 30 日 top）

| 出场原因 | 笔数 | 占比 |
|---|---|---|
| SL_BREAKEVEN | 73 | 47.7% |
| SL_TRAIL_LOCK | 47 | 30.7% |
| SL_INITIAL | 9 | 5.9% |
| DEAD_SIGNAL_EXIT | 7 | 4.6% |
| OVERNIGHT_GAP_STOP | 5 | 3.3% |
| PRE_CLOSE_EXIT | 4 | 2.6% |

**画像结论**：系统行为高度集中在"早保本 + 回撤锁利"两档（合计 ~78%），
初始止损触发极少 — 出场纪律由 BE/TRAIL 主导，而非容忍深亏。
费用净期望近窗仍为负（9/30 参考 −2.89 bps/笔），与 OKR"毛期望=0、产出证据"定位一致。

---

## 11. 已知债务与雷区（建档，勿踩）

| # | 债务 | 影响 |
|---|---|---|
| D1 | god module：`pipeline.py` 9500+ 行 / `order_executor.py` 5000+ 行 | 维护成本高 |
| D2 | 指令写入方直写 playbook_instructions，判分在下单前很远 | 门序列脱节 |
| D3 | trailing_stop 双路径不一致（exit_policy vs horizon） | 勿改 fail-close |
| D4 | 三态（shadow/sim/live）无显式状态机，靠 env var + 约定 | 切态未演练 |
| D5 | sector_limits 未知标的放行 | pass-through |
| D6 | 时区混用（裸 now() 与 now_et() 并存） | DST 风险（cron 已显式处理） |
| D7 | 主管线仍保留大量历史模块（投票/topup 冻结未删；ORB 已删除） | 阅读噪声 |

运维红线（摘自 AGENT.md）：
- 重启统一 `systemctl restart ftseek-us`，重启后必跑 startup smoke；
- 归档/删除前零引用验证必须覆盖五调用面（仓内/crontab/systemd/Hermes/git hooks）；
- 新增推送走 `app/push/`；新增熔断先查分层地图；
- `auto_trading_enabled` 三处裸 regex 读磁盘是刻意设计；
- 字段落库必带基数自检 + 单位/量纲声明 + 契约测试。

---

## 12. 阅读索引

| 想看什么 | 去哪 |
|---|---|
| 实际在跑的架构 | `docs/ARCHITECTURE.md` |
| 当前策略口径（盘前/盘中/盘后） | `docs/CURRENT-STRATEGY-MAP-20260923.md` |
| 熔断分层 | `docs/circuit-breaker-map.md` |
| 数据资产 | `docs/data-map.md` |
| Playbook 模块清单 | `app/playbook/`（60+ 模块） |
| 调度线程 | `app/core/scheduler_core.py::run_forever` |
| 执行编排 | `app/playbook/pipeline.py::run_playbook_auto_execute` |
| 出场引擎 | `app/playbook/exit_engine.py` |
| 券商网关 | `app/execution/dual_broker.py` |
| 当前 OKR | `docs/OKR-2026-10.md` |
| 优化实施方案 | `docs/plans/CHAIN-OPTIMIZATION-PLAN-20261004.md` |
