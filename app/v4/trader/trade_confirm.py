"""
TradeConfirm - 交易确认推送模块

功能:
- 生成钉钉交互式确认卡片
- 接收用户确认/忽略决策
- 确认后发送到 OpenD 执行
- 5 分钟过期自动作废
"""
import time
import logging
from typing import Dict, Optional, Any, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

logger = logging.getLogger("ftseek.v4.trade_confirm")
logger.propagate = False  # 防止日志重复


@dataclass
class TradeConfirmRequest:
    """交易确认请求"""
    code: str
    name: str
    action: str  # BUY or SELL
    trigger_price: float
    quantity: int
    strategy: str
    reason: str
    stop_loss: Optional[float] = None
    target_price: Optional[float] = None
    card_id: str = ""
    push_time: float = field(default_factory=time.time)
    expire_time: float = 0.0
    confirmed: bool = False
    user_decision: Optional[str] = None  # "confirm" or "ignore"
    executed: bool = False
    
    def __post_init__(self):
        # 默认 5 分钟过期
        self.expire_time = self.push_time + 300  # 300 秒 = 5 分钟
        if not self.card_id:
            self.card_id = f"trade_{self.code}_{int(self.push_time)}"
    
    @property
    def time_remaining(self) -> float:
        """剩余时间 (秒)"""
        return max(0, self.expire_time - time.time())
    
    @property
    def is_expired(self) -> bool:
        """是否过期"""
        return time.time() >= self.expire_time
    
    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "action": self.action,
            "trigger_price": self.trigger_price,
            "quantity": self.quantity,
            "strategy": self.strategy,
            "reason": self.reason,
            "stop_loss": self.stop_loss,
            "target_price": self.target_price,
            "card_id": self.card_id,
            "push_time": datetime.fromtimestamp(self.push_time).strftime("%H:%M:%S"),
            "expire_time": datetime.fromtimestamp(self.expire_time).strftime("%H:%M:%S"),
            "time_remaining": f"{self.time_remaining:.0f}秒",
            "confirmed": self.confirmed,
            "user_decision": self.user_decision,
            "executed": self.executed,
        }


class TradeConfirmCard:
    """交易确认卡片生成器"""
    
    def __init__(self):
        self.logger = logging.getLogger("ftseek.v4.trade_confirm_card")
        self.logger.propagate = False  # 防止日志重复

    def generate_action_card(
        self,
        request: TradeConfirmRequest
    ) -> Dict[str, Any]:
        """
        生成钉钉交互式卡片
        
        Args:
            request: 交易确认请求
            
        Returns:
            钉钉卡片配置
        """
        action_text = "买入" if request.action == "BUY" else "卖出"
        action_icon = "🟢" if request.action == "BUY" else "🔴"
        action_color = "primary" if request.action == "BUY" else "danger"
        
        # 格式化止损和目标价
        risk_info = ""
        if request.stop_loss:
            risk_info += f"\n止损价：{request.stop_loss:.2f} 港元"
        if request.target_price:
            risk_info += f"\n目标价：{request.target_price:.2f} 港元"
        
        card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"{action_icon} {request.code} {request.action} 信号"
                    },
                    "template": "red" if request.action == "SELL" else "green"
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**标的**: {request.code} {request.name}\\n**方向**: {request.action}\\n**触发价**: HK${request.trigger_price:.2f}\\n**数量**: {request.quantity} 股\\n**策略**: {request.strategy}\\n**原因**: {request.reason}{risk_info}"
                        }
                    },
                    {
                        "tag": "divider"
                    },
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"⏰ 有效期 5 分钟，过期自动作废\\n🕐 推送时间：{datetime.fromtimestamp(request.push_time).strftime('%H:%M:%S')}\\n🕐 过期时间：{datetime.fromtimestamp(request.expire_time).strftime('%H:%M:%S')}"
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
                                    "code": request.code,
                                    "decision": "confirm",
                                    "card_id": request.card_id
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
                                    "code": request.code,
                                    "decision": "ignore",
                                    "card_id": request.card_id
                                }
                            }
                        ]
                    }
                ]
            }
        }
        
        self.logger.info(f"生成交易确认卡片：{request.code}, 卡片 ID: {request.card_id}")
        return card
    
    def generate_text_backup(
        self,
        request: TradeConfirmRequest
    ) -> str:
        """
        生成文本格式备份
        
        Args:
            request: 交易确认请求
            
        Returns:
            文本消息
        """
        action_text = "买入" if request.action == "BUY" else "卖出"
        action_icon = "🟢" if request.action == "BUY" else "🔴"
        
        risk_info = ""
        if request.stop_loss:
            risk_info += f"\n止损价：{request.stop_loss:.2f} 港元"
        if request.target_price:
            risk_info += f"\n目标价：{request.target_price:.2f} 港元"
        
        text = f"""
━━━━━━━━━━━━━━━━━━━━━━
{action_icon} **{request.code} {request.action} 信号**
━━━━━━━━━━━━━━━━━━━━━━

**标的**: {request.code} {request.name}
**方向**: {request.action}
**触发价**: HK${request.trigger_price:.2f}
**数量**: {request.quantity} 股
**策略**: {request.strategy}
**原因**: {request.reason}
{risk_info}
━━━━━━━━━━━━━━━━━━━━━━

⏰ 有效期 5 分钟，过期自动作废

请在钉钉回复:
• **"确认"** → 执行{action_text}
• **"忽略"** → 取消信号
• 无回复 → 自动作废

🕐 推送时间：{datetime.fromtimestamp(request.push_time).strftime("%H:%M:%S")}
🕐 过期时间：{datetime.fromtimestamp(request.expire_time).strftime("%H:%M:%S")}
━━━━━━━━━━━━━━━━━━━━━━
"""
        return text


