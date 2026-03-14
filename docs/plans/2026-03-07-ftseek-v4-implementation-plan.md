# FTSeek V4 实施计划 - 华尔街日内交易策略师

**创建日期:** 2026-03-07  
**版本:** V4.0.0  
**执行模式:** Subagent-driven (TDD)

---

## 📋 任务分解总览

| 阶段 | 任务数 | 预计时间 | 里程碑 |
|------|--------|----------|--------|
| Phase 1: 核心架构 | 5 任务 | Week 1 | 数据层 + 分析器完成 |
| Phase 2: 策略引擎 | 4 任务 | Week 2 | 日内策略核心完成 |
| Phase 3: 执行与风控 | 5 任务 | Week 3 | 三层风控 + 智能执行 |
| Phase 4: 人设与表达 | 3 任务 | Week 4 | 交易老手语气生成 |
| Phase 5: 测试与优化 | 4 任务 | Week 5 | 回测验证 + 参数优化 |

**总计:** 21 任务 | **预计:** 5 周

---

## Phase 1: 核心架构 (Week 1)

### Task 1.1: 创建 V4 模块结构

**目标:** 建立 V4 新模块目录结构，与 V3 并存

**文件:**
- `/opt/ftseek/FTSeek/app/v4/__init__.py`
- `/opt/ftseek/FTSeek/app/v4/analyzers/__init__.py`
- `/opt/ftseek/FTSeek/app/v4/data/__init__.py`
- `/opt/ftseek/FTSeek/app/v4/strategy/__init__.py`

**验收:**
```bash
python3 -c "from app.v4 import *"  # 导入成功
```

---

### Task 1.2: 实现 PreMarketAnalyzer (盘前分析器)

**目标:** 盘前成交量分析，识别异常放量

**文件:**
- `/opt/ftseek/FTSeek/app/v4/analyzers/premarket.py`
- `/opt/ftseek/FTSeek/tests/v4/test_premarket.py`

**功能:**
- 盘前成交量 vs 正常时段对比
- 异常放量检测 (>3 倍平均)
- 买卖盘口分析
- 生成 PreMarketReport

**TDD 测试:**
```python
def test_volume_ratio_calculation():
    """测试量比计算"""
    analyzer = PreMarketAnalyzer()
    report = analyzer.analyze_volume("HK.00700")
    assert report.volume_ratio > 0
    assert report.is_abnormal == (report.volume_ratio >= 3.0)

def test_abnormal_detection():
    """测试异常检测"""
    # 模拟 5 倍量比数据
    report = analyzer.analyze_volume("HK.00700", mock_volume_ratio=5.0)
    assert report.abnormal_score >= 80
```

---

### Task 1.3: 实现 MomentumScanner (动量扫描器)

**目标:** 短期动量催化因素识别

**文件:**
- `/opt/ftseek/FTSeek/app/v4/analyzers/momentum_scanner.py`
- `/opt/ftseek/FTSeek/tests/v4/test_momentum_scanner.py`

**功能:**
- 财报行情扫描
- 生物科技催化识别
- 科技动量事件
- 新闻情绪分析
- 生成 CatalystOpportunity 列表

**TDD 测试:**
```python
def test_earnings_scan():
    """测试财报扫描"""
    scanner = MomentumScanner()
    catalysts = scanner.scan_earnings_plays()
    assert len(catalysts) > 0
    assert all(c.catalyst_type == "earnings" for c in catalysts)

def test_momentum_ranking():
    """测试动量排名"""
    opportunities = scanner.scan_all()
    top = opportunities[0]
    assert top.momentum_score >= opportunities[-1].momentum_score
```

---

### Task 1.4: 实现 PatternRecognizer (形态识别器)

**目标:** 技术突破形态识别（规则引擎方案）

**文件:**
- `/opt/ftseek/FTSeek/app/v4/analyzers/pattern_recognizer.py`
- `/opt/ftseek/FTSeek/tests/v4/test_pattern_recognizer.py`

**支持形态:**
- 杯柄形态 (Cup & Handle)
- 三角突破 (Triangle Breakout)
- 旗形整理 (Flag Pattern)
- VCP 形态 (Volatility Contraction)

**TDD 测试:**
```python
def test_cup_handle_detection():
    """测试杯柄形态检测"""
    recognizer = PatternRecognizer()
    # 模拟杯柄形态 K 线数据
    klines = generate_cup_handle_pattern()
    patterns = recognizer.detect_patterns("HK.00700", klines)
    assert any(p.pattern_type == "cup_and_handle" for p in patterns)
    assert patterns[0].completion_percent >= 80

def test_breakout_level():
    """测试突破级别判断"""
    pattern = recognizer.detect_breakout(klines)
    assert pattern.breakout_level in ["weak", "moderate", "strong"]
```

