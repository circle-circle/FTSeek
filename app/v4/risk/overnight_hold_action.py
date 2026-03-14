"""
OvernightHoldAction - 钉钉交互式确认卡片

功能:
- 生成钉钉交互式卡片
- 支持按钮点击确认
- 10 分钟过期自动失效
"""
import time
import hashlib
import logging
from typing import Dict, Optional, Any
from datetime import datetime

logger = logging.getLogger("ftseek.v4.overnight_hold_action")


class OvernightHoldCard:
    """隔夜持仓确认卡片"""
    
    def __init__(self):
        self.logger = logging.getLogger("ftseek.v4.overnight_hold_card")
    
    def generate_action_card(
        self,
        request: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        生成钉钉交互式卡片
        
        Args:
            request: 持仓请求数据
            
        Returns:
            钉钉卡片配置
        """
        code = request.get("code", "")
        current_price = request.get("current_price", 0)
        entry_price = request.get("entry_price", 0)
        unrealized_pnl_pct = request.get("unrealized_pnl_pct", 0)
        reasons = request.get("reasons", [])
        expire_time = request.get("expire_time", time.time() + 600)
        
        # 格式化保留理由
        reasons_text = "\\n".join([f"• {r}" for r in reasons])
        
        # 过期时间字符串
        expire_str = datetime.fromtimestamp(expire_time).strftime("%H:%M:%S")
        
        # 生成卡片 ID (用于回调识别)
        card_id = f"overnight_{code}_{int(time.time())}"
        
        card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": "⚠️ 隔夜持仓确认"
                    },
                    "template": "orange"
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**持仓信息**\\n股票：{code}\\n当前价：{current_price:.2f} 港元\\n入场价：{entry_price:.2f} 港元\\n未实现盈利：+{unrealized_pnl_pct:.1%}"
                        }
                    },
                    {
                        "tag": "divider"
                    },
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**保留理由**\\n{reasons_text}"
                        }
                    },
                    {
                        "tag": "divider"
                    },
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"⏰ 请在 10 分钟内选择：\\n• **保留** → 继续持仓过夜\\n• **平仓** → 立即平仓\\n• 无操作 → {expire_str} 自动平仓"
                        }
                    },
                    {
                        "tag": "action",
                        "actions": [
                            {
                                "tag": "button",
                                "text": {
                                    "tag": "plain_text",
                                    "content": "✅ 保留持仓"
                                },
                                "type": "primary",
                                "value": {
                                    "action": "overnight_hold",
                                    "code": code,
                                    "decision": "hold",
                                    "card_id": card_id
                                }
                            },
                            {
                                "tag": "button",
                                "text": {
                                    "tag": "plain_text",
                                    "content": "❌ 立即平仓"
                                },
                                "type": "danger",
                                "value": {
                                    "action": "overnight_hold",
                                    "code": code,
                                    "decision": "close",
                                    "card_id": card_id
                                }
                            }
                        ]
                    }
                ]
            }
        }
        
        self.logger.info(f"生成隔夜持仓卡片：{code}, 卡片 ID: {card_id}")
        return card
    
    def generate_text_backup(
        self,
        request: Dict[str, Any]
    ) -> str:
        """
        生成文本格式备份 (如果交互式卡片不可用)
        
        Args:
            request: 持仓请求数据
            
        Returns:
            文本消息
        """
        code = request.get("code", "")
        current_price = request.get("current_price", 0)
        entry_price = request.get("entry_price", 0)
        unrealized_pnl_pct = request.get("unrealized_pnl_pct", 0)
        reasons = request.get("reasons", [])
        expire_time = request.get("expire_time", time.time() + 600)
        
        reasons_text = "\n".join([f"• {r}" for r in reasons])
        expire_str = datetime.fromtimestamp(expire_time).strftime("%H:%M:%S")
        
        text = f"""
━━━━━━━━━━━━━━━━━━━━━━
⚠️ **隔夜持仓确认**
━━━━━━━━━━━━━━━━━━━━━━

📊 **持仓信息**
股票：{code}
当前价：{current_price:.2f} 港元
入场价：{entry_price:.2f} 港元
未实现盈利：+{unrealized_pnl_pct:.1%}

━━━━━━━━━━━━━━━━━━━━━━

✅ **保留理由**

{reasons_text}

━━━━━━━━━━━━━━━━━━━━━━

⏰ **请在 10 分钟内回复**

回复 **"保留"** → 继续持仓过夜
回复 **"平仓"** → 立即平仓
无回复 → {expire_str} 自动平仓

━━━━━━━━━━━━━━━━━━━━━━

🕐 推送时间：{datetime.now().strftime("%H:%M:%S")}
🕐 过期时间：{expire_str}
━━━━━━━━━━━━━━━━━━━━━━
"""
        return text
    
    def parse_callback(
        self,
        callback_data: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        解析钉钉回调数据
        
        Args:
            callback_data: 钉钉回调数据
            
        Returns:
            解析后的数据 {code, decision, card_id}
        """
        try:
            value = callback_data.get("value", {})
            
            if value.get("action") != "overnight_hold":
                return None
            
            result = {
                "code": value.get("code"),
                "decision": value.get("decision"),  # "hold" or "close"
                "card_id": value.get("card_id"),
                "user_id": callback_data.get("user_id"),
                "timestamp": callback_data.get("timestamp"),
            }
            
            self.logger.info(f"解析回调：{result}")
            return result
            
        except Exception as e:
            self.logger.error(f"解析回调失败：{e}")
            return None


# 全局实例
overnight_hold_card: Optional[OvernightHoldCard] = None


def get_overnight_hold_card() -> OvernightHoldCard:
    """获取 OvernightHoldCard 单例"""
    global overnight_hold_card
    if overnight_hold_card is None:
        overnight_hold_card = OvernightHoldCard()
    return overnight_hold_card
