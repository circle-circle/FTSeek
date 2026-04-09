"""
TradeConfirmSimple - 简化版交易确认推送

使用现有钉钉 webhook 进行推送
通过轮询或关键词回复接收确认
"""
import time
import logging
from typing import Dict, Optional, Any, Callable
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("ftseek.v4.trade_confirm_simple")
logger.propagate = False  # 防止日志重复


@dataclass
class TradeConfirmRequest:
    """交易确认请求"""
    code: str
    name: str
    action: str
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
    user_decision: Optional[str] = None
    executed: bool = False
    
    def __post_init__(self):
        self.expire_time = self.push_time + 300  # 5 分钟
        if not self.card_id:
            self.card_id = f"trade_{self.code}_{int(self.push_time)}"
    
    @property
    def time_remaining(self) -> float:
        return max(0, self.expire_time - time.time())
    
    @property
    def is_expired(self) -> bool:
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
        }


class TradeConfirmManager:
    """交易确认管理器"""
    
    def __init__(self, notifier):
        self.notifier = notifier
        self.pending_requests: Dict[str, TradeConfirmRequest] = {}
        self.execute_callback: Optional[Callable] = None
        self.logger = logging.getLogger("ftseek.v4.trade_confirm_manager")
        self.logger.propagate = False  # 防止日志重复

    def set_execute_callback(self, callback: Callable):
        """设置执行回调"""
        self.execute_callback = callback
    
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
        """创建交易确认请求"""
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
        self.logger.info(f"创建交易确认：{request.code} {request.action}")
        
        return request
    
    def send_confirm_push(self, request: TradeConfirmRequest) -> bool:
        """
        发送确认推送
        
        Args:
            request: 交易确认请求
            
        Returns:
            是否发送成功
        """
        action_text = "买入" if request.action == "BUY" else "卖出"
        action_icon = "🟢" if request.action == "BUY" else "🔴"
        
        risk_info = ""
        if request.stop_loss:
            risk_info += f"\n• 止损：{request.stop_loss:.2f} 港元"
        if request.target_price:
            risk_info += f"\n• 目标：{request.target_price:.2f} 港元"
        
        message = f"""
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

⏰ **有效期 5 分钟**

请在钉钉回复:
• **"确认{action_text}"** → 执行
• **"忽略"** → 取消
• 无回复 → 自动作废

🕐 推送：{request.push_time}
🕐 过期：{request.expire_time}
━━━━━━━━━━━━━━━━━━━━━━
"""
        
        result = self.notifier.send(message, msg_type='markdown')
        return result.get('success', False)
    
    def process_decision(self, card_id: str, decision: str) -> Optional[TradeConfirmRequest]:
        """处理用户决策"""
        if card_id not in self.pending_requests:
            return None
        
        request = self.pending_requests[card_id]
        
        if request.is_expired:
            return None
        
        request.user_decision = decision
        request.confirmed = True
        
        self.logger.info(f"收到决策：{request.code} - {decision}")
        
        # 执行交易
        if decision == "confirm" and self.execute_callback:
            try:
                success = self.execute_callback(request)
                request.executed = success
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
    
    def get_status(self) -> dict:
        return {
            "pending_count": len(self.pending_requests),
            "pending_codes": list(self.pending_requests.keys()),
        }


# 全局实例
trade_confirm_manager: Optional[TradeConfirmManager] = None


def get_trade_confirm_manager(notifier=None) -> TradeConfirmManager:
    """获取单例"""
    global trade_confirm_manager
    if trade_confirm_manager is None:
        if notifier is None:
            from app.v4.notifier.dingtalk_v4 import DingTalkNotifierV4
            import os
            notifier = DingTalkNotifierV4(
                webhook_url=os.getenv('DINGTALK_WEBHOOK_URL', ''),
                secret=os.getenv('DINGTALK_SECRET', '')
            )
        trade_confirm_manager = TradeConfirmManager(notifier)
    return trade_confirm_manager