---

### Task 1.5: 实现 CatalystTracker (催化事件追踪器)

**目标:** 催化事件数据库与追踪

**文件:**
- `/opt/ftseek/FTSeek/app/v4/data/catalyst_db.py`
- `/opt/ftseek/FTSeek/app/v4/analyzers/catalyst_tracker.py`
- `/opt/ftseek/FTSeek/tests/v4/test_catalyst_tracker.py`

**功能:**
- 财报发布日历
- FDA 审批追踪 (美股生物科技)
- 产品发布事件
- 分析师评级变化
- 富途新闻 API 集成

**TDD 测试:**
```python
def test_upcoming_catalysts():
    """测试即将发生的催化事件"""
    tracker = CatalystTracker()
    catalysts = tracker.get_upcoming_catalysts(days=7)
    assert len(catalysts) > 0
    assert all(c.date >= today for c in catalysts)
    assert all(c.date <= today + 7 days for c in catalysts)

def test_catalyst_filter():
    """测试催化事件过滤"""
    tech_catalysts = tracker.filter_by_sector("technology")
    assert all(c.sector == "technology" for c in tech_catalysts)
```

---

## Phase 2: 策略引擎 (Week 2)

### Task 2.1: 实现 IntradayStrategy 核心类

**目标:** 日内交易策略核心逻辑

**文件:**
- `/opt/ftseek/FTSeek/app/v4/strategy/intraday.py`
- `/opt/ftseek/FTSeek/tests/v4/test_intraday_strategy.py`

**功能:**
- 盘前筛选 (09:00-09:30)
- 开盘确认 (09:30-10:00)
- 交易执行 (10:00-15:30)
- 收盘清理 (15:30-16:00)
- generate_signal() 主接口

**TDD 测试:**
```python
def test_signal_generation():
    """测试信号生成"""
    strategy = IntradayStrategy()
    market_data = get_mock_market_data()
    signal = strategy.generate_signal(market_data)
    assert signal.direction in ["LONG", "SHORT", "WATCH"]
    assert signal.entry_price is not None
    assert signal.stop_loss is not None
    assert signal.target_price is not None
    assert 0 <= signal.confidence <= 1.0

def test_session_phases():
    """测试交易时段相位"""
    assert strategy.get_current_phase("09:15") == "premarket_screening"
    assert strategy.get_current_phase("09:45") == "open_confirmation"
    assert strategy.get_current_phase("11:00") == "trade_execution"
    assert strategy.get_current_phase("15:45") == "close_cleanup"
```

---

### Task 2.2: 实现置信度评分系统

**目标:** 信号置信度量化评分

**文件:**
- `/opt/ftseek/FTSeek/app/v4/strategy/confidence_scorer.py`
- `/opt/ftseek/FTSeek/tests/v4/test_confidence_scorer.py`

**评分维度:**
- 技术面分数 (40%)
- 催化事件分数 (30%)
- 情绪面分数 (20%)
- 量能确认分数 (10%)

**TDD 测试:**
```python
def test_confidence_calculation():
    """测试置信度计算"""
    scorer = ConfidenceScorer()
    factors = {
        "technical": 0.8,
        "catalyst": 0.6,
        "sentiment": 0.7,
        "volume": 0.9
    }
    confidence = scorer.calculate(factors)
    expected = 0.8*0.4 + 0.6*0.3 + 0.7*0.2 + 0.9*0.1
    assert abs(confidence - expected) < 0.01

def test_confidence_level():
    """测试置信度分级"""
    assert scorer.get_level(0.85) == "STRONG"
    assert scorer.get_level(0.65) == "RECOMMENDED"
    assert scorer.get_level(0.50) == "WATCH"
    assert scorer.get_level(0.35) == "SKIP"
```

---

### Task 2.3: 整合多因子信号融合

**目标:** 整合盘前分析 + 动量扫描 + 形态识别

**文件:**
- `/opt/ftseek/FTSeek/app/v4/strategy/signal_fusion.py`
- `/opt/ftseek/FTSeek/tests/v4/test_signal_fusion.py`

**融合逻辑:**
```python
final_score = (
    premarket_score * 0.25 +
    momentum_score * 0.30 +
    pattern_score * 0.30 +
    catalyst_score * 0.15
)
```

