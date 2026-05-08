# FTSeek-HK Harness 整车平台 — 完整设计规划 (v1.0)

## 一、Ground Truth: 港股当前框架全景

### 1.1 现有 Harness 组件清单（已存在但大部分未启用）

| 层次 | 组件 | 行数 | 就绪度 | 问题 |
|------|------|------|--------|------|
| **基础设施层** | | | | |
| | `harness/base.py` | 288 | ✅ 可用 | Agent 生命周期/状态管理完整 |
| | `harness/coordinator.py` | 345 | ✅ 可用 | Coordinator + TaskDefinition + WorkflowContext |
| | `harness/messaging.py` | 276 | ✅ 可用 | MessageBus 全局单例，功能完整 |
| | `harness/timeout.py` | 339 | ✅ 可用 | TimeoutController 完整 |
| | `harness/fallback.py` | 337 | ✅ 可用 | ModelRouter + FallbackChain |
| **业务 Agent 层** | | | | |
| | `harness/strategist.py` | 282 | ⚠️ 占位符 | BatchReviewProcessor 依赖不确定 |
| | `harness/risk_officer.py` | 370 | ⚠️ 硬编码 | 阈值与 unified_config.yaml 不同步 |
| | `harness/context_engineer.py` | 307 | ✅ P0 已修 | import os 已修复 |
| | `harness/trade_monitor.py` | 257 | ⚠️ 占位符 | `_fetch_trades` 返回硬编码数据 |
| **Adapter 层** | | | | |
| | `adapters/base_adapter.py` | 126 | ✅ 可用 | 设计合理 |
| | `adapters/collector_adapter.py` | 135 | ✅ P0 已修 | _collector 实际加载 |
| | `adapters/risk_adapter.py` | 208 | ✅ P0 已修 | 方法映射到 RiskManager |
| | `adapters/review_adapter.py` | 165 | ⚠️ 待验证 | BatchReviewProcessor 依赖 |
| | `adapters/report_adapter.py` | 153 | ⚠️ 待验证 | Reporter 依赖 |
| **Workflows** | | | | |
| | `workflows/daily_review.py` | 221 | ⚠️ 需注册 | 工作流定义完整，Agent 未注册 |
| | `workflows/weekly_review.py` | 260 | ⚠️ 需注册 | 同上 |
| **新提取引擎** | | | | |
| | `data_subscriber_engines/multi_timeframe.py` | 419 | ✅ 已验证 | 纯计算，无副作用 |
| | `data_subscriber_engines/dynamic_stop_params.py` | 214 | ✅ 已验证 | 纯计算，无副作用 |
| **试点入口** | | | | |
| | `harness_pilot.py` | 216 | ✅ 已注入 | 独立线程 + event loop |

### 1.2 港股主系统关键特征

| 特征 | 文件 | 说明 |
|------|------|------|
| 入口 | `main.py` 1060 行 | 6 功能段：初始化→OpenD→同步→启动→通知→等待 |
| 数据订阅 | `data_subscriber.py` 3119 行 (原 3584) | 已提取 465 行到 engines |
| 共识引擎 | `strategy/consensus_engine.py` | 加权共识 + MTF 对齐 |
| 风控 | `risk/risk_manager.py` | 同步类，非 async |
| 策略 | `strategy/confluence_v2.py` | 多因子 (RSI+MACD+BB+SuperTrend+MA+Volume) |
| 执行 | `trader/executor.py` | Futu API + 动态 lot_size |
| 新闻管线 | `pipeline/orchestrator.py` | Pre-market 新闻→策略 |
| 盘前计划 | `pipeline/battle_plan_generator.py` | LLM 生成 |
| FastAPI | `server.py` | 回调端口 8899 |

### 1.3 港股交易特性

- **T+0 交易** (当日可买卖)
- **无涨跌幅限制**
- **整手交易** (非 1 股)
- **港股交易时段**: 09:00-12:00 (早市) / 13:00-16:00 (午市)
- **盘前**: 08:30-09:00
- **竞价**: 09:00-09:30
- **Futu OpenD**: 全局 100 Push 槽位

---

## 二、Harness 整车平台设计规划

### 2.1 5 层架构映射

