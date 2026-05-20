# 多策略并行架构设计

> **版本**: v0.1 (2026-05-14)
> **状态**: 设计稿 — 待评审
> **目标读者**: 实施 agent / 架构评审者
> **依赖文档**:
> - `docs/SHADOW_IMPLEMENTATION_REPORT.md` (现有 shadow 实现)
> - `OPTIMIZATION_REPORT_2026-05-12.md` (33 项 Fix, 本文档与其互补)
> - `app/strategy/base.py:BaseStrategy` (现有抽象基类)

---

## 0. TL;DR

将 4 条并行策略 (Main / Scalping / Fast Lane / LLM Plan) 通过**协调层**解耦, 共享单一基础设施, 引入**ExecutionMode 抽象**让 Shadow 与 Backtest 自然成为同一框架下的不同执行后端。

**核心原则**:
1. **不重建** — wrap 现有模块, 不重写业务逻辑
2. **单一真相源** — 行情 / 风控 / 持仓 / 资金账本各只有一份
3. **冲突在协调层解决** — 各策略不感知彼此, 由 Coordinator 仲裁
4. **Shadow = 同代码, 异执行** — 实时 walk-forward 验证, 与历史 backtest 互补

**首期目标 (Phase A, 1 周)**:
- 引入 `StrategyCoordinator` 雏形 (CapitalLedger + SymbolLock)
- 把 4 条策略 wrap 成 Adapter, 接入现有 `StrategyRegistry`
- `Position.owner_strategy` 字段落地, 解锁后续 ExitDispatcher

---

## 1. 现状盘点

### 1.1 已存在的基础设施 (build on these, do not duplicate)

| 模块 | 文件 | 状态 |
|-----|------|------|
| `BaseStrategy` 抽象基类 | `app/strategy/base.py` | ✅ 完整, 含 MarketContext |
| `StrategyRegistry` 单例注册中心 | `app/strategy/registry.py` | ✅ 完整, 支持按 group 查询 |
| Shadow 跟踪 + 统计验证 | `app/evolution/shadow_tracker.py` | ✅ 含 Bonferroni / 效应量门槛 |
| 晋升引擎 | `app/evolution/promotion_engine.py` | ✅ MIN_SAMPLES=20, paired t-test |
| 改进流水线 | `app/evolution/improvement_pipeline.py` | ✅ |
| 已有策略实现 | `app/strategy/pure/{fast_lane,llm_vote,technical}.py` | ✅ |
| 主线信号 | `app/pipeline/signal_pipeline.py` + `app/core/intraday_scorer.py` | ✅ 但未封装为 Strategy |
| Scalping 自治模块 | `app/scalping/{engine,signals,exits,stock_selector,budget}.py` | ✅ 完全独立, 未挂注册中心 |
| LLM 盘前计划 | `app/us_pipeline/{premarket_planner,battle_plan_manager,open_checker}.py` | ✅ 未封装为 Strategy |
| MarketDataRouter | `app/data/market_data_router.py` | ✅ 已是单一入口 |
| RiskKernel 雏形 | `app/risk/{engine,pre_trade_check,risk_state_machine}.py` | ✅ 但未抽象 |

### 1.2 缺口 (本设计要填)

| 缺口 | 影响 | 解决方案 (本设计) |
|------|------|------------------|
| 无 Coordinator | 多策略对同 symbol 同时下单 → 仓位/风险叠加 | §3.1 `StrategyCoordinator` |
| 无 CapitalLedger | `capital_pct` 是软约束, 易超 90% 红线 | §3.2 `CapitalAllocator` 实时账本 |
| 无 SymbolLock | 同一 symbol 被多策略瓜分 | §3.3 `SymbolLockManager` (priority + confidence) |
| 无 ExitDispatcher | `PositionExitMonitor` 一刀切 6 规则, 无视入场策略 | §3.4 `ExitDispatcher` 按 owner_strategy 路由 |
| `Position.owner_strategy` 缺失 | 平仓后无法归因到具体策略, F19 attribution 半残 | §4.1 schema 加列 |
| Shadow 只跟主线 | scalping/fast_lane 无 OOS 实时验证 | §5 ExecutionMode 抽象 |
| OrderGateway 未抽象 | Futu 调用散落, Shadow/Backtest 难注入 | §6 `OrderGateway` 接口 |
| 主线/scalping 用不同 MarketContext | 信号难比较, F8 因子 IC 难做 | §2.3 适配 + 渐进收敛 |

