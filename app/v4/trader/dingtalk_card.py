"""
DingTalkCard - 钉钉交互式卡片生成器

生成带确认/忽略按钮的交易信号卡片
"""
import time
import logging
from typing import Dict, Optional, Any
from datetime import datetime

logger = logging.getLogger("ftseek.v4.dingtalk_card")
logger.propagate = False  # 防止日志重复


class TradeSignalCard:
    """交易信号卡片"""
    
    def __init__(self):
        self.logger = logging.getLogger("ftseek.v4.trade_signal_card")
logger.propagate = False  # 防止日志重复
    
    def generate_card(
        self,
        code: str,
        name: str,
        action: str,
        trigger_price: float,
        quantity: int,
        strategy: str,
        reason: str,
        stop_loss: Optional[float] = None,
        target_price: Optional[float] = None,
        expire_minutes: int = 5
    ) -> Dict[str, Any]:
        """
        生成交易信号卡片
        
        Args:
            code: 股票代码
            name: 股票名称
            action: BUY/SELL
            trigger_price: 触发价
            quantity: 数量
            strategy: 策略
            reason: 原因
            stop_loss: 止损价
            target_price: 目标价
            expire_minutes: 过期时间 (分钟)
            
        Returns:
            钉钉卡片配置
        """
        action_text = "买入" if action == "BUY" else "卖出"
        action_icon = "🟢" if action == "BUY" else "🔴"
        action_color = "primary" if action == "BUY" else "danger"
        
        # 生成卡片 ID
        card_id = f"trade_{code}_{int(time.time())}"
        expire_time = time.time() + (expire_minutes * 60)
        
        # 格式化风险信息
        risk_info = ""
        if stop_loss:
            risk_info += f"\\n• 止损：{stop_loss:.2f} 港元"
        if target_price:
            risk_info += f"\\n• 目标：{target_price:.2f} 港元"
        
        card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"{action_icon} {code} {action} 信号"
                    },
                    "template": "red" if action == "SELL" else "green"
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**标的**: {code} {name}\\n**方向**: {action}\\n**触发价**: HK${trigger_price:.2f}\\n**数量**: {quantity} 股\\n**策略**: {strategy}\\n**原因**: {reason}{risk_info}"
                        }
                    },
                    {
                        "tag": "divider"
                    },
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"⏰ 有效期{expire_minutes}分钟，过期自动作废\\n🕐 推送时间：{datetime.now().strftime('%H:%M:%S')}\\n🕐 过期时间：{datetime.fromtimestamp(expire_time).strftime('%H:%M:%S')}"
                        }
                    },
                    {
                        "tag": "action",
                        "actions": [
                            {
                                "tag": "button",
                                "text": {
                                    "tag": "plain_text",
                                    "content": f"✅ 确认{action_text}"
                                },
                                "type": action_color,
                                "value": {
                                    "action": "trade_confirm",
                                    "code": code,
                                    "decision": "confirm",
                                    "card_id": card_id
                                }
                            },
                            {
                                "tag": "button",
                                "text": {
                                    "tag": "plain_text",
                                    "content": "❌ 忽略"
                                },
                                "type": "default",
                                "value": {
                                    "action": "trade_confirm",
                                    "code": code,
                                    "decision": "ignore",
                                    "card_id": card_id
                                }
                            }
                        ]
                    }
                ]
            }
        }
        
        self.logger.info(f"生成交易信号卡片：{code}, 卡片 ID: {card_id}")
        return card
    
    def generate_text_backup(
        self,
        code: str,
        name: str,
        action: str,
        trigger_price: float,
        quantity: int,
        strategy: str,
        reason: str,
        stop_loss: Optional[float] = None,
        target_price: Optional[float] = None,
        expire_minutes: int = 5
    ) -> str:
        """
        生成文本格式备份
        
        Returns:
            文本消息
        """
        action_text = "买入" if action == "BUY" else "卖出"
        action_icon = "🟢" if action == "BUY" else "🔴"
        
        risk_info = ""
        if stop_loss:
            risk_info += f"\n• 止损：{stop_loss:.2f} 港元"
        if target_price:
            risk_info += f"\n• 目标：{target_price:.2f} 港元"
        
        expire_time = datetime.now().timestamp() + (expire_minutes * 60)
        
        text = f"""
━━━━━━━━━━━━━━━━━━━━━━
{action_icon} **{code} {action} 信号**
━━━━━━━━━━━━━━━━━━━━━━

**标的**: {code} {name}
**方向**: {action}
**触发价**: HK${trigger_price:.2f}
**数量**: {quantity} 股
**策略**: {strategy}
**原因**: {reason}
{risk_info}

━━━━━━━━━━━━━━━━━━━━━━

⏰ 有效期{expire_minutes}分钟，过期自动作废

请在钉钉回复:
• **"确认{action_text}"** → 执行
• **"忽略"** → 取消
• 无回复 → 自动作废

🕐 推送时间：{datetime.now().strftime("%H:%M:%S")}
🕐 过期时间：{datetime.fromtimestamp(expire_time).strftime("%H:%M:%S")}
━━━━━━━━━━━━━━━━━━━━━━
"""
        return text


# 全局实例
trade_signal_card: Optional[TradeSignalCard] = None


def get_trade_signal_card() -> TradeSignalCard:
    """获取单例"""
    global trade_signal_card
    if trade_signal_card is None:
        trade_signal_card = TradeSignalCard()
    return trade_signal_card
