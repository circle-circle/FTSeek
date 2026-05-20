#!/usr/bin/env python3
"""
自动化监控定时任务设置

功能：
1. 设置每日收盘后自动运行交易监控（16:35）
2. 设置每周一自动运行综合分析报告（周一早上9:00）
3. 提供手动运行命令

使用方法：
    python -m app.analytics.setup_monitor           # 设置定时任务
    python -m app.analytics.setup_monitor --remove  # 移除定时任务
    python -m app.analytics.setup_monitor --status  # 查看状态
"""

import argparse
import subprocess
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ftseek.analytics.setup_monitor")

PROJECT_ROOT = Path("/opt/ftseek/FTSeek")
CRON_TAG = "# FTSeek Auto Monitor"

def get_cron_jobs():
    """获取当前cron任务"""
    try:
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            text=True,
            check=False
        )
        return result.stdout
    except Exception as e:
        logger.error(f"获取cron任务失败: {e}")
        return ""

def setup_cron():
    """设置定时任务"""
    try:
        # 获取当前crontab
        current = get_cron_jobs()

        # 检查是否已存在
        if "FTSeek Auto Monitor" in current:
            logger.info("定时任务已存在")
            return

        # 添加新的cron任务
        new_jobs = f"""
# FTSeek Auto Monitor - 每日交易监控 (收盘后5分钟)
35 16 * * 1-5 cd {PROJECT_ROOT} && python -m app.analytics.trade_monitor --today >> {PROJECT_ROOT}/logs/daily_monitor.log 2>&1
# FTSeek Auto Monitor - 每周综合分析 (周一早上)
0 9 * * 1 cd {PROJECT_ROOT} && python -m app.analytics.trade_monitor --week >> {PROJECT_ROOT}/logs/weekly_report.log 2>&1
# FTSeek Auto Monitor - 每周交易分析
0 9 * * 1 cd {PROJECT_ROOT} && python -m app.analytics.trade_analyzer --days 7 >> {PROJECT_ROOT}/logs/weekly_analysis.log 2>&1
"""

        new_crontab = current + new_jobs

        # 写入crontab
        subprocess.run(
            ["crontab", "-"],
            input=new_crontab,
            text=True,
            check=True
        )

        logger.info("✅ 定时任务已设置")
        logger.info("  📅 每日 16:35 - 交易质量监控")
        logger.info("  📅 每周一 9:00 - 综合分析报告")
        logger.info("  📅 每周一 9:00 - 交易数据分析")

    except Exception as e:
        logger.error(f"设置定时任务失败: {e}")
        logger.info("请手动运行以下命令:")
        print(f"""
# 编辑 crontab
crontab -e

# 添加以下行:
# FTSeek Auto Monitor - 每日交易监控 (收盘后5分钟)
35 16 * * 1-5 cd {PROJECT_ROOT} && python -m app.analytics.trade_monitor --today >> {PROJECT_ROOT}/logs/daily_monitor.log 2>&1
# FTSeek Auto Monitor - 每周综合分析 (周一早上)
0 9 * * 1 cd {PROJECT_ROOT} && python -m app.analytics.trade_monitor --week >> {PROJECT_ROOT}/logs/weekly_report.log 2>&1
# FTSeek Auto Monitor - 每周交易分析
0 9 * * 1 cd {PROJECT_ROOT} && python -m app.analytics.trade_analyzer --days 7 >> {PROJECT_ROOT}/logs/weekly_analysis.log 2>&1
        """)

def remove_cron():
    """移除定时任务"""
    try:
        current = get_cron_jobs()

        # 移除FTSeek相关任务
        lines = current.split('\n')
        filtered = []
        skip = False

        for line in lines:
            if "FTSeek Auto Monitor" in line:
                skip = True
                continue
            if skip and line.startswith('#'):
                skip = False
            if not skip:
                filtered.append(line)

        new_crontab = '\n'.join(filtered)

        subprocess.run(
            ["crontab", "-"],
            input=new_crontab,
            text=True,
            check=True
        )

        logger.info("✅ 定时任务已移除")

    except Exception as e:
        logger.error(f"移除定时任务失败: {e}")

def show_status():
    """查看定时任务状态"""
    current = get_cron_jobs()

    if "FTSeek Auto Monitor" in current:
        logger.info("✅ 定时任务已启用")
        for line in current.split('\n'):
            if "FTSeek Auto Monitor" in line or (line.strip() and "trade_monitor" in line):
                logger.info(f"  {line.strip()}")
    else:
        logger.info("❌ 定时任务未设置")
        logger.info("运行 `python -m app.analytics.setup_monitor` 设置")

def main():
    parser = argparse.ArgumentParser(description="自动化监控定时任务设置")
    parser.add_argument("--remove", action="store_true", help="移除定时任务")
    parser.add_argument("--status", action="store_true", help="查看状态")
    args = parser.parse_args()

    if args.remove:
        remove_cron()
    elif args.status:
        show_status()
    else:
        setup_cron()
        show_status()

if __name__ == "__main__":
    main()
