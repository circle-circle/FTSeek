"""
自动交易执行器

连接富途 API，自动执行交易信号
"""
import logging
from typing import Dict, Optional
from datetime import datetime
from dataclasses import dataclass

logger = logging.getLogger("ftseek.execution.auto_executor")

# 导入富途 API (如果可用)
try:
    from futu import OpenTradeContext, TrdEnv, TrdSide, OrderType
    FUTU_AVAILABLE = True
except ImportError:
    FUTU_AVAILABLE = False
    logger.warning("富途 SDK 未安装，使用模拟模式")

from app.storage.trade_recorder import get_trade_recorder
from app.risk.realtime_alert import get_realtime_risk_monitor


@dataclass
class OrderResult:
    """订单结果"""
    success: bool
    order_id: str = ''
    message: str = ''
    filled_price: float = 0.0
    filled_quantity: int = 0


class AutoTradeExecutor:
    """自动交易执行器"""
    
    def __init__(self, simulate: bool = True):
        """
        初始化执行器
        
        Args:
            simulate: 是否模拟交易 (默认 True，安全起见)
        """
        self.simulate = simulate
        self.trade_ctx = None
        self.recorder = get_trade_recorder()
        self.risk_monitor = get_realtime_risk_monitor()
        
        if not self.simulate and FUTU_AVAILABLE:
            self._connect_trade_ctx()
        
        logger.info(f"自动交易执行器已初始化 (模拟模式={self.simulate})")
    
    def _connect_trade_ctx(self):
        """连接富途交易上下文"""
        import os
        
        trade_password = os.getenv('FUTU_TRADE_PASSWORD', '')
        
        self.trade_ctx = OpenTradeContext(
            host='127.0.0.1',
            port=11111,
            trd_env=TrdEnv.SIMULATE if self.simulate else TrdEnv.REAL,
            pwd=trade_password
        )
        
        logger.info(f"富途交易上下文已连接 (环境：{'模拟' if self.simulate else '实盘'})")
    
    def execute_signal(self,
                      code: str,
                      action: str,
                      quantity: int,
                      price: float,
                      strategy: str,
                      signal_confidence: float,
                      ai_score: float,
                      stop_loss: float = 0.0,
                      take_profit: float = 0.0,
                      position_size: float = 0.1) -> OrderResult:
        """
        执行交易信号
        
        Args:
            code: 股票代码 (HK.00700)
            action: BUY/SELL
            quantity: 数量
            price: 价格
            strategy: 策略名称
            signal_confidence: 信号置信度
            ai_score: AI 分数
            stop_loss: 止损价
            take_profit: 止盈价
            position_size: 仓位占比
        
        Returns:
            OrderResult: 订单结果
        """
        logger.info(f"执行交易信号：{code} {action} {quantity}股 @ {price}")
        
        # 1. 风控检查
        risk_ok = self._risk_check(code, action, quantity, price)
        if not risk_ok:
            logger.warning(f"风控检查失败，取消交易：{code}")
            return OrderResult(success=False, message='风控检查失败')
        
        # 2. 执行交易
        if self.simulate:
            result = self._simulate_order(code, action, quantity, price)
        else:
            result = self._real_order(code, action, quantity, price)
        
        # 3. 记录交易
        if result.success:
            self._record_trade(
                code=code,
                strategy=strategy,
                action=action,
                entry_price=result.filled_price,
                quantity=result.filled_quantity,
                signal_confidence=signal_confidence,
                ai_score=ai_score,
                stop_loss=stop_loss,
                take_profit=take_profit,
                position_size=position_size
            )
        
        return result
    
    def _risk_check(self, code: str, action: str, quantity: int, price: float) -> bool:
        """风控检查"""
        status = self.risk_monitor.get_current_status()
        
        # 检查连续亏损
        if status['consecutive_losses'] >= 5:
            logger.warning(f"连续亏损 {status['consecutive_losses']} 笔，停止交易")
            return False
        
        # 检查日亏损
        if status['daily_pnl_percent'] <= -0.05:
            logger.warning(f"日亏损 {abs(status['daily_pnl_percent']):.2%}，停止交易")
            return False
        
        # 检查回撤
        if status['drawdown'] >= 0.15:
            logger.warning(f"回撤 {status['drawdown']:.2%}，停止交易")
            return False
        
        return True
    
    def _simulate_order(self, code: str, action: str, quantity: int, price: float) -> OrderResult:
        """模拟下单"""
        import random
        
        # 模拟 95% 成功率
        success = random.random() < 0.95
        
        if success:
            # 模拟小幅滑点
            if action == 'BUY':
                filled_price = price * (1 + random.uniform(0, 0.002))
            else:
                filled_price = price * (1 - random.uniform(0, 0.002))
            
            logger.info(f"模拟下单成功：{code} {action} {quantity}股 @ {filled_price:.2f}")
            
            return OrderResult(
                success=True,
                order_id=f"SIM_{datetime.now().strftime('%Y%m%d%H%M%S')}",
                message='模拟交易成功',
                filled_price=filled_price,
                filled_quantity=quantity
            )
        else:
            logger.warning(f"模拟下单失败：{code}")
            return OrderResult(success=False, message='模拟交易失败')
    
    def _real_order(self, code: str, action: str, quantity: int, price: float) -> OrderResult:
        """真实下单"""
        if not FUTU_AVAILABLE:
            return OrderResult(success=False, message='富途 SDK 未安装')
        
        if not self.trade_ctx:
            return OrderResult(success=False, message='交易上下文未连接')
        
        try:
            # 解析股票代码
            market, stock_code = code.split('.')
            
            # 设置订单参数
            trd_side = TrdSide.BUY if action == 'BUY' else TrdSide.SELL
            
            # 提交订单
            ret_code, ret_data = self.trade_ctx.place_order(
                price=price,
                qty=quantity,
                code=stock_code,
                trd_side=trd_side,
                order_type=OrderType.NORMAL,
                trd_env=TrdEnv.SIMULATE if self.simulate else TrdEnv.REAL
            )
            
            if ret_code == 0:
                order_id = ret_data['order_id'][0] if 'order_id' in ret_data else ''
                logger.info(f"下单成功：{code} {action} {quantity}股 @ {price} (订单 ID: {order_id})")
                
                return OrderResult(
                    success=True,
                    order_id=str(order_id),
                    message='下单成功',
                    filled_price=price,
                    filled_quantity=quantity
                )
            else:
                error_msg = ret_data.get('msg', '未知错误')
                logger.error(f"下单失败：{code} - {error_msg}")
                
                return OrderResult(success=False, message=error_msg)
        
        except Exception as e:
            logger.error(f"下单异常：{e}")
            return OrderResult(success=False, message=str(e))
    
    def _record_trade(self,
                     code: str,
                     strategy: str,
                     action: str,
                     entry_price: float,
                     quantity: int,
                     signal_confidence: float,
                     ai_score: float,
                     stop_loss: float,
                     take_profit: float,
                     position_size: float):
        """记录交易"""
        self.recorder.record_open(
            code=code,
            strategy=strategy,
            action=action,
            entry_price=entry_price,
            quantity=quantity,
            signal_confidence=signal_confidence,
            ai_score=ai_score,
            factor_scores={},
            stop_loss=stop_loss,
            take_profit=take_profit,
            position_size=position_size
        )
    
    def close_position(self, trade_id: str, exit_price: float, exit_reason: str) -> OrderResult:
        """
        平仓
        
        Args:
            trade_id: 交易 ID
            exit_price: 出场价
            exit_reason: 出场原因
        
        Returns:
            OrderResult: 订单结果
        """
        # 查找交易记录
        records = self.recorder.get_all_records()
        target_trade = None
        
        for record in records:
            if record.trade_id == trade_id:
                target_trade = record
                break
        
        if not target_trade:
            return OrderResult(success=False, message=f'未找到交易：{trade_id}')
        
        # 反向操作平仓
        action = 'SELL' if target_trade.action == 'BUY' else 'BUY'
        
        result = self.execute_signal(
            code=target_trade.code,
            action=action,
            quantity=target_trade.quantity,
            price=exit_price,
            strategy=target_trade.strategy,
            signal_confidence=0,
            ai_score=0
        )
        
        if result.success:
            # 更新交易记录
            self.recorder.record_close(
                trade_id=trade_id,
                exit_price=exit_price,
                exit_reason=exit_reason,
                lessons_learned=''
            )
        
        return result


# 全局单例
_executor = None

def get_auto_trade_executor(simulate: bool = True) -> AutoTradeExecutor:
    """获取自动交易执行器单例"""
    global _executor
    if _executor is None:
        _executor = AutoTradeExecutor(simulate=simulate)
    return _executor