class TradeConfirmManager:
    """交易确认管理器"""
    
    def __init__(self):
        self.pending_requests: Dict[str, TradeConfirmRequest] = {}
        self.card_generator = TradeConfirmCard()
        self.execute_callback: Optional[Callable[[TradeConfirmRequest], bool]] = None
        self.logger = logging.getLogger("ftseek.v4.trade_confirm_manager")
        self.logger.propagate = False  # 防止日志重复

    def set_execute_callback(
        self,
        callback: Callable[[TradeConfirmRequest], bool]
    ):
        """
        设置执行回调函数
        
        Args:
            callback: 执行函数，返回是否成功
        """
        self.execute_callback = callback
        self.logger.info("执行回调已设置")
    
    def create_request(
        self,
        code: str,
        name: str,
        action: str,
        trigger_price: float,
        quantity: int,
        strategy: str,
        reason: str,
        stop_loss: Optional[float] = None,
        target_price: Optional[float] = None
    ) -> TradeConfirmRequest:
        """
        创建交易确认请求
        
        Args:
            code: 股票代码
            name: 股票名称
            action: 买卖方向
            trigger_price: 触发价
            quantity: 数量
            strategy: 策略
            reason: 原因
            stop_loss: 止损价
            target_price: 目标价
            
        Returns:
            交易确认请求
        """
        request = TradeConfirmRequest(
            code=code,
            name=name,
            action=action,
            trigger_price=trigger_price,
            quantity=quantity,
            strategy=strategy,
            reason=reason,
            stop_loss=stop_loss,
            target_price=target_price
        )
        
        self.pending_requests[request.card_id] = request
        
        self.logger.info(
            f"创建交易确认请求：{request.code} {request.action}, "
            f"卡片 ID: {request.card_id}"
        )
        
        return request
    
    def process_decision(
        self,
        card_id: str,
        decision: str
    ) -> Optional[TradeConfirmRequest]:
        """
        处理用户决策
        
        Args:
            card_id: 卡片 ID
            decision: 用户决策 ("confirm" or "ignore")
            
        Returns:
            更新后的请求
        """
        if card_id not in self.pending_requests:
            self.logger.warning(f"未找到请求：{card_id}")
            return None
        
        request = self.pending_requests[card_id]
        
        if request.is_expired:
            self.logger.warning(f"请求已过期：{card_id}")
            return None
        
        request.user_decision = decision
        request.confirmed = True
        
        self.logger.info(
            f"收到交易决策：{request.code} - {decision}"
        )
        
        # 如果确认执行
        if decision == "confirm" and self.execute_callback:
            try:
                success = self.execute_callback(request)
                request.executed = success
                self.logger.info(f"执行{'成功' if success else '失败'}: {request.code}")
            except Exception as e:
                self.logger.error(f"执行失败：{e}")
                request.executed = False
        
        return request
    
    def check_expired(self) -> list:
        """检查过期请求"""
        expired = []
        
        for card_id, request in list(self.pending_requests.items()):
            if request.is_expired and not request.confirmed:
                request.user_decision = "auto_expire"
                expired.append(request)
                self.logger.info(f"请求过期：{request.code}")
        
        return expired
    
    def cleanup(self, max_age: int = 3600):
        """清理过期请求"""
        current_time = time.time()
        
        expired = [
            card_id for card_id, req in self.pending_requests.items()
            if current_time - req.push_time > max_age
        ]
        
        for card_id in expired:
            del self.pending_requests[card_id]
        
        if expired:
            self.logger.info(f"清理{len(expired)}个过期请求")
    
    def get_status(self) -> dict:
        """获取状态"""
        return {
            "pending_count": len(self.pending_requests),
            "pending_codes": list(self.pending_requests.keys()),
            "expired_count": sum(1 for r in self.pending_requests.values() if r.is_expired),
        }


# 全局实例
trade_confirm_manager: Optional[TradeConfirmManager] = None


def get_trade_confirm_manager() -> TradeConfirmManager:
    """获取 TradeConfirmManager 单例"""
    global trade_confirm_manager
    if trade_confirm_manager is None:
        trade_confirm_manager = TradeConfirmManager()
    return trade_confirm_manager