---

## 2. 整体架构 (4 层)

```
┌──────────────────────────────────────────────────────────────────────────┐
│  L4 ExecutionMode  ─ live | shadow | backtest 三态                       │
│  ────────────────────────────────────────────                             │
│  RealOrderGateway / ShadowExecutor / BacktestExecutor                    │
│  同一份策略代码, 不同执行后端 (F29 backtest 框架自然落入此层)              │
└──────────────────────────────────────────────────────────────────────────┘
                                  ▲ 注入
┌──────────────────────────────────────────────────────────────────────────┐
│  L3 StrategyCoordinator  ─ 唯一解决冲突的地方                             │
│  ────────────────────────────────────────────                             │
│  ┌──────────────────┬──────────────────┬──────────────────┐              │
│  │ CapitalAllocator │ SymbolLockMgr    │ SignalArbitrator │              │
│  │ 强制 cap %       │ priority+conf    │ 多信号决断       │              │
│  └──────────────────┴──────────────────┴──────────────────┘              │
│  ┌──────────────────┐                                                    │
│  │ ExitDispatcher   │ 按 position.owner_strategy 路由 ExitPolicy        │
│  └──────────────────┘                                                    │
└──────────────────────────────────────────────────────────────────────────┘
                                  ▲ 调度
┌──────────────────────────────────────────────────────────────────────────┐
│  L2 Strategy (BaseStrategy 子类, 复用 app/strategy/registry.py)           │
│  ────────────────────────────────────────────                             │
│  ┌──────────────┬──────────────┬──────────────┬──────────────┐           │
│  │ MainPipeline │ Scalping     │ FastLane     │ LLMPlan      │           │
│  │ Adapter      │ Adapter      │ Adapter      │ Adapter      │           │
│  │  wraps       │  wraps       │  wraps       │  wraps       │           │
│  │  intraday_   │  scalping/   │  pure/       │  us_pipeline │           │
│  │  scorer      │  engine      │  fast_lane   │  /premarket  │           │
│  └──────────────┴──────────────┴──────────────┴──────────────┘           │
│  统一 contract: select_universe / generate_signal / get_exit_policy      │
└──────────────────────────────────────────────────────────────────────────┘
                                  ▲ 调用
┌──────────────────────────────────────────────────────────────────────────┐
│  L1 Shared Kernel  ─ 单一真相源, 各策略不得复制                          │
│  ────────────────────────────────────────────                             │
│  MarketDataRouter  · RiskKernel  · PositionRegistry                      │
│  OrderGateway(接口) · AttributionStore · paths.py · ConfigContainer       │
└──────────────────────────────────────────────────────────────────────────┘
```

### 2.1 数据流 (open 路径)

```
[L1] MarketDataRouter.tick(symbol)
        │
        ▼
[L2] for strategy in registry.active_strategies():
        signal = strategy.evaluate(symbol, ctx)  # 各策略并行评估
        signals.append((strategy.name, signal))
        │
        ▼
[L3] arbitrated = SignalArbitrator.arbitrate(signals)
        │
        ▼ (winner only)
[L3] CapitalAllocator.try_reserve(strategy, position_pct) ──[REJECT 资金不足]→
        │ ok                                                  ↓
        ▼                                                  rejection_logger
[L3] SymbolLockManager.acquire(symbol, strategy)  ──[REJECT 已锁]→
        │ ok                                                ↓
        ▼                                                rejection_logger
[L1] RiskKernel.pre_trade_check(signal) ──[REJECT 各风控]→
        │ ok                                              ↓
        ▼                                              rejection_logger
[L4] OrderGateway.submit(order)  # live or shadow
        │
        ▼
[L1] PositionRegistry.add(position, owner_strategy=strategy.name)
[L1] AttributionStore.record_entry(...)  # F19 完整 schema
```

