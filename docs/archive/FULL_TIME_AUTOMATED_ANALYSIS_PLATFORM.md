# FTSeek 全时段自动化分析平台设计方案

> 设计时间: 2026-04-12  
> 设计目标: 利用LLM+多源新闻+行情数据，实现**盘前指导→盘中策略→盘后复盘→夜间迭代**的全链路自动化

---

## 📐 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│                    全时段自动化分析平台                        │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │  盘前策略引擎  │  │  盘中动态策略  │  │  盘后复盘引擎  │      │
│  │ 08:30-09:30  │  │ 10:00-16:00  │  │ 16:00-18:00  │      │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘      │
│         │                 │                  │               │
│         └─────────────────┼──────────────────┘               │
│                           ▼                                  │
│                  ┌─────────────────┐                         │
│                  │  LLM策略工厂     │                         │
│                  │ (策略生成+优化)  │                         │
│                  └────────┬────────┘                         │
│                           │                                  │
│         ┌─────────────────┼──────────────────┐               │
│         ▼                 ▼                  ▼               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │  新闻分析器   │  │  行情分析器   │  │  情绪分析器   │      │
│  │ (RSSHub+快讯) │  │ (七周期K线)  │  │ (资金流+VIX) │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              夜间美股监控 (21:00-06:00)                │   │
│  │         美股行情 → 次日港股策略预测                     │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

---

## 🕐 全时段工作流程

### 1️⃣ 盘前策略生成 (08:30-09:30)

**目标**: 基于隔夜新闻+美股表现+技术指标，生成当日交易策略

```python
# 新增模块: app/strategy/pre_market_strategy_generator.py

class PreMarketStrategyGenerator:
    """
    盘前策略生成器
    
    工作流程:
    1. 收集隔夜新闻 (RSSHub Futu + 金十 + 财联社)
    2. 分析美股隔夜表现 (FTSeek-US数据)
    3. 分析技术指标 (七周期K线共振)
    4. LLM综合分析，生成策略池
    5. 策略评分+排序
    6. 钉钉推送交易指导
    """
    
    async def generate_strategies(self, codes: List[str]) -> List[TradingStrategy]:
        """
        生成当日交易策略
        
        Returns:
            策略列表 (按评分排序)
        """
        # 1. 新闻分析
        news_analysis = await self._analyze_overnight_news(codes)
        
        # 2. 美股关联分析
        us_correlation = self._analyze_us_correlation(codes)
        
        # 3. 技术面分析
        technical_snapshot = self._get_technical_snapshot(codes)
        
        # 4. LLM策略生成
        strategies = await self._llm_generate_strategies(
            news_analysis,
            us_correlation,
            technical_snapshot
        )
        
        # 5. 策略评分
        scored_strategies = self._score_strategies(strategies)
        
        # 6. 推送钉钉
        await self._push_to_dingtalk(scored_strategies)
        
        return scored_strategies
```

**输出示例**:

```markdown
## 📊 盘前交易指导 (2026-04-13)

### 🎯 核心策略 (3条)

**1. 腾讯控股 (HK.00700) - 强烈推荐**
- **方向**: 买入
- **目标价**: 420-430
- **止损价**: 405
- **仓位**: 20%
- **逻辑**: 
  - 隔夜美股MSFT涨2.3%，科技板块情绪积极
  - 富途快讯：腾讯游戏版号获批
  - 七周期共振：日线+60M+30M看多
  - LLM置信度: HIGH (0.85)

**2. 美团 (HK.03690) - 观望**
- **方向**: 等待回调买入
- **观察区间**: 120-125
- **逻辑**:
  - 新闻中性，无明显催化剂
  - 技术面：RSI 68，短期超买
  - 建议等待10:00后确认趋势

### ⚠️ 风险提示
- 美联储今晚公布会议纪要，可能引发波动
- 港股通南向资金连续3日净流出
```

---

### 2️⃣ 盘中动态策略 (10:00-16:00)

**目标**: 基于实时新闻快讯，动态生成交易机会

