#!/usr/bin/env python
"""
AutoExitManager 集成演示

演示如何与 PaperTradingEngine 集成使用
"""
import sys
sys.path.insert(0, '/opt/ftseek/FTSeek')

from datetime import datetime, timedelta
from app.v4.core.exit_manager import AutoExitManager
from app.v4.trader.paper_trader import PaperPosition


def demo_basic_usage():
    """基础使用演示"""
    print("=" * 60)
    print("AutoExitManager 基础使用演示")
    print("=" * 60)
    
    # 创建退出管理器
    manager = AutoExitManager(
        target_profit=0.05,    # 5% 止盈
        stop_loss=0.03,        # 3% 止损
        timeout_hours=24,      # 24 小时超时
        trailing_stop=True,    # 启用 Trailing Stop
        trailing_pct=0.02      # 2% 回撤
    )
    
    print(f"\n配置：{manager.get_exit_config()}")
    
    # 场景 1: 止盈触发
    print("\n--- 场景 1: 收益率 +6% (应触发止盈) ---")
    position = PaperPosition(
        code="0700.HK",
        quantity=100,
        avg_cost=100.0,
        current_price=106.0,
        entry_time=datetime.now()
    )
    decision = manager.check_exit(position)
    if decision:
        print(f"✓ 退出决策：{decision.reason}")
        print(f"  预期盈亏：¥{decision.expected_pnl:.2f}")
        print(f"  预期收益率：{decision.expected_return*100:.2f}%")
    else:
        print("✗ 不退出")
    
    # 场景 2: 正常持仓
    print("\n--- 场景 2: 收益率 +3% (不触发) ---")
    position = PaperPosition(
        code="0700.HK",
        quantity=100,
        avg_cost=100.0,
        current_price=103.0,
        entry_time=datetime.now()
    )
    decision = manager.check_exit(position)
    if decision:
        print(f"✓ 退出决策：{decision.reason}")
    else:
        print("✗ 不退出 (继续持仓)")
    
    # 场景 3: 止损触发
    print("\n--- 场景 3: 收益率 -4% (应触发止损) ---")
    position = PaperPosition(
        code="0700.HK",
        quantity=100,
        avg_cost=100.0,
        current_price=96.0,
        entry_time=datetime.now()
    )
    decision = manager.check_exit(position)
    if decision:
        print(f"✓ 退出决策：{decision.reason}")
        print(f"  预期盈亏：¥{decision.expected_pnl:.2f}")
    else:
        print("✗ 不退出")
    
    # 场景 4: Trailing Stop
    print("\n--- 场景 4: Trailing Stop (回撤 3%) ---")
    manager.update_peak_price("MU", 100.0)
    position = PaperPosition(
        code="MU",
        quantity=50,
        avg_cost=90.0,
        current_price=97.0,
        entry_time=datetime.now()
    )
    decision = manager.check_exit(position)
    if decision:
        print(f"✓ 退出决策：{decision.reason}")
        print(f"  最高价：$100.0, 现价：${position.current_price}")
    else:
        print("✗ 不退出")
    
    # 场景 5: 超时退出
    print("\n--- 场景 5: 持仓 25 小时 (应触发超时) ---")
    position = PaperPosition(
        code="0700.HK",
        quantity=100,
        avg_cost=100.0,
        current_price=101.0,
        entry_time=datetime.now() - timedelta(hours=25)
    )
    decision = manager.check_exit(position)
    if decision:
        print(f"✓ 退出决策：{decision.reason}")
        print(f"  持仓时间：25 小时")
    else:
        print("✗ 不退出")
    
    print("\n" + "=" * 60)
    print("演示完成")
    print("=" * 60)


def demo_integration_with_paper_trader():
    """与 PaperTradingEngine 集成演示"""
    print("\n" + "=" * 60)
    print("与 PaperTradingEngine 集成演示")
    print("=" * 60)
    
    # 注意：实际集成时需要数据库
    # 这里仅演示接口调用方式
    
    print("""
集成示例代码:

```python
from app.v4.trader.paper_trader import PaperTradingEngine
from app.v4.core.exit_manager import AutoExitManager

# 初始化
trader = PaperTradingEngine(db_path='data/trading.db')
exit_manager = AutoExitManager(target_profit=0.05, stop_loss=0.03)

# 执行买入
trade = trader.buy(code="0700.HK", quantity=100, market_price=380.0)

# 定期更新价格并检查退出
def check_positions():
    positions = trader.get_all_positions()
    for position in positions:
        # 更新当前价格
        current_price = get_current_price(position.code)
        trader.update_price(position.code, current_price)
        
        # 更新最高价 (用于 Trailing Stop)
        exit_manager.update_peak_price(position.code, current_price)
        
        # 检查是否应该退出
        decision = exit_manager.check_exit(position)
        if decision and decision.should_exit:
            print(f"触发退出：{position.code} 原因={decision.reason}")
            # 执行卖出
            trader.sell(
                code=position.code,
                quantity=position.quantity,
                market_price=current_price
            )
            # 清除最高价记录
            exit_manager.clear_peak_price(position.code)
```
    """)
    
    print("=" * 60)


if __name__ == "__main__":
    demo_basic_usage()
    demo_integration_with_paper_trader()
    
    print("\n✅ AutoExitManager 已就绪，可与 PaperTradingEngine 集成使用")
