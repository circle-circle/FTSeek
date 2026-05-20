# FTSeek-US LLM 新闻推理 + 夜间行情下单策略规划

## 一、问题定义

当前 FTSeek-US 信号生成依赖 **技术指标 + 多模型投票**，但：
- ❌ 新闻数据只做简单情感分析（positive/neutral/negative）
- ❌ 不处理宏观/地缘事件（如：亚马逊收购卫星公司 → 星链竞争 → 通信板块影响）
- ❌ 不关联美股夜间行情（盘前异动、期权链变化、板块轮动）
- ❌ 不生成**具体的下单策略**（方向、仓位、时机、退出条件）

**目标**：构建一个 LLM 驱动的"二阶推理引擎"，实现：
> 宏观事件 → 行业影响 → 个股关联 → 下单策略（含止损/止盈/时机/仓位）

---

## 二、架构设计

### 2.1 整体流程

```
┌──────────────────────────────────────────────────────────────┐
│                    FTSeek-US LLM 策略引擎                      │
│                                                              │
│  数据层                    推理层                    执行层    │
│  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐  │
│  │ 新闻聚合      │     │ 一阶推理      │     │ 策略生成      │  │
│  │ • RSSHub     │────▶│ • 事件分类    │────▶│ • 方向/标的   │  │
│  │ • Futu快讯   │     │ • 情感量化    │     │ • 仓位/入场   │  │
│  │ • SEC 披露   │     │ • 影响范围    │     │ • 止盈/止损   │  │
│  │ • Twitter    │     │              │     │ • 时效/退出   │  │
│  └──────────────┘     ├──────────────┤     └──────────────┘  │
│                       │ 二阶推理      │              │         │
│                       │ • 板块传导    │──────┐      ▼         │
│                       │ • 供应链影响  │      │ ┌──────────────┐│
│                       │ • 竞对关联    │      │ │ 风控过滤      ││
│                       │ • 政策传导    │      │ │ • 仓位限制   ││
│                       └──────────────┘      │ │ • 冷却期     ││
│                       ▲                     │ │ • 熔断检查   ││
│                       │ 行情数据            │ └──────┬───────┘│
│                       │ • 盘前异动          │        │         │
│                       │ • VIX/恐慌指数      │        ▼         │
│                       │ • 期权链变化        │ ┌──────────────┐  │
│                       │ • 板块资金流向      │ │ 订单执行      │  │
│                       │ • 暗池数据          │ │ • 期权 Put   │  │
│                       └─────────────────────┘ │ • 股票 Short │  │
│                                               └──────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

### 2.2 核心模块

#### 模块 1：`LLMStrategyEngine`（策略引擎）
- **职责**：协调数据采集 → LLM 推理 → 策略生成 → 风控过滤 → 下单
- **调度**：美股盘前（ET 08:00）、盘中每 30min、重大新闻触发时
- **输出**：`OrderStrategy` 对象（结构化策略）

#### 模块 2：`NewsReasoningPipeline`（新闻推理管线）
- **职责**：新闻预处理 → 一阶情感分析 → 二阶传导推理
- **输入**：新闻列表 + 持仓/观察名单 + 实时行情
- **输出**：`EventImpact` 对象（事件影响评估）

#### 模块 3：`NightMarketAnalyzer`（夜间行情分析器）
- **职责**：美股盘前异动检测、板块资金流向分析
- **数据源**：Finnhub（盘前价格）、TwelveData（板块ETF）、Yahoo Finance
- **输出**：`NightMarketSnapshot`（盘前行情快照）

#### 模块 4：`OrderStrategyGenerator`（订单策略生成器）
- **职责**：将事件影响 + 行情快照 → 具体下单策略
- **约束**：必须符合风控规则（仓位、冷却期、熔断）
- **输出**：`OrderStrategy` 列表

---

## 三、数据流设计

### 3.1 输入数据结构

```python
@dataclass
class LLMStrategyContext:
    """LLM 策略推理上下文"""
    timestamp: datetime
    market_phase: str  # "pre_market", "open", "close"
    
    # 新闻输入
    news_items: List[NewsItem]
    
    # 行情输入
    watchlist_prices: Dict[str, float]
    pre_market_changes: Dict[str, float]  # {symbol: change_pct}
    vix_level: float
    spy_trend: str  # "up", "down", "flat"
    
    # 板块数据
    sector_momentum: Dict[str, float]  # {sector: momentum_score}
    
    # 持仓状态
    current_positions: List[PositionInfo]
    
    # 历史上下文
    recent_strategies: List[OrderStrategy]  # 最近生成的策略
```

### 3.2 输出数据结构

```python
@dataclass
class OrderStrategy:
    """生成的下单策略"""
    strategy_id: str
    timestamp: datetime
    
    # 策略核心
    symbol: str
    direction: str  # "LONG", "SHORT", "OPTIONS_PUT", "OPTIONS_CALL"
    confidence: float  # 0-1
    reasoning: str  # LLM 推理摘要
    
    # 入场参数
    entry_price_target: float
    entry_type: str  # "market", "limit"
    max_position_pct: float  # 仓位占总资产比例
    
    # 退出参数
    take_profit_pct: float  # 止盈百分比
    stop_loss_pct: float    # 止损百分比
    time_horizon_hours: int  # 持有期限（小时）
    
    # 触发条件
    trigger_condition: str  # "immediate", "on_pullback", "on_breakout"
    expiry_time: datetime
    
    # 关联信息
    related_event: str  # 关联的新闻/事件摘要
    impact_chain: List[str]  # 事件传导链 ["新闻事件" -> "行业影响" -> "个股"]