### 2.2 数据流 (close 路径)

```
[L1] MarketDataRouter.tick(symbol)
        │
        ▼
[L1] for position in registry.positions:  # 单次扫描, 不再多 monitor 各扫
        │
        ▼
[L3] policy = ExitDispatcher.policy_for(position.owner_strategy)
[L3] decision = policy.evaluate(position, ctx)
        │ (no_action | reduce | close)
        ▼
[L4] OrderGateway.submit(close_order)
        │
        ▼
[L1] CapitalAllocator.release(strategy, freed_capital)
[L1] SymbolLockManager.release(symbol)
[L1] AttributionStore.record_exit(decision_id, exit_reason, ...)
```

### 2.3 MarketContext 收敛策略

**问题**: 主线用 `intraday_scorer` 的大 ctx (40+ 字段), scalping 用自己 5min K 字典, fast_lane 又另一套。F8 因子 IC 难做。

**方案 (渐进, 不破坏)**:
- **Phase A**: 各 Adapter 内部把自己的 ctx 转成 `MarketContext` (`app/strategy/base.py` 的现有 dataclass) 后传出去
- **Phase B**: `MarketDataRouter.get_market_context(symbol)` 提供统一 `MarketContext`, 各策略按需取字段 (`extra` dict 装策略特有字段)
- **Phase C** (可选): 删除策略私有 ctx 构造代码

---

## 3. L3 协调层详细设计

### 3.1 StrategyCoordinator (主入口)

```python
# 新增: app/coordinator/strategy_coordinator.py
class StrategyCoordinator:
    """
    多策略并行调度的唯一入口.
    替代 main_loop 中各策略各自调度的现状.
    """
    def __init__(
        self,
        registry: StrategyRegistry,
        capital_allocator: CapitalAllocator,
        symbol_lock: SymbolLockManager,
        signal_arbitrator: SignalArbitrator,
        exit_dispatcher: ExitDispatcher,
        risk_kernel: RiskKernel,
        order_gateway: OrderGateway,           # 注入: live or shadow
        position_registry: PositionRegistry,
        attribution: AttributionStore,
        market_data: MarketDataRouter,
    ):
        ...

    def run_open_cycle(self, symbol: str) -> List[ExecutionResult]:
        """对单个 symbol 跑一轮 open 流程, 返回所有 strategy 的执行结果"""
        signals = []
        for strat in self.registry.active_strategies(market_phase=...):
            ctx = self.market_data.get_context(symbol, channels=strat.get_data_channels())
            sig = strat.generate_signal(symbol, ctx)
            if sig and sig.is_actionable():
                signals.append((strat, sig))

        winner = self.signal_arbitrator.arbitrate(signals)  # priority×conf
        if not winner:
            return [ExecutionResult.NO_SIGNAL]

        strat, sig = winner
        # 资金 + 锁 + 风控 三道闸
        if not self.capital_allocator.try_reserve(strat.name, sig.position_pct):
            return [ExecutionResult.REJECTED("CAPITAL_CAP")]
        if not self.symbol_lock.acquire(symbol, strat.name):
            self.capital_allocator.release(strat.name, sig.position_pct)
            return [ExecutionResult.REJECTED("SYMBOL_LOCKED")]
        rk_result = self.risk_kernel.pre_trade_check(sig)
        if not rk_result.allowed:
            ...rollback...
            return [ExecutionResult.REJECTED(rk_result.reason)]

        # 下单
        order_result = self.order_gateway.submit(self._build_order(strat, sig))
        if order_result.filled:
            position = Position(..., owner_strategy=strat.name)
            self.position_registry.add(position)
            self.attribution.record_entry(strat, sig, position, order_result)
        else:
            # 失败 → 回滚资金/锁
            ...
        return [ExecutionResult(...)]

    def run_exit_cycle(self) -> List[ExecutionResult]:
        """对所有持仓跑一轮 exit 评估"""
        results = []
        for pos in self.position_registry.all():
            policy = self.exit_dispatcher.policy_for(pos.owner_strategy)
            ctx = self.market_data.get_context(pos.symbol)
            decision = policy.evaluate(pos, ctx)
            if decision.action != "NO_ACTION":
                ...submit close, release locks/capital, record_exit...
        return results
```

