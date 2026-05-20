#!/usr/bin/env python3
"""
Whetstone US v2 — Karpathy-style Multi-Symbol Scalping Sharpening Stone

原则:
  - 大量快速迭代 → 数据驱动策略优化
  - 每笔交易都是"磨刀"的一次反馈
  - A/B 策略变体对比, 用数据说话
  - 影子账户独立核算, 不污染主系统

标的池: US.SNDK US.NIO US.AMD US.MU US.LCID (日内振幅>4% 高波动)
策略:   A=动量突破  B=均值回归  双策略并行对比
运行:   python scripts/whetstone_us.py

数据表:
  shadow_trades      — 逐笔交易 (含 strategy_variant 区分 A/B)
  shadow_daily_stats — 日级聚合 (按标的+策略双维度)
"""

import os, sys, json, time, math, uuid, logging, argparse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from collections import defaultdict, deque
from enum import Enum

PROJECT_DIR = "/opt/ftseek/FTSeek-US"
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_DIR, ".env.us"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("whetstone")

# ═══════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════

WHETSTONE_SYMBOLS = ["US.SNDK", "US.NIO", "US.AMD", "US.MU", "US.LCID"]

SHADOW_CAPITAL = 100000.0
MAX_POSITION_PCT = 0.08    # 单笔 8%
MAX_TOTAL_PCT = 0.35       # 总仓位 35%
MAX_CONCURRENT = 6         # 最大并发持仓
RISK_PER_TRADE_PCT = 0.004 # 每笔风险 0.4%

SIGNAL_SCORE_THRESHOLD = 40  # 入场阈值 (从50降到40, 更多交易)
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
MOMENTUM_THRESHOLD = 0.0015  # 0.15% 动量突破 (从0.3%降到0.15%)
REVERSION_RSI_OS = 28       # 回归策略超卖 (更严格)
REVERSION_RSI_OB = 72       # 回归策略超买 (更严格)

STRATEGY_A = "momentum_breakout"   # 动量突破: 顺势追涨杀跌
STRATEGY_B = "mean_reversion"     # 均值回归: 逆势抄底摸顶
STRATEGY_A_MICRO = "momentum_micro"  # 微动量: quote-level 突破 (更快)

MIN_TRADES_PER_SYMBOL = 3   # Karpathy: 每日每标的保底交易数
MAX_DAILY_LOSS_PER_SYMBOL = 100.0  # 单标的日最大亏损
POLL_INTERVAL = 3  # 秒 (3s 采样率 → 15s 内5个tick, 足够检测微动量)

# ═══════════════════════════════════════════════════════════
#  Enums & Dataclasses
# ═══════════════════════════════════════════════════════════

class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

class ExitReason(str, Enum):
    STOP_LOSS = "SL"
    TAKE_PROFIT = "TP"
    TRAILING_STOP = "TRAIL"
    TIME_STOP = "TIME"
    EOD_CLOSE = "EOD"
    SIGNAL_REVERSE = "REV"

@dataclass
class Bar:
    timestamp: datetime
    open: float; high: float; low: float; close: float; volume: float

@dataclass
class Quote:
    symbol: str; timestamp: datetime
    last: float; bid: float; ask: float
    high: float; low: float; volume: float

@dataclass
class TickSnapshot:
    """每轮 poll 的微快照 — 不依赖 K 线收盘"""
    timestamp: datetime
    last: float
    bid: float
    ask: float
    bid_vol: float = 0
    ask_vol: float = 0

class TickTracker:
    """追踪 quote-level 微动量, 解决 K_1M 滞后问题.

    每 3s poll 一次 last_price, 15-30s 即可检测突破,
    比等 K_1M 收盘快 30-90 秒.
    """
    def __init__(self, max_ticks=60):
        self.ticks: deque = deque(maxlen=max_ticks)

    def feed(self, quote: Quote):
        self.ticks.append(TickSnapshot(
            timestamp=quote.timestamp, last=quote.last,
            bid=quote.bid, ask=quote.ask,
        ))

    @property
    def count(self): return len(self.ticks)

    def momentum(self, seconds=15):
        """价格在最近 N 秒内的变化率"""
        if len(self.ticks) < 2: return 0.0
        cutoff = datetime.now() - timedelta(seconds=seconds)
        window = [t for t in self.ticks if t.timestamp >= cutoff]
        if len(window) < 2: return 0.0
        return (window[-1].last - window[0].last) / window[0].last if window[0].last > 0 else 0

    def acceleration(self):
        """mom_15s - mom_30s: 正=加速, 负=减速"""
        return self.momentum(15) - self.momentum(30)

    def spread_pct(self):
        """当前 bid/ask 价差比例"""
        if not self.ticks: return 0.01
        t = self.ticks[-1]
        if t.bid <= 0 or t.ask <= 0: return 0.01
        return (t.ask - t.bid) / t.bid

    def spread_tightening(self):
        """价差是否在收窄 (突破前兆)"""
        if len(self.ticks) < 6: return False
        recent = list(self.ticks)[-6:]
        spreads = [(t.ask - t.bid) / t.bid if t.bid > 0 else 0.01 for t in recent]
        first_half = sum(spreads[:3]) / 3
        second_half = sum(spreads[3:]) / 3
        return second_half < first_half * 0.9

    def bid_ask_bias(self):
        """买卖盘偏向: 正=买盘强 (last靠近ask), 负=卖盘强 (last靠近bid).

        使用 (last - bid) / spread - 0.5, 映射到 [-0.5, +0.5].
        加最小 spread 保护防止除零溢出.
        """
        if len(self.ticks) < 3: return 0.0
        recent = list(self.ticks)[-3:]
        biases = []
        for t in recent:
            if t.bid <= 0: continue
            spread = max(t.ask - t.bid, t.bid * 0.0001)  # 最小 0.01% spread
            # (last-bid)/spread in [0,1], then center to [-0.5, 0.5]
            raw = (t.last - t.bid) / spread
            biases.append(raw - 0.5)
        return sum(biases) / len(biases) if biases else 0.0

