# FTSeek 量化评价与反馈系统 - 验证方案

**版本**: 1.0  
**创建日期**: 2026-04-05

---

## 一、验证目标

验证整个量化评价与反馈系统是否：
1. **逻辑正确** - 各模块按预期工作
2. **链路通畅** - 数据从信号到配置更新全流程贯通
3. **性能达标** - 响应时间、吞吐量满足要求
4. **收益提升** - 优化建议确实改善交易表现
5. **稳定可靠** - 长期运行无故障

---

## 二、验证框架

### 2.1 验证层次

```
┌─────────────────────────────────────────────────────────────┐
│                    Level 5: 收益验证                         │
│          实盘/模拟盘对比，统计显著性检验                      │
├─────────────────────────────────────────────────────────────┤
│                    Level 4: 稳定性验证                       │
│          72 小时连续运行，异常恢复测试                        │
├─────────────────────────────────────────────────────────────┤
│                    Level 3: 性能验证                         │
│          响应时间、吞吐量、资源消耗                           │
├─────────────────────────────────────────────────────────────┤
│                    Level 2: 链路验证                         │
│          端到端数据流，集成点验证                             │
├─────────────────────────────────────────────────────────────┤
│                    Level 1: 逻辑验证                         │
│          单元测试，模块功能验证                               │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 验证矩阵

| 验证类型 | 测试方法 | 验收标准 | 优先级 |
|----------|----------|----------|--------|
| 逻辑验证 | 单元测试 | 覆盖率>90% | P0 |
| 链路验证 | 集成测试 | 100% 链路贯通 | P0 |
| 性能验证 | 压力测试 | 响应<100ms | P1 |
| 收益验证 | 回测对比 | 胜率提升>5% | P1 |
| 稳定性验证 | 耐久测试 | 72 小时无故障 | P2 |

---

## 三、Level 1: 逻辑验证

### 3.1 TradeChain 数据层验证

**测试目标**: 验证 4 层数据结构的完整性

```python
# tests/verification/test_trade_chain_logic.py

def test_signal_layer_completeness():
    """验证信号层字段完整性"""
    chain = TradeChain()
    chain.signal = SignalLayer(
        signal_id="SIG-001",
        signal_score=0.75,
        signal_confidence=0.80,
        consensus_passed=True,
        consensus_weights={'technical': 0.4, 'sentiment': 0.3},
        market_regime='trending_up',
        volatility_atr=0.025,
        ai_sentiment_score=0.78
    )
    
    # 验证所有字段可访问
    assert chain.signal.signal_score == 0.75
    assert chain.signal.consensus_passed == True
    assert 'technical' in chain.signal.consensus_weights

def test_decision_layer_completeness():
    """验证决策层字段完整性"""
    # ...类似测试

def test_execution_layer_completeness():
    """验证执行层字段完整性"""
    # ...类似测试

def test_result_layer_completeness():
    """验证结果层字段完整性"""
    # ...类似测试
```

**验收标准**:
- [ ] 所有字段可正确读写
- [ ] to_dict()/from_dict() 序列化正常
- [ ] 默认值合理

---

### 3.2 TradeChainRecorder 验证

**测试目标**: 验证全链路记录器的 CRUD 操作

```python
# tests/verification/test_recorder_logic.py

def test_start_chain():
    """验证链路启动"""
    recorder = get_trade_chain_recorder()
    chain = recorder.start_chain(
        trade_id="TEST-001",
        code="00700.HK",
        signal_data={...},
        decision_data={...}
    )
    
    assert chain.trade_id == "TEST-001"
    assert recorder.get_chain("TEST-001") is not None

def test_record_execution():
    """验证执行记录"""
    recorder.record_execution("TEST-001", {
        'expected_price': 380.0,
        'actual_fill_price': 380.5,
        'slippage_bps': 13.16
    })
    
    chain = recorder.get_chain("TEST-001")
    assert chain.execution.actual_fill_price == 380.5
    assert chain.execution.slippage_bps == 13.16

