# FTSeek-US 系统深度审查报告

> 审查日期：2026-06-17
> 审查范围：全链路 — 数据采集 → 信号生成 → 多空辩论 → 风险决策 → 交易执行 → Web Dashboard
> 审查方法：5 路并行代码深读（pipeline核心 / 辩论决策 / 数据信号 / Dashboard展示 / 风控执行），共覆盖 ~180 个源文件

---

## 一、总体评价

### 1.1 一句话定性

**这是一套经过真金白银实战锤炼的生产级系统，不是纸上谈兵的学术项目。** 代码中大量的 P0/P1 修复注释、"SNDK disaster"等事故回溯标记、以及层层叠加的防御性工程，都说明这是一个「被市场打过脸、然后认真修正」的系统。

### 1.2 系统规模

| 维度 | 数据 |
|------|------|
| Python 源文件 | ~180 个（排除 venv/node_modules） |
| 核心代码量 | ~50,000+ 行（app/ 目录下） |
| 并发线程 | 7+ daemon threads（scan/monitor/LLM/news/VIX/guard/ORB） |
| 数据源 | 4 级级联（Futu LV3 → Finnhub → TwelveData → AlphaVantage） |
| LLM 模型 | Qwen-Max + DeepSeek-V4-Pro + MiniMax（多模型投票） |
| 风控层数 | 7+ 独立层（PreTradeCheck → PreTradeGate → RiskEngine → CapitalSafety → AbsoluteCircuitBreaker → Broker Notional → KillSwitch） |

### 1.3 评分总览

| 子系统 | 评分 | 简评 |
|--------|------|------|
| 架构设计 | ⭐⭐⭐⭐ | 防御纵深优秀，但 God Object 问题突出 |
| 数据层 | ⭐⭐⭐⭐⭐ | 级联容错 + 本地指标计算，行业一流 |
| 信号生成 | ⭐⭐⭐⭐ | 多因子 + IC 自适应，但双管线共存增加维护负担 |
| 辩论/决策 | ⭐⭐⭐⭐ | 多角色对抗设计精妙，但存在锚定偏差和死代码 |
| 风控/执行 | ⭐⭐⭐⭐⭐ | 纯函数止损引擎 + 水位模型，堪称标杆 |
| Dashboard | ⭐⭐⭐⭐ | 功能完备 + WebSocket 实时，但前后端耦合度高 |
| 代码质量 | ⭐⭐⭐ | 异常吞没过多、配置分散、缺乏集成测试 |

---

## 二、链路流程审查：数据如何从采集到下单

### 2.1 全链路流程图

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         FTSeek-US 全链路流程                              │
│                                                                         │
│  06:00 ET ─────── Pre-Market Phase ──────── 09:30 ET ──── Trading ──── │
│                                                                         │
│  ┌──────────────┐    ┌────────────────┐    ┌──────────────────────┐     │
│  │ Premarket    │    │ LLM Planning   │    │ Signal Scan Loop     │     │
│  │ Enrichment   │───→│ (Debate Engine)│───→│ (5min cycle)         │     │
│  │ · Sector ETF │    │ · Bull R1      │    │ · Market Data Stage  │     │
│  │ · Movers     │    │ · Bear R1      │    │ · Signal Stage       │     │
│  │ · Treasury   │    │ · Arbiter      │    │ · Risk Stage         │     │
│  │ · Insider    │    │ · Factor Screen│    │ · Order Submit       │     │
│  └──────────────┘    │ · LLM Validate │    └──────────┬───────────┘     │
│                      │ · Deep Research│               │                 │
│                      └────────┬───────┘               │                 │
│                               │                       │                 │
│                      ┌────────▼───────┐    ┌──────────▼───────────┐     │
│                      │ Battle Plan    │    │ Position Monitor     │     │
│                      │ JSON           │    │ (30s cycle)          │     │
│                      │ · targets[]    │    │ · Exit Rules         │     │
│                      │ · restricted[] │    │ · Trail Stop         │     │
│                      │ · pos_actions  │    │ · Broker Reconcile   │     │
│                      └────────┬───────┘    └──────────┬───────────┘     │
│                               │                       │                 │
│                      ┌────────▼───────────────────────▼───────────┐     │
│                      │            Web Dashboard                    │     │
│                      │ · / (指挥中心)  · /plan (作战计划)           │     │
│                      │ · /trade (交易) · /watchlist (股票池)       │     │
│                      │ · WebSocket 实时推送 + HTTP 轮询降级        │     │
│                      └────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────────────────────────┘
```

### 2.2 链路通畅性评估

| 环节 | 状态 | 说明 |
|------|------|------|
| 数据源 → 市场数据 | ✅ 通畅 | 4 级级联 + TTL 缓存 + 健康看门狗，几乎不会断 |
| 市场数据 → 技术指标 | ✅ 通畅 | 本地计算（LV3TechnicalCalculator），不依赖外部 API |
| 技术指标 → 信号生成 | ⚠️ 有冗余 | 新旧双管线共存，路由逻辑复杂 |
| 信号 → 风控检查 | ✅ 通畅 | 9 步链式检查，每步独立超时 |
| 风控 → 订单执行 | ✅ 通畅 | 状态机 + 幂等性 + TWAP，执行层成熟 |
| 执行 → 持仓管理 | ✅ 通畅 | RLock 保护 + 5 分钟 broker 对账 |
| 持仓 → 退出管理 | ✅ 通畅 | ATR/Trailing/ScaleOut/TimeStop 四模块 |
| Pre-Market Plan → Dashboard | ✅ 通畅 | JSON 文件 + DataService + WebSocket |
| Dashboard → 用户交互 | ✅ 通畅 | 确认/暂停/手动交易/SL修改 全可操作 |
| **全流程端到端延迟** | **⚠️ 瓶颈** | 30 个标的全扫描需 30-150 秒 |

### 2.3 链路断点风险

```
风险点 1: scan_loop 串行瓶颈
─────────────────────────────
scan_loop 对每个标的是顺序处理的：
  symbol_1: get_full_market_context (1-5s) → signal_stage → risk_stage
  symbol_2: get_full_market_context (1-5s) → signal_stage → risk_stage
  ...
  symbol_30: ...

