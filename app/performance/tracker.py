"""
绩效追踪器模块

核心功能：
- 配对买卖交易 (FIFO 原则)
- 计算简单收益率
- 获取当前持仓
- 计算总收益率

模拟盘模式：只读现有 signals 表，不修改数据库
"""
import sqlite3
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass
import logging

logger = logging.getLogger("ftseek.performance.tracker")


class PerformanceTracker:
    """
    绩效追踪器

    功能：
    - match_trades: 配对买卖交易 (FIFO)
    - calculate_return: 计算单笔收益率
    - get_position: 获取当前持仓
    - get_total_return: 计算总收益率
    """

    def __init__(self, db_path: str = None):
        """
        初始化绩效追踪器

        Args:
            db_path: 数据库路径，默认使用 /opt/ftseek/FTSeek/data/ftseek.db
        """
        if db_path is None:
            db_path = '/opt/ftseek/FTSeek/data/ftseek.db'

        self.db_path = db_path
        logger.info(f"PerformanceTracker initialized with db: {db_path}")

    def _get_connection(self) -> sqlite3.Connection:
        """获取数据库连接"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error as e:
            logger.error(f"Database connection failed: {e}")
            raise

    def _get_signals(self, code: str = None, action: str = None) -> List[Dict]:
        """
        获取交易信号

        Args:
            code: 股票代码，None 表示所有股票
            action: 交易方向 ('buy' 或 'sell', 'BUY' 或 'SELL'), None 表示所有

        Returns:
            信号列表，按时间排序
        """
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            query = """
                SELECT id, code, name, action, price, quantity, strategy,
                       created_at, executed_at, status
                FROM signals
                WHERE status = 'executed'
            """
            params = []

            if code:
                query += " AND code = ?"
                params.append(code)

            if action:
                # 支持大小写
                query += " AND LOWER(action) = ?"
                params.append(action.lower())

            query += " ORDER BY executed_at ASC"

            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as e:
            logger.error(f"Failed to get signals: {e}")
            raise
        finally:
            if conn:
                conn.close()

    def calculate_return(self, buy_price: float, sell_price: float,
                         quantity: int) -> Dict:
        """
        计算单笔交易收益率

        简单收益率公式：(卖出价 - 买入价) / 买入价 × 100%

        Args:
            buy_price: 买入价格
            sell_price: 卖出价格
            quantity: 数量

        Returns:
            {
                'return_rate': float,  # 收益率 (%)
                'profit': float,       # 收益金额
                'cost': float          # 成本
            }
        """
        if quantity == 0:
            return {
                'return_rate': 0.0,
                'profit': 0.0,
                'cost': 0.0
            }

        # 成本 = 买入价 × 数量
        cost = buy_price * quantity

        # 收益 = (卖出价 - 买入价) × 数量
        profit = (sell_price - buy_price) * quantity

        # 收益率 = (卖出价 - 买入价) / 买入价 × 100%
        if buy_price > 0:
            return_rate = (sell_price - buy_price) / buy_price * 100
        else:
            return_rate = 0.0

        return {
            'return_rate': return_rate,
            'profit': profit,
            'cost': cost
        }

    def match_trades(self, code: str = None) -> List[Dict]:
        """
        配对买卖交易

        使用 FIFO (先进先出) 原则匹配买入和卖出信号：
        1. 按时间顺序获取所有已执行的买入和卖出信号
        2. 对于每笔卖出，匹配最早未完全匹配的买入
        3. 支持部分卖出（卖出数量 < 买入数量）

        Args:
            code: 股票代码，None 表示所有股票

        Returns:
            配对列表，每个配对包含：
            {
                'buy_signal_id': str,
                'sell_signal_id': str,
                'code': str,
                'buy_price': float,
                'sell_price': float,
                'quantity': int,
                'return_rate': float,
                'profit': float,
                'cost': float,
                'open_time': str,
                'close_time': str,
                'status': str
            }
        """
        pairs = []

        # 获取所有已执行的信号
        buy_signals = self._get_signals(code=code, action='buy')
        sell_signals = self._get_signals(code=code, action='sell')

        if not buy_signals or not sell_signals:
            return pairs

        # 按股票代码分组处理
        codes = set()
        if code:
            codes.add(code)
        else:
            for sig in buy_signals:
                codes.add(sig['code'])

        for stock_code in codes:
            # 获取该股票的买卖信号
            stock_buys = [s for s in buy_signals if s['code'] == stock_code]
            stock_sells = [s for s in sell_signals if s['code'] == stock_code]

            # 追踪每笔买入的剩余数量
            buy_remaining = {s['id']: s['quantity'] for s in stock_buys}

            # 对每笔卖出进行匹配
            for sell in stock_sells:
                sell_remaining = sell['quantity']

                # FIFO: 按时间顺序匹配买入
                for buy in stock_buys:
                    if sell_remaining <= 0:
                        break

                    if buy_remaining[buy['id']] <= 0:
                        continue

                    # 计算匹配数量
                    match_qty = min(buy_remaining[buy['id']], sell_remaining)

                    # 计算收益率
                    return_info = self.calculate_return(
                        buy_price=buy['price'],
                        sell_price=sell['price'],
                        quantity=match_qty
                    )

                    # 创建配对记录
                    pair = {
                        'buy_signal_id': buy['id'],
                        'sell_signal_id': sell['id'],
                        'code': stock_code,
                        'buy_price': buy['price'],
                        'sell_price': sell['price'],
                        'quantity': match_qty,
                        'return_rate': return_info['return_rate'],
                        'profit': return_info['profit'],
                        'cost': return_info['cost'],
                        'open_time': buy['executed_at'],
                        'close_time': sell['executed_at'],
                        'status': 'closed'
                    }

                    pairs.append(pair)

                    # 更新剩余数量
                    buy_remaining[buy['id']] -= match_qty
                    sell_remaining -= match_qty

        logger.info(f"Matched {len(pairs)} trade pairs")
        return pairs

    def get_position(self, code: str = None) -> Dict:
        """
        获取当前持仓

        通过计算未匹配的买入信号来确定当前持仓：
        持仓数量 = 总买入数量 - 总卖出数量 (已匹配部分)

        Args:
            code: 股票代码，None 表示所有股票（返回汇总）

        Returns:
            {
                'code': str,
                'quantity': int,
                'avg_cost': float,
                'current_value': float
            }
        """
        if code is None:
            # 如果没有指定代码，返回空持仓
            return {
                'code': '',
                'quantity': 0,
                'avg_cost': 0.0,
                'current_value': 0.0
            }

        # 获取该股票的所有已执行信号
        buy_signals = self._get_signals(code=code, action='buy')
        sell_signals = self._get_signals(code=code, action='sell')

        if not buy_signals:
            return {
                'code': code,
                'quantity': 0,
                'avg_cost': 0.0,
                'current_value': 0.0
            }

        # 计算总买入和总卖出
        total_buy_qty = sum(s['quantity'] for s in buy_signals)
        total_sell_qty = sum(s['quantity'] for s in sell_signals)

        # 当前持仓数量
        quantity = max(0, total_buy_qty - total_sell_qty)

        if quantity == 0:
            return {
                'code': code,
                'quantity': 0,
                'avg_cost': 0.0,
                'current_value': 0.0
            }

        # 计算平均成本 (使用 FIFO 原则，剩余的是最近的买入)
        # 简化处理：使用加权平均
        total_cost = sum(s['price'] * s['quantity'] for s in buy_signals)
        avg_cost = total_cost / total_buy_qty if total_buy_qty > 0 else 0.0

        # 当前价值 = 平均成本 × 持仓数量
        current_value = avg_cost * quantity

        return {
            'code': code,
            'quantity': quantity,
            'avg_cost': avg_cost,
            'current_value': current_value
        }

    def get_performance_stats(self, code: str, days: int = 30) -> Dict:
        """
        获取绩效统计 (指定股票和天数)

        Args:
            code: 股票代码
            days: 统计天数，默认 30 天

        Returns:
            {
                'code': str,
                'total_trades': int,
                'winning_trades': int,
                'win_rate': float,
                'total_pnl': float,
                'total_return': float,
                'max_drawdown': float
            }
        """
        # 获取该股票的交易配对
        pairs = self.match_trades(code=code)
        
        if not pairs:
            return {
                'code': code,
                'total_trades': 0,
                'winning_trades': 0,
                'win_rate': 0.0,
                'total_pnl': 0.0,
                'total_return': 0.0,
                'max_drawdown': 0.0
            }
        
        # 过滤指定天数内的交易
        cutoff_time = datetime.now() - timedelta(days=days)
        recent_pairs = []
        
        for pair in pairs:
            try:
                close_time_str = pair['close_time'].replace('T', ' ')
                if '.' in close_time_str:
                    close_time_str = close_time_str.split('.')[0]
                close_time = datetime.strptime(close_time_str, '%Y-%m-%d %H:%M:%S')
                
                if close_time >= cutoff_time:
                    recent_pairs.append(pair)
            except (ValueError, TypeError):
                continue
        
        if not recent_pairs:
            return {
                'code': code,
                'total_trades': 0,
                'winning_trades': 0,
                'win_rate': 0.0,
                'total_pnl': 0.0,
                'total_return': 0.0,
                'max_drawdown': 0.0
            }
        
        # 统计交易数据
        total_trades = len(recent_pairs)
        winning_trades = sum(1 for p in recent_pairs if p['return_rate'] > 0)
        
        # 胜率
        win_rate = winning_trades / total_trades if total_trades > 0 else 0.0
        
        # 总盈亏和总收益率
        total_pnl = sum(p['profit'] for p in recent_pairs)
        total_return = sum(p['return_rate'] for p in recent_pairs)
        
        # 计算最大回撤 (简化：使用最小收益率)
        returns = [p['return_rate'] for p in recent_pairs]
        max_drawdown = min(returns) if returns else 0.0
        
        return {
            'code': code,
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'win_rate': win_rate,
            'total_pnl': total_pnl,
            'total_return': total_return,
            'max_drawdown': max_drawdown
        }
    
    def get_total_return(self, days: int = 7) -> Dict:
        """
        计算总收益率 (指定天数内)

        Args:
            days: 统计天数，默认 7 天

        Returns:
            {
                'total_trades': int,
                'win_rate': float,     # 胜率 (0-1)
                'total_return': float, # 总收益率 (%)
                'avg_return': float    # 平均收益率 (%)
            }
        """
        # 获取所有配对
        pairs = self.match_trades()

        if not pairs:
            return {
                'total_trades': 0,
                'win_rate': 0.0,
                'total_return': 0.0,
                'avg_return': 0.0
            }

        # 过滤指定天数内的交易
        cutoff_time = datetime.now() - timedelta(days=days)
        recent_pairs = []

        for pair in pairs:
            try:
                # Python 3.6 兼容：使用 strptime 解析 ISO 格式
                close_time_str = pair['close_time'].replace('T', ' ')
                if '.' in close_time_str:
                    close_time_str = close_time_str.split('.')[0]
                close_time = datetime.strptime(
                    close_time_str, '%Y-%m-%d %H:%M:%S'
                )

                if close_time >= cutoff_time:
                    recent_pairs.append(pair)
            except (ValueError, TypeError):
                # 时间格式错误，跳过
                continue

        if not recent_pairs:
            return {
                'total_trades': 0,
                'win_rate': 0.0,
                'total_return': 0.0,
                'avg_return': 0.0
            }

        # 统计交易数据
        total_trades = len(recent_pairs)
        winning_trades = sum(1 for p in recent_pairs if p['return_rate'] > 0)

        # 胜率
        win_rate = winning_trades / total_trades if total_trades > 0 else 0.0

        # 总收益率 (简单平均)
        total_return = sum(p['return_rate'] for p in recent_pairs)

        # 平均收益率
        avg_return = total_return / total_trades if total_trades > 0 else 0.0

        return {
            'total_trades': total_trades,
            'win_rate': win_rate,
            'total_return': total_return,
            'avg_return': avg_return
        }


# 全局单例
_tracker: Optional[PerformanceTracker] = None


def get_performance_tracker(db_path: str = None) -> PerformanceTracker:
    """
    获取绩效追踪器单例

    Args:
        db_path: 数据库路径，可选

    Returns:
        PerformanceTracker 实例
    """
    global _tracker
    if _tracker is None:
        _tracker = PerformanceTracker(db_path=db_path)
    return _tracker
