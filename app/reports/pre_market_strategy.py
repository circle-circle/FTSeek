"""
盘前策略展望报告

美东时间 09:00 推送 (北京时间 21:00/22:00)
"""
import logging
from typing import Dict, List, Any
from datetime import datetime

logger = logging.getLogger(__name__)


class PreMarketStrategyReport:
    """盘前策略展望报告生成器"""
    
    def __init__(self, watchlist: List[str] = None):
        """
        初始化
        
        Args:
            watchlist: 关注股票列表 (如为None则从配置文件自动读取)
        """
        if watchlist is None:
            # 从统一配置读取
            try:
                from app.utils.watchlist_manager import get_stock_symbols
                self.watchlist = get_stock_symbols()
            except Exception:
                # fallback到默认值
                self.watchlist = ["TSLA", "NVDA", "MU", "GOOG", "BABA"]
        else:
            self.watchlist = watchlist
    
    def generate_report(self, market_context: Dict[str, Any]) -> str:
        """
        生成盘前策略展望报告
        
        Args:
            market_context: 市场环境数据
                {
                    "vix": float,
                    "spx_change": float,
                    "spy_ma200_ratio": float,
                    "symbols_preview": [
                        {"symbol": "AAPL", "strength": 72, "direction": "LONG", ...},
                        ...
                    ]
                }
        
        Returns:
            报告文本
        """
        vix = market_context.get("vix", 20)
        spx_change = market_context.get("spx_change", 0)
        symbols_preview = market_context.get("symbols_preview", [])
        
        lines = [
            "🌅 FTSeek-US 盘前策略展望",
            f"📅 {datetime.now().strftime('%Y-%m-%d')}",
            "",
            "📊 大盘环境",
        ]
        
        # VIX状态
        if vix < 20:
            # DEB-20260904 #207: 原混入俄语 'благоприятный' (跨市场复制残留)
            vix_status = "低波动 (有利)"
        elif vix < 25:
            vix_status = "正常"
        elif vix < 30:
            vix_status = "偏高 (谨慎)"
        else:
            vix_status = "极高 (高风险)"
        
        lines.append(f"• VIX: {vix:.1f} - {vix_status}")
        
        # SPY涨跌
        spx_emoji = "📈" if spx_change >= 0 else "📉"
        lines.append(f"• SPY期货: {spx_emoji} {spx_change:+.2%}")
        
        # 市场状态
        spy_ratio = market_context.get("spy_ma200_ratio", 1.0)
        if spy_ratio > 1.05:
            trend = "偏多 (站在MA200上方)"
        elif spy_ratio < 0.95:
            trend = "偏空 (低于MA200)"
        else:
            trend = "震荡 (MA200附近)"
        
        lines.append(f"• 趋势: {trend}")
        
        # 今日关注股票
        if symbols_preview:
            lines.append("")
            lines.append("🎯 今日关注股票")
            
            # 按强度排序
            sorted_symbols = sorted(symbols_preview, key=lambda x: x.get("strength", 0), reverse=True)
            
            for i, sym in enumerate(sorted_symbols[:5], 1):
                symbol = sym.get("symbol", "?")
                strength = sym.get("strength", 0)
                direction = sym.get("direction", "NEUTRAL")
                
                dir_emoji = "📈" if direction == "LONG" else "📉" if direction == "SHORT" else "⚪"
                
                lines.append(f"{i}. {symbol} - 综合评分{strength}")
                
                # 各模型观点
                model_scores = sym.get("model_scores", [])
                if model_scores:
                    for ms in model_scores[:3]:
                        lines.append(f"   • {ms.get('model_id', '?')}: {ms.get('strength', 0)} - {ms.get('reasoning', '')}")
        
        # 当前持仓
        positions = market_context.get("current_positions", [])
        if positions:
            lines.append("")
            lines.append("💼 当前持仓")
            for pos in positions[:5]:
                symbol = pos.get("symbol", "?")
                pnl_pct = pos.get("pnl_pct", 0)
                pnl_emoji = "✅" if pnl_pct >= 0 else "⚠️"
                lines.append(f"• {symbol}: {pnl_emoji} {pnl_pct:+.2%}")
        
        # 风险提示
        lines.append("")
        lines.append("⚠️ 今日风险提示")
        
        if vix > 25:
            lines.append("• VIX偏高,建议降低仓位")
        
        if spx_change < -0.02:
            lines.append("• 大盘低开,注意风险控制")
        
        # 今日事件
        events = market_context.get("today_events", [])
        if events:
            lines.append("• 重要事件:")
            for event in events[:3]:
                lines.append(f"  - {event}")
        
        lines.append("")
        lines.append(f"⏰ 报告生成时间: {datetime.now().strftime('%H:%M')}")
        
        return "\n".join(lines)