def test_complete_chain():
    """验证链路完成"""
    recorder.complete_chain("TEST-001", {
        'exit_price': 395.0,
        'pnl': 1500.0,
        'exit_reason': 'take_profit'
    })
    
    chain = recorder.get_chain("TEST-001")
    assert chain.is_closed == True
    assert chain.result.pnl == 1500.0
```

**验收标准**:
- [ ] start_chain → record_execution → complete_chain 流程正确
- [ ] 数据持久化到 JSON 文件
- [ ] 支持按 trade_id 查询

---

### 3.3 策略归因分析验证

**测试目标**: 验证归因分析算法正确性

```python
# tests/verification/test_attribution_logic.py

def test_by_strategy_calculation():
    """验证按策略分组计算"""
    attribution = get_strategy_attribution()
    metrics = attribution.by_strategy()
    
    # 验证胜率计算
    for strategy, m in metrics.items():
        if m.total_trades > 0:
            expected_win_rate = m.winning_trades / m.total_trades
            assert abs(m.win_rate - expected_win_rate) < 0.001

def test_profit_factor_calculation():
    """验证盈亏比计算"""
    # profit_factor = avg_win / avg_loss
    pass

def test_sharpe_ratio_calculation():
    """验证 Sharpe 比率计算"""
    # sharpe = mean(pnls) / stdev(pnls)
    pass
```

**验收标准**:
- [ ] 胜率计算误差 < 0.1%
- [ ] 盈亏比计算正确
- [ ] Sharpe 比率计算正确

---

## 四、Level 2: 链路验证

### 4.1 端到端链路验证

**测试目标**: 验证从信号产生到配置更新的完整链路

```python
# tests/verification/test_end_to_end.py

def test_signal_to_chain链路():
    """验证信号→TradeChain 链路"""
    # 1. ConsensusEngine 生成信号
    engine = ConsensusEngine()
    result = engine.on_consensus("00700.HK", signal_data)
    
    # 2. 验证 TradeChain 已创建
    from app.storage.trade_recorder import get_trade_chain_recorder
    recorder = get_trade_chain_recorder()
    trade_chain_id = result.__dict__.get('_trade_chain_id')
    
    assert trade_chain_id is not None
    assert recorder.get_chain(trade_chain_id) is not None

def test_decision_to_execution 链路 ():
    """验证决策→执行记录链路"""
    # 1. DecisionEngine 处理信号
    signal = {
        'code': '00700.HK',
        'action': SignalAction.BUY,
        'price': 380.0,
        '_trade_chain_id': 'TEST-CHAIN-001'
    }
    
    engine = DecisionEngine(db_path=':memory:')
    result = engine.process_signal(signal)
    
    # 2. 验证执行层已记录
    recorder = get_trade_chain_recorder()
    chain = recorder.get_chain('TEST-CHAIN-001')
    
    assert chain.execution.actual_fill_price > 0
    assert chain.execution.slippage_bps >= 0

def test_exit_to_result 链路 ():
    """验证退出→结果记录链路"""
    # 1. 触发自动退出
    # 2. 验证结果层已填充
    pass
```

**验收标准**:
- [ ] 信号产生后 TradeChain 立即创建
- [ ] 执行后执行层数据立即更新
- [ ] 退出后结果层数据立即更新

---

### 4.2 集成点验证

**测试目标**: 验证各集成点的数据传递

```python
# tests/verification/test_integration_points.py

def test_consensus_engine_integration():
    """验证 ConsensusEngine 集成点"""
    # 检查 _record_signal_chain 方法存在并被调用
    engine = ConsensusEngine()
    assert hasattr(engine, '_record_signal_chain')

def test_decision_engine_integration():
    """验证 DecisionEngine 集成点"""
    engine = DecisionEngine(db_path=':memory:')
    
    # 检查记录方法存在
    assert hasattr(engine, '_record_execution_chain')
    assert hasattr(engine, '_record_exit_chain')
    assert hasattr(engine, '_trade_chain_map')

