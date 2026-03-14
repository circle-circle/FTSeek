#!/usr/bin/env python3
"""
FTSeek 周报推送脚本

用法:
    python3 scripts/weekly_report.py

定时任务 (Crontab):
    0 9 * * 1 cd /opt/ftseek/FTSeek && python3 scripts/weekly_report.py

推送时间：每周一 09:00
"""
import sys
import os
import logging
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("ftseek.weekly_report")

from app.reporters.weekly_report import generate_weekly_report
from app.notifier.dingtalk import DingTalkNotifier
from app import config


def main():
    """主函数"""
    logger.info("开始生成周报...")
    
    try:
        # 生成周报
        report = generate_weekly_report()
        
        if report.total_trades == 0:
            logger.info("上周无交易记录")
        else:
            logger.info(f"周报生成成功：{report.total_trades} 笔交易，胜率 {report.win_rate:.1%}")
        
        # 发送到钉钉
        notifier = DingTalkNotifier(
            webhook_url=config.DINGTALK_WEBHOOK_URL,
            secret=config.DINGTALK_SECRET
        )
        
        # 转换报告格式
        report_data = {
            'week': report.week,
            'total_trades': report.total_trades,
            'wins': report.wins,
            'win_rate': report.win_rate,
            'profit_loss_ratio': report.profit_loss_ratio if report.profit_loss_ratio != float('inf') else 99.99,
            'pnl': report.pnl,
            'pnl_pct': report.pnl_pct,
            'max_drawdown': report.max_drawdown,
            'strategies': report.strategies,
            'best_trade': report.best_trade,
            'worst_trade': report.worst_trade,
            'outlook': report.outlook,
        }
        
        success = notifier.send_weekly_report(report_data)
        
        if success:
            logger.info("周报推送成功")
            return 0
        else:
            logger.error("周报推送失败")
            return 1
            
    except Exception as e:
        logger.exception(f"周报生成失败：{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
