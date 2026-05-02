"""
app/risk/liquidity_guard.py — 港股流动性保护机制 (2026-05-01)

职责:
  检查股票流动性，防止在流动性不足时开仓或持仓。

港股流动性风险:
  1. 买卖盘深度不足: 挂单量少，滑点大
  2. 成交量低: 日均成交量<100万港元，难以进出
  3. 价格不连续: 买卖价差过大
  4. 仙股陷阱: 股价<0.1港元，流动性极差

检测机制:
  - 买卖盘深度: bid/ask 前5档挂单总量
  - 成交量: 当日成交量 vs 30日均量
  - 买卖价差: (ask - bid) / mid_price
  - 股价范围: 过滤仙股

阈值 (可配置):
  MIN_ORDER_BOOK_DEPTH = 100000    # 最小买卖盘深度 (10万港元)
  MAX_BID_ASK_SPREAD = 0.02        # 最大买卖价差 (2%)
  MIN_PRICE = 0.10                 # 最低股价 (0.1港元)
  MIN_VOLUME_RATIO = 0.3           # 最小量比 (当日量/30日均量)
"""
import logging
from typing import Dict, Optional
from datetime import datetime

logger = logging.getLogger("ftseek.risk.liquidity_guard")

# ═══════════════════════════════════════════════════════════════
#  流动性阈值
# ═══════════════════════════════════════════════════════════════

# 最小买卖盘深度 (10万港元)
MIN_ORDER_BOOK_DEPTH = 100000

# 最大买卖价差 (2%)
MAX_BID_ASK_SPREAD = 0.02

# 最低股价 (0.1港元)
MIN_PRICE = 0.10

# 最小量比 (当日成交量/30日均量)
MIN_VOLUME_RATIO = 0.3

# 最大持仓占比 (单只股票不超过总仓位的20%)
MAX_POSITION_PCT = 0.20


class LiquidityGuard:
    """
    流动性保护守护

    用法:
        guard = LiquidityGuard()
        result = guard.check_liquidity(
            code="HK.00700",
            quote_data={
                'bid': 100.0,
                'ask': 100.2,
                'last_price': 100.1,
                'volume': 5000000,
                'avg_volume_30d': 10000000,
                'order_book': {...}
            }
        )
        if not result.passed:
            print(f"流动性检查失败: {result.reason}")
    """

    def __init__(self, min_depth=None, max_spread=None, min_price=None, min_vol_ratio=None):
        self.min_depth = min_depth or MIN_ORDER_BOOK_DEPTH
        self.max_spread = max_spread or MAX_BID_ASK_SPREAD
        self.min_price = min_price or MIN_PRICE
        self.min_vol_ratio = min_vol_ratio or MIN_VOLUME_RATIO

    def check_liquidity(
        self,
        code: str,
        quote_data: Optional[dict] = None,
        position_value: float = 0,
        total_portfolio_value: float = 0,
    ) -> 'LiquidityResult':
        """
        检查股票流动性。

        Args:
            code: 股票代码
            quote_data: 行情数据
            position_value: 当前持仓市值
            total_portfolio_value: 组合总市值

        Returns:
            LiquidityResult
        """
        if not quote_data:
            return LiquidityResult(
                passed=True,
                reason="无行情数据，跳过流动性检查",
                code=code
            )

        # 检查1: 股价范围 (仙股过滤)
        price = quote_data.get('last_price', 0) or quote_data.get('price', 0)
        if price <= 0:
            return LiquidityResult(
                passed=False,
                reason=f"价格无效: {price}",
                code=code
            )
        if price < self.min_price:
            return LiquidityResult(
                passed=False,
                reason=f"仙股: 价格{price} < {self.min_price}",
                code=code,
                risk_level="high"
            )

        # 检查2: 买卖价差
        bid = quote_data.get('bid', 0)
        ask = quote_data.get('ask', 0)
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2
            spread = (ask - bid) / mid
            if spread > self.max_spread:
                return LiquidityResult(
                    passed=False,
                    reason=f"买卖价差过大: {spread:.1%} > {self.max_spread:.0%}",
                    code=code,
                    risk_level="medium"
                )

        # 检查3: 成交量比率
        volume = quote_data.get('volume', 0)
        avg_volume = quote_data.get('avg_volume_30d', 0) or quote_data.get('avg_volume', 0)
        if avg_volume > 0 and volume > 0:
            vol_ratio = volume / avg_volume
            if vol_ratio < self.min_vol_ratio:
                return LiquidityResult(
                    passed=False,
                    reason=f"量比过低: {vol_ratio:.2f} < {self.min_vol_ratio:.2f}",
                    code=code,
                    risk_level="medium"
                )

        # 检查4: 买卖盘深度
        order_book = quote_data.get('order_book', {})
        if order_book:
            bid_depth = sum(qty * price for price, qty in order_book.get('bid', [])[:5])
            ask_depth = sum(qty * price for price, qty in order_book.get('ask', [])[:5])
            total_depth = bid_depth + ask_depth
            if total_depth < self.min_depth:
                return LiquidityResult(
                    passed=False,
                    reason=f"买卖盘深度不足: {total_depth:.0f} < {self.min_depth:.0f}",
                    code=code,
                    risk_level="high"
                )

        # 检查5: 持仓集中度 (可选)
        if position_value > 0 and total_portfolio_value > 0:
            pos_pct = position_value / total_portfolio_value
            if pos_pct > MAX_POSITION_PCT:
                return LiquidityResult(
                    passed=False,
                    reason=f"持仓集中度过高: {pos_pct:.0%} > {MAX_POSITION_PCT:.0%}",
                    code=code,
                    risk_level="high"
                )

        return LiquidityResult(
            passed=True,
            reason="流动性正常",
            code=code,
            risk_level="low"
        )


class LiquidityResult:
    """流动性检查结果"""
    def __init__(self, passed: bool, reason: str, code: str, risk_level: str = "low"):
        self.passed = passed
        self.reason = reason
        self.code = code
        self.risk_level = risk_level


# 全局单例
liquidity_guard = LiquidityGuard()
