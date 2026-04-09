# 快速验证指南

**用途**: 快速验证量化评价与反馈系统是否正常运行

---

## 一、一键运行所有验证

```bash
# 运行完整验证套件
python3 -m tests.verification.run_all_tests

# 预期输出:
# ======================================================================
# 验证测试摘要
# ======================================================================
# 总测试数：10
# 通过：10
# 失败：0
# 通过率：100.0%
# ======================================================================
```

---

## 二、分模块验证

### 2.1 验证 TradeChain 数据层

```bash
python3 -c "
from app.analytics.trade_chain import TradeChain, SignalLayer, DecisionLayer, ExecutionLayer, ResultLayer

# 创建测试链路
chain = TradeChain()
chain.signal = SignalLayer(signal_score=0.75, signal_confidence=0.80)
chain.decision = DecisionLayer(decision_reason='confluence', decision_confidence=8)
chain.execution = ExecutionLayer(expected_price=380.0, actual_fill_price=380.5)
chain.result = ResultLayer(exit_price=395.0, pnl=1500.0)

print('TradeChain 数据层验证：✅ 通过')
"
```

### 2.2 验证 TradeChainRecorder

```bash
python3 -c "
from app.storage.trade_recorder import get_trade_chain_recorder

recorder = get_trade_chain_recorder()

# 完整流程测试
recorder.start_chain('TEST-001', '00700.HK', 
    signal_data={'signal_score': 0.75},
    decision_data={'decision_reason': 'confluence'})

recorder.record_execution('TEST-001', {
    'expected_price': 380.0,
    'actual_fill_price': 380.5,
    'slippage_bps': 13.16
})

recorder.complete_chain('TEST-001', {
    'exit_price': 395.0,
    'pnl': 1500.0
})

print('TradeChainRecorder 验证：✅ 通过')
"
```

### 2.3 验证策略归因分析

```bash
python3 -c "
from app.analytics.strategy_attribution import get_strategy_attribution

attribution = get_strategy_attribution()
report = attribution.get_full_report()

print(f'策略归因验证：✅ 通过')
print(f'  - 总交易数：{report[\"total_chains\"]}')
print(f'  - 策略数量：{len(report[\"by_strategy\"])}')
"
```

### 2.4 验证信号相关性分析

```bash
python3 -c "
from app.analytics.signal_correlation import get_signal_quality_correlation

correlation = get_signal_quality_correlation()
report = correlation.get_full_report()

print(f'信号相关性验证：✅ 通过')
print(f'  - Pearson r = {report[\"score_pnl_correlation\"][\"pearson_r\"]:.4f}')
"
```

### 2.5 验证优化建议生成

```bash
python3 -c "
from app.analytics.optimization_advisor import generate_optimization_recommendations

recommendations = generate_optimization_recommendations(
    strategy_attribution={'by_strategy': {}},
    signal_correlation={'noise_signals': []},
    execution_stats={'avg_slippage_bps': 15}
)

print(f'优化建议生成验证：✅ 通过')
print(f'  - 生成建议数：{len(recommendations)}')
"
```

### 2.6 验证配置热加载

```bash
python3 -c "
from app.core.config_hot_reload import get_config_hot_reloader

reloader = get_config_hot_reloader()
config = reloader.get_current_config()
history = reloader.get_version_history()

print(f'配置热加载验证：✅ 通过')
print(f'  - 当前版本：{reloader.active_version_id}')
print(f'  - 版本数量：{len(history)}')
"
```

---

## 三、收益验证

```bash
# 运行回测对比分析
python3 -m tests.verification.backtest_comparison

# 预期输出:
# ======================================================================
# 收益验证报告 - 回测对比分析
# ======================================================================
# 实际表现:
#   总交易数：5
#   胜率：100.0%
#   总盈亏：6950.00
#   ...
```

---

## 四、查看验证报告

```bash
# 查看验证测试报告
cat /opt/ftseek/FTSeek/data/verification_report.json

# 查看收益验证报告
cat /opt/ftseek/FTSeek/data/backtest_comparison_report.json
```

---

## 五、验收标准检查清单

### Level 1: 逻辑验证
- [ ] TradeChain 4 层数据结构可创建
- [ ] TradeChainRecorder 可记录完整链路
- [ ] StrategyAttribution 可生成归因报告
- [ ] SignalCorrelation 可生成相关性分析
- [ ] OptimizationAdvisor 可生成优化建议
- [ ] ConfigHotReload 可获取配置和版本历史

### Level 2: 链路验证
- [ ] start_chain → record_execution → complete_chain 流程贯通
- [ ] 统计数据正确计算

### Level 3: 性能验证
- [ ] start_chain 响应时间 < 50ms
- [ ] get_full_report 响应时间 < 500ms

### Level 4: 收益验证
- [ ] 可生成回测对比报告
- [ ] 可识别改进机会

---

## 六、常见问题

### Q1: 测试失败 "No module named 'app.analytics'"

**解决**: 确保在项目根目录运行
```bash
cd /opt/ftseek/FTSeek
python3 -m tests.verification.run_all_tests
```

### Q2: "未找到 TradeChain" 错误

**解决**: 这是正常的，因为还没有实际交易数据。验证测试使用模拟数据。

### Q3: 收益验证显示 "暂无交易数据"

**解决**: 需要运行更多交易来积累数据。当前系统正常。

---

## 七、相关文档

- **验证方案**: `docs/VERIFICATION_PLAN.md`
- **验证报告**: `docs/VERIFICATION_REPORT.md`
- **完成报告**: `docs/QUANT_FEEDBACK_LOOP_COMPLETE.md`

---

**最后更新**: 2026-04-05
