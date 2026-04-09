#!/usr/bin/env python3
"""
模拟盘数据同步定时任务

功能:
1. 定时从富途 OpenD 同步持仓和交易数据
2. 数据校验和差异告警
3. 同步日志记录

使用方式:
- 手动执行：python scripts/simulation_sync_cron.py
- 定时任务：添加到 crontab
  */30 9-16 * * 1-5 cd /opt/ftseek/FTSeek && python scripts/simulation_sync_cron.py >> logs/simulation_sync.log 2>&1
"""
import sys
import logging
from datetime import datetime
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.simulation.sync_service import run_sync, init_simulation_account
from app.ftlogging import setup_logging

# 配置日志
setup_logging(
    log_file='logs/simulation_sync.log',
    level=logging.INFO
)
logger = logging.getLogger("ftseek.simulation.cron")


def is_trading_time():
    """检查是否为交易时间"""
    from datetime import time
    now = datetime.now()
    current_time = now.time()
    weekday = now.weekday()
    
    # 周末不交易
    if weekday >= 5:
        return False
    
    # 交易时间：09:30-12:00, 13:00-16:00
    if time(9, 30) <= current_time <= time(12, 0):
        return True
    if time(13, 0) <= current_time <= time(16, 0):
        return True
    
    return False


def main():
    """主函数"""
    logger.info("=" * 60)
    logger.info("模拟盘数据同步任务启动")
    logger.info(f"时间：{datetime.now().isoformat()}")
    logger.info("=" * 60)
    
    # 初始化模拟账户
    logger.info("检查模拟账户...")
    if not init_simulation_account():
        logger.error("模拟账户初始化失败")
        return 1
    
    # 检查是否为交易时间
    if not is_trading_time():
        logger.info("当前非交易时间，跳过同步")
        return 0
    
    # 执行同步
    logger.info("开始同步数据...")
    result = run_sync()
    
    # 输出结果
    if result.success:
        logger.info(f"同步成功：持仓 {result.positions_synced} 只，交易 {result.trades_synced} 笔")
        if result.discrepancies:
            logger.warning(f"发现 {len(result.discrepancies)} 处差异")
        return 0
    else:
        logger.error(f"同步失败：{result.error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
