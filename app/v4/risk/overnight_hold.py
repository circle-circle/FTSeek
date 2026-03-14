"""
OvernightHold - 隔夜持仓确认模块

功能:
- 评估是否适合隔夜持仓
- 推送人工确认请求
- 10 分钟过期自动平仓
- 记录保留理由
"""
import time
import logging
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import threading

logger = logging.getLogger("ftseek.v4.overnight_hold")


@dataclass
class HoldRequest:
    """隔夜持仓请求"""
    code: str
    current_price: float
    entry_price: float
    quantity: int
    unrealized_pnl: float
    unrealized_pnl_pct: float
    reasons: List[str]  # 保留理由
    push_time: float = field(default_factory=time.time)
    expire_time: float = 0.0
    confirmed: bool = False
    confirmed_time: Optional[float] = None
    user_response: Optional[str] = None  # "保留" or "平仓"
    
    def __post_init__(self):
        # 默认 10 分钟过期
        self.expire_time = self.push_time + 600  # 600 秒 = 10 分钟
    
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
            "current_price": self.current_price,
            "entry_price": self.entry_price,
            "unrealized_pnl_pct": f"{self.unrealized_pnl_pct:.1%}",
            "reasons": self.reasons,
            "push_time": datetime.fromtimestamp(self.push_time).strftime("%H:%M:%S"),
            "expire_time": datetime.fromtimestamp(self.expire_time).strftime("%H:%M:%S"),
            "time_remaining": f"{self.time_remaining:.0f}秒",
            "confirmed": self.confirmed,
            "user_response": self.user_response,
        }


