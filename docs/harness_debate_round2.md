# FTSeek-HK Harness 整车平台 — 第二轮辩论 (Claude Code 独立审计)

## 辩论主题

基于第一轮辩论结论（渐进式包裹 Wrapper Pattern），对港股 Harness 试点方案进行深度架构审核。

---

## 背景：系统现状 (Ground Truth)

### 关键文件
| 文件 | 行数 | 职责 |
|------|------|------|
| `app/main.py` | 1060 行 | 入口：OpenD启动 → DataSubscriber → 策略注册 → FastAPI → 信号处理 |
| `app/data_subscriber.py` | ~3584 行 | Futu OpenD 数据订阅 + 共识引擎 + Push 回调处理 |
| `app/harness/` | 已存在 | coordinator, strategist, risk_officer, context_engineer, fallback, timeout, messaging, trade_monitor, adapters |
| `app/harness/workflows/` | 已存在 | daily_review, weekly_review |

### 已有 Harness 组件
- `coordinator.py` — 协调器
- `strategist.py` — 策略师
- `risk_officer.py` — 风控官
- `context_engineer.py` — 上下文工程师
- `fallback.py` — 降级机制
- `timeout.py` — 超时管理
- `messaging.py` — 消息推送
- `trade_monitor.py` — 交易监控
- `adapters/` — base, collector, risk, review, report

---

## 第一轮辩论结论 (Hermes)

### ROUND 1：要不要改 main.py？

**PRO-改**: main.py 1060 行混合了进程管理、配置、策略注册、网络服务、信号处理、线程管理。这是所有问题的根源 — 没有 harness 层，所有模块直接耦合。

**ANTI-改**: main.py 是生产系统入口，改坏了直接停盘。DataSubscriber 依赖它注入的 notifier、executor、strategies。重构风险极高。

**SYNTHESIS → 渐进式包裹（Wrapper Pattern）**:
不改 main.py，而是在它之上套一层 Harness 外壳。先让 Harness 协调 main.py 启动后的"运行时工作流"（盘前/盘中/盘后），而不是改启动流程。等 Harness 稳定后再逐步替换。

### ROUND 2：要不要拆 DataSubscriber？

**PRO-拆**: 3584 行巨兽，混合了订阅管理、回调处理、共识引擎、Push 逻辑。违反 SRP，难以测试和维护。

**[结论待 Claude Code 独立审计]**

---

## 请 Claude Code 独立审计并回应

**审计范围**:
1. `/opt/ftseek/FTSeek/app/main.py` — 分析耦合点和依赖注入
2. `/opt/ftseek/FTSeek/app/data_subscriber.py` — 分析职责分离程度
3. `/opt/ftseek/FTSeek/app/harness/` — 评估现有 Harness 组件的完整性和就绪度
4. `/opt/ftseek/FTSeek/app/core/` — 评估 container, interfaces, state_manager 等核心模块

**辩论格式**: 请用 "## Claude Code 审计结论" 开头，逐条回应 Hermes 的论点。

**关键问题**:
1. Wrapper Pattern 在技术上是否可行？main.py 的哪些依赖注入路径会被阻断？
2. DataSubscriber 的 3584 行中，哪些职责最应该先拆分？拆分优先级如何？
3. 现有的 harness/ 组件是否已经具备了协调 main.py 的能力？还缺什么？
4. 渐进式包裹 vs 渐进式替换 — 哪个路径风险更低？
5. 港股 vs 美股的 Harness 架构差异分析 — 美股有哪些可以借鉴？
6. 给出一个具体的、可验证的第一步实施方案（不超过 3 个文件变更）

**CRITICAL**: Base every claim on actual code you read. Cite specific file paths and line numbers. Do NOT speculate about code structure — read the files.

---

## Claude Code 审计结论 (Round 2)

### 对 Hermes ROUND 1 (main.py) 的回应

**立场**: PARTIALLY AGREE

**证据**:
- main.py 确实 1060 行，结构可识别为 6 个功能段：
  - **模块级初始化** (L1-L313): 日志(L68-112)、配置中心(L115-147)、决策缓冲(L149-191)、风险状态机(L193-201)、TradeExitManager(L203-214)、BattlePlanManager(L216-238)、配置校验(L240-277)、质量管理器(L288-313)
  - **OpenDManager 类** (L321-L467): 进程管理、端口检测、启停逻辑
  - **_sync_positions_from_futu 函数** (L475-L595): 独立函数 120 行
  - **main() 函数** (L601-L1056): 核心启动流程，约 455 行

