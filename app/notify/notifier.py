"""
通知发送模块
负责发送各种通知消息到钉钉
"""
import logging
import time
import threading
from typing import Dict, List, Optional
from datetime import datetime

from app.notify.template import MessageTemplates, format_value, format_icon
from app.notify.alert import get_alert_manager, AlertType, AlertLevel

logger = logging.getLogger("ftseek.notify.notifier")
logger.propagate = False


class DingTalkNotifier:
    """钉钉通知器"""
    
    def __init__(self, webhook_url: str = None, secret: str = None):
        """
        初始化钉钉通知器
        
        Args:
            webhook_url: 钉钉机器人 webhook URL
            secret: 钉钉机器人签名密钥
        """
        import os
        self.webhook_url = webhook_url or os.getenv("DINGTALK_WEBHOOK_URL", "")
        self.secret = secret or os.getenv("DINGTALK_SECRET", "")
        
        self._enabled = True
        self._send_count = 0
        self._error_count = 0
        self._last_send_time = 0
        self._dropped_count = 0
        
        # 告警管理器
        self.alert_manager = get_alert_manager()
        
        # 刷新线程
        self._flush_thread = None
        self._running = False
    
    def _sign_url(self) -> str:
        """签名 URL"""
        import hmac
        import hashlib
        import base64
        import urllib.parse
        
        if not self.secret:
            return self.webhook_url
        
        timestamp = str(round(time.time() * 1000))
        secret_enc = self.secret.encode('utf-8')
        string_to_sign = f'{timestamp}\n{self.secret}'
        string_to_sign_enc = string_to_sign.encode('utf-8')
        hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        
        return f'{self.webhook_url}&timestamp={timestamp}&sign={sign}'
    
    def _can_send(self) -> bool:
        """检查是否可以发送"""
        if not self._enabled:
            return False
        if not self.webhook_url:
            return False
        
        # 限流：每秒最多 1 条
        now = time.time()
        if now - self._last_send_time < 1.0:
            return False
        
        return True
    
    def _record_send(self):
        """记录发送"""
        self._send_count += 1
        self._last_send_time = time.time()
    
    def _do_post(self, payload: dict) -> bool:
        """执行 POST 请求"""
        import requests
        
        try:
            url = self._sign_url()
            resp = requests.post(url, json=payload, timeout=5)
            
            if resp.status_code == 200:
                result = resp.json()
                if result.get('errcode') == 0:
                    self._record_send()
                    return True
                else:
                    logger.warning(f"钉钉发送失败：{result}")
                    self._error_count += 1
                    return False
            else:
                logger.warning(f"钉钉请求失败：{resp.status_code}")
                self._error_count += 1
                return False
        
        except Exception as e:
            logger.warning(f"钉钉发送异常：{e}")
            self._error_count += 1
            return False
    
    def send_markdown(self, text: str) -> bool:
        """
        发送 Markdown 消息
        
        Args:
            text: Markdown 格式文本
        
        Returns:
            True: 发送成功
            False: 发送失败
        """
        if not self._can_send():
            self._dropped_count += 1
            return False
        
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": "FTSeek 通知",
                "text": text
            },
            "at": {
                "isAtAll": True
            }
        }
        
        return self._do_post(payload)
    
    def send_text(self, content: str, at_all: bool = False) -> bool:
        """
        发送文本消息
        
        Args:
            content: 文本内容
            at_all: 是否@所有人
        
        Returns:
            True: 发送成功
            False: 发送失败
        """
        if not self._can_send():
            self._dropped_count += 1
            return False
        
        payload = {
            "msgtype": "text",
            "text": {
                "content": content
            },
            "at": {
                "isAtAll": at_all
            }
        }
        
        return self._do_post(payload)
    
    def send_open_position_message(self, signal, trade_result, position, consensus_result) -> bool:
        """
        发送开仓通知
        
        Args:
            signal: 信号对象
            trade_result: 交易结果
            position: 持仓对象
            consensus_result: 共识结果
        
        Returns:
            True: 发送成功
            False: 发送失败
        """
        try:
            content = MessageTemplates.format_open_position_content(
                signal, trade_result, position, consensus_result
            )
            return self.send_markdown(content)
        except Exception as e:
            logger.error(f"发送开仓通知失败：{e}")
            return False
    
    def send_close_position_message(self, signal, trade_result, position, consensus_result) -> bool:
        """
        发送平仓通知
        
        Args:
            signal: 信号对象
            trade_result: 交易结果
            position: 持仓对象
            consensus_result: 共识结果
        
        Returns:
            True: 发送成功
            False: 发送失败
        """
        try:
            content = MessageTemplates.format_close_position_content(
                signal, trade_result, position, consensus_result
            )
            return self.send_markdown(content)
        except Exception as e:
            logger.error(f"发送平仓通知失败：{e}")
            return False
    
    def send_risk_alert(self, alert_type: str, details: dict) -> bool:
        """
        发送风控告警
        
        Args:
            alert_type: 告警类型
            details: 详细信息
        
        Returns:
            True: 发送成功
            False: 发送失败
        """
        try:
            # 映射告警类型
            alert_type_map = {
                'stop_loss': AlertType.STOP_LOSS,
                'take_profit': AlertType.TAKE_PROFIT,
                'circuit_breaker': AlertType.CIRCUIT_BREAKER,
                'position_limit': AlertType.POSITION_LIMIT,
                'drawdown': AlertType.DRAWDOWN_LIMIT,
                'market_regime': AlertType.MARKET_REGIME,
            }
            
            alert_type_enum = alert_type_map.get(alert_type, AlertType.SYSTEM_ERROR)
            
            # 检查告警
            value = details.get('value', 0)
            message = self.alert_manager.check_alert(alert_type_enum, value, details)
            
            if message:
                return self.send_text(f"🚨 风控告警\n\n{message}")
            
            return True
        
        except Exception as e:
            logger.error(f"发送风控告警失败：{e}")
            return False
    
    def send_daily_stats(self, stats: dict, review: dict = None) -> bool:
        """
        发送每日统计
        
        Args:
            stats: 统计数据
            review: 复盘数据
        
        Returns:
            True: 发送成功
            False: 发送失败
        """
        try:
            lines = [
                "## 📊 FTSeek 每日统计",
                "",
                f"日期：{datetime.now().strftime('%Y-%m-%d')}",
                "",
                f"信号数量：{stats.get('signal_count', 0)}",
                f"交易次数：{stats.get('trade_count', 0)}",
                f"胜率：{format_value(stats.get('win_rate'))}",
                f"盈亏比：{format_value(stats.get('profit_loss_ratio'), '.2f', suffix=':1')}",
                f"总盈亏：{format_value(stats.get('total_pnl'), '.2f', suffix='元')}",
                "",
                "### 策略表现",
            ]
            
            strategy_stats = stats.get('strategy_stats', {})
            for strategy_name, strategy_data in strategy_stats.items():
                win_rate = format_value(strategy_data.get('win_rate'))
                pnl = format_value(strategy_data.get('pnl'), '.2f', suffix='元')
                lines.append(f"- {strategy_name}: 胜率{win_rate} 盈亏{pnl}")
            
            if review:
                lines.extend(["", "### 复盘总结", review.get('summary', '')])
            
            content = "\n".join(lines)
            return self.send_markdown(content)
        
        except Exception as e:
            logger.error(f"发送每日统计失败：{e}")
            return False
    
    def get_stats(self) -> dict:
        """获取发送统计"""
        return {
            'send_count': self._send_count,
            'error_count': self._error_count,
            'dropped_count': self._dropped_count,
            'last_send_time': self._last_send_time,
            'enabled': self._enabled,
        }
    
    def enable(self):
        """启用通知"""
        self._enabled = True
        logger.info("钉钉通知已启用")
    
    def disable(self):
        """禁用通知"""
        self._enabled = False
        logger.info("钉钉通知已禁用")


# 工厂函数
def create_notifier(webhook_url: str = None, secret: str = None) -> DingTalkNotifier:
    """
    创建钉钉通知器实例
    
    Args:
        webhook_url: 钉钉机器人 webhook URL
        secret: 钉钉机器人签名密钥
    
    Returns:
        DingTalkNotifier 实例
    """
    return DingTalkNotifier(webhook_url, secret)
