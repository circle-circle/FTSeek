"""
风险监控面板模块 (Risk Monitor) - FTSeek v3.0

功能：
- 每 2 小时推送交易健康度看板（替换旧版风险监控）
- 策略层：胜率/盈亏比/期望/回撤/Sharpe/Profit Factor
- 执行层：成交率/滑点/延迟等
- 风控层：熔断/拦截率/敞口
- 系统层：存活/行情延迟/DB延迟/队列

推送频率：交易日每 2 小时 (10:00/12:00/14:00/16:00)
"""
import logging
import os
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from dataclasses import dataclass, field

logger = logging.getLogger("ftseek.risk.monitor")
# 2026-04-10: propagate=True (默认), 通过 ftseek 父级 logger 输出


@dataclass
class RiskMonitorData:
    """风险监控数据结构"""
    # 时间信息
    timestamp: str = ""
    time_str: str = ""
    
    # 账户信息
    total_value: float = 0.0
    cash: float = 0.0
    position_value: float = 0.0
    
    # 盈亏信息
    today_pnl: float = 0.0
    today_pnl_pct: float = 0.0
    week_pnl: float = 0.0
    week_pnl_pct: float = 0.0
    drawdown: float = 0.0
    max_drawdown: float = 0.15
    
    # 持仓信息
    position_count: int = 0
    today_trades: int = 0
    max_daily: int = 10
    
    # 风控信息
    consecutive_loss: int = 0
    risk_ok: bool = True
    risk_status: str = "✅ 正常"
    
    # 详细持仓列表
    positions: List[Dict] = field(default_factory=list)
    
    # 健康度看板（新增，Task3 中由 dingtalk 直接渲染）
    health: Any = None  # HealthDashboard 实例（避免循环导入，用 Any）
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            "time": self.time_str,
            "timestamp": self.timestamp,
            "total_value": self.total_value,
            "cash": self.cash,
            "position_value": self.position_value,
            "today_pnl": self.today_pnl,
            "today_pnl_pct": self.today_pnl_pct,
            "week_pnl": self.week_pnl,
            "week_pnl_pct": self.week_pnl_pct,
            "drawdown": self.drawdown,
            "max_drawdown": self.max_drawdown * 100,
            "position_count": self.position_count,
            "today_trades": self.today_trades,
            "max_daily": self.max_daily,
            "consecutive_loss": self.consecutive_loss,
            "risk_ok": self.risk_ok,
            "risk_status": self.risk_status,
            "positions": self.positions,
        }