最坏情况：30 symbols × 5s = 150s 一轮扫描
这意味着最后一个被扫描的标的，拿到的数据可能已经比第一个标的晚了 2 分钟。
在高波动环境下（如 FOMC 公布后），这是真实的延迟风险。

风险点 2: _submit_order() 全局锁
─────────────────────────────────
所有开仓（scan_loop + ORB engine）共享同一把 _open_position_lock。
如果 risk pipeline 对每个候选耗时 5s、5 个候选通过 = 25s 持锁。
期间 ORB engine 无法开仓，可能错过开盘突破窗口。

风险点 3: SQLite 并发写
───────────────────────
7+ 线程共享 SQLite 文件。SQLite 的写锁是库级的，
post_trade_hook + monitor_loop + coordinator_reconcile 同时写 =
database is locked 风险。当前靠 timeout=3-5s 缓解，但不可根治。
```

---

## 三、各子系统深度审查

### 3.1 数据采集层 — ⭐⭐⭐⭐⭐ 行业一流

**优点：**

- **Futu LV3 为主、本地计算为辅**：所有技术指标从 K-line 本地计算（MA/RSI/MACD/ATR/BOLL/KDJ/VWAP/SuperTrend），完全消除了外部 API 限流问题。这是极其聪明的设计。
- **级联容错**：Futu LV3 → Finnhub → TwelveData → AlphaVantage，每一层都有独立的 token bucket 限流器和缓存 TTL。
- **Push 推送架构**：Futu 的 Quote/Kline/Ticker/OrderBook 四种 Push Handler 实现了 <100ms 的实时数据更新。
- **连接健康管理**：后台看门狗（30s）+ TCP 存活验证 + 指数退避重连 + 盘后单次重连策略。
- **VIX 多源容错**：AlphaVantage ^VIX → FRED CSV → Finnhub VIXY proxy（VIXY × 0.85 展期调整）→ 默认值 20.0。

**问题：**

| 问题 | 严重度 | 说明 |
|------|--------|------|
| Futu 单点故障 | P1 | 唯一的 broker 和数据源。Futu OpenD 崩溃 = 整个系统瘫痪。SmartRouter 已实现但只注册了一个 broker。 |
| K-line 请求限流 | P2 | Futu 硬限制 60 次/30 秒。30 个标的 × 多周期 K-line 可能触及上限，触发 30 秒等待。 |
| 无数据质量元指标 | P2 | 数据是否「新鲜」靠 TTL 判断，但不区分「15 秒前的正常报价」和「15 秒前市场已暴跌但缓存还没刷新」。 |

### 3.2 信号生成与因子体系 — ⭐⭐⭐⭐

**优点：**

- **三因子框架（SSS + GDIAN + MFFF）** 设计精巧：SSS 捕捉超卖反转、GDIAN 衡量趋势一致性、MFFF 过滤动量流。
- **IC 自适应权重**：SignalScorer 根据历史 Information Coefficient 动态调整因子权重 — 负 IC 因子归零、高 IC 因子加权。这是真正的自适应学习。
- **策略正交化**：StrategyOrthogonalizer 用 `effective_n = n / (1 + ρ̄ × (n-1))` 计算相关感知权重，防止「同 beta 多倍下注」。
- **成本模型守门**：Almgren-Chriss 市场冲击模型 + 价差 + 手续费 + 借券成本 → 净 edge < 5bps 的信号被惩罚 -20 分。

**问题：**

| 问题 | 严重度 | 说明 |
|------|--------|------|
| **双管线共存** | **P0** | 新旧管线（CRA 3-stage vs legacy _process_symbol_impl）同时存在。Fast Lane 直接绕过新管线。路由逻辑 `_use_new_pipeline()` 增加了一层不确定性。两条路径的 bug 可能互相隐藏。 |
| **Pre-trade checker 修改信号** | P1 | RFC-056/058 的 VIX 过滤器直接修改 `signal.strength`（in-place）。如果同一个信号对象被多个消费者读取，strength 已被悄悄降低了。这是「检查函数产生副作用」的反模式。 |
| **trend_first 策略已禁用** | P2 | IC=-0.24，标注为 DISABLED 但仍保留在代码中。增加了认知负担。 |
| **扫描间隔自适应但不够激进** | P2 | 开盘前 30 分钟（09:30-10:00）扫描间隔 10 分钟。对于 ORB 类策略，开盘头 5 分钟的信息密度远高于其他时段，10 分钟间隔太慢。 |

### 3.3 辩论与决策系统 — ⭐⭐⭐⭐ 设计精妙

**优点：**

- **R1 并行、R2 条件触发**：Bull 和 Bear 在 R1 完全独立运行（不同线程、不同模型、不同 prompt），R2 仅在分歧度 > 0.4 时触发交叉辩论。这减少了锚定偏差。
- **多模型投票 + 偏差校正**：Qwen(70%) + MiniMax(30%)，MiniMax 的分数乘以 0.75 校正系数（因其系统性高估 20-34 分），方向保护机制防止校正翻转共识。
- **三级降级链**：多模型投票 → 单模型(30s) → 纯技术(conf ≤ 0.60)。确保系统永远不会因为 LLM 不可用而完全停摆。
- **统计门控**：StatisticalGate 包含 Bonferroni 校正 + Monte Carlo 置换检验 + Block Bootstrap（保留自相关性）+ Top-N 稳健性。这是真正严谨的策略验证。

**问题：**

| 问题 | 严重度 | 说明 |
|------|--------|------|
| **R2 锚定偏差** | P1 | R2 中 Bull 看到 Bear 的完整 JSON（包括具体的做空目标和入场价位），Bear 也看到 Bull 的全部参数。这会导致「锚定效应」— Bull 可能防御性地调整自己的目标区间，而非真正回应逻辑论证。**建议：R2 只传递对方的 reasoning/data_cited，不传递具体的交易参数。** |
| **分歧度计算脆弱** | P1 | `_calculate_divergence()` 基于标的级别的方向重叠。如果 Bull 分析 LONG 标的、Bear 分析 SHORT 标的（很常见），两者零重叠 → divergence = 0 → R2 永远不触发。即使两者的世界观根本对立。**建议：加入宏观判断分歧（如 regime 判断、VIX 解读）作为额外维度。** |
| **死代码：双辩论引擎** | P1 | `DebateEngine`（debate_engine.py）和 `UnifiedDebateEngine`（unified_debate_engine.py）同时存在。前者有注释 "⚠️ 未接入 — 当前零调用"。增加维护负担和认知混乱。 |
| **投票缓存不够敏感** | P1 | 5 分钟 TTL + 0.8% 价格变动 + 信号哈希（仅 RSI/MACD/regime）。不检查突发新闻、财报意外、VIX 跳变。Flash crash 期间，缓存的投票结果可能危险地过时。 |
| **Memory 共享污染** | P2 | Bull/Bear 和投票系统共享同一个 MemoryInjector 单例。如果某条 lesson 是错误或过时的，同时污染所有 agent。 |
| **LLM 自信度未校准** | P2 | 投票中的 confidence 是 LLM 自报的（0.0-1.0），没有校准机制。一个总是报 0.85 的模型会主导加权平均，尽管它可能是错的。PerformanceTracker 跟踪胜率但不反馈到 confidence 校准。 |

### 3.4 风控与执行层 — ⭐⭐⭐⭐⭐ 标杆级

**优点：**

- **纯函数止损引擎**：`atr_stop_engine.compute_stop_lines()` — 无状态、确定性、含自测套件。计算 4 条候选止损线取最优（Hard Stop / ATR Stop / Chandelier Trail / Breakeven Stop）。这是我见过的最好的止损实现之一。
- **5 级水位模型**：GREEN(0-5%) → YELLOW(5-10%, 0.5×) → ORANGE(10-20%, 0.25×) → RED(20-35%, 0×) → BLACKOUT(>35%, 清仓锁定)。渐进降风险，而非二元开关。
- **Fail-close 哲学**：position_store 缺失 → 拒绝所有开仓；equity 不可用（Live）→ 拒绝；broker 断连（Live）→ 拒绝。系统默认走向安全。
- **绝对熔断器**：日累计亏损 > $2000 → 全封；小时 > $1000 → 暂停 1h；连续 5 亏 → 策略暂停。Append-only JSONL 审计日志。不可绕过。
- **订单状态机**：CREATED → VALIDATED → SENT → ACKED → PARTIALLY_FILLED → FILLED。非法转换抛 ValueError。
- **TWAP 执行器**：大单时间切片（2-8 片，30s 间隔），支持止损中断取消。

**问题：**

| 问题 | 严重度 | 说明 |
|------|--------|------|
| **CapitalAllocator 仍在 observe 模式** | **P0** | `try_reserve()` 默认 `observe_only=True` — 它永远不阻止任何东西。主订单路径 `_submit_order()` 在下单前不调用 `try_reserve()`。这意味着**策略级资金上限是空想而非执行**。 |
| **Symbol 熔断器永不自动解冻** | P1 | `SymbolCircuitBreaker.frozen_symbols` 是一个只增不减的 dict。一旦冻结，永不自动解冻（除非手动清理）。这是内存泄漏 + 操作风险。 |
| **Position Sizing 双系统** | P1 | `position_sizer.py`（纯函数/层级式）和 `capital_safety.calc_position_size()`（Kelly 式/水位式）两套并行。Risk engine 用一套，scheduler 可能用另一套。`DEFAULT_POSITION_PCT = 0.20` 硬编码用于持仓限制检查，而非实际计算的仓位大小。 |
| **Daily PnL 查询失败 = fail-open** | P1 | `_check_daily_drawdown()` 中 DB 查询失败返回 `(True, "")` — 允许继续交易。这与系统其他地方的 fail-close 哲学矛盾。当亏损数据库损坏时，系统应该停止交易而非继续。 |
| **无实时止损执行** | P1 | `RiskEngine.should_exit()` 已弃用并返回 False。止损完全依赖 `PositionExitMonitor` 的轮询间隔（30s）。两次轮询之间的 Flash Crash 可能穿透止损。 |
| **Exit Dispatcher 仅为路由表** | P2 | `ExitDispatcher` 按 `owner_strategy` 路由，但不实际执行退出。真正的退出逻辑仍在单体 `PositionExitMonitor` 中。 |

### 3.5 Web Dashboard — ⭐⭐⭐⭐

**优点：**

- **DataService 抽象层**：TTL 缓存 + 防雷群效应（double-check locking）+ WebSocket 变更推送 + 幽灵持仓对账。这个数据服务层设计得很好。
- **统一快照端点**：`/api/dashboard/snapshot` 用一个请求替代 8+ 个并行 API 调用，内部只做 3-4 次 Futu RPC。
- **WebSocket + HTTP 降级**：主通道 WebSocket（30s 心跳、10 连接上限、指数退避重连），降级通道 HTTP 轮询（Dashboard 15s、Plan 60s）。渐进增强。
- **智能增量推送**：只在有意义变化时推送（持仓 PnL > $1、NAV > $1、价格 > $0.01），避免噪声刷屏。
- **安全层**：HMAC 签名 cookie + PBKDF2 密码 + CSRF Origin 验证 + Rate Limiting + 安全 Headers。对于个人项目来说非常完备。
- **作战计划（/plan）页面**：4 层行动清单（Must Do → Opportunities → Monitor → Avoid）+ Deep Research 展开面板 + 一键跳转交易页。这是从分析到行动的完整闭环。

**问题：**

| 问题 | 严重度 | 说明 |
|------|--------|------|
| **Plan 刷新延迟** | P1 | 作战计划 60 秒轮询 + DataService 5 秒缓存 = 最坏情况下用户看到的是 65 秒前的数据。在止损触发等紧急场景下，这不够快。建议止损/风控触发走 WebSocket 即时推送。 |
| **Deep Research 生成阻塞** | P1 | `POST /api/battle-plan/deep-research/generate` 触发 LLM 调用（~60-90 秒/3 标的）。如果 FastAPI worker 只有一个，这段时间内其他 API 请求会被阻塞。应该改为异步任务 + 轮询结果。 |
| **前后端耦合度高** | P2 | battle_plan.html（1203 行）和 dashboard.html 包含大量内联 JS。没有前端框架（React/Vue），没有打包工具。对于单人维护来说可以，但扩展困难。 |
| **HKD→USD 转换硬编码** | P2 | DataService 中使用 Futu 实时汇率，但 fallback 用硬编码的 7.80。如果 USD/HKD 波动较大，估值会有偏差。 |

### 3.6 作战计划生成管线 — ⭐⭐⭐⭐

**流水线：**

```
factor_engine (5因子量化评分, ~35标的 → Top 8)
    ↓
