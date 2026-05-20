# FTSeek-US 盘前/盘后 LLM 生成逻辑 — 完整数据流分析

> 基于代码实读，不依赖注释。

---

## 1. 盘前 LLM 生成

### 1.1 触发方式：线程轮询（非 cron，非事件驱动）

入口在 `scheduler_core.py` 的 `_llm_strategy_loop()` 线程（第 695-698 行启动）。

该线程每分钟检查一次 `get_market_phase()`，根据阶段决定行为：

```
PRE_MARKET (ET 04:00-09:30 工作日):
  ├─ 调用 run_premarket_planning(scheduler)  # 每日一次，由 _premarket_plan_executed_today 标志控制
  ├─ 每 2 小时调用 run_after_hours_llm_scan()  # 新闻采集 + LLM 推荐
  └─ 盘前观察模式已彻底移除 (2026-04-24)

TRADING (ET 09:30-16:00):
  └─ 完全休眠，禁止 LLM 调用 (硬门禁)

AFTER_HOURS (ET 16:00-20:00):
  ├─ 处理延迟审计队列 _process_deferred_audit_queue()
  └─ 每 2 小时 run_after_hours_llm_scan()

OVERNIGHT (ET 20:00-04:00):
  └─ 每 6 小时 run_after_hours_llm_scan()

WEEKEND:
  └─ 完全休眠
```

### 1.2 多 Agent 协作 vs 单 Agent

盘前计划使用 **两条独立的 LLM 管线**，并行运行：

**管线 A：USPreMarketPlanner（单 LLM）**
- 文件：`app/us_pipeline/premarket_planner.py`
- 调用：Qwen-Max (通过 Bailian/百炼 API)
- 流程：收集盘前数据 → 构造 prompt → 单 LLM 调用 → 生成 JSON 计划 → 写入 `daily_battle_plan_us.json`
- 无多 Agent 协作，是单 LLM 推理

**管线 B：FTSeekUSAgentBridge（文件系统桥接）**
- 文件：`app/llm/agent_bridge.py`
- 机制：通过文件系统 JSON 文件与外部 Hermes Agent 通信
- 流程：写入请求到 `~/.hermes/ftseek/requests/*.json` → 轮询等待响应 → 读取 `~/.hermes/ftseek/responses/*.json`
- **确实连接到外部 Hermes Agent**（如果可用的话），使用文件协议通信
- 包含 SQLite 持久化队列 (`hermes_queue.db`) 用于崩溃恢复

### 1.3 agent_bridge.py 实际实现

关键实现要点：
- **通信协议**：文件系统 JSON 文件（非 HTTP，非 gRPC）
- **请求目录**：`~/.hermes/ftseek/requests/`
- **响应目录**：`~/.hermes/ftseek/responses/`
- **记忆目录**：`~/.hermes/ftseek/memory/us.json`（美股独立记忆）
- **两个核心方法**：
  - `generate_battle_plan()` — 盘前生成作战计划
  - `generate_eod_review()` — 盘后全链路归因
- **轮询机制**：`_check_plan_response()` 每 5 秒检查一次，最多等待 600 秒（10 分钟）
- **降级策略**：Hermes 不可用时返回 fallback 响应（空 targets + 保守风控）
- **持久化**：`AgentRequestQueue` 类使用 SQLite 存储请求状态，支持重试

### 1.4 LLM 生成输出的使用

**管线 A 输出（USPreMarketPlanner）→ BattlePlan**：
1. 生成 JSON 计划保存到 `data/daily_battle_plan_us.json`
2. `USBattlePlanManager` 加载计划
3. 更新龙头池市场阶段和强度评分
4. 通过钉钉推送通知

**管线 B 输出（AgentBridge）→ 合并到 BattlePlan**：
1. `run_premarket_planning()` 在管线 A 完成后调用 `bridge.generate_battle_plan()`
2. 传入完整上下文：敞口分析、价格校准、财报退出、昨日复盘、VIX、持仓、资金状态等
3. 调用 `bridge._check_plan_response()` 等待 Hermes 响应
4. 调用 `_merge_agent_plan_into_battle_plan()` 合并 Agent 的 targets、restricted、strategy_adjustments、risk_warnings 到现有计划
5. 标记 `agent_enhanced: True`

---

## 2. 盘后 LLM 归因

### 2.1 触发时机

由 `scheduler_triggers.py` 中的 `check_daily_review()` 触发：
- 条件：ET 时间 >= 16:00，且当日未执行过
- 在 `signal_pipeline.py` 的 `scan_loop()` 中被定期调用（每 10 分钟扫描间隔）

### 2.2 使用的 Agent