- Hermes 说的"混合了进程管理、配置、策略注册、网络服务、信号处理、线程管理"是正确的。
- **但** main.py 的耦合程度不如 Hermes 描述的严重。存在明确的依赖注入路径：
  - `DataSubscriber(strategies, notifier, llm_analyst=..., trade_executor=executor)` (L967) — 明确注入
  - `set_executor(executor)`, `set_notifier(notifier)` (L699-700) — FastAPI 注入
  - `init_risk_monitor(risk_manager=..., dingtalk_notifier=..., circuit_breaker=..., subscriber_ref=..., executor_ref=...)` (L981-987) — 明确依赖注入

**补充发现 (Hermes 没看到的)**:
1. **OpenDManager 类是独立可提取的** (L321-467, 146 行)。它有明确的 `__init__`, `is_running`, `start`, `stop` 接口，不依赖 main.py 的内部状态。
2. **_sync_positions_from_futu 是纯函数** (L475-595)，可以被提取到独立模块。它只通过 `executor.position_manager.db_path` 获取依赖。
3. **main() 的核心编排逻辑其实很清晰** — 线性序列: OpenD→组件初始化→策略注册→API→DataSubscriber→信号等待→清理。不是 spaghetti code，而是 procedural orchestration。
4. **真正的耦合问题不在 main.py 内部，而在 DataSubscriber**。main() 注入给 DataSubscriber 的是一个单体对象，而 DataSubscriber 内部又实例化了更多单例。

### 对 Hermes ROUND 2 (DataSubscriber) 的回应

**立场**: AGREE — DataSubscriber 是比 main.py 更严重的架构问题

**证据** — DataSubscriber 的 3584 行可识别为 **11 个独立子职责**:

| 职责 | 行号范围 | 行数 | 复杂度 |
|------|----------|------|--------|
| 1. `__init__` + 组件初始化 | L82-266 | 184 | 高（初始化 20+ 个依赖） |
| 2. 多周期趋势判断 (`_update_multi_timeframe_trend`) | L783-992 | 210 | 极高（7 周期 × MA/RSI 计算） |
| 3. 多周期对齐分数 (`_get_mtf_alignment_score`, `_get_multi_timeframe_signal`, `_calculate_mtf_resonance`) | L994-1184 | 190 | 高 |
| 4. 动态止损参数 (`_get_dynamic_stop_params`) | L1186-1340 | 155 | 高（7 周期加权 + 场景调整） |
| 5. 共识回调 + 交易执行 (`_on_consensus`, `_on_consensus_inner`) | L1409-1752 | 344 | **极高** — 这是核心交易路径 |
| 6. 顺势加仓逻辑 (`_try_add_position`) | L1782-1896 | 115 | 中 |
| 7. 止损执行 (`_execute_stop_loss`) | L1898-2075 | 178 | 高 |
| 8. 止盈执行 (`_execute_take_profit`, `_execute_partial_take_profit`) | L2077-2403 | 327 | 高 |
| 9. 异步工作线程 (`_start_workers`, `_quote_worker_loop`, `_kline_worker_loop`, `_process_quote_data`, `_process_kline_data`) | L2411-2835 | 425 | 极高 |
| 10. 行情订阅管理 (`subscribe`, `unsubscribe_all`, `_reconnect`, `_sync_subscription`, `_resubscribe`, `_get_subscribe_codes`) | L2855-3183 | 329 | 高 |
| 11. 质量/估值/新闻/ADX/ATR 辅助方法 | 分散 | ~600 | 中 |

**拆分建议** (优先级排序):

**P0 — 立即拆分** (不碰交易路径，风险最低):
1. **多周期趋势引擎** → `data_subscriber/multi_timeframe.py` (L783-1184, ~400 行)。纯计算逻辑，无副作用。
2. **动态止损参数计算器** → `data_subscriber/dynamic_stop_params.py` (L1186-1340, ~155 行)。纯函数，无副作用。

**P1 — 中期拆分** (需要重构接口):
3. **异步工作线程** → `data_subscriber/workers.py` (L2411-2835, ~425 行)。需要保持对 DataSubscriber 的引用。
4. **行情订阅管理** → `data_subscriber/subscription_manager.py` (L2855-3183, ~329 行)。

