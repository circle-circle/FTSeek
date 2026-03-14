"""
TradingScheduler - 交易定时任务调度器

功能:
- 日报任务调度 (港股/美股)
- 周报任务调度
- 绩效检查任务 (每小时)
- 时区转换 (HKT/EST/Asia/Shanghai)
- 任务失败日志和重试

定时任务配置:
- 港股日报：周一至周五 16:30 HKT (收盘后 30 分钟)
- 美股日报：周一至周五 04:30 EST (收盘后 30 分钟)
- 周报：周日 20:00 Asia/Shanghai
- 绩效检查：每小时一次
"""
import logging
from datetime import datetime
from typing import Optional, Dict, Any

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.v4.report.report_generator import ReportGenerator
from app.v4.core.decision_engine import DecisionEngine


logger = logging.getLogger("ftseek.v4.trading_scheduler")


class TradingScheduler:
    """交易定时任务调度器"""
    
    def __init__(
        self,
        report_generator: ReportGenerator,
        decision_engine: Optional[DecisionEngine] = None,
        db_path: str = None
    ):
        """
        初始化调度器
        
        Args:
            report_generator: 报告生成器实例
            decision_engine: 决策引擎实例 (用于绩效检查)
            db_path: 数据库路径
        """
        self.report_generator = report_generator
        self.decision_engine = decision_engine
        self.db_path = db_path
        
        # 时区配置
        self.timezone_hk = pytz.timezone("Asia/Hong_Kong")  # HKT (UTC+8)
        self.timezone_us = pytz.timezone("US/Eastern")      # EST/EDT (UTC-5/UTC-4)
        self.timezone_shanghai = pytz.timezone("Asia/Shanghai")  # CST (UTC+8)
        
        # 调度器
        self.scheduler = BackgroundScheduler(
            timezone=self.timezone_shanghai,
            logger=logger
        )
        
        # 运行状态
        self._running = False
        self.logger = logger
        
        logger.info("交易调度器已初始化")
    
    def start(self):
        """启动调度器"""
        if self._running:
            raise RuntimeError("Scheduler already running")
        
        self.scheduler.start()
        self._running = True
        
        logger.info("交易调度器已启动")
    
    def stop(self):
        """停止调度器"""
        if not self._running:
            return
        
        # 关闭调度器，等待任务完成
        self.scheduler.shutdown(wait=True)
        self._running = False
        
        logger.info("交易调度器已停止")
    
    def schedule_daily_report(self, code: str, market: str):
        """
        调度日报任务
        
        Args:
            code: 股票代码
            market: 市场类型 (HK/US)
        """
        if market == "HK":
            # 港股日报：周一至周五 16:30 HKT
            trigger = CronTrigger(
                hour=16,
                minute=30,
                day_of_week="mon-fri",
                timezone=self.timezone_hk
            )
            self.scheduler.add_job(
                func=self._send_daily_report,
                trigger=trigger,
                args=[code, market],
                id=f"daily_report_{code}",
                name=f"港股日报-{code}",
                replace_existing=True,
                max_instances=1,
                misfire_grace_time=300  # 5 分钟容错
            )
            logger.info(f"已调度港股日报任务：{code} (周一至周五 16:30 HKT)")
        
        elif market == "US":
            # 美股日报：周一至周五 04:30 EST
            trigger = CronTrigger(
                hour=4,
                minute=30,
                day_of_week="mon-fri",
                timezone=self.timezone_us
            )
            self.scheduler.add_job(
                func=self._send_daily_report,
                trigger=trigger,
                args=[code, market],
                id=f"daily_report_{code}",
                name=f"美股日报-{code}",
                replace_existing=True,
                max_instances=1,
                misfire_grace_time=300  # 5 分钟容错
            )
            logger.info(f"已调度美股日报任务：{code} (周一至周五 04:30 EST)")
        
        else:
            logger.warning(f"未知市场类型：{market}，跳过日报调度")
    
    def schedule_weekly_report(self, code: str):
        """
        调度周报任务
        
        Args:
            code: 股票代码
        """
        # 周报：周日 20:00 Asia/Shanghai
        trigger = CronTrigger(
            hour=20,
            minute=0,
            day_of_week="sun",
            timezone=self.timezone_shanghai
        )
        self.scheduler.add_job(
            func=self._send_weekly_report,
            trigger=trigger,
            args=[code],
            id=f"weekly_report_{code}",
            name=f"周报-{code}",
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=600  # 10 分钟容错
        )
        logger.info(f"已调度周报任务：{code} (周日 20:00 Asia/Shanghai)")
    
    def schedule_performance_check(self):
        """调度绩效检查任务 (每小时一次)"""
        # 每小时检查一次
        trigger = CronTrigger(
            minute=0,  # 每小时整点
            timezone=self.timezone_shanghai
        )
        self.scheduler.add_job(
            func=self._check_performance,
            trigger=trigger,
            id="performance_check",
            name="绩效检查",
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=60  # 1 分钟容错
        )
        logger.info("已调度绩效检查任务 (每小时一次)")
    
    def _send_daily_report(self, code: str, market: str):
        """
        发送日报 (内部方法)
        
        Args:
            code: 股票代码
            market: 市场类型
        """
        try:
            logger.info(f"开始生成日报：{code} ({market})")
            
            result = self.report_generator.send_daily_report(code=code)
            
            if result.get("success"):
                logger.info(f"日报推送成功：{code}")
            else:
                error = result.get("error", "Unknown error")
                logger.error(f"日报推送失败：{code} - {error}")
        
        except Exception as e:
            logger.exception(f"日报推送异常：{code} - {e}")
    
    def _send_weekly_report(self, code: str):
        """
        发送周报 (内部方法)
        
        Args:
            code: 股票代码
        """
        try:
            logger.info(f"开始生成周报：{code}")
            
            result = self.report_generator.send_weekly_report(code=code)
            
            if result.get("success"):
                logger.info(f"周报推送成功：{code}")
            else:
                error = result.get("error", "Unknown error")
                logger.error(f"周报推送失败：{code} - {error}")
        
        except Exception as e:
            logger.exception(f"周报推送异常：{code} - {e}")
    
    def _check_performance(self):
        """检查绩效 (内部方法)"""
        if not self.decision_engine:
            logger.warning("DecisionEngine 未配置，跳过绩效检查")
            return
        
        try:
            logger.debug("开始绩效检查")
            
            # 检查所有持仓的退出条件
            exit_decisions = self.decision_engine.exit_manager.check_all_positions()
            
            for code, exit_decision in exit_decisions:
                if exit_decision.should_exit:
                    logger.info(
                        f"触发退出条件：{code} - {exit_decision.reason} "
                        f"(收益率：{exit_decision.expected_return:.2%})"
                    )
                    
                    # 执行卖出
                    try:
                        self.decision_engine.paper_trader.sell(
                            code=code,
                            reason=exit_decision.reason
                        )
                        logger.info(f"已执行退出：{code}")
                    except Exception as e:
                        logger.error(f"执行退出失败：{code} - {e}")
            
            logger.debug(f"绩效检查完成，检查了 {len(exit_decisions)} 个持仓")
        
        except Exception as e:
            logger.exception(f"绩效检查失败：{e}")


# 全局单例
_scheduler: Optional[TradingScheduler] = None


def init_trading_scheduler(
    report_generator: ReportGenerator,
    decision_engine: Optional[DecisionEngine] = None,
    db_path: str = None
) -> TradingScheduler:
    """
    初始化调度器单例
    
    Args:
        report_generator: 报告生成器实例
        decision_engine: 决策引擎实例
        db_path: 数据库路径
    
    Returns:
        TradingScheduler 实例
    """
    global _scheduler
    _scheduler = TradingScheduler(
        report_generator=report_generator,
        decision_engine=decision_engine,
        db_path=db_path
    )
    return _scheduler


def get_trading_scheduler() -> Optional[TradingScheduler]:
    """
    获取调度器单例
    
    Returns:
        TradingScheduler 实例或 None
    """
    return _scheduler
