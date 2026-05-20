# FTSeek-US 项目简报

> 给外部 AI/人 30 分钟读懂整个系统并给出建议
> 截至: 2026-05-16
> 当前阶段: 模拟盘 (TRADE_ENV=1), 沙盒加速实验中

---

## 1. 一句话定位

美股 AI 量化交易系统, **数据 → 多模型投票/LLM 决策 → 多策略协调 → 风控 → 富途执行 → 全链路归因 → 钉钉/飞书推送**, Python 实现, 单进程多线程, 通过 Futu OpenD 接入实盘 (当前模拟).

---

## 2. 系统拓扑

```
                        ┌──────────────────────────────────────┐
                        │  config/unified_config.yaml (SSOT)   │
                        │  + 配置治理 3 层 (校验/漂移/热重载)    │
                        └─────────────────┬────────────────────┘
                                          │
┌─────────────────────────────────────────┴───────────────────────────────────┐
│                       TradingScheduler (主进程)                              │
│  ┌───────────────────────────────────────────────────────────────────────┐ │
│  │ 数据层    Futu LV3 (主) → TwelveData → Finnhub → AlphaVantage        │ │
│  │ 新闻层    Cailianpress / WallstreetCN / Eastmoney / SEC / RSSHub      │ │
│  └─────────────────────┬─────────────────────────────────────────────────┘ │
│                        │                                                    │
│  ┌─────────────────────┴─────────────────────────────────────────────────┐ │
│  │ 信号层    IntradayScorer (5 因子) + VotingCoordinator + LLM 12 features │
│  │           ↓                                                              │
│  │ 协调层    Phase A: CapitalAllocator / SymbolLockManager /                │
│  │           SignalArbitrator (priority×confidence) / ExitDispatcher       │
│  │           ↓                                                              │
│  │ 风控层    RiskEngine (9-step PreTradeChecker) +                          │
│  │           CircuitBreaker × 3 (system/strategy/symbol) +                 │
│  │           DrawdownController + DailyLossGuard + RiskStateMachine        │
│  │           ↓                                                              │
│  │ 执行层    OrderExecutor → Futu broker_adapter (模拟盘)                  │
│  │           ↓                                                              │
│  │ 监控层    PositionExitMonitor (6 退出规则) +                             │
│  │           ExitDispatcher (按 owner_strategy 路由 ExitPolicy)             │
│  └───────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐  │
│  │ 后台线程: 30s position_sync · IntradayAttribution · Watchdog ·      │  │
│  │           ConfigGov (4h 漂移) · ScalpingEngine 自管 ·                │  │
│  │           PushHub (聚合+异步) · CapitalAllocator reconcile (5min)    │  │
│  └─────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────┬──────────────────────────────┘
                                               │
                              ┌────────────────┴───────────────┐
                              │      推送 (PushHub 聚合)        │
                              │  DingTalk → Feishu → flag 兜底  │
                              └────────────────────────────────┘

       归因         ShadowMirror (4 variant 1:1 镜像) + trade_attribution 表
                    daily_review / weekly_review / Obsidian 双写 markdown
```

---

## 3. 4 个策略 (差异化, Phase A 协调)

| 策略 | 文件 | 信号源 | priority | 特点 |
|----|----|----|:---:|----|
| **主线 (main)** | `pipeline/signal_pipeline.py` + `core/intraday_scorer.py` | trend/momentum/volume/orderflow/lifecycle 5 因子 + 多模型投票 | 5 (TRENDING +5) | 中等持仓 (小时-天) |
| **短线 (scalping)** | `scalping/engine.py` | RSI/VWAP/Bollinger 反向 + 5min 趋势对齐 | 10 (RANGING +5) | <30min 高频, 自管退出 |
| **快车道 (fast_lane)** | `strategy/pure/fast_lane.py` | LLM 盘前计划 entry_zone 匹配 | 8 | 跳过投票直接执行 |
| **LLM Plan** | `us_pipeline/premarket_planner.py` | LLM 生成 battle_plan.json | 3 | 生成计划, 不直接持仓 |

**Adapter Priority 是 regime-aware** (T8b): RANGING 时 scalping +5 / TRENDING 时 main +5 / EXTREME 时 main +5 (多层风控).

---

## 4. 端到端时序 (一笔交易完整链路)

