"""
PaperTradingEngine - 模拟交易引擎

功能:
- 纸面交易执行 (买入/卖出)
- 持仓管理
- 盈亏计算
- 滑点处理 (默认 0.5%)

数据结构:
- PaperPosition: 持仓信息 (含未实现盈亏)
- PaperTrade: 交易记录

核心逻辑:
- 买入：成交价 = 市价 × 1.005 (上浮 0.5% 滑点)
- 卖出：成交价 = 市价 × 0.995 (下浮 0.5% 滑点)
- 支持多次买入 (计算平均成本)
- 支持部分卖出
"""
from datetime import datetime
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field
import logging
from pathlib import Path

from app.v4.models.trading_models import TradingDatabase, PaperPosition as DBPaperPosition, PaperTrade as DBPaperTrade
from app.ftlogging.simulation_logger import get_simulation_logger

logger = logging.getLogger("ftseek.v4.trader.paper_trader")
logger.propagate = False  # 防止日志重复

# 模拟盘日志记录器
_sim_logger = None


def _get_sim_logger():
    """延迟加载模拟盘日志记录器"""
    global _sim_logger
    if _sim_logger is None:
        try:
            _sim_logger = get_simulation_logger()
        except Exception as e:
            logger.warning(f"初始化模拟盘日志记录器失败：{e}")
    return _sim_logger


@dataclass
class PaperPosition:
    """
    模拟持仓数据类
    
    Attributes:
        code: 股票代码
        quantity: 持仓数量
        avg_cost: 平均成本 (含滑点)
        current_price: 当前价格
        entry_time: 首次建仓时间
        direction: 持仓方向 "long"=多头 / "short"=空头
        unrealized_pnl: 未实现盈亏
        unrealized_return: 未实现收益率 (%)
    """
    code: str
    quantity: int
    avg_cost: float
    current_price: float = 0.0
    entry_time: datetime = field(default_factory=datetime.now)
    direction: str = "long"  # "long" 多头 / "short" 空头
    
    @property
    def unrealized_pnl(self) -> float:
        """计算未实现盈亏"""
        if self.current_price == 0:
            return 0.0
        if self.direction == "short":
            # 空头盈亏：成本 - 当前价（价格下跌才赚钱）
            return (self.avg_cost - self.current_price) * self.quantity
        return (self.current_price - self.avg_cost) * self.quantity
    
    @property
    def unrealized_return(self) -> float:
        """计算未实现收益率 (%)"""
        if self.avg_cost == 0 or self.quantity == 0:
            return 0.0
        cost_basis = self.avg_cost * self.quantity
        if cost_basis == 0:
            return 0.0
        return (self.unrealized_pnl / cost_basis) * 100
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "code": self.code,
            "quantity": self.quantity,
            "avg_cost": self.avg_cost,
            "current_price": self.current_price,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "direction": self.direction,
            "unrealized_pnl": self.unrealized_pnl,
            "unrealized_return": self.unrealized_return,
        }


@dataclass
class PaperTrade:
    """
    交易记录数据类
    
    Attributes:
        code: 股票代码
        action: 操作类型 (BUY/SELL)
        quantity: 交易数量
        price: 实际成交价 (含滑点)
        timestamp: 交易时间
        pnl: 盈亏 (卖出时填充)
        return_pct: 收益率 (%)
    """
    code: str
    action: str  # BUY or SELL
    quantity: int
    price: float
    timestamp: datetime
    pnl: float = 0.0
    return_pct: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "code": self.code,
            "action": self.action,
            "quantity": self.quantity,
            "price": self.price,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "pnl": self.pnl,
            "return_pct": self.return_pct,
        }


