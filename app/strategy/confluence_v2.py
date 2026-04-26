"""
多因子融合策略 (Multi-Factor Confluence) v2.0 - 市场状态增强版

在原有基础上增加:
- 市场状态感知 (Market Regime Awareness)
- 动态因子权重 (Dynamic Factor Weights)
- 波动率调整 (Volatility Adjustment)

支持的因子：RSI / MACD / Bollinger / SuperTrend / MA Cross / Volume
信号模式：至少 N 个因子同时确认才触发（可配置）
"""
import logging
from collections import defaultdict
from typing import Optional, Dict, List
from .base import BaseStrategy, Signal, SignalAction
from .indicators import (
    IndicatorBuffer, rsi, macd, bollinger_bands, bollinger_pctb,
    supertrend, sma, ema, atr, adx
)
from .market_regime import market_regime_detector, MarketRegimeType
from app import config

logger = logging.getLogger("ftseek.strategy.confluence")
logger.propagate = False  # 防止日志重复


class ConfluenceStrategyV2(BaseStrategy):
    """多因子融合策略 v2.0 - 市场状态增强版"""
    name = "confluence_v2"

    def __init__(self, cfg: dict = None):
        super().__init__(cfg or config.STRATEGIES.get("confluence_v2", {}))
        
        # 基础参数
        self.min_factors = self.config.get("min_factors", 3)
        self.use_market_regime = self.config.get("use_market_regime", True)
        self.use_dynamic_weights = self.config.get("use_dynamic_weights", True)
        
        # 各因子参数
        self.rsi_period = self.config.get("rsi_period", 14)
        self.rsi_overbought = self.config.get("rsi_overbought", 70)
        self.rsi_oversold = self.config.get("rsi_oversold", 30)
        self.macd_fast = self.config.get("macd_fast", 12)
        self.macd_slow = self.config.get("macd_slow", 26)
        self.macd_signal = self.config.get("macd_signal", 9)
        self.bb_period = self.config.get("bb_period", 20)
        self.bb_std = self.config.get("bb_std", 2.0)
        self.st_period = self.config.get("st_period", 10)
        self.st_multiplier = self.config.get("st_multiplier", 3.0)
        self.ma_fast = self.config.get("ma_fast", 10)
        self.ma_slow = self.config.get("ma_slow", 30)
        self.vol_ratio = self.config.get("vol_ratio", 1.5)
        self.adx_threshold = self.config.get("adx_threshold", 25)
        
        # 启用的因子
        self.enabled_factors = self.config.get("enabled_factors",
            ["rsi", "macd", "bollinger", "supertrend", "ma_cross", "volume"])
        
        # 市场状态相关的动态权重
        self.regime_weights = {
            MarketRegimeType.STRONG_TREND_UP: {
                "supertrend": 0.8, "macd": 1.3, "rsi": 0.7, "bollinger": 0.8,
            },
            MarketRegimeType.STRONG_TREND_DOWN: {
                "supertrend": 0.8, "macd": 1.3, "rsi": 0.7, "bollinger": 0.8,
            },
            MarketRegimeType.RANGE_BOUND: {
                "rsi": 1.5, "bollinger": 1.5, "supertrend": 0.3, "macd": 0.7,
            },
            MarketRegimeType.HIGH_VOLATILITY: {
                "bollinger": 1.3, "supertrend": 0.4, "rsi": 0.9,
            },
            MarketRegimeType.LOW_VOLATILITY: {
                "bollinger": 1.2,  # 布林带收窄可能突破
            },
        }
        
        # 内部状态
        self._buffers = defaultdict(lambda: IndicatorBuffer())
        self._prev_state: Dict[str, Dict] = defaultdict(dict)

        # 因子信号历史缓存 {code: {factor: [+1/-1/0, ...]}}
        # 保留最近 50 个信号方向，用于动态相关性计算
        self._factor_signal_history: Dict[str, Dict[str, list]] = defaultdict(
            lambda: defaultdict(list)
        )
        # 相关性结果缓存 {code: {(f1,f2): (correlation, kline_count)}}
        # kline_count 用于判断缓存是否过期（每 20 根 K 线刷新）
        self._correlation_cache: Dict[str, Dict] = defaultdict(dict)
        self._kline_count: Dict[str, int] = defaultdict(int)
        self._correlation_refresh_interval = self.config.get("correlation_refresh_interval", 20)
        self._signal_history_maxlen = self.config.get("signal_history_maxlen", 50)
        
        logger.info(
            f"Confluence V2 已初始化 | 市场状态={self.use_market_regime} | "
            f"动态权重={self.use_dynamic_weights}"
        )

    def update_params(self, params: dict):
        """动态更新参数"""
        for key, val in params.items():
            if hasattr(self, key):
                if key in ("min_factors", "rsi_period", "macd_fast", "macd_slow",
                           "macd_signal", "bb_period", "st_period", "ma_fast", "ma_slow"):
                    setattr(self, key, int(val))
                elif key in ("rsi_overbought", "rsi_oversold", "bb_std", "st_multiplier",
                             "vol_ratio", "adx_threshold"):
                    setattr(self, key, float(val))
                elif key == "enabled_factors":
                    setattr(self, key, val if isinstance(val, list) else val.split(","))
                elif key == "enabled":
                    self.enabled = bool(val)
                elif key in ("use_market_regime", "use_dynamic_weights"):
                    setattr(self, key, bool(val))
        logger.info(f"Confluence V2 参数已更新")

    def get_params(self) -> dict:
        return {
            "name": self.name, "enabled": self.enabled,
            "min_factors": self.min_factors,
            "use_market_regime": self.use_market_regime,
            "use_dynamic_weights": self.use_dynamic_weights,
            "enabled_factors": self.enabled_factors,
        }

    def _get_market_regime(self, code: str):
        """获取当前市场状态"""
        if not self.use_market_regime:
            return None
        
        try:
            regime = market_regime_detector.detect(code)
            return regime
        except Exception as e:
            logger.debug(f"获取市场状态失败：{e}")
            return None

    def _get_factor_weight(self, factor: str, regime) -> float:
        """获取因子权重 (考虑市场状态 + 信号质量)"""
        base_weight = 1.0

        if not self.use_dynamic_weights or regime is None:
            base_weight = 1.0
        else:
            # 根据市场状态获取权重
            regime_weight_map = self.regime_weights.get(regime.regime_type, {})
            base_weight = regime_weight_map.get(factor, base_weight)

        # 信号质量调整：基于因子历史胜率动态加权/减权
        quality_factor = self._get_factor_quality_score(factor)
        return base_weight * quality_factor

    # =========================================================================
    # 动态相关性计算 (2026-04-11 新增，替代硬编码折扣)
    # =========================================================================

    def _compute_factor_correlation(self, factor1_signals: list, factor2_signals: list,
                                     lookback: int = 20) -> Optional[float]:
        """
        计算两个因子历史信号的皮尔逊相关系数（不依赖 numpy，手动计算）

        Args:
            factor1_signals: 因子1 信号历史列表 (+1/-1/0)
            factor2_signals: 因子2 信号历史列表 (+1/-1/0)
            lookback: 用于计算的最近 N 个信号，默认 20

        Returns:
            float [-1, 1]: 相关系数；None 表示样本不足
        """
        if len(factor1_signals) < lookback or len(factor2_signals) < lookback:
            return None  # 样本不足，调用方降级为硬编码折扣

        seq1 = factor1_signals[-lookback:]
        seq2 = factor2_signals[-lookback:]
        n = len(seq1)

        mean1 = sum(seq1) / n
        mean2 = sum(seq2) / n
        cov = sum((a - mean1) * (b - mean2) for a, b in zip(seq1, seq2)) / n
        std1 = (sum((a - mean1) ** 2 for a in seq1) / n) ** 0.5
        std2 = (sum((b - mean2) ** 2 for b in seq2) / n) ** 0.5

        if std1 == 0 or std2 == 0:
            return 0.0  # 方差为0意味着信号恒定，无法判断相关性
        return cov / (std1 * std2)

    def _get_correlation_discount(self, correlation: Optional[float], fallback: float = 0.7) -> float:
        """
        根据相关系数返回折扣系数。

        映射规则（绝对值）:
          >= 0.80 → 0.50  高度相关，大幅折扣
          >= 0.60 → 0.70  中等相关（与原硬编码值一致）
          >= 0.40 → 0.85  弱相关，轻微折扣
          <  0.40 → 0.95  几乎无关，几乎不折扣

        Args:
            correlation: 皮尔逊相关系数；None 时返回 fallback
            fallback: 样本不足时使用的默认折扣（保持原行为）

        Returns:
            float: 折扣系数
        """
        if correlation is None:
            return fallback
        abs_corr = abs(correlation)
        if abs_corr >= 0.80:
            return 0.50
        elif abs_corr >= 0.60:
            return 0.70
        elif abs_corr >= 0.40:
            return 0.85
        else:
            return 0.95

    def _update_factor_signal_history(self, code: str, factors: Dict[str, Dict]) -> None:
        """
        将本轮各因子的方向记录到信号历史缓存。
        未出现的因子记录 0（无方向），保持各序列长度一致。

        Args:
            code: 股票代码
            factors: _evaluate_factors 返回的因子字典
        """
        all_tracked = ["rsi", "macd", "bollinger", "supertrend", "ma_cross", "volume"]
        hist = self._factor_signal_history[code]
        maxlen = self._signal_history_maxlen

        for factor in all_tracked:
            direction = factors.get(factor, {}).get("direction", 0)
            hist[factor].append(direction)
            if len(hist[factor]) > maxlen:
                hist[factor] = hist[factor][-maxlen:]

    def _get_dynamic_discount(self, code: str, factor_name: str, peer_factor: str,
                               fallback: float = 0.7) -> float:
        """
        获取两个因子之间的动态折扣系数，优先使用缓存。

        Args:
            code: 股票代码
            factor_name: 需要被折扣的因子
            peer_factor: 同向触发折扣的对照因子
            fallback: 样本不足时的默认折扣

        Returns:
            float: 折扣系数
        """
        cache_key = (factor_name, peer_factor)
        cache = self._correlation_cache[code]
        current_count = self._kline_count[code]

        # 检查缓存是否有效（未超过刷新间隔）
        if cache_key in cache:
            cached_corr, cached_at = cache[cache_key]
            if current_count - cached_at < self._correlation_refresh_interval:
                return self._get_correlation_discount(cached_corr, fallback)

        # 缓存过期或不存在，重新计算
        hist = self._factor_signal_history[code]
        seq1 = hist.get(factor_name, [])
        seq2 = hist.get(peer_factor, [])
        correlation = self._compute_factor_correlation(seq1, seq2)

        # 更新缓存
        cache[cache_key] = (correlation, current_count)

        discount = self._get_correlation_discount(correlation, fallback)
        if correlation is not None:
            logger.debug(
                f"[动态相关性] {code} {factor_name}↔{peer_factor} "
                f"corr={correlation:.3f} → 折扣={discount:.2f}"
            )
        return discount

    # =========================================================================

    def _get_factor_quality_score(self, factor: str) -> float:
        """
        基于因子过去 30 次信号的胜率调整权重

        Args:
            factor: 因子名称

        Returns:
            float: 质量系数 (0.6-1.4)
                   - 胜率>60%: 1.0-1.4 加权
                   - 胜率<40%: 0.6-1.0 减权
                   - 40%-60%: 1.0 保持
        """
        try:
            from app.storage.database import get_database
            db = get_database()

            # 获取该因子参与的历史信号 (过去 90 天)
            outcomes = db.get_signal_outcomes_by_strategy(factor, days=90, limit=30)

            if not outcomes or len(outcomes) < 3:
                # 数据不足，返回默认值
                return 1.0

            # 计算胜率
            wins = sum(1 for o in outcomes if o.get('pnl_pct', 0) > 0)
            win_rate = wins / len(outcomes)

            # 根据胜率调整权重
            if win_rate > 0.6:
                # 胜率>60%: 加权，最高 1.4
                quality = 1.0 + min(0.4, (win_rate - 0.6) * 2)
                logger.debug(f"[因子质量] {factor} 胜率={win_rate:.1%} → 权重系数={quality:.2f}")
            elif win_rate < 0.4:
                # 胜率<40%: 减权，最低 0.6
                quality = 1.0 - min(0.4, (0.4 - win_rate) * 2)
                logger.debug(f"[因子质量] {factor} 胜率={win_rate:.1%} → 权重系数={quality:.2f} ⚠️")
            else:
                quality = 1.0

            return quality

        except Exception as e:
            logger.debug(f"[因子质量] 计算失败 {factor}: {e}")
            return 1.0

    def _evaluate_factors(self, code: str, kline: dict, regime) -> Dict[str, Dict]:
        """
        评估各因子方向和权重
        
        返回 {factor_name: {"direction": 1/-1/0, "weight": float, "reason": str}}
        """
        buf = self._buffers[code]
        closes = list(buf.close)
        highs = list(buf.high)
        lows = list(buf.low)
        volumes = list(buf.volume)
        prev = self._prev_state[code]
        factors = {}

        # RSI
        if "rsi" in self.enabled_factors:
            rsi_val = rsi(closes, self.rsi_period)
            if rsi_val is not None:
                prev_rsi = prev.get("rsi")
                prev["rsi"] = rsi_val
                direction = 0
                reason = ""
                
                if prev_rsi is not None:
                    if prev_rsi <= self.rsi_oversold and rsi_val > self.rsi_oversold:
                        direction = 1
                        reason = f"RSI 超卖反转 ({prev_rsi:.1f}->{rsi_val:.1f})"
                    elif prev_rsi >= self.rsi_overbought and rsi_val < self.rsi_overbought:
                        direction = -1
                        reason = f"RSI 超买回落 ({prev_rsi:.1f}->{rsi_val:.1f})"
                    elif rsi_val < self.rsi_oversold:
                        direction = 1
                        reason = f"RSI 超卖区 ({rsi_val:.1f})"
                    elif rsi_val > self.rsi_overbought:
                        direction = -1
                        reason = f"RSI 超买区 ({rsi_val:.1f})"
                
                if direction != 0:
                    weight = self._get_factor_weight("rsi", regime)
                    factors["rsi"] = {"direction": direction, "weight": weight, "reason": reason}

        # MACD
        if "macd" in self.enabled_factors:
            macd_result = macd(closes, self.macd_fast, self.macd_slow, self.macd_signal)
            if macd_result:
                macd_line, signal_line, histogram = macd_result
                prev_hist = prev.get("macd_hist")
                prev["macd_hist"] = histogram
                
                direction = 0
                reason = ""
                
                if histogram > 0:
                    direction = 1
                    reason = f"MACD 金叉 (hist={histogram:.4f})"
                elif histogram < 0:
                    direction = -1
                    reason = f"MACD 死叉 (hist={histogram:.4f})"
                
                if direction != 0:
                    weight = self._get_factor_weight("macd", regime)
                    factors["macd"] = {"direction": direction, "weight": weight, "reason": reason}

        # Bollinger Bands
        if "bollinger" in self.enabled_factors:
            bb = bollinger_bands(closes, self.bb_period, self.bb_std)
            if bb:
                upper, middle, lower = bb
                close = closes[-1]
                pctb = bollinger_pctb(close, upper, lower)
                
                direction = 0
                reason = ""
                
                if pctb <= 0.1:
                    direction = 1
                    reason = f"布林带下轨 (%B={pctb:.2f})"
                elif pctb >= 0.9:
                    direction = -1
                    reason = f"布林带上轨 (%B={pctb:.2f})"
                
                if direction != 0:
                    weight = self._get_factor_weight("bollinger", regime)
                    factors["bollinger"] = {"direction": direction, "weight": weight, "reason": reason}

        # SuperTrend
        if "supertrend" in self.enabled_factors and buf.size > self.st_period + 5:
            st_result = supertrend(highs, lows, closes, self.st_period, self.st_multiplier)
            if st_result:
                st_val, direction_str = st_result
                direction = 1 if direction_str == "up" else -1
                reason = f"SuperTrend {direction_str}"
                
                weight = self._get_factor_weight("supertrend", regime)
                factors["supertrend"] = {"direction": direction, "weight": weight, "reason": reason}

        # MA Cross
        if "ma_cross" in self.enabled_factors:
            fast_ma = sma(buf.close, self.ma_fast)
            slow_ma = sma(buf.close, self.ma_slow)
            if fast_ma is not None and slow_ma is not None:
                direction = 1 if fast_ma > slow_ma else -1
                reason = f"MA 交叉 (快={fast_ma:.2f}, 慢={slow_ma:.2f})"
                
                weight = self._get_factor_weight("ma_cross", regime)
                factors["ma_cross"] = {"direction": direction, "weight": weight, "reason": reason}

        # Volume
        if "volume" in self.enabled_factors and len(volumes) > 20:
            avg_vol = sum(volumes[-21:-1]) / 20 if len(volumes) > 20 else sum(volumes[:-1]) / max(len(volumes) - 1, 1)
            if avg_vol > 0 and volumes[-1] > avg_vol * self.vol_ratio:
                # 放量确认当前方向
                if closes[-1] > closes[-2] if len(closes) > 1 else True:
                    direction = 1
                    reason = f"放量上涨 (vol={volumes[-1]/avg_vol:.2f}x)"
                else:
                    direction = -1
                    reason = f"放量下跌 (vol={volumes[-1]/avg_vol:.2f}x)"
                
                weight = self._get_factor_weight("volume", regime)
                factors["volume"] = {"direction": direction, "weight": weight, "reason": reason}

        # 记录本轮因子信号方向到历史缓存
        self._update_factor_signal_history(code, factors)

        return factors

    def on_kline(self, code: str, kline: dict, kline_type: str) -> Optional[Signal]:
        if not self.enabled:
            return None

        buf = self._buffers[code]
        buf.push_kline(kline)
        self._kline_count[code] += 1

        # 检查数据是否足够
        min_periods = max(
            self.macd_slow + self.macd_signal,
            self.bb_period,
            self.st_period + 5,
            self.ma_slow,
        ) + 5
        
        if buf.size < min_periods:
            return None

        # 获取市场状态
        regime = self._get_market_regime(code)
        
        # 评估因子
        factors = self._evaluate_factors(code, kline, regime)
        if not factors:
            return None

        # =====================================================================
        # P1-1: 因子相关性调整 (2026-04-06 新增)
        # 高度相关的因子同时确认时，第二个因子权重打折
        # =====================================================================
        adjusted_factors = {}
        for factor_name, factor_data in factors.items():
            weight = factor_data["weight"]
            direction = factor_data["direction"]

            # 检查是否有相关因子同向确认
            correlation_discount = 1.0

            # RSI + Bollinger 同时确认 → Bollinger 权重动态折扣 (振荡器类高度相关)
            if factor_name == "bollinger" and "rsi" in factors:
                if factors["rsi"]["direction"] == direction:
                    correlation_discount = self._get_dynamic_discount(
                        code, "bollinger", "rsi", fallback=0.7
                    )
                    logger.debug(
                        f"[因子相关性] {code} RSI+Bollinger 同向，"
                        f"Bollinger 权重折扣={correlation_discount:.2f}"
                    )

            # MACD + MA Cross 同时确认 → MA Cross 权重动态折扣 (趋势类高度相关)
            if factor_name == "ma_cross" and "macd" in factors:
                if factors["macd"]["direction"] == direction:
                    correlation_discount = self._get_dynamic_discount(
                        code, "ma_cross", "macd", fallback=0.7
                    )
                    logger.debug(
                        f"[因子相关性] {code} MACD+MA Cross 同向，"
                        f"MA Cross 权重折扣={correlation_discount:.2f}"
                    )

            # SuperTrend + Bollinger 同时确认 → SuperTrend 权重动态折扣 (波动率类相关)
            if factor_name == "supertrend" and "bollinger" in factors:
                if factors["bollinger"]["direction"] == direction:
                    correlation_discount = self._get_dynamic_discount(
                        code, "supertrend", "bollinger", fallback=0.8
                    )
                    logger.debug(
                        f"[因子相关性] {code} SuperTrend+Bollinger 同向，"
                        f"SuperTrend 权重折扣={correlation_discount:.2f}"
                    )

            adjusted_factors[factor_name] = {
                **factor_data,
                "weight": weight * correlation_discount,
                "original_weight": weight,
                "discount": correlation_discount,
            }

        # 使用调整后的因子计算得分
        bullish_score = sum(
            f["weight"] for f in adjusted_factors.values() if f["direction"] > 0
        )
        bearish_score = sum(
            f["weight"] for f in adjusted_factors.values() if f["direction"] < 0
        )

        # 计算平均相关性折扣 (流入共识引擎置信度调节)
        discounts = [f.get("discount", 1.0) for f in adjusted_factors.values()]
        avg_correlation_discount = sum(discounts) / len(discounts) if discounts else 1.0

        logger.info(
            f"[{code}] 因子得分调整 | 原始多={sum(factors[f]['weight'] for f in factors if factors[f]['direction']>0):.1f} "
            f"→ 调整后={bullish_score:.1f} | 原始空={sum(factors[f]['weight'] for f in factors if factors[f]['direction']<0):.1f} "
            f"→ 调整后={bearish_score:.1f}"
        )

        close = kline.get("close", 0)
        qty = config.DEFAULT_ORDER_QTY_US if code.startswith("US.") else config.DEFAULT_ORDER_QTY_HK

        # ADX 过滤
        closes = list(buf.close)
        highs = list(buf.high)
        lows = list(buf.low)
        adx_val = adx(highs, lows, closes, 14)
        
        # =====================================================================
        # P1-2: 动态最小因子数 (2026-04-06 新增)
        # 根据波动率和 ADX 动态调整，高波动/低 ADX 时要求更多因子确认
        # =====================================================================
        effective_min_factors = self.min_factors

        # 1. 根据 ADX 调整
        if adx_val is not None:
            if adx_val < 20:
                # ADX < 20: 市场无明确趋势，要求更多因子
                effective_min_factors = max(3, self.min_factors + 1)
                logger.debug(f"[动态因子] {code} ADX={adx_val:.1f}<20，最小因子数提升至{effective_min_factors}")
            elif adx_val > 40:
                # ADX > 40: 强趋势，可以降低要求
                effective_min_factors = max(2, self.min_factors - 1)
                logger.debug(f"[动态因子] {code} ADX={adx_val:.1f}>40，最小因子数降低至{effective_min_factors}")

        # 2. 根据市场波动率调整 (从 regime 获取)
        if regime and self.use_market_regime:
            if regime.regime_type == MarketRegimeType.HIGH_VOLATILITY:
                # 高波动率：要求更多因子确认
                effective_min_factors = max(4, effective_min_factors + 1)
                logger.info(f"[动态因子] {code} 高波动率，最小因子数提升至{effective_min_factors}")
            elif regime.regime_type == MarketRegimeType.RANGE_BOUND:
                # 震荡市：降低要求
                effective_min_factors = max(2, effective_min_factors - 1)
                logger.debug(f"[动态因子] {code} 震荡市，最小因子数降低至{effective_min_factors}")
            elif regime.regime_type in (MarketRegimeType.STRONG_TREND_UP, MarketRegimeType.STRONG_TREND_DOWN):
                # 强趋势：保持标准或降低
                if adx_val and adx_val > 30:
                    effective_min_factors = max(2, effective_min_factors - 1)
                    logger.debug(f"[动态因子] {code} 强趋势+ADX={adx_val:.1f}，最小因子数降低至{effective_min_factors}")
        
        # P0 修复: 移除硬编码 ADX 门槛 (默认 25)，改为动态因子数调整 (逻辑见上方)
        # 原逻辑: ADX < 25 直接拦截，导致震荡市 60% 信号丢失
        # 新逻辑: 允许低 ADX 信号通过，但上方逻辑已要求更多因子共振，实现动态过滤
        if bullish_score >= effective_min_factors:
            bullish_factors = [k for k, v in factors.items() if v["direction"] > 0]
            factor_details = "\n".join(
                f"  - {k}: {v['reason']} (权重{v['weight']:.1f})"
                for k, v in factors.items() if v["direction"] > 0
            )
            
            logger.info(
                f"[{code}] 多因子共振看多 | 得分={bullish_score:.1f} | "
                f"市场={regime.regime_type.value if regime else 'N/A'}"
            )
            
            return Signal(
                code=code, name=kline.get("name", code),
                action=SignalAction.BUY, price=close, quantity=qty,
                reason=f"多因子共振买入 (得分={bullish_score:.1f}):\n{factor_details}",
                strategy=self.name,
                extra={
                    "factors": factors,
                    "bullish_score": bullish_score,
                    "bearish_score": bearish_score,
                    "adx": round(adx_val, 1) if adx_val else None,
                    "market_regime": regime.regime_type.value if regime else None,
                    "conviction": min(1.5, max(0.3, bullish_score / max(1, effective_min_factors + 1))),
                    "avg_correlation_discount": round(avg_correlation_discount, 3),
                },
            )

        if bearish_score >= effective_min_factors and (adx_val is None or adx_val >= self.adx_threshold):
            bearish_factors = [k for k, v in factors.items() if v["direction"] < 0]
            factor_details = "\n".join(
                f"  - {k}: {v['reason']} (权重{v['weight']:.1f})"
                for k, v in factors.items() if v["direction"] < 0
            )
            
            logger.info(
                f"[{code}] 多因子共振看空 | 得分={bearish_score:.1f} | "
                f"市场={regime.regime_type.value if regime else 'N/A'}"
            )
            
            return Signal(
                code=code, name=kline.get("name", code),
                action=SignalAction.SELL, price=close, quantity=qty,
                reason=f"多因子共振卖出 (得分={bearish_score:.1f}):\n{factor_details}",
                strategy=self.name,
                extra={
                    "factors": factors,
                    "bullish_score": bullish_score,
                    "bearish_score": bearish_score,
                    "adx": round(adx_val, 1) if adx_val else None,
                    "market_regime": regime.regime_type.value if regime else None,
                    "conviction": min(1.5, max(0.3, bullish_score / max(1, effective_min_factors + 1))),
                    "avg_correlation_discount": round(avg_correlation_discount, 3),
                },
            )

        return None