**第一层：DailyReviewGenerator**（非 LLM，纯统计）
- 生成每日复盘推送（交易统计、胜率、盈亏等）

**第二层：Agent EOD 归因**（通过 AgentBridge）
- 在 `_trigger_eod_agent_review()` 中调用 `bridge.generate_eod_review()`
- 收集全天数据：
  - 交易记录（TradeStore）
  - 决策漏斗（decisions.db）
  - 拒绝统计（RejectionLogger）
  - 资金轨迹（CapitalSafety）
  - 当日作战计划（BattlePlanManager）
- 搜索相关历史记忆（`bridge.search_lessons()`）
- 写入请求到文件系统，轮询等待 Hermes 响应
- 消费响应更新记忆（`_update_memory()`）

### 2.3 输出是否反馈到次日决策

**是的，通过以下链路反馈**：

1. **记忆更新**：`_update_memory()` 写入 `~/.hermes/ftseek/memory/us.json`
   - `historical_lessons[]` — 历史教训
   - `symbol_lessons{}` — 标的特殊规律
   - `strategy_adjustments[]` — 策略调整
   - `performance_tracking{}` — 性能追踪

2. **次日盘前使用**：
   - `generate_battle_plan()` 中 `_format_memory_for_prompt()` 将记忆注入 prompt
   - 包括历史教训（最近 7 条）、策略调整（最近 3 条）、标的规律、性能数据
   - 这些记忆直接影响 Hermes Agent 的次日盘前推理

3. **间接反馈**：
   - AutoTuner 每周五运行，基于历史表现调整策略权重
   - 因子权重学习（G10）和时段-策略对齐学习（G13）每周五更新

---

## 3. 多 Agent 辩论/投票系统

### 3.1 VotingCoordinator 实际实现

**2 模型架构**（2026-04-17 升级，去掉了 GLM-5）：

| 角色 | 模型 | 职责 |
|------|------|------|
| qwen | bailian/qwen-max | 综合策略：宏观事件传导、二阶推理、技术分析 |
| minimax | bailian/MiniMax-M2.5 | 情绪分析：新闻情绪、VIX 恐慌、市场热度 |

工作流程：
1. `_collect_scores_parallel()` — 使用 ThreadPoolExecutor 并行调用两个模型
2. 每个 ModelAgent 独立调用 LLM API，返回 `ModelScore(strength, confidence, direction, reasoning)`
3. 如果超过半数模型失败，切换到 `_generate_technical_fallback()` 纯技术评分
4. `VotingAggregator.aggregate()` 汇总评分
5. 添加仓位建议、市场状态评估
6. 写入 decisions.db

**缓存机制**：`VotingCache` 按 symbol 缓存 30 分钟，价格变动 < 0.3% 时复用

### 3.2 model_agent.py 中各 Agent 角色

`ModelAgent` 类是通用评分器，支持：
- DeepSeek API（通过 `deepseek-*` 环境变量）
- 阿里百炼 API（Qwen/GLM/MiniMax，通过 `bailian-*` 环境变量）

每个 Agent 有：
- 专属角色提示词（来自 VotingCoordinator 的 `_ROLE_PROMPTS`）
- LLM 调用 → 解析 JSON → 返回 ModelScore
- fallback：LLM 不可用时使用规则评分（基于动量、成交量、趋势、VIX）

### 3.3 辩论引擎是否在使用？

**结论：DebateEngine 已实例化但从未被调用。**

证据：
- `VotingCoordinator.__init__()` 中创建了 `DebateEngine(max_rounds=1, fast_mode=True)`
- 但 `run_voting()` 方法中 **没有调用** `self.debate_engine.run_debate()`
- 整个代码库中搜索 `run_debate` 或 `self.debate_engine` 均为零匹配
- `debate_engine.py` 代码完整（ResearcherAgent + DebateEngine），是 **dead code**

辩论引擎是一个完整的实现（Bull/Bear ResearcherAgent，多轮辩论），但当前投票流程不使用它。

---

## 4. 定时调度机制

### 4.1 线程调度

`TradingScheduler.run_forever()` 启动以下后台线程：

| 线程 | 目标 | 频率 |
|------|------|------|
| SignalScanLoop | `_scan_loop()` → `signal_pipeline.scan_loop()` | 每 10 分钟 |
| PositionMonitorLoop | `_monitor_loop()` → `position_monitor.monitor_loop()` | 每 0.5 分钟 |
| LLMStrategyLoop | `_llm_strategy_loop()` | 每分钟检查阶段 |
| NewsV2Loop | `_news_v2_loop()` | 每 180 秒 |
| VIXMonitorLoop | `_vix_monitor_loop()` | 每 300 秒 |
| EmergencyInterceptor | `interceptor.start_monitoring()` | 持续 |
| PositionSync | `start_us_position_sync()` | 每 30 秒 |
| IntradayAttribution | `attr.start()` | 每 60 分钟 |