### 3.2 CapitalAllocator (资金账本)

```python
# 新增: app/coordinator/capital_allocator.py
class CapitalAllocator:
    """
    实时资金账本 — 强制每个 strategy 的 capital_pct 上限.

    取代当前"软约束 + 主+scalp=0.90"的模糊状态.
    """
    def __init__(self, total_equity_provider, config):
        self._allocations = {}  # {strategy_name: reserved_pct}
        self._lock = threading.Lock()
        self._max_total = config.get_dotted("strategies.coordinator.max_total_pct", 0.90)
        self._caps = {
            "main":     config.get_dotted("strategies.main.capital_pct", 0.65),
            "scalping": config.get_dotted("strategies.scalping.capital_pct", 0.25),
            "fast_lane":config.get_dotted("strategies.fast_lane.capital_pct", 0.20),  # 来自 main 内部分配
            "llm_plan": 0.0,  # LLM plan 不直接持仓, 只是给 main 提供 watchlist
        }

    def try_reserve(self, strategy: str, position_pct: float) -> bool:
        with self._lock:
            current = self._allocations.get(strategy, 0.0)
            cap = self._caps.get(strategy, 0.0)
            # 单策略上限
            if current + position_pct > cap:
                return False
            # 全局上限
            if sum(self._allocations.values()) + position_pct > self._max_total:
                return False
            self._allocations[strategy] = current + position_pct
            return True

    def release(self, strategy: str, position_pct: float):
        with self._lock:
            self._allocations[strategy] = max(0.0, self._allocations.get(strategy, 0.0) - position_pct)

    def snapshot(self) -> Dict[str, float]:
        """供 daily_review / dashboard 读"""
        return dict(self._allocations)
```

**配置** (新增到 `unified_config.yaml`):
```yaml
strategies:
  coordinator:
    max_total_pct: 0.90
  main:
    capital_pct: 0.65
    priority: 5
  scalping:
    capital_pct: 0.25
    priority: 10           # 高于 main, 因为时效性强
  fast_lane:
    capital_pct: 0.20      # 在 main 的 0.65 内分配
    priority: 8
```

### 3.3 SymbolLockManager (按用户选择: 优先级 + confidence)

```python
# 新增: app/coordinator/symbol_lock.py
class SymbolLockManager:
    """
    同一 symbol 同时只能被 1 个 strategy 持仓.
    多策略竞争时按 priority + confidence 仲裁 (用户决策 2026-05-14).
    """
    def __init__(self):
        self._locks = {}  # {symbol: (strategy_name, acquired_at)}
        self._lock = threading.Lock()

    def acquire(self, symbol: str, strategy: str) -> bool:
        with self._lock:
            if symbol not in self._locks:
                self._locks[symbol] = (strategy, time.time())
                return True
            return self._locks[symbol][0] == strategy  # 同 strategy 重入 OK

    def release(self, symbol: str):
        with self._lock:
            self._locks.pop(symbol, None)

    def owner(self, symbol: str) -> Optional[str]:
        return self._locks.get(symbol, (None,))[0]
```

### 3.4 SignalArbitrator (priority + confidence)

```python
# 新增: app/coordinator/signal_arbitrator.py
class SignalArbitrator:
    """
    多策略对同 symbol 同时出信号 → 仲裁出唯一 winner.

    评分 = strategy.priority × signal.confidence
    平手按 strategy.priority 决断.
    """
    def arbitrate(
        self, candidates: List[Tuple[BaseStrategy, SignalScore]]
    ) -> Optional[Tuple[BaseStrategy, SignalScore]]:
        if not candidates:
            return None

        scored = []
        for strat, sig in candidates:
            score = strat.priority * sig.confidence
            scored.append((score, strat.priority, strat, sig))

        scored.sort(key=lambda x: (-x[0], -x[1]))
        winner = (scored[0][2], scored[0][3])

        # 写仲裁审计 (用于复盘"被压制的信号是否本可赚"分析)
        self._record_arbitration(candidates, winner)
        return winner
```

