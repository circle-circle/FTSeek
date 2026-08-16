"""DEB-20260805(二) 前置实测: B1 免订阅十档盘口 + C2 extended_time 盘前K线.

只读探测, 零订阅零下单. 结论回填 DEB §四.
"""
import sys, json

sys.path.insert(0, "/opt/ftseek/FTSeek-US")
from futu import OpenQuoteContext, KLType, RET_OK

HOST, PORT = "127.0.0.1", 11111
SYMBOL = "US.AAPL"

ctx = OpenQuoteContext(host=HOST, port=PORT)
results = {}

# ── B1: 未订阅 ORDER_BOOK 直接 HTTP 拉十档 ──
try:
    ret, data = ctx.get_order_book(SYMBOL, num=10)
    if ret == RET_OK and data is not None and len(data) > 0:
        row = data.iloc[0]
        bids = [(row.get(f"BidPrice{i}", 0), row.get(f"BidVol{i}", 0)) for i in range(1, 11)]
        asks = [(row.get(f"AskPrice{i}", 0), row.get(f"AskVol{i}", 0)) for i in range(1, 11)]
        nz_bids = sum(1 for p, v in bids if p > 0 and v > 0)
        nz_asks = sum(1 for p, v in asks if p > 0 and v > 0)
        results["B1_orderbook_no_sub"] = {
            "ok": True, "nonzero_bid_levels": nz_bids, "nonzero_ask_levels": nz_asks,
            "bid1": bids[0], "ask1": asks[0],
        }
    else:
        results["B1_orderbook_no_sub"] = {"ok": False, "ret": ret, "msg": str(data)[:200]}
except Exception as e:
    results["B1_orderbook_no_sub"] = {"ok": False, "error": str(e)}

# ── C2: request_history_kline extended_time 是否含盘前段 ──
try:
    import inspect
    sig = str(inspect.signature(ctx.request_history_kline))
    results["C2_signature"] = sig[:300]
    kwargs = {"code": SYMBOL, "start": "2026-08-04", "end": "2026-08-05",
              "ktype": KLType.K_1M, "max_count": 1000}
    try:
        ret, data, _ = ctx.request_history_kline(extended_time=True, **kwargs)
        mode = "extended_time=True"
    except TypeError as te:
        ret, data, _ = ctx.request_history_kline(**kwargs)
        mode = "fallback(no extended_time param)"
    if ret == RET_OK and data is not None and len(data) > 0:
        tks = data["time_key"].astype(str).tolist()
        day2 = [t for t in tks if t.startswith("2026-08-05")]
        pre = [t for t in day2 if t[11:16] < "09:30"]
        rth = [t for t in day2 if "09:30" <= t[11:16] <= "16:00"]
        results["C2_history_kline"] = {
            "ok": True, "mode": mode, "bars_0805_total": len(day2),
            "premarket_bars": len(pre), "rth_bars": len(rth),
            "first_bar": day2[0] if day2 else None, "last_bar": day2[-1] if day2 else None,
        }
    else:
        results["C2_history_kline"] = {"ok": False, "mode": mode, "ret": ret, "msg": str(data)[:200]}
except Exception as e:
    results["C2_history_kline"] = {"ok": False, "error": str(e)}

ctx.close()
print(json.dumps(results, ensure_ascii=False, indent=1, default=str))
