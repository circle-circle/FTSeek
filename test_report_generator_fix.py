#!/usr/bin/env python3
"""
测试报告生成器修复
2026-03-14: 验证从 futu_cp2 的 trade_journal.json 读取数据
"""
import sys
import logging
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(level=logging.INFO)

def test_report_generator():
    """测试报告生成器数据源"""
    from app.v4.report.report_generator import ReportGenerator, FUTU_CP2_TRADE_JOURNAL
    
    print("="*60)
    print("测试报告生成器数据源修复")
    print("="*60)
    
    # 检查 futu_cp2 交易日志
    print(f"\n1. 检查 futu_cp2 交易日志...")
    print(f"   路径：{FUTU_CP2_TRADE_JOURNAL}")
    print(f"   存在：{FUTU_CP2_TRADE_JOURNAL.exists()}")
    
    if FUTU_CP2_TRADE_JOURNAL.exists():
        import json
        with open(FUTU_CP2_TRADE_JOURNAL, 'r') as f:
            data = json.load(f)
        trades = data.get('trades', [])
        print(f"   交易数量：{len(trades)}")
        
        # 显示最新交易
        if trades:
            print(f"\n   最新交易:")
            for t in trades[:3]:
                print(f"   - {t.get('ticker')}: {t.get('trade_direction')} "
                      f"@ {t.get('entry_price')} ({t.get('status')})")
    
    # 初始化报告生成器
    print(f"\n2. 初始化报告生成器...")
    db_path = '/opt/ftseek/FTSeek/data/ftseek.db'
    generator = ReportGenerator(db_path=db_path)
    
    # 测试加载 futu_cp2 交易
    print(f"\n3. 测试加载 futu_cp2 交易...")
    futu_trades = generator._load_futu_cp2_trades()
    print(f"   加载交易数：{len(futu_trades)}")
    
    # 测试查询交易
    print(f"\n4. 测试查询交易（今日）...")
    from datetime import datetime
    today = datetime.now().strftime('%Y-%m-%d')
    trades = generator._query_trades_by_date(start_date=today, end_date=today)
    print(f"   今日交易数：{len(trades)}")
    
    # 测试查询持仓
    print(f"\n5. 测试查询持仓...")
    positions = generator._query_positions()
    print(f"   持仓数量：{len(positions)}")
    for pos in positions[:5]:
        print(f"   - {pos.get('code')}: {pos.get('quantity')}股 "
              f"@ {pos.get('avg_cost', pos.get('price', 0)):.2f}")
    
    # 测试生成日报
    print(f"\n6. 测试生成日报...")
    try:
        report = generator.send_daily_report(code='0700.HK')
        print(f"   日报生成：{'成功' if report else '失败'}")
    except Exception as e:
        print(f"   日报生成：异常 - {e}")
    
    # 测试生成周报
    print(f"\n7. 测试生成周报...")
    try:
        report = generator.send_weekly_report(code='0700.HK')
        print(f"   周报生成：{'成功' if report else '失败'}")
    except Exception as e:
        print(f"   周报生成：异常 - {e}")
    
    print("\n" + "="*60)
    print("测试完成")
    print("="*60)

if __name__ == "__main__":
    test_report_generator()
