"""定时交易分析器 - 定期分析交易记录并生成报告"""
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from pathlib import Path

logger = logging.getLogger("ftseek_us.scheduled.trade_analyzer")

class TradeAnalyzer:
    """定时交易分析器"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.analysis_interval = config.get("analysis_interval_minutes", 30)  # 默认 30 分钟
        self.last_analysis_time: Optional[datetime] = None
    
    def should_analyze(self) -> bool:
        """判断是否应该进行分析"""
        if self.last_analysis_time is None:
            return True
        
        elapsed = datetime.now() - self.last_analysis_time
        return elapsed.total_seconds() >= self.analysis_interval * 60
    
    def analyze_trades(self, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        分析交易记录
        
        Args:
            trades: 交易记录列表
        
        Returns:
            分析报告
        """
        if not trades:
            return self._empty_report()
        
        # 1. 基础统计
        total_trades = len(trades)
        closed_trades = [t for t in trades if t.get('exit_price') is not None]
        open_trades = [t for t in trades if t.get('exit_price') is None]
        
        # 2. 盈亏统计
        total_pnl = sum(t.get('pnl', 0) for t in closed_trades)
        total_pnl_percent = sum(t.get('pnl_percent', 0) for t in closed_trades) / len(closed_trades) if closed_trades else 0
        
        # 3. 胜率
        winning_trades = [t for t in closed_trades if t.get('pnl', 0) > 0]
        win_rate = len(winning_trades) / len(closed_trades) if closed_trades else 0
        
        # 4. 持仓分析
        open_position_value = sum(
            t.get('quantity', 0) * t.get('current_price', 0)
            for t in open_trades
        )
        
        # 5. 风险指标
        max_drawdown = self._calc_max_drawdown(closed_trades)
        avg_holding_time = self._calc_avg_holding_time(closed_trades)
        
        # 6. 生成报告
        report = {
            "analysis_time": datetime.now().isoformat(),
            "summary": {
                "total_trades": total_trades,
                "closed_trades": len(closed_trades),
                "open_trades": len(open_trades),
                "total_pnl": total_pnl,
                "avg_pnl_percent": total_pnl_percent,
                "win_rate": win_rate,
                "open_position_value": open_position_value,
            },
            "risk": {
                "max_drawdown": max_drawdown,
                "avg_holding_time_hours": avg_holding_time,
            },
            "open_positions": self._summarize_positions(open_trades),
            "recent_trades": self._summarize_recent_trades(closed_trades[-5:]),
        }
        
        self.last_analysis_time = datetime.now()
        logger.info(f"交易分析完成：{total_trades}笔交易，胜率{win_rate:.1%}")
        
        return report
    
    def _empty_report(self) -> Dict[str, Any]:
        """空报告"""
        return {
            "analysis_time": datetime.now().isoformat(),
            "summary": {
                "total_trades": 0,
                "closed_trades": 0,
                "open_trades": 0,
                "total_pnl": 0,
                "avg_pnl_percent": 0,
                "win_rate": 0,
                "open_position_value": 0,
            },
            "risk": {
                "max_drawdown": 0,
                "avg_holding_time_hours": 0,
            },
            "open_positions": [],
            "recent_trades": [],
        }
    
    def _calc_max_drawdown(self, trades: List[Dict]) -> float:
        """计算最大回撤"""
        if not trades:
            return 0.0
        
        cumulative_pnl = 0
        max_pnl = 0
        max_drawdown = 0
        
        for trade in trades:
            cumulative_pnl += trade.get('pnl', 0)
            max_pnl = max(max_pnl, cumulative_pnl)
            drawdown = (max_pnl - cumulative_pnl) / max_pnl if max_pnl > 0 else 0
            max_drawdown = max(max_drawdown, drawdown)
        
        return max_drawdown
    
    def _calc_avg_holding_time(self, trades: List[Dict]) -> float:
        """计算平均持仓时间 (小时)"""
        if not trades:
            return 0.0
        
        holding_times = []
        for trade in trades:
            entry_time = trade.get('entry_time')
            exit_time = trade.get('exit_time')
            if entry_time and exit_time:
                if isinstance(entry_time, str):
                    entry_time = datetime.fromisoformat(entry_time)
                if isinstance(exit_time, str):
                    exit_time = datetime.fromisoformat(exit_time)
                holding_hours = (exit_time - entry_time).total_seconds() / 3600
                holding_times.append(holding_hours)
        
        return sum(holding_times) / len(holding_times) if holding_times else 0.0
    
    def _summarize_positions(self, open_trades: List[Dict]) -> List[Dict]:
        """汇总持仓"""
        summary = []
        for trade in open_trades:
            summary.append({
                "symbol": trade.get('symbol', 'N/A'),
                "direction": trade.get('direction', 'N/A'),
                "quantity": trade.get('quantity', 0),
                "entry_price": trade.get('entry_price', 0),
                "current_price": trade.get('current_price', 0),
                "unrealized_pnl": trade.get('unrealized_pnl', 0),
                "holding_hours": trade.get('holding_hours', 0),
            })
        return summary
    
    def _summarize_recent_trades(self, recent_trades: List[Dict]) -> List[Dict]:
        """汇总最近交易"""
        summary = []
        for trade in recent_trades:
            summary.append({
                "symbol": trade.get('symbol', 'N/A'),
                "direction": trade.get('direction', 'N/A'),
                "pnl": trade.get('pnl', 0),
                "pnl_percent": trade.get('pnl_percent', 0),
                "holding_hours": trade.get('holding_hours', 0),
            })
        return summary
