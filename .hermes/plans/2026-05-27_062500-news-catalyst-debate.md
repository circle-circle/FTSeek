# FTSeek-US 盘前新闻催化剂催化器 — 多模型辩论买卖建议

> **类型**: L3 新特性 (>200行, 新模块)
> **焦点**: F1 (买卖规范) + F3 (框架逻辑清晰)
> **宪法对齐**: 服务北极星 — 提升胜率(≥50%)和日均收益(≥$500)

---

## 1. 目标

基于盘前新闻(财报、监管事件、突发利空/利好)自动检测重大催化事件，通过多模型充分辩论博弈，最终输出可执行的买卖建议。

**具体场景举例**:
- 富途(FUTU)/长桥受证监会监管 → 暴跌前探测 → 生成SHORT信号
- NVDA财报超预期 → 生成LONG信号
- SEC对某公司发起调查 → 加入restricted或生成SHORT信号
- FDA批准某药企新药 → 生成LONG信号

## 2. 现状分析

### 2.1 已有基础设施 (不需重建)

| 模块 | 位置 | 能力 | 状态 |
|------|------|------|------|
| **NewsNow** (中文财经) | `app/data/newsnow_us_news.py` | 华尔街见闻、财联社电报、金十数据 | ✅ 运行中 |
| **Finnhub News** | `app/data/finnhub_provider.py` | 美股公司新闻 (英文) | ✅ 运行中 |
| **RSSHub** | `app/news_v2/sources/rsshub_us_source.py` | WSJ/Reuters/Fed (英文) | ✅ 运行中 |
| **SEC EDGAR** | `app/news_v2/sources/sec_edgar_source.py` | 8-K/10-Q/10-K 公告 | ✅ 运行中 |
| **News V2 Pipeline** | `app/news_v2/pipeline.py` | 去重→相关性→重要性质量门禁 | ✅ 运行中 |
| **News Attribution** | `app/news_v2/processor/news_attribution.py` | T+5m/15m/60m/24h 价格追踪 | ✅ 运行中 |
| **Emergency Interceptor** | `app/us_pipeline/emergency_interceptor.py` | 关键词匹配 + 行情闪崩检测 | ✅ 运行中 |
| **Earnings Calendar** | `app/data/earnings_calendar.py` | Alpha Vantage 财报日历 | ✅ 运行中 |
| **Debate Planner** | `app/agents/debate/debate_planner.py` | Hermes ↔ Claude 双Agent辩论 | ✅ 运行中 |
| **Debate Engine** | `app/agents/debate/debate_engine.py` | Qwen(多) ↔ GLM(空) 辩论 | ✅ 存在 |
| **Voting Coordinator** | `app/agents/voting/voting_coordinator.py` | 2模型投票聚合 | ✅ 运行中 |

### 2.2 核心缺口 (需要新建)

```
新闻采集 ✅ → [??? GAP ???] → 交易信号 ❌
```

| 缺口 | 说明 | 影响 |
|------|------|------|
| **新闻→影响评分** | 新闻采集后无系统化评分机制，不知道对哪些股票影响多大 | 富途监管事件被采集但无人消费 |
| **催化事件分类** | 无统一的催化事件分类体系(监管/财报/FDA/宏观) | 不同类型事件应用同一处理逻辑 |
| **多模型新闻辩论** | 辩论引擎仅用于盘前计划生成，不用于个股新闻分析 | 单模型偏见无法被纠正 |
| **新闻→信号管线** | 新闻评分结果无法注入信号管线生成交易 | 检测到催化事件但无法执行交易 |
| **中文→英文桥接** | NewsNow采集中文新闻但交易管线全英文 | 中文监管新闻无法被管线理解 |

## 3. 架构设计

### 3.1 整体数据流

