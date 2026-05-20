#!/usr/bin/env python3
"""
自动止损计算器 - FTSeek-US

功能:
- 根据入场价自动计算止损价 (卖空 +8%, 做多 -5%)
- 根据入场价自动计算目标价 (卖空 -10%~-15%, 做多 +10%~+20%)
- 输出标准格式供交易脚本调用

使用示例:
    python auto_stoploss.py --symbol TSLA --entry 384.48 --direction SHORT
    python auto_stoploss.py --symbol JD --entry 29.15 --direction LONG

作者：FTSeek-US Elite Squad
版本：1.0 (2026-03-28 P0 改进)
"""

import argparse
import json
import sys

def calculate_stoploss(entry_price, direction, stoploss_pct=8.0):
    """
    计算止损价
    
    Args:
        entry_price: 入场价
        direction: 'SHORT' 或 'LONG'
        stoploss_pct: 止损百分比 (默认 8%)
    
    Returns:
        止损价
    """
    if direction == 'SHORT':
        # 卖空止损 = 入场价 × (1 + 止损%)
        return round(entry_price * (1 + stoploss_pct / 100), 2)
    else:
        # 做多止损 = 入场价 × (1 - 止损%)
        return round(entry_price * (1 - stoploss_pct / 100), 2)

def calculate_takeprofit(entry_price, direction, takeprofit_pct=10.0):
    """
    计算止盈价
    
    Args:
        entry_price: 入场价
        direction: 'SHORT' 或 'LONG'
        takeprofit_pct: 止盈百分比 (默认 10%)
    
    Returns:
        止盈价
    """
    if direction == 'SHORT':
        # 卖空止盈 = 入场价 × (1 - 止盈%)
        return round(entry_price * (1 - takeprofit_pct / 100), 2)
    else:
        # 做多止盈 = 入场价 × (1 + 止盈%)
        return round(entry_price * (1 + takeprofit_pct / 100), 2)

def main():
    parser = argparse.ArgumentParser(description='自动止损计算器 - FTSeek-US')
    parser.add_argument('--symbol', type=str, required=True, help='股票代码')
    parser.add_argument('--entry', type=float, required=True, help='入场价')
    parser.add_argument('--direction', type=str, required=True, 
                        choices=['SHORT', 'LONG'], help='方向')
    parser.add_argument('--stoploss-pct', type=float, default=8.0,
                        help='止损百分比 (默认：卖空 8%, 做多 5%)')
    parser.add_argument('--takeprofit-pct', type=float, default=10.0,
                        help='止盈百分比 (默认：卖空 10%, 做多 10%)')
    parser.add_argument('--json', action='store_true', help='输出 JSON 格式')
    
    args = parser.parse_args()
    
    # 根据方向调整默认止损百分比
    if args.direction == 'LONG' and args.stoploss_pct == 8.0:
        args.stoploss_pct = 5.0  # 做多止损默认 5%
    
    stoploss = calculate_stoploss(args.entry, args.direction, args.stoploss_pct)
    takeprofit = calculate_takeprofit(args.entry, args.direction, args.takeprofit_pct)
    
    if args.json:
        result = {
            'symbol': args.symbol,
            'direction': args.direction,
            'entry_price': args.entry,
            'stoploss_price': stoploss,
            'takeprofit_price': takeprofit,
            'stoploss_pct': f"+{args.stoploss_pct}%" if args.direction == 'SHORT' else f"-{args.stoploss_pct}%",
            'takeprofit_pct': f"-{args.takeprofit_pct}%" if args.direction == 'SHORT' else f"+{args.takeprofit_pct}%"
        }
        print(json.dumps(result, indent=2))
    else:
        print(f"\n{'='*50}")
        print(f"自动止损计算结果 - {args.symbol}")
        print(f"{'='*50}")
        print(f"方向：{args.direction}")
        print(f"入场价：${args.entry:.2f}")
        print(f"止损价：${stoploss:.2f} ({'+' if args.direction == 'SHORT' else '-'}{args.stoploss_pct}%)")
        print(f"止盈价：${takeprofit:.2f} ({'-' if args.direction == 'SHORT' else '+'}{args.takeprofit_pct}%)")
        print(f"{'='*50}\n")

if __name__ == '__main__':
    main()
