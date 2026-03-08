"""
OrderRouter - 智能订单路由模块

功能:
- 订单类型选择 (市价/限价)
- 富途 API 对接
- 订单状态追踪
- 失败重试机制
"""
import time
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import uuid

logger = logging.getLogger("ftseek.v4.order_router")


class OrderSide(str, Enum):
    """订单方向"""
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """订单类型"""
    MARKET = "MARKET"  # 市价单
    LIMIT = "LIMIT"    # 限价单
    STOP = "STOP"      # 止损单


class OrderStatus(str, Enum):
    """订单状态"""
    PENDING = "PENDING"      # 待提交
    SUBMITTED = "SUBMITTED"  # 已提交
    FILLED = "FILLED"        # 已成交
    PARTIALLY_FILLED = "PARTIALLY_FILLED"  # 部分成交
    CANCELLED = "CANCELLED"  # 已取消
    REJECTED = "REJECTED"    # 已拒绝
    UNKNOWN = "UNKNOWN"      # 未知状态


@dataclass
class Order:
    """订单数据类"""
    code: str
    side: OrderSide
    quantity: int
    order_type: OrderType
    price: Optional[float] = None
    stop_price: Optional[float] = None
    status: OrderStatus = OrderStatus.PENDING
    order_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    submitted_at: Optional[float] = None
    filled_at: Optional[float] = None
    filled_quantity: int = 0
    filled_price: float = 0.0
    error_message: Optional[str] = None
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "order_id": self.order_id,
            "code": self.code,
            "side": self.side.value,
            "quantity": self.quantity,
            "order_type": self.order_type.value,
            "price": self.price,
            "stop_price": self.stop_price,
            "status": self.status.value,
            "filled_quantity": self.filled_quantity,
            "filled_price": self.filled_price,
            "created_at": datetime.fromtimestamp(self.created_at).strftime("%Y-%m-%d %H:%M:%S"),
            "submitted_at": datetime.fromtimestamp(self.submitted_at).strftime("%Y-%m-%d %H:%M:%S") if self.submitted_at else None,
            "filled_at": datetime.fromtimestamp(self.filled_at).strftime("%Y-%m-%d %H:%M:%S") if self.filled_at else None,
        }


@dataclass
class OrderResult:
    """订单执行结果"""
    success: bool
    order_id: Optional[str]
    message: str
    filled_quantity: int = 0
    filled_price: float = 0.0
    error_code: Optional[str] = None
    
    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "order_id": self.order_id,
            "message": self.message,
            "filled_quantity": self.filled_quantity,
            "filled_price": self.filled_price,
            "error_code": self.error_code,
        }