@dataclass
class ShadowTrade:
    trade_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    symbol: str = ""
    strategy: str = ""          # A or B
    direction: Direction = Direction.LONG
    signal_score: int = 0
    signal_detail: str = ""     # compact JSON of signal breakdown

    entry_time: datetime = None
    exit_time: datetime = None
    entry_price: float = 0.0
    exit_price: float = 0.0
    quantity: int = 0

    entry_slippage_pct: float = 0.0
    spread_pct: float = 0.0
    commission: float = 0.0

    realized_pnl: float = 0.0
    pnl_pct: float = 0.0
    pnl_r: float = 0.0         # R-multiples

    exit_reason: ExitReason = None
    max_favorable_pct: float = 0.0
    max_adverse_pct: float = 0.0
    holding_minutes: float = 0.0
    market_phase: str = ""
    closed: bool = False

# ═══════════════════════════════════════════════════════════
#  Signal Engine — per-candle computation
# ═══════════════════════════════════════════════════════════

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff > 0: gains += diff
        else: losses += abs(diff)
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0: return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

def calc_atr(bars, period=14):
    if len(bars) < period + 1: return None
    trs = [max(b.high - b.low, abs(b.high - bars[i - 1].close), abs(b.low - bars[i - 1].close))
           for i, b in enumerate(bars[1:], 1)]
    return sum(trs[-period:]) / period

def calc_ma(values, period):
    if len(values) < period: return None
    return sum(values[-period:]) / period

def calc_std(values, period):
    if len(values) < period: return None
    m = calc_ma(values, period)
    return math.sqrt(sum((v - m) ** 2 for v in values[-period:]) / period)

def trend_from_ticks(tracker: TickTracker) -> str:
    """Multi-timeframe tick trend: 30s + 60s + 120s 加权投票.

    比 5M MA 交叉快 3-5 分钟, 且在 midday 低波动中更稳定.
    """
    mom_30 = tracker.momentum(30)
    mom_60 = tracker.momentum(60)
    mom_120 = tracker.momentum(120)

    # Weighted score: shorter timeframes get higher weight
    score = 0.0
    if abs(mom_30) > 0.0002:
        score += math.copysign(3, mom_30)  # 30s weighted ×3
    if abs(mom_60) > 0.0003:
        score += math.copysign(2, mom_60)  # 60s weighted ×2
    if abs(mom_120) > 0.0005:
        score += math.copysign(1, mom_120)  # 120s weighted ×1

    if score > 2: return 'UP'
    if score < -2: return 'DOWN'
    return 'FLAT'


def calc_trend_5m(bars_5m):
    """Return 'UP', 'DOWN', or 'FLAT' based on 5M MA slope"""
    if len(bars_5m) < 10: return 'FLAT'
    closes = [b.close for b in bars_5m]
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    if ma5 > ma10 * 1.003: return 'UP'
    if ma5 < ma10 * 0.997: return 'DOWN'
    return 'FLAT'


def detect_momentum_signal(bars_1m, bars_5m, quote, atr) -> Optional[Dict]:
    """Strategy A: 趋势回调入场 — 等 pullback, 不追 momentum.

    核心理念:
      - 上升趋势: 等 1-2 根微回调K线 (缩量) -> LONG, 而不是追4连阳
      - 下降趋势: 等 1-2 根微反弹K线 (缩量) -> SHORT, 而不是追4连阴
      - 追连续同向K线 = 入场在微结构末端 = 必亏
    """
    closes = [b.close for b in bars_1m]
    volumes = [b.volume for b in bars_1m]
    if len(closes) < 20: return None

    rsi = calc_rsi(closes, 14)
    if rsi is None: return None
    trend = calc_trend_5m(bars_5m)
    if trend == 'FLAT': return None

    # Volume profile
    avg_vol = sum(volumes[-11:-1]) / 10 if len(volumes) >= 11 else sum(volumes[:-1]) / max(len(volumes) - 1, 1)
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0

    # VWAP for entry quality
    vwap_n = sum(b.close * max(b.volume, 1) for b in bars_1m[-20:])
    vwap_d = sum(max(b.volume, 1) for b in bars_1m[-20:])
    vwap = vwap_n / vwap_d if vwap_d > 0 else quote.last

    # Detect last 3 bars direction
    last_3 = closes[-3:]
    bar3_dir = sum(1 for i in range(-3, 0) if closes[i] > closes[i-1])  # 0-3 up bars
    last_bar_up = closes[-1] > closes[-2]
    last_bar_pct = abs(closes[-1] - closes[-2]) / closes[-2] if closes[-2] > 0 else 0

    score_long = 0; score_short = 0; reasons = []

    # -- LONG: UP trend + pullback (last bars are down/flat, not chasing) --
    if trend == 'UP':
        if rsi > 72: return None  # 超买不做

        # Pullback detection: last 1-2 bars down or flat with declining volume
        is_pullback = (bar3_dir <= 1 and not last_bar_up) or (bar3_dir <= 2 and vol_ratio < 0.9)

        if is_pullback:
            score_long += 20; reasons.append("PULLBACK")
            # Deeper pullback = better entry
            if quote.last < vwap:
                score_long += 12; reasons.append("BELOW_VWAP")
            # Volume drying up = pullback ending
            if vol_ratio < 0.75:
                score_long += 10; reasons.append("VOL_DRY")
            # RSI reset from pullback
            if rsi < 55:
                score_long += 10; reasons.append(f"RSI_COOL{rsi:.0f}")
            # Trend strength
            score_long += 15; reasons.append("TREND_UP")

    # -- SHORT: DOWN trend + bounce (last bars are up/flat, not chasing) --
    elif trend == 'DOWN':
        if rsi < 28: return None  # 超卖不做

        # Bounce detection: last 1-2 bars up or flat with declining volume
        is_bounce = (bar3_dir >= 2 and last_bar_up) or (bar3_dir >= 1 and vol_ratio < 0.9)

        if is_bounce:
            score_short += 20; reasons.append("BOUNCE")
            # Higher bounce = better short entry
            if quote.last > vwap:
                score_short += 12; reasons.append("ABOVE_VWAP")
            if vol_ratio < 0.75:
                score_short += 10; reasons.append("VOL_DRY")
            if rsi > 45:
                score_short += 10; reasons.append(f"RSI_WARM{rsi:.0f}")
            score_short += 15; reasons.append("TREND_DN")

    if score_long >= SIGNAL_SCORE_THRESHOLD and score_long > score_short:
        direction = Direction.LONG; score = score_long
    elif score_short >= SIGNAL_SCORE_THRESHOLD and score_short > score_long:
        direction = Direction.SHORT; score = score_short
    else:
        return None

    entry = quote.ask if direction == Direction.LONG else quote.bid
    if entry <= 0: entry = quote.last
    sl_d = atr * 1.5; tp_d = atr * 2.0

    return {
        "strategy": STRATEGY_A, "direction": direction, "score": min(score, 100),
        "entry": entry,
        "sl": entry - sl_d if direction == Direction.LONG else entry + sl_d,
        "tp": entry + tp_d if direction == Direction.LONG else entry - tp_d,
        "reasons": "|".join(reasons), "rsi": round(rsi, 1),
        "vol_ratio": round(vol_ratio, 2),
        "atr": round(atr, 2), "trend": trend, "max_hold_bars": 12,
    }