def test_trade_recorder_enhancement():
    """验证 TradeRecorder 增强"""
    recorder = get_trade_recorder()
    
    # 检查 TradeChainRecorder 方法
    assert hasattr(recorder, 'start_chain')
    assert hasattr(recorder, 'record_execution')
    assert hasattr(recorder, 'complete_chain')
```

**验收标准**:
- [ ] 所有集成点方法存在
- [ ] 数据在各模块间正确传递

---

## 五、Level 3: 性能验证

### 5.1 响应时间测试

**测试目标**: 验证各操作的响应时间

```python
# tests/verification/test_performance.py

import time

def test_start_chain_latency():
    """测试链路启动延迟"""
    recorder = get_trade_chain_recorder()
    
    start = time.perf_counter()
    recorder.start_chain("PERF-001", "00700.HK", {...}, {...})
    end = time.perf_counter()
    
    latency_ms = (end - start) * 1000
    assert latency_ms < 50  # < 50ms

def test_record_execution_latency():
    """测试执行记录延迟"""
    recorder = get_trade_chain_recorder()
    
    start = time.perf_counter()
    recorder.record_execution("PERF-001", {...})
    end = time.perf_counter()
    
    latency_ms = (end - start) * 1000
    assert latency_ms < 30  # < 30ms

def test_attribution_analysis_latency():
    """测试归因分析延迟"""
    attribution = get_strategy_attribution()
    
    start = time.perf_counter()
    report = attribution.get_full_report()
    end = time.perf_counter()
    
    latency_ms = (end - start) * 1000
    assert latency_ms < 500  # < 500ms
```

**验收标准**:
- [ ] start_chain < 50ms
- [ ] record_execution < 30ms
- [ ] get_full_report < 500ms

---

### 5.2 吞吐量测试

**测试目标**: 验证系统并发处理能力

```python
# tests/verification/test_throughput.py

import threading
from concurrent.futures import ThreadPoolExecutor

def test_concurrent_chain_creation():
    """测试并发创建链路"""
    recorder = get_trade_chain_recorder()
    
    def create_chain(i):
        recorder.start_chain(f"CONC-{i:04d}", "00700.HK", {...}, {...})
    
    # 100 个并发请求
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(create_chain, i) for i in range(100)]
        for f in futures:
            f.result()
    
    # 验证所有链路已创建
    assert len(recorder.chains) >= 100

def test_high_frequency_signals():
    """测试高频信号处理"""
    # 模拟 1000 个信号/秒
    pass
```

**验收标准**:
- [ ] 支持 100 并发无错误
- [ ] 支持 1000 信号/秒吞吐

---

### 5.3 内存/资源消耗测试

```python
# tests/verification/test_resource_usage.py

import tracemalloc

def test_memory_usage():
    """测试内存使用"""
    tracemalloc.start()
    
    recorder = get_trade_chain_recorder()
    
    # 创建 1000 条链路
    for i in range(1000):
        recorder.start_chain(f"MEM-{i:04d}", "00700.HK", {...}, {...})
    
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    
    assert peak < 100 * 1024 * 1024  # < 100MB
```

**验收标准**:
- [ ] 1000 条链路内存 < 100MB
- [ ] 无内存泄漏

---

## 六、Level 4: 稳定性验证

### 4.1 72 小时连续运行测试

```python
# tests/verification/test_stability.py

import time
from datetime import datetime

def test_72h_continuous_run():
    """72 小时连续运行测试"""
    errors = []
    
    for hour in range(72):
        try:
            # 模拟正常交易流程
            simulate_trading_cycle()
            
            # 记录状态
            log_status(f"Hour {hour}: OK")
        except Exception as e:
            errors.append((hour, str(e)))
            log_status(f"Hour {hour}: ERROR - {e}")
        
        time.sleep(3600)  # 等待 1 小时
    
    # 验收：错误数 < 3
    assert len(errors) < 3, f"72 小时内发生{len(errors)}次错误"
