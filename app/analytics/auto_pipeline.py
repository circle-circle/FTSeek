#!/usr/bin/env python3
"""
自动化分析Pipeline - 每日定时运行

功能：
1. 每日收盘后自动运行信号分析器
2. 生成分析报告并保存
3. 检测异常并发送通知
4. 更新策略参数（如果启用了自动调整）

使用方法:
    python -m app.analytics.auto_pipeline --once    # 立即运行一次
    python -m app.analytics.auto_pipeline            # 启动定时任务（每日16:30运行）

配置:
    config/unified_config.yaml -> analytics.auto_run
"""

import os
import sys
import argparse
import logging
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger("ftseek.analytics.auto_pipeline")

# 路径配置
PROJECT_ROOT = Path("/opt/ftseek/FTSeek")
REPORT_DIR = PROJECT_ROOT / "data/analysis_reports"
DB_PATH = PROJECT_ROOT / "data/ftseek.db"


def run_signal_analyzer(days: int = 7) -> Optional[str]:
    """
    运行信号分析器

    Args:
        days: 分析最近多少天的数据

    Returns:
        报告文件路径，失败返回 None
    """
    try:
        logger.info(f"开始运行信号分析器 (最近 {days} 天)...")

        # 使用 subprocess 运行分析器
        result = subprocess.run(
            [sys.executable, "-m", "app.analytics.signal_analyzer", "--days", str(days)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=300  # 5分钟超时
        )

        if result.returncode != 0:
            logger.error(f"分析器运行失败: {result.stderr}")
            return None

        # 从输出中提取报告路径
        report_path = None
        for line in result.stdout.split('\n'):
            if "分析报告已保存到:" in line:
                report_path = line.split("分析报告已保存到:")[-1].strip()
                break

        if report_path:
            logger.info(f"分析完成，报告: {report_path}")
        else:
            logger.info("分析完成")

        return report_path

    except subprocess.TimeoutExpired:
        logger.error("分析器运行超时")
        return None
    except Exception as e:
        logger.error(f"分析器运行异常: {e}")
        return None


def run_factor_decay_monitor(days: int = 14) -> bool:
    """
    运行因子衰减监控

    Args:
        days: 监控周期

    Returns:
        是否成功
    """
    try:
        logger.info(f"开始运行因子衰减监控 (最近 {days} 天)...")

        result = subprocess.run(
            [sys.executable, "-m", "app.analytics.factor_decay_monitor", "--days", str(days)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=300
        )

        if result.returncode != 0:
            logger.error(f"因子衰减监控运行失败: {result.stderr}")
            return False

        logger.info("因子衰减监控完成")
        return True

    except Exception as e:
        logger.error(f"因子衰减监控异常: {e}")
        return False


def run_auto_adjustment(days: int = 7) -> bool:
    """
    运行自动策略调整

    Args:
        days: 基于最近多少天的数据调整

    Returns:
        是否成功
    """
    try:
        logger.info(f"开始运行自动策略调整 (基于最近 {days} 天数据)...")

        result = subprocess.run(
            [sys.executable, "-m", "app.analytics.auto_adjuster", "--days", str(days), "--dry-run"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=300
        )

        if result.returncode != 0:
            logger.error(f"自动调整运行失败: {result.stderr}")
            return False

        logger.info("自动策略调整完成")
        return True

    except Exception as e:
        logger.error(f"自动调整异常: {e}")
        return False


def daily_pipeline(days: int = 7):
    """
    每日分析Pipeline

    执行顺序：
    1. 信号分析器
    2. 因子衰减监控
    3. 自动策略调整（预览模式）
    """
    logger.info("=" * 60)
    logger.info(f"开始每日分析Pipeline | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    # 1. 运行信号分析器
    report_path = run_signal_analyzer(days=days)
    if not report_path:
        logger.warning("信号分析器未生成报告，继续执行后续步骤")

    # 2. 运行因子衰减监控
    run_factor_decay_monitor(days=days * 2)  # 使用更长的周期

    # 3. 运行自动策略调整（预览模式）
    run_auto_adjustment(days=days)

    logger.info("每日分析Pipeline完成")

    # 4. 发送通知（如果配置了）
    send_notification(report_path)


def send_notification(report_path: Optional[str] = None):
    """发送分析完成通知"""
    try:
        # 简单记录到日志
        logger.info(f"分析Pipeline完成，报告: {report_path or 'N/A'}")

        # 这里可以扩展为发送钉钉/飞书通知
        # 例如调用 app.notification 模块

    except Exception as e:
        logger.debug(f"通知发送失败: {e}")


def schedule_daily():
    """
    设置每日定时任务

    使用系统 cron 或 APScheduler
    """
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger

        scheduler = BlockingScheduler()

        # 每日 16:30 运行（港股收盘后）
        scheduler.add_job(
            daily_pipeline,
            trigger=CronTrigger(hour=16, minute=30),
            id="daily_analytics_pipeline",
            replace_existing=True
        )

        logger.info("定时任务已设置: 每日 16:30 运行分析Pipeline")
        scheduler.start()

    except ImportError:
        logger.warning("未安装 APScheduler，使用系统 cron 替代")
        setup_cron_job()


def setup_cron_job():
    """设置系统 cron 任务"""
    import subprocess

    cron_line = "30 16 * * * cd /opt/ftseek/FTSeek && python -m app.analytics.auto_pipeline --once >> /opt/ftseek/FTSeek/logs/auto_pipeline.log 2>&1"

    try:
        # 检查是否已存在
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            text=True
        )

        if "auto_pipeline" in result.stdout:
            logger.info("cron任务已存在")
            return

        # 添加 cron 任务
        new_crontab = result.stdout.strip() + f"\n{cron_line}\n"
        subprocess.run(
            ["crontab", "-"],
            input=new_crontab,
            text=True,
            check=True
        )
        logger.info("cron任务已添加")

    except Exception as e:
        logger.error(f"设置cron任务失败: {e}")


def main():
    parser = argparse.ArgumentParser(description="自动化分析Pipeline")
    parser.add_argument("--once", action="store_true", help="立即运行一次")
    parser.add_argument("--days", type=int, default=7, help="分析天数")
    parser.add_argument("--schedule", action="store_true", help="启动定时任务")
    args = parser.parse_args()

    if args.once:
        daily_pipeline(days=args.days)
    elif args.schedule:
        schedule_daily()
    else:
        print("用法:")
        print("  python -m app.analytics.auto_pipeline --once     # 立即运行")
        print("  python -m app.analytics.auto_pipeline --schedule   # 启动定时任务")
        print("  python -m app.analytics.auto_pipeline --days 14    # 分析14天数据")


if __name__ == "__main__":
    main()
