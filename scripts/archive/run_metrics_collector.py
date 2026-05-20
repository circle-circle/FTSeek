#!/usr/bin/env python3
"""
监控指标收集器守护进程

每 5 分钟收集一次系统指标并持久化到数据库
支持钉钉告警集成
"""
import sys
import os
import time
import logging
from datetime import datetime
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.observability.metrics_collector import MetricsCollector
from app.observability.dingtalk_alert import DingTalkAlerter
from app.storage.metrics_tables import MetricsTables

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/opt/ftseek/FTSeek-US/logs/metrics_collector.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("metrics_collector_daemon")

# 配置
DB_PATH = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"
COLLECTION_INTERVAL = 300  # 5 分钟
DINGTALK_WEBHOOK = os.getenv("DINGTALK_WEBHOOK", "")
DINGTALK_SECRET = os.getenv("DINGTALK_SECRET", "")


def load_dingtalk_config():
    """从配置文件加载钉钉配置"""
    config_path = Path(__file__).parent.parent / "config" / "alert_config.json"
    if config_path.exists():
        import json
        try:
            with open(config_path) as f:
                config = json.load(f)
                return config.get("dingtalk", {})
        except Exception as e:
            logger.error(f"加载告警配置失败：{e}")
    return {}


def main():
    """主循环"""
    logger.info("=" * 50)
    logger.info("监控指标收集器启动")
    logger.info(f"数据库路径：{DB_PATH}")
    logger.info(f"收集间隔：{COLLECTION_INTERVAL}秒")
    
    # 初始化
    collector = MetricsCollector(db_path=DB_PATH)
    
    # 加载钉钉配置
    dingtalk_config = load_dingtalk_config()
    dingtalk_alerter = None
    if dingtalk_config.get("webhook"):
        dingtalk_alerter = DingTalkAlerter(
            dingtalk_config["webhook"],
            dingtalk_config.get("secret", "")
        )
        logger.info("钉钉告警已配置")
    else:
        logger.warning("钉钉告警未配置，请在 config/alert_config.json 中配置")
    
    # 发送启动通知
    if dingtalk_alerter:
        dingtalk_alerter.send_alert(
            level="INFO",
            title="🚀 监控服务启动",
            content="监控指标收集器已启动，开始收集系统指标"
        )
    
    iteration = 0
    while True:
        try:
            iteration += 1
            logger.info(f"\n{'='*50}")
            logger.info(f"第 {iteration} 次指标收集")
            logger.info(f"时间：{datetime.now().isoformat()}")
            
            # 收集系统指标
            system_metrics = collector.collect_system_metrics()
            logger.info(f"CPU: {system_metrics['cpu_percent']:.1f}%")
            logger.info(f"内存：{system_metrics['memory_percent']:.1f}%")
            logger.info(f"磁盘：{system_metrics['disk_percent']:.1f}%")
            
            # 持久化指标
            collector.persist_all_metrics()
            logger.info("指标已持久化到数据库")
            
            # 检查小时聚合
            if collector.should_aggregate_hourly():
                collector.aggregate_hourly_metrics()
            
            # 检查告警
            if dingtalk_alerter:
                cpu = system_metrics['cpu_percent']
                memory = system_metrics['memory_percent']
                
                if cpu > 90 or memory > 90:
                    dingtalk_alerter.send_alert(
                        level="CRITICAL",
                        title="⚠️ 系统资源告警",
                        content=f"CPU: {cpu:.1f}%, 内存：{memory:.1f}%"
                    )
                    logger.warning("已发送 CRITICAL 告警")
                elif cpu > 70 or memory > 70:
                    dingtalk_alerter.send_alert(
                        level="WARN",
                        title="⚡ 系统资源警告",
                        content=f"CPU: {cpu:.1f}%, 内存：{memory:.1f}%"
                    )
                    logger.warning("已发送 WARN 告警")
            
            # 清理旧数据 (每 24 小时)
            if iteration % (24 * 60 * 60 // COLLECTION_INTERVAL) == 0:
                collector.cleanup_old_metrics(days=30)
                logger.info("已清理 30 天前的旧指标")
            
            logger.info(f"等待 {COLLECTION_INTERVAL}秒后进行下一次收集...")
            time.sleep(COLLECTION_INTERVAL)
            
        except KeyboardInterrupt:
            logger.info("收到中断信号，正在退出...")
            if dingtalk_alerter:
                dingtalk_alerter.send_alert(
                    level="INFO",
                    title="🛑 监控服务停止",
                    content="监控指标收集器已停止"
                )
            break
        except Exception as e:
            logger.error(f"收集指标失败：{e}", exc_info=True)
            if dingtalk_alerter:
                dingtalk_alerter.send_alert(
                    level="CRITICAL",
                    title="❌ 监控服务异常",
                    content=f"指标收集失败：{str(e)}"
                )
            # 等待 60 秒后重试
            time.sleep(60)


if __name__ == "__main__":
    main()
