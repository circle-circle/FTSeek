"""
OrderRouter 单元测试 - 智能订单路由模块

测试覆盖:
- 订单类型选择 (市价/限价)
- 订单路由决策
- 订单状态追踪
- 失败重试机制
- 富途 API 对接模拟
"""
import pytest
import time
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime
import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.v4.execution.order_router import (
    OrderRouter,
    Order,
    OrderStatus,
    OrderType,
    OrderSide,
    OrderResult
)


class TestOrderEnums:
    """订单枚举测试"""
    
    def test_order_side(self):
        """测试订单方向"""
        assert OrderSide.BUY == "BUY"
        assert OrderSide.SELL == "SELL"
    
    def test_order_type(self):
        """测试订单类型"""
        assert OrderType.MARKET == "MARKET"
        assert OrderType.LIMIT == "LIMIT"
        assert OrderType.STOP == "STOP"
    
    def test_order_status(self):
        """测试订单状态"""
        assert OrderStatus.PENDING == "PENDING"
        assert OrderStatus.SUBMITTED == "SUBMITTED"
        assert OrderStatus.FILLED == "FILLED"
        assert OrderStatus.CANCELLED == "CANCELLED"
        assert OrderStatus.REJECTED == "REJECTED"


class TestOrder:
    """Order 数据类测试"""
    
    def test_order_creation(self):
        """测试订单创建"""
        order = Order(
            code="HK.00700",
            side=OrderSide.BUY,
            quantity=100,
            order_type=OrderType.MARKET,
            price=None
        )
        
        assert order.code == "HK.00700"
        assert order.side == OrderSide.BUY
        assert order.quantity == 100
        assert order.order_type == OrderType.MARKET
        assert order.price is None
        assert order.status == OrderStatus.PENDING
    
    def test_limit_order_creation(self):
        """测试限价订单创建"""
        order = Order(
            code="HK.00700",
            side=OrderSide.BUY,
            quantity=100,
            order_type=OrderType.LIMIT,
            price=100.0
        )
        
        assert order.order_type == OrderType.LIMIT
        assert order.price == 100.0
    
    def test_order_to_dict(self):
        """测试订单转字典"""
        order = Order(
            code="HK.00700",
            side=OrderSide.BUY,
            quantity=100,
            order_type=OrderType.MARKET
        )
        
        order_dict = order.to_dict()
        
        assert order_dict["code"] == "HK.00700"
        assert order_dict["side"] == "BUY"
        assert order_dict["quantity"] == 100
        assert order_dict["order_type"] == "MARKET"


class TestOrderResult:
    """OrderResult 测试结果测试"""
    
    def test_order_result_success(self):
        """测试成功订单结果"""
        result = OrderResult(
            success=True,
            order_id="ORD123456",
            message="Order submitted successfully",
            filled_quantity=100,
            filled_price=100.5
        )
        
        assert result.success == True
        assert result.order_id == "ORD123456"
        assert result.filled_quantity == 100
    
    def test_order_result_failure(self):
        """测试失败订单结果"""
        result = OrderResult(
            success=False,
            order_id=None,
            message="Insufficient funds",
            error_code="FUND_001"
        )
        
        assert result.success == False
        assert result.order_id is None
        assert "Insufficient" in result.message


