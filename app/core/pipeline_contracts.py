"""
管线数据契约 — FTSeek-US Pipeline Data Contracts

Karpathy 原则: 数据结构优于算法。定义管线各阶段之间的纯数据接口，
使策略、风控、执行各层只依赖这些契约，不依赖具体的 scheduler/engine 对象。

这些是目标接口 — 新策略代码应使用这些契约。现有 SignalScore/Order/Position
是内部持久化模型，两者共存不冲突。

使用方式:
    ctx = SymbolContext.from_raw(symbol, market_data, earnings_status, vix_regime)
    candidate = some_strategy.evaluate(ctx)  # 纯函数, 返回 SignalCandidate 或 None
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from datetime import datetime


# ═══════════════════════════════════════════════════════════════════
#  Stage 1 输出: 增强行情上下文
# ═══════════════════════════════════════════════════════════════════

@dataclass
class SymbolContext:
    """增强行情快照 — 原始行情 + 财报 + VIX Regime + 成本参数, 一次性注入

    所有字段都有默认值, 渐进式填充。上游数据源缺字段时不抛异常,
    下游策略自行检查必需字段是否存在。
    """
    symbol: str = ""

    # 价格层
    price: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    prev_close: float = 0.0
    change_pct: float = 0.0          # 日内涨跌幅 (小数, 如 0.015 = 1.5%)

    # 成交量
    volume: int = 0
    avg_volume: int = 0

    # 技术指标
    rsi: float = 50.0
    macd: float = 0.0
    macd_signal: float = 0.0
    bollinger_upper: float = 0.0
    bollinger_lower: float = 0.0
    supertrend_direction: str = ""    # "UP" / "DOWN" / ""
    atr: float = 0.0
    atr_pct: float = 0.0             # ATR / price

    # 均线
    ma5: float = 0.0
    ma20: float = 0.0
    ma50: float = 0.0
    ma200: float = 0.0

    # 市场环境 (从外部注入)
    vix: float = 0.0
    vix_regime: str = "normal"       # "normal" | "elevated" | "extreme"
    spy_price: float = 0.0
    spy_ma200: float = 0.0
    market_sentiment: float = 0.0    # -1.0 ~ 1.0

    # 事件层 (从外部系统注入)
    earnings_blackout: bool = False
    earnings_date: str = ""          # 最近财报日 YYYY-MM-DD
    has_news_catalyst: bool = False
    news_sentiment: float = 0.0      # -1.0 ~ 1.0

    # 成本参数 (动态注入)
    spread_bps: float = 3.0
    adv_shares: int = 5_000_000

    # === RFC-054: Trend-First 策略所需字段 ===
    market_regime: str = ""              # "TREND_UP"/"TREND_DOWN"/"RANGE"/"HIGH_VOL"/"LOW_VOL"
    trend: str = ""                      # "UP"/"DOWN"/"NEUTRAL" (均线排列方向)
    volume_ratio: float = 1.0            # 当前成交量 / 5日均量
    slope_5m: float = 0.0                # 5分钟价格斜率 (%)
    order_flow_imbalance: float = 0.0    # 买卖力量失衡 [-1, 1]
    ticker_buy_sell_ratio: float = 1.0   # 逐笔买卖比
    high_distance_pct: float = 0.0       # 距日内高点距离 (%)
    spy_ma20: float = 0.0                # SPY 20日均线
    spy_ma50: float = 0.0                # SPY 50日均线

    # === ORB (Opening Range Breakout) 所需字段 ===
    day_high: float = 0.0                 # 日内最高价 (Futu snapshot)
    day_low: float = 0.0                  # 日内最低价 (Futu snapshot)
    open_price: float = 0.0               # 今日开盘价
    opening_high: float = 0.0             # 开盘区间高点 (前15分钟)
    opening_low: float = 0.0              # 开盘区间低点 (前15分钟)
    opening_range_ready: bool = False     # 开盘区间是否已确立 (≥15根1分钟K线)

    # 元数据
    source: str = ""                 # "futu_lv3" / "twelvedata" / "finnhub"
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def mid_price(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.price

    @property
    def spread_ratio(self) -> float:
        """价差百分比"""
        mid = self.mid_price
        if mid > 0 and self.bid > 0 and self.ask > 0:
            return (self.ask - self.bid) / mid
        return 0.0

    @property
    def is_ma200_bullish(self) -> bool:
        """SPY > MA200"""
        return self.spy_price > 0 and self.spy_ma200 > 0 and self.spy_price > self.spy_ma200

    @property
    def is_liquid(self) -> bool:
        """流动性充足"""
        return self.volume > 0 and self.spread_ratio < 0.01

    @staticmethod
    def from_raw(symbol: str, context: Dict[str, Any],
                 earnings_status: Optional[Dict] = None,
                 vix_multipliers: Optional[Dict] = None) -> "SymbolContext":
        """从原始 dict context 构造 — 兼容现有 market_data.get_full_market_context() 输出"""
        ctx = SymbolContext(symbol=symbol)
        ctx.price = float(context.get("price", 0) or 0)
        ctx.bid = float(context.get("lv3_bid", context.get("bid", 0)) or 0)
        ctx.ask = float(context.get("lv3_ask", context.get("ask", 0)) or 0)
        ctx.prev_close = float(context.get("prev_close", 0) or 0)
        cp = context.get("change_percent", 0)
        ctx.change_pct = (float(cp) / 100.0) if cp else 0.0
        ctx.volume = int(context.get("volume", 0) or 0)
        ctx.avg_volume = int(context.get("avg_volume", 0) or 0)
        ctx.rsi = float(context.get("rsi", 50.0) or 50.0)
        ctx.macd = float(context.get("macd", 0) or 0)
        ctx.macd_signal = float(context.get("macd_signal", 0) or 0)
        ctx.bollinger_upper = float(context.get("bollinger_upper", context.get("boll_upper", context.get("bb_upper", 0))) or 0)
        ctx.bollinger_lower = float(context.get("bollinger_lower", context.get("boll_lower", context.get("bb_lower", 0))) or 0)
        ctx.supertrend_direction = str(context.get("supertrend_direction", "") or "")
        ctx.atr = float(context.get("atr", 0) or 0)
        ctx.atr_pct = (ctx.atr / ctx.price) if ctx.price > 0 else 0.0
        ctx.ma5 = float(context.get("ma5", 0) or 0)
        ctx.ma20 = float(context.get("ma20", 0) or 0)
        ctx.ma50 = float(context.get("ma50", 0) or 0)
        ctx.ma200 = float(context.get("ma200", 0) or 0)
        ctx.spread_bps = float(context.get("spread_bps", 3.0) or 3.0)
        ctx.adv_shares = int(context.get("adv_shares", 5_000_000) or 5_000_000)

        if earnings_status:
            ctx.earnings_blackout = earnings_status.get("is_blackout", False)
            ctx.earnings_date = earnings_status.get("earnings_date", "")

        if vix_multipliers:
            ctx.vix_regime = vix_multipliers.get("regime", "normal")
            ctx.vix = float(vix_multipliers.get("vix", 0) or 0)

        # RFC-054 Trend-First fields
        ctx.market_regime = str(context.get('market_regime', '') or '')
        ctx.trend = str(context.get('trend', '') or '')
        ctx.volume_ratio = float(context.get('volume_ratio', 1.0) or 1.0)
        ctx.slope_5m = float(context.get('slope_5m', 0.0) or 0.0)
        ctx.order_flow_imbalance = float(context.get('order_flow_imbalance', 0.0) or 0.0)
        ctx.ticker_buy_sell_ratio = float(context.get('ticker_buy_sell_ratio', 1.0) or 1.0)
        ctx.high_distance_pct = float(context.get('high_distance_pct', 0.0) or 0.0)
        ctx.spy_ma20 = float(context.get('spy_ma20', 0.0) or 0.0)
        ctx.spy_ma50 = float(context.get('spy_ma50', 0.0) or 0.0)

        # ORB fields
        ctx.day_high = float(context.get('day_high', 0.0) or 0.0)
        ctx.day_low = float(context.get('day_low', 0.0) or 0.0)
        ctx.open_price = float(context.get('open_price', 0.0) or 0.0)
        ctx.opening_high = float(context.get('opening_high', 0.0) or 0.0)
        ctx.opening_low = float(context.get('opening_low', 0.0) or 0.0)
        ctx.opening_range_ready = bool(context.get('opening_range_ready', False))

        return ctx


# ═══════════════════════════════════════════════════════════════════
#  Stage 2 输出: 信号候选
# ═══════════════════════════════════════════════════════════════════

@dataclass
class SignalCandidate:
    """策略输出 — 经过信号生成但尚未通过风控的候选交易

    每个策略 (快车道/技术面/LLM投票) 输出一个或零个候选。
    多个候选由 PortfolioConstructor 排序择优。

    2026-05-17 增强 (修 R8): 加 trace_id / factor_detail / signal_tags 字段, 让 stages
    管线产出的候选直接携带归因信息, 不再依赖下游 _process_symbol_new_pipeline 临时构造.
    """
    symbol: str
    direction: str                      # "LONG" | "SHORT" (用 str 而非 Direction 以便序列化)
    strength: int = 0                   # 0-100
    confidence: float = 0.0             # 0.0-1.0
    strategy_id: str = ""               # 策略标识
    source: str = ""                    # "fast_lane" | "technical" | "voting_llm"

    # 策略附加上下文
    rationale: str = ""                 # 策略决策理由
    entry_zone: List[float] = field(default_factory=list)  # [low, high]
    catalyst: str = ""
    urgency: str = "NORMAL"             # "NORMAL" | "URGENT"

    # 关联的原始上下文
    context: Optional[SymbolContext] = None

    # 元数据
    created_at: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ── R8 修复 (2026-05-17): 归因字段 — stages 产出时填, _submit_order 直接消费 ──
    trace_id: str = ""                                              # 全链路追溯 ID
    factor_detail: Dict[str, Any] = field(default_factory=dict)     # 5 因子明细 (trend/momentum/volume/orderflow/lifecycle)
    signal_tags: List[str] = field(default_factory=list)            # 因子 tag 列表 (复盘归因 SQL 友好)

    @property
    def is_long(self) -> bool:
        return self.direction.upper() == "LONG"

    @property
    def is_short(self) -> bool:
        return self.direction.upper() == "SHORT"

    @property
    def effective_strength(self) -> int:
        """SHORT 方向转换为 effective strength (100 - strength)"""
        return (100 - self.strength) if self.is_short else self.strength

    @staticmethod
    def from_fast_lane(match: Dict[str, Any]) -> "SignalCandidate":
        """从作战计划匹配结果构造"""
        direction = str(match.get("direction", "LONG")).upper()
        return SignalCandidate(
            symbol=str(match.get("symbol", "")),
            direction=direction,
            strength=int(float(match.get("confidence", 0.7)) * 100),
            confidence=0.85,
            strategy_id="BATTLE_PLAN_FAST_PATH",
            source="fast_lane",
            entry_zone=match.get("entry_zone", []),
            catalyst=str(match.get("catalyst", "")),
            urgency=str(match.get("urgency", "NORMAL")).upper(),
        )

    @staticmethod
    def from_vote_result(symbol: str, vote: Dict[str, Any]) -> "SignalCandidate":
        """从 LLM 投票结果构造"""
        return SignalCandidate(
            symbol=symbol,
            direction=str(vote.get("direction", "NEUTRAL")).upper(),
            strength=int(vote.get("final_strength", 50)),
            confidence=float(vote.get("consensus_level", 0.0)),
            strategy_id="voting_llm",
            source="voting_llm",
            rationale=str(vote.get("summary", "")),
        )


# ═══════════════════════════════════════════════════════════════════
#  Stage 3 输出: 审批后的订单请求
# ═══════════════════════════════════════════════════════════════════

@dataclass
class OrderRequest:
    """风控审批后的可执行订单 — 已通过所有预交易检查, 可直接提交执行引擎"""
    candidate: SignalCandidate

    # 仓位计算
    quantity: int = 0
    order_type: str = "LIMIT"           # "MARKET" | "LIMIT"

    # 风险参数
    stop_loss: float = 0.0
    take_profit_levels: List[Dict[str, float]] = field(default_factory=list)

    # 风控结果
    risk_approved: bool = False
    risk_checks_passed: List[str] = field(default_factory=list)
    risk_notes: List[str] = field(default_factory=list)  # 警告信息, 不阻止执行

    # 执行元数据
    entry_session: str = ""             # "opening_drive" | "morning" | "midday" | "afternoon" | "closing_drive"
    atr_mult: float = 1.5
    created_at: datetime = field(default_factory=datetime.now)

    @property
    def symbol(self) -> str:
        return self.candidate.symbol

    @property
    def direction(self) -> str:
        return self.candidate.direction