def detect_reversion_signal(bars_1m, bars_5m, quote, atr) -> Optional[Dict]:
    """Strategy B: 均值回归 — 逆势抄底摸顶 (仅在趋势衰竭时)"""
    closes = [b.close for b in bars_1m]
    volumes = [b.volume for b in bars_1m]
    if len(closes) < 20: return None

    rsi = calc_rsi(closes, 14)
    if rsi is None: return None
    trend = calc_trend_5m(bars_5m)

    # BB
    ma20 = calc_ma(closes, 20); std20 = calc_std(closes, 20)
    if ma20 is None or std20 is None: return None
    bb_lower = ma20 - 2 * std20; bb_upper = ma20 + 2 * std20
    bb_pos = (quote.last - bb_lower) / (bb_upper - bb_lower) if bb_upper != bb_lower else 0.5

    # VWAP
    vwap_n = sum(b.close * b.volume for b in bars_1m[-20:] if b.volume > 0)
    vwap_d = sum(b.volume for b in bars_1m[-20:] if b.volume > 0)
    vwap = vwap_n / vwap_d if vwap_d > 0 else quote.last
    vwap_dev = (quote.last - vwap) / vwap

    avg_vol = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else sum(volumes[:-1]) / max(len(volumes) - 1, 1)
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0

    score_long = 0; score_short = 0; reasons = []

    # 核心: 顺势不做逆势回归!
    # LONG (抄底) 仅在 DOWN trend 中, SHORT (摸顶) 仅在 UP trend 中
    if trend == 'DOWN' and rsi <= REVERSION_RSI_OS:
        score_long += 25; reasons.append(f"RSI({rsi:.0f})")
    elif trend == 'UP' and rsi >= REVERSION_RSI_OB:
        score_short += 25; reasons.append(f"RSI({rsi:.0f})")

    # BB 极值确认
    if bb_pos < 0.15 and trend == 'DOWN':
        score_long += 20; reasons.append("BB_LOW")
    elif bb_pos > 0.85 and trend == 'UP':
        score_short += 20; reasons.append("BB_HI")

    # VWAP 偏离
    if vwap_dev < -0.008 and trend == 'DOWN':
        score_long += 10; reasons.append(f"VWAP{vwap_dev:+.2%}")
    elif vwap_dev > 0.008 and trend == 'UP':
        score_short += 10; reasons.append(f"VWAP{vwap_dev:+.2%}")

    # 成交量衰竭 (顶部放量是见顶标志)
    if vol_ratio > 1.8:
        bonus = min(10, int(vol_ratio * 3))
        if score_short > score_long: score_short += bonus
        elif score_long > score_short: score_long += bonus
        reasons.append(f"EXHx{vol_ratio:.1f}")

    if score_long >= SIGNAL_SCORE_THRESHOLD and score_long > score_short:
        direction = Direction.LONG; score = score_long
    elif score_short >= SIGNAL_SCORE_THRESHOLD and score_short > score_long:
        direction = Direction.SHORT; score = score_short
    else:
        return None

    entry = quote.ask if direction == Direction.LONG else quote.bid
    sl_d = atr * 1.2; tp_d = atr * 2.0

    return {
        "strategy": STRATEGY_B, "direction": direction, "score": min(score, 100),
        "entry": entry if entry > 0 else quote.last,
        "sl": entry - sl_d if direction == Direction.LONG else entry + sl_d,
        "tp": entry + tp_d if direction == Direction.LONG else entry - tp_d,
        "reasons": "|".join(reasons), "rsi": round(rsi, 1),
        "bb_pos": round(bb_pos, 3), "vwap_dev": round(vwap_dev, 4),
        "vol_ratio": round(vol_ratio, 2), "atr": round(atr, 2), "trend": trend, "max_hold_bars": 18,
    }


def detect_micro_fade(tracker: TickTracker, bars_1m, bars_5m, quote, atr) -> Optional[Dict]:
    """Strategy Au: Quote-level 微动量 breakout.

    用 tick 采样检测微突破, 比 K_1M 快 30-90 秒.
    顺 tick 趋势 + 顺 5M 趋势 (双确认) → 入场.
    非对称离场 (tight SL + trailing TP) 负责风险管理.
    """
    if tracker.count < 10: return None

    trend_5m = calc_trend_5m(bars_5m)
    trend_tick = trend_from_ticks(tracker)

    mom_15 = tracker.momentum(15)
    accel = tracker.acceleration()
    spread_tight = tracker.spread_tightening()
    bias = tracker.bid_ask_bias()

    closes = [b.close for b in bars_1m]
    volumes = [b.volume for b in bars_1m]
    avg_vol = sum(volumes[-11:-1]) / 10 if len(volumes) >= 11 else sum(volumes[:-1]) / max(len(volumes) - 1, 1)
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0

    score_long = 0; score_short = 0; reasons = []

    # -- LONG: 微动量向上 + 趋势支持 --
    if mom_15 > 0.0005:
        pts = min(30, int(mom_15 / 0.0005 * 10))
        score_long += pts; reasons.append(f"uMOM+{mom_15:.4f}")
        if accel > 0: score_long += 10; reasons.append("ACCEL")
        if bias > 0.1: score_long += 8; reasons.append("BUY")
        if spread_tight: score_long += 7; reasons.append("SPRD")
        if vol_ratio > 1.2: score_long += 8; reasons.append(f"VOL{vol_ratio:.1f}")
        # Trend alignment
        if trend_tick == 'UP': score_long += 12; reasons.append("T_UP")
        if trend_5m == 'UP': score_long += 10; reasons.append("5UP")
        elif trend_5m == 'DOWN': score_long -= 10  # counter 5M trend penalty

    # -- SHORT: 微动量向下 + 趋势支持 --
    if mom_15 < -0.0005:
        pts = min(30, int(abs(mom_15) / 0.0005 * 10))
        score_short += pts; reasons.append(f"uMOM-{mom_15:.4f}")
        if accel < 0: score_short += 10; reasons.append("ACCEL")
        if bias < -0.1: score_short += 8; reasons.append("SELL")
        if spread_tight: score_short += 7; reasons.append("SPRD")
        if vol_ratio > 1.2: score_short += 8; reasons.append(f"VOL{vol_ratio:.1f}")
        if trend_tick == 'DOWN': score_short += 12; reasons.append("T_DN")
        if trend_5m == 'DOWN': score_short += 10; reasons.append("5DN")
        elif trend_5m == 'UP': score_short -= 10

    THRESHOLD = 38
    if score_long >= THRESHOLD and score_long > score_short:
        direction = Direction.LONG; score = score_long
    elif score_short >= THRESHOLD and score_short > score_long:
        direction = Direction.SHORT; score = score_short
    else:
        return None

    entry = quote.ask if direction == Direction.LONG else quote.bid
    if entry <= 0: entry = quote.last

    return {
        "strategy": STRATEGY_A_MICRO, "direction": direction, "score": min(score, 100),
        "entry": entry,
        "sl": entry - atr if direction == Direction.LONG else entry + atr,
        "tp": entry + atr * 4 if direction == Direction.LONG else entry - atr * 4,
        "reasons": "|".join(reasons),
        "mom_15s": round(mom_15, 5), "accel": round(accel, 5),
        "bias": round(bias, 3), "vol_ratio": round(vol_ratio, 2),
        "atr": round(atr, 2), "trend": f"tick={trend_tick}_5m={trend_5m}",
        "max_hold_bars": 60,
    }


