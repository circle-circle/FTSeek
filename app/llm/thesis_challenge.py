"""
Thesis Challenge — LLM深度审视用户的进场理由，给出绝对判断。

设计原则:
- 输入深度: 技术指标(MA/RSI/MACD/ATR) + 板块联动 + 近期K线 + 战前计划bear case
- 推理深度: System prompt强制step-by-step多维度检查
- 输出精简: json_mode=True, temperature=0, max_tokens=500
- 每symbol 4h缓存，不重复调用
- verdict只有VALID/INVALID，没有"还行"
"""

import json
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger("ftseek_us.thesis_challenge")

CACHE_TTL_SECONDS = 4 * 3600  # 4 hours

_DB_PATH = None


def _get_db() -> str:
    global _DB_PATH
    if _DB_PATH is None:
        try:
            from app.core.paths import get_trade_records_db
            _DB_PATH = str(get_trade_records_db())
        except Exception:
            _DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "ftseek_us_live.db")
    return _DB_PATH


def _ensure_table():
    """确保thesis_challenges表存在"""
    conn = sqlite3.connect(_get_db())
    conn.execute("""
        CREATE TABLE IF NOT EXISTS thesis_challenges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            thesis TEXT NOT NULL,
            verdict TEXT NOT NULL,
            weakness TEXT DEFAULT '',
            fatal_if TEXT DEFAULT '',
            confidence INTEGER DEFAULT 0,
            context_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            model_used TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tc_symbol ON thesis_challenges(symbol, created_at)")
    conn.commit()
    conn.close()


def get_cached_challenge(symbol: str) -> Optional[Dict[str, Any]]:
    """查询缓存，4h内有效则返回"""
    _ensure_table()
    conn = sqlite3.connect(_get_db())
    conn.row_factory = sqlite3.Row
    cutoff = datetime.fromtimestamp(time.time() - CACHE_TTL_SECONDS).isoformat()
    row = conn.execute(
        "SELECT * FROM thesis_challenges WHERE symbol=? AND created_at > ? ORDER BY id DESC LIMIT 1",
        (symbol, cutoff)
    ).fetchone()
    conn.close()
    if row:
        return {
            "symbol": row["symbol"],
            "thesis": row["thesis"],
            "verdict": row["verdict"],
            "weakness": row["weakness"],
            "fatal_if": row["fatal_if"],
            "confidence": row["confidence"],
            "created_at": row["created_at"],
            "model_used": row["model_used"],
            "cached": True,
        }
    return None


def _save_challenge(symbol: str, thesis: str, result: Dict, model_used: str = ""):
    """保存挑战结果到DB"""
    _ensure_table()
    conn = sqlite3.connect(_get_db())
    conn.execute(
        "INSERT INTO thesis_challenges (symbol, thesis, verdict, weakness, fatal_if, confidence, created_at, model_used) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            symbol,
            thesis,
            result.get("verdict", "UNKNOWN"),
            result.get("weakness", ""),
            result.get("fatal_if", ""),
            int(result.get("confidence", 0)),
            datetime.now().isoformat(),
            model_used,
        )
    )
    conn.commit()
    conn.close()


# ──────────────────────────────────────────────────
#  数据采集: 为LLM构建深度上下文
# ──────────────────────────────────────────────────

def _collect_technicals_reuse(svc, symbol: str) -> Dict:
    """从已连接的svc获取技术指标 (MA/RSI/MACD/ATR/量比)"""
    from futu import RET_OK, SubType
    # Must subscribe before fetching K-line
    ret_sub, _ = svc._quote_ctx.subscribe([symbol], [SubType.K_DAY])
    if ret_sub != RET_OK:
        return {"error": "subscribe_failed"}
    import time; time.sleep(0.3)

    ret, data = svc._quote_ctx.get_cur_kline(symbol, 120, ktype="K_DAY")
    if ret != RET_OK or data.empty or len(data) < 20:
        return {"error": "K线不足"}

    closes = [float(r["close"]) for _, r in data.iterrows()]
    highs = [float(r["high"]) for _, r in data.iterrows()]
    lows = [float(r["low"]) for _, r in data.iterrows()]
    volumes = [float(r["volume"]) for _, r in data.iterrows()]

    def ma(arr, n):
        return round(sum(arr[-n:]) / n, 2) if len(arr) >= n else None

    result = {"cur_price": closes[-1]}

    # Moving averages
    for n in [5, 10, 20, 60]:
        val = ma(closes, n)
        if val:
            result[f"ma{n}"] = val
            result[f"price_vs_ma{n}_pct"] = round((closes[-1] - val) / val * 100, 2)

    # MA alignment
    ma5, ma10, ma20 = ma(closes, 5), ma(closes, 10), ma(closes, 20)
    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20:
            result["ma_alignment"] = "BULLISH"
        elif ma5 < ma10 < ma20:
            result["ma_alignment"] = "BEARISH"
        else:
            result["ma_alignment"] = "MIXED"

    # RSI-14
    if len(closes) >= 15:
        gains, losses = [], []
        for i in range(-14, 0):
            d = closes[i] - closes[i - 1]
            gains.append(max(d, 0))
            losses.append(max(-d, 0))
        avg_gain = sum(gains) / 14
        avg_loss = sum(losses) / 14
        if avg_loss > 0:
            result["rsi14"] = round(100 - (100 / (1 + avg_gain / avg_loss)), 1)
        else:
            result["rsi14"] = 100.0

    # MACD
    if len(closes) >= 26:
        ema12, ema26 = closes[0], closes[0]
        k12, k26 = 2 / 13, 2 / 27
        for p in closes:
            ema12 = p * k12 + ema12 * (1 - k12)
            ema26 = p * k26 + ema26 * (1 - k26)
        macd = round(ema12 - ema26, 4)
        result["macd"] = macd
        result["macd_signal"] = "ABOVE_ZERO" if macd > 0 else "BELOW_ZERO"

    # ATR-14
    if len(closes) >= 15:
        trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
               for i in range(1, len(closes))]
        atr = sum(trs[-14:]) / 14
        result["atr14"] = round(atr, 2)
        result["atr_pct"] = round(atr / closes[-1] * 100, 2)

    # Volume ratio
    if len(volumes) >= 20:
        vol_avg = sum(volumes[-20:]) / 20
        result["vol_ratio"] = round(volumes[-1] / vol_avg, 2) if vol_avg > 0 else 1.0

    # Recent price action
    result["change_5d_pct"] = round((closes[-1] - closes[-5]) / closes[-5] * 100, 2) if len(closes) >= 5 else None
    result["change_20d_pct"] = round((closes[-1] - closes[-20]) / closes[-20] * 100, 2) if len(closes) >= 20 else None
    result["high_20d"] = round(max(highs[-20:]), 2) if len(highs) >= 20 else None
    result["low_20d"] = round(min(lows[-20:]), 2) if len(lows) >= 20 else None

    # Position within 20d range
    if result.get("high_20d") and result.get("low_20d"):
        h, l, c = result["high_20d"], result["low_20d"], closes[-1]
        if h != l:
            result["range_position_pct"] = round((c - l) / (h - l) * 100, 0)

    return result


def _collect_sector_reuse(svc, symbol: str) -> Dict:
    """从已连接的svc获取板块联动数据"""
    SECTOR_MAP = {
        'Technology': ['US.AAPL', 'US.MSFT', 'US.GOOG', 'US.META', 'US.AMZN'],
        'Semiconductors': ['US.NVDA', 'US.AMD', 'US.INTC', 'US.TSM', 'US.MU', 'US.AVGO'],
        'Consumer': ['US.TSLA', 'US.NFLX', 'US.NKE', 'US.SBUX'],
        'Financials': ['US.JPM', 'US.GS', 'US.BAC', 'US.MS'],
        'China_ADR': ['US.BABA', 'US.PDD', 'US.JD', 'US.NIO', 'US.BIDU', 'US.LI'],
    }

    result = {"sector": "Unknown", "peer_changes": {}}

    for sec, tickers in SECTOR_MAP.items():
        if symbol in tickers:
            result["sector"] = sec
            break

    from futu import RET_OK
    peers = [t for sec, tickers in SECTOR_MAP.items()
             for t in tickers if t != symbol and sec == result["sector"]]
    if not peers:
        return result

    # Subscribe peers for snapshot
    svc._quote_ctx.subscribe(peers[:6], [])
    import time; time.sleep(0.3)

    ret, snap = svc._quote_ctx.get_market_snapshot(peers[:6])
    if ret == RET_OK and not snap.empty:
        for _, row in snap.iterrows():
            code = row.get("code", "")
            prev = float(row.get("prev_close_price", 0) or 0)
            cur = float(row.get("cur_price", 0) or 0)
            if prev > 0 and cur > 0:
                result["peer_changes"][code.replace("US.", "")] = round((cur - prev) / prev * 100, 2)

    return result


def _collect_technicals(symbol: str) -> Dict:
    """从Futu获取技术指标 (MA/RSI/MACD/ATR/量比)"""
    try:
        from app.web_dashboard.manual_order_service import ManualOrderService
        svc = ManualOrderService()
        if not svc.connect():
            return {"error": "broker_disconnected"}

        from futu import RET_OK, SubType
        # Must subscribe before fetching K-line
        ret_sub, _ = svc._quote_ctx.subscribe([symbol], [SubType.K_DAY])
        if ret_sub != RET_OK:
            svc.disconnect()
            return {"error": "subscribe_failed"}
        import time; time.sleep(0.5)

        ret, data = svc._quote_ctx.get_cur_kline(symbol, 120, ktype="K_DAY")
        if ret != RET_OK or data.empty or len(data) < 20:
            return {"error": "K线不足"}

        closes = [float(r["close"]) for _, r in data.iterrows()]
        highs = [float(r["high"]) for _, r in data.iterrows()]
        lows = [float(r["low"]) for _, r in data.iterrows()]
        volumes = [float(r["volume"]) for _, r in data.iterrows()]

        def ma(arr, n):
            return round(sum(arr[-n:]) / n, 2) if len(arr) >= n else None

        result = {"cur_price": closes[-1]}

        # Moving averages
        for n in [5, 10, 20, 60]:
            val = ma(closes, n)
            if val:
                result[f"ma{n}"] = val
                result[f"price_vs_ma{n}_pct"] = round((closes[-1] - val) / val * 100, 2)

        # MA alignment (bullish/bearish/neutral)
        ma5, ma10, ma20 = ma(closes, 5), ma(closes, 10), ma(closes, 20)
        if ma5 and ma10 and ma20:
            if ma5 > ma10 > ma20:
                result["ma_alignment"] = "BULLISH"
            elif ma5 < ma10 < ma20:
                result["ma_alignment"] = "BEARISH"
            else:
                result["ma_alignment"] = "MIXED"

        # RSI-14
        if len(closes) >= 15:
            gains, losses = [], []
            for i in range(-14, 0):
                d = closes[i] - closes[i - 1]
                gains.append(max(d, 0))
                losses.append(max(-d, 0))
            avg_gain = sum(gains) / 14
            avg_loss = sum(losses) / 14
            if avg_loss > 0:
                result["rsi14"] = round(100 - (100 / (1 + avg_gain / avg_loss)), 1)
            else:
                result["rsi14"] = 100.0

        # MACD
        if len(closes) >= 26:
            ema12, ema26 = closes[0], closes[0]
            k12, k26 = 2 / 13, 2 / 27
            for p in closes:
                ema12 = p * k12 + ema12 * (1 - k12)
                ema26 = p * k26 + ema26 * (1 - k26)
            macd = round(ema12 - ema26, 4)
            result["macd"] = macd
            result["macd_signal"] = "ABOVE_ZERO" if macd > 0 else "BELOW_ZERO"

        # ATR-14 (volatility)
        if len(closes) >= 15:
            trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
                   for i in range(1, len(closes))]
            atr = sum(trs[-14:]) / 14
            result["atr14"] = round(atr, 2)
            result["atr_pct"] = round(atr / closes[-1] * 100, 2)

        # Volume ratio (today vs 20d avg)
        if len(volumes) >= 20:
            vol_avg = sum(volumes[-20:]) / 20
            result["vol_ratio"] = round(volumes[-1] / vol_avg, 2) if vol_avg > 0 else 1.0

        # Recent price action (5d, 20d)
        result["change_5d_pct"] = round((closes[-1] - closes[-5]) / closes[-5] * 100, 2) if len(closes) >= 5 else None
        result["change_20d_pct"] = round((closes[-1] - closes[-20]) / closes[-20] * 100, 2) if len(closes) >= 20 else None
        result["high_20d"] = round(max(highs[-20:]), 2) if len(highs) >= 20 else None
        result["low_20d"] = round(min(lows[-20:]), 2) if len(lows) >= 20 else None

        # Position within 20d range
        if result.get("high_20d") and result.get("low_20d"):
            h, l, c = result["high_20d"], result["low_20d"], closes[-1]
            if h != l:
                result["range_position_pct"] = round((c - l) / (h - l) * 100, 0)

        svc.disconnect()
        return result

    except Exception as e:
        logger.debug(f"[ThesisChallenge] Technicals error: {e}")
        return {"error": str(e)}


def _collect_sector_context(symbol: str) -> Dict:
    """获取板块联动数据"""
    SECTOR_MAP = {
        'Technology': ['US.AAPL', 'US.MSFT', 'US.GOOG', 'US.META', 'US.AMZN'],
        'Semiconductors': ['US.NVDA', 'US.AMD', 'US.INTC', 'US.TSM', 'US.MU', 'US.AVGO'],
        'Consumer': ['US.TSLA', 'US.NFLX', 'US.NKE', 'US.SBUX'],
        'Financials': ['US.JPM', 'US.GS', 'US.BAC', 'US.MS'],
        'China_ADR': ['US.BABA', 'US.PDD', 'US.JD', 'US.NIO', 'US.BIDU', 'US.LI'],
    }

    result = {"sector": "Unknown", "peer_changes": {}}

    # Find sector
    for sec, tickers in SECTOR_MAP.items():
        if symbol in tickers:
            result["sector"] = sec
            break

    # Get peer quotes
    try:
        from app.web_dashboard.manual_order_service import ManualOrderService
        svc = ManualOrderService()
        if not svc.connect():
            return result

        from futu import RET_OK
        peers = [t for sec, tickers in SECTOR_MAP.items()
                 for t in tickers if t != symbol and sec == result["sector"]]
        if not peers:
            svc.disconnect()
            return result

        # Must subscribe before snapshot
        svc._quote_ctx.subscribe(peers[:6], [])
        import time; time.sleep(0.3)

        ret, snap = svc._quote_ctx.get_market_snapshot(peers[:6])
        if ret == RET_OK and not snap.empty:
            for _, row in snap.iterrows():
                code = row.get("code", "")
                prev = float(row.get("prev_close_price", 0) or 0)
                cur = float(row.get("cur_price", 0) or 0)
                if prev > 0 and cur > 0:
                    result["peer_changes"][code.replace("US.", "")] = round((cur - prev) / prev * 100, 2)

        svc.disconnect()
    except Exception:
        pass

    return result


def _collect_trade_history(symbol: str) -> Dict:
    """获取该symbol的历史交易表现"""
    try:
        conn = sqlite3.connect(_get_db())
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT direction, realized_pnl, entry_time, exit_time "
            "FROM trade_records WHERE symbol=? AND trade_mode != 'simulate' "
            "ORDER BY exit_time DESC LIMIT 5",
            (symbol,)
        ).fetchall()
        conn.close()

        if not rows:
            return {"count": 0}

        wins = sum(1 for r in rows if float(r["realized_pnl"] or 0) > 0)
        total_pnl = sum(float(r["realized_pnl"] or 0) for r in rows)
        return {
            "count": len(rows),
            "wins": wins,
            "win_rate": round(wins / len(rows) * 100, 0) if rows else 0,
            "total_pnl": round(total_pnl, 2),
            "last_trades": [
                {
                    "direction": r["direction"],
                    "pnl": round(float(r["realized_pnl"] or 0), 2),
                    "date": (r["exit_time"] or "")[:10],
                }
                for r in rows
            ],
        }
    except Exception:
        return {"count": 0}


# ──────────────────────────────────────────────────
#  System Prompt: 强制step-by-step多维度推理
# ──────────────────────────────────────────────────

SYSTEM_PROMPT = """你是一个严苛的量化交易审计师。你的任务是判断用户的进场理由(thesis)在当前市场条件下是否成立。

你必须按以下步骤逐一检查(内部推理,不输出):

## 检查清单 (5个维度,逐一过)

### 1. 技术面一致性
- thesis方向与均线排列(MA alignment)是否一致?
- RSI是否处于极端区域(>70超买做多=危险, <30超卖做空=危险)?
- MACD信号与thesis方向是否矛盾?
- 价格在20日区间的位置(高位做多=追高, 低位做空=追空)?

### 2. 板块联动
- 同板块peers今日涨跌是否与thesis方向一致?
- 如果peers普遍下跌但用户做多,这是逆板块趋势

### 3. 风险收益比
- 从当前价格到thesis的止损/失效价,空间多大?
- ATR波动率相对于预期收益是否合理?
- 持仓已亏损时,thesis是否只是"不愿认错"的借口?

### 4. 历史表现
- 该symbol过去的交易胜率和盈亏比
- 是否反复在同一只股票上亏损

### 5. Thesis实质内容
- thesis是否包含具体的逻辑链(因为A所以B)?
- 还是空泛的"看好"/"盘感"/"跟风"?
- 如果thesis是情绪化的("觉得跌多了该反弹"),直接INVALID

## 判定规则
- 5个维度中≥3个支持thesis → VALID
- 5个维度中≥3个反对thesis → INVALID
- 技术面+板块联动都反对 → INVALID (不管其他维度)
- thesis无实质内容 → 直接INVALID

## 输出格式 (严格JSON,不输出推理过程)
{
  "verdict": "VALID" 或 "INVALID",
  "weakness": "一句话指出最致命的问题(必须引用具体数据)",
  "fatal_if": "具体的价格或事件条件,触发则thesis必死",
  "confidence": 0-100,
  "check_results": {
    "technical": "SUPPORT" 或 "AGAINST" 或 "NEUTRAL",
    "sector": "SUPPORT" 或 "AGAINST" 或 "NEUTRAL",
    "risk_reward": "OK" 或 "POOR",
    "history": "POSITIVE" 或 "NEGATIVE" 或 "NONE",
    "thesis_quality": "STRONG" 或 "WEAK" 或 "EMPTY"
  }
}

要求:
- weakness必须引用具体数字(如"RSI=72超买区做多"而非"技术指标不利")
- fatal_if必须是可验证的条件(如"跌破$140"或"TSM财报下调指引")
- check_results的5个维度必须全部填写
- 不要输出推理过程,不要给建议,只输出JSON"""


# ──────────────────────────────────────────────────
#  主函数
# ──────────────────────────────────────────────────

def challenge_thesis(
    symbol: str,
    thesis: str,
    direction: str = "",
    cur_price: float = 0,
    cost_price: float = 0,
    pnl_pct: float = 0,
    hold_days: int = 0,
    bear_cases: str = "",
) -> Dict[str, Any]:
    """
    调用LLM深度挑战thesis。先查缓存，无缓存则采集数据+调用。
    
    Returns: {verdict, weakness, fatal_if, confidence, check_results, cached, model_used, created_at}
    """
    # 1. Check cache
    cached = get_cached_challenge(symbol)
    if cached:
        return cached

    # 2. Collect deep context (single broker connection for efficiency)
    tech, sector, trade_hist = {}, {}, {}
    try:
        from app.web_dashboard.manual_order_service import ManualOrderService as _Svc
        _svc = _Svc()
        _connected = _svc.connect()
    except Exception:
        _connected = False

    if _connected:
        try:
            tech = _collect_technicals_reuse(_svc, symbol)
        except Exception as e:
            tech = {"error": str(e)}
        try:
            sector = _collect_sector_reuse(_svc, symbol)
        except Exception as e:
            sector = {"sector": "Unknown", "peer_changes": {}}
        try:
            _svc.disconnect()
        except Exception:
            pass

    try:
        trade_hist = _collect_trade_history(symbol)
    except Exception:
        trade_hist = {"count": 0}

    # 3. Build rich prompt
    user_prompt = f"""## 标的: {symbol}
## 方向: {direction}
## 进场理由(thesis): {thesis}
## 持仓状态: 成本${cost_price:.2f} → 现价${cur_price:.2f} (盈亏{pnl_pct:+.1f}%), 持仓{hold_days}天"""

    # Technical data
    if "error" not in tech:
        user_prompt += "\n\n## 技术指标"
        for key in ["ma_alignment", "rsi14", "macd_signal", "atr_pct", "vol_ratio",
                     "price_vs_ma20_pct", "change_5d_pct", "change_20d_pct", "range_position_pct"]:
            if key in tech and tech[key] is not None:
                label = key.replace("_", " ").title()
                user_prompt += f"\n- {label}: {tech[key]}"
        if "cur_price" in tech:
            user_prompt += f"\n- Current Price: ${tech['cur_price']:.2f}"
    else:
        user_prompt += f"\n\n## 技术指标: 不可用 ({tech['error']})"

    # Sector context
    if sector.get("peer_changes"):
        user_prompt += f"\n\n## 板块联动 ({sector['sector']})"
        for peer, chg in sector["peer_changes"].items():
            user_prompt += f"\n- {peer}: {chg:+.1f}%"

    # Trade history
    if trade_hist.get("count", 0) > 0:
        user_prompt += f"\n\n## 该symbol历史交易"
        user_prompt += f"\n- 总交易次数: {trade_hist['count']}"
        user_prompt += f"\n- 胜率: {trade_hist['win_rate']:.0f}%"
        user_prompt += f"\n- 总盈亏: ${trade_hist['total_pnl']:.2f}"
        if trade_hist.get("last_trades"):
            for t in trade_hist["last_trades"][:3]:
                user_prompt += f"\n- {t['date']} {t['direction']}: ${t['pnl']:+.2f}"

    # Bear cases from battle plan
    if bear_cases:
        user_prompt += f"\n\n## 空头审计师质疑(来自战前计划):\n{bear_cases}"

    # 4. Call LLM
    try:
        from app.llm.unified_service import UnifiedLLMService
        result = UnifiedLLMService.call(
            "thesis_challenge",
            user_prompt,
            system=SYSTEM_PROMPT,
            temperature=0,
            max_tokens=500,
            json_mode=True,
            timeout=30,
        )

        if result.success:
            # Parse JSON from response
            raw = result.response
            if isinstance(raw, str):
                raw = raw.strip()
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                if raw.endswith("```"):
                    raw = raw[:-3]
                raw = raw.strip()
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = {"verdict": "INVALID", "weakness": "LLM输出解析失败", "fatal_if": "需人工判断", "confidence": 0, "check_results": {}}
            else:
                parsed = raw if isinstance(raw, dict) else {"verdict": "INVALID", "weakness": "LLM返回异常", "fatal_if": "需人工判断", "confidence": 0, "check_results": {}}

            # Validate required fields
            if parsed.get("verdict") not in ("VALID", "INVALID"):
                parsed["verdict"] = "INVALID"
            parsed["confidence"] = max(0, min(100, int(parsed.get("confidence", 0))))
            parsed["weakness"] = str(parsed.get("weakness", ""))[:300]
            parsed["fatal_if"] = str(parsed.get("fatal_if", ""))[:300]
            # Ensure check_results exists
            if not isinstance(parsed.get("check_results"), dict):
                parsed["check_results"] = {}

            # Save to DB
            _save_challenge(symbol, thesis, parsed, result.model_used or "")

            return {
                "symbol": symbol,
                "thesis": thesis,
                "verdict": parsed["verdict"],
                "weakness": parsed["weakness"],
                "fatal_if": parsed["fatal_if"],
                "confidence": parsed["confidence"],
                "check_results": parsed.get("check_results", {}),
                "created_at": datetime.now().isoformat(),
                "model_used": result.model_used or "",
                "cached": False,
            }
        else:
            logger.error(f"[ThesisChallenge] LLM调用失败: {result.error}")
            return {"error": result.error or "LLM调用失败", "cached": False}

    except Exception as e:
        logger.error(f"[ThesisChallenge] 异常: {e}")
        return {"error": str(e), "cached": False}