```
┌─────────────────────────────────────────────────────────────────┐
│                    新闻催化事件检测器                              │
│                   (NewsCatalystDetector)                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  NewsNow ──┐                                                     │
│  Finnhub ──┤                                                     │
│  RSSHub ───┼──→ [质量门禁] ──→ [催化事件分类器] ──→ 催化事件队列   │
│  SEC EDGAR ┤   (已有)        (新建 L1)                           │
│  Earnings ─┘                                                     │
│                                                                  │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                    多模型新闻辩论引擎                              │
│                   (NewsCatalystDebate)                           │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                       │
│  │ 宏观策略师 │  │ 行业专家  │  │ 风控官   │                       │
│  │ (Qwen)   │  │(MiniMax) │  │ (DeepSeek)│                      │
│  └────┬─────┘  └────┬─────┘  └────┬──────┘                      │
│       │             │             │                               │
│       └──────┬──────┴──────┬──────┘                               │
│              ▼             ▼                                       │
│       [辩论聚合器] → [共识/分歧判定]                               │
│              │                                                     │
│              ▼                                                     │
│       NewsCatalystVerdict                                         │
│       {symbol, direction, strength, confidence,                   │
│        consensus_level, reasoning, risk_warnings}                 │
│                                                                  │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                    信号管线集成                                   │
│               (CatalystSignalInjector)                           │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  CatalystVerdict ──→ [SignalCandidate] ──→ 现有管线:             │
│                                             ├── Risk Stage ✅    │
│                                             ├── Battle Plan ✅   │
│                                             ├── Order Executor ✅│
│                                             └── Exit Monitor ✅  │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 模块设计

#### 模块 A: `app/catalyst/event_classifier.py` — 催化事件分类器

**职责**: 从新闻流中提取催化事件，分类 + 关联标的

```python
@dataclass
class CatalystEvent:
    event_id: str           # 唯一ID
    event_type: str         # REGULATORY | EARNINGS | FDA | MACRO | BREAKING | UPGRADE | DOWNGRADE
    severity: str           # CRITICAL | HIGH | MEDIUM | LOW
    tickers: List[str]      # 受影响标的 ["US.FUTU", "US.LB"]
    headline: str           # 原始标题
    summary: str            # LLM摘要 (中英统一)
    source: str             # 新闻来源
    source_language: str    # zh | en
    published_at: datetime
    raw_items: List[dict]   # 原始新闻条目
```

**分类规则** (基于 `emergency_interceptor.py` 已有逻辑扩展):

| 事件类型 | 关键词 (中/英) | 默认severity | 默认方向 |
|---------|--------------|-------------|---------|
| REGULATORY | 证监会/CSRC/SEC investigation/调查/处罚/禁令 | CRITICAL | SHORT |
| EARNINGS_BEAT | 财报超预期/earnings beat/revenue beat | HIGH | LONG |
| EARNINGS_MISS | 财报不及预期/earnings miss/guidance cut | HIGH | SHORT |
| FDA_APPROVAL | FDA批准/FDA approved/新药获批 | HIGH | LONG |
| FDA_REJECTION | FDA拒绝/clinical trial failed/CRL | CRITICAL | SHORT |
| DELISTING | 退市/delisting/强制摘牌 | CRITICAL | SHORT |
| UPGRADE | 上调评级/upgrade/price target raised | MEDIUM | LONG |
| DOWNGRADE | 下调评级/downgrade/price target cut | MEDIUM | SHORT |
| MACRO_SHOCK | 加息/降息/关税/制裁/tariff/sanctions | HIGH | 取决于方向 |
| BREAKING | 突发/breaking/flash | HIGH | 需辩论 |

**标的关联逻辑**:
1. 直接提及: 新闻中出现 ticker 或公司名 (FUTU → US.FUTU)
2. 行业关联: "中概股监管" → 所有中概股 (FUTU, BABA, PDD, BIDU...)
3. 供应链关联: "NVDA芯片出口限制" → NVDA + 下游客户
4. 竞品关联: "TSLA降价" → TSLA + RIVN, LCID, NIO

#### 模块 B: `app/catalyst/news_debate.py` — 多模型新闻辩论

**职责**: 对高severity催化事件启动3视角辩论

**辩论架构** (遵循已有辩论引擎设计模式):

```
Round 1: 并行独立分析 (3个视角同时启动)
  ├── 宏观策略师 (Qwen-Max): 宏观传导链 + 市场情绪影响
  ├── 行业专家 (MiniMax-M2.5): 行业竞争格局 + 基本面影响  
  └── 风控官 (DeepSeek-V3): 风险评估 + 最坏情景 + 对冲建议

Round 2: 交叉验证 (串行)
  ├── 将 Round 1 三方结论合并 → 每方审阅其他两方观点
  └── 允许修正/坚持/反驳

Arbiter: 聚合判定
  ├── 共识度 ≥ 0.7 → 输出强信号 (strength × 1.2)
  ├── 共识度 0.4-0.7 → 输出标准信号
  └── 共识度 < 0.4 → 输出弱信号或放弃 (strength × 0.5)
```

**辩论 Prompt 模板** (每个视角):

```
你是 FTSeek 美股量化系统的 [角色名]。

## 催化事件
- 类型: {event_type}
- 严重度: {severity}
- 标的: {tickers}
- 标题: {headline}
- 摘要: {summary}
- 来源: {source} ({source_language})

## 市场背景
- VIX: {vix}
- SPY趋势: {spy_trend}
- 市场阶段: {regime}