# ═══════════════════════════════════════════════════════════
#  Shadow Account — virtual capital tracking
# ═══════════════════════════════════════════════════════════

class ShadowAccount:
    """影子账户: 每笔交易独立核算, 资金出入精确匹配."""

    def __init__(self, capital: float = SHADOW_CAPITAL):
        self.initial = capital
        self.cash = capital
        self.equity = capital
        self.deployed: Dict[str, float] = {}  # trade_id → actual capital deployed
        self.daily_pnl: Dict[str, float] = defaultdict(float)
        self.daily_trades: Dict[str, int] = defaultdict(int)
        self.daily_loss: Dict[str, float] = defaultdict(float)
        self.all_pnl: List[float] = []
        self.equity_curve: List[Tuple[datetime, float]] = []

    def allocate(self, symbol: str, trade_value: float) -> bool:
        if trade_value > self.cash * 0.95:
            return False
        total_used = self.initial - self.cash
        if total_used + trade_value > self.initial * MAX_TOTAL_PCT:
            return False
        return True

    def execute(self, trade_id: str, trade_value: float):
        self.cash -= trade_value
        self.deployed[trade_id] = trade_value

    def settle(self, trade_id: str, pnl: float):
        capital = self.deployed.pop(trade_id, SHADOW_CAPITAL * MAX_POSITION_PCT)
        self.cash += capital + pnl
        self.equity = self.cash
        self.all_pnl.append(pnl)
        self.equity_curve.append((datetime.now(), self.equity))


# ═══════════════════════════════════════════════════════════
#  Database
# ═══════════════════════════════════════════════════════════

def _strat_label(strategy: str) -> str:
    """Map strategy constant → short label for DB storage"""
    return {"momentum_breakout": "A", "mean_reversion": "B", "momentum_micro": "A_micro"}.get(strategy, strategy)