```

---

## 四、实现方案（分 3 阶段）

### Phase 1：基础新闻推理引擎（本周可完成）
**目标**：在现有新闻采集管线上增加 LLM 二阶推理

| 任务 | 文件 | 说明 |
|------|------|------|
| 1.1 新闻预处理 | `app/strategy/news_preprocessor.py` | 去重、分类（个股/行业/宏观）、时间排序 |
| 1.2 LLM 推理提示词 | `app/strategy/llm_reasoning_prompts.py` | 一阶情感 + 二阶传导模板 |
| 1.3 策略引擎核心 | `app/strategy/llm_strategy_engine.py` | 调度 LLM 调用、解析输出、生成策略 |
| 1.4 集成到调度器 | `app/core/scheduler_tasks.py` | 新增新闻推理任务，盘前 + 盘中触发 |
| 1.5 推送通道 | `app/notifier/strategy_notifier.py` | 策略推送到钉钉/飞书 |

**预期效果**：
```
📰 新闻：亚马逊逾百亿美元收购卫星公司
🧠 推理：星链竞争加剧 → 通信板块承压 → TMT 板块重估
📊 关联：US.AAPL（卫星通信布局）、US.GOOGL（星链合作）
💡 策略：短期观望，关注 AAPL 盘前跌幅 >2% 时买入 Put
```

### Phase 2：夜间行情分析器（下周）
**目标**：结合美股盘前异动数据增强推理

| 任务 | 文件 | 说明 |
|------|------|------|
| 2.1 盘前数据采集 | `app/data/night_market_collector.py` | Finnhub 盘前价格、TwelveData 板块ETF |
| 2.2 异动检测 | `app/analysis/pre_market_scanner.py` | 检测涨跌幅 >3% 的股票 |
| 2.3 VIX 情绪分析 | `app/analysis/vix_analyzer.py` | 恐慌/贪婪指数计算 |
| 2.4 与 LLM 推理融合 | `app/strategy/llm_strategy_engine.py` | 行情数据注入推理上下文 |

### Phase 3：自动下单策略生成（2-3 周）
**目标**：策略经过风控后自动提交到执行层

| 任务 | 文件 | 说明 |
|------|------|------|
| 3.1 风控过滤器 | `app/risk/strategy_validator.py` | 仓位检查、冷却期、熔断、黑名单 |
| 3.2 策略路由 | `app/execution/strategy_router.py` | 将策略路由到 ShortStrategyRouter |
| 3.3 策略追踪 | `app/storage/strategy_tracker.py` | 策略生命周期管理（生成→执行→验证→结束） |
| 3.4 回测验证 | `scripts/backtest_llm_strategies.py` | 历史新闻 + 行情回测策略有效性 |

---

## 五、关键技术细节

### 5.1 LLM 推理提示词模板

```python
LLM_REASONING_PROMPT = """
你是一个专业的美股策略分析师。请对以下新闻进行二阶推理分析：

新闻：{news_title}
内容：{news_content}
情感：{sentiment}
时间：{timestamp}

请完成以下分析：
1. **事件分类**：[个股/行业/宏观/地缘/政策]
2. **直接影响**：哪些公司/行业直接受影响？
3. **二阶传导**：通过什么机制传导到其他行业/个股？
4. **美股关联**：对以下持仓/观察名单中的股票有何影响？
   观察名单：{watchlist}
   当前持仓：{positions}

5. **策略建议**（仅针对影响评分 > 0.6 的股票）：
   - 方向：看涨/看跌/观望
   - 理由：简洁说明
   - 优先级：高/中/低
   - 预期影响幅度：<2% / 2-5% / >5%

输出 JSON 格式，包含上述分析结果。
"""
```

### 5.2 策略生成流程

```
新闻 → LLM 推理 → EventImpact → + 行情数据 → OrderStrategy → 风控过滤 → 推送/执行
```

### 5.3 与现有系统集成

| 现有模块 | 集成方式 | 说明 |
|---------|---------|------|
| `scheduler_core.py` | 新增定时任务 | 盘前 08:00 ET + 盘中每 30min |
| `decision/tasks.py` | 新增新闻推理任务 | 与现有投票任务并行 |
| `short_strategy.py` | 策略路由对接 | LLM 策略 → ShortStrategyRouter |
| `option_quick_exit.py` | 退出策略复用 | LLM 生成的止盈/止损覆盖默认值 |
| `config_us.py` | 新增配置项 | LLM 提供商、模型、参数 |

---

## 六、风险控制

| 风险 | 缓解措施 |
|------|---------|
| LLM 幻觉/错误推理 | 策略仅推送到通知层（Phase 1），不自动执行；需人工确认 |
| API 调用失败/超时 | 降级为纯技术面投票，LLM 推理不阻塞主流程 |
| 过度交易 | 冷却期 + 单日策略上限（如最多 5 条） |
| 策略质量差 | Phase 4 增加回测验证，淘汰低效策略 |

---

## 七、时间表

| 阶段 | 预计时间 | 里程碑 |
|------|---------|--------|
| Phase 1 | 本周内 | 新闻推理引擎可运行，推送到钉钉/飞书 |
| Phase 2 | 下周 | 夜间行情分析器上线，数据注入推理 |
| Phase 3 | 2-3 周 | 自动下单策略生成（风控后执行） |
| Phase 4 | 4 周+ | 回测验证 + 策略质量优化 |

---

## 八、需要确认的事项

1. **LLM 提供商**：使用现有 MiniMax/DeepSeek 还是需要新增（如 Claude/OpenRouter）？
2. **新闻源扩展**：是否需要增加 SEC 披露、Twitter 数据、暗池数据？
3. **执行权限**：Phase 1 是否仅推送通知（不自动执行）？
4. **优先级**：是否需要先验证 LLM 推理质量（用历史新闻回测）再上线？
