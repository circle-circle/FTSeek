"""
OpenDExecutor - OpenD 交易执行器

功能:
- 连接富途 OpenD
- 执行买入/卖出订单
- 订单状态追踪
- 执行结果反馈
"""
import os
import time
import logging
from typing import Dict, Optional, Any, Tuple
from dataclasses import dataclass

logger = logging.getLogger("ftseek.v4.opend_executor")


@dataclass
class OrderResult:
    """订单执行结果"""
    success: bool
    order_id: str = ""
    message: str = ""
    filled_price: float = 0.0
    filled_quantity: int = 0
    error_code: str = ""
    
    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "order_id": self.order_id,
            "message": self.message,
            "filled_price": self.filled_price,
            "filled_quantity": self.filled_quantity,
            "error_code": self.error_code,
        }


class OpenDExecutor:
    """OpenD 交易执行器 (强制模拟盘保护)"""
    
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 11111,
        simulate: bool = None  # None 表示从环境变量读取
    ):
        # 强制模拟盘保护
        from pathlib import Path
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent.parent.parent / '.env')
        
        force_simulate = os.getenv('FORCE_SIMULATE_MODE', 'true').lower() == 'true'
        real_locked = os.getenv('REAL_TRADING_LOCKED', 'true').lower() == 'true'
        
        # 如果强制模拟模式，忽略传入的 simulate 参数
        if force_simulate or real_locked:
            self.simulate = True
            logger.warning("⚠️ 强制模拟盘模式已启用 - 实盘交易已锁定")
            logger.warning("⚠️ 如需切换到实盘，需完成验证并修改 .env 配置")
        else:
            self.simulate = simulate if simulate is not None else True
        
        # 模拟盘不需要密码
        self.trade_password = '' if self.simulate else os.getenv('FUTU_TRADE_PASSWORD', '')
        """
        初始化执行器
        
        Args:
            host: OpenD 主机
            port: OpenD 端口
            simulate: 是否模拟交易
        """
        self.host = host
        self.port = port
        self.simulate = simulate
        self.client = None
        self.logger = logging.getLogger("ftseek.v4.opend_executor")
        
        self.logger.info(
            f"OpenD 执行器初始化：{host}:{port}, 模拟模式：{simulate}"
        )
    
    def connect(self) -> bool:
        """
        连接 OpenD
        
        Returns:
            是否成功
        """
        try:
            from futu import OpenSecTradeContext
            from futu.common.constant import TrdEnv, TrdMarket
            
            # 交易上下文 (使用证券交易上下文，支持港股/美股/A 股)
            self.client = OpenSecTradeContext(host=self.host, port=self.port)
            
            # 解锁交易 (模拟盘跳过密码验证)
            if self.simulate:
                self.logger.info("模拟盘模式，跳过交易密码验证")
                self.logger.info(f"OpenD 连接成功，模式：模拟")
                return True
            else:
                password = self.trade_password
                ret, msg = self.client.unlock_trade(password=password)
                if ret == 0:
                    self.logger.info(f"交易解锁成功 (实盘)")
                else:
                    self.logger.error(f"交易解锁失败：{msg}")
                    return False
            
            self.logger.info(f"OpenD 连接成功，模式：{'模拟' if self.simulate else '真实'}")
            return True
            
        except Exception as e:
            self.logger.error(f"OpenD 连接失败：{e}")
            self.client = None
            return False
    
    def execute_order(
        self,
        code: str,
        action: str,
        quantity: int,
        price: Optional[float] = None
    ) -> OrderResult:
        """
        执行订单
        
        Args:
            code: 股票代码
            action: 买卖方向 (BUY/SELL)
            quantity: 数量
            price: 价格 (None 为市价单)
            
        Returns:
            执行结果
        """
        if not self.client:
            if not self.connect():
                return OrderResult(
                    success=False,
                    message="OpenD 未连接",
                    error_code="NOT_CONNECTED"
                )
        
        try:
            from futu import TrdSide, OrderType
            
            # 方向转换
            side = TrdSide.BUY if action == "BUY" else TrdSide.SELL
            
            # 订单类型
            order_type = OrderType.NORMAL if price else OrderType.MKT
            
            # 模拟模式
            if self.simulate:
                return self._simulate_order(code, action, quantity, price)
            
            # 真实下单
            ret_code, ret_data = self.client.place_order(
                price=price or 0,
                qty=quantity,
                code=code,
                trd_side=side,
                order_type=order_type
            )
            
            if ret_code == 0:
                order_id = ret_data.get("order_id", "")
                self.logger.info(f"订单提交成功：{order_id}")
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    message="订单已提交"
                )
            else:
                error_msg = ret_data.get("msg", "未知错误")
                self.logger.error(f"订单提交失败：{error_msg}")
                return OrderResult(
                    success=False,
                    message=error_msg,
                    error_code=str(ret_code)
                )
                
        except Exception as e:
            self.logger.error(f"执行订单失败：{e}")
            return OrderResult(
                success=False,
                message=str(e),
                error_code="EXECUTION_ERROR"
            )
    
    def _simulate_order(
        self,
        code: str,
        action: str,
        quantity: int,
        price: Optional[float] = None
    ) -> OrderResult:
        """
        模拟订单执行
        
        Args:
            code: 股票代码
            action: 买卖方向
            quantity: 数量
            price: 价格
            
        Returns:
            执行结果
        """
        # 模拟延迟
        time.sleep(0.5)
        
        # 生成模拟订单 ID
        order_id = f"SIM_{int(time.time())}_{code}"
        
        # 模拟成交价
        filled_price = price if price else 0.0
        
        self.logger.info(
            f"[模拟] 订单执行：{code} {action} {quantity}股 @ {filled_price}"
        )
        
        return OrderResult(
            success=True,
            order_id=order_id,
            message="[模拟交易] 订单已执行",
            filled_price=filled_price,
            filled_quantity=quantity
        )
    
    def cancel_order(self, order_id: str) -> bool:
        """
        取消订单
        
        Args:
            order_id: 订单 ID
            
        Returns:
            是否成功
        """
        if not self.client:
            return False
        
        try:
            if self.simulate:
                self.logger.info(f"[模拟] 取消订单：{order_id}")
                return True
            
            ret_code, ret_data = self.client.cancel_order(order_id)
            return ret_code == 0
            
        except Exception as e:
            self.logger.error(f"取消订单失败：{e}")
            return False
    
    def get_order_status(self, order_id: str) -> Optional[Dict[str, Any]]:
        """
        获取订单状态
        
        Args:
            order_id: 订单 ID
            
        Returns:
            订单状态
        """
        if not self.client:
            return None
        
        try:
            if self.simulate:
                return {
                    "order_id": order_id,
                    "status": "FILLED",
                    "message": "[模拟] 已成交"
                }
            
            # 查询订单
            ret_code, ret_data = self.client.get_history_order_list(
                filter_orders_by_order_ids=[order_id]
            )
            
            if ret_code == 0 and ret_data:
                return ret_data[0]
            return None
            
        except Exception as e:
            self.logger.error(f"查询订单状态失败：{e}")
            return None
    
    def disconnect(self):
        """断开连接"""
        if self.client:
            self.client.close()
            self.client = None
            self.logger.info("OpenD 已断开连接")
    
    def get_status(self) -> dict:
        """获取状态"""
        return {
            "connected": self.client is not None,
            "host": self.host,
            "port": self.port,
            "simulate": self.simulate,
        }


# 全局实例
opend_executor: Optional[OpenDExecutor] = None


def get_opend_executor(
    host: str = "127.0.0.1",
    port: int = 11111,
    simulate: bool = True
) -> OpenDExecutor:
    """获取 OpenDExecutor 单例"""
    global opend_executor
    if opend_executor is None:
        opend_executor = OpenDExecutor(host=host, port=port, simulate=simulate)
    return opend_executor