```python
# 新增模块: app/strategy/intraday_news_strategy.py

class IntradayNewsStrategyEngine:
    """
    盘中新闻驱动策略引擎
    
    触发机制:
    1. 重大新闻到达 (关键词匹配)
    2. 新闻情绪突变 (sentiment_score > 0.7 或 < -0.7)
    3. 多源新闻共振 (≥3个源报道同一事件)
    
    工作流程:
    1. 新闻监听 (RSSHub实时推送)
    2. 快速分类 (财报/监管/并购/宏观)
    3. 情绪分析 (LLM快速模式)
    4. 策略生成 (基于历史相似事件)
    5. 风控检查
    6. 推送执行建议
    """
    
    async def on_news_event(self, news: NewsItem):
        """
        新闻事件触发策略生成
        
        Args:
            news: 新闻条目
        """
        # 1. 快速分类
        category = self._classify_news(news)
        
        # 2. 影响评估
        impact = await self._assess_impact(news, category)
        
        if impact.severity < "high":
            return  # 低影响新闻不生成策略
        
        # 3. 历史相似事件检索
        similar_events = self._find_similar_historical_events(news)
        
        # 4. LLM策略生成
        strategy = await self._llm_generate_intraday_strategy(
            news,
            impact,
            similar_events
        )
        
        # 5. 时效性检查 (新闻发布<5分钟才有效)
        if news.age_minutes > 5:
            logger.info(f"新闻已过时 ({news.age_minutes}分钟)，跳过")
            return
        
        # 6. 推送+执行
        await self._push_and_execute(strategy)
```

**策略类型**:

| 新闻类型 | 策略逻辑 | 持仓时间 | 示例 |
|---------|---------|---------|------|
| **财报超预期** | 立即买入，追踪止盈 | 1-3天 | 腾讯Q1利润+30% |
| **监管处罚** | 立即卖出/做空 | 1-5天 | 反垄断罚款 |
| **并购重组** | 买入被并购方 | 1-2周 | 阿里收购高鑫零售 |
| **宏观数据** | 根据数据方向调整仓位 | 1-3天 | 美联储加息 |
| **突发黑天鹅** | 快速减仓/对冲 | 当天 | 地缘政治事件 |

---

### 3️⃣ 盘后复盘与策略迭代 (16:00-18:00)

**目标**: 基于当日交易结果，优化策略参数

```python
# 增强模块: app/review/strategy_optimizer.py

class PostMarketStrategyOptimizer:
    """
    盘后策略优化器
    
    工作流程:
    1. 收集当日所有交易
    2. 归因分析 (为什么赚/亏)
    3. 策略效果评估
    4. LLM策略优化建议
    5. 更新策略参数
    6. 生成次日策略方向
    """
    
    async def optimize_strategies(self, date: str) -> OptimizationReport:
        """
        优化策略
        
        Returns:
            优化报告
        """
        # 1. 收集当日交易
        trades = self._get_today_trades(date)
        
        # 2. 归因分析
        attribution = await self._run_attribution(trades)
        
        # 3. 策略表现
        strategy_performance = self._evaluate_strategies(trades)
        
        # 4. LLM优化建议
        llm_suggestions = await self._llm_optimize(
            attribution,
            strategy_performance
        )
        
        # 5. 应用优化
        self._apply_optimizations(llm_suggestions)
        
        # 6. 生成报告
        report = self._generate_report(
            attribution,
            strategy_performance,
            llm_suggestions
        )
        
        # 7. 推送钉钉
        await self._push_optimization_report(report)
        
        return report
```

---

### 4️⃣ 夜间美股监控 (21:00-次日06:00)

**目标**: 监控美股表现，预测次日港股策略

```python
# 新增模块: app/strategy/us_overnight_monitor.py

class USOvernightMonitor:
    """
    美股隔夜监控器
    
    工作流程:
    1. 监控美股持仓相关股票
    2. 收集美股新闻
    3. 分析美股走势
    4. 预测次日港股影响
    5. 生成次日策略预演
    6. 重大事件实时推送
    """
    
    async def monitor_us_market(self):
        """
        美股监控主循环
        
        运行时间: 21:00 - 06:00 HKT
        """
        while self._running:
            # 1. 获取美股实时数据
            us_data = await self._fetch_us_market_data()
            
            # 2. 分析涨跌幅
            change_analysis = self._analyze_us_changes(us_data)
            
            # 3. 新闻分析
            us_news = await self._analyze_us_news()
            
            # 4. LLM影响评估
            impact = await self._llm_assess_hk_impact(
                change_analysis,
                us_news
            )
            
            # 5. 重大事件推送
            if impact.severity >= "high":
                await self._push_urgent_alert(impact)
            
            # 6. 更新次日策略预演
            await self._update_next_day_preview(impact)
            
            # 每5分钟检查一次
            await asyncio.sleep(300)
```

