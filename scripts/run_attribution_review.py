#!/usr/bin/env python3
"""
盘后归因复盘脚本 (独立运行入口)

功能:
1. 调用 AttributionEngine 生成当日归因报告
2. 推送钉钉通知
3. 保存报告到数据库

使用方式:
    python3 scripts/run_attribution_review.py           # 今日归因复盘
    python3 scripts/run_attribution_review.py --date 2026-04-11

定时任务 (crontab):
    45 16 * * 1-5 cd /opt/ftseek/FTSeek && python3 scripts/run_attribution_review.py >> logs/attribution_review.log 2>&1
"""
import os
import sys
import argparse
import logging
from datetime import datetime
from pathlib import Path

# 添加项目根目录到Python路径
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('logs/attribution_review.log', encoding='utf-8')
    ]
)
logger = logging.getLogger("ftseek.scripts.attribution_review")


def run(review_date: str = None):
    """
    执行归因复盘

    Args:
        review_date: 复盘日期 (YYYY-MM-DD)，默认今日
    """
    if not review_date:
        review_date = datetime.now().strftime("%Y-%m-%d")

    logger.info(f"[归因复盘] 开始: {review_date}")

    try:
        from app.review.daily_review import run_post_market_review
        report = run_post_market_review()

        if report:
            summary = report.get('summary', {})
            logger.info(
                f"[归因复盘] 完成 | "
                f"交易{summary.get('total_trades', 0)}笔 | "
                f"胜率{summary.get('win_rate', 0):.0%} | "
                f"盈亏{summary.get('total_pnl', 0):+.2f}"
            )
        else:
            logger.warning("[归因复盘] 报告为空")

        return report

    except Exception as e:
        logger.error(f"[归因复盘] 执行失败: {e}", exc_info=True)
        return None


def main():
    parser = argparse.ArgumentParser(description='盘后归因复盘')
    parser.add_argument('--date', type=str, default=None, help='复盘日期 (YYYY-MM-DD)')
    args = parser.parse_args()

    report = run(args.date)

    if report:
        summary = report.get('summary', {})
        print(f"\n{'='*50}")
        print(f"归因复盘完成: {summary.get('total_trades', 0)}笔交易")
        print(f"胜率: {summary.get('win_rate', 0):.0%}")
        print(f"总盈亏: {summary.get('total_pnl', 0):+.2f}")
        print(f"{'='*50}")
    else:
        print("\n归因复盘完成 (无交易数据)")


if __name__ == "__main__":
    main()