**审计表** (新增):
```sql
CREATE TABLE signal_arbitration (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    winner_strategy TEXT NOT NULL,
    winner_score    REAL,
    candidates_json TEXT,  -- 所有候选信号详情
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
```

### 3.5 ExitDispatcher (按入场策略路由退出规则)

```python
# 新增: app/coordinator/exit_dispatcher.py
class ExitDispatcher:
    """
    按 position.owner_strategy 路由到对应 ExitPolicy.

    替代当前 PositionExitMonitor 的"6 规则不分入场"现状.
    F16 (入场→退出 thesis 对称) 的实现底座.
    """
    def __init__(self):
        self._policies = {}  # {strategy_name: ExitPolicy}

    def register(self, strategy_name: str, policy: "ExitPolicy"):
        self._policies[strategy_name] = policy

    def policy_for(self, owner_strategy: str) -> "ExitPolicy":
        if owner_strategy not in self._policies:
            # 兜底: 用主线策略
            return self._policies["main"]
        return self._policies[owner_strategy]
```

**ExitPolicy 接口**:
```python
# 新增: app/strategies/exit_policy.py
class ExitPolicy(Protocol):
    name: str

    def evaluate(self, position: Position, ctx: MarketContext) -> ExitDecision:
        """
        Returns:
            ExitDecision(action="NO_ACTION" | "REDUCE" | "CLOSE",
                         qty_pct=0-1,
                         reason="DAYTRADE_TP1" | "FLASH_EXIT" | ...,
                         priority=int  # 多规则触发时取高的)
        """
```

各策略提供自己的 ExitPolicy 实现:
- `MainPipelineExitPolicy` → wrap 现有 `PositionExitMonitor` 的 day_trade_mode 规则
- `ScalpingExitPolicy` → wrap `app/scalping/exits.py`
- `FastLaneExitPolicy` → 紧 ATR 短线 (复用 main 的 chandelier 但参数更紧)
- `LLMPlanExitPolicy` → 按 plan 中的 stop/target 直接退出

---

## 4. L1 Shared Kernel 增强

### 4.1 PositionRegistry + owner_strategy

**Schema 变更** (幂等 ALTER):
```sql
ALTER TABLE open_positions ADD COLUMN owner_strategy TEXT;
ALTER TABLE trade_records  ADD COLUMN owner_strategy TEXT;
-- index
CREATE INDEX idx_pos_owner ON open_positions(owner_strategy);
CREATE INDEX idx_trd_owner ON trade_records(owner_strategy);
```

**填值时机**:
- 开仓时 `Position(..., owner_strategy=strat.name)`
- 平仓时 `trade_records.owner_strategy = position.owner_strategy`
- 老数据 (无 owner_strategy) 通过 `source_type` 推断, 或标 "legacy"

### 4.2 RiskKernel 抽象 (Phase B)

把现有散落的 `pre_trade_check / circuit_breaker / drawdown_control / pdt_guard` 聚成 `RiskKernel` 门面:

```python
# 新增: app/risk/risk_kernel.py
class RiskKernel:
    """
    风控统一门面. 不重写, 只代理.
    """
    def __init__(self, ...):
        self.pre_trade = PreTradeChecker(...)
        self.circuit_breaker = ...
        self.drawdown = ...
        self.pdt_guard = ...
        self.state_machine = ...
        self.budget_check = ...  # F10 的 _check_trade_risk_within_budget

    def pre_trade_check(self, signal, strategy_context) -> RiskResult:
        for check in [self.state_machine, self.circuit_breaker, self.pdt_guard,
                      self.budget_check, self.pre_trade]:
            r = check.check(signal, strategy_context)
            if not r.allowed:
                return r
        return RiskResult(allowed=True)
```

