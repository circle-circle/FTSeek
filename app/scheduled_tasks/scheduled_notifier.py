"""定时通知器 - 定期推送交易分析和告警"""
import logging
from datetime import datetime, time
from typing import Dict, Any, List, Optional, Callable
from pathlib import Path

logger = logging.getLogger("ftseek_us.scheduled.notifier")

class ScheduledNotifier:
    """定时通知器"""
    
    def __init__(self, notifier, config: Dict[str, Any]):
        self.notifier = notifier  # DingTalkNotifier 实例
        self.config = config
        
        # 通知时间表
        self.notification_times = config.get("notification_times", [
            {"hour": 9, "minute": 30, "label": "开盘"},      # 美东 9:30 (北京时间 21:30)
            {"hour": 12, "minute": 0, "label": "盘中"},      # 美东 12:00 (北京时间 0:00)
            {"hour": 16, "minute": 0, "label": "收盘"},      # 美东 16:00 (北京时间 4:00)
        ])
        
        # 分析间隔 (分钟)
        self.analysis_interval = config.get("analysis_interval_minutes", 30)
        
        # 上次通知时间
        self.last_notification_time: Optional[datetime] = None
    
    def should_notify(self) -> bool:
        """判断是否应该发送通知"""
        now = datetime.now()
        
        # 检查是否到达通知时间
        for schedule in self.notification_times:
            schedule_time = now.replace(
                hour=schedule["hour"],
                minute=schedule["minute"],
                second=0,
                microsecond=0
            )
            
            # 如果在调度时间前后 5 分钟内，且尚未通知
            if abs((now - schedule_time).total_seconds()) <= 300:
                if self.last_notification_time is None or \
                   (now - self.last_notification_time).total_seconds() > 600:
                    return True
        
        # 检查是否到达分析间隔
        if self.last_notification_time is None:
            return True
        
        elapsed = (now - self.last_notification_time).total_seconds()
        return elapsed >= self.analysis_interval * 60
    
    def send_analysis_notification(self, analysis_report: Dict[str, Any]):
        """发送分析通知"""
        try:
            summary = analysis_report.get("summary", {})
            risk = analysis_report.get("risk", {})
            
            # 构建通知内容
            title = f"📊 FTSeek-US 交易分析 {datetime.now().strftime('%m-%d %H:%M')}"
            
            message = f"""
【交易统计】
• 总交易：{summary.get('total_trades', 0)}笔
• 已平仓：{summary.get('closed_trades', 0)}笔
• 持仓中：{summary.get('open_trades', 0)}笔
• 总盈亏：${summary.get('total_pnl', 0):.2f}
• 平均盈亏：{summary.get('avg_pnl_percent', 0):.1f}%
• 胜率：{summary.get('win_rate', 0):.1%}

【风险指标】
• 最大回撤：{risk.get('max_drawdown', 0):.1%}
• 平均持仓：{risk.get('avg_holding_time_hours', 0):.1f}小时

【当前持仓】
{self._format_positions(analysis_report.get('open_positions', []))}

【最近交易】
{self._format_trades(analysis_report.get('recent_trades', []))}
"""
            
            # 发送钉钉通知
            self.notifier.notify_system_alert(
                title=title,
                message=message,
                level="INFO"
            )
            
            self.last_notification_time = datetime.now()
            logger.info("✅ 分析通知已发送")
            
        except Exception as e:
            logger.error(f"发送分析通知失败：{e}", exc_info=True)
    
    def send_alert(self, alert_type: str, alert_data: Dict[str, Any]):
        """发送告警通知"""
        try:
            title = f"⚠️ FTSeek-US 告警 {alert_type}"
            message = self._format_alert(alert_type, alert_data)
            
            self.notifier.notify_system_alert(
                title=title,
                message=message,
                level="WARNING"
            )
            
            logger.info(f"✅ 告警通知已发送：{alert_type}")
            
        except Exception as e:
            logger.error(f"发送告警通知失败：{e}", exc_info=True)
    
    def _format_positions(self, positions: List[Dict]) -> str:
        """格式化持仓"""
        if not positions:
            return "• 无持仓"
        
        lines = []
        for pos in positions[:5]:  # 最多显示 5 个
            pnl_sign = "+" if pos.get('unrealized_pnl', 0) >= 0 else ""
            lines.append(
                f"• {pos.get('symbol', 'N/A')} {pos.get('direction', 'N/A')} "
                f"{pos.get('quantity', 0)}股 "
                f"盈亏：{pnl_sign}${pos.get('unrealized_pnl', 0):.2f}"
            )
        
        if len(positions) > 5:
            lines.append(f"• ... 还有{len(positions) - 5}个持仓")
        
        return "\n".join(lines)
    
    def _format_trades(self, trades: List[Dict]) -> str:
        """格式化交易"""
        if not trades:
            return "• 无交易"
        
        lines = []
        for trade in trades:
            pnl_sign = "+" if trade.get('pnl', 0) >= 0 else ""
            lines.append(
                f"• {trade.get('symbol', 'N/A')} {trade.get('direction', 'N/A')} "
                f"盈亏：{pnl_sign}${trade.get('pnl', 0):.2f} "
                f"({trade.get('pnl_percent', 0):.1f}%)"
            )
        
        return "\n".join(lines)
    
    def _format_alert(self, alert_type: str, alert_data: Dict[str, Any]) -> str:
        """格式化告警"""
        if alert_type == "max_drawdown":
            return f"""
【回撤告警】
• 当前回撤：{alert_data.get('current_drawdown', 0):.1%}
• 回撤阈值：{alert_data.get('threshold', 0):.1%}
• 建议：关注风险控制
"""
        elif alert_type == "position_limit":
            return f"""
【仓位告警】
• 当前仓位：{alert_data.get('current_position', 0):.1%}
• 仓位阈值：{alert_data.get('threshold', 0):.1%}
• 建议：控制仓位
"""
        elif alert_type == "error":
            return f"""
【错误告警】
• 错误类型：{alert_data.get('error_type', 'N/A')}
• 错误信息：{alert_data.get('error_message', 'N/A')}
• 建议：检查系统状态
"""
        else:
            return f"""
【告警】
• 类型：{alert_type}
• 详情：{alert_data}
"""