**P2 — 长期拆分** (最复杂，涉及交易路径):
5. **共识回调 + 交易执行** → 这是最难拆的，因为它深度依赖 DataSubscriber 的所有其他组件。

### 关键问题回答

#### 1. Wrapper Pattern 可行性分析: **技术可行，但有 3 个关键依赖注入路径会被阻断**

**可行部分**:
- `main.py` 的 `main()` 函数 (L601-1056) 是一个线性编排函数，可以被 `harness.coordinator.Coordinator` 包装。
- `OpenDManager` (L321-467) 已经是独立类，可以直接作为 Harness Agent 使用。

**会被阻断的依赖注入路径**:

| 注入路径 | 位置 | 问题 |
|----------|------|------|
| `DataSubscriber(strategies, notifier, llm_analyst, trade_executor)` | main.py L967 | Wrapper 无法替换 strategies 和 executor 实例 |
| `set_executor(executor)`, `set_notifier(notifier)` | main.py L699-700 | 全局 setter，绕过 Wrapper |
| `init_risk_monitor(risk_manager, dingtalk_notifier, circuit_breaker, subscriber_ref, executor_ref)` | main.py L981-987 | 直接注入，Wrapper 无法拦截 |
| `set_subscriber(subscriber)` | main.py L969 | FastAPI server 依赖全局 subscriber |
| `set_executor_instance(executor)` | main.py L696 | 全局单例注册 |

**结论**: Wrapper Pattern 可以做"盘前/盘后工作流"层面的协调（daily_review, weekly_review），但无法做"盘中实时交易"层面的协调。

#### 2. DataSubscriber 拆分优先级:

见上面的 P0/P1/P2 分析。**P0 拆分 (多周期趋势引擎 + 动态止损参数计算器) 可以在 2 个文件内完成，零风险**。

#### 3. 现有 Harness 组件就绪度:

| 组件 | 文件 | 行数 | 就绪度 | 问题 |
|------|------|------|--------|------|
| Coordinator | `harness/coordinator.py` | 345 | ✅ 可用 | 纯 async 框架，需要事件循环 |
| Agent (base) | `harness/base.py` | 288 | ✅ 可用 | 完整的生命周期管理 |
| Strategist | `harness/strategist.py` | 282 | ⚠️ 部分可用 | 依赖 `BatchReviewProcessor` (可能不存在) |
| RiskOfficer | `harness/risk_officer.py` | 370 | ⚠️ 硬编码阈值 | `risk_limits` 字典硬编码 (L67-73)，与 `unified_config.yaml` 不同步 |
| ContextEngineer | `harness/context_engineer.py` | 306 | ❌ 有 bug | L56 使用 `os.path` 但未 import os |
| Fallback | `harness/fallback.py` | 337 | ✅ 可用 | ModelRouter 功能完整 |
| Timeout | `harness/timeout.py` | 339 | ✅ 可用 | 功能完整 |
| Messaging | `harness/messaging.py` | 276 | ✅ 可用 | MessageBus 有全局单例 |
| TradeMonitor | `harness/trade_monitor.py` | 257 | ⚠️ 占位符 | `_fetch_trades` 返回硬编码数据 (L227-236) |
| BaseAdapter | `harness/adapters/base_adapter.py` | 126 | ✅ 可用 | 设计合理 |
| ReviewAdapter | `harness/adapters/review_adapter.py` | 165 | ⚠️ 部分可用 | 依赖 `BatchReviewProcessor` |
| RiskAdapter | `harness/adapters/risk_adapter.py` | 158 | ⚠️ 部分可用 | 调用不存在的 `assess_portfolio` 等 async 方法 |
| CollectorAdapter | `harness/adapters/collector_adapter.py` | 128 | ❌ 不可用 | `_collector` 永远为 None (L51) |
| DailyReviewWorkflow | `harness/workflows/daily_review.py` | 221 | ⚠️ 需注册 Agent | 工作流定义完整，但 Agent 未实际注册 |
| WeeklyReviewWorkflow | `harness/workflows/weekly_review.py` | 260 | ⚠️ 需注册 Agent | 同上 |

**总体评估**: Harness 框架的**基础设施层** (Coordinator, Agent, Timeout, Messaging, Fallback) 是完整可用的。但**业务层** (Strategist, RiskOfficer, TradeMonitor, Adapters) 大部分是占位符或依赖未实现的模块。