## 你的分析任务
1. 此事件对 {tickers} 的影响方向和幅度
2. 影响时间窗口 (盘中/数日/长期)
3. 建议操作 (LONG/SHORT/AVOID) + 信心度
4. 最大风险因素

返回JSON:
{
  "direction": "LONG|SHORT|AVOID",
  "strength": 0-100,
  "confidence": 0.0-1.0,
  "time_horizon": "intraday|swing|position",
  "reasoning": "...",
  "risk_factors": ["..."],
  "suggested_sl_pct": -X.X,
  "suggested_tp_pct": X.X
}
```

#### 模块 C: `app/catalyst/signal_injector.py` — 催化信号注入器

**职责**: 将辩论结论注入信号管线

```python
@dataclass
class CatalystSignal:
    symbol: str              # US.FUTU
    direction: str           # LONG / SHORT
    strength: int            # 0-100 (辩论共识强度)
    confidence: float        # 0.0-1.0
    catalyst_event_id: str   # 关联催化事件
    consensus_level: float   # 辩论共识度
    reasoning: str           # 辩论结论摘要
    sl_pct: float            # 建议止损%
    tp_pct: float            # 建议止盈%
```

**注入策略**:
1. **盘前注入** (8:30-9:25 ET): 催化信号加入 premarket planner 的 targets
2. **盘中注入** (9:30-16:00 ET): 催化信号直接生成 SignalCandidate
3. **盘后注入** (16:00-20:00 ET): 催化信号加入次日计划

**与现有管线的交互**:
- `signal_stage.py`: 在策略编排中增加 "catalyst" 优先级 (快车道之前)
- `battle_plan_manager.py`: 催化事件可动态添加 target 或 restricted
- `order_executor.py`: 催化信号走标准做空门禁 + Plan Gate

## 4. 实现步骤

### Phase 1: 催化事件分类器 (L1, ~150行)
**文件**: `app/catalyst/event_classifier.py` (新建)
**依赖**: 复用 `news_v2/pipeline.py` 输出

1. 定义 `CatalystEvent` dataclass
2. 实现关键词匹配 + LLM辅助分类
3. 实现标的关联 (直接提及 + 行业/供应链/竞品)
4. 实现中文→英文桥接 (LLM翻译标题+摘要)
5. 单元测试: 覆盖所有事件类型

### Phase 2: 多模型新闻辩论 (L2, ~300行)
**文件**: `app/catalyst/news_debate.py` (新建)
**依赖**: 复用 `app/agents/debate/debate_engine.py` 的 ResearcherAgent 模式

1. 定义3个视角的 system prompt
2. 实现并行辩论 (ThreadPoolExecutor, 3个LLM同时调用)
3. 实现交叉验证 (Round 2 串行)
4. 实现聚合判定 (共识度计算 + strength调整)
5. 缓存: 同一event_id 24h内不重复辩论
6. 单元测试: 模拟3模型返回，验证聚合逻辑

### Phase 3: 信号注入 (L1, ~100行)
**文件**: `app/catalyst/signal_injector.py` (新建)
**修改**: `app/pipeline/stages/signal_stage.py` (+15行)

1. 定义 `CatalystSignal` dataclass
2. 实现信号优先级队列 (催化信号 > 快车道 > 技术面)
3. 在 `signal_stage.py` 增加 catalyst 策略检查
4. 实现与 Battle Plan 的动态交互 (添加target/restricted)
5. 单元测试: 信号注入 → SignalCandidate 转换

### Phase 4: 调度集成 (L1, ~80行)
**修改**: `app/core/scheduler_core.py` (+30行)
**新建**: `app/catalyst/detector.py` (调度入口)

1. 在 scheduler 中注册 `CatalystDetector` 定时任务 (每5分钟)
2. 盘前 (8:30 ET): 全量扫描过夜新闻 → 催化事件 → 辩论 → 注入计划
3. 盘中 (9:30-16:00 ET): 增量扫描 → 高severity事件 → 快速辩论(1轮) → 注入管线
4. 盘后 (16:05 ET): 收盘后新闻 → 加入次日计划

### Phase 5: 归因闭环 (L2, ~120行)
**修改**: `app/news_v2/processor/news_attribution.py` (+40行)

1. 催化事件 → 交易 → PnL 全链路追踪
2. 辩论准确率统计 (方向命中率 × 事件类型)
3. 月度报告: "哪类催化事件最赚钱"
4. 教训回填: 辩论错误的案例 → LessonInjector

## 5. 配置设计

```yaml
# unified_config.yaml 新增 section
catalyst:
  enabled: true
  scan_interval_minutes: 5
  
  # 事件分类
  classifier:
    min_severity: MEDIUM        # 低于此级别不触发辩论
    llm_assist: true            # 关键词匹配不足时用LLM辅助
    chinese_bridge: true        # 中文新闻翻译为英文摘要
    
  # 辩论配置
  debate:
    enabled: true
    models:
      macro_strategist: qwen-plus       # 宏观策略师
      industry_expert: minimax-m2.5     # 行业专家
      risk_officer: deepseek-pro        # 风控官
    timeout_seconds: 120                # 辩论总超时
    min_severity_for_debate: HIGH       # 低于此级别跳过辩论直接评分
    consensus_threshold: 0.4            # 低于此值放弃信号
    
  # 信号注入
  signal_injection:
    priority: before_fast_lane          # 催化信号优先级
    min_strength: 50                    # 最低信号强度
    max_positions_per_catalyst: 2       # 单个催化事件最多开2个仓
    cooldown_minutes: 60                # 同标的催化信号冷却
    
  # 标的关联映射
  sector_mapping:
    chinese_adr: ["FUTU", "BABA", "PDD", "BIDU", "JD", "NIO", "LI", "XPEV"]
    ai_semiconductor: ["NVDA", "AMD", "AVGO", "MU", "ARM", "QCOM"]
    ev: ["TSLA", "RIVN", "LCID", "NIO", "XPEV", "LI"]
