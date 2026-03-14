#!/usr/bin/env python3
"""
测试收盘统计修复
2026-03-14: 验证从富途 OpenD API 获取实际数据
"""
import sys
import logging
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(level=logging.INFO)

def test_daily_stats():
    """测试收盘统计生成"""
    from app.reporters.daily_stats import DailyStatsReporter
    
    reporter = DailyStatsReporter()
    
    print("="*60)
    print("测试收盘统计生成")
    print("="*60)
    
    # 测试今日统计
    print("\n1. 测试今日统计...")
    today_stats = reporter.get_today_stats()
    print(f"   今日信号：{today_stats['total_signals']}")
    print(f"   已执行：{today_stats['executed_signals']}")
    print(f"   忽略/延期：{today_stats['ignored_signals']}")
    print(f"   今日盈亏：HK${today_stats['today_pnl']:.2f} ({today_stats['today_pnl_percent']:.2f}%)")
    
    # 测试本周统计
    print("\n2. 测试本周统计...")
    weekly_stats = reporter.get_weekly_stats()
    print(f"   本周盈亏：HK${weekly_stats['weekly_pnl']:.2f} ({weekly_stats['weekly_pnl_percent']:.2f}%)")
    
    # 测试持仓获取
    print("\n3. 测试持仓获取...")
    positions = reporter.get_current_positions()
    print(f"   持仓数量：{len(positions)}")
    for pos in positions[:5]:
        print(f"   - {pos['code']}: {pos.get('quantity', 0)}股 @ HK${pos.get('entry_price', 0):.2f}")
    
    # 测试风控状态
    print("\n4. 测试风控状态...")
    risk_status = reporter.get_risk_status()
    print(f"   状态：{risk_status['status']}")
    print(f"   连续亏损：{risk_status['consecutive_losses']}")
    print(f"   当前回撤：{risk_status['current_drawdown']:.2f}%")
    
    # 生成完整报告
    print("\n5. 生成完整报告...")
    stats = reporter.generate_report()
    print(f"\n{reporter.format_message(stats)}")
    
    print("\n" + "="*60)
    print("测试完成")
    print("="*60)

if __name__ == "__main__":
    test_daily_stats()
