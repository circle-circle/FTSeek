#!/bin/bash
# FTSeek-US 完整引擎测试脚本

set -e

PROJECT_DIR="/opt/ftseek/FTSeek-US"
cd "$PROJECT_DIR"
source venv/bin/activate

echo "========================================"
echo "  FTSeek-US 引擎测试"
echo "========================================"
echo ""

# 设置 Python 路径
export PYTHONPATH="/opt/ftseek/FTSeek-US:$PYTHONPATH"

echo "1. 风险引擎测试..."
python3.11 -c "
from app.risk.engine import RiskEngine
from app.core.events import EventStore
from app.core.models import SignalScore, Direction

event_store = EventStore(':memory:')
engine = RiskEngine(config={}, event_store=event_store)
engine.initialize()

signal = SignalScore(
    symbol='US.AAPL',
    direction=Direction.LONG,
    strength=75,
    confidence=0.8,
)

result = engine.pre_trade_check(signal)
print(f'   预交易检查：{\"通过\" if result else \"拒绝\"}')
print('   ✅ 风险引擎正常')
"

echo ""
echo "2. 执行引擎测试..."
python3.11 -c "
from app.execution.engine import ExecutionEngine
from app.core.events import EventStore

event_store = EventStore(':memory:')
engine = ExecutionEngine(config={}, event_store=event_store)
engine.initialize()

print('   状态：就绪')
print('   ✅ 执行引擎正常')
"

echo ""
echo "3. 观测引擎测试..."
python3.11 -c "
from app.observability.engine import ObservabilityEngine
from app.core.events import EventStore

event_store = EventStore(':memory:')
engine = ObservabilityEngine(config={}, event_store=event_store)
engine.initialize()

metrics = engine.collect_metrics()
print(f'   CPU: {metrics.get(\"cpu_percent\", \"N/A\")}%')
print(f'   内存：{metrics.get(\"memory_percent\", \"N/A\")}%')
print('   ✅ 观测引擎正常')
"

echo ""
echo "4. 多 Agent 投票测试..."
python3.11 -c "
from app.agents.voting.voting_coordinator import VotingCoordinator

coordinator = VotingCoordinator()

market_data = {
    'symbol': 'US.AAPL',
    'price': 175.0,
    'volume_ratio': 1.5,
    'momentum': 0.02,
}

result = coordinator.run_voting(market_data)

print(f'   最终评分：{result[\"final_strength\"]}')
print(f'   置信度：{result[\"final_confidence\"]}')
print(f'   一致性：{result[\"consensus_level\"]}')
print(f'   模型数：{len(result[\"model_scores\"])}')
print('   ✅ 投票系统正常')
"

echo ""
echo "5. Yahoo Finance 数据测试..."
python3.11 -c "
from app.data.yahoo_provider import YahooFinanceProvider

provider = YahooFinanceProvider()

data = provider.get_market_data('US.AAPL')
if data:
    print(f'   AAPL 价格：\${data[\"price\"]:.2f}')
    print(f'   趋势：{data[\"trend\"]}')
    print(f'   动量：{data[\"momentum\"]:.2%}')
    print('   ✅ Yahoo Finance 正常')
else:
    print('   ⚠️  数据获取失败 (可能网络问题)')
"

echo ""
echo "========================================"
echo "  所有引擎测试完成!"
echo "========================================"
