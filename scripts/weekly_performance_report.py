#!/usr/bin/env python3
"""
FTSeek 周报复盘定时脚本

每周一 09:00 自动执行，生成并推送上周周报到钉钉群

使用方法:
    python3 scripts/weekly_performance_report.py --week last
    python3 scripts/weekly_performance_report.py --week last --push

定时任务 (crontab):
    0 9 * * 1 cd /opt/ftseek/FTSeek && export $(cat .env | grep -v '^#' | xargs) && python3 scripts/weekly_performance_report.py --push >> logs/weekly_report.log 2>&1

环境变量:
    DINGTALK_WEBHOOK_URL: 钉钉机器人 Webhook URL
    DINGTALK_SECRET: 钉钉机器人加签 Secret
"""
import sys
import logging
import argparse
from pathlib import Path
from datetime import datetime, timedelta

# 添加项目根目录到 Python 路径
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from app.performance.weekly_review import WeeklyReviewer, get_weekly_reviewer


# 配置日志
log_file = project_root / "logs" / "weekly_report.log"
log_file.parent.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger("ftseek.scripts.weekly_performance_report")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='FTSeek 周报复盘定时脚本')
    parser.add_argument(
        '--week',
        choices=['this', 'last'],
        default='last',
        help='生成本周或上周的报告，默认：last'
    )
    parser.add_argument(
        '--push',
        action='store_true',
        help='推送到钉钉'
    )
    parser.add_argument(
        '--db',
        type=str,
        default=None,
        help='数据库路径'
    )
    
    args = parser.parse_args()
    
    logger.info("=" * 50)
    logger.info("开始执行周报复盘")
    logger.info(f"报告周期：{args.week} week")
    logger.info(f"执行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 初始化复盘器
    db_path = args.db or '/opt/ftseek/FTSeek/data/ftseek.db'
    
    try:
        reviewer = get_weekly_reviewer(db_path=db_path)
    except Exception as e:
        logger.error(f"初始化复盘器失败：{e}")
        return 1
    
    # 确定时间范围
    week_start, week_end = reviewer.get_week_range(args.week)
    logger.info(f"报告时间范围：{week_start} 至 {week_end}")
    
    # 生成报告
    try:
        report = reviewer.generate_report(week_start, week_end)
        logger.info("报告生成成功")
    except Exception as e:
        logger.error(f"生成报告失败：{e}", exc_info=True)
        return 1
    
    # 格式化报告
    try:
        report_text = reviewer.format_report_text(report)
        logger.info("报告格式化成功")
    except Exception as e:
        logger.error(f"格式化报告失败：{e}", exc_info=True)
        return 1
    
    # 输出报告到日志
    logger.info("\n" + report_text)
    
    # 推送到钉钉
    if args.push:
        from app import config
        
        webhook_url = config.DINGTALK_WEBHOOK_URL
        
        if not webhook_url:
            logger.warning("⚠️  DINGTALK_WEBHOOK_URL 未配置，跳过推送")
            return 0
        
        try:
            success = reviewer.push_to_dingtalk(report, webhook_url)
            
            if success:
                logger.info("✅ 周报已推送到钉钉")
                return 0
            else:
                logger.error("❌ 钉钉推送失败")
                return 1
        except Exception as e:
            logger.error(f"钉钉推送异常：{e}", exc_info=True)
            return 1
    
    logger.info("周报生成完成（未推送）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
