"""
DingTalkNotifier V4 - 钉钉推送格式化模块

功能:
- 盘前观察列表推送 (09:00)
- 交易信号推送 (实时)
- 风险监控警报 (实时)
- 收盘统计报告 (16:30)
"""
import time
import hmac
import hashlib
import base64
import urllib.parse
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import requests

logger = logging.getLogger("ftseek.v4.dingtalk_notifier")


class PushType(str, Enum):
    """推送类型"""
    PREMARKET_WATCHLIST = "PREMARKET_WATCHLIST"  # 盘前观察列表
    TRADE_SIGNAL = "TRADE_SIGNAL"                  # 交易信号
    RISK_ALERT = "RISK_ALERT"                      # 风险警报
    DAILY_REPORT = "DAILY_REPORT"                  # 日报


class AlertLevel(str, Enum):
    """警报级别"""
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass
class WatchlistItem:
    """观察列表项"""
    code: str
    name: str = ""
    priority: int = 1
    reason: str = ""
    watch_price: Optional[float] = None
    
    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "priority": self.priority,
            "reason": self.reason,
            "watch_price": self.watch_price,
        }


@dataclass
class SignalPush:
    """信号推送"""
    code: str
    action: str  # BUY or SELL
    entry_price: float
    stop_loss: Optional[float]
    target_price: float
    confidence: float
    reason: str = ""
    quantity: Optional[int] = None
    
    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "action": self.action,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "target_price": self.target_price,
            "confidence": self.confidence,
            "reason": self.reason,
        }


@dataclass
class AlertPush:
    """警报推送"""
    level: AlertLevel
    alert_type: str
    code: str
    message: str
    value: Optional[float] = None
    
    def to_dict(self) -> dict:
        return {
            "level": self.level.value,
            "alert_type": self.alert_type,
            "code": self.code,
            "message": self.message,
            "value": self.value,
        }


@dataclass
class DailyReport:
    """日报"""
    date: str
    total_trades: int
    winning_trades: int
    net_pnl: float
    win_rate: float
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    
    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.total_trades - self.winning_trades,
            "net_pnl": self.net_pnl,
            "win_rate": self.win_rate,
            "gross_profit": self.gross_profit,
            "gross_loss": self.gross_loss,
        }


