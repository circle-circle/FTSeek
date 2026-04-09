"""
ReportGenerator - 交易报告生成器

功能:
- 生成每日交易报告（日报）
- 生成每周交易报告（周报）
- 推送钉钉通知
- 支持多股票汇总

报告类型:
- 日报：当日交易明细、收益、持仓、累计收益
- 周报：周收益率、胜率、最大回撤、夏普比率

2026-03-14 修复：从 futu_cp2 的 trade_journal.json 读取实际交易数据
"""
import logging
import sqlite3
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path
import requests

logger = logging.getLogger("ftseek.v4.report_generator")
logger.propagate = False  # 防止日志重复

# futu_cp2 交易日志路径
FUTU_CP2_TRADE_JOURNAL = Path("/opt/futu_cp2/data/trade_journal.json")


class ReportGenerator:
    """交易报告生成器"""
    
    def __init__(self, db_path: str, dingtalk_webhook: str = None):
        """
        初始化报告生成器
        
        Args:
            db_path: 数据库路径
            dingtalk_webhook: 钉钉 webhook URL
        """
        self.db_path = db_path
        self.dingtalk_webhook = dingtalk_webhook
        
        # 股票名称映射
        self.stock_names = {
            '0700.HK': '腾讯',
            '9988.HK': '阿里',
            'MU': 'MU 美股',
            'TSLA': '特斯拉',
            'NVDA': '英伟达',
        }
        
        # 初始化数据库表
        self._init_tables()
        
        logger.info(f"报告生成器已初始化，数据库：{db_path}")
        logger.info(f"futu_cp2 交易日志：{FUTU_CP2_TRADE_JOURNAL}")
    
    def _init_tables(self):
        """初始化报告相关的数据库表"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 报告交易记录表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS report_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                action TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price REAL NOT NULL,
                pnl REAL DEFAULT 0,
                pnl_percent REAL DEFAULT 0,
                timestamp DATETIME NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 报告持仓表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS report_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                avg_cost REAL NOT NULL,
                current_price REAL NOT NULL,
                market_type TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 报告绩效表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS report_performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                date DATE NOT NULL,
                daily_return REAL DEFAULT 0,
                cumulative_return REAL DEFAULT 0,
                trade_count INTEGER DEFAULT 0,
                win_count INTEGER DEFAULT 0,
                max_drawdown REAL DEFAULT 0,
                sharpe_ratio REAL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(code, date)
            )
        """)
        
        # 创建索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_report_trades_code ON report_trades(code)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_report_trades_timestamp ON report_trades(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_report_performance_code ON report_performance(code)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_report_performance_date ON report_performance(date)")
        
        conn.commit()
        conn.close()
    
    def _get_stock_name(self, code: str) -> str:
        """获取股票名称"""
        return self.stock_names.get(code, code)
    
    def _load_futu_cp2_trades(self) -> List[Dict]:
        """
        从 futu_cp2 的 trade_journal.json 加载交易数据
        2026-03-14 新增：解决 FTSeek 数据库无实际交易数据的问题
        
        Returns:
            交易列表
        """
        if not FUTU_CP2_TRADE_JOURNAL.exists():
            logger.warning(f"futu_cp2 交易日志不存在：{FUTU_CP2_TRADE_JOURNAL}")
            return []
        
        try:
            with open(FUTU_CP2_TRADE_JOURNAL, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            trades = data.get('trades', [])
            logger.info(f"从 futu_cp2 加载 {len(trades)} 条交易记录")
            return trades
            
        except Exception as e:
            logger.error(f"加载 futu_cp2 交易日志失败：{e}")
            return []
    
    def _convert_trade_format(self, trade: Dict) -> Dict:
        """
        将 futu_cp2 交易格式转换为报告生成器格式
        
        Args:
            trade: futu_cp2 交易记录
        
        Returns:
            转换后的交易记录
        """
        # 转换股票代码格式
        ticker = trade.get('ticker', '')
        code = ticker.replace('.', '_') if '.' in ticker else ticker
        
        # 转换日期格式
        entry_date = trade.get('entry_date', '')
        if 'T' in entry_date:
            entry_date = entry_date.split('T')[0]
        
        exit_date = trade.get('exit_date', '')
        if exit_date and 'T' in exit_date:
            exit_date = exit_date.split('T')[0]
        
        return {
            'code': code,
            'action': 'BUY' if trade.get('trade_direction') == 'long' else 'SELL',
            'quantity': trade.get('position_size', 0),
            'price': trade.get('entry_price', 0),
            'pnl': trade.get('pnl', 0) or 0,
            'pnl_percent': trade.get('pnl_percent', 0) or 0,
            'timestamp': trade.get('entry_date', ''),
            'entry_date': entry_date,
            'exit_date': exit_date,
            'status': trade.get('status', 'open'),
            'notes': trade.get('notes', ''),
        }
    
    def _get_market_currency(self, market_type: str) -> str:
        """获取市场货币符号"""
        currencies = {
            'HK': '¥',
            'US': '$',
            'CN': '¥',
        }
        return currencies.get(market_type, '¥')
    
    def _get_market_type(self, code: str) -> str:
        """根据股票代码获取市场类型"""
        if code.endswith('.HK'):
            return 'HK'
        elif code in ['MU', 'TSLA', 'NVDA', 'AAPL', 'MSFT', 'GOOGL', 'AMZN']:
            return 'US'
        else:
            return 'CN'
    
    def _query_trades_by_date(self, code: str = None, start_date: str = None, end_date: str = None) -> List[Dict]:
        """
        查询交易记录
        2026-03-14 修复：优先从 futu_cp2 的 trade_journal.json 读取实际交易数据
        """
        # 2026-03-14 修复：首先尝试从 futu_cp2 加载交易数据
        futu_trades = self._load_futu_cp2_trades()
        
        if futu_trades:
            # 过滤和转换 futu_cp2 交易数据
            filtered_trades = []
            for trade in futu_trades:
                # 转换格式
                converted = self._convert_trade_format(trade)
                
                # 应用过滤条件
                if code and converted['code'] != code:
                    continue
                
                trade_date = converted.get('entry_date', '')
                if start_date and trade_date < start_date:
                    continue
                if end_date and trade_date > end_date:
                    continue
                
                filtered_trades.append(converted)
            
            if filtered_trades:
                logger.info(f"从 futu_cp2 获取 {len(filtered_trades)} 条交易记录")
                return filtered_trades
        
        # 备选方案：从本地数据库查询
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            query = "SELECT * FROM report_trades WHERE 1=1"
            params = []
            
            if code:
                query += " AND code = ?"
                params.append(code)
            
            if start_date:
                query += " AND timestamp >= ?"
                params.append(start_date)
            
            if end_date:
                query += " AND timestamp <= ?"
                params.append(end_date)
            
            query += " ORDER BY timestamp DESC"
            
            cursor.execute(query, params)
            trades = [dict(row) for row in cursor.fetchall()]
            
            conn.close()
            
            if trades:
                logger.info(f"从本地数据库获取 {len(trades)} 条交易记录")
            return trades
            
        except Exception as e:
            logger.error(f"从数据库查询交易失败：{e}")
            return []
    
    def _query_positions(self, code: str = None) -> List[Dict]:
        """
        查询持仓记录
        2026-03-14 修复：优先从 futu_cp2 的 trade_journal.json 推导持仓
        """
        # 2026-03-14 修复：从 futu_cp2 推导持仓（open 状态的交易）
        futu_trades = self._load_futu_cp2_trades()
        
        if futu_trades:
            # 按股票代码聚合持仓
            position_map = {}
            
            for trade in futu_trades:
                if trade.get('status') != 'open':
                    continue
                
                ticker = trade.get('ticker', '')
                direction = trade.get('trade_direction', 'long')
                position_size = trade.get('position_size', 0)
                entry_price = trade.get('entry_price', 0)
                
                # 合并同方向持仓
                key = f"{ticker}_{direction}"
                if key not in position_map:
                    position_map[key] = {
                        'code': ticker,
                        'direction': 'BUY' if direction == 'long' else 'SELL',
                        'quantity': 0,
                        'avg_cost': 0,
                        'total_cost': 0,
                        'market_type': 'US' if '.' not in ticker or ticker.split('.')[0] not in ['HK', 'SH', 'SZ'] else 'HK',
                    }
                
                pos = position_map[key]
                total_cost = pos['total_cost'] + (entry_price * position_size)
                total_qty = pos['quantity'] + position_size
                pos['quantity'] = total_qty
                pos['avg_cost'] = total_cost / total_qty if total_qty > 0 else 0
                pos['total_cost'] = total_cost
            
            # 转换为列表
            positions = list(position_map.values())
            
            # 应用代码过滤
            if code:
                positions = [p for p in positions if p['code'] == code]
            
            if positions:
                logger.info(f"从 futu_cp2 推导 {len(positions)} 条持仓记录")
                return positions
        
        # 备选方案：从本地数据库查询
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            query = "SELECT * FROM report_positions WHERE 1=1"
            params = []
            
            if code:
                query += " AND code = ?"
                params.append(code)
            
            cursor.execute(query, params)
            positions = [dict(row) for row in cursor.fetchall()]
            
            conn.close()
            
            if positions:
                logger.info(f"从本地数据库获取 {len(positions)} 条持仓记录")
            return positions
            
        except Exception as e:
            logger.error(f"从数据库查询持仓失败：{e}")
            return []
    
    def _query_performance(self, code: str = None, start_date: str = None, end_date: str = None) -> List[Dict]:
        """查询绩效记录"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        query = "SELECT * FROM report_performance WHERE 1=1"
        params = []
        
        if code:
            query += " AND code = ?"
            params.append(code)
        
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        
        query += " ORDER BY date DESC"
        
        cursor.execute(query, params)
        performance = [dict(row) for row in cursor.fetchall()]
        
        conn.close()
        return performance
    
    def _calculate_daily_stats(self, trades: List[Dict]) -> Dict:
        """计算当日交易统计"""
        if not trades:
            return {
                'total_trades': 0,
                'buy_count': 0,
                'sell_count': 0,
                'total_pnl': 0,
                'avg_pnl_percent': 0,
            }
        
        total_trades = len(trades)
        buy_count = sum(1 for t in trades if t['action'] == 'BUY')
        sell_count = sum(1 for t in trades if t['action'] == 'SELL')
        total_pnl = sum(t.get('pnl', 0) or 0 for t in trades)
        
        pnl_percents = [t.get('pnl_percent', 0) or 0 for t in trades if t.get('pnl', 0) != 0]
        avg_pnl_percent = sum(pnl_percents) / len(pnl_percents) if pnl_percents else 0
        
        return {
            'total_trades': total_trades,
            'buy_count': buy_count,
            'sell_count': sell_count,
            'total_pnl': total_pnl,
            'avg_pnl_percent': avg_pnl_percent,
        }
    
    def _calculate_weekly_stats(self, trades: List[Dict]) -> Dict:
        """计算周度交易统计"""
        if not trades:
            return {
                'total_trades': 0,
                'win_count': 0,
                'loss_count': 0,
                'win_rate': 0,
                'total_pnl': 0,
                'total_return': 0,
                'max_drawdown': 0,
                'sharpe_ratio': 0,
            }
        
        total_trades = len(trades)
        win_count = sum(1 for t in trades if t.get('pnl', 0) and t['pnl'] > 0)
        loss_count = sum(1 for t in trades if t.get('pnl', 0) and t['pnl'] < 0)
        win_rate = win_count / total_trades if total_trades > 0 else 0
        
        total_pnl = sum(t.get('pnl', 0) or 0 for t in trades)
        
        # 计算总收益率（简化）
        total_capital = sum(t['price'] * t['quantity'] for t in trades if t['action'] == 'BUY')
        total_return = total_pnl / total_capital if total_capital > 0 else 0
        
        # 计算最大回撤（简化：使用绩效表数据）
        max_drawdown = 0
        
        # 计算夏普比率（简化）
        if len(trades) > 1:
            returns = [t.get('pnl_percent', 0) or 0 for t in trades]
            avg_return = sum(returns) / len(returns)
            std_return = (sum((r - avg_return) ** 2 for r in returns) / len(returns)) ** 0.5
            sharpe_ratio = avg_return / std_return if std_return > 0 else 0
        else:
            sharpe_ratio = 0
        
        return {
            'total_trades': total_trades,
            'win_count': win_count,
            'loss_count': loss_count,
            'win_rate': win_rate,
            'total_pnl': total_pnl,
            'total_return': total_return,
            'max_drawdown': max_drawdown,
            'sharpe_ratio': sharpe_ratio,
        }
    
    def _format_daily_report(self, stock_data: Dict) -> str:
        """格式化日报内容"""
        lines = []
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"📊 交易日报 - {datetime.now().strftime('%Y-%m-%d')}")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("")
        
        total_pnl_hk = 0
        total_pnl_us = 0
        total_cumulative_hk = 0
        total_cumulative_us = 0
        
        for code, data in stock_data.items():
            name = self._get_stock_name(code)
            market_type = self._get_market_type(code)
            currency = self._get_market_currency(market_type)
            
            trades = data.get('trades', [])
            position = data.get('position')
            performance = data.get('performance', {})
            
            stats = self._calculate_daily_stats(trades)
            
            lines.append(f"【{name} {code}】")
            lines.append(f"├─ 模式：模拟")
            lines.append(f"├─ 当日交易：{stats['total_trades']} 笔 ({stats['buy_count']} 买 {stats['sell_count']} 卖)")
            
            # 当日收益
            pnl = stats['total_pnl']
            pnl_percent = stats['avg_pnl_percent'] * 100
            if pnl != 0:
                pnl_sign = "+" if pnl > 0 else ""
                lines.append(f"├─ 当日收益：{pnl_sign}{currency}{abs(pnl):,.0f} ({pnl_sign}{pnl_percent:.1f}%)")
            else:
                if trades and all(t['action'] == 'BUY' for t in trades):
                    lines.append(f"├─ 当日收益：{currency}0 (持仓中)")
                else:
                    lines.append(f"├─ 当日收益：{currency}0 (无交易)")
            
            # 累计收益
            cumulative_return = performance.get('cumulative_return', 0)
            cumulative_pnl = performance.get('cumulative_pnl', 0)
            if cumulative_return != 0:
                cum_sign = "+" if cumulative_return > 0 else ""
                icon = " ✅" if cumulative_return >= 0.05 else ""
                lines.append(f"├─ 累计收益：{cum_sign}{currency}{abs(cumulative_pnl):,.0f} ({cum_sign}{cumulative_return*100:.1f}%){icon}")
            else:
                lines.append(f"├─ 累计收益：{currency}0 (新建仓)")
            
            # 当前持仓
            if position:
                qty = position['quantity']
                price = position['current_price']
                lines.append(f"└─ 当前持仓：{qty} 股 @ {currency}{price:.1f}")
            else:
                lines.append(f"└─ 当前持仓：无持仓")
            
            lines.append("")
            
            # 累计收益用于汇总
            if market_type == 'HK':
                total_pnl_hk += pnl
                total_cumulative_hk += cumulative_pnl
            else:
                total_pnl_us += pnl
                total_cumulative_us += cumulative_pnl
        
        # 汇总
        lines.append("【汇总】")
        if total_cumulative_hk != 0 or total_cumulative_us != 0:
            lines.append(f"├─ 总收益：¥{total_cumulative_hk:,.0f} + ${total_cumulative_us:,.0f}")
        else:
            lines.append(f"├─ 总收益：¥0 + $0")
        
        # 目标进度（简化：假设目标 5%）
        target_progress = (total_cumulative_hk / 100000 * 100) if total_cumulative_hk > 0 else 0
        if target_progress > 0:
            lines.append(f"└─ 目标进度：{target_progress:.0f}% (目标 5%)")
        else:
            lines.append(f"└─ 目标进度：0% (目标 5%)")
        
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        
        return "\n".join(lines)
    
    def _format_weekly_report(self, stock_data: Dict) -> str:
        """格式化周报内容"""
        lines = []
        
        # 计算当前周数
        now = datetime.now()
        week_num = now.isocalendar()[1]
        year = now.year
        
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"📈 交易周报 - {year}-W{week_num:02d}")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("")
        
        suggestions = []
        
        for code, data in stock_data.items():
            name = self._get_stock_name(code)
            trades = data.get('trades', [])
            performance = data.get('performance', {})
            
            stats = self._calculate_weekly_stats(trades)
            
            # 从绩效表获取更准确的数据
            weekly_return = performance.get('daily_return', stats['total_return']) * 100
            win_rate = stats['win_rate'] * 100
            max_drawdown = performance.get('max_drawdown', stats['max_drawdown']) * 100
            sharpe_ratio = performance.get('sharpe_ratio', stats['sharpe_ratio'])
            
            lines.append(f"【{name} {code}】")
            lines.append(f"├─ 周收益率：{weekly_return:+.1f}%")
            lines.append(f"├─ 交易次数：{stats['total_trades']} 笔")
            lines.append(f"├─ 胜率：{win_rate:.0f}% ({stats['win_count']}/{stats['total_trades']})")
            lines.append(f"├─ 最大回撤：{max_drawdown:+.1f}%")
            lines.append(f"└─ 夏普比率：{sharpe_ratio:.2f}")
            lines.append("")
            
            # 生成建议
            if weekly_return >= 5:
                suggestions.append(f"{name}：连续 3 天>5%，建议切换实盘")
            elif weekly_return > 0:
                suggestions.append(f"{name}：保持模拟模式 (表现良好)")
            else:
                suggestions.append(f"{name}：保持模拟模式 (观察中)")
        
        # 建议部分
        if suggestions:
            lines.append("【建议】")
            for i, suggestion in enumerate(suggestions):
                prefix = "├─" if i < len(suggestions) - 1 else "└─"
                lines.append(f"{prefix} {suggestion}")
            lines.append("")
        
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        
        return "\n".join(lines)
    
    def generate_daily_report(self, code: str = None, date: str = None) -> str:
        """
        生成日报
        
        Args:
            code: 股票代码，None 表示所有股票
            date: 日期 (YYYY-MM-DD)，None 表示今天
            
        Returns:
            格式化的日报文本
        """
        if date is None:
            date = datetime.now().strftime('%Y-%m-%d')
        
        start_dt = f"{date} 00:00:00"
        end_dt = f"{date} 23:59:59"
        
        # 获取股票列表
        if code:
            codes = [code]
        else:
            # 获取所有有交易的股票
            trades = self._query_trades_by_date(start_date=start_dt, end_date=end_dt)
            codes = list(set(t['code'] for t in trades))
            
            # 如果没有今日交易，获取所有持仓的股票
            if not codes:
                positions = self._query_positions()
                codes = list(set(p['code'] for p in positions))
        
        # 收集每只股票的数据
        stock_data = {}
        for stock_code in codes:
            trades = self._query_trades_by_date(
                code=stock_code, 
                start_date=start_dt, 
                end_date=end_dt
            )
            
            positions = self._query_positions(code=stock_code)
            position = positions[0] if positions else None
            
            # 获取绩效数据
            performance_list = self._query_performance(code=stock_code, start_date=date, end_date=date)
            performance = performance_list[0] if performance_list else {}
            
            stock_data[stock_code] = {
                'trades': trades,
                'position': position,
                'performance': performance,
            }
        
        # 如果没有数据，生成空报告
        if not stock_data:
            return f"━━━━━━━━━━━━━━━━━━━━━━\n📊 交易日报 - {date}\n━━━━━━━━━━━━━━━━━━━━━━\n\n今日无交易记录\n\n━━━━━━━━━━━━━━━━━━━━━━"
        
        return self._format_daily_report(stock_data)
    
    def generate_weekly_report(self, code: str = None, week: str = None) -> str:
        """
        生成周报
        
        Args:
            code: 股票代码，None 表示所有股票
            week: 周数 (YYYY-Www)，None 表示当前周
            
        Returns:
            格式化的周报文本
        """
        if week is None:
            now = datetime.now()
            week_num = now.isocalendar()[1]
            week = f"{now.year}-W{week_num:02d}"
        
        # 计算周的起止日期
        # 简化：使用最近 7 天
        end_date = datetime.now()
        start_date = end_date - timedelta(days=6)
        
        start_dt = start_date.strftime('%Y-%m-%d 00:00:00')
        end_dt = end_date.strftime('%Y-%m-%d 23:59:59')
        
        # 获取股票列表
        if code:
            codes = [code]
        else:
            trades = self._query_trades_by_date(start_date=start_dt, end_date=end_dt)
            codes = list(set(t['code'] for t in trades))
            
            if not codes:
                positions = self._query_positions()
                codes = list(set(p['code'] for p in positions))
        
        # 收集每只股票的数据
        stock_data = {}
        for stock_code in codes:
            trades = self._query_trades_by_date(
                code=stock_code,
                start_date=start_dt,
                end_date=end_dt
            )
            
            # 获取本周绩效数据
            performance_list = self._query_performance(
                code=stock_code,
                start_date=start_date.strftime('%Y-%m-%d'),
                end_date=end_date.strftime('%Y-%m-%d')
            )
            
            # 合并绩效数据
            performance = {}
            if performance_list:
                # 取最新的绩效记录
                performance = performance_list[0]
            
            stock_data[stock_code] = {
                'trades': trades,
                'performance': performance,
            }
        
        # 如果没有数据，生成空报告
        if not stock_data:
            now = datetime.now()
            week_num = now.isocalendar()[1]
            return f"━━━━━━━━━━━━━━━━━━━━━━\n📈 交易周报 - {now.year}-W{week_num:02d}\n━━━━━━━━━━━━━━━━━━━━━━\n\n本周无交易记录\n\n━━━━━━━━━━━━━━━━━━━━━━"
        
        return self._format_weekly_report(stock_data)
    
    def send_daily_report(self, code: str = None) -> Dict[str, Any]:
        """
        生成并推送日报
        
        Args:
            code: 股票代码，None 表示所有股票
            
        Returns:
            推送结果
        """
        if not self.dingtalk_webhook:
            logger.warning("钉钉 webhook 未配置，无法推送日报")
            return {"success": False, "error": "Webhook not configured"}
        
        try:
            report = self.generate_daily_report(code=code)
            
            payload = {
                "msgtype": "markdown",
                "markdown": {
                    "title": "交易日报",
                    "text": report
                }
            }
            
            response = requests.post(self.dingtalk_webhook, json=payload, timeout=10)
            result = response.json()
            
            if result.get("errcode") == 0:
                logger.info(f"日报推送成功")
                return {"success": True, "result": result}
            else:
                logger.error(f"日报推送失败：{result}")
                return {"success": False, "error": result}
        
        except Exception as e:
            logger.error(f"日报推送异常：{e}")
            return {"success": False, "error": str(e)}
    
    def send_weekly_report(self, code: str = None) -> Dict[str, Any]:
        """
        生成并推送周报
        
        Args:
            code: 股票代码，None 表示所有股票
            
        Returns:
            推送结果
        """
        if not self.dingtalk_webhook:
            logger.warning("钉钉 webhook 未配置，无法推送周报")
            return {"success": False, "error": "Webhook not configured"}
        
        try:
            report = self.generate_weekly_report(code=code)
            
            payload = {
                "msgtype": "markdown",
                "markdown": {
                    "title": "交易周报",
                    "text": report
                }
            }
            
            response = requests.post(self.dingtalk_webhook, json=payload, timeout=10)
            result = response.json()
            
            if result.get("errcode") == 0:
                logger.info(f"周报推送成功")
                return {"success": True, "result": result}
            else:
                logger.error(f"周报推送失败：{result}")
                return {"success": False, "error": result}
        
        except Exception as e:
            logger.error(f"周报推送异常：{e}")
            return {"success": False, "error": str(e)}
    
    def record_trade(self, code: str, action: str, quantity: int, price: float, 
                    pnl: float = 0, pnl_percent: float = 0, timestamp: str = None):
        """
        记录交易（供外部调用）
        
        Args:
            code: 股票代码
            action: BUY/SELL
            quantity: 数量
            price: 价格
            pnl: 盈亏（卖出时填充）
            pnl_percent: 盈亏百分比
            timestamp: 时间戳，默认当前时间
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        timestamp = timestamp or datetime.now().isoformat()
        
        cursor.execute("""
            INSERT INTO report_trades (code, action, quantity, price, pnl, pnl_percent, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (code, action, quantity, price, pnl, pnl_percent, timestamp))
        
        conn.commit()
        conn.close()
        
        logger.debug(f"记录交易：{code} {action} {quantity}@{price}")
    
    def update_position(self, code: str, quantity: int, avg_cost: float, 
                       current_price: float, market_type: str = None):
        """
        更新持仓（供外部调用）
        
        Args:
            code: 股票代码
            quantity: 数量
            avg_cost: 成本价
            current_price: 当前价
            market_type: HK/US/CN
        """
        if market_type is None:
            market_type = self._get_market_type(code)
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        now = datetime.now().isoformat()
        
        # 检查是否已存在
        cursor.execute("SELECT id FROM report_positions WHERE code = ?", (code,))
        row = cursor.fetchone()
        
        if row:
            cursor.execute("""
                UPDATE report_positions 
                SET quantity = ?, avg_cost = ?, current_price = ?, updated_at = ?
                WHERE code = ?
            """, (quantity, avg_cost, current_price, now, code))
        else:
            cursor.execute("""
                INSERT INTO report_positions (code, quantity, avg_cost, current_price, market_type)
                VALUES (?, ?, ?, ?, ?)
            """, (code, quantity, avg_cost, current_price, market_type))
        
        conn.commit()
        conn.close()
        
        logger.debug(f"更新持仓：{code} {quantity}@{avg_cost}")
    
    def update_performance(self, code: str, date: str, daily_return: float,
                          cumulative_return: float, trade_count: int = 0,
                          win_count: int = 0, max_drawdown: float = 0,
                          sharpe_ratio: float = 0):
        """
        更新绩效（供外部调用）
        
        Args:
            code: 股票代码
            date: 日期 (YYYY-MM-DD)
            daily_return: 日收益率
            cumulative_return: 累计收益率
            trade_count: 交易次数
            win_count: 盈利次数
            max_drawdown: 最大回撤
            sharpe_ratio: 夏普比率
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT OR REPLACE INTO report_performance 
            (code, date, daily_return, cumulative_return, trade_count, win_count, max_drawdown, sharpe_ratio)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (code, date, daily_return, cumulative_return, trade_count, win_count, max_drawdown, sharpe_ratio))
        
        conn.commit()
        conn.close()
        
        logger.debug(f"更新绩效：{code} {date} {daily_return:.2%}")


# 全局单例
_report_generator: Optional[ReportGenerator] = None


def init_report_generator(db_path: str, dingtalk_webhook: str = None) -> ReportGenerator:
    """初始化报告生成器单例"""
    global _report_generator
    _report_generator = ReportGenerator(db_path, dingtalk_webhook)
    return _report_generator


def get_report_generator() -> Optional[ReportGenerator]:
    """获取报告生成器单例"""
    return _report_generator
