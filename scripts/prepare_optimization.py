#!/usr/bin/env python3
"""准备 AutoResearch 参数优化"""
import sys
sys.path.insert(0, '/root/.openclaw/workspace-planner')

from harness.agents import Strategist
import asyncio

async def prepare():
    """准备优化"""
    strategist = Strategist()
    
    # 定义参数空间
    param_spaces = {
        "confluence_v2": {
            "min_factors": [2, 3, 4],
            "rsi_period": [10, 12, 14, 16],
            "macd_fast": [8, 10, 12, 14],
            "macd_slow": [20, 24, 28, 32],
            "bb_std": [1.5, 1.8, 2.0, 2.2, 2.5],
            "st_multiplier": [2.5, 2.8, 3.0, 3.2, 3.5],
        },
        "supertrend": {
            "atr_period": [8, 10, 12, 14],
            "base_multiplier": [2.0, 2.3, 2.5, 2.8, 3.0],
            "range_adjust_factor": [1.0, 1.2, 1.3, 1.5],
        },
    }
    
    print("✅ AutoResearch 参数优化准备完成")
    print()
    print("参数空间:")
    for strategy, params in param_spaces.items():
        print(f"\n{strategy}:")
        for param, values in params.items():
            print(f"  - {param}: {values}")
    
    print("\n下一步:")
    print("1. 运行优化：python3 /opt/ftseek/FTSeek/scripts/run_optimization.py")
    print("2. 查看结果：/opt/ftseek/data/harness_analysis/optimize_*.json")

if __name__ == "__main__":
    asyncio.run(prepare())