**TDD 测试:**
```python
def test_fusion_score():
    """测试融合评分"""
    fusion = SignalFusion()
    scores = {
        "premarket": 0.8,
        "momentum": 0.7,
        "pattern": 0.9,
        "catalyst": 0.6
    }
    final = fusion.calculate(scores)
    assert 0 <= final <= 1.0
    assert final > 0.7  # 综合评分应高于阈值

def test_fusion_decision():
    """测试融合决策"""
    decision = fusion.make_decision(final_score=0.75)
    assert decision.action in ["BUY", "SELL", "WATCH", "SKIP"]
```

---

### Task 2.4: 实现 Watchlist 管理

**目标:** 动态观察列表管理

**文件:**
- `/opt/ftseek/FTSeek/app/v4/strategy/watchlist_manager.py`
- `/opt/ftseek/FTSeek/tests/v4/test_watchlist_manager.py`

**功能:**
- 盘前生成观察列表 (Top 10-20)
- 盘中动态调整
- 优先级排序
- 自动移除失效标的

**TDD 测试:**
```python
def test_watchlist_generation():
    """测试观察列表生成"""
    manager = WatchlistManager()
    watchlist = manager.generate_premarket_watchlist()
    assert len(watchlist) <= 20
    assert all(w.priority_score > 0 for w in watchlist)
    # 验证按优先级排序
    for i in range(len(watchlist) - 1):
        assert watchlist[i].priority_score >= watchlist[i+1].priority_score

def test_dynamic_update():
    """测试动态更新"""
    manager.update_watchlist(market_data)
    assert len(manager.watchlist) <= 20
    # 失效标的应被移除
    assert not any(w.is_stale for w in manager.watchlist)
```

---

## Phase 3: 执行与风控 (Week 3)

### Task 3.1: 实现三层风控系统 - 交易前风控

**目标:** PreTradeRisk 模块

**文件:**
- `/opt/ftseek/FTSeek/app/v4/risk/pre_trade.py`
- `/opt/ftseek/FTSeek/tests/v4/test_pre_trade_risk.py`

**检查项:**
- 可用资金检查
- 单日交易次数限制
- 股票黑名单过滤
- 催化事件验证

**TDD 测试:**
```python
def test_capital_check():
    """测试资金检查"""
    risk = PreTradeRisk()
    assert risk.check_capital(required=10000, available=15000) == True
    assert risk.check_capital(required=20000, available=15000) == False

def test_daily_limit_check():
    """测试日交易次数限制"""
    risk.set_daily_trades(8, max_limit=10)
    assert risk.check_daily_limit() == True
    risk.set_daily_trades(10, max_limit=10)
    assert risk.check_daily_limit() == False
```

---

### Task 3.2: 实现三层风控系统 - 交易中风控

**目标:** IntraTradeRisk 模块

**文件:**
- `/opt/ftseek/FTSeek/app/v4/risk/intra_trade.py`
- `/opt/ftseek/FTSeek/tests/v4/test_intra_trade_risk.py`

**功能:**
- 实时盈亏监控
- 移动止损触发
- 异常波动检测
- 仓位动态调整

**TDD 测试:**
```python
def test_trailing_stop():
    """测试移动止损"""
    risk = IntraTradeRisk()
    risk.set_entry_price(100)
    risk.set_trailing_stop(0.03)  # 3%
    
    # 价格上涨到 110，止损应上移到 106.7
    risk.update_current_price(110)
    assert risk.trailing_stop_price == 106.7
    
    # 价格下跌到 106，应触发止损
    risk.update_current_price(106)
    assert risk.should_stop_loss() == True

def test_abnormal_volatility():
    """测试异常波动检测"""
    risk.detect_abnormal_volatility(price_series)
    assert risk.is_abnormal == (volatility_ratio > threshold)
```

---

### Task 3.3: 实现三层风控系统 - 交易后风控

**目标:** PostTradeRisk 模块

**文件:**
- `/opt/ftseek/FTSeek/app/v4/risk/post_trade.py`
- `/opt/ftseek/FTSeek/tests/v4/test_post_trade_risk.py`

**功能:**
- 日终盈亏统计
- 连续亏损熔断
- 策略表现评估
- 周报/月报生成

