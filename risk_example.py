#!/usr/bin/env python3.8
"""
风险管理示例脚本

用法:
    cd /opt/ftseek/FTSeek
    export API_TOKEN="5a8f072c2cba9db1579dd829214a38112fdcddefe089d4ba58e05742097dd272"
    python3.8 risk_example.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.risk.risk_manager import RiskManager, RiskLimit
from app.risk.position_sizer import PositionSizingMethod
from app.risk.stop_loss import StopLossType


def run_risk_management_demo():
    """运行风险管理演示"""
    print("\n" + "="*80)
    print("  风险管理演示")
    print("="*80 + "\n")

    # 1. 创建风险管理器
    print("创建风险管理器...")
    limits = RiskLimit(
        max_position_percent=0.20,      # 单标的最大 20%
        max_total_exposure=0.80,        # 总仓位最大 80%
        max_daily_loss=0.05,            # 日亏损最大 5%
        max_drawdown=0.15,              # 最大回撤 15%
        max_trades_per_day=10,          # 每日最多 10 笔交易
    )
    
    risk_mgr = RiskManager(
        initial_capital=100000.0,
        risk_per_trade=0.02,            # 每笔风险 2%
        limits=limits,
    )
    
    print(f"初始资金：¥{risk_mgr.initial_capital:,.0f}")
    print(f"单笔风险：{risk_mgr.risk_per_trade:.1%}")
    print()

    # 2. 模拟交易信号
    signals = [
        {
            "code": "HK.00700",
            "action": "BUY",
            "price": 380.0,
            "strategy": "confluence_v2",
            "strength": 0.9,
        },
        {
            "code": "HK.09988",
            "action": "BUY",
            "price": 148.0,
            "strategy": "rsi",
            "strength": 0.7,
        },
        {
            "code": "US.AAPL",
            "action": "BUY",
            "price": 245.0,
            "strategy": "macd",
            "strength": 0.8,
        },
    ]

    # 3. 执行交易
    print("执行交易...")
    print("-" * 80)
    
    for signal in signals:
        code = signal["code"]
        price = signal["price"]
        
        # 检查风险限制
        can_trade, reason = risk_mgr.check_risk_limits(signal)
        
        if not can_trade:
            print(f"❌ {code}: {reason}")
            continue
        
        # 计算止损 (使用 ATR 止损)
        atr_value = price * 0.025  # 假设 ATR 为价格的 2.5%
        stop_loss = risk_mgr.stop_loss_calculator.calculate(
            entry_price=price,
            direction=signal["action"],
            method=StopLossType.ATR,
            atr_value=atr_value,
        )
        
        # 计算仓位
        position_size = risk_mgr.calculate_position(
            signal=signal,
            stop_loss_price=stop_loss.price,
            volatility=atr_value,
        )
        
        if position_size.quantity > 0:
            # 执行交易
            risk_mgr.execute_trade(signal, position_size, stop_loss)
            
            print(f"✅ {code}:")
            print(f"   数量：{position_size.quantity}股")
            print(f"   价值：¥{position_size.position_value:,.0f}")
            print(f"   止损：¥{stop_loss.price:.2f} ({stop_loss.distance_percent:.1%})")
            print(f"   风险：¥{position_size.risk_amount:,.0f} ({position_size.risk_percent:.1%})")
        else:
            print(f"⚠️  {code}: 仓位为 0 ({position_size.notes})")
    
    print("-" * 80)
    print()

    # 4. 更新价格并检查
    print("更新价格...")
    price_updates = {
        "HK.00700": 385.0,  # 上涨
        "HK.09988": 145.0,  # 下跌
        "US.AAPL": 250.0,   # 上涨
    }
    
    for code, new_price in price_updates.items():
        risk_mgr.update_position(code, new_price)
        position = risk_mgr.positions.get(code)
        if position:
            pnl = (new_price - position["entry_price"]) * position["quantity"]
            print(f"{code}: ¥{position['entry_price']:.0f} → ¥{new_price:.0f} | 盈亏：¥{pnl:,.0f}")
    
    print()

    # 5. 获取风险指标
    print("风险指标摘要:")
    print("=" * 80)
    summary = risk_mgr.get_summary()
    
    print(f"资金:")
    print(f"  总资金：¥{summary['capital']['total']:,.0f}")
    print(f"  可用：¥{summary['capital']['available']:,.0f}")
    print(f"  已用：¥{summary['capital']['used']:,.0f}")
    print()
    
    print(f"仓位:")
    print(f"  总仓位：{summary['exposure']['total']}")
    print(f"  持仓数：{summary['exposure']['positions']}")
    print()
    
    print(f"盈亏:")
    print(f"  未实现：{summary['pnl']['unrealized']}")
    print(f"  已实现：{summary['pnl']['realized']}")
    print(f"  总计：{summary['pnl']['total']}")
    print()
    
    print(f"风险:")
    print(f"  最大回撤：{summary['risk']['max_drawdown']}")
    print(f"  当前回撤：{summary['risk']['current_drawdown']}")
    print(f"  胜率：{summary['risk']['win_rate']}")
    print()
    
    print(f"今日:")
    print(f"  交易次数：{summary['today']['trades']}")
    print(f"  今日盈亏：{summary['today']['pnl']}")
    print("=" * 80)
    print()

    # 6. 测试风险限制
    print("测试风险限制...")
    print("-" * 80)
    
    # 尝试超出单标的限制
    large_signal = {
        "code": "HK.00700",
        "action": "BUY",
        "price": 385.0,
        "strength": 1.0,
    }
    
    can_trade, reason = risk_mgr.check_risk_limits(large_signal)
    print(f"加仓 HK.00700: {'✅ 允许' if can_trade else '❌ 拒绝'} - {reason}")
    
    print()
    print("演示完成!")
    print()

    return risk_mgr


def main():
    """主函数"""
    print("\n" + "="*80)
    print("  FTSeek 风险管理示例")
    print("="*80)
    
    risk_mgr = run_risk_management_demo()
    
    print("\n关键要点:")
    print("1. 使用固定风险法计算仓位 (每笔风险 2%)")
    print("2. ATR 止损动态调整 (2x ATR)")
    print("3. 多重风险限制 (单日、回撤、仓位)")
    print("4. 实时跟踪盈亏和风险指标")
    print("\n")


if __name__ == "__main__":
    main()