class RiskMonitor:
    """
    风险监控器 - FTSeek v3.0
    
    负责收集和推送风险监控面板数据
    """

    def __init__(self, risk_manager=None, database=None, dingtalk_notifier=None,
                 circuit_breaker=None, subscriber_ref=None, executor_ref=None):
        """
        初始化风险监控器
        
        Args:
            risk_manager:      风控管理器实例
            database:          数据库实例（保留兼容）
            dingtalk_notifier: 钉钉推送实例
            circuit_breaker:   熔断器实例（风控层）
            subscriber_ref:    DataSubscriber 实例（行情延迟）
            executor_ref:      TradeExecutor 实例（执行层/滑点）
        """
        self.risk_manager = risk_manager
        self.database = database
        self.dingtalk_notifier = dingtalk_notifier
        self.circuit_breaker = circuit_breaker
        self.subscriber_ref = subscriber_ref
        self.executor_ref = executor_ref
        
        # 推送时间配置 (交易日)
        self.push_times = ["10:00", "12:00", "14:00", "16:00"]
        
        logger.info("风险监控器已初始化")

    def collect_monitor_data(self) -> RiskMonitorData:
        """
        收集风险监控数据
        
        Returns:
            RiskMonitorData: 风险监控数据
        """
        now = datetime.now()
        data = RiskMonitorData()
        data.timestamp = now.isoformat()
        data.time_str = now.strftime("%H:%M")
        
        # 1. 从风控管理器获取账户信息
        if self.risk_manager:
            status = self.risk_manager.get_status()
            
            # 账户信息
            capital_info = status.get("capital", {})
            data.total_value = capital_info.get("total", 0.0)
            data.cash = self.risk_manager.account.get("available_cash", 0.0)
            data.position_value = data.total_value - data.cash
            
            # 回撤信息
            drawdown_str = capital_info.get("drawdown", "0%")
            data.drawdown = float(drawdown_str.replace("%", "")) / 100.0 if drawdown_str else 0.0
            data.max_drawdown = self.risk_manager.config.max_drawdown
            
            # 持仓信息
            position_info = status.get("position", {})
            data.position_count = position_info.get("count", 0)
            
            # 今日交易信息
            today_info = status.get("today", {})
            trades_str = today_info.get("trades", "0/10")
            data.today_trades = int(trades_str.split("/")[0]) if "/" in trades_str else 0
            data.max_daily = self.risk_manager.config.max_daily_trades
            
            # 盈亏信息
            today_pnl_str = today_info.get("pnl", "0.00")
            data.today_pnl = float(today_pnl_str) if today_pnl_str else 0.0
            data.today_pnl_pct = (data.today_pnl / data.total_value * 100) if data.total_value > 0 else 0.0
            
            weekly_info = status.get("weekly", {})
            week_pnl_str = weekly_info.get("pnl", "0.00")
            data.week_pnl = float(week_pnl_str) if week_pnl_str else 0.0
            data.week_pnl_pct = (data.week_pnl / data.total_value * 100) if data.total_value > 0 else 0.0
            
            # 风控信息
            system_info = status.get("system", {})
            data.consecutive_loss = system_info.get("consecutive_losses", 0)
            
            # 判断风控状态
            data.risk_ok = (
                data.drawdown < data.max_drawdown and
                data.today_trades < data.max_daily and
                data.consecutive_loss < self.risk_manager.config.consecutive_loss_limit
            )
            data.risk_status = "✅ 正常" if data.risk_ok else "⚠️ 异常"
            
            # 获取持仓详情
            data.positions = self._get_position_details()
        else:
            # 无风控管理器时返回空数据
            logger.warning("风控管理器未初始化，返回空监控数据")
            data.risk_status = "⚠️ 未初始化"
        
        # 采集健康度看板（新增）
        try:
            from app.risk.health_dashboard import HealthDashboardCollector
            collector = HealthDashboardCollector(
                risk_manager=self.risk_manager,
                circuit_breaker=self.circuit_breaker,
                subscriber_ref=self.subscriber_ref,
                executor_ref=self.executor_ref,
            )
            data.health = collector.collect()
        except Exception as e:
            logger.warning(f"健康度看板采集失败: {e}")
        
        return data

    def _get_position_details(self) -> List[Dict]:
        """获取持仓详情"""
        positions = []
        
        if self.risk_manager:
            for code, pos in self.risk_manager.account.get("positions", {}).items():
                quantity = pos.get("quantity", 0)
                entry_price = pos.get("entry_price", 0)
                current_price = pos.get("current_price", entry_price)
                
                pnl_pct = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
                
                positions.append({
                    "code": code,
                    "name": pos.get("name", ""),
                    "quantity": quantity,
                    "entry_price": entry_price,
                    "current_price": current_price,
                    "market_value": quantity * current_price,
                    "pnl": (current_price - entry_price) * quantity,
                    "pnl_pct": pnl_pct,
                })
        
        return positions

    def check_and_push(self) -> bool:
        """
        检查当前时间并推送监控面板
        
        Returns:
            bool: 是否成功推送
        """
        now = datetime.now()
        current_time = now.strftime("%H:%M")
        
        # 检查是否是推送时间
        if current_time not in self.push_times:
            logger.debug(f"当前时间 {current_time} 不是推送时间，跳过")
            return False
        
        # 检查是否是交易日 (周一到周五)
        if now.weekday() >= 5:  # 5=周六，6=周日
            logger.info("周末，跳过风险监控推送")
            return False
        
        # 收集数据并推送
        return self.push_monitor()

    def push_monitor(self) -> bool:
        """
        推送交易健康度看板（每2小时）
        
        Returns:
            bool: 是否成功推送
        """
        try:
            # 收集监控数据（含健康度看板）
            data = self.collect_monitor_data()
            
            if self.dingtalk_notifier:
                # 优先使用新版健康度看板推送
                if data.health is not None and hasattr(self.dingtalk_notifier, 'send_health_dashboard'):
                    success = self.dingtalk_notifier.send_health_dashboard(data)
                else:
                    # 降级到旧版风险监控推送
                    success = self.dingtalk_notifier.send_risk_monitor(data.to_dict())
                if success:
                    logger.info(f"交易健康度看板已推送：{data.time_str}")
                    return True
                else:
                    logger.error("交易健康度看板推送失败")
                    return False
            else:
                # 无钉钉推送时，打印日志
                self._print_monitor_data(data)
                return True
                
        except Exception as e:
            logger.error(f"推送交易健康度看板异常：{e}", exc_info=True)
            return False

    def _print_monitor_data(self, data: RiskMonitorData):
        """打印监控数据到日志"""
        logger.info(f"""
📊 FTSeek 风险监控 ({data.time_str})
---
账户总值：HK${data.total_value:,.2f}
可用现金：HK${data.cash:,.2f} ({data.cash/data.total_value*100:.0f}%)
持仓市值：HK${data.position_value:,.2f} ({data.position_value/data.total_value*100:.0f}%)
---
今日盈亏：HK${data.today_pnl:+,.2f} ({data.today_pnl_pct:+.2f}%)
本周盈亏：HK${data.week_pnl:+,.2f} ({data.week_pnl_pct:+.2f}%)
总回撤：{data.drawdown:.2f}% (安全线 {data.max_drawdown*100:.0f}%)
---
持仓数量：{data.position_count} 只
今日交易：{data.today_trades}/{data.max_daily} 次
连续亏损：{data.consecutive_loss} 次
---
风控状态：{data.risk_status}
""")

    def get_formatted_message(self, data: RiskMonitorData) -> str:
        """
        获取格式化的监控消息
        
        Args:
            data: 风险监控数据
            
        Returns:
            str: 格式化的消息文本
        """
        today_icon = "🟢" if data.today_pnl >= 0 else "🔴"
        week_icon = "🟢" if data.week_pnl >= 0 else "🔴"
        
        message = f"""📊 FTSeek 风险监控 ({data.time_str})

账户总值：HK${data.total_value:,.2f}
可用现金：HK${data.cash:,.2f} ({data.cash/data.total_value*100:.0f}%)
持仓市值：HK${data.position_value:,.2f} ({data.position_value/data.total_value*100:.0f}%)

{today_icon} 今日盈亏：HK${data.today_pnl:+,.2f} ({data.today_pnl_pct:+.2f}%)
{week_icon} 本周盈亏：HK${data.week_pnl:+,.2f} ({data.week_pnl_pct:+.2f}%)
总回撤：{data.drawdown:.2f}% (安全线 {data.max_drawdown*100:.0f}%)

持仓数量：{data.position_count} 只
今日交易：{data.today_trades}/{data.max_daily} 次
连续亏损：{data.consecutive_loss} 次

风控状态：{data.risk_status}
"""
        return message


# ==================== 全局单例 ====================

risk_monitor: Optional[RiskMonitor] = None


def get_risk_monitor(risk_manager=None, database=None, dingtalk_notifier=None,
                     circuit_breaker=None, subscriber_ref=None, executor_ref=None) -> RiskMonitor:
    """获取风险监控器单例"""
    global risk_monitor
    if risk_monitor is None:
        risk_monitor = RiskMonitor(risk_manager, database, dingtalk_notifier,
                                   circuit_breaker, subscriber_ref, executor_ref)
    return risk_monitor


def init_risk_monitor(risk_manager=None, database=None, dingtalk_notifier=None,
                      circuit_breaker=None, subscriber_ref=None, executor_ref=None) -> RiskMonitor:
    """初始化风险监控器（强制重新创建）"""
    global risk_monitor
    risk_monitor = RiskMonitor(risk_manager, database, dingtalk_notifier,
                               circuit_breaker, subscriber_ref, executor_ref)
    return risk_monitor
