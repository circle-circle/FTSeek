"""
个股详情路由 — K线 + 盘口 + 逐笔 + 技术分析 + LLM 分析
"""
import asyncio
import json
import logging
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.web_dashboard.data_service import get_data_service, safe_float

logger = logging.getLogger("ftseek_us.routes.stock_detail")
router = APIRouter()

# K 线类型映射
_KTYPE_MAP = {
    "K_1M": "K_1M", "K_5M": "K_5M", "K_15M": "K_15M",
    "K_30M": "K_30M", "K_60M": "K_60M", "K_DAY": "K_DAY",
    "K_WEEK": "K_WEEK", "K_MON": "K_MON",
}


def _subscribe_symbol(svc, symbol: str, sub_types: list) -> bool:
    try:
        from futu import RET_OK, SubType
        type_map = {
            "K_DAY": SubType.K_DAY, "K_1M": SubType.K_1M,
            "K_5M": SubType.K_5M, "K_15M": SubType.K_15M,
            "K_30M": SubType.K_30M, "K_60M": SubType.K_60M,
            "ORDER_BOOK": SubType.ORDER_BOOK,
            "TICKER": SubType.TICKER, "QUOTE": SubType.QUOTE,
        }
        futu_types = [type_map[t] for t in sub_types if t in type_map]
        if not futu_types:
            return True
        ret, err = svc._quote_ctx.subscribe([symbol], futu_types, subscribe_push=False)
        if ret != RET_OK:
            logger.warning(f"[StockDetail] subscribe failed {symbol}: {err}")
            return False
        return True
    except Exception as e:
        logger.error(f"[StockDetail] subscribe error {symbol}: {e}")
        return False


def compute_technicals(svc, symbol: str) -> dict:
    """日线技术指标 (MA, RSI, MACD, ATR, 量比)"""
    try:
        from futu import RET_OK
        ret, data = svc._quote_ctx.get_cur_kline(symbol, 120, ktype="K_DAY")
        if ret != RET_OK or data.empty or len(data) < 20:
            return {"error": "K线数据不足"}

        closes = [float(r["close"]) for _, r in data.iterrows()]
        highs = [float(r["high"]) for _, r in data.iterrows()]
        lows = [float(r["low"]) for _, r in data.iterrows()]
        volumes = [float(r["volume"]) for _, r in data.iterrows()]

        def ma(arr, n):
            return sum(arr[-n:]) / n if len(arr) >= n else None

        ma5, ma10, ma20, ma60 = ma(closes, 5), ma(closes, 10), ma(closes, 20), ma(closes, 60)

        # RSI
        rsi = None
        if len(closes) >= 15:
            gains, losses = [], []
            for i in range(-14, 0):
                d = closes[i] - closes[i - 1]
                gains.append(max(d, 0))
                losses.append(max(-d, 0))
            avg_gain = sum(gains) / 14
            avg_loss = sum(losses) / 14
            if avg_loss > 0:
                rsi = 100 - (100 / (1 + avg_gain / avg_loss))
            else:
                rsi = 100.0

        # MACD
        macd_line, signal_line, histogram = None, None, None
        if len(closes) >= 26:
            ema12, ema26 = closes[0], closes[0]
            k12, k26 = 2 / 13, 2 / 27
            ema12_vals, ema26_vals = [], []
            for p in closes:
                ema12 = p * k12 + ema12 * (1 - k12)
                ema26 = p * k26 + ema26 * (1 - k26)
                ema12_vals.append(ema12)
                ema26_vals.append(ema26)
            macd_vals = [e12 - e26 for e12, e26 in zip(ema12_vals, ema26_vals)]
            k9 = 2 / 10
            sig = macd_vals[0]
            for m in macd_vals:
                sig = m * k9 + sig * (1 - k9)
            macd_line = round(macd_vals[-1], 4)
            signal_line = round(sig, 4)
            histogram = round(macd_line - signal_line, 4)

        # ATR
        atr = None
        if len(closes) >= 15:
            trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
                   for i in range(1, len(closes))]
            atr = sum(trs[-14:]) / 14 if len(trs) >= 14 else None

        vol_ma5 = ma(volumes, 5)
        vol_cur = volumes[-1]

        recent_5d = [{
            "date": str(r.get("time_key", ""))[:10],
            "open": round(float(r["open"]), 2), "high": round(float(r["high"]), 2),
            "low": round(float(r["low"]), 2), "close": round(float(r["close"]), 2),
            "volume": int(float(r["volume"])),
        } for _, r in data.tail(5).iterrows()]

        return {
            "closes": closes, "highs": highs, "lows": lows, "volumes": volumes,
            "recent_5d": recent_5d, "cur_price": round(closes[-1], 2),
            "ma5": round(ma5, 2) if ma5 else None,
            "ma10": round(ma10, 2) if ma10 else None,
            "ma20": round(ma20, 2) if ma20 else None,
            "ma60": round(ma60, 2) if ma60 else None,
            "rsi14": round(rsi, 1) if rsi is not None else None,
            "macd": macd_line, "macd_signal": signal_line, "macd_hist": histogram,
            "atr14": round(atr, 2) if atr else None,
            "20d_high": round(max(highs[-20:]), 2),
            "20d_low": round(min(lows[-20:]), 2),
            "vol_current": int(vol_cur),
            "vol_ma5": int(vol_ma5) if vol_ma5 else None,
            "vol_ratio": round(vol_cur / vol_ma5, 2) if vol_ma5 and vol_ma5 > 0 else None,
            "vol_avg_20d": int(sum(volumes[-20:]) / 20) if len(volumes) >= 20 else None,
            "vol_today": int(volumes[-1]),
        }
    except Exception as e:
        return {"error": "数据查询异常"}