---

## 5. L4 ExecutionMode (Shadow 复用)

### 5.1 OrderGateway 抽象

```python
# 新增: app/execution/order_gateway.py
class OrderGateway(Protocol):
    name: str  # "live" | "shadow_v1" | "backtest"

    def submit(self, order: Order) -> OrderResult: ...
    def cancel(self, order_id: str) -> bool: ...
    def get_fills(self) -> List[Fill]: ...

# Adapter 1: 实盘
class RealOrderGateway:
    def __init__(self, futu_ctx, account):
        self.futu = futu_ctx
    def submit(self, order):
        # 现有 _submit_order 逻辑搬到这里
        ...

# Adapter 2: Shadow (本设计核心)
class ShadowExecutor:
    """
    用真实 quote 模拟 fill, 写到 shadow_orders 表.
    不占用真实 Futu 槽位 (除了订阅 quote).
    """
    def __init__(self, name: str, params: Dict, slippage_model):
        self.name = name
        self.params = params  # 该 shadow 实例的参数覆写
        self.slippage = slippage_model

    def submit(self, order):
        quote = self.market_data.get_quote(order.symbol)
        fill_price = self.slippage.apply(quote, order)
        # 写到 shadow_orders
        ...
        return OrderResult(filled=True, avg_fill_price=fill_price, ...)
```

### 5.2 Coordinator 多 Executor 注入

按用户决策 (实盘 + 1 shadow), 起步配置:

```python
# main.py 启动时
coordinator_live = StrategyCoordinator(
    registry=registry,
    order_gateway=RealOrderGateway(futu_ctx),
    ...
)
coordinator_shadow = StrategyCoordinator(
    registry=registry_with_param_override(  # 同一批 strategy, 不同参数
        {"main.min_confidence": 0.6},  # 例: 测试更高门槛
    ),
    order_gateway=ShadowExecutor("shadow_v1", params={...}),
    ...
)

# 主循环并行调度
for symbol in universe:
    coordinator_live.run_open_cycle(symbol)
    coordinator_shadow.run_open_cycle(symbol)
```

**资源共享**:
- Futu quote 订阅 1 份 (两个 coordinator 都从 MarketDataRouter 取 cache)
- Risk 状态机 / 资金账本各自独立 (shadow 有自己的 VirtualAccount, 已存在)
- `shadow_daily_results` / `shadow_orders` 已存在 (`docs/SHADOW_IMPLEMENTATION_REPORT.md`)

### 5.3 Shadow 与现有 `app/evolution/shadow_tracker.py` 关系

| 现状 | 本设计后 |
|-----|---------|
| `shadow_tracker.py` 把主线信号"分流"到 shadow, 用旧/新参数对比 | `ShadowExecutor` 是 OrderGateway 实现, Coordinator 完全无感知 |
| 仅覆盖主线 | 4 条策略全部走 shadow (统一 OrderGateway 注入) |
| 参数差异手动配 | 通过 strategy.params 覆写, ConfigWriter 整合 |
| 统计验证 (Bonferroni / t-test) | **保留, 复用** (`promotion_engine.py` 的核心) |

→ **`shadow_tracker.py` 不删, 改造为 ShadowExecutor 的统计后端**: ShadowExecutor 调 `shadow_tracker.record(...)` 写每日数据, `promotion_engine` 仍然负责晋升判断。

---

## 6. 文件级变更清单

### 6.1 新增文件 (Phase A)