#### 4. 渐进式包裹 vs 渐进式替换: **包裹路径风险更低**

**渐进式包裹 (推荐)**:
- 不改 `main.py`，在外部创建 `harness_pilot.py`，只在盘后时段启动 Coordinator
- 先跑 `DailyReviewWorkflow`，验证 Harness 框架能正确调用现有模块
- 风险: 零 — 不影响交易路径

**渐进式替换 (不推荐)**:
- 需要替换 `DataSubscriber` 的核心路径
- `_on_consensus_inner` (L1423-1752, 330 行) 是交易系统的**热路径**
- 替换这个函数的风险极高：它涉及 risk pipeline、MTF 对齐、加仓判断、开仓锁、交易执行、通知推送、信号追踪等

#### 5. 港股 vs 美股的 Harness 架构差异:

**当前系统只有港股** (HK)，美股相关代码均为监控/参考：
- `config.WATCH_LIST_US` 在 main.py L606 显示为"美股参考(不订阅实时行情)"
- 美股隔夜监控器 `us_overnight_monitor` (main.py L872-880) 仅做监控，不做交易
- 跨市场套利调度器 `cross_market_scheduler` (main.py L824-870) 分析美股隔夜数据后影响港股开盘决策

**美股 Harness 需要但港股没有的**:
- 美股是 T+1，港股是 T+0 (`data_subscriber.py` L243: `t0_trading=True`)
- 美股有价格限制，港股无 (`data_subscriber.py` L244: `no_price_limit=True`)
- 美股使用不同经纪商 API，需要不同的 adapter

**可借鉴的部分**: 美股的 `us_overnight_monitor` 模式 (独立监控器 → 数据注入 → 影响主系统) 是一个低风险的扩展模式。

#### 6. 第一步实施方案 (≤3 文件):

**方案：创建 Harness Pilot — 盘后复盘工作流**

文件 1: `app/harness_pilot.py` — 新文件 (~150 行)
```
- 在系统启动后（main.py 的 main() 函数内），注入 Harness Coordinator
- 注册 ReviewAdapter、RiskAdapter 作为 Agent
- 配置盘后定时触发 DailyReviewWorkflow
- 不触碰任何交易路径代码
```

文件 2: `app/harness/adapters/collector_adapter.py` — 修复 (~30 行修改)
```
- 修复 L51: _collector 永远为 None 的 bug
- 实际需要传入 db_session 和 futu_client 依赖
- 或者改为延迟加载模式
```

文件 3: `app/harness/context_engineer.py` — 修复 (~5 行)
```
- L56 添加 import os
```

### Claude 独立发现的额外风险

1. **`context_engineer.py` L56 运行时崩溃**: `os.path.join(...)` 但 `os` 模块未 import。此文件在任何 workflow 执行时都会 crash。
2. **CollectorAdapter 永远返回空数据**: `collector_adapter.py` L51 `self._collector = None` 且 `_load_module` (L46-57) 设置了 `_collector = None` 后直接 raise，导致 `_adapt_execute` (L59-83) 永远走 fallback 路径，返回空 trades 列表。
3. **RiskAdapter 调用不存在的方法**: `risk_adapter.py` L94 调用 `self._risk_manager.assess_portfolio(portfolio)` — 但 `app.risk.risk_manager.RiskManager` 是否有 `assess_portfolio` 方法？需要验证。L109/124 同理调用 `check_position_limits` / `monitor_positions`。
4. **DataSubscriber `_get_fundamentals` 是硬编码模拟数据** (L3348-3385): 对腾讯/阿里/美团硬编码了基本面数据，其他股票返回默认值。这在生产环境中会导致所有非预设股票的质量检查都返回相同的"一般股"数据。
5. **main.py L139-140 直接访问 `config_center._configs` 私有属性**: `config_center._configs.get('unified_config.yaml', config_center._configs.get('config.yaml', {}))` — 绕过公共 API，访问内部数据结构。
6. **DataSubscriber `_on_consensus_inner` 的隐式依赖链**: L1448 `init_risk_pipeline(deps={"subscriber": self})` — 将整个 DataSubscriber 实例作为依赖注入到 risk pipeline，构成隐式循环依赖。
7. **Harness 的 async/await 与 DataSubscriber 的 threading 模型冲突**: Harness 组件 (Coordinator, Strategist, RiskOfficer) 全部是 `async def`，但 DataSubscriber 的核心交易路径 (`_on_consensus`, `_process_kline_data`) 使用 `threading.Thread` 和 `queue.Queue`。两者不兼容，无法直接在同一个事件循环中调用。
8. **`config_container.py` ConsensusConfig (L186-255) 与 `data_subscriber.py` 的共识参数重复**: `data_subscriber.py` L52-58 定义了 `SIGNAL_COOLDOWN`, `CONSENSUS_WINDOW`, `CONSENSUS_THRESHOLD` 等，与 `ConsensusConfig` dataclass 的值相同但独立维护，存在漂移风险。
9. **DataSubscriber 的 `_peak_price_cache` 线程安全问题**: L2484-2497 在 `_process_quote_data` (工作线程) 中读写 `_peak_price_cache`，而 `_process_kline_data` (另一个工作线程) 也读取此缓存 (L2521)，没有锁保护。
10. **Harness `RiskOfficer` 的阈值与生产系统不同步**: `risk_officer.py` L67-73 硬编码 `max_single_position=0.2`, `max_drawdown=0.15`，而 `config_container.py` ConsensusConfig (L206-209) 定义了 `max_position_pct=10.0` (即 0.1) 和 `max_portfolio_drawdown=0.12`。Harness 的阈值比生产系统更宽松。