llm_validator (8个并行LLM调用 → VALID/CONDITIONAL/INVALID)
    ↓
pipeline.py (合成排名 → Top 5 → 叙事生成)
    ↓
daily_battle_plan_us.json ←── 单一事实来源
    ↓ [按需触发]
deep_analysis_engine (多头/空头辩论 per target)
    ↓ [按需触发]
deep_research_pipeline (4分析师 + 辩论 + 综合) → 最深层分析
```

**优点：**

- **因子量化 + LLM 验证双保险**：量化筛选避免纯主观，LLM 验证捕捉量化模型遗漏的离散催化。
- **合成排名公式合理**：`Score = (factor_score × 0.6 + llm_confidence × 0.4) × verdict_multiplier`。LLM 否决权（INVALID → 0 分）是正确的。
- **Conviction Tracker 衰减模型**：信心分数随时间、价格变动、VIX 变化、板块表现而衰减。不是静态的"生成后不管"。
- **Open Checker（开盘检查器）**：09:15 自动检查每个目标 — 跳空 >3% / 方向背离 / 经济事件 / RSI 极端 → 自动取消。这是对「隔夜计划 vs 开盘现实」差距的自动修正。

**问题：**

| 问题 | 严重度 | 说明 |
|------|--------|------|
| **计划过期降级过于宽松** | P1 | 超过 1 天的计划自动降级为「loose mode」— 所有 watchlist 标的都被激活。这意味着过期计划反而比新鲜计划更宽松。应该反过来：过期 → 更严格或完全失效。 |
| **Fast Lane 绕过太多检查** | P1 | Battle Plan 匹配到的标的走 Fast Lane：直接构建 SignalScore，绕过 signal_stage 的多策略编排。虽然有后续 risk pipeline 检查，但跳过了 signal quality 评估。 |
| **Entry Zone 验证仅 ±5%** | P2 | 入场价区间的合理性检查仅为「当前价格 ±5%」。在高 ATR 标的（如 MU ATR=7.5%/日），±5% 可能太窄。 |

---

## 四、架构级问题（Top 5）

### 4.1 God Object: TradingScheduler — P0

**问题**：TradingScheduler 约 1750 行，注入 30+ 依赖项。它同时拥有数据路由、新闻提供者、投票协调器、风险引擎、执行引擎、持仓存储、3 个风险管线、ORB 引擎、订阅管理器等等。所有管线模块以 `scheduler` 作为第一参数，直接深入其内部属性（`scheduler.market_data`、`scheduler.risk_engine`、`scheduler.position_store._lock`）。

**影响**：
- 任何模块的修改都可能波及 scheduler
- 单元测试几乎不可能（需要 mock 整个 scheduler）
- 代码审查者需要理解 1750 行才能改任何一行

**建议**：
```python
# 当前：
def signal_stage(scheduler, symbol, ctx):
    data = scheduler.market_data.get_quote(symbol)
    risk = scheduler.risk_engine.pre_trade_check(signal)
    pos = scheduler.position_store.get(symbol)

