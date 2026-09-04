"""
盘后总结报告

美东时间 16:05 推送 (北京时间 04:05/05:05)
"""
import logging
from typing import Dict, List, Any
from datetime import datetime

logger = logging.getLogger(__name__)


class PostMarketSummaryReport:
    """盘后总结报告生成器"""
    
    def generate_report(self, trading_stats: Dict[str, Any]) -> str:
        """
        生成盘后总结报告
        
        Args:
            trading_stats: 交易统计数据
                {
                    "date": "2026-04-05",
                    "total_trades": int,
                    "wins": int,
                    "losses": int,
                    "total_pnl": float,
                    "win_rate": float,
                    "trades_detail": [...],
                    "current_positions": [...],
                    "best_trade": {...},
                    "worst_trade": {...},
                }
        
        Returns:
            报告文本
        """
        date = trading_stats.get("date", datetime.now().strftime("%Y-%m-%d"))
        total_trades = trading_stats.get("total_trades", 0)
        wins = trading_stats.get("wins", 0)
        losses = trading_stats.get("losses", 0)
        total_pnl = trading_stats.get("total_pnl", 0)
        win_rate = trading_stats.get("win_rate", 0)
        
        pnl_emoji = "💰" if total_pnl >= 0 else "🔴"
        pnl_sign = "+" if total_pnl >= 0 else ""
        
        lines = [
            "📊 FTSeek-US 盘后总结",
            f"📅 {date}",
            "",
            "📈 交易统计",
            f"• 总交易: {total_trades} 笔",
            f"• 盈利: {wins} 笔",
            f"• 亏损: {losses} 笔",
            f"• 胜率: {win_rate:.1%}",
            f"• 总盈亏: {pnl_emoji} {pnl_sign}${total_pnl:.2f}",
        ]
        
        # 今日交易明细
        trades_detail = trading_stats.get("trades_detail", [])
        if trades_detail:
            lines.append("")
            lines.append("📋 今日交易明细")
            
            for trade in trades_detail[:5]:
                symbol = trade.get("symbol", "?")
                action = trade.get("action", "?")
                pnl = trade.get("pnl", 0)
                pnl_pct = trade.get("pnl_pct", 0)
                
                trade_emoji = "✅" if pnl >= 0 else "❌"
                # DEB-20260904 #206: 单笔符号按自身盈亏, 原复用总盈亏 pnl_sign —
                # 总盈时亏损交易显示 '+$-50.00' 双符号
                _t_sign = "+" if pnl >= 0 else ""
                lines.append(f"• {trade_emoji} {symbol} {action}: {_t_sign}${pnl:.2f} ({pnl_pct:+.2%})")
        
        # 最佳/最差交易
        best_trade = trading_stats.get("best_trade")
        worst_trade = trading_stats.get("worst_trade")
        
        if best_trade or worst_trade:
            lines.append("")
            lines.append("🏆 今日最佳/最差")
            
            if best_trade:
                symbol = best_trade.get("symbol", "?")
                pnl_pct = best_trade.get("pnl_pct", 0)
                # DEB-20260904 #206: 硬编码 '+' 在全亏日显示 '+-3.50%', 改 :+ 自动带号
                lines.append(f"• 最佳: {symbol} {pnl_pct:+.2%}")
            
            if worst_trade:
                symbol = worst_trade.get("symbol", "?")
                pnl_pct = worst_trade.get("pnl_pct", 0)
                lines.append(f"• 最差: {symbol} {pnl_pct:.2%}")
        
        # 当前持仓
        current_positions = trading_stats.get("current_positions", [])
        if current_positions:
            lines.append("")
            lines.append("💼 当前持仓")
            
            for pos in current_positions[:5]:
                symbol = pos.get("symbol", "?")
                pnl_pct = pos.get("pnl_pct", 0)
                pnl_emoji = "✅" if pnl_pct >= 0 else "⚠️"
                holding_days = pos.get("holding_days", 0)
                
                lines.append(f"• {symbol}: {pnl_emoji} {pnl_pct:+.2%} (持仓{holding_days}天)")
        
        # 策略表现
        strategy_performance = trading_stats.get("strategy_performance", {})
        if strategy_performance:
            lines.append("")
            lines.append("🎯 策略表现")
            
            for strategy_name, perf in strategy_performance.items():
                trades = perf.get("trades", 0)
                win_rate = perf.get("win_rate", 0)
                pnl = perf.get("pnl", 0)
                
                lines.append(f"• {strategy_name}: {trades}笔, 胜率{win_rate:.1%}, 盈亏${pnl:.2f}")
        
        # 明日展望
        lines.append("")
        lines.append("🔮 明日展望")
        
        tomorrow_outlook = trading_stats.get("tomorrow_outlook", "")
        if tomorrow_outlook:
            lines.append(f"• {tomorrow_outlook}")
        else:
            lines.append("• 继续执行现有策略")
            lines.append("• 关注盘前信号扫描结果")
        
        # 改进建议
        improvements = trading_stats.get("improvements", [])
        if improvements:
            lines.append("")
            lines.append("💡 改进建议")
            for imp in improvements[:3]:
                lines.append(f"• {imp}")
        
        lines.append("")
        lines.append(f"⏰ 报告生成时间: {datetime.now().strftime('%H:%M')}")
        
        return "\n".join(lines)
