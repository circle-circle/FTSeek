"""
持仓管理器 (Position Manager)

负责跟踪模拟交易持仓状态，支持开仓、平仓、盈亏计算。
"""

import sqlite3
from dataclasses import dataclass
from typing import List, Optional
from datetime import datetime
import os


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
    """
    
    def __init__(self, db_path: str = None):
        """
        初始化持仓管理器
        
        Args:
            db_path: 数据库文件路径，默认使用 /opt/ftseek/FTSeek/data/ftseek.db
        """
        self.db_path = db_path or '/opt/ftseek/FTSeek/data/ftseek.db'
        self._ensure_table()
    
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
