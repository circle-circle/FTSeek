"""
Futu Broker Adapter — 富途交易适配器

目的：封装所有 Futu API 调用差异（模拟/实盘），
业务层只调用 Adapter 接口，无需关心底层是模拟还是实盘。

设计原则：
1. 统一接口：模拟和实盘使用相同的方法签名
2. 内部路由：Adapter 内部根据 TradingEnvironment 路由到正确的账户/环境
3. 幂等性：所有下单操作支持幂等重试
4. 错误归一化：统一错误格式，便于上层处理
"""

import logging
import threading
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

from app.core.trading_env import get_trading_env, TradingMode
from app.core.interfaces import ITradeExecutor, OrderResult, Position, TradeSignal, OrderStatus

logger = logging.getLogger("ftseek.adapter.futu")


@dataclass
class FutuAccount:
    """富途账户信息"""
    account_id: int
    market: str  # "HK" | "US"
    is_simulate: bool


class FutuBrokerAdapter:
    """
    富途交易适配器
    
    统一管理 Futu OpenD 连接、账户路由、环境切换。
    
    替代方案:
    - 替代分散在各处的 OpenSecTradeContext 创建
    - 替代 if config.TRADE_ENV == 1 分支
    - 替代 _get_account_for_code() 散落逻辑
    """
    
    def __init__(self):
        self._env = get_trading_env()
        self._hk_ctx = None  # 港股交易上下文
        self._us_ctx = None  # 美股交易上下文
        self._lock = threading.Lock()
        
        # 缓存账户 ID
        self._account_cache: Dict[str, int] = {}
    
    # ===== 连接管理 =====
    
    def get_trade_context(self, market: str):
        """
        获取交易上下文（自动根据环境创建）
        
        替代: 各处手动创建 OpenSecTradeContext + 判断 TrdEnv
        """
        if market.upper() == "US":
            return self._get_us_context()
        else:
            return self._get_hk_context()
    
    def _get_hk_context(self):
        """获取港股交易上下文"""
        if self._hk_ctx is None:
            from futu import OpenSecTradeContext, TrdEnv
            acc_id = self._env.get_account_id("HK.", market="HK")
            self._hk_ctx = OpenSecTradeContext(
                filter_trdmarket="HK",
                host=self._get_opend_host(),
                port=self._get_opend_port(),
                security_firm=self._get_security_firm()
            )
            self._hk_ctx.set_ctx(acc_id, self._env.get_futu_trd_env())
            logger.info(
                f"港股交易上下文已创建，环境: {'模拟' if self._env.is_simulate else '实盘'}"
            )
        return self._hk_ctx
    
    def _get_us_context(self):
        """获取美股交易上下文"""
        if self._us_ctx is None:
            from futu import OpenSecTradeContext, TrdEnv
            acc_id = self._env.get_account_id("US.", market="US")
            self._us_ctx = OpenSecTradeContext(
                filter_trdmarket="US",
                host=self._get_opend_host(),
                port=self._get_opend_port(),
                security_firm=self._get_security_firm()
            )
            self._us_ctx.set_ctx(acc_id, self._env.get_futu_trd_env())
            logger.info(
                f"美股交易上下文已创建，环境: {'模拟' if self._env.is_simulate else '实盘'}"
            )
        return self._us_ctx
    
    def _get_opend_host(self) -> str:
        from app.core.config_container import OpenDConfig
        cfg = OpenDConfig.from_env()
        return cfg.host
    
    def _get_opend_port(self) -> int:
        from app.core.config_container import OpenDConfig
        cfg = OpenDConfig.from_env()
        return cfg.port
    
    def _get_security_firm(self):
        from futu import SecurityFirm
        return SecurityFirm.FUTUSECURITIES
    
    def get_account_id(self, code: str) -> int:
        """获取股票代码对应的账户 ID"""
        market = "US" if code.startswith("US.") else "HK"
        return self._env.get_account_id(code, market=market)
    
    # ===== 交易操作 =====
    
    def place_order(self, code: str, price: float, qty: int,
                    side: str, order_type: str = "NORMAL") -> OrderResult:
        """
        下单
        
        Args:
            code: 股票代码 (e.g., "HK.00700", "US.AAPL")
            price: 价格
            qty: 数量
            side: "BUY" 或 "SELL"
            order_type: 订单类型
        
        Returns:
            OrderResult
        """
        from futu import TrdSide, OrderType, TrdEnv, OrderStatus as FutuOrderStatus
        from app.core.trading_env import get_trading_env
        
        env = get_trading_env()
        trd_env = env.get_futu_trd_env()
        account_id = self.get_account_id(code)
        
        market = "US" if code.startswith("US.") else "HK"
        ctx = self.get_trade_context(market)
        
        try:
            # 映射 side
            futu_side = TrdSide.BUY if side == "BUY" else TrdSide.SELL
            
            # 映射 order type
            order_type_map = {
                "NORMAL": OrderType.NORMAL,
                "MARKET": OrderType.MARKET,
                "LIMIT": OrderType.NORMAL,
                "STOP": OrderType.ABSOLUTE_LIMIT,
            }
            futu_type = order_type_map.get(order_type, OrderType.NORMAL)
            
            # 下单
            ret, data = ctx.place_order(
                price=price,
                qty=qty,
                code=code,
                trd_side=futu_side,
                order_type=futu_type,
                trd_env=trd_env,
                acc_id=account_id,
            )
            
            if ret == 0:
                order_id = data.get("order_id", "")
                futu_order_id = data.get("futu_order_id", None)
                logger.info(
                    f"下单成功: {code} {side} {qty}@{price}, "
                    f"order_id={order_id}, futu_order_id={futu_order_id}, "
                    f"环境={'模拟' if env.is_simulate else '实盘'}"
                )
                return OrderResult(
                    success=True,
                    order_id=str(order_id),
                    futu_order_id=str(futu_order_id) if futu_order_id else None,
                )
            else:
                error_msg = data.get("error", "未知错误") if isinstance(data, dict) else str(data)
                logger.error(f"下单失败: {code} {side} {qty}@{price} - {error_msg}")
                return OrderResult(
                    success=False,
                    order_id="",
                    error_message=error_msg,
                )
        
        except Exception as e:
            logger.error(f"下单异常: {code} {side} {qty}@{price} - {e}", exc_info=True)
            return OrderResult(
                success=False,
                order_id="",
                error_message=str(e),
            )
    
    def cancel_order(self, order_id: str, code: str = "") -> bool:
        """撤单"""
        from futu import ModifyOrderOp, TrdEnv
        
        env = get_trading_env()
        trd_env = env.get_futu_trd_env()
        
        if code:
            account_id = self.get_account_id(code)
            market = "US" if code.startswith("US.") else "HK"
        else:
            account_id = self._env.get_account_id("", market="HK")
            market = "HK"
        
        ctx = self.get_trade_context(market)
        
        try:
            ret, data = ctx.modify_order(
                ModifyOrderOp.CANCEL,
                order_id,
                0, 0,
                trd_env=trd_env,
                acc_id=account_id,
            )
            success = ret == 0
            logger.info(f"撤单 {'成功' if success else '失败'}: order_id={order_id}")
            return success
        except Exception as e:
            logger.error(f"撤单异常: order_id={order_id} - {e}")
            return False
    
    def query_positions(self, market: Optional[str] = None) -> List[Dict[str, Any]]:
        """查询持仓"""
        from futu import TrdEnv
        
        env = get_trading_env()
        trd_env = env.get_futu_trd_env()
        
        results = []
        markets = [market.upper()] if market else ["HK", "US"]
        
        for m in markets:
            try:
                ctx = self.get_trade_context(m)
                account_id = self._env.get_account_id("", market=m)
                ret, data = ctx.position_list_query(
                    trd_env=trd_env,
                    acc_id=account_id,
                )
                if ret == 0:
                    results.extend(data.to_dict('records') if hasattr(data, 'to_dict') else [])
            except Exception as e:
                logger.error(f"查询{m}持仓失败: {e}")
        
        return results
    
    def query_orders(self, status: Optional[str] = None,
                     market: Optional[str] = None) -> List[Dict[str, Any]]:
        """查询订单"""
        from futu import TrdEnv
        
        env = get_trading_env()
        trd_env = env.get_futu_trd_env()
        
        results = []
        markets = [market.upper()] if market else ["HK", "US"]
        
        for m in markets:
            try:
                ctx = self.get_trade_context(m)
                account_id = self._env.get_account_id("", market=m)
                ret, data = ctx.order_list_query(
                    trd_env=trd_env,
                    acc_id=account_id,
                )
                if ret == 0:
                    records = data.to_dict('records') if hasattr(data, 'to_dict') else []
                    if status:
                        records = [r for r in records if r.get("order_status") == status]
                    results.extend(records)
            except Exception as e:
                logger.error(f"查询{m}订单失败: {e}")
        
        return results
    
    def query_deals(self, code: Optional[str] = None) -> List[Dict[str, Any]]:
        """查询成交"""
        from futu import TrdEnv
        
        env = get_trading_env()
        trd_env = env.get_futu_trd_env()
        
        results = []
        markets = ["HK", "US"]
        
        for m in markets:
            try:
                ctx = self.get_trade_context(m)
                account_id = self._env.get_account_id("", market=m)
                ret, data = ctx.deal_list_query(
                    trd_env=trd_env,
                    acc_id=account_id,
                    code=code,
                )
                if ret == 0:
                    results.extend(data.to_dict('records') if hasattr(data, 'to_dict') else [])
            except Exception as e:
                logger.error(f"查询{m}成交失败: {e}")
        
        return results
    
    # ===== 环境切换 =====
    
    def on_mode_switch(self, new_mode: TradingMode):
        """
        模式切换时重置上下文
        
        切换模拟↔实盘后，必须重新创建交易上下文
        """
        with self._lock:
            self._hk_ctx = None
            self._us_ctx = None
            self._account_cache.clear()
            self._env = get_trading_env()  # 刷新环境引用
            logger.info(f"FutuBrokerAdapter 已重置，新环境: {new_mode.name}")
    
    # ===== 环境信息 =====
    
    def get_env_info(self) -> Dict[str, Any]:
        """获取当前环境信息"""
        return {
            "mode": self._env.mode.name,
            "is_simulate": self._env.is_simulate,
            "is_real": self._env.is_real,
            "simulate_account_id": self._env._simulate_account_id,
            "real_account_hk": self._env._real_account_hk,
            "real_account_us": self._env._real_account_us,
            "capabilities": {
                "max_positions_multiplier": self._env.capabilities.max_positions_multiplier,
                "skip_futu_position_check": self._env.capabilities.skip_futu_position_check,
                "allow_negative_cash": self._env.capabilities.allow_negative_cash,
            }
        }


# ===== 全局单例（过渡期兼容） =====

_broker_adapter: Optional[FutuBrokerAdapter] = None
_adapter_lock = threading.Lock()


def get_broker_adapter() -> FutuBrokerAdapter:
    """获取全局 Broker Adapter 实例"""
    global _broker_adapter
    if _broker_adapter is None:
        with _adapter_lock:
            if _broker_adapter is None:
                _broker_adapter = FutuBrokerAdapter()
    return _broker_adapter


def reset_broker_adapter():
    """重置 Broker Adapter（用于模式切换后）"""
    global _broker_adapter
    with _adapter_lock:
        if _broker_adapter:
            _broker_adapter.on_mode_switch(get_trading_env().mode)
