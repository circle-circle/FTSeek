"""
模拟盘日志记录模块

功能:
- 独立的模拟盘日志记录
- 与实盘日志物理隔离 (不同文件/数据库表)
- 记录内容：
  - 交易信号 (时间、股票代码、方向、价格)
  - 下单记录 (模拟单号、数量、价格)
  - 成交记录 (成交价、成交量、时间)
  - 持仓变化
  - 账户资金变化
- 日志格式统一 (与 FTSeek-US 对齐)
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Any, List
from dataclasses import dataclass, asdict, field
from enum import Enum
import uuid

from app.storage.simulation_tables import get_simulation_database

logger = logging.getLogger("ftseek.logging.simulation_logger")
logger.propagate = False  # 防止日志重复


class SimulationLogType(str, Enum):
    """模拟盘日志类型"""
    SIGNAL = "signal"              # 交易信号
    ORDER = "order"                # 下单记录
    FILL = "fill"                  # 成交记录
    POSITION_CHANGE = "position"   # 持仓变化
    ACCOUNT_CHANGE = "account"     # 账户资金变化
    ERROR = "error"                # 错误日志


@dataclass
class SimulationLogEntry:
    """模拟盘日志条目"""
    log_type: str                  # 日志类型
    code: str                      # 股票代码
    action: str                    # 操作方向 BUY/SELL/SHORT/COVER
    price: float                   # 价格
    quantity: int                  # 数量
    order_id: Optional[str]        # 订单 ID
    signal_id: Optional[str]       # 信号 ID
    strategy: Optional[str]        # 策略名称
    message: str                   # 日志消息
    details: Optional[Dict]        # 详细信息
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class SignalLog:
    """交易信号日志"""
    signal_id: str
    code: str
    action: str
    price: float
    quantity: int
    strategy: str
    confidence: float
    reason: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class OrderLog:
    """下单记录日志"""
    order_id: str
    signal_id: Optional[str]
    code: str
    action: str
    quantity: int
    price: float
    order_type: str                # MARKET/LIMIT
    status: str                    # pending/submitted/filled/cancelled/failed
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    error_message: Optional[str] = None
    
    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class FillLog:
    """成交记录日志"""
    order_id: str
    fill_id: str
    code: str
    action: str
    filled_quantity: int
    filled_price: float
    commission: float
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class PositionChangeLog:
    """持仓变化日志"""
    code: str
    change_type: str               # OPEN/ADD/REDUCE/CLOSE
    quantity: int
    avg_cost: float
    current_quantity: int
    unrealized_pnl: float
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class AccountChangeLog:
    """账户资金变化日志"""
    account_id: str
    change_type: str               # DEPOSIT/WITHDRAW/PNL/FEE
    amount: float
    balance_before: float
    balance_after: float
    reason: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self) -> Dict:
        return asdict(self)


class SimulationLogger:
    """
    模拟盘日志记录器
    
    功能:
    - 独立的模拟盘日志记录
    - 与实盘日志物理隔离
    - 支持多种日志类型
    - 自动记录到数据库和文件
    """
    
    def __init__(self, log_dir: str = None):
        """
        初始化模拟盘日志记录器
        
        Args:
            log_dir: 日志文件目录，默认使用配置目录
        """
        if log_dir:
            self.log_dir = Path(log_dir)
        else:
            from app import config
            self.log_dir = config.DATA_DIR / "simulation_logs"
        
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        # 获取数据库实例
        self.db = get_simulation_database()
        
        # 文件日志配置
        self._setup_file_logger()
        
        logger.info(f"SimulationLogger 初始化完成，日志目录：{self.log_dir}")
    
    def _setup_file_logger(self):
        """配置文件日志"""
        self.file_logger = logging.getLogger("ftseek.simulation.file")
        self.file_logger.setLevel(logging.INFO)
        self.file_logger.propagate = False
        
        # 移除已有处理器
        self.file_logger.handlers = []
        
        # 文件处理器
        log_file = self.log_dir / f"simulation_{datetime.now().date().isoformat()}.log"
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(logging.INFO)
        
        # 格式化器
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(formatter)
        
        self.file_logger.addHandler(file_handler)
    
    def _get_log_file(self) -> Path:
        """获取今日日志文件"""
        return self.log_dir / f"simulation_{datetime.now().date().isoformat()}.log"
    
    # ==================== 信号日志 ====================
    
    def log_signal(self, code: str, action: str, price: float,
                   quantity: int, strategy: str, confidence: float,
                   reason: str, signal_id: str = None) -> str:
        """
        记录交易信号
        
        Args:
            code: 股票代码
            action: 操作方向 (BUY/SELL/SHORT/COVER)
            price: 信号价格
            quantity: 数量
            strategy: 策略名称
            confidence: 置信度
            reason: 信号原因
            signal_id: 信号 ID (可选，自动生成)
            
        Returns:
            str: 信号 ID
        """
        signal_id = signal_id or f"SIG-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        
        log_entry = SignalLog(
            signal_id=signal_id,
            code=code,
            action=action,
            price=price,
            quantity=quantity,
            strategy=strategy,
            confidence=confidence,
            reason=reason
        )
        
        # 记录到数据库
        self.db.log_simulation_event(
            log_type=SimulationLogType.SIGNAL.value,
            code=code,
            action=action,
            price=price,
            quantity=quantity,
            signal_id=signal_id,
            message=f"交易信号：{code} {action} {quantity}股 @ {price:.2f}",
            details=json.dumps(log_entry.to_dict(), ensure_ascii=False)
        )
        
        # 记录到文件
        self.file_logger.info(
            f"[SIGNAL] {signal_id} | {code} {action} {quantity}股 @ {price:.2f} | "
            f"策略={strategy} 置信度={confidence:.2f} | {reason}"
        )
        
        logger.info(f"记录信号日志：{signal_id}")
        return signal_id
    
    # ==================== 订单日志 ====================
    
    def log_order_created(self, code: str, action: str, quantity: int,
                          price: float, order_type: str = "MARKET",
                          signal_id: str = None, strategy: str = None) -> str:
        """
        记录订单创建
        
        Args:
            code: 股票代码
            action: 操作方向
            quantity: 数量
            price: 订单价格
            order_type: 订单类型 (MARKET/LIMIT)
            signal_id: 关联信号 ID
            strategy: 策略名称
            
        Returns:
            str: 订单 ID
        """
        order_id = f"ORD-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        
        log_entry = OrderLog(
            order_id=order_id,
            signal_id=signal_id,
            code=code,
            action=action,
            quantity=quantity,
            price=price,
            order_type=order_type,
            status='pending'
        )
        
        # 记录到数据库
        self.db.log_simulation_event(
            log_type=SimulationLogType.ORDER.value,
            code=code,
            action=action,
            price=price,
            quantity=quantity,
            order_id=order_id,
            signal_id=signal_id,
            message=f"订单创建：{code} {action} {quantity}股 @ {price:.2f} ({order_type})",
            details=json.dumps(log_entry.to_dict(), ensure_ascii=False)
        )
        
        self.file_logger.info(
            f"[ORDER CREATED] {order_id} | {code} {action} {quantity}股 @ {price:.2f} | "
            f"类型={order_type}" + (f" 信号={signal_id}" if signal_id else "")
        )
        
        logger.info(f"记录订单创建：{order_id}")
        return order_id
    
    def log_order_submitted(self, order_id: str, futu_order_id: str = None):
        """
        记录订单提交
        
        Args:
            order_id: 订单 ID
            futu_order_id: 富途订单 ID (可选)
        """
        # 记录到数据库
        self.db.log_simulation_event(
            log_type=SimulationLogType.ORDER.value,
            order_id=order_id,
            message=f"订单提交：{order_id}" + (f" -> 富途 {futu_order_id}" if futu_order_id else ""),
            details=json.dumps({'order_id': order_id, 'futu_order_id': futu_order_id}, ensure_ascii=False)
        )
        
        self.file_logger.info(
            f"[ORDER SUBMITTED] {order_id}" + (f" -> 富途 {futu_order_id}" if futu_order_id else "")
        )
        
        logger.info(f"记录订单提交：{order_id}")
    
    def log_order_filled(self, order_id: str, code: str, action: str,
                         filled_quantity: int, filled_price: float,
                         commission: float = 0, signal_id: str = None) -> str:
        """
        记录订单成交
        
        Args:
            order_id: 订单 ID
            code: 股票代码
            action: 操作方向
            filled_quantity: 成交数量
            filled_price: 成交价格
            commission: 佣金
            signal_id: 关联信号 ID
            
        Returns:
            str: 成交 ID
        """
        fill_id = f"FILL-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        
        log_entry = FillLog(
            order_id=order_id,
            fill_id=fill_id,
            code=code,
            action=action,
            filled_quantity=filled_quantity,
            filled_price=filled_price,
            commission=commission
        )
        
        # 记录到数据库
        self.db.log_simulation_event(
            log_type=SimulationLogType.FILL.value,
            code=code,
            action=action,
            price=filled_price,
            quantity=filled_quantity,
            order_id=order_id,
            signal_id=signal_id,
            message=f"订单成交：{code} {action} {filled_quantity}股 @ {filled_price:.2f}",
            details=json.dumps(log_entry.to_dict(), ensure_ascii=False)
        )
        
        self.file_logger.info(
            f"[FILL] {fill_id} | {order_id} | {code} {action} {filled_quantity}股 @ {filled_price:.2f} | "
            f"佣金={commission:.2f}"
        )
        
        logger.info(f"记录成交日志：{fill_id}")
        return fill_id
    
    def log_order_failed(self, order_id: str, error_message: str,
                         code: str = None, action: str = None):
        """
        记录订单失败
        
        Args:
            order_id: 订单 ID
            error_message: 错误信息
            code: 股票代码
            action: 操作方向
        """
        # 记录到数据库
        self.db.log_simulation_event(
            log_type=SimulationLogType.ERROR.value,
            code=code,
            action=action,
            order_id=order_id,
            message=f"订单失败：{order_id}",
            details=json.dumps({
                'order_id': order_id,
                'error_message': error_message
            }, ensure_ascii=False)
        )
        
        self.file_logger.error(
            f"[ORDER FAILED] {order_id} | 原因：{error_message}"
        )
        
        logger.error(f"记录订单失败：{order_id} - {error_message}")
    
    def log_order_cancelled(self, order_id: str, reason: str = ""):
        """
        记录订单取消
        
        Args:
            order_id: 订单 ID
            reason: 取消原因
        """
        self.db.log_simulation_event(
            log_type=SimulationLogType.ORDER.value,
            order_id=order_id,
            message=f"订单取消：{order_id}",
            details=json.dumps({
                'order_id': order_id,
                'reason': reason
            }, ensure_ascii=False)
        )
        
        self.file_logger.info(
            f"[ORDER CANCELLED] {order_id}" + (f" | 原因：{reason}" if reason else "")
        )
        
        logger.info(f"记录订单取消：{order_id}")
    
    # ==================== 持仓日志 ====================
    
    def log_position_change(self, code: str, change_type: str,
                            quantity: int, avg_cost: float,
                            current_quantity: int, unrealized_pnl: float):
        """
        记录持仓变化
        
        Args:
            code: 股票代码
            change_type: 变化类型 (OPEN/ADD/REDUCE/CLOSE)
            quantity: 变化数量
            avg_cost: 平均成本
            current_quantity: 当前持仓数量
            unrealized_pnl: 未实现盈亏
        """
        log_entry = PositionChangeLog(
            code=code,
            change_type=change_type,
            quantity=quantity,
            avg_cost=avg_cost,
            current_quantity=current_quantity,
            unrealized_pnl=unrealized_pnl
        )
        
        # 记录到数据库
        self.db.log_simulation_event(
            log_type=SimulationLogType.POSITION_CHANGE.value,
            code=code,
            action=change_type,
            price=avg_cost,
            quantity=quantity,
            message=f"持仓变化：{code} {change_type} {quantity}股，当前={current_quantity}股",
            details=json.dumps(log_entry.to_dict(), ensure_ascii=False)
        )
        
        pnl_icon = "💰" if unrealized_pnl >= 0 else "💸"
        self.file_logger.info(
            f"[POSITION {change_type}] {code} | 变动={quantity}股 | "
            f"成本={avg_cost:.2f} | 当前={current_quantity}股 | {pnl_icon} 未实现盈亏={unrealized_pnl:.2f}"
        )
        
        logger.info(f"记录持仓变化：{code} {change_type}")
    
    # ==================== 账户日志 ====================
    
    def log_account_change(self, account_id: str, change_type: str,
                           amount: float, balance_before: float,
                           balance_after: float, reason: str):
        """
        记录账户资金变化
        
        Args:
            account_id: 账户 ID
            change_type: 变化类型 (DEPOSIT/WITHDRAW/PNL/FEE)
            amount: 变化金额
            balance_before: 变化前余额
            balance_after: 变化后余额
            reason: 变化原因
        """
        log_entry = AccountChangeLog(
            account_id=account_id,
            change_type=change_type,
            amount=amount,
            balance_before=balance_before,
            balance_after=balance_after,
            reason=reason
        )
        
        # 记录到数据库
        self.db.log_simulation_event(
            log_type=SimulationLogType.ACCOUNT_CHANGE.value,
            message=f"账户变化：{change_type} {amount:.2f}, 余额={balance_after:.2f}",
            details=json.dumps(log_entry.to_dict(), ensure_ascii=False)
        )
        
        icon_map = {
            'DEPOSIT': '💵',
            'WITHDRAW': '💸',
            'PNL': '📈' if amount >= 0 else '📉',
            'FEE': '💳'
        }
        icon = icon_map.get(change_type, '💰')
        
        self.file_logger.info(
            f"[ACCOUNT {change_type}] {account_id} | {icon} {amount:+.2f} | "
            f"余额：{balance_before:.2f} -> {balance_after:.2f} | {reason}"
        )
        
        logger.info(f"记录账户变化：{account_id} {change_type}")
    
    # ==================== 查询接口 ====================
    
    def get_signals(self, code: str = None, days: int = 7) -> List[Dict]:
        """获取交易信号日志"""
        logs = self.db.get_simulation_logs(
            log_type=SimulationLogType.SIGNAL.value,
            code=code,
            days=days
        )
        return logs
    
    def get_orders(self, code: str = None, days: int = 7) -> List[Dict]:
        """获取订单日志"""
        logs = self.db.get_simulation_logs(
            log_type=SimulationLogType.ORDER.value,
            code=code,
            days=days
        )
        return logs
    
    def get_fills(self, code: str = None, days: int = 7) -> List[Dict]:
        """获取成交日志"""
        logs = self.db.get_simulation_logs(
            log_type=SimulationLogType.FILL.value,
            code=code,
            days=days
        )
        return logs
    
    def get_position_changes(self, code: str = None, days: int = 7) -> List[Dict]:
        """获取持仓变化日志"""
        logs = self.db.get_simulation_logs(
            log_type=SimulationLogType.POSITION_CHANGE.value,
            code=code,
            days=days
        )
        return logs
    
    def get_account_changes(self, days: int = 7) -> List[Dict]:
        """获取账户变化日志"""
        logs = self.db.get_simulation_logs(
            log_type=SimulationLogType.ACCOUNT_CHANGE.value,
            days=days
        )
        return logs
    
    def get_all_logs(self, code: str = None, days: int = 7) -> List[Dict]:
        """获取所有日志"""
        logs = self.db.get_simulation_logs(code=code, days=days)
        return logs
    
    def get_daily_summary(self, date: str = None) -> Dict:
        """
        获取每日汇总
        
        Args:
            date: 日期 (YYYY-MM-DD)，默认今天
            
        Returns:
            Dict: 汇总统计
        """
        if date is None:
            date = datetime.now().date().isoformat()
        
        logs = self.db.get_simulation_logs(days=1)
        
        summary = {
            'date': date,
            'signal_count': 0,
            'order_count': 0,
            'fill_count': 0,
            'position_changes': 0,
            'account_changes': 0,
            'error_count': 0,
            'total_commission': 0.0
        }
        
        for log in logs:
            log_type = log.get('log_type')
            if log_type == SimulationLogType.SIGNAL.value:
                summary['signal_count'] += 1
            elif log_type == SimulationLogType.ORDER.value:
                summary['order_count'] += 1
            elif log_type == SimulationLogType.FILL.value:
                summary['fill_count'] += 1
                details = log.get('details', '{}')
                try:
                    detail_dict = json.loads(details)
                    summary['total_commission'] += detail_dict.get('commission', 0)
                except (json.JSONDecodeError, ValueError):
                    pass
            elif log_type == SimulationLogType.POSITION_CHANGE.value:
                summary['position_changes'] += 1
            elif log_type == SimulationLogType.ACCOUNT_CHANGE.value:
                summary['account_changes'] += 1
            elif log_type == SimulationLogType.ERROR.value:
                summary['error_count'] += 1
        
        return summary


# 全局单例
_simulation_logger: Optional[SimulationLogger] = None


def get_simulation_logger(log_dir: str = None) -> SimulationLogger:
    """获取模拟盘日志记录器单例"""
    global _simulation_logger
    if _simulation_logger is None:
        _simulation_logger = SimulationLogger(log_dir)
    return _simulation_logger


def reset_simulation_logger():
    """重置模拟盘日志记录器单例 (用于测试)"""
    global _simulation_logger
    _simulation_logger = None
