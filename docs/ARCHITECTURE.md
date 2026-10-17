# FTSeek-US 架构总览

> 本文档描述**实际在跑的代码链路**，不是 yaml 声称的链路。每条论断带 file:line，可复核。
> 大量"已禁用"的规则/模块只是 `enabled: false`，代码仍存在；本文只讲 LIVE 部分，死代码/债务见 §8。
> 最后更新: 2026-10-17（行号时效校正: §1 双锁/§2 唯一执行链/§3 退出宿主/§4 熔断锚点与 §8 D1
> 按当日 HEAD 逐条复核回写; 2026-08-20 全文重写版的行号已因 pipeline 拆分与重构漂移）

---

## 0. 一句话现状

**三态共存（shadow / futu_simulate / live）架构已就位，当前运行在 futu_simulate（富途模拟盘）。**
Playbook 是**唯一**下单管线；旧主信号管线被**双锁**冻结（只扫描研究、禁止下单）。
8/31 终裁（`docs/specs/august-verdict-framework-20260831.md`）决定 GO/PIVOT/STOP。

| 维度 | 状态 |
|------|------|
| 交易环境 | futu_simulate（`AUTO_TRADE_ACCOUNT=sim`，.env.us:29） |
| 下单管线 | 仅 Playbook（`playbook.enabled=true, execute_mode=futu_simulate`，unified_config.yaml:1034-1035） |
| 主信号管线 | 冻结（双锁，§1）——扫描/研究仍跑，下单被拦 |
| ORB 辅线 | `orb.enabled=false`（unified_config.yaml:936），引擎不启动（scheduler_core.py:709） |
| topup 补仓 | 冻结（`topup.enabled=false`，unified_config.yaml:1278-1279，DEB-20260805 终裁） |
| 账户 | ~$97K equity，DD 水位 GREEN |

---

## 1. 主管线冻结：双锁 + 一层配置静默

```
锁 1 (扫描侧): app/pipeline/signal_pipeline.py:222-223
  scan_loop() 入口: _skip_execution_in_trading = True  (局部 + scheduler 双写, 硬编码非配置)
  消费点: signal_pipeline.py:650 / 1305, stages/signal_stage.py:620
  → 信号照算(研究/IC 积累), 但候选不带下单资格
  注意: scan_loop 线程本身不 gate 掉 — 还驱动日/周复盘、快照、watchdog 心跳 (:219-221 注释自述)

锁 2 (执行侧): app/pipeline/order_executor.py:3559  [_submit_order 白名单, 硬编码]
  if source_type not in (PLAYBOOK_SIM, PLAYBOOK_LIVE): 拦截
  → 即使锁1被绕过(如盘前信号路径), 新仓也出不了 order_executor
  （2026-09-20 用户裁 Playbook 唯一自动交易管线 ⇒ 白名单已由 +ORB_ENGINE 收窄）

配置层 (第三道): strategy.main_scan_enabled=false (unified_config.yaml:1825, 2026-07-24 减法R2)
  → 静默 _run_signal_scan_impl 扫描本体; scan_loop 线程仍跑非交易 housekeeping
```

`signal_pipeline.py:219-221` 注释自述锁1+锁2 为有意冗余：**两锁同时失效才会漏单**；
配置层再独立兜一层（三层各自足以冻结下单）。
冻结范围 = 下单；扫描/信号/IC 遥测按配置保留运行（为终裁与因子研究攒数据）。

---

## 2. Playbook 唯一入场链（实际 LIVE 路径）

```
指令生成 (盘前 LLM + 通道):
  DA-* (Daily Analyst) / universe_expansion-* (UE) / intraday_reentry-* /
  TFB-* (topup bridge, 已冻结) / generate(空 decision_id)
    → 直写 playbook_instructions 表   ⚠️ 旁路风险见 §8

执行 (唯一执行口):
  app/playbook/playbook_service.py
    execute_instruction() :1034
      → _execute_via_dual_broker() :1505     ← 说明注释见 :1807
                                                  "DA直写/reentry/投票 全部 writer 的唯一执行路径"
        → _dual.submit_order(_order, target_env=target_env) :1940
          → order_executor._submit_order (过 §1 锁2 白名单: PLAYBOOK_SIM/PLAYBOOK_LIVE)
            → ~25 道内联风控门 → Futu SIMULATE broker

退出: 不走主线 ExitMonitor → monitor_playbook_sim_exits 自管 (§3)
```

