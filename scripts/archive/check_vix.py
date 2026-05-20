#!/usr/bin/env python3
"""
VIX 指数监控脚本 - FTSeek-US

独立脚本，用于 crontab 定时执行 VIX 监控
获取 VIX 指数并推送警报

使用方法:
  cd /opt/ftseek/FTSeek-US && python3 scripts/check_vix.py
"""
import logging
import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.risk.vix_monitor import VIXMonitor, send_vix_alert
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("ftseek_us.check_vix")


def main():
    """主函数"""
    logger.info("开始执行 VIX 监控")
    
    # 创建 VIX 监控器
    monitor = VIXMonitor()
    
    # 检查 VIX 级别
    result = monitor.check_vix_level()
    
    logger.info(f"VIX 检查结果：{result}")
    
    # 获取飞书 Webhook
    feishu_webhook = os.getenv("FEISHU_WEBHOOK_URL_US", "")
    
    # 发送警报 (仅当 VIX 异常时)
    send_vix_alert(feishu_webhook, result)
    
    logger.info("VIX 监控完成")


if __name__ == "__main__":
    main()