```
T+0   market_data → IntradayScorer.score()
        → 5 因子 {trend, momentum, volume, orderflow, lifecycle} 加权
        → IntradaySignal (含 _factor_detail)

T+1   VotingCoordinator + N model agents (qwen/deepseek/minimax)
        → ConsensusEngine 共识
        → SignalScorer (8 因子, 含 news/regime/risk_reward)
        → SignalScore (trace_id=US-YYYYMMDD-XXXXXX)

T+2   risk_engine.pre_trade_check(signal):
        ↳ system_breaker → strategy_breaker → symbol_breaker
        ↳ PreTradeChecker (9 步) → drawdown → position_limits
        ↳ symbol_daily_loss_guard → reversal_cooldown (F1, 防 whipsaw)

T+3   Coordinator (Phase A):
        ↳ SignalArbitrator.arbitrate (effective_priority × confidence)
        ↳ SymbolLock.acquire (sandbox enforce)
        ↳ CapitalAllocator.try_reserve

T+4   _execute_long/short:
        ↳ Futu place_order (模拟盘)
        ↳ Position(trace_id, owner_strategy, ...)
        ↳ position_store.add + DB 持久化
        ↳ trade_attribution INSERT (factor_* 5 列 + thesis + regime)

T+5   异步:
        ↳ thesis_inferer (deepseek): 5 类入场理由
        ↳ shadow_mirror.mirror_entry: 4 variants
        ↳ PushHub.push(trade_entry): 5min 聚合

[每 30s monitor_loop]
        PositionExitMonitor.scan_positions:
        ↳ skip scalping (自管) + skip PROTECTED/FROZEN (M1)
        ↳ ExitDispatcher.policy_for(owner) → MainPipelineExitPolicy
        ↳ 6 规则 fallback (flash_exit/day_trade/scale_out/time_stop/pre_close/trailing)
        ↳ SL monotonic guard (CR-3): LONG 只能 max(_old, _new)

[价格触发 SL]
        execute_close → trade_attribution UPDATE exit_*
        → CapitalAllocator.release + SymbolLock.release
        → DailyLossGuard.record_pnl + PnLOutlierMonitor.check
        → PushHub.push(trade_exit): 大额立即 / 小额 5min 聚合
        → daily_review 04:00 ET 触发 LLM 复盘
```

---

## 5. 关键不变量 (设计铁律)

1. **风控单一闸门**: 所有下单必经 `risk_engine.pre_trade_check` (主线 + scalping)
2. **退出裁决唯一**: 只有 `_close_position` 真正平仓, 其他都是 trigger
3. **TRADE_ENV=1 强制**: 启动期检测 `TRADE_ENV=0` 立即拒启
4. **SL 单调**: chandelier 后 SL 只能向有利方向移动 (LONG 上移 / SHORT 下移)
5. **owner_strategy 是 SoT**: 不用 strategy_id (DEPRECATED)
6. **Scalping 自管退出**: PositionExitMonitor 显式过滤
7. **trace_id 贯穿全链路**: signal→position→attribution→shadow→trade_records
8. **多源持仓默认隔离**: M1 用户手动仓 + 历史仓默认 PROTECTED, 显式 takeover 才 MANAGED
9. **沙盒 enforce**: `mode.sandbox.enabled=true` 时强制 SymbolLock + SignalArbitrator 真实仲裁
10. **PushHub 异步**: 主线推送 0 阻塞, ThreadPoolExecutor fire-and-forget

---

## 6. 当前痛点 (5/15 实测)

### 业绩
- 14 笔 / 12 平 / **胜率 16.67%**
- +$891 总盈亏 (但 +$1219 ARM SHORT 一笔贡献)
- 最大单笔损 -$283 BA TRAILING_STOP
- 当前回撤 -1.49%, 最大 -6.07%

### 已修复 (5/16)
- ✅ Sandbox universe 不生效 (旧轮询表)
- ✅ ShadowMirror 0 mirrors (Z1 修复)
- ✅ trace_id 22/26 NULL (T1)
- ✅ trade_attribution factor_* 100% NULL (T2/T3)
- ✅ SymbolLock 4 路径泄漏 (R1)
- ✅ PushHub 同步阻塞 → 异步 (R2)
- ✅ Whipsaw 反向冷却 (F1)
- ✅ 推送格式不一 + 7 套实现散乱 → PushHub 统一聚合
- ✅ M1 多源持仓集成 (broker_only 智能识别策略孤儿)
- ✅ time_stop sharpe=-0.40 → 禁用

### 已知未修
- 🟡 partial_fill 半成品 (实盘前必装)
- 🟡 OpenD 运行时无主动健康监测 (只在启动期)
- 🟡 时钟跳变 0 防御
- 🟡 kill_switch CLI 缺失 (RiskStateMachine 已能实现, 待包装)

---

## 7. 关键文件路径

