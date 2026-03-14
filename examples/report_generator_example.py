#!/usr/bin/env python
"""
ReportGenerator 使用示例

演示如何生成和推送交易报告
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.v4.report.report_generator import ReportGenerator
import tempfile
import os

def main():
    # 创建临时数据库
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    
    try:
        # 初始化报告生成器
        generator = ReportGenerator(db_path=db_path)
        
        print("=" * 60)
        print("ReportGenerator 使用示例")
        print("=" * 60)
        print()
        
        # 1. 记录交易
        print("1. 记录交易数据...")
        generator.record_trade('0700.HK', 'BUY', 100, 380.5, 0, 0)
        generator.record_trade('0700.HK', 'SELL', 100, 385.2, 470, 0.0123)
        generator.record_trade('MU', 'BUY', 50, 85.3, 0, 0)
        print("   ✓ 记录了 3 笔交易")
        print()
        
        # 2. 更新持仓
        print("2. 更新持仓数据...")
        generator.update_position('0700.HK', 100, 385.2, 388.5, 'HK')
        generator.update_position('MU', 50, 85.3, 86.1, 'US')
        print("   ✓ 更新了 2 只股票的持仓")
        print()
        
        # 3. 更新绩效
        print("3. 更新绩效数据...")
        from datetime import datetime
        today = datetime.now().strftime('%Y-%m-%d')
        generator.update_performance('0700.HK', today, 0.032, 0.058, 2, 1, -0.01, 1.5)
        generator.update_performance('MU', today, 0.009, 0.009, 1, 0, 0, 0)
        print("   ✓ 更新了绩效统计")
        print()
        
        # 4. 生成日报
        print("4. 生成日报...")
        print("-" * 60)
        daily_report = generator.generate_daily_report()
        print(daily_report)
        print()
        
        # 5. 生成单只股票的日报
        print("5. 生成单只股票 (0700.HK) 的日报...")
        print("-" * 60)
        daily_report_single = generator.generate_daily_report(code='0700.HK')
        print(daily_report_single)
        print()
        
        # 6. 添加更多交易用于周报
        print("6. 添加更多交易数据用于周报...")
        from datetime import timedelta
        now = datetime.now()
        for i in range(4):
            trade_date = (now - timedelta(days=i)).isoformat()
            pnl = 500 if i % 2 == 0 else -200
            generator.record_trade('0700.HK', 'SELL' if i % 2 == 0 else 'BUY', 
                                 100, 380.5 + i, pnl, pnl/38000, trade_date)
        print("   ✓ 添加了 4 笔历史交易")
        print()
        
        # 7. 生成周报
        print("7. 生成周报...")
        print("-" * 60)
        weekly_report = generator.generate_weekly_report()
        print(weekly_report)
        print()
        
        # 8. 推送报告（需要配置 webhook）
        print("8. 推送报告...")
        # 实际使用时配置 webhook:
        # generator = ReportGenerator(db_path=db_path, 
        #                            dingtalk_webhook="https://oapi.dingtalk.com/robot/send?access_token=xxx")
        # result = generator.send_daily_report()
        # print(f"   推送结果：{result}")
        print("   (需要配置 webhook 才能推送)")
        print()
        
        print("=" * 60)
        print("示例完成!")
        print("=" * 60)
        
    finally:
        os.unlink(db_path)

if __name__ == '__main__':
    main()