# 建议：引入 PipelineContext 数据类
@dataclass
class PipelineContext:
    market_data: MarketDataRouter
    risk_engine: RiskEngine
    position_store: PositionStore
    # ... 只暴露需要的接口

# 各 stage 接收 PipelineContext 而非 scheduler
def signal_stage(ctx: PipelineContext, symbol, market_ctx):
    ...
```

### 4.2 双管线共存 — P0

**问题**：新旧管线同时活跃。`_use_new_pipeline()` 决定路由，但 Fast Lane 绕过新管线、legacy 路径仍然完整保留。

**影响**：
- Bug 可以存在于任一路径，互相隐藏
- 修复一个路径的问题可能在另一个路径重演
- 新增策略需要在两个路径都实现

**建议**：选定一个截止日期（如 30 天内），将 legacy 路径的所有功能迁移到 CRA 3-stage，然后删除 legacy 代码。在过渡期，为每条路径添加 telemetry 标记（哪个路径产生了信号、哪个路径执行了订单），用数据驱动迁移决策。

### 4.3 异常吞没模式 — P0

**问题**：代码库大量使用 `except Exception as e: logger.warning(..., "不影响主流程")`。虽然这防止了崩溃，但也掩盖了系统性故障。

```python
# 典型模式（遍布全局）：
try:
    result = shared_kernel.get_vix_regime()