class ShadowDB:
    def __init__(self, db_path=None):
        if db_path is None:
            db_path = os.path.join(PROJECT_DIR, "data", "ftseek_us.db")
        self.db_path = db_path
        self._init()

    def _conn(self):
        import sqlite3
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def _init(self):
        with self._conn() as c:
            # Schema version tracking
            c.execute("CREATE TABLE IF NOT EXISTS shadow_schema_version (version INTEGER PRIMARY KEY)")
            current = c.execute("SELECT MAX(version) FROM shadow_schema_version").fetchone()[0] or 0

            if current < 1:
                c.executescript("""
                    CREATE TABLE IF NOT EXISTS shadow_trades (
                        trade_id TEXT PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        strategy TEXT NOT NULL DEFAULT 'A',
                        direction TEXT NOT NULL,
                        signal_score INTEGER DEFAULT 0,
                        signal_detail TEXT DEFAULT '',
                        entry_time TEXT, exit_time TEXT,
                        entry_price REAL, exit_price REAL,
                        quantity INTEGER DEFAULT 0,
                        entry_slippage_pct REAL DEFAULT 0,
                        spread_pct REAL DEFAULT 0,
                        commission REAL DEFAULT 0,
                        realized_pnl REAL DEFAULT 0,
                        pnl_pct REAL DEFAULT 0,
                        pnl_r REAL DEFAULT 0,
                        exit_reason TEXT,
                        max_favorable_pct REAL DEFAULT 0,
                        max_adverse_pct REAL DEFAULT 0,
                        holding_minutes REAL DEFAULT 0,
                        market_phase TEXT DEFAULT '',
                        closed INTEGER DEFAULT 0,
                        created_at TEXT DEFAULT (datetime('now'))
                    );
                    CREATE INDEX IF NOT EXISTS idx_st_sym ON shadow_trades(symbol);
                    CREATE INDEX IF NOT EXISTS idx_st_strat ON shadow_trades(strategy);
                    CREATE INDEX IF NOT EXISTS idx_st_date ON shadow_trades(created_at);

                    CREATE TABLE IF NOT EXISTS shadow_daily_stats (
                        date TEXT NOT NULL,
                        symbol TEXT NOT NULL DEFAULT 'ALL',
                        strategy TEXT NOT NULL DEFAULT 'ALL',
                        total_trades INTEGER DEFAULT 0,
                        wins INTEGER DEFAULT 0,
                        losses INTEGER DEFAULT 0,
                        win_rate REAL DEFAULT 0,
                        total_pnl REAL DEFAULT 0,
                        avg_pnl REAL DEFAULT 0,
                        avg_win REAL DEFAULT 0,
                        avg_loss REAL DEFAULT 0,
                        profit_factor REAL DEFAULT 0,
                        total_r REAL DEFAULT 0,
                        avg_r REAL DEFAULT 0,
                        best_r REAL DEFAULT 0,
                        worst_r REAL DEFAULT 0,
                        avg_holding_min REAL DEFAULT 0,
                        avg_mae_pct REAL DEFAULT 0,
                        avg_mfe_pct REAL DEFAULT 0,
                        sharpe REAL DEFAULT 0,
                        detail_json TEXT DEFAULT '{}',
                        updated_at TEXT DEFAULT (datetime('now')),
                        PRIMARY KEY (date, symbol, strategy)
                    );
                """)
                c.execute("INSERT INTO shadow_schema_version VALUES (1)")
                logger.info("DB schema v1 已创建")

    def save_trade(self, t: ShadowTrade):
        with self._conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO shadow_trades (
                    trade_id, symbol, strategy, direction, signal_score, signal_detail,
                    entry_time, exit_time, entry_price, exit_price, quantity,
                    entry_slippage_pct, spread_pct, commission,
                    realized_pnl, pnl_pct, pnl_r, exit_reason,
                    max_favorable_pct, max_adverse_pct, holding_minutes, market_phase, closed
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                t.trade_id, t.symbol, t.strategy, t.direction.value,
                t.signal_score, json.dumps(t.signal_detail, default=str),
                t.entry_time.isoformat() if t.entry_time else None,
                t.exit_time.isoformat() if t.exit_time else None,
                t.entry_price, t.exit_price, t.quantity,
                t.entry_slippage_pct, t.spread_pct, t.commission,
                t.realized_pnl, t.pnl_pct, t.pnl_r,
                t.exit_reason.value if t.exit_reason else None,
                t.max_favorable_pct, t.max_adverse_pct,
                t.holding_minutes, t.market_phase,
                1 if t.closed else 0
            ))

    def daily_stats(self):
        today = datetime.now().strftime("%Y-%m-%d")
        with self._conn() as c:
            # Per symbol+strategy
            for symbol in WHETSTONE_SYMBOLS:
                for strat in [STRATEGY_A, STRATEGY_A_MICRO, STRATEGY_B]:
                    self._compute_stats(c, today, symbol, _strat_label(strat))
            # Per strategy overall
            for strat in [STRATEGY_A, STRATEGY_A_MICRO, STRATEGY_B]:
                self._compute_stats(c, today, "ALL", _strat_label(strat))
            # Global
            self._compute_stats(c, today, "ALL", "ALL")

    def _compute_stats(self, c, today, symbol, strategy):
        where = "WHERE date(entry_time)=? AND closed=1"
        params = [today]
        if symbol != "ALL":
            where += " AND symbol=?"
            params.append(symbol)
        if strategy != "ALL":
            where += " AND strategy=?"
            params.append(strategy)

        row = c.execute(f"""
            SELECT COUNT(*) n, SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) w,
                   SUM(realized_pnl) tp, AVG(realized_pnl) ap,
                   AVG(CASE WHEN realized_pnl>0 THEN realized_pnl END) aw,
                   AVG(CASE WHEN realized_pnl<=0 THEN realized_pnl END) al,
                   SUM(CASE WHEN realized_pnl>0 THEN realized_pnl END) gp,
                   ABS(SUM(CASE WHEN realized_pnl<=0 THEN realized_pnl END)) gl,
                   SUM(pnl_r) tr, AVG(pnl_r) ar,
                   MAX(pnl_r) br, MIN(pnl_r) wr,
                   AVG(holding_minutes) ah, AVG(max_adverse_pct) amae, AVG(max_favorable_pct) amfe
            FROM shadow_trades {where}
        """, params).fetchone()

        if not row or row["n"] == 0: return

        n, w, l = row["n"], row["w"] or 0, row["n"] - (row["w"] or 0)
        wr = w / n if n else 0
        pf = (row["gp"] or 0) / (row["gl"] or 1.0) if row["gl"] else 99

        trades = c.execute(f"SELECT pnl_pct FROM shadow_trades {where} ORDER BY entry_time", params).fetchall()
        pcts = [t["pnl_pct"] for t in trades]
        sharpe = (sum(pcts) / n) / (math.sqrt(sum((x - sum(pcts)/n)**2 for x in pcts) / n) + 0.0001) * math.sqrt(n) if n >= 3 else 0

        detail = {}
        for dim, col in [("exit", "exit_reason"), ("phase", "market_phase")]:
            rows = c.execute(f"SELECT {col} v, COUNT(*) n, SUM(realized_pnl) s, AVG(pnl_pct) a FROM shadow_trades {where} GROUP BY {col}", params).fetchall()
            detail[dim] = {r["v"]: {"n": r["n"], "pnl": r["s"], "avg_pct": r["a"]} for r in rows}

        c.execute("""
            INSERT OR REPLACE INTO shadow_daily_stats VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
        """, (today, symbol, strategy, n, w, l, wr, row["tp"], row["ap"], row["aw"], row["al"],
              pf, row["tr"], row["ar"], row["br"], row["wr"], row["ah"], row["amae"], row["amfe"],
              sharpe, json.dumps(detail, default=str), datetime.now().isoformat()))


# ═══════════════════════════════════════════════════════════
#  Market Phase
# ═══════════════════════════════════════════════════════════

def get_market_phase(now=None):
    """Beijing (UTC+8) → ET (UTC-4): subtract 12 hours"""
    if now is None: now = datetime.now()
    et = now - timedelta(hours=12)
    m = et.hour * 60 + et.minute
    if m < 570: return "pre_market"
    if m < 600: return "open_drive"
    if m < 690: return "morning"
    if m < 840: return "midday"
    if m < 930: return "afternoon"
    if m < 960: return "close_drive"
    return "after_hours"

def is_market_open():
    return get_market_phase() != "after_hours"


# ═══════════════════════════════════════════════════════════
#  Whetstone Runner
# ═══════════════════════════════════════════════════════════

