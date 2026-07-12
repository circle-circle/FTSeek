#!/usr/bin/env python3.11
"""
美股监控器 - Cron 任务入口

用途:
- 通过 cron 定期执行 (每 5-15 分钟)
- 检查美股持仓的止损止盈条件
- 触发时自动平仓 + 钉钉推送

配置 cron 示例 (crontab -e):
# 每 5 分钟检查一次 (美东交易时段 21:30-04:00)
*/5 21-23 * * * cd /opt/ftseek/FTSeek && /usr/local/bin/python3.11 scripts/us_stock_monitor_cron.py >> logs/us_monitor.log 2>&1
*/5 0-3 * * * cd /opt/ftseek/FTSeek && /usr/local/bin/python3.11 scripts/us_stock_monitor_cron.py >> logs/us_monitor.log 2>&1

# 每 15 分钟检查一次 (美东非交易时段)
*/15 4-20 * * * cd /opt/ftseek/FTSeek && /usr/local/bin/python3.11 scripts/us_stock_monitor_cron.py >> logs/us_monitor.log 2>&1
"""
import os
import sys
import logging
from datetime import datetime

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/opt/ftseek/FTSeek/logs/us_monitor.log', encoding='utf-8')
    ]
)
logger = logging.getLogger("ftseek.us_cron")

# 添加项目路径
sys.path.insert(0, '/opt/ftseek/FTSeek')

def main():
    """主函数"""
    logger.info("=" * 60)
    logger.info("美股监控检查开始")
    logger.info("=" * 60)
    
    try:
        # 导入监控器
        from app.data.us_stock_monitor import get_us_stock_monitor
        
        # 加载配置
        import yaml
        config_path = '/opt/ftseek/FTSeek/config/us_stock_config.yaml'
        
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            logger.info(f"配置已加载：{config_path}")
        else:
            config = {}
            logger.warning("配置文件不存在，使用默认配置")
        
        # 获取监控器实例
        monitor_config = config.get('monitor', {})
        monitor = get_us_stock_monitor(monitor_config)
        
        # 连接富途
        if not monitor._connect_quote_ctx():
            logger.error("无法连接富途 API，跳过本次检查")
            return 1
        
        # 检查所有持仓
        monitor.check_all_positions()
        
        # 输出状态
        status = monitor.get_status()
        logger.info(f"检查完成 | 累计检查={status['check_count']} 触发={status['trigger_count']}")
        
        logger.info("=" * 60)
        return 0
    
    except Exception as e:
        logger.error(f"执行异常：{e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
