#!/usr/bin/env python3
"""
FTSeek 风险监控推送脚本

功能：
- 每 2 小时推送风险监控面板 (10:00/12:00/14:00/16:00)
- 监控账户总值/可用现金/持仓市值
- 监控今日/本周/总回撤
- 监控持仓数量/今日交易次数/连续亏损次数
- 监控风控状态

使用方式：
    python3 scripts/risk_monitor.py          # 检查并推送 (如果到时间)
    python3 scripts/risk_monitor.py --force  # 强制推送 (忽略时间)
    python3 scripts/risk_monitor.py --test   # 测试模式

配置：
- 环境变量：DINGTALK_WEBHOOK_URL, DINGTALK_SECRET
- 配置文件：config/risk_config.yaml

定时任务 (crontab):
    0 10,12,14,16 * * 1-5 cd /opt/ftseek/FTSeek && python3 scripts/risk_monitor.py
"""
import os
import sys
import logging
import argparse
from datetime import datetime
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.risk.risk_manager import get_risk_manager, RiskManager
from app.risk.risk_monitor import get_risk_monitor, RiskMonitor
from app.storage.database import get_database, Database
from app.notifier.dingtalk import DingTalkNotifier
from app import config

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/opt/ftseek/FTSeek/logs/risk_monitor.log', encoding='utf-8')
    ]
)
logger = logging.getLogger("ftseek.scripts.risk_monitor")


def init_components():
    """初始化所有组件"""
    logger.info("初始化组件...")
    
    # 1. 初始化数据库
    try:
        db = get_database()
        logger.info("数据库已初始化")
    except Exception as e:
        logger.warning(f"数据库初始化失败：{e}")
        db = None
    
    # 2. 初始化风控管理器
    try:
        config_path = "/opt/ftseek/FTSeek/config/risk_config.yaml"
        risk_manager = get_risk_manager(config_path)
        logger.info(f"风控管理器已初始化 | 模式={risk_manager.config.trading_mode}")
    except Exception as e:
        logger.warning(f"风控管理器初始化失败：{e}")
        risk_manager = RiskManager()
    
    # 3. 初始化钉钉推送
    try:
        webhook = os.environ.get("DINGTALK_WEBHOOK_URL", config.DINGTALK_WEBHOOK_URL)
        secret = os.environ.get("DINGTALK_SECRET", config.DINGTALK_SECRET)
        
        if webhook:
            dingtalk = DingTalkNotifier(webhook_url=webhook, secret=secret)
            logger.info("钉钉推送已初始化")
        else:
            logger.warning("钉钉 Webhook 未配置，使用日志输出")
            dingtalk = None
    except Exception as e:
        logger.warning(f"钉钉推送初始化失败：{e}")
        dingtalk = None
    
    # 4. 初始化风险监控器
    risk_monitor = get_risk_monitor(
        risk_manager=risk_manager,
        database=db,
        dingtalk_notifier=dingtalk
    )
    logger.info("风险监控器已初始化")
    
    return risk_monitor, db, risk_manager, dingtalk


def check_push_time() -> bool:
    """
    检查当前是否是推送时间
    
    Returns:
        bool: 是否是推送时间
    """
    now = datetime.now()
    
    # 检查是否是交易日 (周一到周五)
    if now.weekday() >= 5:  # 5=周六，6=周日
        logger.info(f"周末 ({now.strftime('%A')})，跳过推送")
        return False
    
    # 检查小时 (10, 12, 14, 16)
    push_hours = [10, 12, 14, 16]
    current_hour = now.hour
    
    if current_hour not in push_hours:
        logger.info(f"当前时间 {now.strftime('%H:%M')} 不是推送时间，下次推送：{push_hours}点")
        return False
    
    # 检查是否在整点附近 (前后 5 分钟内)
    current_minute = now.minute
    if current_minute > 5:
        logger.info(f"当前时间 {now.strftime('%H:%M')} 已过推送窗口 (0-5 分)，跳过")
        return False
    
    return True


def run_monitor(force: bool = False, test: bool = False):
    """
    运行风险监控推送
    
    Args:
        force: 是否强制推送 (忽略时间检查)
        test: 是否测试模式 (只打印，不推送)
    """
    logger.info("=" * 60)
    logger.info("FTSeek 风险监控推送开始")
    logger.info("=" * 60)
    
    # 初始化组件
    risk_monitor, db, risk_manager, dingtalk = init_components()
    
    # 检查推送时间
    if not force and not test:
        if not check_push_time():
            logger.info("未到推送时间，退出")
            return False
    
    # 收集并推送监控数据
    try:
        data = risk_monitor.collect_monitor_data()
        
        logger.info(f"监控数据已收集:")
        logger.info(f"  账户总值：HK${data.total_value:,.2f}")
        logger.info(f"  持仓市值：HK${data.position_value:,.2f}")
        logger.info(f"  今日盈亏：HK${data.today_pnl:+,.2f}")
        logger.info(f"  风控状态：{data.risk_status}")
        
        if test:
            logger.info("测试模式：不实际推送")
            print(risk_monitor.get_formatted_message(data))
            return True
        
        # 推送到钉钉
        if dingtalk:
            success = risk_monitor.push_monitor()
            if success:
                logger.info("✅ 风险监控推送成功")
                return True
            else:
                logger.error("❌ 风险监控推送失败")
                return False
        else:
            # 无钉钉时打印到日志
            risk_monitor._print_monitor_data(data)
            logger.info("监控数据已输出到日志")
            return True
            
    except Exception as e:
        logger.error(f"风险监控推送异常：{e}", exc_info=True)
        return False
    
    finally:
        logger.info("=" * 60)
        logger.info("FTSeek 风险监控推送结束")
        logger.info("=" * 60)


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="FTSeek 风险监控推送脚本")
    parser.add_argument("--force", action="store_true", help="强制推送 (忽略时间检查)")
    parser.add_argument("--test", action="store_true", help="测试模式 (只打印，不推送)")
    parser.add_argument("--init-data", action="store_true", help="初始化测试数据")
    
    args = parser.parse_args()
    
    # 初始化测试数据 (如果需要)
    if args.init_data:
        logger.info("初始化测试数据...")
        init_test_data()
    
    # 运行监控
    success = run_monitor(force=args.force, test=args.test)
    
    # 退出码
    sys.exit(0 if success else 1)


def init_test_data():
    """初始化测试数据"""
    from app.risk.risk_manager import RiskManager
    
    risk_manager = RiskManager()
    
    # 模拟账户数据
    risk_manager.account = {
        "total_capital": 1025340.0,
        "available_cash": 512670.0,
        "peak_capital": 1050000.0,
        "positions": {
            "HK.00700": {
                "quantity": 100,
                "entry_price": 420.0,
                "current_price": 428.5,
                "name": "腾讯控股",
            },
            "HK.09988": {
                "quantity": 200,
                "entry_price": 85.0,
                "current_price": 86.2,
                "name": "阿里巴巴",
            },
            "HK.09992": {
                "quantity": 150,
                "entry_price": 95.0,
                "current_price": 94.5,
                "name": "泡泡玛特",
            },
        }
    }
    
    # 模拟交易统计
    risk_manager.today_trades = 4
    risk_manager.today_pnl = 8450.0
    risk_manager.weekly_pnl = 32100.0
    risk_manager.consecutive_losses = 0
    
    logger.info("测试数据已初始化")


if __name__ == "__main__":
    main()