**TDD 测试:**
```python
def test_consecutive_loss_circuit():
    """测试连续亏损熔断"""
    risk = PostTradeRisk()
    risk.record_loss()  # 连续 5 次
    risk.record_loss()
    risk.record_loss()
    risk.record_loss()
    risk.record_loss()
    assert risk.should_circuit_break() == True
    assert risk.cooling_off_remaining_minutes == 60

def test_daily_pnl_stats():
    """测试日终统计"""
    stats = risk.generate_daily_stats()
    assert stats.total_trades >= 0
    assert stats.win_rate >= 0 and stats.win_rate <= 1
    assert stats.net_pnl is not None
```

---

### Task 3.4: 实现 OrderRouter (订单路由)

**目标:** 智能订单路由

**文件:**
- `/opt/ftseek/FTSeek/app/v4/execution/order_router.py`
- `/opt/ftseek/FTSeek/tests/v4/test_order_router.py`

**功能:**
- 订单类型选择 (市价/限价)
- 富途 API 对接
- 订单状态追踪
- 失败重试机制

**TDD 测试:**
```python
def test_order_routing():
    """测试订单路由"""
    router = OrderRouter()
    signal = get_buy_signal()
    order = router.route(signal)
    assert order.side == "BUY"
    assert order.qty == signal.quantity
    assert order.type in ["MARKET", "LIMIT"]

def test_order_status_tracking():
    """测试订单状态追踪"""
    router.submit_order(order)
    status = router.get_order_status(order.id)
    assert status in ["PENDING", "FILLED", "CANCELLED", "REJECTED"]
```

---

### Task 3.5: 实现 SmartExecution (智能执行)

**目标:** VWAP/TWAP 智能执行算法

**文件:**
- `/opt/ftseek/FTSeek/app/v4/execution/smart_execution.py`
- `/opt/ftseek/FTSeek/tests/v4/test_smart_execution.py`

**功能:**
- VWAP 执行策略
- TWAP 执行策略
- 市场冲击最小化
- 执行质量评估

**TDD 测试:**
```python
def test_vwap_execution():
    """测试 VWAP 执行"""
    executor = SmartExecution(strategy="vwap")
    slices = executor.calculate_slices(total_qty=1000, duration_minutes=60)
    assert len(slices) > 0
    assert sum(s.qty for s in slices) == 1000
    # VWAP 应在成交量大的时段分配更多

def test_execution_quality():
    """测试执行质量评估"""
    quality = executor.evaluate_execution(filled_orders, vwap_benchmark)
    assert quality.slippage_bps is not None
    assert quality.market_impact is not None
```

---

## Phase 4: 人设与表达 (Week 4)

### Task 4.1: 实现 VoiceEngine (语气生成引擎)

**目标:** 交易老手语气生成

**文件:**
- `/opt/ftseek/FTSeek/app/v4/persona/voice_engine.py`
- `/opt/ftseek/FTSeek/tests/v4/test_voice_engine.py`

**语气特征:**
- 自信简洁
- 数据驱动
- 概率思维
- 风险意识

**TDD 测试:**
```python
def test_confident_tone():
    """测试自信语气"""
    voice = VoiceEngine()
    signal = get_strong_buy_signal()
    message = voice.generate_message(signal)
    assert "强烈" in message or "必须" in message
    assert len(message) < 200  # 简洁

def test_data_driven():
    """测试数据驱动表达"""
    message = voice.generate_message(signal)
    # 应包含具体数字
    assert any(char.isdigit() for char in message)
    # 应包含价格/百分比
    assert "%" in message or "$" in message or "HK$" in message
```

---

### Task 4.2: 实现 ActionableInsight (可执行建议生成)

**目标:** 生成可执行的交易建议

**文件:**
- `/opt/ftseek/FTSeek/app/v4/persona/actionable_insight.py`
- `/opt/ftseek/FTSeek/tests/v4/test_actionable_insight.py`

**输出模板:**
```
{股票代码} {方向}
入场：{价格}
止损：{价格}
目标：{价格}
胜率：{概率}%
盈亏比：1:{比率}
理由：{简短理由}
```

**TDD 测试:**
```python
def test_actionable_format():
    """测试可执行格式"""
    insight = ActionableInsight()
    output = insight.generate(signal)
    assert "入场" in output
    assert "止损" in output
    assert "目标" in output
    assert "胜率" in output

def test_no_vague_language():
    """测试无模糊语言"""
    output = insight.generate(signal)
    vague_words = ["可能", "也许", "考虑", "关注"]
    assert not any(word in output for word in vague_words)
```

---

### Task 4.3: 实现钉钉推送格式化

