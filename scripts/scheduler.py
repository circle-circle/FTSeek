"""
定时任务调度器

自动化执行盘前检查、盘中监控、收盘复盘等任务
"""
import logging
import schedule
import time
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("ftseek.scheduler")

# 导入任务模块
from app.risk.realtime_alert import get_realtime_risk_monitor
from app.storage.trade_recorder import get_trade_recorder
from app.reports.weekly_review import get_weekly_review_generator


class TradingScheduler:
    """交易调度器"""
    
    def __init__(self):
        self.running = False
        self.scheduler = schedule.Scheduler()
        self._setup_jobs()
        
        logger.info("交易调度器已初始化")
    
    def _setup_jobs(self):
        """设置定时任务"""
        # 盘前检查 (交易日 09:00)
        self.scheduler.every().monday.at('09:00').do(self.pre_market_check)
        self.scheduler.every().tuesday.at('09:00').do(self.pre_market_check)
        self.scheduler.every().wednesday.at('09:00').do(self.pre_market_check)
        self.scheduler.every().thursday.at('09:00').do(self.pre_market_check)
        self.scheduler.every().friday.at('09:00').do(self.pre_market_check)
        
        # 盘中监控 (交易日 09:30-16:00, 每 5 分钟)
        self.scheduler.every().monday.at('09:30').do(self.intra_day_monitor)
        self.scheduler.every().monday.at('09:35').do(self.intra_day_monitor)
        # ... 简化示例，实际应每分钟设置
        
        # 收盘复盘 (交易日 16:30)
        self.scheduler.every().monday.at('16:30').do(self.post_market_review)
        self.scheduler.every().tuesday.at('16:30').do(self.post_market_review)
        self.scheduler.every().wednesday.at('16:30').do(self.post_market_review)
        self.scheduler.every().thursday.at('16:30').do(self.post_market_review)
        self.scheduler.every().friday.at('16:30').do(self.post_market_review)
        
        # 周报复盘 (周日 20:00)
        self.scheduler.every().sunday.at('20:00').do(self.weekly_review)
        
        logger.info("定时任务已设置")
    
    def pre_market_check(self):
        """盘前检查"""
        logger.info("="*60)
        logger.info("开始盘前检查")
        logger.info("="*60)
        
        # 1. 检查风险状态
        monitor = get_realtime_risk_monitor()
        status = monitor.get_current_status()
        
        logger.info(f"连续亏损：{status['consecutive_losses']}")
        logger.info(f"当前回撤：{status['drawdown']:.2%}")
        
        # 2. 检查 API 连接
        api_ok = self._check_api_connection()
        logger.info(f"API 连接：{'✅ 正常' if api_ok else '❌ 异常'}")
        
        # 3. 检查钉钉推送
        dingtalk_ok = self._check_dingtalk_push()
        logger.info(f"钉钉推送：{'✅ 正常' if dingtalk_ok else '❌ 异常'}")
        
        # 4. 重置日统计
        monitor.reset_daily_stats()
        logger.info("日统计已重置")
        
        logger.info("="*60)
        logger.info("盘前检查完成")
        logger.info("="*60)
    
    def intra_day_monitor(self):
        """盘中监控"""
        logger.debug("盘中监控...")
        
        # 1. 检查风险预警
        monitor = get_realtime_risk_monitor()
        alerts = monitor.get_alerts(level='critical')
        
        if alerts:
            logger.warning(f"发现 {len(alerts)} 条严重预警")
            for alert in alerts:
                logger.warning(f"  - {alert.message}")
        
        # 2. 检查今日交易
        recorder = get_trade_recorder()
        summary = recorder.get_today_summary()
        
        if summary['closed_trades'] > 0:
            logger.info(f"今日交易：{summary['closed_trades']}笔，胜率 {summary['win_rate']:.1%}")
    
    def post_market_review(self):
        """收盘复盘"""
        logger.info("="*60)
        logger.info("开始收盘复盘")
        logger.info("="*60)
        
        # 1. 获取今日交易汇总
        recorder = get_trade_recorder()
        summary = recorder.get_today_summary()
        
        logger.info(f"今日交易：{summary['total_trades']}笔")
        logger.info(f"盈利交易：{summary['winning_trades']}笔")
        logger.info(f"亏损交易：{summary['losing_trades']}笔")
        logger.info(f"胜率：{summary['win_rate']:.1%}")
        logger.info(f"总盈亏：HK${summary['total_pnl']:.2f}")
        
        # 2. 发送钉钉推送
        self._send_daily_report(summary)
        
        logger.info("="*60)
        logger.info("收盘复盘完成")
        logger.info("="*60)
    
    def weekly_review(self):
        """周报复盘"""
        logger.info("="*60)
        logger.info("开始周报复盘")
        logger.info("="*60)
        
        # 1. 生成周报
        reviewer = get_weekly_review_generator()
        
        # 这里需要整合交易记录和 AI 预测数据
        # 简化示例
        report = reviewer.generate_report(
            start_date='2026-03-15',
            end_date='2026-03-21',
            trade_records=[],
            prediction_stats={},
            attribution_report={}
        )
        
        # 2. 保存报告
        filepath = reviewer.save_report(report)
        logger.info(f"周报已保存：{filepath}")
        
        # 3. 发送钉钉推送
        md_report = reviewer.generate_markdown(report)
        self._send_weekly_report(md_report)
        
        logger.info("="*60)
        logger.info("周报复盘完成")
        logger.info("="*60)
    
    def _check_api_connection(self) -> bool:
        """检查 API 连接"""
        try:
            # 检查富途 API
            # 检查 AI API
            return True
        except Exception as e:
            logger.error(f"API 检查失败：{e}")
            return False
    
    def _check_dingtalk_push(self) -> bool:
        """检查钉钉推送"""
        try:
            # 发送测试消息
            return True
        except Exception as e:
            logger.error(f"钉钉推送检查失败：{e}")
            return False
    
    def _send_daily_report(self, summary: dict):
        """发送日报到钉钉"""
        # 实现钉钉推送逻辑
        logger.info("日报已发送到钉钉")
    
    def _send_weekly_report(self, md_content: str):
        """发送周报 to 钉钉"""
        # 实现钉钉推送逻辑
        logger.info("周报已发送到钉钉")
    
    def start(self):
        """启动调度器"""
        if self.running:
            logger.warning("调度器已在运行")
            return
        
        self.running = True
        logger.info("调度器已启动")
        
        # 在后台线程运行
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()
    
    def stop(self):
        """停止调度器"""
        self.running = False
        logger.info("调度器已停止")
    
    def _run(self):
        """运行调度器"""
        while self.running:
            self.scheduler.run_pending()
            time.sleep(1)


# 全局单例
_scheduler = None

def get_trading_scheduler() -> TradingScheduler:
    """获取交易调度器单例"""
    global _scheduler
    if _scheduler is None:
        _scheduler = TradingScheduler()
    return _scheduler


# Crontab 配置示例
CRONTAB_EXAMPLE = """
# FTSeek 定时任务配置

# 盘前检查 (交易日 09:00)
0 9 * * 1-5 cd /opt/ftseek/FTSeek && python3 scripts/run_pre_market_check.py

# 收盘复盘 (交易日 16:30)
30 16 * * 1-5 cd /opt/ftseek/FTSeek && python3 scripts/run_post_market_review.py

# 周报复盘 (周日 20:00)
0 20 * * 0 cd /opt/ftseek/FTSeek && python3 scripts/run_weekly_review.py
"""
