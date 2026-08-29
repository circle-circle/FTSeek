"""
财报专属风控模块 (Earnings Risk Guard)

Phase 2 核心组件: 针对财报期间的特殊风控逻辑

职责:
1. 财报期间仓位限制 (单票不超过总资金 5%)
2. 财报期间止损宽度调整 (行业差异化 ATR 倍数)
3. 财报日禁止开盘追单 (IV Crush 保护)
4. 跳空缺口保护 (Gap Down/Up 熔断)
5. Flash Exit 阈值调整 (财报期间更敏感)

作者: FTSeek-US Team
版本: 1.0 (2026-04-23)
"""

import os
import logging
from datetime import datetime, timedelta
from app.utils.timezone import now_et, ET
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger("ftseek.us_pipeline.earnings_risk")


# ============================================================================
#  财报风控配置
# ============================================================================
class EarningsRiskConfig:
    """财报期间风控参数"""
    
    # 仓位限制
    MAX_EARNINGS_POSITION_PCT = 0.05  # 单票最大 5% 总资金
    MAX_TOTAL_EARNINGS_PCT = 0.15  # 所有财报仓位合计不超过 15%
    
    # 时间窗口
    EARNINGS_BLACKOUT_HOURS_BEFORE = 24  # 财报前 24h 禁止新建仓位
    EARNINGS_HOLD_MAX_HOURS = 72  # 财报持仓最多持有 72h
    
    # 止损/止盈
    EARNINGS_GAP_STOP_PCT = 0.10  # 财报跳空止损 10% (绝对值)
    EARNINGS_FORCED_EXIT_PNL_PCT = -0.08  # 财报持仓亏损 8% 强制出场
    
    # Flash Exit (财报期间更敏感)
    FLASH_EXIT_THRESHOLD_MULTIPLIER = 0.8  # 财报期间 Flash Exit 阈值 = 0.8x ATR
    FLASH_EXIT_VOLUME_MULTIPLIER = 2.5  # 成交量超过均值 2.5 倍触发 Flash
    
    # 行业差异化 ATR 倍数 (已在 earnings_calendar.py 中定义)
    # 此处仅作为 fallback
    DEFAULT_ATR_MULTIPLIER_BASE = 2.5
    DEFAULT_ATR_MULTIPLIER_EARNINGS = 3.5
    DEFAULT_ATR_MULTIPLIER_FLASH = 1.5


@dataclass
class EarningsPosition:
    """财报持仓信息"""
    symbol: str
    ticker: str
    entry_price: float
    current_price: float
    size: int
    sector: str
    atr_value: float
    atr_multiplier: float
    report_date: str
    entry_time: datetime
    earnings_type: str  # "PRE" (财报前) / "POST" (财报后)