**目标:** 优化钉钉推送格式，符合交易老手风格

**文件:**
- `/opt/ftseek/FTSeek/app/v4/notifier/dingtalk_v4.py`
- `/opt/ftseek/FTSeek/tests/v4/test_dingtalk_v4.py`

**推送类型:**
- 盘前观察列表 (09:00)
- 交易信号推送 (实时)
- 风险监控警报 (实时)
- 收盘统计报告 (16:30)

**TDD 测试:**
```python
def test_signal_push_format():
    """测试信号推送格式"""
    notifier = DingTalkNotifier()
    message = notifier.format_signal(signal)
    assert message.contains_required_fields()
    assert message.length_within_limit()

def test_emergency_alert():
    """测试紧急警报"""
    alert = notifier.format_alert(alert_type="CIRCUIT_BREAK")
    assert "🚨" in alert or "⚠️" in alert
    assert alert.priority == "HIGH"
```

---

## Phase 5: 测试与优化 (Week 5)

### Task 5.1: 单元测试全覆盖

**目标:** 所有模块单元测试覆盖率≥80%

**文件:**
- `/opt/ftseek/FTSeek/tests/v4/test_all.py` (整合测试)

**执行:**
```bash
cd /opt/ftseek/FTSeek
pytest tests/v4/ -v --cov=app.v4 --cov-report=html
```

**验收:**
- 所有测试通过
- 覆盖率报告生成
- 关键路径 100% 覆盖

---

### Task 5.2: 简化版回测验证

**目标:** 基于历史信号的统计验证

**文件:**
- `/opt/ftseek/FTSeek/app/v4/backtest/simple_backtest.py`
- `/opt/ftseek/FTSeek/tests/v4/test_backtest.py`

**功能:**
- 加载历史信号
- 模拟执行结果
- 计算胜率/盈亏比/夏普比率
- 生成回测报告

**TDD 测试:**
```python
def test_backtest_metrics():
    """测试回测指标"""
    backtest = SimpleBacktest()
    results = backtest.run(signal_history, price_history)
    assert results.win_rate > 0
    assert results.profit_loss_ratio > 0
    assert results.sharpe_ratio is not None

def test_max_drawdown():
    """测试最大回撤计算"""
    results = backtest.run(...)
    assert results.max_drawdown <= expected_threshold
```

---

### Task 5.3: 参数优化

**目标:** 策略参数优化

**文件:**
- `/opt/ftseek/FTSeek/app/v4/optimization/parameter_optimizer.py`

**优化参数:**
- 置信度阈值
- 止损/止盈比例
- 仓位分配
- 风控参数

**方法:**
- 网格搜索
- 交叉验证
- 避免过拟合

---

### Task 5.4: 模拟盘测试

**目标:** 模拟盘运行验证

**文件:**
- `/opt/ftseek/FTSeek/scripts/run_paper_trading.py`

**流程:**
1. 配置模拟账户
2. 运行 V4 策略
3. 记录所有信号和执行
4. 每日复盘
5. 持续优化

**验收:**
- 连续 5 个交易日稳定运行
- 无重大 bug
- 收益/回撤符合预期

---

## 📝 执行说明

### Subagent 调度格式

每个任务使用以下格式调度 subagent:

```
Goal: [任务目标一句话]
Context: [为什么重要，对应哪个计划文件]
Files: [具体文件路径]
Constraints: [禁止事项 - 无范围蔓延，仅 TDD]
Verify: [如何确认成功 - 测试通过，具体命令]
Task text: [粘贴完整任务描述]
```

### TDD 强制要求

每个任务必须遵循：
1. 先写测试 (测试失败)
2. 实现代码 (测试通过)
3. 重构优化 (测试保持通过)
4. 提交 (包含测试 + 代码)

### 代码审查流程

每个任务完成后：
1. Spec Reviewer 确认代码符合规范
2. Code Quality Reviewer 确认代码质量
3. 两者都通过后标记任务完成

---

## ✅ 完成标准

### 功能完成
- [ ] 所有 21 个任务完成
- [ ] 所有测试通过
- [ ] 代码覆盖率≥80%

### 性能完成
- [ ] 单次信号生成<100ms
- [ ] 支持同时监控 100+ 股票
- [ ] 系统可用性≥99%

### 人设完成
- [ ] 输出语气符合交易老手风格
- [ ] 建议可执行，不含糊
- [ ] 置信度表达清晰
- [ ] 风险提示到位

---

**下一步:** 确认计划后，开始执行 Task 1.1