```

## 6. 风险与对策

| 风险 | 严重度 | 对策 |
|------|--------|------|
| 新闻误分类 (假阳性) | 🔴 高 | LLM二阶确认 + 辩论共识度门槛 + 最小strength限制 |
| 辩论超时 (3模型并行) | 🟡 中 | 120s超时 + 降级到单模型快速评分 |
| 新闻延迟 (比行情慢) | 🟡 中 | 催化信号仅作为辅助，不替代行情指标 |
| 中文翻译误差 | 🟡 中 | 保留原文 + LLM翻译双通道 |
| 过度交易 (每个新闻都交易) | 🔴 高 | cooldown + max_positions限制 + strength门槛 |
| 与现有管线冲突 | 🟡 中 | 催化信号走标准风控流程，不绕过任何门禁 |

## 7. 验证计划

### 7.1 历史回测
- 用过去30天的NewsNow数据，回测催化事件检测准确率
- 统计: 检出率 / 误报率 / 辩论方向准确率

### 7.2 影子测试 (先观察后启用)
- Phase 1-3 完成后以 shadow 模式运行 1 周
- 记录"如果启用了会怎样"的对照数据
- 启用条件: 辩论方向准确率 ≥ 55%, 催化信号胜率 ≥ 40%

### 7.3 Gate 5 验证清单
- [ ] 催化事件分类覆盖所有10种类型
- [ ] 辩论3模型全部成功返回 (无超时)
- [ ] 信号注入后走完整风控链 (Risk → Plan Gate → Order)
- [ ] 中文新闻正确关联到US ticker
- [ ] cooldown正确生效 (同标的60min内不重复)
- [ ] 归因数据正确写入 news_attribution.db

## 8. 文件清单

| 操作 | 文件 | 行数估计 |
|------|------|---------|
| 新建 | `app/catalyst/__init__.py` | 5 |
| 新建 | `app/catalyst/event_classifier.py` | 150 |
| 新建 | `app/catalyst/news_debate.py` | 300 |
| 新建 | `app/catalyst/signal_injector.py` | 100 |
| 新建 | `app/catalyst/detector.py` | 80 |
| 新建 | `app/catalyst/models.py` | 50 |
| 修改 | `app/pipeline/stages/signal_stage.py` | +15 |
| 修改 | `app/core/scheduler_core.py` | +30 |
| 修改 | `app/news_v2/processor/news_attribution.py` | +40 |
| 修改 | `config/unified_config.yaml` | +30 |
| 新建 | `tests/catalyst/test_classifier.py` | 100 |
| 新建 | `tests/catalyst/test_debate.py` | 80 |
| 新建 | `tests/catalyst/test_injector.py` | 60 |

**总计**: ~1040行 (L3级别)

## 9. 时间估算

| Phase | 工作量 | 前置条件 |
|-------|--------|---------|
| Phase 1: 催化事件分类器 | 2-3小时 | 无 |
| Phase 2: 多模型辩论 | 3-4小时 | Phase 1 |
| Phase 3: 信号注入 | 1-2小时 | Phase 2 |
| Phase 4: 调度集成 | 1-2小时 | Phase 1-3 |
| Phase 5: 归因闭环 | 1-2小时 | Phase 4 |
| 影子测试 | 1周 (自动) | Phase 1-5 |

**总计**: 8-13小时开发 + 1周影子测试