class DingTalkNotifierV4:
    """钉钉通知器 V4"""
    
    def __init__(
        self,
        webhook_url: Optional[str] = None,
        secret: Optional[str] = None,
        max_length: int = 200,
        enable_push: bool = True
    ):
        """
        初始化钉钉通知器
        
        Args:
            webhook_url: 钉钉机器人 webhook URL
            secret: 钉钉机器人加签 secret
            max_length: 消息最大长度
            enable_push: 是否启用推送
        """
        self.webhook_url = webhook_url
        self.secret = secret
        self.max_length = max_length
        self.enable_push = enable_push
        
        # Emoji 库
        self.emojis = {
            "BUY": "🟢",
            "SELL": "🔴",
            "INFO": "📢",
            "WARNING": "⚠️",
            "CRITICAL": "🚨",
            "PROFIT": "✅",
            "LOSS": "❌",
            "WATCH": "👀"
        }
        
        self.logger = logging.getLogger("ftseek.v4.dingtalk_notifier")
    
    def format_premarket_watchlist(self, watchlist: List[WatchlistItem]) -> str:
        """
        格式化盘前观察列表
        
        Args:
            watchlist: 观察列表
            
        Returns:
            格式化后的消息
        """
        # 按优先级排序
        sorted_watchlist = sorted(watchlist, key=lambda x: x.priority)
        
        lines = [f"【盘前观察】{datetime.now().strftime('%m-%d %H:%M')}"]
        lines.append(f"共{len(sorted_watchlist)}只标的")
        lines.append("")
        
        for i, item in enumerate(sorted_watchlist[:10], 1):  # 最多 10 只
            icon = self.emojis["WATCH"]
            price_info = f"@{item.watch_price}" if item.watch_price else ""
            lines.append(f"{i}. {icon} {item.code} {item.name} {price_info}")
            if item.reason:
                lines.append(f"   理由：{item.reason}")
        
        output = "\n".join(lines)
        
        if len(output) > self.max_length:
            output = output[:self.max_length - 3] + "..."
        
        return output
    
    def format_trade_signal(self, signal: SignalPush) -> str:
        """
        格式化交易信号
        
        Args:
            signal: 信号推送
            
        Returns:
            格式化后的消息
        """
        icon = self.emojis.get(signal.action, "📢")
        action_text = "买入" if signal.action == "BUY" else "卖出"
        
        # 置信度表达
        if signal.confidence >= 0.80:
            conf_text = f"高置信 ({signal.confidence:.0%})"
        elif signal.confidence >= 0.60:
            conf_text = f"中等 ({signal.confidence:.0%})"
        else:
            conf_text = f"低 ({signal.confidence:.0%})"
        
        lines = [
            f"{icon}【{action_text}】{signal.code}",
            f"入场：{signal.entry_price}",
            f"止损：{signal.stop_loss or '无'}",
            f"目标：{signal.target_price}",
            f"置信：{conf_text}"
        ]
        
        # 盈亏比
        if signal.stop_loss:
            if signal.action == "BUY":
                reward = signal.target_price - signal.entry_price
                risk = signal.entry_price - signal.stop_loss
            else:
                reward = signal.entry_price - signal.target_price
                risk = signal.stop_loss - signal.entry_price
            
            rr = reward / risk if risk > 0 else 0
            lines.append(f"盈亏比：1:{rr:.1f}")
        
        # 理由
        if signal.reason:
            lines.append(f"理由：{signal.reason}")
        
        output = "\n".join(lines)
        
        if len(output) > self.max_length:
            output = output[:self.max_length - 3] + "..."
        
        return output
    
    def format_risk_alert(self, alert: AlertPush) -> str:
        """
        格式化风险警报
        
        Args:
            alert: 警报推送
            
        Returns:
            格式化后的消息
        """
        icon = self.emojis.get(alert.level.value, "📢")
        
        level_text = {
            "INFO": "提示",
            "WARNING": "警告",
            "CRITICAL": "紧急"
        }.get(alert.level.value, alert.level.value)
        
        lines = [
            f"{icon}【{level_text}】{alert.alert_type}",
            f"标的：{alert.code}",
            f"消息：{alert.message}"
        ]
        
        if alert.value is not None:
            lines.append(f"数值：{alert.value}")
        
        return "\n".join(lines)
    
    def format_daily_report(self, report: DailyReport) -> str:
        """
        格式化日报
        
        Args:
            report: 日报
            
        Returns:
            格式化后的消息
        """
        # 盈亏图标
        if report.net_pnl > 0:
            pnl_icon = self.emojis["PROFIT"]
            pnl_sign = "+"
        elif report.net_pnl < 0:
            pnl_icon = self.emojis["LOSS"]
            pnl_sign = ""
        else:
            pnl_icon = "➖"
            pnl_sign = ""
        
        lines = [
            f"{pnl_icon}【交易日报】{report.date}",
            f"交易：{report.total_trades}笔",
            f"胜率：{report.win_rate:.0%} ({report.winning_trades}胜{report.total_trades - report.winning_trades}负)",
            f"盈亏：{pnl_sign}{report.net_pnl:.0f}"
        ]
        
        # 详细统计
        if report.gross_profit > 0 or report.gross_loss > 0:
            lines.append(f"毛盈利：+{report.gross_profit:.0f} | 毛亏损：-{report.gross_loss:.0f}")
        
        if report.largest_win > 0 or report.largest_loss < 0:
            lines.append(f"最大盈利：+{report.largest_win:.0f} | 最大亏损：{report.largest_loss:.0f}")
        
        return "\n".join(lines)
    
    def format_multiple_alerts(self, alerts: List[AlertPush]) -> str:
        """
        格式化多条警报
        
        Args:
            alerts: 警报列表
            
        Returns:
            格式化后的消息
        """
        if not alerts:
            return "无警报"
        
        lines = [f"【警报汇总】{len(alerts)}条"]
        lines.append("")
        
        for alert in alerts[:5]:  # 最多 5 条
            icon = self.emojis.get(alert.level.value, "📢")
            lines.append(f"{icon} {alert.code}: {alert.message}")
        
        if len(alerts) > 5:
            lines.append(f"... 还有{len(alerts) - 5}条")
        
        return "\n".join(lines)
    
    def format_with_template(self, signal: SignalPush, template: str) -> str:
        """
        使用自定义模板格式化
        
        Args:
            signal: 信号推送
            template: 模板字符串
            
        Returns:
            格式化后的消息
        """
        output = template.format(
            code=signal.code,
            action=signal.action,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss or '无',
            target_price=signal.target_price,
            confidence=f"{signal.confidence:.0%}",
            reason=signal.reason
        )
        
        if len(output) > self.max_length:
            output = output[:self.max_length - 3] + "..."
        
        return output
    
    def send(self, message: str, msg_type: str = "text") -> Dict[str, Any]:
        """
        发送消息
        
        Args:
            message: 消息内容
            msg_type: 消息类型 (text/markdown)
            
        Returns:
            发送结果
        """
        if not self.enable_push:
            self.logger.info(f"推送已禁用：{message[:50]}...")
            return {"success": False, "error": "Push disabled"}
        
        if not self.webhook_url:
            self.logger.warning("Webhook URL 未配置")
            return {"success": False, "error": "No webhook URL"}
        
        try:
            # 生成签名
            timestamp = str(round(time.time() * 1000))
            secret_enc = self.secret.encode('utf-8') if self.secret else None
            
            if secret_enc:
                string_to_sign = f'{timestamp}\n{self.secret}'
                string_to_sign_enc = string_to_sign.encode('utf-8')
                hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
                sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
                
                webhook = f"{self.webhook_url}&timestamp={timestamp}&sign={sign}"
            else:
                webhook = self.webhook_url
            
            # 构建请求
            if msg_type == "markdown":
                payload = {
                    "msgtype": "markdown",
                    "markdown": {
                        "title": "FTSeek",
                        "text": message
                    }
                }
            else:
                payload = {
                    "msgtype": "text",
                    "text": {
                        "content": message
                    }
                }
            
            # 发送
            response = requests.post(webhook, json=payload, timeout=10)
            result = response.json()
            
            if result.get("errcode") == 0:
                self.logger.info(f"推送成功：{message[:30]}...")
                return {"success": True, "result": result}
            else:
                self.logger.error(f"推送失败：{result}")
                return {"success": False, "error": result}
                
        except Exception as e:
            self.logger.error(f"发送异常：{e}")
            return {"success": False, "error": str(e)}
    
    def send_premarket_watchlist(self, watchlist: List[WatchlistItem]) -> Dict[str, Any]:
        """发送盘前观察列表"""
        message = self.format_premarket_watchlist(watchlist)
        return self.send(message, msg_type="text")
    
    def send_trade_signal(self, signal: SignalPush) -> Dict[str, Any]:
        """发送交易信号"""
        message = self.format_trade_signal(signal)
        return self.send(message, msg_type="text")
    
    def send_risk_alert(self, alert: AlertPush) -> Dict[str, Any]:
        """发送风险警报"""
        message = self.format_risk_alert(alert)
        return self.send(message, msg_type="text")
    
    def send_daily_report(self, report: DailyReport) -> Dict[str, Any]:
        """发送日报"""
        message = self.format_daily_report(report)
        return self.send(message, msg_type="text")
    
    def get_status(self) -> dict:
        """获取通知器状态"""
        return {
            "webhook_configured": self.webhook_url is not None,
            "secret_configured": self.secret is not None,
            "max_length": self.max_length,
            "enable_push": self.enable_push,
        }


# 全局实例 (可选)
dingtalk_notifier_v4: Optional[DingTalkNotifierV4] = None


def get_dingtalk_notifier_v4() -> DingTalkNotifierV4:
    """获取 DingTalkNotifierV4 单例"""
    global dingtalk_notifier_v4
    if dingtalk_notifier_v4 is None:
        dingtalk_notifier_v4 = DingTalkNotifierV4()
    return dingtalk_notifier_v4