- **跨进程标的锁 (2026-10-17 审计)**: `_execute_via_dual_broker` 在下单前于
  `data/locks/symbol_orders/<symbol>.lock` 取非阻塞 flock — scheduler 与看板是
  **两个进程**，进程内 `threading.Lock` 不能跨进程互斥；取不到 ⇒ 拒绝下单
  (fail-closed，现场见 `playbook_service._try_acquire_cross_process_symbol_lock`)。
  回滚: 删跨进程锁块 + 还原调用点。
- 配置：`playbook.auto_execute=true`（unified_config.yaml:1023）
- 通道判别用 `decision_id` 前缀；分通道漏斗观测见 `scripts/short_supply_funnel_report.py`

---

## 3. 退出监控：两套宿主，各管各的仓

```
[主线仓] scheduler_core _monitor_loop (2116)
  → app/pipeline/position_monitor.py::monitor_loop()
    → ExitMonitor + ExitRuleRegistry (app/risk/position_exit_monitor.py)
      registry 或 legacy 兜底 → order_executor._close_position()
  playbook 仓被显式排除: position_monitor.py:352-360
    "由 monitor_playbook_sim_exits 自管, 不纳入主线 PositionStore"

[Playbook 仓] scheduler_core _playbook_exit_fast_loop (2903, 2026-07-21 独立成环)
  → app/playbook/pipeline.py::monitor_playbook_sim_exits (别名: pipeline.py:3583)
    市场开放时每 10s 跑一次（yaml 现值 10s，代码默认 20s；端到端退出延迟 ~25s）
    旧宿主 llm_strategy_loop 调用点已让位 (scheduler_core.py:2905-2910 注释 + :1929)
  order_executor 旧管线平仓路径对 playbook 仓关闭: app/pipeline/order_executor.py:4633
```

### Playbook 退出规则族（`_check_playbook_exit_rules`，pipeline.py:2678）

配置在 `config/playbook_horizon.yaml::factor_exit_rules`（**≠ exit_policy.yaml**），按序：
[0] 开盘冷却 15min SL 放宽 4% → [0.5] 30min 无盈利收紧 SL 1% → [1] SL_INITIAL/BREAKEVEN/TRAIL_LOCK →
[1.5] TAKE_PROFIT (RR≥1.5) → [2] 隔夜跳空 >3% → [3] PRE_CLOSE 收盘前 8min →
[3.4] MFE_STALL（当前 disabled）→ [4] TIME_EXIT (>6h+亏损) → [5] MAX_HOLD_T5 (Cohort B ≥5 交易日)。

> ⚠️ **已知不一致（landmine，未修复）**：`trailing_stop` 规则（即 ATR 硬止损）在 `exit_policy.yaml::exit_rules` **无对应 key**（position_exit_monitor.py:271-285 注释自述）：
> - Dispatcher 主路径要求显式 `enabled`（缺省=禁用）→ **跳过** trailing_stop
> - Registry 内层路径缺省=不禁用 → trailing_stop **仅靠此路径存活**
> - 故 registry 禁用逻辑**保持"缺省=启用"**，切勿改 fail-close，否则关掉实盘 ATR 止损。
> - 根因修复 = DEB-20260820 执行项 P1-2（排队，终审后）。
> - （2026-08-12：ExitRuleDispatcher 第三路径已删除——自 5/31 dispatcher_enabled=false 已接线未通电，从未成为主路径。）

---

## 4. 风控体系

### 熔断器族（无统一 L0-L4 编号，实际模块如下）