class TestOrderRouter:
    """OrderRouter 核心功能测试"""
    
    def test_router_initialization(self):
        """测试路由器初始化"""
        router = OrderRouter()
        
        assert router.max_retries == 3
        assert router.retry_delay == 1.0
        assert len(router.pending_orders) == 0
    
    def test_market_order_routing(self):
        """测试市价订单路由"""
        router = OrderRouter()
        
        order = Order(
            code="HK.00700",
            side=OrderSide.BUY,
            quantity=100,
            order_type=OrderType.MARKET
        )
        
        routed_order = router.route(order)
        
        assert routed_order is not None
        assert routed_order.order_type == OrderType.MARKET
        assert routed_order.status == OrderStatus.PENDING
    
    def test_limit_order_routing(self):
        """测试限价订单路由"""
        router = OrderRouter()
        
        order = Order(
            code="HK.00700",
            side=OrderSide.BUY,
            quantity=100,
            order_type=OrderType.LIMIT,
            price=100.0
        )
        
        routed_order = router.route(order)
        
        assert routed_order.order_type == OrderType.LIMIT
        assert routed_order.price == 100.0
    
    def test_order_validation(self):
        """测试订单验证"""
        router = OrderRouter()
        
        # 有效订单
        valid_order = Order(
            code="HK.00700",
            side=OrderSide.BUY,
            quantity=100,
            order_type=OrderType.MARKET
        )
        assert router.validate_order(valid_order) == True
        
        # 无效订单 - 数量为零
        invalid_order = Order(
            code="HK.00700",
            side=OrderSide.BUY,
            quantity=0,
            order_type=OrderType.MARKET
        )
        assert router.validate_order(invalid_order) == False
        
        # 无效订单 - 限价单无价格
        invalid_limit = Order(
            code="HK.00700",
            side=OrderSide.BUY,
            quantity=100,
            order_type=OrderType.LIMIT,
            price=None
        )
        assert router.validate_order(invalid_limit) == False
    
    def test_order_submission(self):
        """测试订单提交"""
        router = OrderRouter()
        
        order = Order(
            code="HK.00700",
            side=OrderSide.BUY,
            quantity=100,
            order_type=OrderType.MARKET
        )
        
        # Mock 富途客户端
        mock_client = Mock()
        mock_client.place_order.return_value = {
            "order_id": "ORD123456",
            "status": "SUBMITTED"
        }
        router.futu_client = mock_client
        
        result = router.submit_order(order)
        
        assert result.success == True
        assert result.order_id == "ORD123456"
        mock_client.place_order.assert_called_once()
    
    def test_order_submission_failure(self):
        """测试订单提交失败"""
        router = OrderRouter()
        
        order = Order(
            code="HK.00700",
            side=OrderSide.BUY,
            quantity=100,
            order_type=OrderType.MARKET
        )
        
        # Mock 失败的富途客户端
        mock_client = Mock()
        mock_client.place_order.side_effect = Exception("API Error")
        router.futu_client = mock_client
        
        result = router.submit_order(order)
        
        assert result.success == False
        assert result.error_code == "API_ERROR"
    
    def test_order_status_tracking(self):
        """测试订单状态追踪"""
        router = OrderRouter()
        
        order = Order(
            code="HK.00700",
            side=OrderSide.BUY,
            quantity=100,
            order_type=OrderType.MARKET
        )
        
        # 提交订单
        router.pending_orders["ORD123"] = order
        order.status = OrderStatus.SUBMITTED
        
        # 查询状态
        status = router.get_order_status("ORD123")
        
        assert status == OrderStatus.SUBMITTED
    
    def test_order_status_not_found(self):
        """测试订单状态不存在"""
        router = OrderRouter()
        
        status = router.get_order_status("NON_EXISTENT")
        
        assert status == OrderStatus.UNKNOWN
    
    def test_order_cancellation(self):
        """测试订单取消"""
        router = OrderRouter()
        
        order = Order(
            code="HK.00700",
            side=OrderSide.BUY,
            quantity=100,
            order_type=OrderType.MARKET
        )
        
        router.pending_orders["ORD123"] = order
        
        # Mock 富途客户端
        mock_client = Mock()
        mock_client.cancel_order.return_value = {"success": True}
        router.futu_client = mock_client
        
        result = router.cancel_order("ORD123")
        
        assert result.success == True
        assert order.status == OrderStatus.CANCELLED
    
    def test_retry_mechanism(self):
        """测试重试机制"""
        router = OrderRouter(max_retries=3, retry_delay=0.1)
        
        order = Order(
            code="HK.00700",
            side=OrderSide.BUY,
            quantity=100,
            order_type=OrderType.MARKET
        )
        
        # Mock 前两次失败，第三次成功
        mock_client = Mock()
        mock_client.place_order.side_effect = [
            Exception("API Error 1"),
            Exception("API Error 2"),
            {"order_id": "ORD123", "status": "SUBMITTED"}
        ]
        router.futu_client = mock_client
        
        result = router.submit_order_with_retry(order)
        
        assert result.success == True
        assert result.order_id == "ORD123"
        assert mock_client.place_order.call_count == 3
    
    def test_retry_exhausted(self):
        """测试重试耗尽"""
        router = OrderRouter(max_retries=2, retry_delay=0.1)
        
        order = Order(
            code="HK.00700",
            side=OrderSide.BUY,
            quantity=100,
            order_type=OrderType.MARKET
        )
        
        # Mock 一直失败
        mock_client = Mock()
        mock_client.place_order.side_effect = Exception("API Error")
        router.futu_client = mock_client
        
        result = router.submit_order_with_retry(order)
        
        assert result.success == False
        assert mock_client.place_order.call_count == 2
    
    def test_order_type_selection_auto(self):
        """测试自动订单类型选择"""
        router = OrderRouter()
        
        # 市价信号 - 紧急
        urgent_signal = Mock()
        urgent_signal.urgency = "HIGH"
        order_type = router.select_order_type(urgent_signal)
        assert order_type == OrderType.MARKET
        
        # 限价信号 - 不紧急
        normal_signal = Mock()
        normal_signal.urgency = "LOW"
        normal_signal.target_price = 100.0
        order_type = router.select_order_type(normal_signal)
        assert order_type == OrderType.LIMIT
    
    def test_smart_order_routing(self):
        """测试智能订单路由"""
        router = OrderRouter()
        
        # Mock 信号
        signal = Mock()
        signal.code = "HK.00700"
        signal.direction = "BUY"
        signal.quantity = 100
        signal.urgency = "HIGH"
        signal.target_price = None
        
        order = router.create_order_from_signal(signal)
        
        assert order.code == "HK.00700"
        assert order.side == OrderSide.BUY
        assert order.quantity == 100
        assert order.order_type == OrderType.MARKET
    
    def test_order_history(self):
        """测试订单历史"""
        router = OrderRouter()
        
        # 添加订单
        order1 = Order(code="HK.00700", side=OrderSide.BUY, quantity=100, order_type=OrderType.MARKET)
        order2 = Order(code="HK.09988", side=OrderSide.SELL, quantity=200, order_type=OrderType.LIMIT, price=80.0)
        
        router.order_history.append(order1)
        router.order_history.append(order2)
        
        history = router.get_order_history()
        
        assert len(history) == 2
    
    def test_order_history_filter_by_code(self):
        """测试按代码过滤订单历史"""
        router = OrderRouter()
        
        order1 = Order(code="HK.00700", side=OrderSide.BUY, quantity=100, order_type=OrderType.MARKET)
        order2 = Order(code="HK.00700", side=OrderSide.SELL, quantity=100, order_type=OrderType.MARKET)
        order3 = Order(code="HK.09988", side=OrderSide.BUY, quantity=200, order_type=OrderType.MARKET)
        
        router.order_history.extend([order1, order2, order3])
        
        history = router.get_order_history(code="HK.00700")
        
        assert len(history) == 2
    
    def test_order_history_filter_by_status(self):
        """测试按状态过滤订单历史"""
        router = OrderRouter()
        
        order1 = Order(code="HK.00700", side=OrderSide.BUY, quantity=100, order_type=OrderType.MARKET)
        order1.status = OrderStatus.FILLED
        
        order2 = Order(code="HK.09988", side=OrderSide.BUY, quantity=200, order_type=OrderType.MARKET)
        order2.status = OrderStatus.CANCELLED
        
        router.order_history.extend([order1, order2])
        
        history = router.get_order_history(status=OrderStatus.FILLED)
        
        assert len(history) == 1
        assert history[0].status == OrderStatus.FILLED


class TestFutuAPIIntegration:
    """富途 API 集成测试"""
    
    def test_futu_client_initialization(self):
        """测试富途客户端初始化"""
        router = OrderRouter()
        
        # 默认应该有客户端 (可能是 Mock 或 None)
        assert hasattr(router, 'futu_client')
    
    def test_order_conversion_to_futu(self):
        """测试订单转换为富途格式"""
        router = OrderRouter()
        
        order = Order(
            code="HK.00700",
            side=OrderSide.BUY,
            quantity=100,
            order_type=OrderType.LIMIT,
            price=100.0
        )
        
        futu_order = router.convert_to_futu_order(order)
        
        assert futu_order is not None
        # 验证转换逻辑
    
    def test_futu_order_result_parsing(self):
        """测试富途订单结果解析"""
        router = OrderRouter()
        
        futu_response = {
            "order_id": "ORD123456",
            "status": "SUBMITTED",
            "code": "HK.00700",
            "qty": 100,
            "price": 100.0
        }
        
        result = router.parse_futu_result(futu_response)
        
        assert result.success == True
        assert result.order_id == "ORD123456"


# 运行测试
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