---

## 🏗️ 核心模块设计

### LLM策略工厂

```python
# 新增模块: app/ai/strategy_factory.py

class LLMStrategyFactory:
    """
    LLM策略工厂
    
    功能:
    1. 基于多源输入生成策略
    2. 策略评分+排序
    3. 策略回测验证
    4. 策略参数优化
    """
    
    STRATEGY_TEMPLATES = {
        "momentum": "动量策略",
        "mean_reversion": "均值回归",
        "breakout": "突破策略",
        "news_driven": "新闻驱动",
        "cross_market": "跨市场套利",
        "event_driven": "事件驱动",
    }
    
    async def generate_strategy(
        self,
        code: str,
        news_context: NewsContext,
        technical_context: TechnicalContext,
        market_context: MarketContext,
        strategy_type: str = "auto",
    ) -> TradingStrategy:
        """
        生成交易策略
        
        Args:
            code: 股票代码
            news_context: 新闻上下文
            technical_context: 技术上下文
            market_context: 市场上下文
            strategy_type: 策略类型 (auto=自动选择)
        
        Returns:
            TradingStrategy: 生成的策略
        """
        # 1. 自动选择策略类型
        if strategy_type == "auto":
            strategy_type = self._select_strategy_type(
                news_context,
                technical_context
            )
        
        # 2. 构建Prompt
        prompt = self._build_strategy_prompt(
            code,
            news_context,
            technical_context,
            market_context,
            strategy_type
        )
        
        # 3. LLM生成
        llm_output = await self._call_llm(prompt)
        
        # 4. 解析策略
        strategy = self._parse_strategy(llm_output)
        
        # 5. 策略验证
        validation = self._validate_strategy(strategy)
        
        if not validation.passed:
            logger.warning(f"策略验证失败: {validation.reasons}")
            return None
        
        # 6. 评分
        strategy.score = self._score_strategy(strategy)
        
        return strategy
```

---

### 新闻-策略映射引擎

```python
# 新增模块: app/strategy/news_strategy_mapper.py

class NewsStrategyMapper:
    """
    新闻-策略映射引擎
    
    功能:
    1. 新闻分类 (财报/监管/并购/宏观)
    2. 历史相似事件检索
    3. 策略模板匹配
    4. 策略参数调优
    """
    
    # 新闻类型到策略的映射
    NEWS_STRATEGY_MAP = {
        "earnings_beat": {
            "strategy": "momentum",
            "entry": "立即买入",
            "exit": "追踪止盈 (ATR×2)",
            "position": "15-20%",
            "holding_period": "1-3天",
        },
        "earnings_miss": {
            "strategy": "mean_reversion",
            "entry": "等待企稳",
            "exit": "反弹5%退出",
            "position": "5-10%",
            "holding_period": "1-2天",
        },
        "regulatory_penalty": {
            "strategy": "exit",
            "entry": "立即减仓",
            "exit": "清仓",
            "position": "0%",
            "holding_period": "回避",
        },
        "merger_acquisition": {
            "strategy": "event_driven",
            "entry": "买入被并购方",
            "exit": "并购完成退出",
            "position": "20-30%",
            "holding_period": "1-4周",
        },
        "macro_fed_rate": {
            "strategy": "macro_allocation",
            "entry": "根据方向调整",
            "exit": "数据消化后",
            "position": "调整±10%",
            "holding_period": "3-5天",
        },
    }
    
    async def map_news_to_strategy(
        self,
        news: NewsItem,
        historical_events: List[HistoricalEvent]
    ) -> StrategyTemplate:
        """
        将新闻映射到策略模板
        
        Args:
            news: 新闻条目
            historical_events: 历史相似事件
        
        Returns:
            StrategyTemplate: 策略模板
        """
        # 1. 新闻分类
        category = self._classify_news(news)
        
        # 2. 获取基础策略
        base_strategy = self.NEWS_STRATEGY_MAP.get(category)
        
        if not base_strategy:
            return None
        
        # 3. 历史事件调整
        if historical_events:
            adjustment = self._learn_from_history(
                historical_events,
                base_strategy
            )
            base_strategy.update(adjustment)
        
        # 4. LLM优化
        optimized = await self._llm_optimize_strategy(
            news,
            base_strategy,
            historical_events
        )
        
        return optimized
```

