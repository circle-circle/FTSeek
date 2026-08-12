# FTSeek-US 架构总览

> 本文档描述**实际在跑的代码链路**，不是 yaml 声称的链路。
> 大量"已禁用"的规则/模块只是 `enabled: false`，代码仍存在；本文只讲 LIVE 部分，死代码见末尾清单。
> 最后更新: 2026-06-20

---

## 0. 一句话现状

实盘跑的是 **两条入场线 + 一套退出监控**，共享风控层。信号 IC≈0.02（无预测力），唯一 alpha 是 `SHORT + closing_drive`。自动交易可由 Dashboard 总闸开关，手动线实盘运行。

| 维度 | 状态 |
|------|------|
| 交易环境 | 实盘 (TRADE_ENV=0) |
| 自动交易 | 受 `feature_flags.auto_trading_enabled` 总闸控制 |
| 手动交易 | 实盘运行 (Dashboard → Futu) |
| 账户 | ~$82K equity |

---

## 1. 双线路总图

```
┌──────────────────────────────────────────────────────────────────┐
│  AUTO 线  (app/pipeline/signal_pipeline.py)                      │
├──────────────────────────────────────────────────────────────────┤
│ scan_loop()  [每 5min, 时段自适应]                                │
│  ├─ ModeSwitchService.is_auto_enabled()  ← 总闸, 关则跳过扫描      │
│  ├─ 市场相位 / 熔断 / 数据健康 / KillSwitch 门禁                   │
│  └─ _run_signal_scan_body()                                       │
│      ├─ [POLLING 路径] per-symbol → _process_symbol_new_pipeline  │
│      │     └─ 3-stage pipeline:                                  │
│      │        market_data_stage → signal_stage → risk_stage      │
│      │        → _submit_order()                                  │
│      └─ [CRA 路径] 收集所有 candidate                            │
│            → PortfolioConstructor.score_candidates()             │
│            → allocate_risk_budget() → _submit_order()            │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  _submit_order_inner()  [app/pipeline/order_executor.py:3188]    │
│  ~25 道内联风控门 (顺序见 §3) → _execute_long/_execute_short      │
│  → Futu broker (TWAP if notional > $5K)                          │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│  MANUAL 线  (app/web_dashboard/)                                  │
├──────────────────────────────────────────────────────────────────┤
│ Dashboard UI → ManualOrderService.submit_order()                 │
│  → order/validator.validate_order()  [11 道校验, 见 §3]            │
│  → Futu place_order()  ← 直达 broker, 不走 _submit_order          │
│  → ManualHoldRegistry  ← 标记 PROTECTED, ExitMonitor 不碰         │
└──────────────────────────────────────────────────────────────────┘
```

**关键差异**：手动线**不经过** `_submit_order_inner` 的 25 道门，用自己的轻量 validator。两条线共享的只有：Kill Switch、PDT、pre_trade_safety_check、capital_safety、drawdown。手动线**没有** VIX guard / IC gate / circuit breaker / battle_plan gate / 反向仓位硬拦截。

---

## 2. 信号生成 (signal_stage)

`app/pipeline/stages/signal_stage.py:process()` 按序尝试策略，collect-all 模型：

| # | 策略 | 配置门 | 状态 | 说明 |
|---|------|--------|------|------|
| 1 | fast_lane | 无 (battle plan match) | LIVE | 计划标的 ACTIVE 时 |
| 2 | orb_pipeline | regime 门 | LIVE | ETF 开盘突破 |
| 3 | trend_first | `strategies.trend_first.enabled` | ❌ false (IC=-0.24) | |
| 4 | technical | `strategies.technical.enabled` | ✅ LIVE | **唯一正 IC (+0.23)**, 动量+回撤入场 |
| 5 | voting_llm | `strategies.voting_llm.enabled` | ❌ false (40.8%WR) | |
| 6 | insider | `strategies.insider.enabled` | ✅ LIVE | SEC Form4 集群买入 |
| 7 | earnings_revision | `strategies.earnings_revision.enabled` | ❌ false (IC=-0.19) | |

收集后：regime 质量门槛 → 方向门禁 → SPY 方向过滤 → 多候选融合(confluence bonus) / 冲突取最强。

**Portfolio 打分** (`portfolio_constructor.py`)：5 维加权
`strength×0.30 + confidence×0.20 + session×0.20 + direction×0.15 + plan×0.15`
ATR 止损定价 → `capital_safety.calc_position_size()` 仓位。

> ⚠️ 4 个权重 map 硬编码在 `portfolio_constructor.py` (SESSION_STRATEGY_ALIGNMENT / REGIME_NET_EXPOSURE_TARGET / SCORE_WEIGHTS / SESSION_EXPOSURE_TARGET_MODIFIER)，未外部化到 yaml。

---

## 3. 风控门序列