### 4.2 是否有 cron？

**没有使用系统 cron**。全部调度通过 Python 线程 + `time.sleep()` 循环实现。

在 `scan_loop()` 内部检查定时任务的触发条件：
- `check_daily_review()` — 每日 16:00+ ET
- `check_weekly_review()` — 每周五 16:00+ ET
- `check_monthly_review()` — 每月 1 日 10:00 ET
- `check_position_snapshot()` — 每小时整点 + 15:55 + 16:05 ET

### 4.3 盘前/盘后时间判断逻辑

`MarketPhase` 枚举 + `get_market_phase()` 函数（`scheduler_triggers.py`）：

```
WEEKEND:     周六/周日
PRE_MARKET:  工作日 04:00-09:30 ET
TRADING:     工作日 09:30-16:00 ET
AFTER_HOURS: 工作日 16:00-20:00 ET
OVERNIGHT:   工作日 20:00-04:00 ET
```

时区：`America/New_York` (ET)

---

## 5. 完整数据流图

```
┌─────────────────────────────────────────────────────────────────────┐
│                     FTSeek-US 系统架构数据流                           │
└─────────────────────────────────────────────────────────────────────┘

                    ┌─────────────────────────┐
                    │   main.py (启动入口)      │
                    │  ┌───────────────────┐    │
                    │  │ DI Container 装配  │    │
                    │  └───────────────────┘    │
                    │  ┌───────────────────┐    │
                    │  │ AgentBridge 初始化 │◄───┼── 文件系统协议
                    │  │ (Hermes Agent)    │    │    ~/.hermes/ftseek/
                    │  └───────────────────┘    │
                    │  ┌───────────────────┐    │
                    │  │ BattlePlan 加载   │    │
                    │  │ (重启时重新校验)   │    │
                    │  └───────────────────┘    │
                    └──────────┬────────────────┘
                               │
                    ┌──────────▼────────────────┐
                    │  TradingScheduler          │
                    │  run_forever()              │
                    │                             │
                    │  ┌─────┐ ┌─────┐ ┌──────┐  │
                    │  │Scan │ │Moni │ │LLM   │  │
                    │  │Loop │ │Loop │ │Loop  │  │
                    │  └──┬──┘ └──┬──┘ └──┬───┘  │
                    └─────┼───────┼───────┼───────┘
                          │       │       │
         ┌────────────────┤       │       │
         │                │       │       │
    ┌────▼─────┐   ┌──────▼──┐   │   ┌─────▼──────────────────────┐
    │信号扫描   │   │持仓监控  │   │   │  _llm_strategy_loop()      │
    │(每10min)  │   │(每30s)  │   │   │  按阶段自适应               │
    └────┬─────┘   └─────────┘   │   └─────┬──────────────────────┘
         │                       │         │
    ┌────▼──────────────────┐    │    ┌────▼─────────────────────┐
    │ process_symbol()      │    │    │ PRE_MARKET (04:00-09:30) │
    │                       │    │    │                          │
    │ 1. 获取行情数据        │    │    │ ① run_premarket_planning()│
    │ 2. 构建 context        │    │    │    ┌──────────────────┐  │
    │ 3. 信号生成:           │    │    │    │ USPreMarketPlanner│  │
    │    TRADING阶段:        │    │    │    │ (Qwen-Max单LLM)   │  │
    │    _generate_tech_signal│   │    │    │ → battle_plan.json │  │
    │    (纯技术面,无LLM)     │    │    │    └──────────────────┘  │
    │    非TRADING:          │    │    │    ┌──────────────────┐  │
    │    VotingCoordinator  │    │    │    │ AgentBridge       │  │
    │    .run_voting()      │    │    │    │ (Hermes Agent)    │  │
    │    ┌───────────────┐  │    │    │    │ → merge into plan │  │
    │    │ ModelAgent:   │  │    │    │    └──────────────────┘  │
    │    │  qwen-max     │  │    │    │                          │
    │    │  MiniMax-M2.5 │  │    │    │ ② run_after_hours_llm()  │
    │    │ (并行LLM调用)  │  │    │    │    (每2小时, 仅推送)     │
    │    └───────────────┘  │    │    └──────────────────────────┘
    │ 4. BattlePlan门禁检查  │    │
    │    USBattlePlanManager│    │    ┌─────────────────────────┐
    │    .check_trade()     │    │    │ TRADING (09:30-16:00)   │
    │ 5. RiskEngine 风控     │    │    │                         │
    │ 6. 下单执行            │    │    │ LLM 完全禁止 (硬门禁)    │
    └───────────────────────┘    │    │ 纯技术面扫描 + 执行       │
                                 │    │ EmergencyInterceptor 监控  │
                                 │    └─────────────────────────┘
                                 │
                                 │    ┌─────────────────────────┐
                                 │    │ AFTER_HOURS (16:00-20:00)│
                                 │    │                         │
                                 │    │ ① _process_deferred_audit│
                                 │    │ ② run_after_hours_llm() │
                                 │    │ ③ check_daily_review()  │
                                 │    │    ┌──────────────────┐  │
                                 │    │    │ DailyReviewGenerator│
                                 │    │    │ (统计复盘推送)    │  │
                                 │    │    └──────────────────┘  │
                                 │    │    ┌──────────────────┐  │
                                 │    │    │ _trigger_eod_agent│  │
                                 │    │    │ _review()         │  │
                                 │    │    │ → AgentBridge     │  │
                                 │    │    │ → generate_eod_review│
                                 │    │    │ → 更新 us.json 记忆│
                                 │    │    └──────────────────┘  │
                                 │    └─────────────────────────┘
                                 │
                                 │    ┌─────────────────────────┐
                                 │    │ OVERNIGHT (20:00-04:00) │
                                 │    │ run_after_hours_llm()   │
                                 │    │ (每6小时, 低频)          │
                                 │    └─────────────────────────┘
                                 │
                                 │    ┌─────────────────────────┐
                                 │    │ WEEKEND                 │
                                 │    │ 完全休眠                │
                                 │    │ 重置所有日级标志         │
                                 │    └─────────────────────────┘
                                 │
    ┌────────────────────────────▼──────────────────────────────────┐
    │                    数据反馈回路                                 │
    │                                                                 │
    │  EOD Agent Review → us.json 记忆                                │
    │       │                                                         │
    │       ▼                                                         │
    │  次日盘前 AgentBridge → 读取记忆 → 注入 prompt                    │
    │       │                                                         │
    │       ▼                                                         │
    │  Hermes Agent 输出 → merge into battle_plan                     │
    │       │                                                         │
    │       ▼                                                         │
    │  BattlePlan → 盘中 check_trade() 门禁 → 允许/拒绝交易            │
    │                                                                 │
    │  AutoTuner (每周五) → 策略权重调整                                │
    │  Factor Learning (每周五) → 因子权重 + 时段-策略对齐              │
    └─────────────────────────────────────────────────────────────────┘
```

