"""
实时行情订阅模块

订阅富途 OpenD 实时行情，触发策略检查
"""
import logging
from typing import Dict, List, Callable, Optional
from datetime import datetime

logger = logging.getLogger("ftseek.data.realtime_subscriber")
logger.propagate = False  # 防止日志重复

# 导入富途 API
try:
    from futu import OpenQuoteContext, KLType, SubType
    FUTU_AVAILABLE = True
except ImportError:
    FUTU_AVAILABLE = False
    logger.warning("富途 SDK 未安装，使用模拟模式")


class RealtimeDataSubscriber:
    """实时行情订阅器"""
    
    def __init__(self, host: str = '127.0.0.1', port: int = 11111):
        """
        初始化订阅器
        
        Args:
            host: OpenD 主机
            port: OpenD 端口
        """
        self.host = host
        self.port = port
        self.quote_ctx = None
        self.subscribed_codes: List[str] = []
        self.callbacks: List[Callable] = []
        
        if FUTU_AVAILABLE:
            self._connect_quote_ctx()
        
        logger.info(f"实时行情订阅器已初始化 ({host}:{port})")
    
    def _connect_quote_ctx(self):
        """连接富途行情上下文"""
        self.quote_ctx = OpenQuoteContext(host=self.host, port=self.port)
        logger.info("富途行情上下文已连接")
    
    def subscribe(self, codes: List[str], callback: Callable = None):
        """
        订阅股票行情
        
        Args:
            codes: 股票代码列表 ['HK.00700', 'HK.9988']
            callback: 行情回调函数
        """
        if callback:
            self.callbacks.append(callback)
        
        for code in codes:
            if code not in self.subscribed_codes:
                self.subscribed_codes.append(code)
        
        if FUTU_AVAILABLE and self.quote_ctx:
            self._futu_subscribe(codes)
        else:
            logger.info(f"模拟订阅：{codes}")
    
    def _futu_subscribe(self, codes: List[str]):
        """富途订阅"""
        # 解析股票代码
        stock_codes = [code.split('.')[1] for code in codes]
        
        # 订阅实时报价
        self.quote_ctx.subscribe(stock_codes, [SubType.QUOTE])
        
        # 设置回调
        self.quote_ctx.set_handler(self)
        
        logger.info(f"已订阅 {len(codes)} 只股票行情")
    
    def on_recv_quote(self, quote_list: list):
        """接收实时报价回调"""
        logger.debug(f"收到 {len(quote_list)} 条报价")
        
        for quote in quote_list:
            code = quote['code']
            
            # 触发策略检查
            self._trigger_strategy_check(code, quote)
    
    def _trigger_strategy_check(self, code: str, quote: Dict):
        """触发策略检查"""
        for callback in self.callbacks:
            try:
                callback(code, quote)
            except Exception as e:
                logger.error(f"回调执行失败：{e}")
    
    def unsubscribe(self, codes: List[str]):
        """取消订阅"""
        for code in codes:
            if code in self.subscribed_codes:
                self.subscribed_codes.remove(code)
        
        if FUTU_AVAILABLE and self.quote_ctx:
            stock_codes = [code.split('.')[1] for code in codes]
            self.quote_ctx.unsubscribe(stock_codes, [SubType.QUOTE])
    
    def get_subscription_status(self) -> Dict:
        """获取订阅状态"""
        return {
            'connected': self.quote_ctx is not None,
            'subscribed_codes': self.subscribed_codes,
            'callback_count': len(self.callbacks)
        }
    
    def close(self):
        """关闭连接"""
        if self.quote_ctx:
            self.quote_ctx.close()
            logger.info("行情上下文已关闭")


# 全局单例
_subscriber = None

def get_realtime_subscriber(host: str = '127.0.0.1', port: int = 11111) -> RealtimeDataSubscriber:
    """获取实时行情订阅器单例"""
    global _subscriber
    if _subscriber is None:
        _subscriber = RealtimeDataSubscriber(host=host, port=port)
    return _subscriber


# 策略检查回调示例
def strategy_check_callback(code: str, quote: Dict):
    """
    策略检查回调
    
    Args:
        code: 股票代码
        quote: 实时报价
    """
    logger.debug(f"策略检查：{code}")
    
    # 1. 获取最新价格
    price = quote.get('last_done', 0)
    
    # 2. 计算因子
    # ...
    
    # 3. 生成信号
    # ...
    
    # 4. 执行交易
    # ...