### Auto 线 `_submit_order_inner` (order_executor.py:3188) — 25 道内联门

| # | 行 | 门 | 失败行为 |
|---|----|----|---------|
| 1 | 3196 | AbsoluteCircuitBreaker (日/时/连亏) | fail-close |
| 2 | 3217 | VIX 数据新鲜度 | fail-close |
| 3 | 3233 | VIX>28 硬阻 (滞回解封<25) | block |
| 4 | 3253 | auto_trading_enabled 总闸 | block |
| 5 | 3264 | 日 auto 交易数上限 | block |
| 6 | 3283 | auto 预算 (capital_safety) | block |
| 7 | 3296 | IC 质量门 (策略/方向级) | block |
| 8 | 3377 | Kill Switch | fail-close |
| 9 | 3391 | Broker 持仓 dedup | skip |
| 10 | 3411 | opening_drive 时段门禁 | block |
| 11 | 3424 | 退出屏障 (防闪退重入死循环) | block |
| 12 | 3438 | FLASH_EXIT 熔断 | block |
| 13 | 3443 | 信号冷却复检 | block |
| 14 | 3452 | 标的日亏限额 ($500) | block |
| 15 | 3460 | pre_trade_safety_check | block |
| 16 | 3497 | 市场广度信号 | 调整 |
| 17 | 3536 | PDT guard (实盘) | block |
| 18 | 3558 | 标的+方向冷却 | block |
| 19 | 3564 | 反向冷却 (whipsaw) | block |
| 20 | 3577 | SymbolLock 互斥 | block |
| 21 | 3598 | Coordinator 多源仲裁 | block |
| 22 | 3671 | _check_gates_and_conflicts (子门 ~15 道) | block |
| 23 | 3685 | 同向重复仓位拦截 | block |
| 24 | 3703 | CapitalAllocator.try_reserve | block |
| 25 | 3731 | 方向路由 → _execute_short/_execute_long | — |

### Manual 线 `validate_order` (order/validator.py:20) — 11 道

KillSwitch → 参数 → $50K 上限 → 日 20 次 → ALLOW_REAL → PDT → 集中度(pre_trade_safety) → buying power → 日回撤 → capital_safety 水位 → 双路预算 → 限价偏离。

---

## 4. 退出监控 (两层级联，非竞争)

```
scheduler_core PositionMonitorLoop
  → app/pipeline/position_monitor.py::monitor_loop()    [编排: 循环/对账/broker同步]
    → _check_positions()
      [1] ExitMonitor + ExitRuleRegistry  ← 唯一退出权威
          app/risk/position_exit_monitor.py
          内部 _check_exit_for_position() → registry 或 legacy
      [2] _check_via_legacy           ← 仅 registry 出错时兜底
    → order_executor._close_position()  [执行: broker + DB + notify]

  (2026-08-12: ExitRuleDispatcher 第三路径已删除 — 自 5/31 dispatcher_enabled=false
   已接线未通电, 从未成为主路径; playbook 仓位由 monitor_playbook_sim_exits 自管退出)
```

### 三条 LIVE 退出规则

| 规则 | 触发 | 动作 |
|------|------|------|
| **atr_stop** | entry ± 4.0×ATR, max 3% (SHORT max 10%) | 全平 |
| **scale_out** | profit ≥ 1.0 ATR, 按 `{direction}_{strategy}` 分层 | 分批平 + ratchet 收紧剩余止损 |
| **pre_close** | 收盘前 8min | 盈利落袋 / 亏损保留过夜(带 -3% 隔夜硬止损) |

**保护**：
- Grace Period：入场后 45min 仅允许硬止损（浮亏≥3% 立即解除）
- 退出冷却：120s
- 手动仓位：PROTECTED，ExitMonitor 不干预

### scale_out ↔ atr_stop ratchet 接线
`scale_out.py` 返回 `stop_price`/`new_stop_pct` → `_execute_exit` 单调推 `trail_state` SL →
`atr_stop_engine.compute_stop_lines()` 读 `trail_state["scale_out_stop_floor"]` 作下限 clamp →
剩余仓位止损从 entry±4×ATR 收紧到 ratchet 水位。
`trail_state_store` 持久化 `triggered_tiers` 到 SQLite，跨重启去重。

> ⚠️ **已知不一致 (landmine)**：`trailing_stop` 规则（即 ATR 硬止损）在 `exit_policy.yaml::exit_rules` **无对应 key**。
> - Dispatcher 主路径要求显式 `enabled`（缺省=禁用）→ **跳过** trailing_stop
> - Registry 内层路径缺省=不禁用 → trailing_stop **仅靠此路径存活**
> - 故 `position_exit_monitor.py` 的 registry 禁用逻辑**保持"缺省=启用"**，切勿改为 fail-close，否则会关掉实盘 ATR 止损。
> - 根因修复（让 dispatcher 也跑 trailing_stop）属架构变更，待后续灰度。

