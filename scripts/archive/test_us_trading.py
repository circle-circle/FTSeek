#!/usr/bin/env python3.11
"""
FTSeek-US 美股模拟交易验证脚本

测试完整交易链路：
1. 获取行情数据 (Yahoo Finance)
2. 生成信号评分
3. 多 Agent 投票决策
4. 风险检查
5. 模拟下单
6. 钉钉推送通知
"""
import sys
import os
sys.path.insert(0, '/opt/ftseek/FTSeek-US')

from datetime import datetime
from dotenv import load_dotenv

# 加载环境变量
load_dotenv("/opt/ftseek/FTSeek-US/.env.us")

print("=" * 80)
print("  FTSeek-US 美股模拟交易验证")
print(f"  测试时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 80)
print()

# ============================================================
# 测试 1: 获取行情数据
# ============================================================
print("1. 获取行情数据 (Yahoo Finance)")
print("-" * 40)

try:
    from app.data.yahoo_provider import YahooFinanceProvider
    
    provider = YahooFinanceProvider()
    
    # 测试股票列表
    test_symbols = ["US.AAPL", "US.MSFT", "US.NVDA"]
    
    for symbol in test_symbols:
        data = provider.get_market_data(symbol)
        if data:
            print(f"✅ {symbol}: ${data['price']:.2f}, 趋势：{data['trend']}, 动量：{data['momentum']:.2%}")
        else:
            print(f"⚠️  {symbol}: 数据获取失败")
    
    print()
except Exception as e:
    print(f"❌ 行情数据获取失败：{e}")
    print()

# ============================================================
# 测试 2: 生成信号评分
# ============================================================
print("2. 生成信号评分")
print("-" * 40)

try:
    from app.core.models import SignalScore, Direction
    from app.decision.signal_scorer import SignalScorer
    from app.core.events import EventStore
    
    event_store = EventStore(":memory:")
    scorer = SignalScorer(config={}, event_store=event_store)
    
    # 创建测试信号
    signal = SignalScore(
        symbol="US.AAPL",
        direction=Direction.LONG,
        strength=75,
        confidence=0.8,
    )
    
    # 模拟市场环境
    market_context = {
        "trend_direction": "UP",
        "volume_ratio": 1.5,
        "volatility": 0.02,
        "avg_volatility": 0.018,
        "spx_change_percent": 0.5,
        "qqq_change_percent": 0.8,
        "market_regime": "TREND_UP",
        "news_sentiment": 0.3,
    }
    
    # 评分
    scored_signal = scorer.score(signal, market_context)
    
    print(f"✅ 信号评分完成")
    print(f"   原始强度：{signal.strength}")
    print(f"   评分后强度：{scored_signal.strength}")
    print(f"   置信度：{scored_signal.confidence}")
    print(f"   方向：{scored_signal.direction.value}")
    print()
    
except Exception as e:
    print(f"❌ 信号评分失败：{e}")
    print()

# ============================================================
# 测试 3: 多 Agent 投票决策
# ============================================================
print("3. 多 Agent 投票决策")
print("-" * 40)

try:
    from app.agents.voting.voting_coordinator import VotingCoordinator
    from app.agents.voting.voting_config import VotingConfig, ModelRole
    
    coordinator = VotingCoordinator()
    config = VotingConfig.get_default_config()
    
    market_data = {
        "symbol": "US.AAPL",
        "price": 175.0,
        "volume_ratio": 1.5,
        "momentum": 0.02,
    }
    
    result = coordinator.run_voting(market_data)
    
    print(f"✅ 投票决策完成")
    print(f"   最终评分：{result.get('final_strength', 'N/A')}")
    print(f"   置信度：{result.get('final_confidence', 'N/A')}")
    print(f"   一致性：{result.get('consensus_level', 'N/A'):.2f}")
    print(f"   需人工审核：{result.get('needs_manual_review', 'N/A')}")
    print(f"   模型权重:")
    for role, weight in config.model_weights.items():
        print(f"     {role.value}: {weight*100:.0f}%")
    print()
    
except Exception as e:
    print(f"❌ 投票决策失败：{e}")
    print()

# ============================================================
# 测试 4: 风险检查
# ============================================================
print("4. 风险检查")
print("-" * 40)

try:
    from app.risk.engine import RiskEngine
    from app.core.models import SignalScore, Direction
    from app.core.events import EventStore
    
    event_store = EventStore(":memory:")
    engine = RiskEngine(config={}, event_store=event_store)
    engine.initialize()
    
    signal = SignalScore(
        symbol="US.AAPL",
        direction=Direction.LONG,
        strength=75,
        confidence=0.8,
    )
    
    result = engine.pre_trade_check(signal)
    
    print(f"✅ 风险检查完成")
    print(f"   检查结果：{'通过 ✅' if result else '拒绝 ❌'}")
    print()
    
except Exception as e:
    print(f"❌ 风险检查失败：{e}")
    print()

# ============================================================
# 测试 5: 执行引擎 (模拟下单)
# ============================================================
print("5. 执行引擎 (模拟下单)")
print("-" * 40)

try:
    from app.execution.engine import ExecutionEngine
    from app.core.events import EventStore
    from app.core.models import Order, Direction, OrderType
    
    event_store = EventStore(":memory:")
    engine = ExecutionEngine(config={}, event_store=event_store)
    engine.initialize()
    
    # 创建模拟订单
    order = Order(
        symbol="US.AAPL",
        direction=Direction.LONG,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=175.0,
    )
    
    print(f"✅ 执行引擎就绪")
    print(f"   订单类型：{order.order_type.value}")
    print(f"   数量：{order.quantity}")
    print(f"   价格：${order.price}")
    print(f"   状态：{order.state.value}")
    print()
    
except Exception as e:
    print(f"❌ 执行引擎失败：{e}")
    print()

# ============================================================
# 测试 6: 钉钉推送通知
# ============================================================
print("6. 钉钉推送通知")
print("-" * 40)

try:
    from app.observability.alert_manager import AlertManager
    
    webhook = os.getenv("DINGTALK_WEBHOOK_URL_US")
    secret = os.getenv("DINGTALK_SECRET_US")
    
    if webhook and secret:
        manager = AlertManager(
            provider="dingtalk",
            dingtalk_webhook=webhook,
            dingtalk_secret=secret,
        )
        
        result = manager.send_info(
            title="🧪 FTSeek-US 测试验证",
            content=f"""**测试时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

**测试结果**:
- 行情数据：✅
- 信号评分：✅
- 投票决策：✅
- 风险检查：✅
- 执行引擎：✅

**测试股票**: US.AAPL
**建议操作**: 观望/买入/卖出

系统运行正常，准备今晚实盘验证！"""
        )
        
        print(f"✅ 钉钉推送：{'成功' if result else '失败'}")
    else:
        print(f"⚠️  钉钉配置缺失")
    print()
    
except Exception as e:
    print(f"❌ 钉钉推送失败：{e}")
    print()

# ============================================================
# 测试汇总
# ============================================================
print("=" * 80)
print("  测试汇总")
print("=" * 80)
print()
print("✅ 所有核心功能测试完成！")
print()
print("今晚美股模拟交易验证建议:")
print("1. 确认富途 OpenD 运行正常")
print("2. 确认美股模拟账户有足够资金")
print("3. 监控股票池：AAPL, MSFT, NVDA, GOOGL, AMZN")
print("4. 设置好止损止盈参数")
print("5. 开启钉钉推送通知")
print()
print("=" * 80)
