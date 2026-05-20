#!/usr/bin/env python3
"""
FTSeek-US 监控器运行脚本

功能:
- 加载模拟持仓
- 启动价格监控
- 自动止损止盈
- 记录交易日志
"""
import sys
import os
import signal
import time
from datetime import datetime

sys.path.insert(0, '/opt/ftseek/FTSeek-US')

from app.us_stock_monitor import USStockMonitor, MonitorConfig
from app.futu_integration import get_futu_integration
from app.market_data_backup import get_market_backup

print("=" * 70)
print("FTSeek-US 美股监控器")
print("=" * 70)
print(f"启动时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print()

# ============================================================
#  配置
# ============================================================
config = MonitorConfig(
    check_interval=300,  # 5 分钟检查一次
    night_interval=900,  # 15 分钟检查一次 (夜间)
    default_stop_loss=-0.08,
    default_take_profit=0.25,
    trailing_stop_enabled=True,
    trailing_trigger=0.10,
    trailing_percent=0.05,
    dingtalk_enabled=True,
    push_on_trigger=True,
    push_daily_summary=True,
    auto_trade_enabled=True,  # 启用自动交易
    futu_host="127.0.0.1",
    futu_port=11111,
    futu_trd_env="SIMULATE"  # 模拟盘
)

# ============================================================
#  创建监控器
# ============================================================
monitor = USStockMonitor(config)

# 注册信号处理
def signal_handler(signum, frame):
    print(f"\n收到信号 {signum}, 停止监控...")
    monitor.stop()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ============================================================
#  启动监控
# ============================================================
try:
    print("【初始化】")
    print("-" * 70)
    monitor.initialize()
    
    print(f"\n【状态】")
    print(f"  运行状态：运行中")
    print(f"  持仓数量：{len(monitor.positions)}")
    print(f"  检查频率：{config.check_interval}秒 (交易时段)")
    print(f"  自动交易：{'启用' if config.auto_trade_enabled else '禁用'}")
    print(f"  模拟模式：是")
    
    print(f"\n【持仓列表】")
    if monitor.positions:
        for code, pos in monitor.positions.items():
            print(f"  {code}: {pos.qty}股 @ ${pos.entry_price:.2f}")
    else:
        print("  无持仓")
    
    print(f"\n【日志】")
    print(f"  文件：/opt/ftseek/FTSeek-US/logs/ftseek_us.log")
    print(f"  级别：INFO")
    
    print("\n" + "=" * 70)
    print("✅ 监控器已启动 (按 Ctrl+C 停止)")
    print("=" * 70)
    print()
    
    # 启动监控循环
    monitor.running = True
    monitor._monitor_loop()
    
except KeyboardInterrupt:
    print("\n\n用户中断，停止监控...")
    monitor.stop()
except Exception as e:
    print(f"\n❌ 启动失败：{e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