class WhetstoneRunner:
    def __init__(self, symbols=None, dry_run=False, max_cycles=0):
        self.symbols = symbols or WHETSTONE_SYMBOLS
        self.dry_run = dry_run
        self.max_cycles = max_cycles
        self.running = True

        self.db = ShadowDB()
        self.account = ShadowAccount()
        self.quote_ctx = None

        # Per-symbol state
        self.bars_1m: Dict[str, List[Bar]] = defaultdict(list)
        self.bars_5m: Dict[str, List[Bar]] = defaultdict(list)
        self.quotes: Dict[str, Quote] = {}
        self.tick_trackers: Dict[str, TickTracker] = defaultdict(TickTracker)
        self.positions: Dict[str, ShadowTrade] = {}  # trade_id → trade
        self.position_bars: Dict[str, int] = {}       # trade_id → bars held
        self.trail_high: Dict[str, float] = {}         # trade_id → highest price seen (LONG)
        self.trail_low: Dict[str, float] = {}           # trade_id → lowest price seen (SHORT)
        self.last_exit_time: Dict[str, datetime] = {}    # symbol → last exit timestamp
        self.cooldown_seconds = 30  # 离场后 30s 禁止同标的入场

        # Stats
        self.cycle = 0
        self.trades_today: Dict[str, int] = defaultdict(int)  # per symbol
        self.start_time = datetime.now()
        self.last_stats_update = datetime.now()

    # ── Futu ──────────────────────────────────────────────

    def _connect(self):
        if self.dry_run: return True
        from futu import OpenQuoteContext
        host = os.getenv("OPEND_HOST", "127.0.0.1")
        port = int(os.getenv("OPEND_PORT", "11111"))
        self.quote_ctx = OpenQuoteContext(host=host, port=port)
        logger.info(f"Futu 已连接 {host}:{port}")
        return True

    def _subscribe(self):
        if self.dry_run: return
        from futu import SubType, RET_OK
        ret, data = self.quote_ctx.subscribe(self.symbols, [SubType.QUOTE, SubType.K_1M, SubType.K_5M])
        if ret != RET_OK:
            logger.error(f"订阅失败: {data}")
        else:
            logger.info(f"已订阅 {len(self.symbols)} 标的: QUOTE + K_1M + K_5M")

    # ── Data Fetch ────────────────────────────────────────

    def _fetch(self):
        if self.dry_run: return self._fetch_dry()
        from futu import RET_OK, KLType

        # Fetch quotes for all symbols at once
        ret, data = self.quote_ctx.get_market_snapshot(self.symbols)
        if ret != RET_OK:
            return
        for _, row in data.iterrows():
            sym = row["code"]
            self.quotes[sym] = Quote(
                symbol=sym, timestamp=datetime.now(),
                last=float(row["last_price"]),
                bid=float(row.get("bid_price", 0) or 0),
                ask=float(row.get("ask_price", 0) or 0),
                high=float(row["high_price"]), low=float(row["low_price"]),
                volume=float(row["volume"]),
            )
            self.tick_trackers[sym].feed(self.quotes[sym])

        # Fetch K lines per symbol
        for sym in self.symbols:
            self._fetch_kline(sym, KLType.K_1M, self.bars_1m, 60)
            self._fetch_kline(sym, KLType.K_5M, self.bars_5m, 40)

    def _fetch_kline(self, sym, ktype, storage, count):
        from futu import RET_OK
        ret, data = self.quote_ctx.get_cur_kline(sym, count, ktype)
        if ret != RET_OK: return
        bars = []
        for _, row in data.iterrows():
            ts_str = str(row["time_key"])
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S") if len(ts_str) > 10 else datetime.strptime(ts_str, "%Y-%m-%d")
            bars.append(Bar(ts, float(row["open"]), float(row["high"]),
                          float(row["low"]), float(row["close"]), float(row["volume"])))
        existing = storage.get(sym, [])
        if existing:
            last_ts = existing[-1].timestamp
            bars = [b for b in bars if b.timestamp > last_ts]
        storage[sym].extend(bars)
        if len(storage[sym]) > 100:
            storage[sym] = storage[sym][-100:]

    def _fetch_dry(self):
        import random
        random.seed(42)
        t = datetime.now()
        for sym in self.symbols:
            base = {"US.SNDK": 1450, "US.NIO": 6.5, "US.AMD": 440, "US.MU": 790, "US.LCID": 5.9}.get(sym, 100)
            vol = random.uniform(0.001, 0.03)
            price = base * (1 + random.gauss(0, vol))
            spread = price * 0.0005
            self.quotes[sym] = Quote(sym, t, price, price - spread/2, price + spread/2, price*1.02, price*0.98, random.randint(50000, 500000))
            bars = self.bars_1m.get(sym, [])
            if not bars:
                for i in range(80, 0, -1):
                    p = base * (1 + random.gauss(0, vol/3))
                    bars.append(Bar(t - timedelta(minutes=i), p-1, p+2, p-2, p, random.randint(5000, 80000)))
            new_p = price + random.gauss(0, base * vol * 0.3)
            bars.append(Bar(t, bars[-1].close, max(new_p, bars[-1].close), min(new_p, bars[-1].close), new_p, random.randint(5000, 80000)))
            self.bars_1m[sym] = bars[-100:]

    # ── Execution Simulation ──────────────────────────────

    def _simulate_entry(self, trade: ShadowTrade, quote: Quote):
        direction = trade.direction
        if direction == Direction.LONG:
            fill = quote.ask * 1.002 if quote.ask > 0 else quote.last * 1.002
            spread = (quote.ask - quote.bid) / quote.bid if quote.bid > 0 else 0.0005
        else:
            fill = quote.bid * 0.998 if quote.bid > 0 else quote.last * 0.998
            spread = (quote.ask - quote.bid) / quote.ask if quote.ask > 0 else 0.0005

        trade_value = SHADOW_CAPITAL * MAX_POSITION_PCT
        qty = max(1, int(trade_value / fill))
        commission = qty * fill * 0.00003  # 0.003% commission

        trade.entry_price = fill
        trade.quantity = qty
        trade.entry_slippage_pct = 0.002
        trade.spread_pct = spread
        trade.commission = commission

    def _check_exit(self, trade: ShadowTrade, quote: Quote, atr: float, bars_held: int):
        """Asymmetric exit: tight SL, trailing TP, no TIME stop.

        SL 从对手价计算 (SHORT用ask, LONG用bid), 留出 spread 缓冲.
        """
        cur = quote.last
        entry = trade.entry_price
        if cur <= 0 or entry <= 0: return False, None, 0

        tid = trade.trade_id
        MAX_BARS = 120  # 6 min safety net (足够 trailing TP 激活)

        if trade.direction == Direction.LONG:
            pnl_pct = (cur - entry) / entry
            prev_high = self.trail_high.get(tid, entry)
            if cur > prev_high:
                self.trail_high[tid] = cur
            # SL 从 bid (离场价) 计算
            worst_exit = quote.bid if quote.bid > 0 else cur
            sl_buffer = atr * 1.5
            sl_price = worst_exit - sl_buffer
            sl_hit = cur <= sl_price
            # Trailing: profit > atr*1.0 → trail by atr*0.5
            trail_activated = self.trail_high.get(tid, entry) > entry + atr * 1.0
            trail_hit = trail_activated and cur <= self.trail_high[tid] - atr * 0.5
            tp_hit = cur >= entry + atr * 4.0
        else:
            pnl_pct = (entry - cur) / entry
            prev_low = self.trail_low.get(tid, entry)
            if cur < prev_low:
                self.trail_low[tid] = cur
            # SL 从 ask (离场价) 计算
            worst_exit = quote.ask if quote.ask > 0 else cur
            sl_buffer = atr * 1.5
            sl_price = worst_exit + sl_buffer
            sl_hit = cur >= sl_price
            trail_activated = self.trail_low.get(tid, entry) < entry - atr * 1.0
            trail_hit = trail_activated and cur >= self.trail_low[tid] + atr * 0.5
            tp_hit = cur <= entry - atr * 4.0

        if pnl_pct > trade.max_favorable_pct: trade.max_favorable_pct = pnl_pct
        if pnl_pct < trade.max_adverse_pct: trade.max_adverse_pct = pnl_pct

        exit_p = quote.bid if trade.direction == Direction.LONG else quote.ask
        if exit_p <= 0: exit_p = cur

        if sl_hit:
            return True, ExitReason.STOP_LOSS, exit_p
        if trail_hit:
            return True, ExitReason.TRAILING_STOP, exit_p
        if tp_hit:
            return True, ExitReason.TAKE_PROFIT, exit_p
        if bars_held >= MAX_BARS:
            return True, ExitReason.TIME_STOP, exit_p

        return False, None, 0

    # ── Trade Lifecycle ───────────────────────────────────

    def _enter(self, symbol: str, signal: Dict, quote: Quote, phase: str):
        trade = ShadowTrade(
            symbol=symbol,
            strategy={"momentum_breakout": "A", "mean_reversion": "B", "momentum_micro": "A_micro"}.get(signal["strategy"], "A"),
            direction=signal["direction"],
            signal_score=signal["score"],
            signal_detail={k: v for k, v in signal.items()},
            entry_time=datetime.now(),
            market_phase=phase,
        )
        self._simulate_entry(trade, quote)

        trade_value = trade.entry_price * trade.quantity
        if not self.account.allocate(symbol, trade_value):
            return

        self.account.execute(trade.trade_id, trade_value)
        self.positions[trade.trade_id] = trade
        self.position_bars[trade.trade_id] = 0
        self.trades_today[symbol] += 1
        self.db.save_trade(trade)

        tag_map = {"A": "A", "B": "B", "A_micro": "Aμ"}
        tag = tag_map.get(trade.strategy, "?")
        logger.info(f"▶ [{tag}] {symbol} {trade.direction.value} "
                   f"@{trade.entry_price:.2f} x{trade.quantity} "
                   f"Score={trade.signal_score} [{signal['reasons']}] "
                   f"SL={signal['sl']:.2f} TP={signal['tp']:.2f}")

    def _exit(self, trade_id: str, quote: Quote, reason: ExitReason):
        trade = self.positions.pop(trade_id, None)
        if not trade: return
        self.position_bars.pop(trade_id, None)
        self.trail_high.pop(trade_id, None)
        self.trail_low.pop(trade_id, None)
        self.last_exit_time[trade.symbol] = datetime.now()

        if trade.direction == Direction.LONG:
            exit_p = quote.bid * 0.998 if quote.bid > 0 else quote.last
            trade.pnl_pct = (exit_p - trade.entry_price) / trade.entry_price
        else:
            exit_p = quote.ask * 1.002 if quote.ask > 0 else quote.last
            trade.pnl_pct = (trade.entry_price - exit_p) / trade.entry_price

        trade.exit_price = exit_p
        trade.exit_time = datetime.now()
        trade.exit_reason = reason
        trade.closed = True
        trade.holding_minutes = (trade.exit_time - trade.entry_time).total_seconds() / 60
        trade.realized_pnl = trade.pnl_pct * trade.entry_price * trade.quantity - trade.commission
        trade.pnl_r = trade.pnl_pct / RISK_PER_TRADE_PCT

        self.account.settle(trade_id, trade.realized_pnl)
        self.account.daily_pnl[trade.symbol] += trade.realized_pnl
        self.account.daily_trades[trade.symbol] += 1
        if trade.realized_pnl < 0:
            self.account.daily_loss[trade.symbol] += abs(trade.realized_pnl)
        self.db.save_trade(trade)

        tag_map = {"A": "A", "B": "B", "A_micro": "Aμ"}
        tag = tag_map.get(trade.strategy, "?")
        logger.info(f"◀ [{tag}] {trade.symbol} {reason.value} "
                   f"@{exit_p:.2f} P&L=${trade.realized_pnl:+.2f} "
                   f"({trade.pnl_pct:+.2%}) R={trade.pnl_r:+.1f}R "
                   f"Hold={trade.holding_minutes:.1f}m")

    # ── Main Loop ─────────────────────────────────────────

    def run(self):
        logger.info("═" * 65)
        logger.info(f"  Whetstone US v2 — Karpathy Multi-Symbol Scalping")
        logger.info(f"  标的: {', '.join(self.symbols)}")
        logger.info(f"  策略: A={STRATEGY_A}  Aμ=micro_breakout  B={STRATEGY_B}")
        logger.info(f"  资金: ${SHADOW_CAPITAL:,.0f} | 仓位: {MAX_POSITION_PCT:.0%}/笔")
        logger.info(f"  阈值: Score≥{SIGNAL_SCORE_THRESHOLD} | RSI {RSI_OVERSOLD}/{RSI_OVERBOUGHT}")
        logger.info(f"  模式: {'DRY-RUN' if self.dry_run else 'LIVE (Futu OpenD)'}")
        logger.info("═" * 65)

        if not self.dry_run:
            self._connect()
            self._subscribe()
            time.sleep(3)

        try:
            while self.running:
                self.cycle += 1
                self._fetch()
                phase = get_market_phase()

                if phase == "after_hours" and not self.dry_run:
                    if self.cycle % 12 == 0:
                        logger.info(f"[{phase}] 盘后静默, 持仓={len(self.positions)}")
                    time.sleep(10)
                    continue

                # Process each symbol
                for sym in self.symbols:
                    quote = self.quotes.get(sym)
                    bars = self.bars_1m.get(sym, [])
                    if not quote or len(bars) < 20:
                        continue

                    atr = calc_atr(bars, 14)
                    if atr is None: atr = quote.last * 0.005

                    # ATR% filter: skip symbols where spread+slippage > potential edge
                    atr_pct = atr / quote.last if quote.last > 0 else 0
                    MIN_ATR_PCT = 0.002  # 0.2% minimum ATR for scalping viability
                    if atr_pct < MIN_ATR_PCT:
                        continue  # 波动太低, spread 成本无法覆盖

                    # Check existing positions for this symbol
                    for tid, trade in list(self.positions.items()):
                        if trade.symbol != sym: continue
                        bars_held = self.position_bars.get(tid, 0) + 1
                        self.position_bars[tid] = bars_held
                        exit_trigger, reason, exit_p = self._check_exit(trade, quote, atr, bars_held)
                        if exit_trigger:
                            self._exit(tid, quote, reason)

                    # Skip if at capacity
                    if len(self.positions) >= MAX_CONCURRENT: continue
                    if self.account.daily_loss.get(sym, 0) >= MAX_DAILY_LOSS_PER_SYMBOL: continue

                    # Per-symbol cooldown after exit (防止连环重复入场)
                    last_exit = self.last_exit_time.get(sym)
                    if last_exit and (datetime.now() - last_exit).total_seconds() < self.cooldown_seconds:
                        continue

                    # Generate signals: micro-breakout first (faster), then K_1M
                    ticker = self.tick_trackers.get(sym)
                    sig_micro = detect_micro_fade(ticker, bars, self.bars_5m.get(sym, []), quote, atr) if ticker else None
                    sig_a = detect_momentum_signal(bars, self.bars_5m.get(sym, []), quote, atr)
                    sig_b = detect_reversion_signal(bars, self.bars_5m.get(sym, []), quote, atr)

                    # Pick best signal: micro gets priority bonus for earlier entry
                    candidates = []
                    if sig_micro: candidates.append((sig_micro["score"] + 5, sig_micro))  # +5 优先微动量
                    if sig_a: candidates.append((sig_a["score"], sig_a))
                    if sig_b: candidates.append((sig_b["score"], sig_b))
                    best = max(candidates, key=lambda x: x[0])[1] if candidates else None

                    if best:
                        # Cooldown: skip if same symbol+direction already held
                        dup = False
                        strat_label = ("A_micro" if best["strategy"] == STRATEGY_A_MICRO
                                       else "A" if best["strategy"] == STRATEGY_A
                                       else "B")
                        for t in self.positions.values():
                            if t.symbol == sym and t.strategy == strat_label and t.direction == best["direction"]:
                                dup = True; break
                        if not dup:
                            self._enter(sym, best, quote, phase)

                # Periodic status
                if self.cycle % 12 == 0:
                    self._status(phase)

                # Stats update
                if (datetime.now() - self.last_stats_update).seconds >= 300:
                    self.db.daily_stats()
                    self.last_stats_update = datetime.now()

                if self.max_cycles and self.cycle >= self.max_cycles:
                    break

                time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()

    def _status(self, phase):
        pos_count = len(self.positions)
        total_pnl = sum(t.realized_pnl for t in self.positions.values() if t.closed)
        closed = sum(1 for t in self.positions.values() if t.closed) + len([1 for t in self.positions.values()]) - pos_count

        # Per-symbol trades
        sym_stats = []
        for sym in self.symbols:
            n = self.trades_today.get(sym, 0)
            sym_pnl = self.account.daily_pnl.get(sym, 0)
            sym_stats.append(f"{sym.split('.')[1]}:{n}t/{sym_pnl:+.0f}")

        line = (f"[{phase:12s}] 仓:{pos_count} 资金:${self.account.equity:,.0f} | " +
                " | ".join(sym_stats))
        logger.info(line)

    def _shutdown(self):
        self.running = False
        phase = get_market_phase()
        logger.info("正在退出...")

        # Close all positions
        for tid, trade in list(self.positions.items()):
            q = self.quotes.get(trade.symbol)
            if q:
                self._exit(tid, q, ExitReason.EOD_CLOSE)

        self.db.daily_stats()

        # Summary
        elapsed = (datetime.now() - self.start_time).total_seconds()
        closed = [t for t in self.positions.values() if t.closed]
        all_trades = list(self.positions.values())
        total_pnl = sum(t.realized_pnl for t in closed)
        wins = sum(1 for t in closed if t.realized_pnl > 0)

        logger.info("═" * 65)
        logger.info(f"  Whetstone v2 运行摘要 ({elapsed:.0f}s)")
        logger.info(f"  交易: {len(closed)}笔 | 胜率: {wins/len(closed):.1%}" if closed else "  交易: 0笔")
        for sym in self.symbols:
            n = self.trades_today.get(sym, 0)
            pnl = self.account.daily_pnl.get(sym, 0)
            logger.info(f"  {sym:12s}: {n}笔 P&L=${pnl:+.2f}")
        logger.info(f"  总P&L: ${total_pnl:+,.2f}")
        logger.info(f"  影子账户: ${self.account.equity:,.0f} (初始 ${SHADOW_CAPITAL:,.0f})")
        logger.info("═" * 65)

        if self.quote_ctx:
            try: self.quote_ctx.close()
            except: pass


# ═══════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Whetstone US v2 — Karpathy Multi-Symbol Scalping")
    parser.add_argument("--symbols", default=",".join(WHETSTONE_SYMBOLS),
                       help=f"标的列表 (默认: {','.join(WHETSTONE_SYMBOLS)})")
    parser.add_argument("--dry-run", action="store_true", help="模拟数据, 不连Futu")
    parser.add_argument("--max-cycles", type=int, default=0, help="最大周期数 (0=无限)")
    parser.add_argument("--db", default=None, help="数据库路径")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",")]
    runner = WhetstoneRunner(symbols=symbols, dry_run=args.dry_run, max_cycles=args.max_cycles)
    if args.db:
        runner.db = ShadowDB(args.db)
    runner.run()


if __name__ == "__main__":
    main()