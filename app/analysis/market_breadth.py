"""
Market Breadth Tracker — 系统恐慌检测 (Muyuan P0: 2026-06-02)

论文核心发现: 当 ≥6 只股票同时触发超卖信号时, 反转 alpha 是基线的 11.6 倍, 胜率 78%。
本模块在每轮扫描中追踪同时处于超卖状态的标的数量, 为入场决策提供横截面广度信号。

超卖定义 (任一满足):
  - RSI < 30 (经典超卖)
  - 价格低于 MA20 且 change_percent < -2% (趋势超卖)
  - slope_5m < -0.5% (短期动量超卖)

用法:
  from app.analysis.market_breadth import get_market_breadth
  breadth = get_market_breadth()
  breadth.record_symbol("US.TSLA", context)   # 每轮扫描每个标的调用一次
  count = breadth.get_oversold_count()          # 当前轮超卖标的数
  is_panic = breadth.is_panic_mode()            # count >= 6
"""
import threading
import time
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger("ftseek.market_breadth")

# ── 论文阈值 ──
PANIC_THRESHOLD = 6        # ≥6 只同时超卖 = 系统恐慌
AMPLIFIED_THRESHOLD = 4    # 4-5 只 = 中等恐慌
ELEVATED_THRESHOLD = 2     # 2-3 只 = 少量恐慌

# ── 超卖判定参数 ──
RSI_OVERSOLD = 30          # RSI 超卖阈值
CHANGE_OVERSOLD = -2.0     # change_percent 超卖阈值 (%)
SLOPE_OVERSOLD = -0.5      # slope_5m 超卖阈值 (%)
MA_DEVIATION = -0.02       # 价格低于 MA20 的偏离度阈值


