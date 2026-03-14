#!/usr/bin/env python3
"""
FTSeek 收盘统计功能测试脚本

测试场景:
1. 无数据时不报错
2. 统计数据格式正确
3. 推送消息格式正确
"""
import sys
import logging
from pathlib import Path
from datetime import datetime, timedelta

# 添加项目根目录到 Python 路径
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from app.reporters.daily_stats import DailyStatsReporter, DailyStats
from app.storage.database import get_database
from app import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ftseek.test.daily_stats")


def test_empty_data():
    """测试无数据时不报错"""
    logger.info("=" * 50)
    logger.info("测试 1: 无数据时不报错")
    
    try:
        reporter = DailyStatsReporter()
        stats = reporter.generate_report()
        message = reporter.format_message(stats)
        
        assert stats.total_signals == 0
        assert stats.executed_signals == 0
        assert stats.today_pnl == 0.0
        assert "📊 FTSeek 收盘统计" in message
        
        logger.info("✓ 测试通过：无数据时正常生成报告")
        return True
    except Exception as e:
        logger.error(f"✗ 测试失败：{e}")
        return False


def test_message_format():
    """测试消息格式"""
    logger.info("=" * 50)
    logger.info("测试 2: 消息格式正确")
    
    try:
        reporter = DailyStatsReporter()
        stats = reporter.generate_report()
        message = reporter.format_message(stats)
        
        # 检查必需字段
        required_fields = [
            "📊 FTSeek 收盘统计",
            "今日信号",
            "已执行",
            "忽略/延期",
            "今日盈亏",
            "本周盈亏",
            "当前持仓",
            "风控状态",
            "连续亏损",
            "总回撤",
            "安全线"
        ]
        
        for field in required_fields:
            assert field in message, f"缺少字段：{field}"
        
        logger.info("✓ 测试通过：消息格式正确")
        logger.info("\n" + message)
        return True
    except Exception as e:
        logger.error(f"✗ 测试失败：{e}")
        return False


def test_stats_dataclass():
    """测试 DailyStats 数据类"""
    logger.info("=" * 50)
    logger.info("测试 3: DailyStats 数据类")
    
    try:
        stats = DailyStats(
            date="2026-03-01",
            total_signals=8,
            executed_signals=5,
            ignored_signals=3,
            today_pnl=12450.0,
            today_pnl_percent=1.23,
            weekly_pnl=45600.0,
            weekly_pnl_percent=4.56,
            positions=[
                {'code': 'HK.00700', 'name': '腾讯控股', 'unrealized_pnl_percent': 2.3},
                {'code': 'HK.09988', 'name': '阿里巴巴', 'unrealized_pnl_percent': 1.8},
                {'code': 'HK.09992', 'name': '泡泡玛特', 'unrealized_pnl_percent': -0.5},
            ],
            consecutive_losses=0,
            current_drawdown=0.021,
            max_drawdown_limit=0.15,
            risk_status='normal'
        )
        
        reporter = DailyStatsReporter()
        message = reporter.format_message(stats)
        
        # 检查格式
        assert "今日信号：8 次" in message
        assert "已执行：5 次" in message
        assert "忽略/延期：3 次" in message
        assert "+HK$12,450" in message
        assert "+HK$45,600" in message
        assert "HK.00700 腾讯控股" in message
        assert "风控状态：✅ 正常" in message
        
        logger.info("✓ 测试通过：DailyStats 数据类正确")
        logger.info("\n" + message)
        return True
    except Exception as e:
        logger.error(f"✗ 测试失败：{e}")
        return False


def main():
    """运行所有测试"""
    logger.info("FTSeek 收盘统计功能测试")
    logger.info("=" * 50)
    
    results = []
    results.append(("无数据测试", test_empty_data()))
    results.append(("格式测试", test_message_format()))
    results.append(("数据类测试", test_stats_dataclass()))
    
    logger.info("=" * 50)
    logger.info("测试结果汇总:")
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✓ 通过" if result else "✗ 失败"
        logger.info(f"  {name}: {status}")
    
    logger.info(f"\n总计：{passed}/{total} 测试通过")
    
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