---

## 📊 数据流设计

```
┌─────────────────────────────────────────────────────────────┐
│                      数据采集层                               │
│                                                             │
│  RSSHub Futu → 富途快讯 (3分钟缓存)                          │
│  金十数据    → 财经快讯 (5分钟缓存)                          │
│  财联社      → A股新闻 (5分钟缓存)                           │
│  FTSeek-US   → 美股数据 (实时)                               │
│  Futu OpenD    → 港股行情 (实时)                             │
│  Futu OpenD    → 资金流向 (1分钟)                            │
└──────────────────────┬──────────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                      数据分析层                               │
│                                                             │
│  新闻分析器 → 分类+情绪+关键词提取                            │
│  技术分析器 → 七周期K线+指标计算                              │
│  情绪分析器 → 资金流+VIX+市场情绪                             │
│  LLM分析器  → 综合研判+策略生成                               │
└──────────────────────┬──────────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                      策略工厂层                               │
│                                                             │
│  盘前策略生成器 (08:30)                                      │
│    ├─ 隔夜新闻分析                                           │
│    ├─ 美股关联分析                                           │
│    ├─ 技术面分析                                             │
│    └─ LLM策略生成                                            │
│                                                             │
│  盘中动态策略引擎 (10:00-16:00)                              │
│    ├─ 新闻实时监听                                           │
│    ├─ 快速分类+影响评估                                       │
│    ├─ 历史相似事件检索                                        │
│    └─ 策略生成+推送                                          │
│                                                             │
│  盘后策略优化器 (16:00-18:00)                                │
│    ├─ 交易归因分析                                           │
│    ├─ 策略效果评估                                           │
│    ├─ LLM优化建议                                            │
│    └─ 策略参数更新                                           │
│                                                             │
│  夜间美股监控器 (21:00-06:00)                                │
│    ├─ 美股行情监控                                           │
│    ├─ 美股新闻分析                                           │
│    ├─ 次日影响预测                                           │
│    └─ 次日策略预演                                           │
└──────────────────────┬──────────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                      执行推送层                               │
│                                                             │
│  钉钉推送 → 交易指导+策略建议+风险提示                         │
│  共识引擎 → 信号评分+置信度计算                               │
│  风控引擎 → 策略风控检查                                      │
│  执行引擎 → 模拟/实盘下单                                     │
└─────────────────────────────────────────────────────────────┘
```

---

## 🔧 实施路径

### Phase 1: 盘前策略生成 (1-2周)

**优先级**: 🔴 最高

**任务**:
1. ✅ 创建 `app/strategy/pre_market_strategy_generator.py`
2. ✅ 集成RSSHub Futu新闻源 (已完成)
3. ✅ 集成FTSeek-US美股数据
4. ✅ 扩展现有LLM分析能力
5. ✅ 钉钉推送盘前指导
6. ✅ 定时任务配置 (08:30触发)

**预期效果**:
- 每日08:30推送3-5条核心策略
- 策略包含：方向、目标价、止损、仓位、逻辑
- LLM置信度标注

---

### Phase 2: 盘中动态策略 (2-3周)

**优先级**: 🟡 高

**任务**:
1. 创建 `app/strategy/intraday_news_strategy.py`
2. 新闻实时监听+快速分类
3. 历史相似事件数据库
4. LLM快速策略生成 (<30秒)
5. 钉钉推送交易机会
6. 时效性控制 (新闻<5分钟)

**预期效果**:
- 重大新闻到达后3分钟内推送策略
- 每日2-5条盘中机会
- 胜率>60%

---

### Phase 3: 盘后策略优化 (1-2周)

**优先级**: 🟡 高

**任务**:
1. 扩展现有归因分析系统
2. 创建 `app/review/strategy_optimizer.py`
3. LLM策略优化建议
4. 策略参数自动调优
5. 钉钉推送优化报告

**预期效果**:
- 每日16:30推送优化报告
- 策略胜率持续提升
- 参数自适应调整

---

### Phase 4: 夜间美股监控 (2-3周)

**优先级**: 🟢 中