class EarningsRiskGuard:
    """
    财报专属风控守卫
    
    在交易执行前检查是否处于财报期间，
    如果是，则应用更严格的风控规则。
    """
    
    def __init__(self, config: Optional[EarningsRiskConfig] = None):
        self.config = config or EarningsRiskConfig()
        self._earnings_symbols = {}  # symbol -> earnings info
        self._last_update = None
    
    def update_earnings_calendar(self, earnings_list: List[Dict]):
        """
        更新财报日历缓存
        
        Args:
            earnings_list: 来自 EarningsCalendar.get_recently_reported() 的列表
        """
        self._earnings_symbols = {}
        for entry in earnings_list:
            sym = entry["symbol"]
            self._earnings_symbols[sym] = {
                "ticker": entry["ticker"],
                "report_date": entry["report_date"],
                "sector": entry["sector"],
                "atr_multiplier": entry["atr_multiplier"],
                "flash_exit_multiplier": entry["flash_exit_multiplier"],
                "time_of_day": entry.get("time_of_day", "N/A"),
            }
        self._last_update = now_et()
        logger.info(f"财报日历已更新: {len(self._earnings_symbols)} 只股票")

        # 活性探针留痕 (2026-08-29, G6 教训): 本守卫的活载体是内存态, 表级审计看不见它
        # (死表 earnings_calendar 曾导致"门从未拦截"的误判下线)。写状态文件供
        # scripts/liveness_probe.py 核查喂数链路是否存活。best-effort, 失败不影响守卫。
        try:
            import json as _json
            from pathlib import Path as _Path
            _lv = _Path(__file__).resolve().parents[2] / "data" / "liveness"
            _lv.mkdir(parents=True, exist_ok=True)
            (_lv / "earnings_guard_feed.json").write_text(_json.dumps({
                "last_update": self._last_update.isoformat(),
                "n_symbols": len(self._earnings_symbols),
            }), encoding="utf-8")
        except Exception:
            pass
    
    def is_earnings_period(self, symbol: str) -> bool:
        """
        检查股票是否处于财报期间 (财报日前后 3 天)
        
        Args:
            symbol: Futu 格式代码
            
        Returns:
            bool
        """
        if symbol not in self._earnings_symbols:
            return False
        
        info = self._earnings_symbols[symbol]
        try:
            report_date = datetime.strptime(info["report_date"], "%Y-%m-%d")
            # strptime returns naive datetime; make it tz-aware before subtracting from now_et()
            if report_date.tzinfo is None:
                report_date = ET.localize(report_date)
            today = now_et()
            delta = (today - report_date).days
            # 财报日前 1 天 到 财报日后 2 天 = 3 天窗口
            return -1 <= delta <= 2
        except ValueError:
            return False
    
    def is_earnings_blackout(self, symbol: str) -> Tuple[bool, str]:
        """
        检查是否处于财报禁建期 (财报前 24h)
        
        Returns:
            (is_blackout, reason)
        """
        if symbol not in self._earnings_symbols:
            return False, ""
        
        info = self._earnings_symbols[symbol]
        try:
            report_date = datetime.strptime(info["report_date"], "%Y-%m-%d")
            # strptime returns naive datetime; make it tz-aware before subtracting from now_et()
            if report_date.tzinfo is None:
                report_date = ET.localize(report_date)
            now = now_et()
            hours_until = (report_date - now).total_seconds() / 3600
            
            if 0 < hours_until <= self.config.EARNINGS_BLACKOUT_HOURS_BEFORE:
                return True, f"财报禁建期: 距财报还有 {hours_until:.0f}h"
            elif hours_until <= 0:
                return False, ""  # 已发布，不禁建
            else:
                return False, ""
        except ValueError:
            return False, ""
    
    def get_atr_multiplier(self, symbol: str, is_earnings_period: bool = False) -> float:
        """
        获取 ATR 倍数 (行业差异化)
        
        Args:
            symbol: Futu 格式代码
            is_earnings_period: 是否处于财报期间
            
        Returns:
            ATR 倍数 (用于止损计算)
        """
        if symbol in self._earnings_symbols:
            info = self._earnings_symbols[symbol]
            return info["atr_multiplier"]
        
        return self.config.DEFAULT_ATR_MULTIPLIER_EARNINGS if is_earnings_period else self.config.DEFAULT_ATR_MULTIPLIER_BASE
    
    def check_position_limits(self, symbol: str, proposed_size: int, 
                              proposed_price: float, total_equity: float) -> Tuple[bool, str]:
        """
        检查仓位限制
        
        Args:
            symbol: 股票代码
            proposed_size: 计划买入数量
            proposed_price: 计划买入价格
            total_equity: 账户总净值
            
        Returns:
            (allowed, reason)
        """
        proposed_value = proposed_size * proposed_price
        position_pct = proposed_value / total_equity if total_equity > 0 else 1.0
        
        if position_pct > self.config.MAX_EARNINGS_POSITION_PCT:
            return False, (
                f"仓位超限: {position_pct:.1%} > {self.config.MAX_EARNINGS_POSITION_PCT:.0%} "
                f"(建议最大 {int(total_equity * self.config.MAX_EARNINGS_POSITION_PCT / proposed_price)} 股)"
            )
        
        # 检查所有财报仓位合计
        # (这里需要外部传入当前所有持仓，简化处理)
        
        return True, "通过"
    
    def check_flash_exit(self, price_change_pct: float, atr_pct: float,
                         volume_ratio: float = 1.0) -> Tuple[bool, str]:
        """
        检查是否需要触发 Flash Exit (财报期间更敏感)
        
        Args:
            price_change_pct: 价格变动百分比 (如 -0.05 = -5%)
            atr_pct: ATR 占当前价格的百分比
            volume_ratio: 当前成交量 / 平均成交量
            
        Returns:
            (should_exit, reason)
        """
        threshold = self.config.FLASH_EXIT_THRESHOLD_MULTIPLIER * atr_pct
        
        # 价格触发
        if abs(price_change_pct) > threshold:
            return True, f"Flash Exit: 价格变动 {price_change_pct:.1%} > 阈值 {threshold:.1%} (财报期间)"
        
        # 成交量触发 (放量下跌)
        if price_change_pct < -threshold * 0.5 and volume_ratio > self.config.FLASH_EXIT_VOLUME_MULTIPLIER:
            return True, f"Flash Exit: 放量下跌 {price_change_pct:.1%}, 量比 {volume_ratio:.1f}x"
        
        return False, ""
    
    def check_forced_exit(self, position: EarningsPosition) -> Tuple[bool, str]:
        """
        检查是否需要强制出场 (财报持仓特殊规则)
        
        Returns:
            (should_exit, reason)
        """
        # 1. 超时检查
        hours_held = (now_et() - position.entry_time).total_seconds() / 3600
        if hours_held > self.config.EARNINGS_HOLD_MAX_HOURS:
            return True, f"财报持仓超时: {hours_held:.0f}h > {self.config.EARNINGS_HOLD_MAX_HOURS}h"
        
        # 2. 亏损检查
        pnl_pct = (position.current_price - position.entry_price) / position.entry_price
        if pnl_pct < self.config.EARNINGS_FORCED_EXIT_PNL_PCT:
            return True, f"财报持仓亏损: {pnl_pct:.1%} < {self.config.EARNINGS_FORCED_EXIT_PNL_PCT:.0%}"
        
        # 3. 跳空缺口检查
        gap_pct = abs(position.current_price - position.entry_price) / position.entry_price
        if gap_pct > self.config.EARNINGS_GAP_STOP_PCT:
            return True, f"财报跳空缺口过大: {gap_pct:.1%} > {self.config.EARNINGS_GAP_STOP_PCT:.0%}"
        
        return False, ""
    
    def get_earnings_status(self, symbol: str) -> Dict:
        """
        获取股票的财报风控状态
        
        Returns:
            状态字典
        """
        is_earnings = self.is_earnings_period(symbol)
        is_blackout, blackout_reason = self.is_earnings_blackout(symbol)
        atr_mult = self.get_atr_multiplier(symbol, is_earnings)
        
        info = self._earnings_symbols.get(symbol, {})
        
        return {
            "symbol": symbol,
            "is_earnings_period": is_earnings,
            "is_blackout": is_blackout,
            "blackout_reason": blackout_reason,
            "atr_multiplier": atr_mult,
            "report_date": info.get("report_date", "N/A"),
            "sector": info.get("sector", "Unknown"),
            "max_position_pct": self.config.MAX_EARNINGS_POSITION_PCT,
        }
    
    def format_status_report(self) -> str:
        """格式化当前所有财报股票的状态报告"""
        if not self._earnings_symbols:
            return "📊 无活跃财报监控。"
        
        lines = ["📊 财报风控状态:"]
        lines.append("")
        
        for symbol, info in self._earnings_symbols.items():
            status = self.get_earnings_status(symbol)
            
            if status["is_earnings_period"]:
                icon = "⚠️"
            elif status["is_blackout"]:
                icon = "🚫"
            else:
                icon = "📈"
            
            lines.append(
                f"{icon} **{symbol}** ({info['sector']})"
            )
            lines.append(f"   - 财报日: {info['report_date']}")
            lines.append(f"   - ATR 倍数: {status['atr_multiplier']}x")
            if status["is_blackout"]:
                lines.append(f"   - 🚫 {status['blackout_reason']}")
            lines.append("")
        
        return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    
    from app.data.earnings_calendar import EarningsCalendar
    
    # 测试
    guard = EarningsRiskGuard()
    cal = EarningsCalendar()
    
    # 获取最近发布的财报
    recent = cal.get_recently_reported(days_back=2)
    guard.update_earnings_calendar(recent)
    
    print("=" * 60)
    print("财报风控状态")
    print("=" * 60)
    print(guard.format_status_report())
    
    # 测试仓位限制
    print("\n" + "=" * 60)
    print("仓位限制测试 (总资金 $100K)")
    print("=" * 60)
    for symbol in guard._earnings_symbols:
        allowed, reason = guard.check_position_limits(
            symbol, 100, 150.0, 100000
        )
        print(f"  {symbol}: {'✅' if allowed else '❌'} {reason}")
