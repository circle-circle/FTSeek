"""
持仓管理器 (Position Manager)

负责跟踪模拟交易持仓状态，支持开仓、平仓、盈亏计算。
支持金字塔加仓逻辑。
"""

import sqlite3
from dataclasses import dataclass
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
import os
import yaml


@dataclass
class Position:
    """持仓数据模型"""
    id: int
    code: str
    action: str  # BUY/SELL
    quantity: int
    entry_price: float
    entry_time: str
    current_price: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    status: str = 'OPEN'
    exit_price: Optional[float] = None
    realized_pnl: Optional[float] = None


class PositionManager:
    """
    持仓管理器
    
    管理交易持仓的创建、查询、更新和平仓操作。
    使用 SQLite 数据库持久化存储持仓信息。
    支持金字塔加仓逻辑。
    """
    
    def __init__(self, db_path: str = None, config_path: str = None):
        """
        初始化持仓管理器
        
        Args:
            db_path: 数据库文件路径，默认使用 /opt/ftseek/FTSeek/data/ftseek.db
            config_path: 配置文件路径，默认使用 /opt/ftseek/FTSeek/config/position_strategy.yaml
        """
        self.db_path = db_path or '/opt/ftseek/FTSeek/data/ftseek.db'
        self.config_path = config_path or '/opt/ftseek/FTSeek/config/position_strategy.yaml'
        self._ensure_table()
        self._config = self._load_config()
    
    def _load_config(self) -> Dict[str, Any]:
        """
        加载配置文件
        
        Returns:
            配置字典，如果文件不存在则返回默认配置
        """
        default_config = {
            'position': {
                'initial_ratio': 0.50,
                'max_position_ratio': 0.20,
                'total_max_ratio': 0.80
            },
            'pyramid': {
                'enabled': True,
                'level1': {
                    'price_increase': 0.05,
                    'ratio': 0.30,
                    'min_confidence': 0.70
                },
                'level2': {
                    'price_increase': 0.10,
                    'ratio': 0.20,
                    'min_confidence': 0.70
                }
            },
            'cooldown': {
                'hours': 4,
                'max_trades_per_day': 3,
                'min_confidence': 0.60,
                'add_position_confidence': 0.70
            }
        }
        
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f)
                    # 合并配置，确保所有键都存在
                    return self._merge_config(default_config, config)
            except Exception:
                pass
        
        return default_config
    
    def _merge_config(self, default: Dict, loaded: Dict) -> Dict:
        """
        合并配置字典
        
        Args:
            default: 默认配置
            loaded: 加载的配置
            
        Returns:
            合并后的配置
        """
        result = default.copy()
        for key, value in loaded.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._merge_config(result[key], value)
            else:
                result[key] = value
        return result
    
    def _get_connection(self) -> sqlite3.Connection:
        """获取数据库连接"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def _ensure_table(self):
        """确保 positions 表存在"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    action TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    entry_time TEXT NOT NULL,
                    current_price REAL,
                    unrealized_pnl REAL,
                    status TEXT DEFAULT 'OPEN',
                    exit_price REAL,
                    realized_pnl REAL,
                    exit_time TEXT
                )
            ''')
            conn.commit()
        finally:
            conn.close()
    
    def get_position(self, code: str) -> Optional[Position]:
        """
        获取某股票的当前持仓 (status=OPEN)
        
        Args:
            code: 股票代码
            
        Returns:
            Position 对象，如果不存在则返回 None
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM positions 
                WHERE code = ? AND status = 'OPEN'
                LIMIT 1
            ''', (code,))
            row = cursor.fetchone()
            
            if row is None:
                return None
            
            return self._row_to_position(row)
        finally:
            conn.close()
    
    def get_all_positions(self) -> List[Position]:
        """
        获取所有持仓
        
        Returns:
            所有持仓的列表
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM positions 
                WHERE status = 'OPEN'
            ''')
            rows = cursor.fetchall()
            
            return [self._row_to_position(row) for row in rows]
        finally:
            conn.close()
    
    def create_position(self, code: str, action: str, quantity: int, 
                       entry_price: float, entry_time: str) -> int:
        """
        创建持仓记录
        
        Args:
            code: 股票代码
            action: 买卖方向 (BUY/SELL)
            quantity: 数量
            entry_price: 开仓价格
            entry_time: 开仓时间
            
        Returns:
            新建持仓的 ID
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO positions 
                (code, action, quantity, entry_price, entry_time, status)
                VALUES (?, ?, ?, ?, ?, 'OPEN')
            ''', (code, action, quantity, entry_price, entry_time))
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()
    
    def update_price(self, code: str, price: float):
        """
        更新当前价和浮动盈亏
        
        Args:
            code: 股票代码
            price: 当前价格
        """
        position = self.get_position(code)
        if position is None:
            return
        
        # 计算浮动盈亏
        unrealized_pnl = self.calculate_pnl(
            entry_price=position.entry_price,
            exit_price=price,
            quantity=position.quantity,
            action=position.action
        )
        
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE positions 
                SET current_price = ?, unrealized_pnl = ?
                WHERE code = ? AND status = 'OPEN'
            ''', (price, unrealized_pnl, code))
            conn.commit()
        finally:
            conn.close()
    
    def close_position(self, code: str, exit_price: float, exit_time: str) -> float:
        """
        平仓并返回已实现盈亏
        
        Args:
            code: 股票代码
            exit_price: 平仓价格
            exit_time: 平仓时间
            
        Returns:
            已实现盈亏 (正=盈利，负=亏损)
        """
        position = self.get_position(code)
        if position is None:
            return 0.0
        
        # 计算已实现盈亏
        realized_pnl = self.calculate_pnl(
            entry_price=position.entry_price,
            exit_price=exit_price,
            quantity=position.quantity,
            action=position.action
        )
        
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE positions 
                SET status = 'CLOSED',
                    exit_price = ?,
                    exit_time = ?,
                    realized_pnl = ?,
                    current_price = ?,
                    unrealized_pnl = ?
                WHERE code = ? AND status = 'OPEN'
            ''', (exit_price, exit_time, realized_pnl, exit_price, realized_pnl, code))
            conn.commit()
        finally:
            conn.close()
        
        return realized_pnl
    
    def partial_close_position(self, code: str, close_qty: int, exit_price: float, exit_time: str) -> float:
        """
        部分平仓并返回已实现盈亏
        
        Args:
            code: 股票代码
            close_qty: 平仓数量
            exit_price: 平仓价格
            exit_time: 平仓时间
            
        Returns:
            已实现盈亏 (正=盈利，负=亏损)
        """
        position = self.get_position(code)
        if position is None:
            return 0.0
        
        if close_qty >= position.quantity:
            # 如果平仓数量 >= 持仓数量，则全部平仓
            return self.close_position(code, exit_price, exit_time)
        
        # 计算已实现盈亏 (按平仓数量计算)
        realized_pnl = self.calculate_pnl(
            entry_price=position.entry_price,
            exit_price=exit_price,
            quantity=close_qty,
            action=position.action
        )
        
        # 更新持仓：减少数量，保持 OPEN 状态
        new_quantity = position.quantity - close_qty
        
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE positions 
                SET quantity = ?,
                    current_price = ?,
                    unrealized_pnl = ?,
                    exit_price = ?,
                    exit_time = ?,
                    realized_pnl = COALESCE(realized_pnl, 0) + ?
                WHERE code = ? AND status = 'OPEN'
            ''', (new_quantity, exit_price, realized_pnl, exit_price, exit_time, realized_pnl, code))
            conn.commit()
        finally:
            conn.close()
        
        return realized_pnl
    
    def calculate_pnl(self, entry_price: float, exit_price: float,
                      quantity: int, action: str) -> float:
        """
        计算盈亏
        
        Args:
            entry_price: 开仓价格
            exit_price: 平仓价格 (或当前价)
            quantity: 数量
            action: 买卖方向 (BUY/SELL)
            
        Returns:
            盈亏金额 (正=盈利，负=亏损)
        """
        if action == 'BUY':
            # 买入：价格上涨盈利
            pnl = (exit_price - entry_price) * quantity
        else:
            # 卖出 (做空)：价格下跌盈利
            pnl = (entry_price - exit_price) * quantity
        
        return round(pnl, 2)
    
    def check_stop_loss(self, code: str, current_price: float) -> bool:
        """
        检查是否触发止损 (-5%)
        
        Args:
            code: 股票代码
            current_price: 当前价格
            
        Returns:
            True 如果触发止损，否则 False
        """
        position = self.get_position(code)
        if position is None:
            return False
        
        avg_cost = position.entry_price
        pnl_rate = (current_price - avg_cost) / avg_cost
        return pnl_rate <= -0.05
    
    def check_take_profit(self, code: str, current_price: float) -> bool:
        """
        检查是否触发止盈 (+15%)
        
        Args:
            code: 股票代码
            current_price: 当前价格
            
        Returns:
            True 如果触发止盈，否则 False
        """
        position = self.get_position(code)
        if position is None:
            return False
        
        avg_cost = position.entry_price
        pnl_rate = (current_price - avg_cost) / avg_cost
        return pnl_rate >= 0.15
    
    def calculate_stop_loss_price(self, avg_cost: float) -> float:
        """
        计算止损价格
        
        Args:
            avg_cost: 平均成本价
            
        Returns:
            止损价格 (成本价的 95%)
        """
        return round(avg_cost * (1 - 0.05), 2)
    
    def calculate_take_profit_price(self, avg_cost: float) -> float:
        """
        计算止盈价格
        
        Args:
            avg_cost: 平均成本价
            
        Returns:
            止盈价格 (成本价的 115%)
        """
        return round(avg_cost * (1 + 0.15), 2)
    
    def _row_to_position(self, row: sqlite3.Row) -> Position:
        """将数据库行转换为 Position 对象"""
        return Position(
            id=row['id'],
            code=row['code'],
            action=row['action'],
            quantity=row['quantity'],
            entry_price=row['entry_price'],
            entry_time=row['entry_time'],
            current_price=row['current_price'],
            unrealized_pnl=row['unrealized_pnl'],
            status=row['status'],
            exit_price=row['exit_price'],
            realized_pnl=row['realized_pnl']
        )
    
    # ==================== 金字塔加仓相关方法 ====================
    
    def can_add_position(self, code: str, signal_confidence: float) -> bool:
        """
        检查是否可以加仓
        
        检查条件:
        1. 是否有持仓
        2. 仓位上限 (added < initial)
        3. 信号置信度
        4. 冷却时间
        5. 今日交易次数
        
        Args:
            code: 股票代码
            signal_confidence: 信号置信度 (0-1)
            
        Returns:
            bool: 是否可以加仓
        """
        # 1. 检查是否有持仓
        position = self.get_position(code)
        if position is None:
            return False
        
        # 2. 检查仓位上限 (需要查询历史交易记录计算已加仓数量)
        # 获取初始仓位和已加仓数量
        initial_quantity = self._get_initial_position_quantity(code)
        added_quantity = self._get_added_position_quantity(code)
        
        if added_quantity >= initial_quantity:
            return False
        
        # 3. 检查信号置信度
        min_confidence = self._config.get('cooldown', {}).get('add_position_confidence', 0.70)
        if signal_confidence < min_confidence:
            return False
        
        # 4. 检查冷却时间
        if self.is_in_cooling_period(code):
            return False
        
        # 5. 检查今日交易次数
        today_count = self.get_today_trade_count(code)
        max_trades = self._config.get('cooldown', {}).get('max_trades_per_day', 3)
        if today_count >= max_trades:
            return False
        
        return True
    
    def get_add_quantity(self, code: str, price_increase: float) -> int:
        """
        计算加仓数量 (金字塔)
        
        金字塔加仓规则:
        - 价格涨幅 >= 10%: 加仓 20%
        - 价格涨幅 >= 5%: 加仓 30%
        - 否则：不加仓
        
        Args:
            code: 股票代码
            price_increase: 价格涨幅 (小数，如 0.05 表示 5%)
            
        Returns:
            int: 加仓数量，无法加仓则返回 0
        """
        position = self.get_position(code)
        if position is None:
            return 0
        
        # 获取初始仓位
        initial_quantity = self._get_initial_position_quantity(code)
        if initial_quantity == 0:
            return 0
        
        pyramid_config = self._config.get('pyramid', {})
        
        # 检查金字塔加仓是否启用
        if not pyramid_config.get('enabled', True):
            return 0
        
        # 检查 level2 (涨幅 >= 10%)
        level2 = pyramid_config.get('level2', {})
        if price_increase >= level2.get('price_increase', 0.10):
            ratio = level2.get('ratio', 0.20)
            add_qty = int(initial_quantity * ratio)
            # 确保不超过剩余可加仓数量
            added_qty = self._get_added_position_quantity(code)
            max_add = initial_quantity - added_qty
            return min(add_qty, max_add)
        
        # 检查 level1 (涨幅 >= 5%)
        level1 = pyramid_config.get('level1', {})
        if price_increase >= level1.get('price_increase', 0.05):
            ratio = level1.get('ratio', 0.30)
            add_qty = int(initial_quantity * ratio)
            # 确保不超过剩余可加仓数量
            added_qty = self._get_added_position_quantity(code)
            max_add = initial_quantity - added_qty
            return min(add_qty, max_add)
        
        return 0
    
    def is_in_cooling_period(self, code: str, hours: int = None) -> bool:
        """
        检查是否在冷却期内
        
        Args:
            code: 股票代码
            hours: 冷却时间 (小时), 默认从配置文件读取
            
        Returns:
            bool: 是否在冷却期内
        """
        if hours is None:
            hours = self._config.get('cooldown', {}).get('hours', 4)
        
        # 获取最后交易时间
        last_trade_time = self._get_last_trade_time(code)
        if last_trade_time is None:
            return False
        
        # 计算冷却期结束时间
        now = datetime.now()
        cooldown_end = last_trade_time + timedelta(hours=hours)
        
        return now < cooldown_end
    
    def get_today_trade_count(self, code: str) -> int:
        """
        获取今日交易次数
        
        查询 paper_trades 表今日记录
        
        Args:
            code: 股票代码
            
        Returns:
            int: 今日交易次数
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            today = datetime.now().strftime('%Y-%m-%d')
            cursor.execute('''
                SELECT COUNT(*) FROM paper_trades 
                WHERE code = ? AND date(timestamp) = date(?)
            ''', (code, today))
            result = cursor.fetchone()
            return result[0] if result else 0
        finally:
            conn.close()
    
    def _get_initial_position_quantity(self, code: str) -> int:
        """
        获取初始仓位数量 (第一次开仓的数量)
        
        Args:
            code: 股票代码
            
        Returns:
            int: 初始仓位数量
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT quantity FROM positions 
                WHERE code = ? 
                ORDER BY entry_time ASC 
                LIMIT 1
            ''', (code,))
            result = cursor.fetchone()
            return result[0] if result else 0
        finally:
            conn.close()
    
    def _get_added_position_quantity(self, code: str) -> int:
        """
        获取已加仓数量 (通过 paper_trades 中的 ADD 类型记录计算)
        
        Args:
            code: 股票代码
            
        Returns:
            int: 已加仓数量
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT COALESCE(SUM(quantity), 0) FROM paper_trades 
                WHERE code = ? AND trade_type = 'ADD'
            ''', (code,))
            result = cursor.fetchone()
            return result[0] if result else 0
        finally:
            conn.close()
    
    def _get_last_trade_time(self, code: str) -> Optional[datetime]:
        """
        获取最后交易时间
        
        Args:
            code: 股票代码
            
        Returns:
            datetime: 最后交易时间，无交易记录返回 None
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT timestamp FROM paper_trades 
                WHERE code = ? 
                ORDER BY timestamp DESC 
                LIMIT 1
            ''', (code,))
            result = cursor.fetchone()
            if result and result[0]:
                # 兼容 Python 3.6: 使用 strptime 替代 fromisoformat
                timestamp_str = result[0]
                try:
                    # 尝试 ISO 格式 (带微秒)
                    return datetime.strptime(timestamp_str, '%Y-%m-%dT%H:%M:%S.%f')
                except ValueError:
                    try:
                        # 尝试 ISO 格式 (不带微秒)
                        return datetime.strptime(timestamp_str, '%Y-%m-%dT%H:%M:%S')
                    except ValueError:
                        # 尝试数据库默认格式
                        return datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
            return None
        finally:
            conn.close()