| 模块 | 位置 |
|------|------|
| AbsoluteCircuitBreaker（_submit_order_inner 第一道，不受任何 flag 控制） | app/risk/absolute_circuit_breaker.py:37；app/pipeline/order_executor.py:3586-3600 |
| StrategyCircuitBreaker（连亏3/日-3%/时-2% → 暂停30min） | app/risk/circuit_breaker.py:28；unified_config.yaml:1374-1378 |
| SymbolCircuitBreaker / SystemCircuitBreaker | app/risk/circuit_breaker.py:88 / :179 |
| AccountCircuitBreaker / MarketCircuitBreaker | app/risk/account_circuit_breaker.py:41 / market_circuit_breaker.py:25 |

### 资金安全水位（capital_safety.py:42-77；yaml risk.capital_safety.water_levels :1356-1372）

| 水位 | 回撤区间 | pos_mult / risk_mult |
|------|---------|----------------------|
| GREEN | 0% ~ -5% | 1.0 / 1.0 |
| YELLOW | -5% ~ -10% | 0.5 / 0.5 |
| ORANGE | -10% ~ -20% | 0.25 / 0.25 |
| RED | -20% ~ -35% | 0.0 / 0.0 |
| BLACKOUT | > -35% | 熔断锁仓（capital_safety.py:165） |

恢复逻辑：水位回 GREEN/YELLOW 且回撤脱离熔断线自动解锁（capital_safety.py:231-233）。
回撤熔断曾静默失效（双路径查不同账户），已修 + 突变检测后门已删除（fail-safe，见 memory capital-safety-drawdown-fix）。

### 执行门（app/pipeline/order_executor.py::_submit_order_inner 内联 ~25 道）

AbsoluteCircuitBreaker(日/时/连亏) → VIX 新鲜度 → VIX>28 硬阻 → auto 总闸 →
日交易数 → 预算 → IC 质量门 → KillSwitch → broker dedup → 时段门 → 退出屏障 →
FLASH_EXIT 熔断 → 冷却复检 → 标的日亏限额($500) → pre_trade_safety → 广度 →
PDT → 标的/方向/反向冷却 → SymbolLock → Coordinator 仲裁 → 子门组 → 同向重复拦截 →
CapitalAllocator.try_reserve → 方向路由。
（行号随版本漂移，以 `_submit_order_inner` 内顺序为准。）

---

## 5. Scheduler 后台线程（scheduler_core.py:1646-1709 实际 spawn 清单）

| 线程 | spawn 行 | 目标 | 状态 |
|------|---------|------|------|
| SignalScan | 1646 | signal_pipeline.scan_loop() | LIVE（只扫描不下单，§1；扫描本体被 main_scan_enabled=false 静默，线程跑 housekeeping） |
| PositionMonitor | 1652 | position_monitor.monitor_loop() | LIVE |
| LLMStrategy | 1658 | 盘前计划/盘后复盘/playbook 生成+auto_execute(300s) | LIVE |
| NewsV2 | 1665 | news_pipeline_v2 | ❌ 已退役（news_v2.enabled=false，yaml:780；scheduler_core.py:433-434 "C2 退役"） |
| VIXMonitor | 1673 | VIX 告警 | 条件启动 |
| ManualGuard | 1680 | 手动仓风控（ManualOrderService 固定 REAL 路由，di_container.py:141） | LIVE |
| InsiderScan | 1687 | SEC Form4 日扫 | LIVE |
| PlaybookExitFast | 1696 | monitor_playbook_sim_exits（10s，yaml 现值；代码默认 20s） | LIVE（§3） |
| ORB Engine | 703 | orb_engine.start() | ❌ 未启动（enabled=false；注意 yaml:949 mode=active，若误开会直接主动交易） |

启动前：`_restore_positions_from_db()` + `startup_reconcile()`（内存↔DB 双向同步）。

---

## 6. 三态共存与环境路由