class MarketBreadth:
    """线程安全的横截面广度追踪器"""

    def __init__(self):
        self._lock = threading.Lock()
        # 当前扫描轮次的超卖标的集合
        self._oversold_symbols: set = set()
        # 所有扫描过的标的 (用于判断是否为同一轮)
        self._scanned_symbols: set = set()
        # 超卖明细 (for logging)
        self._oversold_details: Dict[str, Dict[str, Any]] = {}
        # 上一轮的超卖数 (用于变化检测)
        self._prev_oversold_count: int = 0
        # 当前轮次的时间戳
        self._scan_timestamp: float = 0
        # 轮次内计数器
        self._scan_count: int = 0

    def start_new_scan(self):
        """标记新一轮扫描开始 — 重置计数器"""
        with self._lock:
            # 保存上一轮结果
            self._prev_oversold_count = len(self._oversold_symbols)
            # 重置当前轮
            self._oversold_symbols = set()
            self._scanned_symbols = set()
            self._oversold_details = {}
            self._scan_timestamp = time.time()
            self._scan_count = 0

    def record_symbol(self, symbol: str, context: Dict[str, Any]) -> bool:
        """记录一个标的的市场数据, 判断是否超卖

        Args:
            symbol: US.TSLA
            context: market_data_router.get_full_market_context() 返回的字典

        Returns:
            True if the symbol is oversold
        """
        is_oversold = self._check_oversold(context)

        with self._lock:
            self._scanned_symbols.add(symbol)
            self._scan_count += 1
            if is_oversold:
                self._oversold_symbols.add(symbol)
                self._oversold_details[symbol] = {
                    "rsi": context.get("rsi", 0),
                    "change_pct": context.get("change_percent", 0),
                    "slope_5m": context.get("slope_5m", 0),
                    "price": context.get("price", 0),
                    "ma20": context.get("ma20", 0),
                }
        return is_oversold

    def _check_oversold(self, context: Dict[str, Any]) -> bool:
        """判断单个标的是否处于超卖状态

        三个独立条件, 任一满足即为超卖:
          1. RSI < 30 (经典超卖)
          2. 价格低于 MA20 2%+ 且日内跌幅 > 2% (趋势超卖)
          3. 5 分钟斜率 < -0.5% (短期动量超卖)
        """
        rsi = context.get("rsi", 50)
        change_pct = context.get("change_percent", 0)
        slope_5m = context.get("slope_5m", 0)
        price = context.get("price", 0)
        ma20 = context.get("ma20", 0)

        # 条件 1: RSI 超卖
        if 0 < rsi < RSI_OVERSOLD:
            return True

        # 条件 2: 趋势超卖 (价格偏离 MA20 + 日内大跌)
        if ma20 > 0 and price > 0:
            ma_deviation = (price - ma20) / ma20
            if ma_deviation < MA_DEVIATION and change_pct < CHANGE_OVERSOLD:
                return True

        # 条件 3: 短期动量超卖
        if slope_5m < SLOPE_OVERSOLD:
            return True

        return False

    def get_oversold_count(self) -> int:
        """当前轮超卖标的数量"""
        with self._lock:
            return len(self._oversold_symbols)

    def get_oversold_symbols(self) -> list:
        """当前轮超卖标的列表"""
        with self._lock:
            return list(self._oversold_symbols)

    def is_panic_mode(self) -> bool:
        """是否处于系统恐慌模式 (≥6 只同时超卖)"""
        return self.get_oversold_count() >= PANIC_THRESHOLD

    def is_amplified(self) -> bool:
        """是否处于放大模式 (≥4 只同时超卖)"""
        return self.get_oversold_count() >= AMPLIFIED_THRESHOLD

    def get_breadth_tier(self) -> str:
        """获取当前广度层级

        Returns:
            "panic" (≥6), "amplified" (4-5), "elevated" (2-3), "normal" (0-1)
        """
        count = self.get_oversold_count()
        if count >= PANIC_THRESHOLD:
            return "panic"
        elif count >= AMPLIFIED_THRESHOLD:
            return "amplified"
        elif count >= ELEVATED_THRESHOLD:
            return "elevated"
        else:
            return "normal"

    def get_signal_boost(self) -> Dict[str, Any]:
        """根据广度层级返回信号增强参数

        论文数据:
          - 孤立信号 (1 只): +110bp, 66.2% WR (基线)
          - 2-3 只: +778bp, 59.0% WR (7.1×)
          - 4-5 只: +423bp, 59.9% WR (3.8×)
          - ≥6 只: +1285bp, 77.7% WR (11.6×)

        Returns:
            {"strength_bonus": int, "confidence_mult": float, "block_short": bool, "tier": str}
        """
        count = self.get_oversold_count()
        tier = self.get_breadth_tier()

        if tier == "panic":
            return {
                "strength_bonus": 30,       # LONG strength +30
                "confidence_mult": 1.3,     # confidence × 1.3
                "block_short": True,        # 恐慌时禁止做空 (反弹太暴力)
                "tier": "panic",
                "oversold_count": count,
            }
        elif tier == "amplified":
            return {
                "strength_bonus": 15,
                "confidence_mult": 1.15,
                "block_short": True,
                "tier": "amplified",
                "oversold_count": count,
            }
        elif tier == "elevated":
            return {
                "strength_bonus": 8,
                "confidence_mult": 1.05,
                "block_short": False,
                "tier": "elevated",
                "oversold_count": count,
            }
        else:
            return {
                "strength_bonus": 0,
                "confidence_mult": 1.0,
                "block_short": False,
                "tier": "normal",
                "oversold_count": count,
            }

    def log_snapshot(self):
        """输出当前轮次的广度快照 (INFO 级别)"""
        with self._lock:
            count = len(self._oversold_symbols)
            total = self._scan_count
            tier = self.get_breadth_tier()
            symbols = list(self._oversold_symbols)
            prev = self._prev_oversold_count

        if count > 0:
            logger.info(
                f"[广度] 超卖={count}/{total} 层级={tier} "
                f"(上轮={prev}) 标的={symbols}"
            )
            if tier in ("panic", "amplified"):
                details = []
                with self._lock:
                    for sym in symbols[:5]:
                        d = self._oversold_details.get(sym, {})
                        details.append(
                            f"{sym}(RSI={d.get('rsi',0):.0f}, "
                            f"chg={d.get('change_pct',0):.1f}%)"
                        )
                logger.warning(
                    f"🚨 [广度·{tier.upper()}] {count} 只同时超卖: {'; '.join(details)}"
                )
        else:
            logger.info(f"[广度] 超卖=0/{total} 层级=normal (无超卖标的)")


# ─── Singleton ───
_instance: Optional[MarketBreadth] = None
_lock = threading.Lock()


def get_market_breadth() -> MarketBreadth:
    """获取全局 MarketBreadth 单例"""
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = MarketBreadth()
    return _instance