class OvernightHoldEvaluator:
    """隔夜持仓评估器"""
    
    def __init__(self):
        self.pending_requests: Dict[str, HoldRequest] = {}
        self.logger = logging.getLogger("ftseek.v4.overnight_hold")
        
        # 评估条件阈值
        self.min_profit_pct = 0.05  # 最小盈利 5%
        self.max_position_pct = 0.30  # 最大仓位 30%
        self.min_volume_ratio = 2.0  # 最小量比 2 倍
        self.close_to_high_pct = 0.90  # 收盘价接近高点 90%
    
    def evaluate(
        self,
        code: str,
        position: Dict[str, Any],
        market_data: Dict[str, Any],
        news_sentiment: Dict[str, Any]
    ) -> Optional[HoldRequest]:
        """
        评估是否适合隔夜持仓
        
        Args:
            code: 股票代码
            position: 持仓信息 {entry_price, quantity, current_price}
            market_data: 市场数据 {high, volume_ratio, rsi, macd_golden}
            news_sentiment: 新闻情绪 {is_positive, reasons}
            
        Returns:
            HoldRequest (如果适合) 或 None
        """
        self.logger.info(f"评估隔夜持仓：{code}")
        
        # 1. 计算未实现盈亏
        entry_price = position.get("entry_price", 0)
        current_price = position.get("current_price", 0)
        quantity = position.get("quantity", 0)
        
        if entry_price <= 0 or current_price <= 0:
            return None
        
        unrealized_pnl = (current_price - entry_price) * quantity
        unrealized_pnl_pct = (current_price - entry_price) / entry_price
        
        # 2. 检查保留条件
        reasons = []
        
        # 2.1 盈利检查
        if unrealized_pnl_pct < self.min_profit_pct:
            self.logger.debug(f"{code} 盈利不足：{unrealized_pnl_pct:.1%} < {self.min_profit_pct:.1%}")
            return None
        reasons.append(f"盈利{unrealized_pnl_pct:.1%} > 5%")
        
        # 2.2 利好消息持续性
        if news_sentiment.get("is_positive"):
            news_reasons = news_sentiment.get("reasons", [])
            reasons.extend(news_reasons)
        else:
            self.logger.debug(f"{code} 无利好消息")
            return None
        
        # 2.3 涨势持续性
        high = market_data.get("high", current_price)
        if current_price >= high * self.close_to_high_pct:
            reasons.append(f"收盘价接近高点 ({current_price/high:.0%})")
        else:
            self.logger.debug(f"{code} 收盘价不够强：{current_price/high:.0%}")
            return None
        
        volume_ratio = market_data.get("volume_ratio", 1.0)
        if volume_ratio >= self.min_volume_ratio:
            reasons.append(f"成交量放大{volume_ratio:.1f}倍")
        else:
            self.logger.debug(f"{code} 量能不足：{volume_ratio:.1f}")
            return None
        
        # 技术指标
        rsi = market_data.get("rsi", 50)
        if rsi > 60:
            reasons.append(f"RSI={rsi:.0f} 强势区")
        
        if market_data.get("macd_golden"):
            reasons.append("MACD 金叉")
        
        # 2.4 仓位检查
        position_pct = position.get("position_pct", 0)
        if position_pct > self.max_position_pct:
            self.logger.debug(f"{code} 仓位过重：{position_pct:.0%}")
            return None
        
        # 3. 创建请求
        request = HoldRequest(
            code=code,
            current_price=current_price,
            entry_price=entry_price,
            quantity=quantity,
            unrealized_pnl=unrealized_pnl,
            unrealized_pnl_pct=unrealized_pnl_pct,
            reasons=reasons
        )
        
        self.pending_requests[code] = request
        
        self.logger.info(
            f"创建隔夜持仓请求：{code}, "
            f"盈利{unrealized_pnl_pct:.1%}, "
            f"理由{len(reasons)}条"
        )
        
        return request
    
    def format_push_message(self, request: HoldRequest) -> str:
        """
        格式化推送消息
        
        Args:
            request: 持仓请求
            
        Returns:
            推送消息文本
        """
        expire_time = datetime.fromtimestamp(request.expire_time).strftime("%H:%M:%S")
        
        reasons_text = "\n".join([f"• {r}" for r in request.reasons])
        
        message = f"""
━━━━━━━━━━━━━━━━━━━━━━
⚠️ **隔夜持仓确认**
━━━━━━━━━━━━━━━━━━━━━━

📊 **持仓信息**
股票：{request.code}
当前价：{request.current_price:.2f} 港元
入场价：{request.entry_price:.2f} 港元
未实现盈利：+{request.unrealized_pnl:.2f} 港元 ({request.unrealized_pnl_pct:.1%})

━━━━━━━━━━━━━━━━━━━━━━

✅ **保留理由**

{reasons_text}

━━━━━━━━━━━━━━━━━━━━━━

⏰ **请在 10 分钟内回复**

回复 **"保留"** → 继续持仓过夜
回复 **"平仓"** → 立即平仓
无回复 → {expire_time} 自动平仓

━━━━━━━━━━━━━━━━━━━━━━

🕐 推送时间：{datetime.fromtimestamp(request.push_time).strftime("%H:%M:%S")}
🕐 过期时间：{expire_time}
━━━━━━━━━━━━━━━━━━━━━━
"""
        return message
    
    def process_response(
        self,
        code: str,
        response: str
    ) -> Optional[HoldRequest]:
        """
        处理用户回复
        
        Args:
            code: 股票代码
            response: 用户回复 ("保留" or "平仓")
            
        Returns:
            更新后的请求
        """
        if code not in self.pending_requests:
            self.logger.warning(f"未找到待处理请求：{code}")
            return None
        
        request = self.pending_requests[code]
        
        if request.is_expired:
            self.logger.warning(f"请求已过期：{code}")
            return None
        
        request.user_response = response
        request.confirmed = True
        request.confirmed_time = time.time()
        
        self.logger.info(f"收到用户回复：{code} - {response}")
        
        return request
    
    def check_expired(self) -> List[HoldRequest]:
        """检查过期请求"""
        expired = []
        
        for code, request in list(self.pending_requests.items()):
            if request.is_expired and not request.confirmed:
                request.user_response = "auto_close"  # 自动平仓
                expired.append(request)
                self.logger.info(f"请求过期，自动平仓：{code}")
        
        return expired
    
    def cleanup(self):
        """清理已完成请求"""
        current_time = time.time()
        
        # 保留 1 小时内的请求
        self.pending_requests = {
            code: req for code, req in self.pending_requests.items()
            if current_time - req.push_time < 3600
        }
    
    def get_status(self) -> dict:
        """获取状态"""
        return {
            "pending_count": len(self.pending_requests),
            "pending_codes": list(self.pending_requests.keys()),
            "expired_count": sum(1 for r in self.pending_requests.values() if r.is_expired),
        }


# 全局实例
overnight_hold_evaluator: Optional[OvernightHoldEvaluator] = None


def get_overnight_hold_evaluator() -> OvernightHoldEvaluator:
    """获取 OvernightHoldEvaluator 单例"""
    global overnight_hold_evaluator
    if overnight_hold_evaluator is None:
        overnight_hold_evaluator = OvernightHoldEvaluator()
    return overnight_hold_evaluator
