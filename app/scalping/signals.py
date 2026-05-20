"""
信号检测 — RSI / VWAP / 布林带

核心原则:
  - 不预测方向，只响应价格偏离
  - 多指标投票 (2/3 确认) 降低误判
  - 纯技术指标，无 LLM
  - VIX 自适应 BB: 高波动放宽带宽，避免假突破
"""

from typing import Optional, Dict, List
import logging

logger = logging.getLogger(__name__)

LONG = "LONG"
SHORT = "SHORT"


def vix_adaptive_std(vix: float, base_std: float = 2.0) -> float:
    """
    VIX 自适应 BB 标准差倍数

    dyn = base_std × (1 + 0.05 × (VIX - 20))
    Floor: 1.5σ, Cap: 4.0σ

    当 VIX > 20: BB 带宽扩大，2σ 突破在统计上不再显著
    当 VIX < 20: BB 带宽缩小，突破信号更可靠
    """
    if vix <= 0:
        return base_std
    dyn = base_std * (1.0 + 0.05 * (vix - 20.0))
    return max(1.5, min(4.0, dyn))


class SignalDetector:
    """
    多指标信号检测
    
    三个独立指标:
      1. RSI 极端值 (14周期, 超卖<20, 超买>80)  # P0 修复 (2026-05-12): 从 25/75 收紧
      2. VWAP 偏离 (阈值 1.8%)                   # P0 修复 (2026-05-12): 从 1.2% 收紧
      3. 布林带边界 (20周期, 2.5倍标准差)         # P0 修复 (2026-05-12): 从 2.0σ 收紧
    
    聚合规则: 至少 2/3 指标指向同一方向
    """

    def __init__(
        self,
        rsi_period: int = 14,
        rsi_oversold: float = 20,      # P0 修复: 从 25 收紧
        rsi_overbought: float = 80,    # P0 修复: 从 75 收紧
        vwap_deviation_pct: float = 0.018,  # P0 修复: 从 0.012 收紧
        bollinger_period: int = 20,
        bollinger_std: float = 2.5,    # P0 修复: 从 2.0 收紧
        min_signals: int = 2,
        # P0 新增 (2026-05-20): LONG 接飞刀防护
        # 归因数据: vwap+bollinger 无 RSI 31笔亏$850, VWAP>2% below 39笔仅3胜
        long_guard_cfg: Optional[Dict] = None,
    ):
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.vwap_deviation_pct = vwap_deviation_pct
        self.bollinger_period = bollinger_period
        self.bollinger_std = bollinger_std
        self.min_signals = min_signals

        # P0 (2026-05-20): LONG 接飞刀防护配置
        _lg = long_guard_cfg or {}
        self.long_guard_enabled = _lg.get("enabled", True)
        # 无 RSI 确认的 LONG 信号一律拒绝 (vwap+bollinger 组合 31笔亏$850)
        self.long_guard_require_rsi = _lg.get("require_rsi_confirmation", True)
        # VWAP 偏离超过此阈值拒绝做多 (接飞刀: 39笔 VWAP>2% below 仅3胜)
        self.long_guard_knife_pct = _lg.get("knife_catch_vwap_pct", 0.03)
        # 趋势过滤: 价格低于 N 日 EMA 时拒绝做多
        self.long_guard_trend_ema = _lg.get("trend_ema_period", 0)  # 0=禁用

    def check_rsi(self, rsi_14: float) -> Optional[str]:
        """
        RSI 极端值 → 反向交易
        
        RSI < 25: 超卖，做多
        RSI > 75: 超买，做空
        25-75: 无信号
        """
        if rsi_14 < self.rsi_oversold:
            return LONG
        elif rsi_14 > self.rsi_overbought:
            return SHORT
        return None

    def check_vwap(self, price: float, vwap: float) -> Optional[str]:
        """
        VWAP 偏离 → 反向交易
        
        价格 < VWAP - 1.2%: 做多 (低于机构成本)
        价格 > VWAP + 1.2%: 做空 (高于机构成本)
        """
        if vwap <= 0:
            return None

        deviation = (price - vwap) / vwap

        if deviation < -self.vwap_deviation_pct:
            return LONG
        elif deviation > self.vwap_deviation_pct:
            return SHORT
        return None

    def check_bollinger(
        self, price: float, upper: float, lower: float
    ) -> Optional[str]:
        """
        布林带边界 → 反向交易

        价格 < 下轨: 做多
        价格 > 上轨: 做空

        Y2 (2026-05-15, 基于 LCID afternoon bb 带宽 $0.02 的 sanity check):
        如果 bb 带宽 < 0.1% 价格 → 数据异常 (标准差崩溃), 不出信号
        """
        if upper <= 0 or lower <= 0:
            return None

        # Y2: bb 带宽崩溃检测 — 防 LCID-style 噪音信号
        bandwidth = upper - lower
        if bandwidth < price * 0.001 or bandwidth <= 0:
            return None  # 数据异常, 不出信号

        if price < lower:
            return LONG
        elif price > upper:
            return SHORT
        return None

    def aggregate(
        self,
        rsi_dir: Optional[str],
        vwap_dir: Optional[str],
        bb_dir: Optional[str],
        bb_weight: float = 1.0,
    ) -> Optional[str]:
        """
        多数投票: 至少 min_signals (默认 2) 个指标指向同一方向

        bb_weight: VIX 自适应 BB 投票权重 (0.0-1.0), 高 VIX 时降权

        Returns:
            "LONG", "SHORT", or None
        """
        rsi_vote = 1 if rsi_dir else 0
        vwap_vote = 1 if vwap_dir else 0
        bb_vote = bb_weight if bb_dir else 0

        long_weight = (
            (rsi_vote if rsi_dir == LONG else 0) +
            (vwap_vote if vwap_dir == LONG else 0) +
            (bb_vote if bb_dir == LONG else 0)
        )
        short_weight = (
            (rsi_vote if rsi_dir == SHORT else 0) +
            (vwap_vote if vwap_dir == SHORT else 0) +
            (bb_vote if bb_dir == SHORT else 0)
        )

        if long_weight >= self.min_signals:
            return LONG
        elif short_weight >= self.min_signals:
            return SHORT

        return None

    def detect(
        self,
        price: float,
        rsi_14: float,
        vwap: float,
        bb_upper: float,
        bb_lower: float,
        allowed_directions: Optional[List[str]] = None,
        vix: Optional[float] = None,
        price_momentum_3m: Optional[float] = None,
        # P0 新增 (2026-05-09): 订单流深度数据
        order_book: Optional[Dict] = None,
        order_book_cfg: Optional[Dict] = None,
        # P1 新增 (2026-05-09): 多时间框架数据
        k5m_trend: Optional[str] = None,  # "UP"/"DOWN"/"NEUTRAL"
        multi_tf_cfg: Optional[Dict] = None,
        # P1 新增 (2026-05-09): 逐笔成交数据
        ticker_flow: Optional[Dict] = None,
        ticker_cfg: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """
        完整信号检测流程

        Args:
            price: 当前价格
            rsi_14: RSI(14) 值
            vwap: 当日 VWAP
            bb_upper: 布林带上轨
            bb_lower: 布林带下轨
            allowed_directions: 时段允许的方向 (None = 都允许)
            vix: VIX 值, 用于自适应 BB 投票权重
            price_momentum_3m: 3分钟价格变化率 (P1 新增，用于早期信号)
            order_book: 订单流深度数据 (P0 新增)
            order_book_cfg: 订单流配置 (P0 新增)
            k5m_trend: 5分钟趋势方向 (P1 新增)
            multi_tf_cfg: 多时间框架配置 (P1 新增)
            ticker_flow: 逐笔成交分析结果 (P1 新增)
            ticker_cfg: 逐笔成交配置 (P1 新增)

        Returns:
            {"direction": "LONG"/"SHORT", "signals": [...], "votes": int} or None
        """
        # 1. 各指标独立检测
        rsi_dir = self.check_rsi(rsi_14)
        vwap_dir = self.check_vwap(price, vwap)
        bb_dir = self.check_bollinger(price, bb_upper, bb_lower)

        # 2. VIX 自适应: BB 投票权重
        bb_weight = 1.0
        if vix is not None and vix > 0 and bb_dir is not None:
            adaptive = vix_adaptive_std(vix, self.bollinger_std)
            if adaptive > 3.0:       # VIX > 30: BB 宽度 > 3.0σ, 2σ 突破无统计意义
                bb_weight = 0.0
            elif adaptive > 2.5:     # VIX > 25: BB 宽度 > 2.5σ, 半票
                bb_weight = 0.5

        # 3. 聚合投票
        direction = self.aggregate(rsi_dir, vwap_dir, bb_dir, bb_weight=bb_weight)

        # ── 高置信度检测: 3 指标全票一致 ──
        high_conviction = False
        if direction and rsi_dir and vwap_dir and bb_dir:
            if rsi_dir == vwap_dir == bb_dir == direction:
                high_conviction = True

        # ═══════════════════════════════════════════════════════════
        # P1 修复 (2026-05-08): 早期信号机制
        # 当 1个核心指标接近触发 + 动量加速时，提前入场
        # 解决 66% 交易被时间止损打断的问题
        # ═══════════════════════════════════════════════════════════
        early_signal = None
        if not direction and price_momentum_3m is not None:
            # 核心指标: RSI 接近极端值 (但还没到)
            rsi_near_oversold = self.rsi_oversold < rsi_14 < (self.rsi_oversold + 5)
            rsi_near_overbought = (self.rsi_overbought - 5) < rsi_14 < self.rsi_overbought

            # VWAP 接近偏离阈值
            if vwap > 0:
                vwap_dev = abs(price - vwap) / vwap
                vwap_near_threshold = (self.vwap_deviation_pct * 0.7) < vwap_dev < self.vwap_deviation_pct
            else:
                vwap_near_threshold = False

            # 动量确认: 价格快速远离极端区域
            momentum_threshold = 0.002  # 3分钟 0.2% 变化

            # 早期做多: RSI接近超卖 + 价格开始反弹
            if rsi_near_oversold and price_momentum_3m > momentum_threshold:
                if allowed_directions is None or LONG in allowed_directions:
                    early_signal = {
                        "direction": LONG,
                        "signals": ["early_rsi_reversal"],
                        "votes": 1,
                        "is_early": True,
                        "reason": f"RSI接近超卖({rsi_14:.0f}) + 3m动量+{price_momentum_3m*100:.2f}%",
                    }

            # 早期做空: RSI接近超买 + 价格开始回落
            elif rsi_near_overbought and price_momentum_3m < -momentum_threshold:
                if allowed_directions is None or SHORT in allowed_directions:
                    early_signal = {
                        "direction": SHORT,
                        "signals": ["early_rsi_reversal"],
                        "votes": 1,
                        "is_early": True,
                        "reason": f"RSI接近超买({rsi_14:.0f}) + 3m动量{price_momentum_3m*100:.2f}%",
                    }

            # 早期做多: VWAP接近偏离 + 价格反弹
            elif vwap_near_threshold and price < vwap and price_momentum_3m > momentum_threshold:
                if allowed_directions is None or LONG in allowed_directions:
                    early_signal = {
                        "direction": LONG,
                        "signals": ["early_vwap_reversal"],
                        "votes": 1,
                        "is_early": True,
                        "reason": f"VWAP偏离{vwap_dev*100:.1f}% + 3m动量+{price_momentum_3m*100:.2f}%",
                    }
        # ═══════════════════════════════════════════════════════════

        if not direction:
            if early_signal:
                logger.info(
                    f"[SignalDetector] ⚡ 早期信号 | {early_signal['direction']} | "
                    f"{early_signal['reason']}"
                )
                return early_signal
            return None

        # 4. 时段过滤
        if allowed_directions and direction not in allowed_directions:
            logger.debug(
                f"[SignalDetector] {direction} 信号被时段过滤拒绝 "
                f"(允许: {allowed_directions})"
            )
            return None

        # ═══════════════════════════════════════════════════════════
        # P0 新增 (2026-05-20): LONG 接飞刀防护
        # 归因数据:
        #   - vwap+bollinger 无 RSI: 31笔亏$850 (胜率接近0)
        #   - VWAP偏离>2% below: 39笔仅3胜 (接飞刀)
        #   - LCID(-$820/20笔), UBER(-$179/12笔) 集中在下跌股
        # ═══════════════════════════════════════════════════════════
        if self.long_guard_enabled and direction == LONG:
            # 检查1: RSI 确认 — vwap+bollinger 组合无 RSI 时拒绝
            if self.long_guard_require_rsi and rsi_dir != LONG:
                logger.info(
                    f"[SignalDetector] 🛡️ LONG 接飞刀防护: RSI 未确认超卖 "
                    f"(RSI={rsi_14:.1f}, 需<{self.rsi_oversold}), "
                    f"信号组合 {'/'.join(s for s, d in [('rsi', rsi_dir), ('vwap', vwap_dir), ('bollinger', bb_dir)] if d == LONG)} → 拒绝入场"
                )
                return None

            # 检查2: VWAP 偏离过大 — 价格远低于 VWAP 时拒绝做多 (接飞刀)
            if vwap > 0:
                vwap_dev = (price - vwap) / vwap
                if vwap_dev < -self.long_guard_knife_pct:
                    logger.info(
                        f"[SignalDetector] 🛡️ LONG 接飞刀防护: VWAP偏离 {vwap_dev:+.2%} "
                        f"< -{self.long_guard_knife_pct:.1%} (价格远低于机构成本) → 拒绝入场"
                    )
                    return None

        # ═══════════════════════════════════════════════════════════
        # P0 新增 (2026-05-09): 订单流过滤 — 确认反转点有深度支撑
        # ═══════════════════════════════════════════════════════════
        if order_book and order_book_cfg and order_book_cfg.get("enabled", True):
            fallback = order_book_cfg.get("fallback_enabled", True)
            bid_ask_ratio = order_book.get("bid_ask_ratio", 0)
            min_bid_ask = order_book_cfg.get("min_bid_ask_ratio", 1.5)
            min_ask_bid = order_book_cfg.get("min_ask_bid_ratio", 1.5)

            if direction == LONG:
                # 做多需要买盘深度显著大于卖盘
                if bid_ask_ratio < min_bid_ask:
                    if not fallback:
                        logger.debug(
                            f"[SignalDetector] {direction} 被订单流过滤拒绝 "
                            f"(bid/ask={bid_ask_ratio:.2f} < {min_bid_ask})"
                        )
                        return None
                    else:
                        logger.debug(
                            f"[SignalDetector] ⚠️ 订单流不支撑做多 "
                            f"(bid/ask={bid_ask_ratio:.2f}), 降级到技术指标"
                        )
            elif direction == SHORT:
                # 做空需要卖盘深度显著大于买盘 (ask/bid = 1/ratio)
                ask_bid_ratio = 1.0 / bid_ask_ratio if bid_ask_ratio > 0 else 0
                if ask_bid_ratio < min_ask_bid:
                    if not fallback:
                        logger.debug(
                            f"[SignalDetector] {direction} 被订单流过滤拒绝 "
                            f"(ask/bid={ask_bid_ratio:.2f} < {min_ask_bid})"
                        )
                        return None
                    else:
                        logger.debug(
                            f"[SignalDetector] ⚠️ 订单流不支撑做空 "
                            f"(ask/bid={ask_bid_ratio:.2f}), 降级到技术指标"
                        )

        # ═══════════════════════════════════════════════════════════
        # P1 新增 (2026-05-09): 多时间框架确认 — 5 分钟趋势过滤
        # ═══════════════════════════════════════════════════════════
        if k5m_trend and multi_tf_cfg and multi_tf_cfg.get("enabled", True):
            if multi_tf_cfg.get("trend_confirm", True):
                if direction == LONG and k5m_trend == "DOWN":
                    if not multi_tf_cfg.get("fallback_enabled", True):
                        logger.debug(
                            f"[SignalDetector] {direction} 被 5M 趋势过滤拒绝 "
                            f"(5M 趋势下降)"
                        )
                        return None
                    else:
                        logger.debug(
                            f"[SignalDetector] ⚠️ 5M 趋势下降但 1M 做多信号, "
                            f"降级到技术指标"
                        )
                elif direction == SHORT and k5m_trend == "UP":
                    if not multi_tf_cfg.get("fallback_enabled", True):
                        logger.debug(
                            f"[SignalDetector] {direction} 被 5M 趋势过滤拒绝 "
                            f"(5M 趋势上升)"
                        )
                        return None
                    else:
                        logger.debug(
                            f"[SignalDetector] ⚠️ 5M 趋势上升但 1M 做空信号, "
                            f"降级到技术指标"
                        )

        # ═══════════════════════════════════════════════════════════
        # P1 新增 (2026-05-09): 逐笔成交异动检测 — 机构大单确认
        # ═══════════════════════════════════════════════════════════
        if ticker_flow and ticker_cfg and ticker_cfg.get("enabled", True):
            large_buy = ticker_flow.get("large_buy_count", 0)
            large_sell = ticker_flow.get("large_sell_count", 0)
            imbalance = ticker_cfg.get("imbalance_ratio", 2.0)

            if direction == LONG and large_sell > large_buy * imbalance:
                # 做空大单显著多于做多 → 拒绝做多
                if not ticker_cfg.get("fallback_enabled", True):
                    logger.debug(
                        f"[SignalDetector] {direction} 被逐笔成交过滤拒绝 "
                        f"(large sell/buy = {large_sell}/{large_buy})"
                    )
                    return None
            elif direction == SHORT and large_buy > large_sell * imbalance:
                # 做多大单显著多于做空 → 拒绝做空
                if not ticker_cfg.get("fallback_enabled", True):
                    logger.debug(
                        f"[SignalDetector] {direction} 被逐笔成交过滤拒绝 "
                        f"(large buy/sell = {large_buy}/{large_sell})"
                    )
                    return None

        # 5. 构建结果
        active_signals = []
        if rsi_dir == direction:
            active_signals.append("rsi")
        if vwap_dir == direction:
            active_signals.append("vwap")
        if bb_dir == direction:
            active_signals.append("bollinger")

        result = {
            "direction": direction,
            "signals": active_signals,
            "votes": len(active_signals),
            "price": price,
            "rsi_14": rsi_14,
            "vwap": vwap,
            "bb_upper": bb_upper,
            "bb_lower": bb_lower,
            "is_early": False,
            "high_conviction": high_conviction,  # P1 修复 (2026-05-09): 3/3 全票一致
        }

        logger.info(
            f"[SignalDetector] ✅ 信号触发 | 方向: {direction} | "
            f"投票: {'/'.join(active_signals)} | "
            f"价格: ${price:.2f} | RSI: {rsi_14:.1f}"
            f"{' [高置信度 3/3]' if high_conviction else ''}"
        )

        return result

    def update_config(self, config: Dict):
        """更新配置 (热重载)"""
        if "rsi_oversold" in config:
            self.rsi_oversold = config["rsi_oversold"]
        if "rsi_overbought" in config:
            self.rsi_overbought = config["rsi_overbought"]
        if "vwap_deviation_pct" in config:
            self.vwap_deviation_pct = config["vwap_deviation_pct"]
        if "min_signals" in config:
            self.min_signals = config["min_signals"]
        # P0 (2026-05-20): LONG 接飞刀防护热重载
        if "long_guard" in config:
            lg = config["long_guard"]
            if "enabled" in lg:
                self.long_guard_enabled = lg["enabled"]
            if "require_rsi_confirmation" in lg:
                self.long_guard_require_rsi = lg["require_rsi_confirmation"]
            if "knife_catch_vwap_pct" in lg:
                self.long_guard_knife_pct = lg["knife_catch_vwap_pct"]
        logger.info("[SignalDetector] 配置已更新")


# ================================================================
# 预测型信号: Momentum (Breakout/Breakdown)
# ================================================================

class MomentumDetector:
    """
    动量/突破信号检测 (预测型)
    
    核心逻辑:
      1. Breakout Long: 价格突破阻力 + 放量 -> 追涨
      2. Breakdown Short: 价格跌破支撑 + 放量 -> 杀跌
      3. Exhaustion Short: 触及阻力 + 缩量 + RSI超买 -> 摸顶
    
    与 SignalDetector 的区别:
      - SignalDetector 是均值回归 (反向交易)
      - MomentumDetector 是动量跟随 (顺向交易)
    """

    def __init__(
        self,
        breakout_threshold: float = 0.005,     # 突破 0.5%
        breakdown_threshold: float = 0.005,     # 跌破 0.5%
        volume_surge: float = 1.2,              # 放量 1.2x
        volume_dry: float = 0.8,                # 缩量 0.8x
        rsi_overbought: float = 70,             # 摸顶 RSI 阈值
    ):
        self.breakout_threshold = breakout_threshold
        self.breakdown_threshold = breakdown_threshold
        self.volume_surge = volume_surge
        self.volume_dry = volume_dry
        self.rsi_overbought = rsi_overbought

    def detect(
        self,
        price: float,
        volume: float,
        avg_volume: float,
        resistance: float,
        support: float,
        rsi_14: float = 50,
        allowed_directions: Optional[List[str]] = None,
    ) -> Optional[Dict]:
        """
        检测动量信号
        
        Returns:
            {"direction": "...", "type": "...", "strength": float} or None
        """
        if price <= 0 or avg_volume <= 0:
            return None

        vol_ratio = volume / avg_volume
        is_volume_surge = vol_ratio > self.volume_surge
        is_volume_dry = vol_ratio < self.volume_dry

        # 1. Breakout Long (阻力突破)
        if resistance > 0 and price >= resistance * (1 + self.breakout_threshold):
            if is_volume_surge:
                if allowed_directions and LONG not in allowed_directions:
                    return None
                return {
                    "direction": LONG,
                    "type": "MOMENTUM_BREAKOUT",
                    "strength": 0.9,
                    "reason": f"放量突破阻力 (Vol {vol_ratio:.1f}x)",
                }

        # 2. Breakdown Short (支撑破位)
        if support > 0 and price <= support * (1 - self.breakdown_threshold):
            if is_volume_surge:
                if allowed_directions and SHORT not in allowed_directions:
                    return None
                return {
                    "direction": SHORT,
                    "type": "MOMENTUM_BREAKDOWN",
                    "strength": 0.95,
                    "reason": f"放量跌破支撑 (Vol {vol_ratio:.1f}x)",
                }

        # 3. Exhaustion Short (阻力衰竭/摸顶)
        if resistance > 0 and abs(price - resistance) / resistance < self.breakout_threshold:
            if is_volume_dry and rsi_14 > self.rsi_overbought:
                if allowed_directions and SHORT not in allowed_directions:
                    return None
                return {
                    "direction": SHORT,
                    "type": "MOMENTUM_EXHAUSTION",
                    "strength": 0.75,
                    "reason": f"阻力位量缩衰竭 (RSI {rsi_14:.0f})",
                }

        return None