---

## 5. Scheduler 后台线程

`scheduler_core.py::run_forever()` 启动 8 线程：

| 线程 | 目标 | 作用 |
|------|------|------|
| SignalScanLoop | signal_pipeline.scan_loop() | 信号生成 (主) |
| PositionMonitorLoop | position_monitor.monitor_loop() | 退出监控 (主) |
| LLMStrategyLoop | phase-adaptive | 盘前计划/盘后 LLM/playbook |
| NewsV2Loop | news_pipeline_v2 (180s) | 新闻采集 (observe) |
| VIXMonitorLoop | (300s) | VIX 告警 |
| ManualGuardLoop | manual_guard_loop | 手动仓风控 |
| InsiderScanLoop | SEC Form4 日扫 | 内部人信号 |
| ORB Engine | orb_engine.start() | ETF 独立突破辅线 |

启动前：`_restore_positions_from_db()` + `startup_reconcile()` (内存↔DB 双向同步)。

---

## 6. Battle Plan 生成→消费链

```
生成 (盘前 LLM):
  app/pipeline/llm_pipeline.py:run_premarket_planning()
  app/plan/pipeline.py / app/llm/agent_bridge.py
    → 写 data/daily_battle_plan_us.json

消费 (运行时):
  app/us_pipeline/battle_plan_manager.py  ← Singleton, 加载 JSON
    ├─ signal_stage.py:289   fast_lane 策略匹配 (target ACTIVE)
    ├─ portfolio_constructor.py:808  plan_alignment 打分维度
    ├─ order_executor.py:1782  _check_gates_and_conflicts 门 J: check_trade()
    └─ scheduler_core.py:1053  注入 daily_active_pool
```

> ⚠️ `app/playbook/` 是**独立**的 advisory 系统（Layer 0-3：regime→screener→LLM指令→shadow追踪），`playbook.enabled=true` 但 `execute_mode=simulate`。它**不写** daily_battle_plan_us.json，**不进**实时入场路径。USBattlePlanManager 才是 consumer。

---

## 7. SSOT 索引

| 参数类 | 位置 |
|--------|------|
| 全局配置 | `config/unified_config.yaml` |
| 退出策略 (LIVE 规则) | `app/risk/exit_policy.yaml` |
| Portfolio 权重 map | `portfolio_constructor.py` 硬编码 (待外部化) |
| LLM 路由 | `unified_config.yaml::llm.models` + `llm.features` |
| 影子系统 (回测/辩论) | `config/exit_profiles.yaml` (≠主线 exit_policy.yaml) |

---

## 8. 已删 / Dormant 清单 (2026-06-20 清理)

### 本轮删除（零消费者，实盘行为零改变）
- `app/risk/us_risk_pipeline.py` + `app/risk/checks/` — 实例化后 `.run()` 从不调用，实际风控走 order_executor 内联门
- `app/core/position_monitor.py` — re-export stub，零 importer
- `app/risk/options_quality_gate.py` — 零外部 import
- (Phase B) disabled 退出规则的代码+接线：flash_exit / velocity_exit / time_stop / day_trade_mode

### Dormant（嵌在 live 路径，标 fail-safe 后保留，待后续灰度移除）
- `chandelier_trail` / `breakeven` / `cost_buffer` — 嵌在 `atr_stop_engine.compute_stop_lines()`，被 live `trailing_stop` 调用。
  yaml `enabled: false`，且 `atr_stop_engine.py` 默认值已改为 fail-safe（缺省=禁用）。拆数学属工作代码变更，留后续单独灰度。
- `progressive_trailing` 的 6 个子 key（session_blend / effective_atr_floor / fallback_sl 等）仍被 live 消费，块名具有误导性。

### 保持不动（探索证明 LIVE，勿删）
`dynamic_exit` (regime-aware SL/TP) / `pipeline.master_switch` (StrategyRouter shadow 路由) / `llm.strategy_engine` / `legacy_aliases` / catalyst (reverse_catalyst active) / orb 引擎 / `time_stop_hours` (Position 字段，≠退出规则 time_stop)。

---

## 9. 数据驱动结论（策略层）

- 信号 IC≈0.02，无预测力
- **SHORT + closing_drive**：77.1% WR, +46.2% — 唯一 alpha
- LONG (any)：20.9% WR, -$2.9K — 系统性亏损
- opening_drive 入场：136 笔 -366.6% — 已硬拦
- HIGH_VOL (VIX>25)：141 笔 -154K (占亏 71%) — VIX>28 硬阻

> 真正驱动 PnL 的只有 3 个开关：方向(SHORT)、时段(禁 opening_drive)、波动率(VIX 门禁)。
> config 里大量"收紧/放宽/分层"参数是在无 alpha 系统上微调，是"越调越乱"的根因。