def register(app, templates):
    @router.get("/stock/{symbol}", response_class=HTMLResponse)
    async def stock_detail_page(symbol: str, request: Request):
        symbol = symbol.upper()
        if not symbol.startswith("US."):
            symbol = "US." + symbol
        return templates.TemplateResponse("stock_detail.html", {
            "request": request, "symbol": symbol,
        })

    @router.get("/api/stock/{symbol}/kline")
    async def stock_kline(symbol: str, ktype: str = "K_5M", num: int = 100):
        ds = get_data_service()
        symbol = symbol.upper()
        try:
            raw = await ds.get_kline(symbol, num=num, ktype=ktype)
            if raw:
                records = [{
                    "time": str(r.get("time_key", "")),
                    "open": float(r.get("open", 0)),
                    "high": float(r.get("high", 0)),
                    "low": float(r.get("low", 0)),
                    "close": float(r.get("close", 0)),
                    "volume": int(float(r.get("volume", 0))),
                } for r in raw]
                return {"symbol": symbol, "kline": records}
            return {"symbol": symbol, "kline": [], "error": "no_data"}
        except Exception as e:
            return {"symbol": symbol, "kline": [], "error": "数据查询异常"}

    @router.get("/api/stock/{symbol}/orderbook")
    async def stock_orderbook(symbol: str):
        ds = get_data_service()
        symbol = symbol.upper()
        try:
            data = await ds.get_orderbook(symbol, num=10)
            if data:
                bids = data.get("Bid", [])
                asks = data.get("Ask", [])
                return {
                    "symbol": symbol,
                    "bids": [{"price": float(b[0]), "volume": int(b[1]), "order_count": int(b[2]) if len(b) > 2 else 0} for b in bids],
                    "asks": [{"price": float(a[0]), "volume": int(a[1]), "order_count": int(a[2]) if len(a) > 2 else 0} for a in asks],
                }
            return {"symbol": symbol, "bids": [], "asks": [], "error": "no_data"}
        except Exception as e:
            return {"symbol": symbol, "bids": [], "asks": [], "error": "数据查询异常"}

    @router.get("/api/stock/{symbol}/ticker")
    async def stock_ticker(symbol: str, num: int = 100):
        ds = get_data_service()
        symbol = symbol.upper()
        try:
            raw = await ds.get_rt_ticker(symbol, num=num)
            if raw:
                records = [{
                    "time": str(r.get("time_key", "")),
                    "price": float(r.get("price", 0)),
                    "volume": int(float(r.get("volume", 0))),
                    "direction": str(r.get("ticker_direction", "")),
                    "type": str(r.get("type", "")),
                } for r in raw]
                return {"symbol": symbol, "ticker": records}
            return {"symbol": symbol, "ticker": [], "error": "no_data"}
        except Exception as e:
            return {"symbol": symbol, "ticker": [], "error": "数据查询异常"}

    @router.get("/api/stock/{symbol}/analysis")
    async def stock_analysis(symbol: str):
        ds = get_data_service()
        svc = ds.svc()
        if not svc.connect():
            return JSONResponse({"error": "broker_not_connected"}, 503)
        symbol = symbol.upper()

        result = {"symbol": symbol, "technicals": {}, "position": None, "recommendation": {}}

        await ds._run(_subscribe_symbol, svc, symbol, ["K_DAY"])
        tech = await ds._run(compute_technicals, svc, symbol)

        if "error" in tech:
            result["technicals"] = {"error": tech["error"]}
        else:
            result["technicals"] = {
                "cur_price": tech["cur_price"],
                "ma5": tech["ma5"], "ma10": tech["ma10"],
                "ma20": tech["ma20"], "ma60": tech["ma60"],
                "rsi14": tech["rsi14"],
                "macd": tech["macd"], "macd_signal": tech["macd_signal"], "macd_hist": tech["macd_hist"],
                "atr14": tech["atr14"],
                "vol_current": tech["vol_current"], "vol_ma5": tech["vol_ma5"], "vol_ratio": tech["vol_ratio"],
            }

            signals = []
            score = 0
            cur = tech["cur_price"]
            ma5, ma10, ma20 = tech["ma5"], tech["ma10"], tech["ma20"]
            rsi = tech["rsi14"]

            if ma5 and ma10 and ma20:
                if ma5 > ma10 > ma20:
                    signals.append(("MA多头排列", "+bullish"))
                    score += 20
                elif ma5 < ma10 < ma20:
                    signals.append(("MA空头排列", "+bearish"))
                    score -= 20
                if cur > ma20:
                    score += 10
                else:
                    score -= 10

            if rsi is not None:
                if rsi > 70:
                    signals.append((f"RSI超买({rsi:.0f})", "+bearish"))
                    score -= 15
                elif rsi < 30:
                    signals.append((f"RSI超卖({rsi:.0f})", "+bullish"))
                    score += 15
                elif 40 < rsi < 60:
                    signals.append((f"RSI中性({rsi:.0f})", "+neutral"))

            macd_line = tech["macd"]
            signal_line = tech["macd_signal"]
            histogram = tech["macd_hist"]
            if histogram is not None:
                if histogram > 0 and macd_line > signal_line:
                    signals.append(("MACD金叉", "+bullish"))
                    score += 15
                elif histogram < 0 and macd_line < signal_line:
                    signals.append(("MACD死叉", "+bearish"))
                    score -= 15

            vol_ma5 = tech["vol_ma5"]
            vol_cur = tech["vol_current"]
            if vol_ma5 and vol_ma5 > 0:
                vratio = vol_cur / vol_ma5
                if vratio > 2:
                    signals.append((f"放量{vratio:.1f}x", "+attention"))
                elif vratio < 0.5:
                    signals.append((f"缩量{vratio:.1f}x", "+attention"))

            if score >= 30:
                rec, rec_color = "偏多", "green"
            elif score <= -30:
                rec, rec_color = "偏空", "red"
            else:
                rec, rec_color = "中性观望", "yellow"

            result["recommendation"] = {
                "direction": rec, "color": rec_color,
                "score": score, "signals": signals,
            }

        # 引擎持仓
        try:
            from app.core.paths import get_trade_records_db
            rows = await ds.db_query(
                "SELECT symbol, direction, quantity, avg_entry_price, stop_loss, take_profit, current_price "
                "FROM open_positions WHERE symbol = ? AND status = 'open'",
                (symbol,), db_path=str(get_trade_records_db()),
            )
            if rows:
                r = rows[0]
                result["position"] = {
                    "symbol": r["symbol"], "direction": r["direction"], "quantity": r["quantity"],
                    "entry_price": round(float(r.get("avg_entry_price") or 0), 2),
                    "stop_loss": round(float(r.get("stop_loss") or 0), 2),
                    "take_profit": round(float(r.get("take_profit") or 0), 2),
                    "current_price": round(float(r.get("current_price") or 0), 2),
                }
        except Exception:
            pass

        return result

    @router.get("/api/stock/{symbol}/llm-analysis")
    async def stock_llm_analysis(symbol: str):
        ds = get_data_service()
        svc = ds.svc()
        if not svc.connect():
            return JSONResponse({"error": "broker_not_connected"}, 503)
        symbol = symbol.upper()

        data_bundle = {"symbol": symbol}
        await ds._run(_subscribe_symbol, svc, symbol, ["K_DAY", "ORDER_BOOK", "TICKER", "QUOTE"])

        tech = await ds._run(compute_technicals, svc, symbol)
        if "error" not in tech:
            data_bundle["daily_summary"] = {
                "cur_price": tech["cur_price"],
                "ma5": tech["ma5"], "ma10": tech["ma10"],
                "ma20": tech["ma20"], "ma60": tech["ma60"],
                "rsi14": tech["rsi14"], "atr14": tech["atr14"],
                "recent_5d": tech["recent_5d"],
                "20d_high": tech["20d_high"], "20d_low": tech["20d_low"],
                "vol_avg_20d": tech["vol_avg_20d"], "vol_today": tech["vol_today"],
            }

        # 盘口 + 逐笔 + snapshot (H4: 在线程池中并行)
        try:
            from futu import RET_OK
            import asyncio as _aio
            _loop = _aio.get_running_loop()
            ob_task = _loop.run_in_executor(ds._pool, svc._quote_ctx.get_order_book, symbol, 5)
            ticker_task = _loop.run_in_executor(ds._pool, svc._quote_ctx.get_rt_ticker, symbol, 200)
            snap_task = _loop.run_in_executor(ds._pool, svc._quote_ctx.get_market_snapshot, [symbol])
            (ret2, ob), (ret3, ticker), (ret4, snap) = await _aio.gather(ob_task, ticker_task, snap_task)
            if ret2 == RET_OK and ob:
                bids = ob.get("Bid", [])
                asks = ob.get("Ask", [])
                data_bundle["orderbook"] = {
                    "top_bid": {"price": bids[0][0], "volume": bids[0][1]} if bids else None,
                    "top_ask": {"price": asks[0][0], "volume": asks[0][1]} if asks else None,
                    "bid_depth": sum(b[1] for b in bids),
                    "ask_depth": sum(a[1] for a in asks),
                    "imbalance": round((sum(b[1] for b in bids) - sum(a[1] for a in asks)) /
                                       max(sum(b[1] for b in bids) + sum(a[1] for a in asks), 1), 3),
                }

            if ret3 == RET_OK and not ticker.empty:
                buy_vol = sell_vol = buy_cnt = sell_cnt = 0
                for _, r in ticker.iterrows():
                    d = str(r.get("ticker_direction", "")).lower()
                    v = int(float(r.get("volume", 0)))
                    if "buy" in d or d == "b":
                        buy_vol += v; buy_cnt += 1
                    elif "sell" in d or d == "s":
                        sell_vol += v; sell_cnt += 1
                total = buy_vol + sell_vol
                data_bundle["ticker_summary"] = {
                    "buy_volume": buy_vol, "sell_volume": sell_vol,
                    "buy_ratio": round(buy_vol / total, 3) if total > 0 else 0.5,
                    "buy_count": buy_cnt, "sell_count": sell_cnt,
                    "avg_buy_size": round(buy_vol / max(buy_cnt, 1), 0),
                    "avg_sell_size": round(sell_vol / max(sell_cnt, 1), 0),
                }

            if ret4 == RET_OK and not snap.empty:
                row = snap.iloc[0]
                data_bundle["snapshot"] = {
                    "prev_close": safe_float(row.get("prev_close_price")),
                    "open": safe_float(row.get("open_price")),
                    "high": safe_float(row.get("high_price")),
                    "low": safe_float(row.get("low_price")),
                    "volume": int(safe_float(row.get("volume"))),
                    "turnover": safe_float(row.get("turnover")),
                    "pe_ratio": safe_float(row.get("pe_ratio")),
                    "market_cap": safe_float(row.get("market_cap")),
                }
        except Exception as e:
            data_bundle["data_error"] = "data_fetch_failed"

        # 引擎持仓
        try:
            from app.core.paths import get_trade_records_db
            rows = await ds.db_query(
                "SELECT direction, quantity, avg_entry_price, stop_loss, take_profit, current_price "
                "FROM open_positions WHERE symbol = ? AND status = 'open'",
                (symbol,), db_path=str(get_trade_records_db()),
            )
            if rows:
                r = rows[0]
                data_bundle["engine_position"] = {
                    "direction": r["direction"], "quantity": r["quantity"],
                    "entry_price": round(float(r.get("avg_entry_price") or 0), 2),
                    "stop_loss": round(float(r.get("stop_loss") or 0), 2),
                    "take_profit": round(float(r.get("take_profit") or 0), 2),
                    "current_price": round(float(r.get("current_price") or 0), 2),
                }
        except Exception:
            pass

        # LLM 调用 (在线程池中执行, 不阻塞 event loop)
        system_prompt = """你是 FTSeek 量化交易系统的 LLM 分析引擎。基于提供的实时市场数据，给出专业、可操作的分析建议。

【核心原则】
1. 数据驱动：每个判断必须有数据支撑，不猜测
2. 方向明确：给出清晰的 LONG/SHORT/NEUTRAL 方向，避免模棱两可
3. 风险优先：始终标注关键止损位和风险点
4. 简洁有力：总分析不超过200字

【返回格式】纯 JSON，不要代码块包裹:
{
  "direction": "LONG" | "SHORT" | "NEUTRAL",
  "confidence": 0.0-1.0,
  "summary": "一句话结论（20字内）",
  "reasoning": "核心逻辑（50-100字）",
  "key_levels": {"support": 数值, "resistance": 数值},
  "risk_notes": "风险提示（30字内）",
  "action": "建议操作（买入/卖出/持有/观望/加仓/减仓，10字内）"
}"""

        user_prompt = f"分析标的 {symbol} 的当前状态，给出交易建议。\n\n数据:\n{json.dumps(data_bundle, ensure_ascii=False, indent=2)}"

        try:
            from app.llm.unified_service import UnifiedLLMService

            # 在线程池中执行 LLM 调用 (最长 60s, 不阻塞 event loop)
            loop = asyncio.get_running_loop()
            llm_result = await loop.run_in_executor(
                ds._pool,
                lambda: UnifiedLLMService.call(
                    "stock_analyst", user_prompt,
                    system=system_prompt, temperature=0.2,
                    max_tokens=4000, json_mode=True, timeout=60,
                ),
            )

            if llm_result and llm_result.success:
                resp_text = llm_result.response
                if not resp_text:
                    return {"symbol": symbol, "error": "LLM返回为空(可能token配额耗尽)"}
                if isinstance(resp_text, dict):
                    analysis = resp_text
                elif isinstance(resp_text, str):
                    resp_text = resp_text.strip()
                    if resp_text.startswith("```"):
                        resp_text = resp_text.split("\n", 1)[1] if "\n" in resp_text else resp_text[3:]
                    if resp_text.endswith("```"):
                        resp_text = resp_text[:-3]
                    resp_text = resp_text.strip()
                    analysis = json.loads(resp_text)
                else:
                    analysis = resp_text
                return {
                    "symbol": symbol, "analysis": analysis,
                    "model_used": getattr(llm_result, 'model_used', 'unknown'),
                    "latency_ms": getattr(llm_result, 'latency', 0),
                }
            else:
                err = getattr(llm_result, 'error', None) or 'unknown'
                return {"symbol": symbol, "error": f"LLM调用失败: {err}"}
        except json.JSONDecodeError as e:
            return {"symbol": symbol, "error": f"LLM返回解析失败: {e}"}
        except Exception as e:
            return {"symbol": symbol, "error": f"分析异常: {e}"}

    @router.get("/api/stock/{symbol}/sector")
    async def stock_sector(symbol: str):
        """股票所属板块 + 关联股票 (Futu get_owner_plate + get_referencestocklist)"""
        from app.web_dashboard.data_service import get_data_service
        ds = get_data_service()

        symbol = symbol.upper()
        if not symbol.startswith("US."):
            symbol = "US." + symbol

        plate_data, ref_stocks = await asyncio.gather(
            ds.get_owner_plate(symbol),
            ds.get_reference_stocks(symbol),
        )

        return {
            "symbol": symbol,
            "plates": plate_data.get("plates", []),
            "reference_stocks": ref_stocks[:10],
        }

    app.include_router(router)