**任务**:
1. 创建 `app/strategy/us_overnight_monitor.py`
2. FTSeek-US数据集成
3. 美股新闻分析
4. 次日港股影响预测
5. 重大事件实时推送

**预期效果**:
- 21:00-06:00持续监控
- 重大事件5分钟内推送
- 次日08:00生成策略预演

---

### Phase 5: 策略工厂完善 (持续优化)

**优先级**: 🟢 中

**任务**:
1. 策略模板库扩充 (10+策略类型)
2. 策略回测验证
3. 策略评分系统
4. 策略参数自动优化
5. A/B测试框架

**预期效果**:
- 策略库覆盖主要市场场景
- 策略胜率>65%
- 年化收益>30%

---

## 📈 预期收益

### 量化指标

| 指标 | 当前 | 目标 | 提升 |
|------|------|------|------|
| **日均交易机会** | 2-3 | 5-8 | +150% |
| **策略胜率** | 55% | 65% | +18% |
| **响应速度** | 10分钟 | 3分钟 | -70% |
| **年化收益** | 20% | 35% | +75% |
| **最大回撤** | 15% | 10% | -33% |

### 质化收益

1. **全时段覆盖**: 从单点(开盘)扩展到全时段(24小时)
2. **策略多样化**: 从单一跨市场套利扩展到6+策略类型
3. **自适应能力**: 策略参数自动优化，适应市场变化
4. **决策质量**: LLM+数据双重验证，减少主观偏差
5. **执行效率**: 从手动审核到半自动/全自动执行

---

## ⚠️ 风险控制

### 策略风控

1. **策略评分门槛**: score < 0.6 不推送
2. **仓位控制**: 单策略≤20%，总仓位≤80%
3. **止损机制**: 所有策略必须设置止损
4. **时效控制**: 新闻策略<5分钟，过期作废
5. **黑名单机制**: 连续亏损3次暂停该策略

### 系统风控

1. **LLM降级**: LLM不可用时使用规则策略
2. **数据源容错**: 多源备份，单一失效不影响
3. **推送限流**: 每小时≤5条，每日≤30条
4. **异常监控**: 系统异常自动告警
5. **人工审核**: 重大策略需人工确认

---

## 📝 配置文件

```yaml
# config/strategy_factory.yaml

# 盘前策略生成
pre_market:
  enabled: true
  trigger_time: "08:30"
  max_strategies: 5
  min_score: 0.6
  llm_timeout: 120
  
# 盘中动态策略
intraday:
  enabled: true
  news_age_threshold: 5  # 分钟
  max_strategies_per_hour: 3
  min_impact_severity: "high"
  llm_timeout: 30  # 快速模式
  
# 盘后策略优化
post_market:
  enabled: true
  trigger_time: "16:30"
  optimization_enabled: true
  auto_apply: false  # 需人工确认
  
# 夜间美股监控
us_overnight:
  enabled: true
  monitor_hours: "21:00-06:00"
  check_interval: 300  # 5分钟
  push_urgent_only: true
  
# 策略工厂
strategy_factory:
  templates:
    - momentum
    - mean_reversion
    - breakout
    - news_driven
    - cross_market
    - event_driven
  auto_select: true
  backtest_enabled: true
  min_backtest_days: 30
  
# 风控
risk_control:
  max_position_per_strategy: 0.2
  max_total_position: 0.8
  stop_loss_required: true
  max_consecutive_losses: 3
  push_rate_limit:
    per_hour: 5
    per_day: 30
```

---

## 🎯 总结

### 核心价值

1. **全时段**: 24小时不间断分析，不错过任何机会
2. **自动化**: 从数据采集到策略生成全自动
3. **智能化**: LLM深度参与，策略质量持续提升
4. **可追溯**: 全链路留痕，归因清晰
5. **可优化**: 盘后复盘，策略持续迭代

### 实施建议

1. **先易后难**: Phase 1 (盘前) → Phase 2 (盘中) → Phase 3 (盘后) → Phase 4 (夜间)
2. **快速验证**: Phase 1用1-2周完成，快速验证效果
3. **数据驱动**: 每个阶段都设置量化指标，用数据说话
4. **人工审核**: 前期人工审核所有策略，后期逐步自动化
5. **持续优化**: 每周复盘策略效果，持续迭代

---

**设计人**: FTSeek AI Assistant  
**日期**: 2026-04-12  
**版本**: v1.0
