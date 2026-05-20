"""
FTSeek-US 财报感知信号修正器 (Earnings-Aware Signal Modifier)

职责:
1. 在信号生成前，根据财报状态调整信号权重
2. 财报前: 降低信号权重 (避免财报噪音)
3. 财报后: 放大信号权重 (捕捉动量)
4. 提供仓位调整建议

集成点:
- app.pipeline.signal_pipeline._process_symbol_impl (信号生成前)
- risk pipeline 的 PositionLimitCheck
- exit monitor 的 Flash Exit 阈值调整

作者: FTSeek-US Team
版本: 1.0 (2026-04-29)
"""

import logging
from typing import Dict, Optional, Tuple
from datetime import datetime, timedelta

logger = logging.getLogger("ftseek.us_pipeline.earnings_modifier")


class EarningsSignalModifier:
    """
    财报感知信号修正器
    
    根据财报日历调整信号权重和仓位建议。
    """
    
    def __init__(self, earnings_calendar=None):
        self.earnings_calendar = earnings_calendar
        self._earnings_cache = {}
        self._cache_ttl = timedelta(hours=4)
        self._last_update = None
    
    def update_cache(self, watchlist_symbols=None):
        """更新财报缓存"""
        if self.earnings_calendar is None:
            return
        
        try:
            earnings = self.earnings_calendar.get_watchlist_earnings(
                watchlist_symbols=watchlist_symbols,
                days_ahead=14,
                force_refresh=True,
            )
            
            self._earnings_cache = {}
            for e in earnings:
                self._earnings_cache[e["symbol"]] = {
                    "ticker": e["ticker"],
                    "name": e["name"],
                    "report_date": e["report_date"],
                    "sector": e["sector"],
                    "days_until": e["days_until"],
                    "time_of_day": e["time_of_day"],
                    "estimate": e["estimate"],
                }
            
            self._last_update = datetime.now()
            logger.info(f"[EarningsModifier] 缓存已更新: {len(self._earnings_cache)} 只")
            
        except Exception as e:
            logger.warning(f"[EarningsModifier] 缓存更新失败: {e}")
    
    def get_earnings_context(self, symbol: str) -> Dict:
        """获取股票的财报上下文"""
        now = datetime.now()
        if self._last_update is None or (now - self._last_update) > self._cache_ttl:
            self.update_cache()
        
        if symbol not in self._earnings_cache:
            return {
                "has_earnings": False,
                "days_until": None,
                "is_pre_earnings": False,
                "is_post_earnings": False,
                "sector": "Unknown",
                "report_date": None,
            }
        
        info = self._earnings_cache[symbol]
        days = info["days_until"]
        
        return {
            "has_earnings": True,
            "days_until": days,
            "is_pre_earnings": 0 <= days <= 3,
            "is_post_earnings": -2 <= days < 0,
            "sector": info["sector"],
            "report_date": info["report_date"],
            "time_of_day": info.get("time_of_day", "N/A"),
        }
    
    def adjust_signal_strength(self, symbol: str, base_strength: float, 
                                direction: str = "LONG") -> Tuple[float, str]:
        """根据财报状态调整信号强度"""
        ctx = self.get_earnings_context(symbol)
        
        if not ctx["has_earnings"]:
            return base_strength, "无财报，信号不变"
        
        if ctx["is_pre_earnings"]:
            reduction = 0.5
            adjusted = base_strength * (1 - reduction)
            reason = f"财报前 {ctx['days_until']} 天，信号降权 {reduction:.0%}"
            logger.info(f"[EarningsModifier] {symbol} 财报前降权: {base_strength:.0f} → {adjusted:.0f}")
            return adjusted, reason
        
        if ctx["is_post_earnings"]:
            boost = 0.3
            adjusted = min(100, base_strength * (1 + boost))
            reason = f"财报后 {abs(ctx['days_until'])} 天，信号放大 {boost:.0%}"
            logger.info(f"[EarningsModifier] {symbol} 财报后放大: {base_strength:.0f} → {adjusted:.0f}")
            return adjusted, reason
        
        return base_strength, f"财报日 {ctx['days_until']} 天，信号不变"
    
    def get_position_advice(self, symbol: str, base_size: int, 
                            total_equity: float) -> Tuple[int, str]:
        """根据财报状态调整仓位建议"""
        ctx = self.get_earnings_context(symbol)
        
        if not ctx["has_earnings"]:
            return base_size, "无财报，仓位不变"
        
        if ctx["is_pre_earnings"]:
            adjusted = max(0, int(base_size * 0.5))
            return adjusted, "财报前降仓 50%"
        
        if ctx["is_post_earnings"]:
            return base_size, "财报后正常仓位"
        
        return 0, "财报日禁止建仓"
    
    def get_summary(self) -> Dict:
        """获取财报监控摘要"""
        pre_earnings = []
        post_earnings = []
        
        for sym, info in self._earnings_cache.items():
            if 0 <= info["days_until"] <= 3:
                pre_earnings.append(sym)
            elif -2 <= info["days_until"] < 0:
                post_earnings.append(sym)
        
        return {
            "total_monitored": len(self._earnings_cache),
            "pre_earnings": pre_earnings,
            "post_earnings": post_earnings,
            "last_updated": self._last_update.isoformat() if self._last_update else None,
        }


_modifier = None


def get_earnings_modifier(earnings_calendar=None) -> EarningsSignalModifier:
    """获取全局实例"""
    global _modifier
    if _modifier is None:
        _modifier = EarningsSignalModifier(earnings_calendar)
    return _modifier