```
/Users/xuyuan.xy/Desktop/FTSeek-US/
├── main.py                              # 入口 (DI 容器装配)
├── config/unified_config.yaml           # SSOT 配置
├── app/
│   ├── core/scheduler_core.py          # TradingScheduler 主类
│   ├── core/models.py                  # Position/SignalScore/Order dataclass
│   ├── pipeline/
│   │   ├── signal_pipeline.py         # 信号扫描 + 决策管线
│   │   ├── order_executor.py          # 下单执行 + 仓位管理
│   │   └── position_monitor.py        # 持仓监控 + 退出
│   ├── coordinator/                    # Phase A 多策略协调
│   │   ├── strategy_coordinator.py
│   │   ├── capital_allocator.py
│   │   ├── symbol_lock.py
│   │   ├── signal_arbitrator.py
│   │   └── exit_dispatcher.py
│   ├── strategies/
│   │   ├── adapters/                  # 4 strategy adapters (regime-aware priority)
│   │   └── exit_policies.py           # 4 ExitPolicy (main/scalping/fast_lane/llm_plan)
│   ├── risk/                          # 17 风控子模块
│   ├── scalping/engine.py             # 短线引擎 (自管)
│   ├── llm/                           # LLM features (12 个, L1-L12)
│   ├── news_v2/                       # 新闻 V2 (6 sources + smart relevance)
│   ├── evolution/shadow_mirror.py     # Z1 1:1 镜像 (4 variants)
│   ├── push/                          # 统一推送 (PushHub)
│   ├── positions/                     # M1 多源持仓集成
│   └── observability/watchdog.py      # 心跳监控
├── data/
│   ├── ftseek_us.db                   # 主数据库 (trade_records / open_positions / trade_attribution / shadow_trades)
│   ├── decisions.db                   # 决策审计 (signal_arbitration / position_protection_log)
│   └── daily_battle_plan_us.json      # LLM 盘前计划
├── tests/coordinator/                  # 187 测试用例
└── docs/                              # 24 文档 (审计 + 修复 + 设计)
```

---

## 8. 数据库关键表

| DB / 表 | 用途 |
|----|----|
| `ftseek_us.db.trade_records` | 完整交易历史 (entry/exit/factor_*/owner_strategy/trace_id/...) |
| `ftseek_us.db.open_positions` | 当前未平仓 (含 action_log_json 监控痕迹) |
| `ftseek_us.db.trade_attribution` | 5 因子归因 (factor_trend/momentum/volume/orderflow/lifecycle/entry_thesis/regime) |
| `ftseek_us.db.shadow_trades` | 4 variant 镜像数据 (A/B 对比) |
| `ftseek_us.db.signal_outcomes` | 短线信号 → 实际结果 |
| `ftseek_us.db.pnl_outliers` | PnL 5σ 异常事件 |
| `decisions.db.signal_arbitration` | 多策略候选仲裁审计 |
| `decisions.db.position_protection_log` | 接管/释放/冻结 audit |
| `decisions.db.tuning_proposals` | L8 daily_auto_tuner 提案 |

---

## 9. 用户偏好 (历次对话累积)

- **karpathy 风格**: less is more, 实用 > 漂亮, 监控 > 预防
- **数据驱动**: 任何修改必须基于实际交易数据 (5/14, 5/15 反复迭代)
- **不脑补**: 拒绝想当然, 必须 grep + sqlite3 + log 取证
- **多想一步**: 不简单实施, 评估方案合理性
- **铁律敏感**: "不破坏现有功能 / 不改核心交易逻辑 / 默认隔离保护"
- **中文沟通**, 但代码注释中英混用 OK
- **沙盒优先**: 当前模拟盘验证期, universe 12 只 (NVDA/TSLA/AMD + 高波动小盘 RIVN/LCID/SNDK/ARM/NIO/BWXT/MU + SPY/QQQ)

---

## 10. 系统级数据 (5/16)

```
当前持仓 2 笔:
  US.QQQ  LONG  728  entry=$649.77  current_sl=$700.98 (保利 +7.9%)  source=策略孤儿 → MANAGED
  US.ARM  LONG  54   entry=$210.69  current_sl=$196.02              source=AUTO_SCALPING

资金:
  current_equity = $1,076,604
  cash = $553,796
  buying_power = $1,624,055
  drawdown = -1.49% (max -6.07%)

近 30 天交易统计:
  total ~700 笔, win_rate 偏低 (~17%)
  TRAILING_STOP 80 笔 win=50% sharpe=+0.23 (✅ keep)
  sl_hit 113 笔 win=17% (⚠ review)
  time_stop 7 笔 win=29% sharpe=-0.40 (🚨 已禁用)
  STRUCTURE_EXIT 33 笔 win=30% (⚠ Y3 收紧)
  tp_hit 5 笔 win=100% sharpe=+3.32 (✅ keep)

PushHub stats (开盘后):
  推送量预期下降 ~70% (5min 聚合 + 60s dedup)
  channels: dingtalk (主) > feishu (备) > flag_file (兜底)

测试覆盖:
  187/187 全绿 (135 旧 + 18 PushHub + 28 M1 + 6 M1.1)
```

