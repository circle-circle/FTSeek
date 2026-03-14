#!/usr/bin/env python3
"""
FTSeek 报告生成器端到端测试

测试周报和月报生成器的完整功能
"""
import sys
import os
import logging
from pathlib import Path
from datetime import datetime, timedelta

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("ftseek.test_reports")

from app.storage.database import Database
from app.reporters.weekly_report import WeeklyReportGenerator
from app.reporters.monthly_report import MonthlyReportGenerator


def create_test_data(db: Database):
    """创建测试交易数据"""
    logger.info("创建测试交易数据...")
    
    now = datetime.now()
    
    # 上周的交易数据 (用于周报测试)
    last_week_monday = now - timedelta(days=now.weekday() + 7)
    
    test_trades_week = [
        {
            'id': f'test_week_{i}',
            'code': code,
            'name': name,
            'action': 'BUY',
            'entry_price': entry_price,
            'exit_price': exit_price,
            'quantity': quantity,
            'pnl': (exit_price - entry_price) * quantity,
            'pnl_percent': ((exit_price - entry_price) / entry_price) * 100,
            'strategy': strategy,
            'entry_time': (last_week_monday + timedelta(days=i % 5)).isoformat(),
            'exit_time': (last_week_monday + timedelta(days=i % 5 + 2)).isoformat(),
            'status': 'closed',
        }
        for i, (code, name, entry_price, exit_price, quantity, strategy) in enumerate([
            ('HK.00700', '腾讯控股', 300.0, 320.0, 100, 'fusion_v2'),
            ('HK.09988', '阿里巴巴', 80.0, 85.0, 500, 'ma_cross'),
            ('HK.09992', '泡泡玛特', 50.0, 48.0, 200, 'rsi_rebound'),
            ('HK.00700', '腾讯控股', 320.0, 340.0, 100, 'fusion_v2'),
            ('HK.03690', '美团', 120.0, 125.0, 300, 'fusion_v2'),
        ])
    ]
    
    # 上月的交易数据 (用于月报测试)
    last_month = now.replace(day=1) - timedelta(days=1)
    if last_month.month == 12:
        last_month = last_month.replace(year=last_month.year - 1, month=1)
    else:
        last_month = last_month.replace(month=last_month.month)
    
    test_trades_month = [
        {
            'id': f'test_month_{i}',
            'code': code,
            'name': name,
            'action': 'BUY',
            'entry_price': entry_price,
            'exit_price': exit_price,
            'quantity': quantity,
            'pnl': (exit_price - entry_price) * quantity,
            'pnl_percent': ((exit_price - entry_price) / entry_price) * 100,
            'strategy': strategy,
            'entry_time': (last_month + timedelta(days=i * 3)).isoformat(),
            'exit_time': (last_month + timedelta(days=i * 3 + 5)).isoformat(),
            'status': 'closed',
        }
        for i, (code, name, entry_price, exit_price, quantity, strategy) in enumerate([
            ('HK.00700', '腾讯控股', 300.0, 330.0, 100, 'fusion_v2'),
            ('HK.09988', '阿里巴巴', 80.0, 85.0, 500, 'ma_cross'),
            ('HK.09992', '泡泡玛特', 50.0, 55.0, 200, 'rsi_rebound'),
            ('HK.00700', '腾讯控股', 330.0, 350.0, 100, 'fusion_v2'),
            ('HK.03690', '美团', 120.0, 130.0, 300, 'fusion_v2'),
            ('HK.00005', '汇丰控股', 60.0, 58.0, 400, 'ma_cross'),
            ('HK.01810', '小米集团', 15.0, 17.0, 1000, 'rsi_rebound'),
            ('HK.00700', '腾讯控股', 350.0, 340.0, 100, 'fusion_v2'),
        ])
    ]
    
    # 保存测试数据
    for trade in test_trades_week + test_trades_month:
        db.save_trade(trade)
    
    logger.info(f"创建了 {len(test_trades_week)} 笔周测试数据和 {len(test_trades_month)} 笔月测试数据")


