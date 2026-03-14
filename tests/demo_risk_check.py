#!/usr/bin/env python
"""
风控检查集成演示脚本

演示 RiskManager.check_before_trade() 的使用场景
"""
import sys
sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.risk.risk_manager import RiskManager, RiskConfig
from app.trader.executor import TradeExecutor
from app.strategy.base import Signal, SignalAction


def demo_risk_check():
    """演示风控检查功能"""
    print("=" * 60)
    print("风控检查集成演示")
    print("=" * 60)
    
    # 创建风控配置
    config = RiskConfig(
        min_confidence=0.60,
        max_single_position=0.20,
        max_daily_trades=10,
        max_daily_loss=0.05,
        max_drawdown=0.15,
        initial_cash=1000000.0
    )
    
    # 创建风控管理器
    risk_manager = RiskManager(limits=config)
    
    # 设置健康账户状态
    risk_manager.account = {
        "total_capital": 1000000.0,
        "available_cash": 800000.0,
        "positions": {},
        "peak_capital": 1000000.0
    }
    
    # 创建测试信号
    signal = Signal(
        id="demo_001",
        code="US.AAPL",
        name="Apple Inc",
        action=SignalAction.BUY,
        price=175.50,
        quantity=100,
        reason="技术面突破",
        strategy="动量策略"
    )
    signal.extra['confidence'] = 0.85
    
    print("\n📊 账户状态:")
    print(f"  总资金：${risk_manager.account['total_capital']:,.0f}")
    print(f"  可用资金：${risk_manager.account['available_cash']:,.0f}")
    print(f"  持仓数：{len(risk_manager.account['positions'])}")
    print(f"  峰值资金：${risk_manager.account['peak_capital']:,.0f}")
    
    print("\n📈 交易信号:")
    print(f"  代码：{signal.code}")
    print(f"  动作：{signal.action.value}")
    print(f"  价格：${signal.price:.2f}")
    print(f"  数量：{signal.quantity}")
    print(f"  置信度：{signal.extra['confidence']:.0%}")
    
    print("\n🔍 执行风控检查...")
    result = risk_manager.check_before_trade(signal)
    
    print(f"\n✅ 检查结果：{'通过' if result.passed else '❌ 失败'}")
    print("\n详细检查项:")
    for check_name, passed in result.details.items():
        status = "✅" if passed else "❌"
        print(f"  {status} {check_name}: {'通过' if passed else '失败'}")
    
    print(f"\n⏰ 检查时间：{result.timestamp}")
    
    # 演示失败场景
    print("\n" + "=" * 60)
    print("演示失败场景：置信度过低")
    print("=" * 60)
    
    low_conf_signal = Signal(
        id="demo_002",
        code="US.AAPL",
        name="Apple Inc",
        action=SignalAction.BUY,
        price=175.50,
        quantity=100,
        reason="信号较弱",
        strategy="动量策略"
    )
    low_conf_signal.extra['confidence'] = 0.45  # 低于阈值 60%
    
    result2 = risk_manager.check_before_trade(low_conf_signal)
    print(f"\n❌ 检查结果：{'通过' if result2.passed else '失败'}")
    print(f"  置信度检查：{'✅ 通过' if result2.details.get('confidence') else '❌ 失败'}")
    
    if not result2.passed:
        failed_checks = [k for k, v in result2.details.items() if not v]
        print(f"\n🚫 失败的检查项：{', '.join(failed_checks)}")
        print(f"💡 建议：拒绝此交易信号")
    
    print("\n" + "=" * 60)
    print("演示完成")
    print("=" * 60)


if __name__ == '__main__':
    demo_risk_check()