```

**验收标准**:
- [ ] 72 小时无崩溃
- [ ] 错误数 < 3 次
- [ ] 内存增长 < 20%

---

### 4.2 异常恢复测试

```python
def test_file_io_error_recovery():
    """测试文件 IO 错误恢复"""
    # 模拟文件写入失败
    # 验证系统能降级运行
    
def test_data_corruption_recovery():
    """测试数据损坏恢复"""
    # 模拟 JSON 文件损坏
    # 验证系统能重建或跳过
```

**验收标准**:
- [ ] IO 错误时不崩溃
- [ ] 数据损坏时可恢复

---

## 七、Level 5: 收益验证

### 5.1 回测对比测试

```python
# tests/verification/test_backtest_comparison.py

def test_optimization_improvement():
    """验证优化建议带来的收益提升"""
    
    # 1. 基准回测 (无优化)
    baseline_result = run_backtest(
        start_date="2025-01-01",
        end_date="2025-06-30",
        use_optimization=False
    )
    
    # 2. 优化回测 (应用建议)
    optimized_result = run_backtest(
        start_date="2025-01-01",
        end_date="2025-06-30",
        use_optimization=True
    )
    
    # 3. 对比指标
    assert optimized_result.win_rate > baseline_result.win_rate
    assert optimized_result.sharpe > baseline_result.sharpe
    assert optimized_result.max_drawdown < baseline_result.max_drawdown
```

**验收标准**:
- [ ] 胜率提升 > 5%
- [ ] Sharpe 提升 > 10%
- [ ] 最大回撤降低 > 10%

---

### 5.2 前瞻性测试 (Forward Testing)

```python
def test_forward_performance():
    """验证样本外表现"""
    # 使用未见过的数据验证
    pass
```

---

## 八、验证报告模板

### 8.1 验证执行记录

```markdown
## 验证执行报告

**执行日期**: YYYY-MM-DD
**执行人**: [姓名]
**版本**: [代码版本]

### 执行摘要

| 验证级别 | 测试用例数 | 通过数 | 失败数 | 通过率 |
|----------|------------|--------|--------|--------|
| Level 1  | 20         | 20     | 0      | 100%   |
| Level 2  | 10         | 10     | 0      | 100%   |
| Level 3  | 5          | 5      | 0      | 100%   |
| Level 4  | 3          | 3      | 0      | 100%   |
| Level 5  | 2          | 2      | 0      | 100%   |

### 关键指标

| 指标 | 目标值 | 实际值 | 状态 |
|------|--------|--------|------|
| 胜率提升 | >5% | 7.2% | ✅ |
| Sharpe 提升 | >10% | 15.3% | ✅ |
| 响应时间 | <100ms | 45ms | ✅ |
| 72 小时错误 | <3 | 0 | ✅ |
```

---

## 九、执行计划

### Phase 1: 逻辑验证 (2 天)
- [ ] 编写单元测试
- [ ] 执行逻辑验证测试
- [ ] 修复发现的 bug

### Phase 2: 链路验证 (1 天)
- [ ] 编写集成测试
- [ ] 执行端到端测试
- [ ] 验证数据流完整性

### Phase 3: 性能验证 (1 天)
- [ ] 编写性能测试
- [ ] 执行压力测试
- [ ] 优化性能瓶颈

### Phase 4: 稳定性验证 (3 天)
- [ ] 启动 72 小时测试
- [ ] 监控资源消耗
- [ ] 记录并分析错误

### Phase 5: 收益验证 (2 天)
- [ ] 执行回测对比
- [ ] 分析统计显著性
- [ ] 生成验证报告

**总计**: 9 天

---

## 十、附录

### A. 测试数据生成器

```python
# tests/verification/test_data_generator.py

def generate_mock_trade_chain():
    """生成模拟 TradeChain 用于测试"""
    pass

def generate_mock_signals(count=1000):
    """生成模拟信号"""
    pass
```

### B. 性能基准

| 操作 | 基准时间 (ms) |
|------|---------------|
| start_chain | 30 |
| record_execution | 20 |
| complete_chain | 25 |
| get_full_report | 300 |

### C. 联系方式

如有问题，请联系开发团队。