## 6. 断链/缺失环节分析

### 已确认的断链：

1. **DebateEngine 死代码**：`debate_engine.py` 完整实现但从未被调用。`VotingCoordinator` 中实例化了它但 `run_voting()` 中没有调用。

2. **盘中 LLM 完全禁用**：TRADING 阶段硬编码禁止 LLM 调用，意味着盘中的实时决策完全依赖纯技术面投票（技术指标 + 规则 fallback），LLM 仅在盘前/盘后起作用。

3. **Agent Bridge 依赖外部进程**：Hermes Agent 需要作为独立进程运行并监听 `~/.hermes/ftseek/requests/` 目录。如果 Hermes Agent 未启动，Agent Bridge 会降级到空响应。代码中没有自动启动 Hermes Agent 的机制。

4. **`run_llm_strategy_scan()` 的调用时机模糊**：在 `_llm_strategy_loop()` 中，TRADING 阶段直接 return（禁止 LLM），PRE_MARKET 阶段调用 `run_premarket_planning()` 而非 `run_llm_strategy_scan()`。`run_llm_strategy_scan()` 函数存在但似乎没有被 `_llm_strategy_loop` 直接调用。

### 完整链路确认：

| 环节 | 状态 | 说明 |
|------|------|------|
| 多 Agent 投票 → 信号生成 | ✅ 活跃 | PRE_MARKET/AFTER_HOURS 阶段使用 |
| VotingCoordinator → decisions.db | ✅ 活跃 | `_record_decision_to_db()` |
| 盘前计划 → BattlePlan | ✅ 活跃 | 每日生成，开盘加载 |
| BattlePlan → 盘中门禁 | ✅ 活跃 | `check_trade()` 在每个 symbol 处理时调用 |
| Agent Bridge → 盘前计划合并 | ✅ 活跃 | `run_premarket_planning()` 后半段 |
| EOD 归因 → 记忆更新 | ✅ 活跃 | `_trigger_eod_agent_review()` |
| 记忆 → 次日盘前 | ✅ 活跃 | `_format_memory_for_prompt()` |
| DebateEngine → 投票 | ❌ 死代码 | 实例化但从未调用 |
| LLM → 盘中实时决策 | ❌ 禁用 | 硬编码门禁禁止 |
