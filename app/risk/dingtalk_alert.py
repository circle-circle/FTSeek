"""
钉钉风险警报通知

将风险警报实时推送到钉钉群

2026-04-13 修复: 统一推送入口，使用全局 notifier 共享 webhook/限流。
"""
import logging
import requests
import hmac
import hashlib
import base64
import urllib.parse
import time
from typing import Optional, List
from dataclasses import dataclass

from app.risk.realtime_monitor import RiskAlert
from app import config

logger = logging.getLogger("ftseek.risk.dingtalk_alert")
logger.propagate = False  # 防止日志重复


@dataclass
class DingTalkAlertConfig:
    """钉钉警报配置"""
    webhook_url: str
    secret: Optional[str] = None
    at_all: bool = False
    at_users: List[str] = None


class DingTalkAlertSender:
    """
    钉钉警报发送器
    
    支持：
    - 文本消息
    - Markdown 消息
    - ActionCard 消息
    - @指定用户
    """

    def __init__(self, config: DingTalkAlertConfig):
        """
        初始化发送器
        
        Args:
            config: 钉钉配置
        """
        self.webhook_url = config.webhook_url
        self.secret = config.secret
        self.at_all = config.at_all
        self.at_users = config.at_users or []

        logger.info(f"钉钉警报发送器已初始化 | webhook={self.webhook_url[:50]}...")

    def _sign_url(self) -> str:
        """生成加签 URL"""
        if not self.secret:
            return self.webhook_url
        
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{self.secret}"
        hmac_code = hmac.new(
            self.secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        
        return f"{self.webhook_url}&timestamp={timestamp}&sign={sign}"

    def _get_at_section(self) -> dict:
        """生成@用户配置"""
        at = {"isAtAll": self.at_all}
        if self.at_users:
            at["at"] = {"atUserIds": self.at_users}
        return at

    def send_text(self, content: str, alert_level: str = "INFO") -> bool:
        """
        发送文本消息
        
        Args:
            content: 消息内容
            alert_level: 警报级别
        """
        # FTSeek 适配: 集成通知降噪 (2026-05-17)
        from app.notifier.notification_noise import check_and_log
        severity = "critical" if alert_level in ("CRITICAL", "ERROR") else "info"
        
        if not check_and_log(content, route_type="dingtalk", severity=severity):
            logger.debug(f"[钉钉通知] 降噪拦截: {alert_level} - {content[:30]}...")
            return True  # 返回 True 避免调用方误判为失败
            
        emoji = self._get_level_emoji(alert_level)
        full_content = f"{emoji} {content}"
        
        payload = {
            "msgtype": "text",
            "text": {"content": full_content},
            "at": {"isAtAll": self.at_all},
        }
        
        return self._send(payload)

    def send_markdown(self, title: str, text: str, alert_level: str = "INFO") -> bool:
        """
        发送 Markdown 消息
        
        Args:
            title: 消息标题
            text: Markdown 内容
            alert_level: 警报级别
        """
        # FTSeek 适配: 集成通知降噪 (2026-05-17)
        from app.notifier.notification_noise import check_and_log
        severity = "critical" if alert_level in ("CRITICAL", "ERROR") else "info"
        
        if not check_and_log(text, route_type="dingtalk_md", severity=severity):
            logger.debug(f"[钉钉通知] Markdown 降噪拦截: {title}")
            return True
            
        emoji = self._get_level_emoji(alert_level)
        full_title = f"{emoji} {title}"
        
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": full_title,
                "text": text,
            },
            "at": {"isAtAll": self.at_all},
        }
        
        return self._send(payload)

    def send_action_card(
        self,
        title: str,
        text: str,
        btns: List[dict],
        alert_level: str = "INFO",
    ) -> bool:
        """
        发送 ActionCard 消息（带按钮）
        
        Args:
            title: 标题
            text: 内容
            btns: 按钮列表 [{"title": "xxx", "actionURL": "xxx"}]
            alert_level: 警报级别
        """
        emoji = self._get_level_emoji(alert_level)
        full_title = f"{emoji} {title}"
        
        payload = {
            "msgtype": "actionCard",
            "actionCard": {
                "title": full_title,
                "text": text,
                "btnOrientation": "1",  # 按钮竖直排列
                "btns": btns,
            },
            "at": {"isAtAll": self.at_all},
        }
        
        return self._send(payload)

    def send_alert(self, alert: RiskAlert) -> bool:
        """
        发送风险警报
        
        Args:
            alert: 风险警报对象
        """
        # 根据警报级别选择消息格式
        if alert.level == "CRITICAL":
            # 严重警报：发送 ActionCard，带快速操作按钮
            return self._send_critical_alert(alert)
        elif alert.level == "WARNING":
            # 警告：发送 Markdown
            return self._send_warning_alert(alert)
        else:
            # 信息：发送文本
            return self._send_info_alert(alert)

    def _send_critical_alert(self, alert: RiskAlert) -> bool:
        """发送严重警报"""
        title = "🚨 严重风险警报"
        
        text = f"""## {alert.type}

**标的**: {alert.code or 'N/A'}

**内容**: {alert.message}

**当前值**: {alert.value:.2%} if alert.value else 'N/A'

**阈值**: {alert.threshold:.2%} if alert.threshold else 'N/A'

**时间**: {alert.timestamp_str}

---
⚠️ **请立即处理！**"""

        btns = [
            {
                "title": "查看风险仪表盘",
                "actionURL": f"{config.CALLBACK_EXTERNAL_URL}/risk/dashboard",
            },
            {
                "title": "查看持仓详情",
                "actionURL": f"{config.CALLBACK_EXTERNAL_URL}/risk/positions",
            },
        ]
        
        return self.send_action_card(title, text, btns, alert.level)

    def _send_warning_alert(self, alert: RiskAlert) -> bool:
        """发送警告警报"""
        title = "⚠️ 风险预警"
        
        timestamp_str = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(alert.timestamp),
        )
        
        text = f"""## {alert.type}

| 项目 | 值 |
|------|------|
| **标的** | {alert.code or 'N/A'} |
| **内容** | {alert.message} |
| **当前值** | {alert.value:.2%} if alert.value else 'N/A' |
| **阈值** | {alert.threshold:.2%} if alert.threshold else 'N/A' |
| **时间** | {timestamp_str} |

---
⚠️ 请密切关注"""

        return self.send_markdown(title, text, alert.level)

    def _send_info_alert(self, alert: RiskAlert) -> bool:
        """发送信息警报"""
        content = f"[{alert.type}] {alert.message}"
        return self.send_text(content, alert.level)

    def send_risk_summary(self, dashboard: dict) -> bool:
        """
        发送风险摘要（定时推送）
        
        Args:
            dashboard: 风险仪表盘数据
        """
        title = "📊 风险日报"
        
        risk_level_emoji = {
            "LOW": "🟢",
            "MEDIUM": "🟡",
            "HIGH": "🟠",
            "CRITICAL": "🔴",
        }
        
        emoji = risk_level_emoji.get(dashboard.get("risk_level", "LOW"), "🟢")
        
        text = f"""## {emoji} 风险状态摘要

**时间**: {dashboard.get('timestamp', 'N/A')}

### 风险评分
**{dashboard.get('risk_score', 0):.1f} / 100** - {dashboard.get('risk_level', 'LOW')}

### 核心指标
| 指标 | 值 |
|------|------|
| 总资金 | ¥{dashboard.get('metrics', {}).get('total_capital', 0):,.0f} |
| 总仓位 | {dashboard.get('metrics', {}).get('total_exposure', '0%')} |
| 当前回撤 | {dashboard.get('metrics', {}).get('current_drawdown', '0%')} |
| 持仓数 | {dashboard.get('summary', {}).get('total_positions', 0)} |
| 高风险持仓 | {dashboard.get('summary', {}).get('high_risk_positions', 0)} |

### 持仓风险
"""
        
        positions = dashboard.get("positions", {})
        if positions:
            for code, risk in positions.items():
                pnl = risk.get("unrealized_pnl_percent", "0%")
                stop_dist = risk.get("stop_loss_distance", "0%")
                risk_lvl = risk.get("risk_level", "LOW")
                
                risk_emoji = "🟢" if risk_lvl == "LOW" else "🟡" if risk_lvl == "MEDIUM" else "🔴"
                
                text += f"\n{risk_emoji} **{code}**: 盈亏{pnl} | 止损距离{stop_dist} | 风险{risk_lvl}"
        else:
            text += "\n无持仓"
        
        # 最近警报
        recent_alerts = dashboard.get("recent_alerts", [])
        if recent_alerts:
            text += "\n\n### 最近警报\n"
            for alert in recent_alerts[-3:]:
                text += f"\n- [{alert['level']}] {alert['message']}"
        
        text += "\n\n---\n✅ 系统运行正常"
        
        return self.send_markdown(title, text, "INFO")

    def _send(self, payload: dict) -> bool:
        """发送消息到钉钉 - 2026-04-13: 使用全局 notifier 统一限流"""
        # 优先使用全局 notifier
        try:
            from app.notifier import get_dingtalk_notifier
            global_notifier = get_dingtalk_notifier()
            if global_notifier.webhook_url:
                return global_notifier._do_post(payload)
        except Exception as e:
            logger.warning(f"获取全局 notifier 失败，回退到直接发送: {e}")
        
        # 后备: 直接 POST
        try:
            url = self._sign_url()
            response = requests.post(url, json=payload, timeout=10)
            result = response.json()
            
            if result.get("errcode") == 0:
                logger.info(f"钉钉消息发送成功")
                return True
            else:
                logger.error(f"钉钉消息发送失败：{result}")
                return False
        except Exception as e:
            logger.error(f"钉钉消息发送异常：{e}", exc_info=True)
            return False

    def _get_level_emoji(self, level: str) -> str:
        """获取警报级别对应的 emoji"""
        emoji_map = {
            "CRITICAL": "🚨",
            "WARNING": "⚠️",
            "INFO": "ℹ️",
        }
        return emoji_map.get(level, "ℹ️")


# 全局单例
dingtalk_alert_sender = None


def init_dingtalk_alert(
    webhook_url: str = None,
    secret: str = None,
    at_all: bool = False,
) -> DingTalkAlertSender:
    """初始化钉钉警报发送器"""
    global dingtalk_alert_sender
    
    url = webhook_url or config.DINGTALK_WEBHOOK_URL
    sec = secret or config.DINGTALK_SECRET
    
    dingtalk_alert_sender = DingTalkAlertSender(
        DingTalkAlertConfig(
            webhook_url=url,
            secret=sec,
            at_all=at_all,
        )
    )
    
    return dingtalk_alert_sender