```
┌─────────────────────────────────────────────────────────┐
│  Layer 0: Orchestrator (方向盘 + 导航)                     │
│  - Coordinator: 任务分解 → Agent 调度 → 结果汇总          │
│  - Workflows: DailyReview / WeeklyReview / PreMarket      │
│  - TimeGate: 港股 5 阶段时段感知                           │
├─────────────────────────────────────────────────────────┤
│  Layer 1: Engine (发动机 — 可替换)                          │
│  - LLMEngine: 统一调用接口，qwen/deepseek 热切换           │
│  - MTFEngine: 多周期趋势计算 (已提取)                      │
│  - StopEngine: 动态止损参数 (已提取)                       │
│  - FallbackChain: 降级路径 (已存在)                        │
├─────────────────────────────────────────────────────────┤
│  Layer 2: Policy/Rules (刹车)                              │
│  - RiskOfficer: 声明式规则，非硬编码                       │
│  - RiskAdapter: 桥接 RiskManager (已修复)                  │
│  - CircuitBreaker: 熔断器 (已存在于主系统)                  │
├─────────────────────────────────────────────────────────┤
│  Layer 3: Observability (仪表盘)                           │
│  - MessageBus: 统一事件上报 (已存在)                       │
│  - TradeMonitor: 实时监控 (需修复)                         │
│  - CollectorAdapter: 数据采集 (已修复)                     │
├─────────────────────────────────────────────────────────┤
│  Layer 4: Memory + Learning (记忆 + 自我进化)               │
│  - ContextEngineer: 数据压缩/记忆管理 (已修复)              │
│  - ReviewAdapter: 复盘适配器 (待验证)                      │
│  - Lessons 注入: 次日盘前自动注入 (待实现)                  │
└─────────────────────────────────────────────────────────┘
     ↓
┌─────────────────────────────────────────────────────────┐
│  Layer 0: External Tools (可插拔外部能力)                   │
│  LLM: qwen/deepseek | Data: Futu/AkShare | Notify: 飞书/钉钉 │
│  Broker: Futu Trade API                                   │
└─────────────────────────────────────────────────────────┘
```

### 2.2 渐进式实施路线

#### Phase 1 (本周 — ✅ 已完成): 止血 + 试点
- [x] 修复 3 个 P0 bug (context_engineer, collector_adapter, risk_adapter)
- [x] 提取 2 个计算引擎 (multi_timeframe, dynamic_stop_params)
- [x] 创建 HarnessPilot 包裹层
- [x] 注入 main.py 启动流程 (try/except 包裹)
- [ ] Claude Code 辩论审核 (当前)

#### Phase 2 (下周): 盘后复盘闭环
- [ ] TradeMonitor 适配实际数据源 (对接 position_manager + trade_records)
- [ ] DailyReviewWorkflow 端到端跑通 (collect → analyze → assess → report)
- [ ] WeeklyReviewWorkflow 端到端跑通
- [ ] 飞书/钉钉推送复盘报告

#### Phase 3 (次周): 盘前规划注入
- [ ] PreMarketWorkflow: 新闻扫描 → LLM 分析 → BattlePlan 生成
- [ ] RiskOfficer 与 RiskManager 对齐 (读取 unified_config.yaml)
- [ ] ContextEngineer 接入 lessons 注入 (次日盘前自动加载)

#### Phase 4 (第 4 周): 盘中监控 (影子模式)
- [ ] RiskOfficer 盘中实时监控 (只读/不拦截)
- [ ] TradeMonitor 实时异常检测
- [ ] 影子模式验证 ≥ 14 天

#### Phase 5 (第 5-6 周): 盘中拦截 (渐进激活)
- [ ] RiskOfficer 激活拦截 (仅 P0 级别)
- [ ] 熔断机制与 Harness 集成
- [ ] 全链路 trace_id 注入

### 2.3 关键设计约束

1. **绝不重写 main.py**: 渐进式包裹，等 Harness 稳定后再逐步替换
2. **独立 event loop**: 不与 DataSubscriber 的 threading 冲突
3. **防御性注入**: 所有 Harness 组件用 try/except 包裹，失败不影响交易
4. **影子模式优先**: 任何新功能必须先 shadow 运行 ≥ 14 天
5. **配置驱动**: 阈值从 unified_config.yaml 读取，不硬编码
6. **市场差异通过配置注入**: 平台代码不含 `if market == "HK"` 判断

### 2.4 当前框架合理性评估

#### ✅ 合理之处
1. **Agent 基类设计**: `base.py` 的生命周期 (init → execute → shutdown) 合理
2. **Coordinator 任务图**: TaskDefinition + dependencies 支持 DAG 编排
3. **Adapter 模式**: 桥接现有模块是正确方向，避免重写
4. **FallbackChain**: 降级路径是实盘必备
5. **MessageBus**: 事件驱动架构是平台化的正确基础

#### ⚠️ 需要改进
1. **RiskOfficer 阈值硬编码**: 与 unified_config.yaml 不同步，需改为配置驱动
2. **TradeMonitor 占位符**: `_fetch_trades` 返回空数据，需对接实际数据源
3. **Strategist 依赖不确定**: `BatchReviewProcessor` 是否存在待验证
4. **Workflows 未注册**: DailyReview/WeeklyReview 定义了但未接入 Coordinator
5. **缺少 TimeGate**: 港股有 5 个交易阶段，Harness 当前无时段感知能力
6. **缺少 trace_id**: 全链路追踪未实现

#### ❌ 风险项
1. **async/async 与 threading 冲突**: Harness 是 async，DataSubscriber 核心路径是 threading.Thread
2. **全局单例泛滥**: MessageBus 全局单例 + Coordinator 全局单例，多实例场景下可能冲突
3. **无健康检查**: Harness 组件无健康检查机制，crash 后无法自动恢复
