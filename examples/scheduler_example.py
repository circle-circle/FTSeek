#!/usr/bin/env python3.8
"""
定时任务调度器使用示例

演示如何配置和使用 TradingScheduler:
- 初始化调度器
- 配置日报任务 (港股/美股)
- 配置周报任务
- 配置绩效检查
- 启动/停止调度器
"""
import logging
import time
from app.v4.scheduler import TradingScheduler, init_trading_scheduler
from app.v4.report.report_generator import init_report_generator
from app.v4.core.decision_engine import init_decision_engine

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("scheduler_example")


def main():
    """主函数"""
    logger.info("=== 定时任务调度器示例 ===")
    
    # 配置参数
    DB_PATH = "data/trading.db"
    DINGTALK_WEBHOOK = None  # 如果需要推送，配置 webhook URL
    
    # 1. 初始化报告生成器
    logger.info("初始化报告生成器...")
    report_generator = init_report_generator(
        db_path=DB_PATH,
        dingtalk_webhook=DINGTALK_WEBHOOK
    )
    
    # 2. 初始化决策引擎 (用于绩效检查)
    logger.info("初始化决策引擎...")
    decision_engine = init_decision_engine(
        db_path=DB_PATH,
        dingtalk_webhook=DINGTALK_WEBHOOK
    )
    
    # 3. 初始化调度器
    logger.info("初始化调度器...")
    scheduler = init_trading_scheduler(
        report_generator=report_generator,
        decision_engine=decision_engine,
        db_path=DB_PATH
    )
    
    # 4. 配置定时任务
    
    # 港股日报 - 0700.HK (腾讯)
    # 周一至周五 16:30 HKT (收盘后 30 分钟)
    logger.info("配置港股日报任务：0700.HK")
    scheduler.schedule_daily_report("0700.HK", "HK")
    
    # 港股日报 - 9988.HK (阿里)
    logger.info("配置港股日报任务：9988.HK")
    scheduler.schedule_daily_report("9988.HK", "HK")
    
    # 美股日报 - TSLA (特斯拉)
    # 周一至周五 04:30 EST (收盘后 30 分钟)
    logger.info("配置美股日报任务：TSLA")
    scheduler.schedule_daily_report("TSLA", "US")
    
    # 美股日报 - NVDA (英伟达)
    logger.info("配置美股日报任务：NVDA")
    scheduler.schedule_daily_report("NVDA", "US")
    
    # 周报 - 所有股票
    # 周日 20:00 Asia/Shanghai
    logger.info("配置周报任务：0700.HK")
    scheduler.schedule_weekly_report("0700.HK")
    
    # 绩效检查 - 每小时检查止盈止损条件
    logger.info("配置绩效检查任务 (每小时)")
    scheduler.schedule_performance_check()
    
    # 5. 启动调度器
    logger.info("启动调度器...")
    scheduler.start()
    
    logger.info("调度器已启动，任务列表:")
    for job in scheduler.scheduler.get_jobs():
        logger.info(f"  - {job.name}: {job.next_run_time}")
    
    # 6. 保持运行 (实际使用时应该作为服务运行)
    logger.info("调度器运行中... (按 Ctrl+C 停止)")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("收到停止信号...")
    
    # 7. 停止调度器
    logger.info("停止调度器...")
    scheduler.stop()
    logger.info("调度器已停止")


if __name__ == "__main__":
    main()