---

## 11. 给外部 LLM 的建议方向 (思考起点)

### 问"系统当前最大风险是什么?"
- 实盘前 partial_fill 半成品必修 (R3)
- 主线胜率长期低位 (~17%) — 是信号质量问题还是 regime 问题?
- 5/15 single trade ARM +$1219 占当日 PnL >100% — 单笔依赖
- coordinator 已 enforce 但未跑足 7 天 baseline

### 问"信号质量怎么提升?"
- 当前 5 因子 + 8 因子 (T5 已去重双计 trend/volume)
- 缺 factor → PnL 归因模型 (T7 推迟, 需 30+ 笔 baseline)
- LLM Plan 5/5 missed (entry_zone 价格陈旧, O6 修复但需观察)

### 问"风控可以加什么?"
- 已有: 9 step PreTrade + 3 层 CircuitBreaker + DrawdownController + DailyLossGuard + ReversalCooldown + RiskStateMachine
- 缺: kill_switch CLI / 时钟跳变防御 / OpenD 运行时健康监测

### 问"多策略协调够了吗?"
- Phase A (observe_only=true) 在生产, sandbox 强制 enforce
- Phase B 全 enforce 待 7 天数据
- 缺: 真正的 multi-candidate 数据 (signal_arbitration 表 0 行 — coord 还在 observe_only)

### 问"复盘归因够细吗?"
- 已有: 5 因子落库 + entry_thesis (5 类) + regime + session + vix_at_entry + signal_tags
- 已有: ShadowMirror 4 variant (baseline/tighter_stop/no_chandelier/quick_exit)
- 缺: 因子边际贡献回归模型 (T7 等数据)

### 问"基础设施可观测性?"
- 已有: Watchdog 心跳, IntradayAttribution 60min, daily_review JSON 11 段
- 已有: PushHub 健康 ping (每小时)
- 缺: Grafana 等可视化 (用户明确不做, 靠主动推送)

---

## 12. 一周修复时间线 (上下文重要)

```
5/14   初始审计 (Y1-Y5 数据驱动修复)
5/15   PreTrade SoT 收敛 (R1-R4 重构)
5/16   早晨   O1-O7 (sandbox/shadow/time_stop/voting/owner/plan/universe)
5/16   中午   T1-T6 (trace_id + factor_* + lock enforce + reconcile)
5/16   下午   T8b/T9b/PnL (regime priority + LLM factors + 测试污染)
5/16   下午   F1-F3 (whipsaw + partial fill + cooldown SoT)
5/16   傍晚   PushHub 重构 (统一推送 + 聚合 + 防疲劳)
5/16   傍晚   R1+R2 (SymbolLock 泄漏 + PushHub 异步)
5/16   晚上   M1+M1.1 (多源持仓集成 + 智能 source 推断)
```

**核心方向**: 从"勉强能跑" → "数据完整可复盘 + 多源安全集成"

---

## 13. 待外部 LLM Review 的问题

如果你是接手的下一个 AI 或工程师, 请重点 review:
1. **5/15 16.67% 胜率是策略问题还是数据缺失/sample 问题?** (修复后等 7 天再看)
2. **187 测试覆盖够吗?** 关键路径 (主线下单/平仓/熔断) 是否真有 e2e 集成测试?
3. **PushHub 异步化是否引入新竞态?** ThreadPoolExecutor 4 worker 处理高并发是否够?
4. **M1 智能 source 推断是否过拟合 QQQ 单例?** 推断逻辑 generalize 到其他策略孤儿?
5. **RiskAggregator 计算敞口时 SHORT 应负值还是绝对值?** 当前是 abs, 对冲场景可能不对.
6. **Phase A → Phase B 切换准则 (signal_arbitration 表 0 行) 是否过严?**

---

**结论**: 系统功能完整, 187/187 测试绿, 数据完整性已大幅修复. 主要瓶颈是**信号胜率长期低位**和**实盘前 partial_fill 必装**. 下一步 7 天等 baseline 数据, 评估是否切 Phase B enforce.