```
app/coordinator/
  __init__.py
  strategy_coordinator.py    # §3.1
  capital_allocator.py       # §3.2
  symbol_lock.py             # §3.3
  signal_arbitrator.py       # §3.4
  exit_dispatcher.py         # §3.5

app/strategies/              # 新目录, 与 app/strategy/ 区分 (前者: 适配器, 后者: 已有基类)
  __init__.py
  base_adapter.py            # 把 BaseStrategy 包成 Coordinator 接受的形态
  exit_policy.py             # ExitPolicy Protocol + 4 个实现

  adapters/
    main_pipeline.py         # wraps signal_pipeline + intraday_scorer
    scalping.py              # wraps app/scalping/
    fast_lane.py             # wraps app/strategy/pure/fast_lane
    llm_plan.py              # wraps app/us_pipeline/

app/execution/               # 新目录
  __init__.py
  order_gateway.py           # OrderGateway Protocol
  real_gateway.py            # 现有 _submit_order 搬过来
  shadow_executor.py         # 新, 复用现有 SlippageModel

app/risk/risk_kernel.py      # Phase B, 不在 Phase A 范围
```

### 6.2 修改文件 (Phase A)

```
app/storage/trade_store.py
  - ALTER TABLE 加 owner_strategy 列 (open_positions + trade_records)

app/core/scheduler_core.py
  - main loop 改为调 coordinator.run_open_cycle / run_exit_cycle
  - 不再各自调度

app/strategy/registry.py
  - 增加 strategy.priority / capital_pct 字段读取
  - 增加 active_strategies(market_phase=...) 方法

app/risk/position_exit_monitor.py
  - DEPRECATED 标记 (Phase B 由 ExitDispatcher 替代)
  - Phase A 暂保留, 等 ExitDispatcher 全覆盖再删
```

### 6.3 配置变更

```yaml
# unified_config.yaml 新增节点
strategies:
  coordinator:
    enabled: true            # Phase A 灰度开关
    max_total_pct: 0.90
    arbitration_audit: true
  main:
    capital_pct: 0.65
    priority: 5
  scalping:
    capital_pct: 0.25
    priority: 10
  fast_lane:
    capital_pct: 0.20
    priority: 8
  llm_plan:
    capital_pct: 0.0
    priority: 3
```

---

## 7. 分期实施计划

### Phase A — 抽象层 + Capital Ledger (本周, 不改业务)
**目标**: 协调层接管调度, 但每个策略仍跑原逻辑. 引入 owner_strategy 数据.

1. 新建 `app/coordinator/` 5 个文件 (代码骨架)
2. 新建 `app/strategies/` 4 个 Adapter (空实现, 内部调原模块)
3. ALTER trade_store 加 owner_strategy
4. scheduler_core.py 主循环切到 coordinator (灰度: `strategies.coordinator.enabled` 开关)
5. CapitalAllocator + SymbolLock 仅观察记录, 不强制 (`enforce: false` 模式)
6. 写 5 条单测覆盖核心冲突场景

**验收**:
- [ ] `coordinator.enabled=true` 后跑一交易日, 全部 trade_records.owner_strategy 非空
- [ ] daily_review 出 `capital_allocation` 字段, 各 strategy 资金账本与 PositionRegistry 加总一致 (±$1)
- [ ] 5 条单测全绿
- [ ] 无新失败的现有单测

### Phase B — ExitDispatcher + 强制冲突仲裁 (下周)
**目标**: ExitDispatcher 接管退出. SymbolLock + SignalArbitrator 切到 enforce 模式.

1. ExitDispatcher 上线, 实现 4 个 ExitPolicy adapter
2. PositionExitMonitor 改为调用 ExitDispatcher (兼容期同时存在)
3. SignalArbitrator 切 enforce, 写 signal_arbitration 表
4. SymbolLock 切 enforce
5. CapitalAllocator 切 enforce
6. F16 thesis-driven exit 在此基础上启动

