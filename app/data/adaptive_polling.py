#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能自适应轮询控制器 (Adaptive Polling Controller) - FTSeek-US

设计原则：
1. 免费 API 限流是硬约束 → 不能高频轮询所有股票
2. 美股波动大 → 需要快速响应持仓股异动
3. 无实时推送 → 用本地预测填补两次 API 调用之间的空白

架构：
┌─────────────────────────────────────────────────────────────────┐
│                    智能自适应轮询控制器                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────────────────┐   │
│  │ 分层轮询调度 │→ │ 事件驱动触发 │→ │ 本地价格预测引擎     │   │
│  │ (3个优先级) │  │ (新闻/SPY)  │  │ (API 调用间估算)     │   │
│  └─────────────┘  └─────────────┘  └──────────────────────┘   │
│         ↓                    ↓                    ↓             │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              多数据源智能轮换层                           │   │
│  │  TwelveData (8/min) → Finnhub (60/min) → AV (5/min)   │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│  核心指标：每轮扫描 API 调用次数可控，持仓股响应 < 5 分钟        │
└─────────────────────────────────────────────────────────────────┘
"""

import logging
import time
import math
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("ftseek_us.adaptive_polling")


# ============================================================
#  股票优先级分层
# ============================================================
class StockTier(str, Enum):
    """
    股票轮询优先级分层

    P0 (持仓股):     每 3-5 分钟轮询一次 → 止损/止盈必须及时
    P1 (高波动股):   每 10-15 分钟轮询一次 → 潜在交易机会
    P2 (普通观察股): 每 30 分钟轮询一次 → 常规扫描
    """
    POSITION = "P0_POSITION"       # 持仓中 (最高优先级)
    HIGH_VOLATILITY = "P1_HIGH_VOL"  # 高波动/新闻活跃
    WATCHLIST = "P2_WATCHLIST"     # 普通观察列表


@dataclass
class StockPollingConfig:
    """单只股票的轮询配置"""
    symbol: str
    tier: StockTier
    # 轮询间隔（秒）
    min_interval: int       # 最短间隔（波动大时）
    max_interval: int       # 最长间隔（波动小时）
    current_interval: int   # 当前实际间隔（自适应调整）
    # 状态
    last_poll_time: float = 0.0
    last_price: float = 0.0
    last_change_pct: float = 0.0
    volatility_24h: float = 0.0  # 24 小时波动率
    consecutive_unchanged: int = 0  # 连续价格不变次数
    interval_shortened: bool = False  # 标记：本轮间隔是否刚被缩短（用于日志节流）

    def should_poll(self, current_time: float) -> bool:
        """判断是否应该轮询"""
        if self.last_poll_time == 0:
            return True  # 从未轮询过
        return (current_time - self.last_poll_time) >= self.current_interval

    def adjust_interval(self, price: float, change_pct: float):
        """
        根据价格变化自适应调整轮询间隔

        规则：
        - 价格变化 > 3% → 缩短间隔 (加快响应)
        - 价格变化 1-3% → 保持当前间隔
        - 价格变化 < 1% → 延长间隔 (节省 API)
        - 连续 3 次不变 → 直接跳到 max_interval
        """
        price_change = abs(change_pct) if change_pct else 0

        if price_change > 0.03:  # > 3%
            # 已禁用：不再因波动缩短间隔，以节省 API 额度
            pass
            self.consecutive_unchanged = 0
            logger.debug(f"[轮询检查] {self.symbol} 波动 {price_change:.1%}, 保持间隔 {self.current_interval}s")
        elif price_change > 0.01:  # 1-3%
            # 保持当前间隔，重置连续不变计数
            self.consecutive_unchanged = 0
        elif price_change > 0.001:  # 0.1-1%
            # 小幅波动，稍微延长
            self.current_interval = min(
                self.max_interval,
                int(self.current_interval * 1.2)
            )
            self.consecutive_unchanged = 0
        else:  # < 0.1% 几乎没动
            self.consecutive_unchanged += 1
            if self.consecutive_unchanged >= 3:
                self.current_interval = self.max_interval
                logger.debug(
                    f"[轮询调整] {self.symbol} 连续 {self.consecutive_unchanged} 次无变化 → "
                    f"间隔延长至 {self.max_interval}s"
                )
            else:
                self.current_interval = min(
                    self.max_interval,
                    int(self.current_interval * 1.1)
                )

        self.last_price = price
        self.last_change_pct = change_pct


# ============================================================
#  本地价格预测引擎
# ============================================================
class LocalPricePredictor:
    """
    在两次 API 调用之间，基于技术指标估算当前价格区间

    用途：
    - 持仓股在轮询间隔内，用预测值判断是否需要提前触发止损
    - 减少不必要的 API 调用
    """

    def __init__(self):
        # 缓存每只股票的技术参数
        self._indicators: Dict[str, Dict] = {}

    def update_indicators(self, symbol: str, indicators: Dict):
        """更新技术指标缓存"""
        self._indicators[symbol] = {
            "price": indicators.get("price", 0),
            "ma5": indicators.get("ma5", 0),
            "ma20": indicators.get("ma20", 0),
            "atr": indicators.get("atr", 0),
            "rsi": indicators.get("rsi", 50),
            "trend": indicators.get("trend", "NEUTRAL"),
            "volatility": indicators.get("volatility", 0),
            "update_time": time.time(),
        }

    def estimate_current_price(
        self,
        symbol: str,
        time_since_last_update: float,
        market_sentiment: float = 0.0,
    ) -> Optional[Dict[str, float]]:
        """
        估算当前价格区间（非精确值，仅用于风控判断）

        Returns:
            {"estimated_price": float, "upper_bound": float, "lower_bound": float, "confidence": float}
            或 None (无缓存数据)
        """
        indicators = self._indicators.get(symbol)
        if not indicators:
            return None

        price = indicators["price"]
        atr = indicators.get("atr", price * 0.02)
        volatility = indicators.get("volatility", 0.02)
        trend = indicators.get("trend", "NEUTRAL")

        # 基于时间的漂移估算
        # 假设价格服从几何布朗运动，期望漂移 ~0，方差随时间增长
        hours_elapsed = time_since_last_update / 3600.0
        expected_move = volatility * math.sqrt(hours_elapsed) * price

        # 趋势调整
        trend_bias = 0.0
        if trend == "UP":
            trend_bias = expected_move * 0.3  # 向上趋势给 30% 正向偏置
        elif trend == "DOWN":
            trend_bias = -expected_move * 0.3

        # 市场情绪调整
        sentiment_bias = market_sentiment * expected_move * 0.2

        estimated = price + trend_bias + sentiment_bias
        upper = estimated + expected_move * 1.0
        lower = estimated - expected_move * 1.0

        # 置信度随时间衰减
        confidence = max(0.3, 1.0 - hours_elapsed * 0.15)  # 每过 1 小时降 15%

        return {
            "estimated_price": estimated,
            "upper_bound": upper,
            "lower_bound": lower,
            "confidence": confidence,
        }

    def check_stop_loss_risk(
        self,
        symbol: str,
        stop_loss_price: float,
        time_since_last_update: float,
        market_sentiment: float = 0.0,
    ) -> Tuple[bool, float]:
        """
        检查预估价格是否触及止损线

        Returns:
            (is_at_risk, risk_probability)
        """
        # 2026-04-20 修复：仅在数据陈旧（>15分钟）时启用预估风控
        # 1. 如果有最新数据，应直接使用实时价格检查止损，而非依赖预测模型。
        # 2. 刚启动或刚轮询过时，预测模型可能因缓存未同步导致计算异常。
        # 3. 避免盘后时段因时间跨度大导致方差发散误报。
        if time_since_last_update < 900:
            return False, 0.0

        estimation = self.estimate_current_price(symbol, time_since_last_update, market_sentiment)
        if not estimation:
            return False, 0.0

        lower_bound = estimation["lower_bound"]
        confidence = estimation["confidence"]
        
        # 额外保护：如果置信度过低，忽略风控警报
        if confidence < 0.6:
            return False, 0.0

        # 如果下界已经接近止损线，认为有风险
        if lower_bound <= stop_loss_price * 1.01:  # 1% 容差
            risk_prob = max(confidence, 0.5)
            return True, risk_prob

        # 如果下界在止损线 3% 以内，中等风险
        if lower_bound <= stop_loss_price * 1.03:
            return True, confidence * 0.5

        return False, 0.0


# ============================================================
#  事件驱动触发器
# ============================================================
class EventDrivenTrigger:
    """
    当外部事件发生时，立即触发相关股票的轮询

    事件类型：
    1. 重大新闻 (LLM 判定 HIGH impact)
    2. SPY/QQQ 异动 (> 1% 波动)
    3. VIX 飙升 (> 25)
    4. 持仓股关联新闻
    """

    def __init__(self, polling_controller: "AdaptivePollingController"):
        self.controller = polling_controller
        self._last_spy_check: float = 0
        self._last_spy_price: float = 0

    def on_major_news(self, symbols: List[str], impact: str = "HIGH"):
        """重大新闻事件 → 立即轮询相关股票"""
        if impact == "HIGH":
            for sym in symbols:
                self.controller.force_poll(sym, reason=f"重大新闻 ({impact})")
                logger.info(f"[事件驱动] {sym} 因重大新闻触发立即轮询")

    def on_market_move(self, spy_change_pct: float, vix: float):
        """
        大盘异动 → 轮询所有持仓股 + 高波动股

        触发条件：
        - SPY 涨跌 > 0.5%
        - VIX > 25
        """
        now = time.time()
        if now - self._last_spy_check < 300:  # 5 分钟内不重复触发
            return

        self._last_spy_check = now

        triggered = False
        if abs(spy_change_pct) > 0.005:  # SPY 波动 > 0.5%
            triggered = True
            logger.warning(
                f"[事件驱动] SPY 异动 {spy_change_pct:+.1%} → 触发持仓股轮询"
            )
        if vix > 25:
            triggered = True
            logger.warning(f"[事件驱动] VIX={vix:.1f} > 25 → 触发全部轮询")

        if triggered:
            self.controller.force_poll_tier(
                StockTier.POSITION,
                reason=f"大盘异动 (SPY={spy_change_pct:+.1%}, VIX={vix:.1f})"
            )
            if abs(spy_change_pct) > 0.01 or vix > 30:
                # 更大波动时，连 P1 也触发
                self.controller.force_poll_tier(
                    StockTier.HIGH_VOLATILITY,
                    reason=f"大盘剧烈波动 (SPY={spy_change_pct:+.1%}, VIX={vix:.1f})"
                )

    def on_position_stop_loss_nearby(self, symbol: str, stop_loss: float, current_price: float):
        """持仓股接近止损 → 缩短轮询间隔"""
        distance_pct = abs(current_price - stop_loss) / current_price
        if distance_pct < 0.03:  # 距止损 < 3%
            self.controller.force_poll(symbol, reason=f"接近止损 (距 {distance_pct:.1%})")
            # 缩短该股的轮询间隔
            cfg = self.controller.get_config(symbol)
            if cfg:
                cfg.current_interval = max(cfg.min_interval, 180)  # 最短 3 分钟
                logger.warning(
                    f"[事件驱动] {symbol} 距止损仅 {distance_pct:.1%} → "
                    f"轮询间隔缩短至 3 分钟"
                )


# ============================================================
#  主控制器
# ============================================================
class AdaptivePollingController:
    """
    智能自适应轮询控制器

    使用方式：
    1. 初始化时注册股票池
    2. 调用 get_polling_batch() 获取本轮应该轮询的股票
    3. 调用 update_result() 更新轮询结果，自动调整间隔
    4. 事件驱动场景调用 force_poll() 立即轮询
    """

    def __init__(self, api_budget_per_scan: int = 12):
        """
        Args:
            api_budget_per_scan: 每轮扫描最大 API 调用次数
                - 旧版: 12 (受 TwelveData 8/min 限制)
                - 新版: 股票总数 (Futu LV3 本地快照无限制, 2026-04-20)
        """
        self.api_budget = api_budget_per_scan
        self._configs: Dict[str, StockPollingConfig] = {}
        self._force_poll_queue: List[Tuple[str, str]] = []  # (symbol, reason)
        self.predictor = LocalPricePredictor()
        self.event_trigger = EventDrivenTrigger(self)
        self._lock = __import__("threading").Lock()

        # 统计
        self.stats = {
            "total_polls": 0,
            "force_polls": 0,
            "api_calls_this_scan": 0,
            "last_scan_time": 0.0,
        }

    # ── 注册/配置 ──────────────────────────────────────────────

    def register_symbol(self, symbol: str, tier: StockTier, initial_price: float = 0):
        """注册股票到轮询系统
        
        P2 修复 (2026-04-16): 优化轮询间隔以适应 Finnhub 60次/分钟限制
        目标: 5-10 只股票，总 API 调用 < 40次/分钟
        """
        with self._lock:
            # 优化后的间隔 (平衡响应速度和 API 限制)
            intervals = {
                StockTier.POSITION: (180, 600),        # P0: 3-10 分钟 (持仓股优先)
                StockTier.HIGH_VOLATILITY: (300, 900), # P1: 5-15 分钟 (TIER1 活跃股)
                StockTier.WATCHLIST: (900, 1800),      # P2: 15-30 分钟 (TIER2/ETF)
            }
            min_int, max_int = intervals.get(tier, (600, 1800))

            self._configs[symbol] = StockPollingConfig(
                symbol=symbol,
                tier=tier,
                min_interval=min_int,
                max_interval=max_int,
                current_interval=min_int,  # P2 修复: 从最短间隔开始, 确保首次扫描能覆盖
                last_price=initial_price,
            )
            logger.info(
                f"[轮询注册] {symbol} 层级={tier.value} "
                f"间隔={min_int}-{max_int}s"
            )

    def update_tier(self, symbol: str, new_tier: StockTier):
        """动态调整股票层级 (如开仓后从 P2 → P0)"""
        with self._lock:
            if symbol in self._configs:
                cfg = self._configs[symbol]
                cfg.tier = new_tier
                # P2 修复 (2026-04-16): 与 register_symbol 保持一致的间隔
                intervals = {
                    StockTier.POSITION: (180, 600),
                    StockTier.HIGH_VOLATILITY: (300, 900),
                    StockTier.WATCHLIST: (900, 1800),
                }
                min_int, max_int = intervals.get(new_tier, (600, 1800))
                cfg.min_interval = min_int
                cfg.max_interval = max_int
                cfg.current_interval = min_int  # 切换层级时立即缩短
                logger.info(
                    f"[轮询调整] {symbol} 层级 {new_tier.value} → "
                    f"间隔重置为 {min_int}s"
                )

    # ── 轮询调度 ──────────────────────────────────────────────

    def get_polling_batch(self) -> List[str]:
        """
        获取本轮应该轮询的股票列表

        优先级：
        1. 强制轮询队列 (事件驱动)
        2. P0 持仓股 (到期的)
        3. P1 高波动股 (到期的)
        4. P2 普通股 (到期的，直到 API 预算耗尽)
        """
        now = time.time()
        batch = []

        with self._lock:
            # 1. 强制轮询队列优先
            while self._force_poll_queue and len(batch) < self.api_budget:
                symbol, reason = self._force_poll_queue.pop(0)
                if symbol in self._configs:
                    batch.append(symbol)
                    self.stats["force_polls"] += 1
                    logger.debug(f"[轮询批次] 强制: {symbol} ({reason})")

            # 2. 按层级顺序获取到期股票
            for tier in [StockTier.POSITION, StockTier.HIGH_VOLATILITY, StockTier.WATCHLIST]:
                remaining_budget = self.api_budget - len(batch)
                if remaining_budget <= 0:
                    break

                for sym, cfg in self._configs.items():
                    if cfg.tier != tier:
                        continue
                    if cfg.should_poll(now):
                        batch.append(sym)
                        if len(batch) >= self.api_budget:
                            break

            self.stats["api_calls_this_scan"] = len(batch)
            self.stats["last_scan_time"] = now

        return batch

    def update_result(self, symbol: str, price: float, change_pct: float, indicators: Dict = None):
        """
        更新轮询结果，触发自适应调整

        Args:
            symbol: 股票代码
            price: 最新价格
            change_pct: 涨跌幅
            indicators: 技术指标 (用于预测引擎)
        """
        with self._lock:
            cfg = self._configs.get(symbol)
            if not cfg:
                return

            cfg.adjust_interval(price, change_pct)
            cfg.last_poll_time = time.time()
            self.stats["total_polls"] += 1

            # 更新预测引擎
            if indicators:
                self.predictor.update_indicators(symbol, indicators)

            # 检查是否需要事件驱动响应 (仅在间隔真正缩短时报警，避免刷屏)
            if cfg.tier == StockTier.POSITION and abs(change_pct) > 0.02 and cfg.interval_shortened:
                logger.warning(
                    f"[自适应] {symbol} 大幅波动 {change_pct:+.1%} → "
                    f"间隔已自动缩短至 {cfg.current_interval}s"
                )
                cfg.interval_shortened = False  # 消耗标记

    # ── 强制轮询 ──────────────────────────────────────────────

    def force_poll(self, symbol: str, reason: str = "manual"):
        """立即加入轮询队列"""
        with self._lock:
            self._force_poll_queue.append((symbol, reason))

    def force_poll_tier(self, tier: StockTier, reason: str = "event"):
        """强制轮询某个层级的所有股票"""
        with self._lock:
            for sym, cfg in self._configs.items():
                if cfg.tier == tier:
                    self._force_poll_queue.append((sym, f"{reason} [{tier.value}]"))

    def get_config(self, symbol: str) -> Optional[StockPollingConfig]:
        """获取股票轮询配置"""
        return self._configs.get(symbol)

    # ── 风控集成 ──────────────────────────────────────────────

    def check_stop_loss_risk_for_positions(self, positions: Dict[str, Dict]) -> List[Dict]:
        """
        检查所有持仓股的预估止损风险

        Args:
            positions: {symbol: {"stop_loss": float, "entry_price": float}}

        Returns:
            有风险的头寸列表
        """
        at_risk = []
        for symbol, pos_info in positions.items():
            stop_loss = pos_info.get("stop_loss", 0)
            if stop_loss <= 0:
                continue

            cfg = self._configs.get(symbol)
            if not cfg:
                continue

            time_since_poll = time.time() - cfg.last_poll_time
            is_risk, prob = self.predictor.check_stop_loss_risk(
                symbol, stop_loss, time_since_poll
            )
            if is_risk:
                at_risk.append({
                    "symbol": symbol,
                    "stop_loss": stop_loss,
                    "last_price": cfg.last_price,
                    "risk_probability": prob,
                    "time_since_poll": time_since_poll,
                    "estimated_lower": self.predictor.estimate_current_price(
                        symbol, time_since_poll
                    ).get("lower_bound", 0),
                })
                # 自动触发立即轮询
                self.force_poll(symbol, reason=f"预估触及止损 (概率 {prob:.0%})")

        return at_risk

    # ── 统计 ──────────────────────────────────────────────────

    def get_status(self) -> Dict:
        """获取轮询系统状态"""
        with self._lock:
            tier_counts = {}
            for cfg in self._configs.values():
                tier = cfg.tier.value
                if tier not in tier_counts:
                    tier_counts[tier] = {"count": 0, "next_poll_in": []}
                tier_counts[tier]["count"] += 1
                next_in = max(0, cfg.current_interval - (time.time() - cfg.last_poll_time))
                tier_counts[tier]["next_poll_in"].append(int(next_in))

            # 计算每个层级的下次轮询时间
            for tier_info in tier_counts.values():
                if tier_info["next_poll_in"]:
                    tier_info["min_next_poll"] = min(tier_info["next_poll_in"])
                    tier_info["max_next_poll"] = max(tier_info["next_poll_in"])
                    tier_info["avg_next_poll"] = sum(tier_info["next_poll_in"]) // len(tier_info["next_poll_in"])
                del tier_info["next_poll_in"]

            return {
                "total_symbols": len(self._configs),
                "tier_counts": tier_counts,
                "force_queue_size": len(self._force_poll_queue),
                "api_budget": self.api_budget,
                "stats": self.stats.copy(),
                "rate_limits": self._get_rate_limit_summary(),
            }

    def _get_rate_limit_summary(self) -> Dict:
        """从 MarketDataRouter 获取限速摘要 (需要外部注入)"""
        # 此方法由外部调用 router.get_rate_limit_status() 后更新
        return {}
