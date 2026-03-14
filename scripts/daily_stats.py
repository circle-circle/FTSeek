#!/usr/bin/env python3
"""
FTSeek 收盘统计定时脚本

每日 16:30 自动推送收盘统计到钉钉群

使用方法:
    python3 scripts/daily_stats.py

定时任务 (crontab):
    30 16 * * 1-5 cd /opt/ftseek/FTSeek && python3 scripts/daily_stats.py >> logs/daily_stats.log 2>&1

环境变量:
    DINGTALK_WEBHOOK_URL: 钉钉机器人 Webhook URL
    DINGTALK_SECRET: 钉钉机器人加签 Secret
    API_TOKEN: API 鉴权 Token
"""
import sys
import logging
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from app.reporters.daily_stats import send_daily_stats, DailyStatsReporter
from app import config

# 配置日志
log_file = project_root / "logs" / "daily_stats.log"
log_file.parent.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger("ftseek.scripts.daily_stats")


def main():
    """主函数"""
    logger.info("=" * 50)
    logger.info("开始执行收盘统计推送")
    
    # 检查配置
    if not config.DINGTALK_WEBHOOK_URL:
        logger.warning("⚠️  DINGTALK_WEBHOOK_URL 未配置，跳过推送")
        logger.info("生成收盘统计预览:")
        try:
            reporter = DailyStatsReporter()
            stats = reporter.generate_report()
            print("\n" + reporter.format_message(stats) + "\n")
        except Exception as e:
            logger.error(f"生成统计失败：{e}")
        return 0
    
    try:
        # 发送收盘统计
        success = send_daily_stats()
        
        if success:
            logger.info("收盘统计推送完成 ✓")
            return 0
        else:
            logger.error("收盘统计推送失败 ✗")
            return 1
            
    except Exception as e:
        logger.error(f"执行异常：{e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