**验收**:
- [ ] 构造 Main + Scalping 同时盯 NVDA → 只 1 笔成交, 另一笔写 rejection
- [ ] 资金溢出测试: 持仓 0.85, 新信号 pos=0.10 → REJECTED("CAPITAL_CAP")
- [ ] 30 天 daily_review: exit_reason 与 owner_strategy 强相关 (Cramer's V > 0.4)

### Phase C — ExecutionMode + Shadow 复用 (第 3 周)
**目标**: OrderGateway 抽象, Shadow 跑全部 4 策略.

1. RealOrderGateway 替代现有 `_submit_order`
2. ShadowExecutor 实现, 接 `shadow_tracker.py` 统计后端
3. main.py 启动 live + 1 shadow coordinator
4. daily_review 增加 "live vs shadow PnL 对比" 段
5. F29 backtest 框架的 BacktestExecutor 自然落入此层

**验收**:
- [ ] 24h 后 shadow_daily_results 有 N 行 (N = 当日交易数 × 2)
- [ ] live PnL 与 shadow PnL 偏差 < 滑点模型预期上限
- [ ] promotion_engine 能按现有规则判断 shadow 参数是否优于 live

### Phase D — 死代码清理 + 文档 (第 4 周)
- 删除 PositionExitMonitor 兼容层
- 各策略原 main loop 入口 (scalping/engine.py 的独立调度) 删
- F25/F26 deep_review dead code 顺便清
- 出 `docs/strategy_runbook.md` 教如何加新策略

---

## 8. 兼容性 / 风险 / 回滚

### 8.1 兼容性
- **配置**: 旧 `risk_limits.max_total_position` 保留, 新 `strategies.coordinator.max_total_pct` 优先
- **数据库**: 全部 ALTER 幂等, 老 DB 升级即用
- **API**: BaseStrategy 不动, 现有 4 条策略 0 修改 (Adapter 包在外面)

### 8.2 关键风险
| 风险 | 应对 |
|-----|------|
| Coordinator 引入死锁 (CapitalLock + SymbolLock 双锁) | 严格按"先 Capital 后 Symbol"顺序, 单测覆盖 |
| Adapter 层增加 latency (信号→下单延迟) | Phase A 后基准测试, 上限 50ms (90 分位) |
| Shadow 占用过多 quote 订阅 | 共享 MarketDataRouter cache, 不重复订阅 |
| Old code 还在调旧 PositionExitMonitor | Phase B 兼容期检查所有调用点, Phase D 才删 |
| 灰度开关失效导致全量切换 | `strategies.coordinator.enabled` 默认 false, 显式开启 |

### 8.3 回滚
- 任何 Phase 失败 → `strategies.coordinator.enabled: false` → 立即回到原调度
- ConfigWriter 已支持回滚 (`rollback_param`), 任何参数变更都可一键退回

---

## 9. 与现有 33 项 Fix 的关系

| Fix 编号 | 关系 |
|---------|------|
| F10 风险单位 R | RiskKernel 集成 budget_check |
| F13/F22 exit_reason×pnl | ExitDispatcher 落地后, by_owner_strategy 维度自动可查 |
| F16 入场→退出 thesis 对称 | **直接由 ExitPolicy 接口实现** |
| F17 组合层面止损 | RiskKernel 内置 |
| F25/F26 deep_review 死码 | Phase D 清理 |
| F27 ConfigWriter 扩白名单 | 已有, 本设计支持 strategies.* 配置 |
| F29 backtest 框架 | **BacktestExecutor 自然落入 L4**, 不再单建一套 |
| F30 walk-forward | Shadow 提供"实时 walk-forward", 与 backtest 互补 |

---

## 10. 待确认 (评审问题)

1. **Adapter 命名**: `app/strategies/adapters/main_pipeline.py` vs `app/strategies/main_pipeline_adapter.py` — 哪个更符合现有命名习惯?
2. **Phase A 灰度开关默认值**: 默认 `enabled: true` 直接接管, 还是 `false` 需手动开?
3. **owner_strategy 老数据补回**: 通过 source_type 反推, 还是统一标 "legacy"?
4. **Shadow 实例参数差异**: 由 ConfigContainer 多 profile 支持 (新增 `unified_config_shadow_v1.yaml`), 还是 strategy.params 运行时覆写?
5. **ExitDispatcher 兜底**: 当 owner_strategy 缺失时用 main 的 ExitPolicy, 还是用最严的 (FlashExit + ATR stop)?

---

**结束.** 如批准, 进入 Phase A 实施, 预计 5 个工作日完成代码骨架 + 5 条核心单测 + 灰度上线.