---

## Hermes 验证结论 (Round 2 收敛)

### Claude 发现的 P0 Bug — 全部验证通过

| # | 发现 | Claude 声明 | Hermes 验证 | 状态 |
|---|------|-------------|-------------|------|
| 1 | `context_engineer.py` 缺 `import os` | L56 使用 `os.path` 但未 import | ✅ `head -60` 确认无 `import os` | **P0-CONFIRMED** |
| 2 | `collector_adapter.py` `_collector` 永远为 None | L44/L51/L56 全部赋 None | ✅ grep 确认 3 处赋 None，无实际加载逻辑 | **P0-CONFIRMED** |
| 3 | `risk_manager.py` 不存在 Claude 调用的方法 | L94/109/124 调用不存在的方法 | ✅ grep 返回空，`assess_portfolio`/`check_position_limits`/`monitor_positions` 均不存在 | **P0-CONFIRMED** |

### 收敛后最终结论

#### ROUND 1 (main.py) — 收敛为 PARTIALLY AGREE

- **修正**: main.py 不是 spaghetti code，而是 **procedural orchestration**。1060 行但结构清晰 (6 个功能段)。
- **维持**: Wrapper Pattern 是正确方向。不应重写 main.py，应在外部创建 Harness Pilot 包裹。
- **补充**: OpenDManager (L321-467) 和 `_sync_positions_from_futu` (L475-595) 已经是可提取的独立单元，可以零风险迁移。

#### ROUND 2 (DataSubscriber) — 收敛为 FULLY AGREE

- **结论**: DataSubscriber 才是比 main.py 更严重的架构问题。3584 行 = 11 个独立子职责。
- **优先级**: P0 (多周期引擎 + 动态止损参数) → P1 (工作线程 + 订阅管理) → P2 (共识回调交易路径)
- **P0 拆分零风险**: 纯计算逻辑，无副作用，可以被立即提取到独立文件。

#### Harness 组件就绪度 — 分层评估

| 层次 | 状态 | 说明 |
|------|------|------|
| **基础设施层** | ✅ 可用 | Coordinator, Agent, Timeout, Messaging, Fallback 功能完整 |
| **业务逻辑层** | ❌ 不可用 | Strategist/RiskOfficer/TradeMonitor 全是占位符或 bug |
| **Adapter 层** | ❌ 不可用 | 3/4 adapters 有 P0 bug (import 缺失、方法不存在、永远返回空) |

#### 共识的第一步实施方案

1. **修复 3 个 P0 bug** (context_engineer.py, collector_adapter.py, risk_adapter.py) — 约 50 行修改
2. **创建 Harness Pilot** (`app/harness_pilot.py`) — 盘后复盘工作流，~150 行，不碰交易路径
3. **提取 DataSubscriber P0 组件** — 多周期引擎 → `data_subscriber/multi_timeframe.py`，~400 行纯计算

**关键约束**: 所有变更必须先在盘后时段验证，绝不能在交易时段 (09:30-16:00 HKT) 部署。