class PaperTradingEngine:
    """
    模拟交易引擎
    
    功能:
    - 买入/卖出执行 (含滑点)
    - 持仓管理
    - 盈亏计算
    - 交易记录
    
    Args:
        db_session: 数据库会话 (可选，默认创建新连接)
        db_path: 数据库路径 (可选，仅在创建新连接时使用)
        slippage: 滑点比例 (默认 0.005 = 0.5%)
    """
    
    def __init__(self, db_session=None, db_path: str = None, slippage: float = 0.005):
        """
        初始化交易引擎
        
        Args:
            db_session: SQLAlchemy 会话 (可选)
            db_path: 数据库文件路径 (可选)
            slippage: 滑点比例 (默认 0.5%)
        """
        self.slippage = slippage
        self.db_session = db_session
        
        # 内存中存储当前价格
        self._current_prices: Dict[str, float] = {}
        
        # 如果未提供会话，创建数据库实例
        if db_session is None:
            if db_path:
                self.db = TradingDatabase(db_path)
            else:
                self.db = TradingDatabase()
            self.db_session = self.db.get_connection().__enter__()
            self._owns_session = True
        else:
            self.db = None
            self._owns_session = False
        
        # 初始化模拟盘日志记录器
        self.sim_logger = _get_sim_logger()
        
        logger.info(f"PaperTradingEngine 初始化完成，滑点：{slippage*100:.2f}%")
    
    def __del__(self):
        """清理资源"""
        if self._owns_session and hasattr(self, 'db_session') and self.db_session:
            try:
                self.db_session.close()
            except (AttributeError, Exception):
                pass
    
    def buy(self, code: str, quantity: int, market_price: float, 
            timestamp: datetime = None) -> PaperTrade:
        """
        买入股票
        
        Args:
            code: 股票代码
            quantity: 买入数量
            market_price: 市价
            timestamp: 交易时间 (默认当前时间)
            
        Returns:
            PaperTrade: 交易记录
            
        Raises:
            ValueError: 参数验证失败
        """
        # 参数验证
        if quantity <= 0:
            raise ValueError(f"买入数量必须大于 0: {quantity}")
        if market_price <= 0:
            raise ValueError(f"价格必须大于 0: {market_price}")
        
        timestamp = timestamp or datetime.now()
        
        # 计算成交价 (上浮滑点)
        exec_price = market_price * (1 + self.slippage)
        
        # 获取现有持仓
        db_position = self.db.get_position_by_code(code, direction="long")
        
        # 记录订单创建
        order_id = None
        if self.sim_logger:
            order_id = self.sim_logger.log_order_created(
                code=code,
                action="BUY",
                quantity=quantity,
                price=exec_price,
                order_type="MARKET"
            )
        
        if db_position:
            # 已有持仓，计算新的平均成本
            old_cost = db_position.avg_cost * db_position.quantity
            new_cost = exec_price * quantity
            total_quantity = db_position.quantity + quantity
            new_avg_cost = (old_cost + new_cost) / total_quantity
            
            # 更新持仓
            self.db.update_paper_position(
                position_id=db_position.id,
                quantity=total_quantity,
                avg_cost=new_avg_cost
            )
            logger.info(f"加仓 {code}: {quantity}股 @ {exec_price:.3f}, 新平均成本：{new_avg_cost:.3f}")
            
            # 记录持仓变化
            if self.sim_logger:
                self.sim_logger.log_position_change(
                    code=code,
                    change_type="ADD",
                    quantity=quantity,
                    avg_cost=new_avg_cost,
                    current_quantity=total_quantity,
                    unrealized_pnl=0.0
                )
        else:
            # 新建多头持仓
            self.db.create_paper_position(
                code=code,
                quantity=quantity,
                avg_cost=exec_price,
                entry_time=timestamp,
                direction="long"
            )
            logger.info(f"建仓 {code}: {quantity}股 @ {exec_price:.3f}")
            
            # 记录持仓变化
            if self.sim_logger:
                self.sim_logger.log_position_change(
                    code=code,
                    change_type="OPEN",
                    quantity=quantity,
                    avg_cost=exec_price,
                    current_quantity=quantity,
                    unrealized_pnl=0.0
                )
        
        # 记录交易
        db_trade = self.db.create_paper_trade(
            code=code,
            action="BUY",
            quantity=quantity,
            price=exec_price,
            timestamp=timestamp,
            pnl=0.0
        )
        
        # 记录成交
        if self.sim_logger:
            self.sim_logger.log_order_filled(
                order_id=order_id or "",
                code=code,
                action="BUY",
                filled_quantity=quantity,
                filled_price=exec_price,
                commission=0.0
            )
        
        return PaperTrade(
            code=code,
            action="BUY",
            quantity=quantity,
            price=exec_price,
            timestamp=timestamp,
            pnl=0.0,
            return_pct=0.0
        )
    
    def sell(self, code: str, quantity: int, market_price: float,
             timestamp: datetime = None) -> PaperTrade:
        """
        卖出股票
        
        Args:
            code: 股票代码
            quantity: 卖出数量
            market_price: 市价
            timestamp: 交易时间 (默认当前时间)
            
        Returns:
            PaperTrade: 交易记录
            
        Raises:
            ValueError: 参数验证失败或卖出数量超过持仓
        """
        # 参数验证
        if quantity <= 0:
            raise ValueError(f"卖出数量必须大于 0: {quantity}")
        if market_price <= 0:
            raise ValueError(f"价格必须大于 0: {market_price}")
        
        timestamp = timestamp or datetime.now()
        
        # 获取现有持仓
        db_position = self.db.get_position_by_code(code, direction="long")
        
        if not db_position:
            raise ValueError(f"无持仓可卖：{code}")
        
        if quantity > db_position.quantity:
            raise ValueError(
                f"卖出数量 ({quantity}) 超过持仓 ({db_position.quantity}): {code}"
            )
        
        # 计算成交价 (下浮滑点)
        exec_price = market_price * (1 - self.slippage)
        
        # 计算盈亏
        pnl = (exec_price - db_position.avg_cost) * quantity
        
        # 计算收益率
        cost_basis = db_position.avg_cost * quantity
        return_pct = (pnl / cost_basis * 100) if cost_basis > 0 else 0.0
        
        # 记录订单创建
        order_id = None
        if self.sim_logger:
            order_id = self.sim_logger.log_order_created(
                code=code,
                action="SELL",
                quantity=quantity,
                price=exec_price,
                order_type="MARKET"
            )
        
        # 更新持仓
        new_quantity = db_position.quantity - quantity
        change_type = "CLOSE" if new_quantity == 0 else "REDUCE"
        
        if new_quantity == 0:
            # 清空持仓
            self.db.delete_paper_position(db_position.id)
            logger.info(f"清仓 {code}: {quantity}股 @ {exec_price:.3f}, 盈亏：{pnl:.2f}")
            
            # 记录持仓变化
            if self.sim_logger:
                self.sim_logger.log_position_change(
                    code=code,
                    change_type=change_type,
                    quantity=quantity,
                    avg_cost=db_position.avg_cost,
                    current_quantity=0,
                    unrealized_pnl=pnl
                )
        else:
            # 部分卖出，平均成本不变
            self.db.update_paper_position(
                position_id=db_position.id,
                quantity=new_quantity
            )
            logger.info(f"减仓 {code}: {quantity}股 @ {exec_price:.3f}, 剩余：{new_quantity}股")
            
            # 记录持仓变化
            if self.sim_logger:
                self.sim_logger.log_position_change(
                    code=code,
                    change_type=change_type,
                    quantity=quantity,
                    avg_cost=db_position.avg_cost,
                    current_quantity=new_quantity,
                    unrealized_pnl=0.0
                )
        
        # 记录交易
        db_trade = self.db.create_paper_trade(
            code=code,
            action="SELL",
            quantity=quantity,
            price=exec_price,
            timestamp=timestamp,
            pnl=pnl
        )
        
        # 记录成交
        if self.sim_logger:
            self.sim_logger.log_order_filled(
                order_id=order_id or "",
                code=code,
                action="SELL",
                filled_quantity=quantity,
                filled_price=exec_price,
                commission=0.0
            )
        
        return PaperTrade(
            code=code,
            action="SELL",
            quantity=quantity,
            price=exec_price,
            timestamp=timestamp,
            pnl=pnl,
            return_pct=return_pct
        )
    
    def get_position(self, code: str, direction: str = None) -> Optional[PaperPosition]:
        """
        获取单个持仓
        
        Args:
            code: 股票代码
            direction: 持仓方向 "long"/"short"，None 时返回第一条匹配
            
        Returns:
            PaperPosition: 持仓信息，无持仓返回 None
        """
        db_position = self.db.get_position_by_code(code, direction=direction)
        
        if not db_position:
            return None
        
        # 获取存储的当前价格
        current_price = self._current_prices.get(code, 0.0)
        
        return PaperPosition(
            code=db_position.code,
            quantity=db_position.quantity,
            avg_cost=db_position.avg_cost,
            current_price=current_price,
            entry_time=db_position.entry_time,
            direction=db_position.direction
        )
    
    def get_all_positions(self) -> List[PaperPosition]:
        """
        获取所有持仓
        
        Returns:
            List[PaperPosition]: 持仓列表
        """
        db_positions = self.db.get_all_positions()
        
        return [
            PaperPosition(
                code=p.code,
                quantity=p.quantity,
                avg_cost=p.avg_cost,
                current_price=self._current_prices.get(p.code, 0.0),
                entry_time=p.entry_time,
                direction=p.direction
            )
            for p in db_positions
        ]
    
    def update_price(self, code: str, current_price: float):
        """
        更新股票当前价格 (用于计算未实现盈亏)
        
        Args:
            code: 股票代码
            current_price: 当前价格
        """
        self._current_prices[code] = current_price
        logger.debug(f"更新 {code} 当前价格：{current_price}")
    
    def calculate_pnl(self, position: PaperPosition) -> tuple:
        """
        计算持仓盈亏
        
        Args:
            position: 持仓对象
            
        Returns:
            tuple: (pnl, return_pct) 盈亏和收益率
        """
        if position.current_price == 0 or position.quantity == 0:
            return (0.0, 0.0)
        
        pnl = position.unrealized_pnl
        return_pct = position.unrealized_return
        
        return (pnl, return_pct)
    
    def get_trade_history(self, code: str) -> List[PaperTrade]:
        """
        获取单只股票的交易历史
        
        Args:
            code: 股票代码
            
        Returns:
            List[PaperTrade]: 交易记录列表 (最新在前)
        """
        db_trades = self.db.get_trades_by_code(code)
        
        return [
            PaperTrade(
                code=t.code,
                action=t.action,
                quantity=t.quantity,
                price=t.price,
                timestamp=t.timestamp,
                pnl=t.pnl if t.pnl is not None else 0.0,
                return_pct=0.0
            )
            for t in db_trades
        ]
    
    def get_all_trades(self, limit: int = 100) -> List[PaperTrade]:
        """
        获取所有交易记录
        
        Args:
            limit: 最大返回数量
            
        Returns:
            List[PaperTrade]: 交易记录列表
        """
        db_trades = self.db.get_recent_trades(limit)
        
        return [
            PaperTrade(
                code=t.code,
                action=t.action,
                quantity=t.quantity,
                price=t.price,
                timestamp=t.timestamp,
                pnl=t.pnl if t.pnl is not None else 0.0,
                return_pct=0.0
            )
            for t in db_trades
        ]
    
    def short(self, code: str, quantity: int, market_price: float,
              timestamp: datetime = None) -> PaperTrade:
        """
        做空股票（卖空建仓）
        
        Args:
            code: 股票代码
            quantity: 做空数量
            market_price: 当前市价
            timestamp: 交易时间（默认当前时间）
            
        Returns:
            PaperTrade: 交易记录
        """
        if quantity <= 0:
            raise ValueError(f"做空数量必须大于 0: {quantity}")
        if market_price <= 0:
            raise ValueError(f"价格必须大于 0: {market_price}")
        
        timestamp = timestamp or datetime.now()
        
        # 空头成交价：市价下浮滑点（以较低价卖出，模拟最差成交）
        exec_price = market_price * (1 - self.slippage)
        
        # 记录订单创建
        order_id = None
        if self.sim_logger:
            order_id = self.sim_logger.log_order_created(
                code=code,
                action="SHORT",
                quantity=quantity,
                price=exec_price,
                order_type="MARKET"
            )
        
        # 获取现有空头持仓
        db_position = self.db.get_position_by_code(code, direction="short")
        
        if db_position:
            # 已有空头持仓，计算新均价
            old_cost = db_position.avg_cost * db_position.quantity
            new_cost = exec_price * quantity
            total_quantity = db_position.quantity + quantity
            new_avg_cost = (old_cost + new_cost) / total_quantity
            self.db.update_paper_position(
                position_id=db_position.id,
                quantity=total_quantity,
                avg_cost=new_avg_cost
            )
            logger.info(f"加空 {code}: {quantity}股 @ {exec_price:.3f}, 新均价：{new_avg_cost:.3f}")
            
            # 记录持仓变化
            if self.sim_logger:
                self.sim_logger.log_position_change(
                    code=code,
                    change_type="ADD",
                    quantity=quantity,
                    avg_cost=new_avg_cost,
                    current_quantity=total_quantity,
                    unrealized_pnl=0.0
                )
        else:
            # 新建空头持仓
            self.db.create_paper_position(
                code=code,
                quantity=quantity,
                avg_cost=exec_price,
                entry_time=timestamp,
                direction="short"
            )
            logger.info(f"建空 {code}: {quantity}股 @ {exec_price:.3f}")
            
            # 记录持仓变化
            if self.sim_logger:
                self.sim_logger.log_position_change(
                    code=code,
                    change_type="OPEN",
                    quantity=quantity,
                    avg_cost=exec_price,
                    current_quantity=quantity,
                    unrealized_pnl=0.0
                )
        
        # 记录交易（action=SHORT 区分多头 BUY）
        self.db.create_paper_trade(
            code=code,
            action="SHORT",
            quantity=quantity,
            price=exec_price,
            timestamp=timestamp,
            pnl=0.0
        )
        
        # 记录成交
        if self.sim_logger:
            self.sim_logger.log_order_filled(
                order_id=order_id or "",
                code=code,
                action="SHORT",
                filled_quantity=quantity,
                filled_price=exec_price,
                commission=0.0
            )
        
        # 钉钉推送：建空通知
        try:
            from app.notifier.dingtalk import DingTalkNotifier
            notifier = DingTalkNotifier()
            notifier.send_text(
                f"[模拟盘] 📉 建空成功\n"
                f"标的：{code}\n"
                f"数量：{quantity} 股\n"
                f"成交价：{exec_price:.3f}\n"
                f"时间：{timestamp.strftime('%Y-%m-%d %H:%M:%S')}"
            )
        except Exception as e:
            logger.debug(f"建空钉钉推送失败（不影响交易）：{e}")
        
        return PaperTrade(
            code=code,
            action="SHORT",
            quantity=quantity,
            price=exec_price,
            timestamp=timestamp,
            pnl=0.0,
            return_pct=0.0
        )
    
    def cover(self, code: str, quantity: int, market_price: float,
              timestamp: datetime = None) -> PaperTrade:
        """
        平空仓（买入回补）
        
        Args:
            code: 股票代码
            quantity: 回补数量
            market_price: 当前市价
            timestamp: 交易时间（默认当前时间）
            
        Returns:
            PaperTrade: 交易记录，含空头盈亏
        """
        if quantity <= 0:
            raise ValueError(f"回补数量必须大于 0: {quantity}")
        if market_price <= 0:
            raise ValueError(f"价格必须大于 0: {market_price}")
        
        timestamp = timestamp or datetime.now()
        
        # 获取现有空头持仓
        db_position = self.db.get_position_by_code(code, direction="short")
        
        if not db_position:
            raise ValueError(f"无空头持仓可回补：{code}")
        
        if quantity > db_position.quantity:
            raise ValueError(
                f"回补数量 ({quantity}) 超过空头持仓 ({db_position.quantity}): {code}"
            )
        
        # 平空成交价：市价上浮滑点（以较高价买回，模拟最差成交）
        exec_price = market_price * (1 + self.slippage)
        
        # 空头盈亏：开仓价 - 平仓价，价格下跌则盈利
        pnl = (db_position.avg_cost - exec_price) * quantity
        
        # 收益率
        cost_basis = db_position.avg_cost * quantity
        return_pct = (pnl / cost_basis * 100) if cost_basis > 0 else 0.0
        
        # 记录订单创建
        order_id = None
        if self.sim_logger:
            order_id = self.sim_logger.log_order_created(
                code=code,
                action="COVER",
                quantity=quantity,
                price=exec_price,
                order_type="MARKET"
            )
        
        new_quantity = db_position.quantity - quantity
        change_type = "CLOSE" if new_quantity == 0 else "REDUCE"
        
        if new_quantity == 0:
            self.db.delete_paper_position(db_position.id)
            logger.info(f"平空 {code}: {quantity}股 @ {exec_price:.3f}, 盈亏：{pnl:+.2f}")
            
            # 记录持仓变化
            if self.sim_logger:
                self.sim_logger.log_position_change(
                    code=code,
                    change_type=change_type,
                    quantity=quantity,
                    avg_cost=db_position.avg_cost,
                    current_quantity=0,
                    unrealized_pnl=pnl
                )
        else:
            self.db.update_paper_position(
                position_id=db_position.id,
                quantity=new_quantity
            )
            logger.info(f"减空 {code}: {quantity}股 @ {exec_price:.3f}, 剩余：{new_quantity}股")
            
            # 记录持仓变化
            if self.sim_logger:
                self.sim_logger.log_position_change(
                    code=code,
                    change_type=change_type,
                    quantity=quantity,
                    avg_cost=db_position.avg_cost,
                    current_quantity=new_quantity,
                    unrealized_pnl=0.0
                )
        
        # 记录交易（action=COVER 区分多头 SELL）
        self.db.create_paper_trade(
            code=code,
            action="COVER",
            quantity=quantity,
            price=exec_price,
            timestamp=timestamp,
            pnl=pnl
        )
        
        # 记录成交
        if self.sim_logger:
            self.sim_logger.log_order_filled(
                order_id=order_id or "",
                code=code,
                action="COVER",
                filled_quantity=quantity,
                filled_price=exec_price,
                commission=0.0
            )
        
        # 钉钉推送：平空通知
        try:
            from app.notifier.dingtalk import DingTalkNotifier
            notifier = DingTalkNotifier()
            pnl_icon = "💰" if pnl >= 0 else "💸"
            notifier.send_text(
                f"[模拟盘] {pnl_icon} 平空成功\n"
                f"标的：{code}\n"
                f"数量：{quantity} 股\n"
                f"成交价：{exec_price:.3f}\n"
                f"空头盈亏：HK${pnl:+,.2f} ({return_pct:+.2f}%)\n"
                f"时间：{timestamp.strftime('%Y-%m-%d %H:%M:%S')}"
            )
        except Exception as e:
            logger.debug(f"平空钉钉推送失败（不影响交易）：{e}")
        
        return PaperTrade(
            code=code,
            action="COVER",
            quantity=quantity,
            price=exec_price,
            timestamp=timestamp,
            pnl=pnl,
            return_pct=return_pct
        )
    
    def close(self):
        """关闭数据库连接"""
        if self._owns_session and self.db_session:
            self.db_session.close()
            logger.info("PaperTradingEngine 数据库连接已关闭")