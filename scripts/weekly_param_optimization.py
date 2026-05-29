#!/usr/bin/env python3
"""
weekly_param_optimization.py — cron 入口

调用方式:
  python scripts/weekly_param_optimization.py [--no-notify] [--compare-only]

cron 推荐 (每周六 02:00 ET, 完整盘后数据已沉淀):
  0 2 * * 6  cd /opt/ftseek/FTSeek-US && /usr/bin/python scripts/weekly_param_optimization.py >> logs/weekly_opt.log 2>&1

设计:
- 优化结果只产出建议报告, 不修改实盘配置 (人工审核后才能 apply)
- 落地路径: data/optimization_reports/
- 失败不致命, 仅记录日志 + 通知
"""
import argparse
import logging
import os
import sys
from pathlib import Path

# 确保从项目根运行也能 import
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logger = logging.getLogger("weekly_param_optimization")


def _get_notifier(disable: bool):
    if disable:
        return None
    try:
        from app.notifier.feishu import get_notifier
        return get_notifier()
    except Exception as e:
        logger.warning(f"notifier 加载失败: {e}")
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="周频参数优化 + 实盘对比")
    parser.add_argument("--no-notify", action="store_true", help="禁用飞书通知")
    parser.add_argument("--compare-only", action="store_true",
                        help="仅运行 live-vs-backtest 对比, 不做参数优化")
    parser.add_argument("--days", type=int, default=30,
                        help="对比窗口天数 (仅对 --compare-only 生效)")
    args = parser.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    notifier = _get_notifier(args.no_notify)

    if args.compare_only:
        from app.backtest.live_backtest_comparison import run_comparison
        run_comparison(days=args.days, notifier=notifier)
        return 0

    from app.backtest.weekly_optimizer import run_weekly_optimization
    from app.backtest.live_backtest_comparison import run_comparison

    try:
        run_weekly_optimization(notifier=notifier)
    except Exception as e:
        logger.error(f"weekly_optimization 失败: {e}", exc_info=True)
        # 不阻断 compare
    try:
        run_comparison(days=args.days, notifier=notifier)
    except Exception as e:
        logger.error(f"live_vs_backtest 失败: {e}", exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