except Exception as e:
    logger.debug(f"SharedKernel error: {e}")  # 静默吞没
    result = DEFAULT_REGIME  # 永远使用默认值
```

**影响**：如果 SharedKernel 初始化失败，VIX regime 数据会永远使用默认值，但没有任何告警机制通知运维。系统「正常运行」但质量降级是隐性的。

**建议**：
1. 引入「降级计数器」— 每个模块的 silent catch 次数，超过阈值触发 PushHub 告警
2. 在 Dashboard /system 页面展示「降级状态面板」— 哪些模块正在使用降级路径
3. 区分「可恢复异常」和「不可恢复异常」— 前者 warning，后者 error + 告警

### 4.4 配置来源分散 — P1

**问题**：参数至少来自 5 个来源：
1. TradingScheduler 类级常量
2. `unified_config.yaml`（通过 ConfigKeys / get_dotted）
3. 环境变量
4. `exit_policy.yaml`
5. `battle_plan.json`（动态）

同一个参数（如 `min_confidence`）可能从多个路径读取，各有不同的 fallback 链。

**建议**：统一到一个 `ResolvedConfig` 对象，在启动时一次性解析所有来源，之后所有模块只读取这个对象。已有 `ConfigContainer` 的雏形，需要强制推广。

### 4.5 Async/Sync 混用 — P1

**问题**：
- `EmergencyInterceptor` 全 async（asyncio + AsyncOpenAI）
- `NewsPipelineV2` 在 sync 线程中调用 `asyncio.run_until_complete()`
- `LLM Pipeline` 用 `asyncio.new_event_loop()` 但跑在 sync 线程中
- FastAPI 是 async 的，但 DataService 的 Futu 调用用 `run_in_executor` 包装 sync 调用

**影响**：
- 线程中的 `asyncio.run()` 与主事件循环冲突
- async 函数中调用 sync 的 Futu SDK 可能阻塞事件循环
- 调试困难：错误可能发生在不同的事件循环中

**建议**：选择一个方向统一。对于这个系统（主要是 sync SDK），建议核心管线保持 sync，只在网络 I/O 密集的部分（新闻抓取、LLM 调用）使用 async，并通过专门的 bridge 线程与 sync 管线交互。

---

## 五、具体改进建议（按优先级）

### P0 — 立即处理（影响资金安全）

| # | 建议 | 预期收益 | 工作量 |
|---|------|----------|--------|
| 1 | **激活 CapitalAllocator 的 enforce 模式**。`try_reserve()` 改为 `observe_only=False`，并在 `_submit_order()` 前强制调用。 | 策略级资金上限从空想变为执行 | 2-3 天 |
| 2 | **修复 Pre-trade checker 的信号变异**。RFC-056/058 过滤器改为返回调整后的副本，不修改原始 signal 对象。 | 消除共享信号对象被意外修改的隐患 | 1 天 |
| 3 | **修复 Daily PnL 查询的 fail-open**。`_check_daily_drawdown()` DB 失败时返回 `(False, "DB error")` 而非 `(True, "")`。 | 亏损数据库损坏时系统停止交易而非继续 | 半天 |
| 4 | **添加降级告警**。引入 DegradationCounter，silent catch 超过阈值（如 10 次/小时）触发 PushHub 告警。 | 隐性降级变为显性告警 | 2 天 |

### P1 — 短期改进（1-2 周）

| # | 建议 | 预期收益 |
|---|------|----------|
| 5 | **修复 R2 锚定偏差**。R2 只传递对方的 reasoning/data_cited，不传递具体交易参数。 | 减少辩论中的锚定效应 |
| 6 | **修复 SymbolCircuitBreaker 内存泄漏**。添加 auto-unfreeze（如 24h 后自动解冻 + 重新评估）。 | 消除永久冻结的操作风险 |
| 7 | **统一 Position Sizing**。选定一套 sizing 系统（建议保留纯函数版本），删除另一套。 | 消除两套系统不一致的风险 |
| 8 | **投票缓存添加突发事件检查**。在 cache hit 逻辑中加入：VIX 变化 > 5% / 突发新闻 > 0 → invalidate。 | 防止 flash crash 期间使用过时投票 |
| 9 | **作战计划过期逻辑反转**。过期计划应该更严格（而非更宽松）：过期 → 只允许已有持仓的风控操作，不允许新开仓。 | 消除过期计划带来的虚假安全感 |
| 10 | **Deep Research 异步化**。`/deep-research/generate` 改为后台任务（Celery 或 asyncio.create_task），前端轮询结果。 | 防止 60-90s LLM 调用阻塞整个 API |

### P2 — 中期改进（1-3 月）

| # | 建议 | 预期收益 |
|---|------|----------|
| 11 | **扫描并行化**。将 scan_loop 中的 per-symbol 处理改为 ThreadPoolExecutor（4-8 workers），将全扫描时间从 150s 降到 20-40s。 | 大幅减少数据延迟 |
| 12 | **清理死代码**。删除 DebateEngine（已标注零调用）、scheduler_tasks.py（已标注 deprecated）、trend_first 策略（IC=-0.24 已禁用）。 | 减少维护负担和认知混乱 |
| 13 | **引入 PipelineContext 数据类**。替代 scheduler god object 作为管线模块的参数。 | 降低耦合度、提升可测试性 |
| 14 | **添加 LLM 自信度校准**。跟踪每个模型的 confidence vs 实际胜率，计算 calibration curve，用 Platt scaling 校正。 | 提升投票系统的准确性 |
| 15 | **迁移 SQLite → PostgreSQL**。7+ 线程并发写的场景不适合 SQLite。PostgreSQL 的行级锁和 MVCC 能根治 database locked 问题。 | 消除并发写冲突 |
| 16 | **前端现代化**。将 battle_plan.html 的内联 JS 抽取为独立模块，引入轻量框架（Alpine.js 或 Vue），添加组件化。 | 提升可维护性 |
| 17 | **集成测试套件**。当前无端到端测试。至少需要：完整的 scan → signal → risk → order → position → exit 测试，验证全链路不断。 | 防止修改一处破坏另一处 |

---

## 六、逻辑合理性审查

### 6.1 合理的逻辑

| 逻辑 | 评价 |
|------|------|
| VIX 分级风控（Euphoria/Normal/Caution/Fear/Crisis） | ✅ 数据驱动，每级有独立的历史胜率支撑 |
| ATR 自适应止损（4 条候选线取最优） | ✅ 纯函数、确定性、自测完备 |
| 多模型投票 + 偏差校正 + 方向保护 | ✅ 正确处理了系统性偏差和共识翻转 |
| 策略正交化（correlation-aware weighting） | ✅ 防止了「同 beta 多倍下注」的常见量化陷阱 |
| 成本模型守门（net edge < 5bps → 惩罚） | ✅ 理论盈利 vs 实际盈利的关键桥梁 |
| 5 级水位渐进降风险 | ✅ 比二元开关更符合真实风控需求 |
| Battle Plan Open Checker（开盘检查器） | ✅ 自动修正「隔夜计划 vs 开盘现实」的差距 |
| Conviction Tracker 衰减模型 | ✅ 信心不应是静态的 |
| Fail-close 默认安全（多数场景） | ✅ 除了 Daily PnL fail-open 那个例外 |

### 6.2 值得质疑的逻辑

| 逻辑 | 质疑 |
|------|------|
| **LLM 在盘中禁用** | TRADING 阶段 LLM 完全休眠（`_llm_strategy_loop` → SLEEP）。理由是避免 LLM 延迟影响执行速度。但这意味着盘中出现的重大新闻/变化无法触发 LLM 重新评估。Emergency Interceptor 是独立的 async 系统，但它只用关键词匹配，不用 LLM 分析。 |
| **Fast Lane 绕过 signal quality** | Battle Plan 匹配到的标的跳过 signal_stage 的多策略编排。如果 battle plan 是在 pre-market 生成的（基于前一天数据），而盘中情况已变，Fast Lane 可能执行一个已过时的计划。Open Checker 做了部分修正，但只在 09:15 运行一次。 |
| **Voting 的 Neutral Zone 处理** | Strength 40-60 + 低 std_dev → 信号被压制（不交易）。这看似合理，但如果两个模型都在 50 附近（weak signal），std_dev 低只是说明它们一致地不确定。一致的不确定应该被尊重（不交易），但当前的实现也阻止了低 std_dev + weak signal 作为辅助确认信号。 |
| **MiniMax 作为 devil's advocate** | MiniMax 被定位为「情绪与风险分析专家」，权重 30%，偏差校正 ×0.75。但如果 MiniMax 的新闻情绪分析质量不如 Qwen 的综合分析（考虑到 MiniMax 的系统性高估），给它 30% 权重 + devil's advocate 角色是否在引入噪声？建议回测：去掉 MiniMax 后系统胜率/收益是否改善。 |
| **计划确认机制的人机交互** | Dashboard 有 confirm/pause 按钮，但 auto-trading 检查 `_confirmed` 和 `_paused` 标志。如果用户忘记确认计划（或忘记取消暂停），auto-trading 会被阻塞。这应该有一个超时机制：如 09:30 前未确认 → 自动降级为半自动（只执行 Must Do 风控操作）。 |

---

## 七、与 Deep Research 报告的对比

将代码实现与 `deepresearch/` 下的 4 份报告对比：

| 报告描述 | 代码实现 | 一致性 |
|----------|----------|--------|
| 四分析师并行（技术/新闻/情绪/基本面） | `deep_research_pipeline.py` Stage 6 确实 4 分析师并行 | ✅ 一致 |
| Bull/Bear 辩论 | `UnifiedDebateEngine` R1 并行 + R2 条件触发 | ✅ 一致 |
| 三方风险辩论（激进/中性/保守） | `deep_research_pipeline.py` Stage 8.5 三方并行 | ✅ 一致 |
| 证据强度标注 | Prompt 中要求，但代码中无结构化校验 | ⚠️ 依赖 LLM 遵从 |
| 数据完整性表 | Prompt 中要求，但代码中无自动化生成 | ⚠️ 依赖 LLM 遵从 |
| 市场扫描四信号打分 | `factor_engine.py` 是 5 因子，`pre_market_ranker.py` 是 4 维度 | ⚠️ 与报告描述有差异 |
| Value Override 机制 | 代码中未找到显式的 value override 逻辑 | ❌ 可能是 LLM 行为而非代码逻辑 |

**关键差异**：报告中的「市场扫描」和「深度研究」在代码中实际是两个独立的管线：
- **市场扫描** = `factor_engine.py` + `llm_validator.py` + `pipeline.py`（pre-market 定时运行）
- **深度研究** = `deep_research_pipeline.py`（按需触发，Dashboard 按钮或命令行）

报告中展示的「四信号打分 → Top5 → 深度研究 → 多空辩论 → 风险三方 → 组合经理」是一个**概念上的端到端流程**，在代码中被拆成了多个独立模块，每个模块有自己的输入输出契约。这是合理的工程分解。

---

## 八、总结

### 8.1 系统最强的 3 点

1. **防御纵深**：7+ 层独立风控，每层都能独立阻止灾难。即使上游全部失败，最后一层（AbsoluteCircuitBreaker + KillSwitch）仍能保住底线。
2. **纯函数止损引擎**：ATR Stop Engine 是教科书级的实现 — 无状态、确定性、自测完备、regime-aware。
3. **数据层韧性**：4 级级联 + 本地指标计算 + Push 推送 + 健康看门狗。在真实生产环境中，这套数据层几乎不会完全瘫痪。

### 8.2 系统最弱的 3 点

1. **God Object + 双管线**：TradingScheduler 1750 行 + 新旧管线共存 = 修改风险高、测试覆盖率低、认知负担重。这是技术债的最大来源。
2. **异常吞没**：`except Exception → logger.debug → 继续` 模式遍布全局。系统可能在多个模块降级运行，但没有任何外部可见的告警。
3. **CapitalAllocator 空转**：策略级资金上限只记录不执行，是最大的「看着安全但实际未生效」的安全装置。

### 8.3 如果只做一件事

**激活 CapitalAllocator 的 enforce 模式，并在 `_submit_order()` 前强制调用。**

这是投入产出比最高的改进 — 2-3 天工作量，直接将策略级风险控制从「理论设计」升级为「实际执行」。在所有其他问题中，这是唯一一个**看起来已经实现、实际上没有生效**的安全机制。

---

*本报告基于对 FTSeek-US 全量源代码的深度审查，所有结论均可追溯到具体文件和行号。*
