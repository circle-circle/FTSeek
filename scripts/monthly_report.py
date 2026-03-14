#!/usr/bin/env python3
"""
FTSeek 月报推送脚本

用法:
    python3 scripts/monthly_report.py

定时任务 (Crontab):
    0 9 1 * * cd /opt/ftseek/FTSeek && python3 scripts/monthly_report.py

推送时间：每月 1 日 09:00
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
logger = logging.getLogger("ftseek.monthly_report")

from app.reporters.monthly_report import generate_monthly_report
from app.notifier.dingtalk import DingTalkNotifier
from app import config


def main():
    """主函数"""
    logger.info("开始生成月报...")
    
    try:
        # 生成月报
        report = generate_monthly_report()
        
        if report.total_trades == 0:
            logger.info("上月无交易记录")
        else:
            logger.info(f"月报生成成功：{report.total_trades} 笔交易，收益率 {report.pnl_pct:.2f}%")
        
        # 发送到钉钉
        notifier = DingTalkNotifier(
            webhook_url=config.DINGTALK_WEBHOOK_URL,
            secret=config.DINGTALK_SECRET
        )
        
        # 转换报告格式
        report_data = {
            'month': report.month,
            'total_trades': report.total_trades,
            'wins': report.wins,
            'win_rate': report.win_rate,
            'profit_loss_ratio': report.profit_loss_ratio if report.profit_loss_ratio != float('inf') else 99.99,
            'pnl': report.pnl,
            'pnl_pct': report.pnl_pct,
            'max_drawdown': report.max_drawdown,
            'sharpe_ratio': report.sharpe_ratio,
            'sortino_ratio': report.sortino_ratio,
            'strategies': report.strategies,
            'plan': report.plan,
        }
        
        success = notifier.send_monthly_report(report_data)
        
        if success:
            logger.info("月报推送成功")
            return 0
        else:
            logger.error("月报推送失败")
            return 1
            
    except Exception as e:
        logger.exception(f"月报生成失败：{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