def test_weekly_report():
    """测试周报生成"""
    logger.info("=" * 60)
    logger.info("测试周报生成")
    logger.info("=" * 60)
    
    generator = WeeklyReportGenerator()
    report = generator.generate()
    
    logger.info(f"\n📊 周报测试结果:")
    logger.info(f"  周期：{report.week} ({report.start_date} ~ {report.end_date})")
    logger.info(f"  交易次数：{report.total_trades}")
    logger.info(f"  胜率：{report.win_rate:.1%}")
    logger.info(f"  盈亏比：{report.profit_loss_ratio:.2f}")
    logger.info(f"  总盈亏：HK${report.pnl:,.2f} ({report.pnl_pct:+.2f}%)")
    logger.info(f"  最大回撤：{report.max_drawdown:.2f}%")
    
    if report.strategies:
        logger.info(f"\n  策略表现:")
        for strategy, data in report.strategies.items():
            logger.info(f"    - {strategy}: {data.get('win_rate', 0):.1%} 胜率 ({data.get('count', 0)} 次)")
    
    if report.best_trade:
        logger.info(f"\n  最佳交易：{report.best_trade.get('code')} {report.best_trade.get('pnl_pct', 0):+.2f}%")
    
    if report.worst_trade:
        logger.info(f"  最差交易：{report.worst_trade.get('code')} {report.worst_trade.get('pnl_pct', 0):+.2f}%")
    
    logger.info(f"\n  下周展望：{report.outlook}")
    
    return report


def test_monthly_report():
    """测试月报生成"""
    logger.info("\n" + "=" * 60)
    logger.info("测试月报生成")
    logger.info("=" * 60)
    
    generator = MonthlyReportGenerator()
    report = generator.generate()
    
    logger.info(f"\n📊 月报测试结果:")
    logger.info(f"  月份：{report.month} ({report.start_date} ~ {report.end_date})")
    logger.info(f"  交易天数：{report.trading_days}")
    logger.info(f"  交易次数：{report.total_trades}")
    logger.info(f"  胜率：{report.win_rate:.1%}")
    logger.info(f"  盈亏比：{report.profit_loss_ratio:.2f}")
    logger.info(f"  总盈亏：HK${report.pnl:,.2f} ({report.pnl_pct:+.2f}%)")
    logger.info(f"  最大回撤：{report.max_drawdown:.2f}%")
    logger.info(f"  夏普比率：{report.sharpe_ratio:.2f}")
    logger.info(f"  索提诺比率：{report.sortino_ratio:.2f}")
    logger.info(f"  波动率：{report.volatility:.2f}%")
    
    if report.strategies:
        logger.info(f"\n  策略归因:")
        for strategy, data in report.strategies.items():
            contribution = data.get('contribution', 0) * 100
            logger.info(f"    - {strategy}: HK${data.get('pnl', 0):,.2f} ({contribution:+.1f}% 贡献)")
    
    logger.info(f"\n  下月计划：{report.plan}")
    
    return report


def main():
    """主函数"""
    logger.info("FTSeek 报告生成器端到端测试")
    logger.info("=" * 60)
    
    # 使用临时文件数据库 (避免内存数据库连接问题)
    import tempfile
    temp_db_path = tempfile.mktemp(suffix='.db')
    db = Database(temp_db_path)
    logger.info(f"使用临时数据库：{temp_db_path}")
    
    # 创建测试数据
    create_test_data(db)
    
    # 临时替换数据库实例
    import app.reporters.weekly_report as weekly_module
    import app.reporters.monthly_report as monthly_module
    
    original_weekly_db = weekly_module.get_database() if hasattr(weekly_module, 'get_database') else None
    original_monthly_db = monthly_module.get_database() if hasattr(monthly_module, 'get_database') else None
    
    # 运行测试 (生成器会自动使用数据库单例，数据已插入)
    try:
        # 重新创建生成器以使用新数据库
        import app.storage.database as db_module
        # 覆盖数据库单例
        db_module.db = db
        
        weekly_report = test_weekly_report()
        monthly_report = test_monthly_report()
        
        logger.info("\n" + "=" * 60)
        logger.info("✅ 所有测试通过!")
        logger.info("=" * 60)
        
        return 0
        
    except Exception as e:
        logger.exception(f"❌ 测试失败：{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