class OrderRouter:
    """智能订单路由器"""
    
    def __init__(
        self,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        futu_host: str = "127.0.0.1",
        futu_port: int = 11111,
    ):
        """
        初始化订单路由器
        
        Args:
            max_retries: 最大重试次数 (默认 3)
            retry_delay: 重试延迟 (秒，默认 1.0)
            futu_host: 富途 OpenD 主机
            futu_port: 富途 OpenD 端口
        """
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.futu_host = futu_host
        self.futu_port = futu_port
        
        # 订单追踪
        self.pending_orders: Dict[str, Order] = {}
        self.order_history: List[Order] = []
        
        # 富途客户端 (延迟初始化)
        self.futu_client = None
        
        self.logger = logging.getLogger("ftseek.v4.order_router")
    
    def _init_futu_client(self):
        """初始化富途客户端"""
        try:
            from futu import OpenQuoteContext, OpenTradeContext, Config
            from futu.common.constant import TrdEnv
            
            # 配置
            Config.set_host(self.futu_host, self.futu_port)
            
            # 交易上下文 (模拟环境)
            self.futu_client = OpenTradeContext(
                trd_env=TrdEnv.SIMULATE,
                host=self.futu_host,
                port=self.futu_port
            )
            
            self.logger.info(f"富途交易客户端已初始化 (模拟模式)")
            
        except ImportError:
            self.logger.warning("富途 SDK 未安装，使用 Mock 客户端")
            self.futu_client = self._create_mock_client()
        except Exception as e:
            self.logger.error(f"富途客户端初始化失败：{e}")
            self.futu_client = self._create_mock_client()
    
    def _create_mock_client(self) -> Any:
        """创建 Mock 客户端 (用于测试)"""
        class MockClient:
            def place_order(self, **kwargs):
                return {"order_id": f"MOCK_{int(time.time())}", "status": "SUBMITTED"}
            
            def cancel_order(self, **kwargs):
                return {"success": True}
            
            def get_order_status(self, **kwargs):
                return {"status": "FILLED"}
        
        return MockClient()
    
    def route(self, order: Order) -> Order:
        """
        路由订单
        
        Args:
            order: 订单对象
            
        Returns:
            路由后的订单
        """
        # 验证订单
        if not self.validate_order(order):
            order.status = OrderStatus.REJECTED
            order.error_message = "订单验证失败"
            self.logger.warning(f"订单验证失败：{order.code}")
            return order
        
        # 根据订单类型路由
        if order.order_type == OrderType.MARKET:
            return self._route_market_order(order)
        elif order.order_type == OrderType.LIMIT:
            return self._route_limit_order(order)
        elif order.order_type == OrderType.STOP:
            return self._route_stop_order(order)
        else:
            order.status = OrderStatus.REJECTED
            order.error_message = f"未知订单类型：{order.order_type}"
            return order
    
    def _route_market_order(self, order: Order) -> Order:
        """路由市价单"""
        self.logger.info(f"路由市价单：{order.code} {order.side.value} {order.quantity}")
        order.status = OrderStatus.PENDING
        return order
    
    def _route_limit_order(self, order: Order) -> Order:
        """路由限价单"""
        self.logger.info(f"路由限价单：{order.code} {order.side.value} {order.quantity} @ {order.price}")
        order.status = OrderStatus.PENDING
        return order
    
    def _route_stop_order(self, order: Order) -> Order:
        """路由止损单"""
        self.logger.info(f"路由止损单：{order.code} {order.side.value} {order.quantity} stop={order.stop_price}")
        order.status = OrderStatus.PENDING
        return order
    
    def validate_order(self, order: Order) -> bool:
        """
        验证订单
        
        Args:
            order: 订单对象
            
        Returns:
            是否有效
        """
        # 检查代码
        if not order.code or not isinstance(order.code, str):
            self.logger.error("无效的股票代码")
            return False
        
        # 检查数量
        if order.quantity <= 0:
            self.logger.error(f"无效的数量：{order.quantity}")
            return False
        
        # 检查限价单价格
        if order.order_type == OrderType.LIMIT and order.price is None:
            self.logger.error("限价单必须指定价格")
            return False
        
        # 检查止损单价格
        if order.order_type == OrderType.STOP and order.stop_price is None:
            self.logger.error("止损单必须指定止损价格")
            return False
        
        return True
    
    def submit_order(self, order: Order) -> OrderResult:
        """
        提交订单
        
        Args:
            order: 订单对象
            
        Returns:
            订单结果
        """
        if not self.futu_client:
            self._init_futu_client()
        
        try:
            # 生成订单 ID
            order.order_id = f"ORD_{int(time.time())}_{uuid.uuid4().hex[:8]}"
            
            # 转换并提交
            futu_order = self.convert_to_futu_order(order)
            
            # 调用富途 API
            response = self.futu_client.place_order(**futu_order)
            
            # 解析结果
            result = self.parse_futu_result(response)
            
            if result.success:
                order.status = OrderStatus.SUBMITTED
                order.submitted_at = time.time()
                order.order_id = result.order_id
                self.pending_orders[order.order_id] = order
                self.order_history.append(order)
                self.logger.info(f"订单提交成功：{order.order_id}")
            else:
                order.status = OrderStatus.REJECTED
                order.error_message = result.message
            
            return result
            
        except Exception as e:
            self.logger.error(f"订单提交失败：{e}")
            return OrderResult(
                success=False,
                order_id=None,
                message=str(e),
                error_code="API_ERROR"
            )
    
    def submit_order_with_retry(self, order: Order) -> OrderResult:
        """
        提交订单 (带重试)
        
        Args:
            order: 订单对象
            
        Returns:
            订单结果
        """
        for attempt in range(self.max_retries):
            result = self.submit_order(order)
            
            if result.success:
                return result
            
            # 重试前等待
            if attempt < self.max_retries - 1:
                self.logger.warning(f"订单提交失败，{self.retry_delay}秒后重试 ({attempt + 1}/{self.max_retries})")
                time.sleep(self.retry_delay)
        
        self.logger.error(f"订单提交失败，已重试{self.max_retries}次")
        return OrderResult(
            success=False,
            order_id=None,
            message=f"重试{self.max_retries}次后仍失败",
            error_code="MAX_RETRIES_EXCEEDED"
        )
    
    def get_order_status(self, order_id: str) -> OrderStatus:
        """
        获取订单状态
        
        Args:
            order_id: 订单 ID
            
        Returns:
            订单状态
        """
        # 检查待处理订单
        if order_id in self.pending_orders:
            return self.pending_orders[order_id].status
        
        # 检查历史订单
        for order in self.order_history:
            if order.order_id == order_id:
                return order.status
        
        return OrderStatus.UNKNOWN
    
    def cancel_order(self, order_id: str) -> OrderResult:
        """
        取消订单
        
        Args:
            order_id: 订单 ID
            
        Returns:
            取消结果
        """
        if not self.futu_client:
            self._init_futu_client()
        
        try:
            # 调用富途 API
            response = self.futu_client.cancel_order(order_id=order_id)
            
            if response.get("success", False):
                # 更新本地状态
                if order_id in self.pending_orders:
                    self.pending_orders[order_id].status = OrderStatus.CANCELLED
                
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    message="订单已取消"
                )
            else:
                return OrderResult(
                    success=False,
                    order_id=order_id,
                    message=response.get("message", "取消失败")
                )
                
        except Exception as e:
            self.logger.error(f"取消订单失败：{e}")
            return OrderResult(
                success=False,
                order_id=order_id,
                message=str(e),
                error_code="API_ERROR"
            )
    
    def select_order_type(self, signal: Any) -> OrderType:
        """
        根据信号选择订单类型
        
        Args:
            signal: 交易信号
            
        Returns:
            订单类型
        """
        urgency = getattr(signal, 'urgency', 'NORMAL')
        
        if urgency == "HIGH":
            # 紧急信号使用市价单
            return OrderType.MARKET
        elif urgency == "LOW":
            # 不紧急使用限价单
            return OrderType.LIMIT
        else:
            # 默认根据是否有目标价格决定
            if hasattr(signal, 'target_price') and signal.target_price:
                return OrderType.LIMIT
            return OrderType.MARKET
    
    def create_order_from_signal(self, signal: Any) -> Order:
        """
        从交易信号创建订单
        
        Args:
            signal: 交易信号
            
        Returns:
            订单对象
        """
        order_type = self.select_order_type(signal)
        
        order = Order(
            code=getattr(signal, 'code', ''),
            side=OrderSide.BUY if getattr(signal, 'direction', 'BUY') == 'BUY' else OrderSide.SELL,
            quantity=getattr(signal, 'quantity', 100),
            order_type=order_type,
            price=getattr(signal, 'target_price', None),
        )
        
        return order
    
    def convert_to_futu_order(self, order: Order) -> dict:
        """
        转换为富途订单格式
        
        Args:
            order: 订单对象
            
        Returns:
            富途订单字典
        """
        try:
            from futu.common.constant import TrdSide, OrderType as FutuOrderType
            
            # 方向转换
            side = TrdSide.BUY if order.side == OrderSide.BUY else TrdSide.SELL
            
            # 订单类型转换
            if order.order_type == OrderType.MARKET:
                order_type = FutuOrderType.MKT  # 市价单
            elif order.order_type == OrderType.LIMIT:
                order_type = FutuOrderType.NORMAL  # 正常限价单
            elif order.order_type == OrderType.STOP:
                order_type = FutuOrderType.STOP  # 止损单
            else:
                order_type = FutuOrderType.NORMAL
        except (ImportError, AttributeError):
            # 测试环境或无富途 SDK，使用字符串
            side = "BUY" if order.side == OrderSide.BUY else "SELL"
            order_type = order.order_type.value
        
        return {
            "code": order.code,
            "trd_side": side,
            "order_type": order_type,
            "qty": order.quantity,
            "price": order.price if order.price else 0,
            "stop_price": order.stop_price if order.stop_price else 0,
        }
    
    def parse_futu_result(self, response: dict) -> OrderResult:
        """
        解析富途返回结果
        
        Args:
            response: 富途 API 响应
            
        Returns:
            订单结果
        """
        if response.get("error"):
            return OrderResult(
                success=False,
                order_id=None,
                message=response.get("error", "未知错误"),
                error_code="FUTU_ERROR"
            )
        
        return OrderResult(
            success=True,
            order_id=response.get("order_id"),
            message=response.get("message", "提交成功"),
            filled_quantity=response.get("qty", 0),
            filled_price=response.get("price", 0.0)
        )
    
    def get_order_history(
        self,
        code: Optional[str] = None,
        status: Optional[OrderStatus] = None,
        limit: int = 50
    ) -> List[Order]:
        """
        获取订单历史
        
        Args:
            code: 股票代码过滤
            status: 订单状态过滤
            limit: 返回数量限制
            
        Returns:
            订单列表
        """
        history = self.order_history
        
        if code:
            history = [o for o in history if o.code == code]
        
        if status:
            history = [o for o in history if o.status == status]
        
        return history[-limit:]
    
    def get_pending_orders(self) -> List[Order]:
        """获取所有待处理订单"""
        return list(self.pending_orders.values())
    
    def clear_filled_orders(self):
        """清除已成交订单"""
        filled_ids = [
            oid for oid, order in self.pending_orders.items()
            if order.status in [OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED]
        ]
        
        for order_id in filled_ids:
            del self.pending_orders[order_id]
        
        self.logger.info(f"清除{len(filled_ids)}个已成交订单")
    
    def get_status(self) -> dict:
        """获取路由器状态"""
        return {
            "pending_orders": len(self.pending_orders),
            "total_history": len(self.order_history),
            "futu_client_initialized": self.futu_client is not None,
            "max_retries": self.max_retries,
            "retry_delay": self.retry_delay,
        }


# 全局实例 (可选)
order_router: Optional[OrderRouter] = None


def get_order_router() -> OrderRouter:
    """获取 OrderRouter 单例"""
    global order_router
    if order_router is None:
        order_router = OrderRouter()
    return order_router