```
环境 SSOT = AUTO_TRADE_ACCOUNT (sim|real)   .env.us:29
  ├─ broker 路由: position_sync.py:495-497  TrdEnv.SIMULATE if "sim" else REAL
  ├─ 仓位同步只查当前 env 一侧: position_sync.py:576-577  _envs_to_query 单元素
  │    (同文件 :566 注释"三态融合: 同时查询 SIMULATE+REAL"已过期, 代码实为单 env)
  ├─ ModeSwitchService 只认它 (mode_switch_service.py:158/250)
  └─ 已废弃: TRADE_ENV/FUTU_TRADE_ENV 不再控制 broker
       TRADE_ENV=1 残留语义 = DB 路由 (1→ftseek_us.db; 写 0 会切库丢 OKR 数据, .env.us:21-24)

live 额外硬门: ALLOW_REAL_TRADING=false (.env.us:116)

三态:
  shadow        = 影子/重放模块 (不碰 broker): exit_shadow/limit_entry_shadow/short_boost_shadow…
                  (纯 simulate 执行模式已废弃: playbook_service.py:762-775 强制降级 futu_simulate)
  futu_simulate = 当前运行态 (Playbook → Futu SIMULATE)
  live          = 毕业后 AUTO_TRADE_ACCOUNT=real + ALLOW_REAL_TRADING=true
```

> ⚠️ 三态靠 env var + 约定维持，**无显式状态机**；切态未演练。
> 风险与修复 = DEB-20260820 执行项 P1-4（排队，毕业通道最高测试优先级）。

---

## 7. SSOT 索引

| 参数类 | 位置 |
|--------|------|
| 全局配置 | `config/unified_config.yaml` |
| 退出策略（LIVE 规则） | `app/risk/exit_policy.yaml` |
| 环境路由 | `.env.us::AUTO_TRADE_ACCOUNT`（非 TRADE_ENV） |
| Playbook 执行参数 | `unified_config.yaml::playbook` (:1022+) |
| 影子系统（回测/辩论） | `config/exit_profiles.yaml`（≠主线 exit_policy.yaml） |
| LLM 路由 | `unified_config.yaml::llm.models` + `llm.features` |
| 决策过程记录 | `docs/debates/`（索引: index_generated.md + README.md） |
| 进度队列 | `handoff.md`（唯一交接真源） |

---

## 8. 已知债务与 landmine（DEB-20260820 全系统审计建档）

| # | 债务 | 锚点 | 修复项 |
|---|------|------|--------|
| D1 | god module：pipeline.py 3,646 行 + pipeline_parts/ 8,366 行 (2026-10-17 实测) / order_executor.py 5,808 行（无专属测试文件）；`except Exception` = pipeline.py 68 + parts 238 处 | app/playbook/, app/pipeline/ | P1-3（部分缓解：pipeline 已拆 parts，余排队） |
| D2 | 指令写入方（DA pipeline.py:1953/2194、reentry intraday_reentry.py:430、UE scripts/llm_universe_scan.py:1335）直写 playbook_instructions：收口点在 `PlaybookDB.save_instruction` 内的 admission（db.py:208-232），但**判分在下单前很远**，与 order_executor 门序列脱节 | app/playbook/ | P1-5（排队） |
| D3 | trailing_stop 双路径不一致（§3 landmine） | position_exit_monitor.py:271-285 | P1-2（排队） |
| D4 | 三态无状态机（§6） | position_sync.py env 读取 | P1-4（排队） |
| D5 | sector_limits 未知标的放行（pass-through） | app/risk/sector_limits.py:139-141 | P2-4（排队） |
| D6 | regime 乘数表硬编码 3×、两套 IC 系统并存、SL 计算多源 | 多处 | P2-5（排队） |
| D7 | 时区混用：裸 `datetime.now()` 与 `now_et()` 并存 | 多处 | P2-7（排队） |

> 全部机制类修复排队至 8/31 终裁后（验证期白名单 = 修尺子/攒样本/做减法）。
> 量化侧现状（无方向 alpha / 费用 12bps 结构性出血 / SHORT 供给枯竭）见 DEB-20260820 §四。
