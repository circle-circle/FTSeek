# -*- coding: utf-8 -*-
"""拉取 watchlist 44 票 1分K 历史 (2026-01-02 ~ 2026-08-18).

频控: 富途 60次/30s/连接 → sleep 0.6s + 限频退避重试。
断点续传: 按票落盘 data/replay_bars_1m/{sym}.json, 已有完整文件则跳过。
输出: [datetime, open, high, low, close, volume]
"""
import json, os, sys, time
from pathlib import Path

ROOT = Path("/opt/ftseek/FTSeek-US")
sys.path.insert(0, str(ROOT))
OUT_DIR = ROOT / "data" / "replay_bars_1m"
OUT_DIR.mkdir(exist_ok=True)

START, END = "2026-01-02", "2026-08-18"
WATCH = ["US.TSLA","US.COIN","US.CAT","US.GS","US.CVX","US.NVDA","US.RIVN","US.UBER","US.SNDK",
         "US.ARM","US.MU","US.BWXT","US.LLY","US.AAPL","US.MSFT","US.GOOG","US.AMZN","US.META",
         "US.AVGO","US.TSM","US.BABA","US.PDD","US.NIO","US.NFLX","US.CRM","US.ORCL","US.JPM",
         "US.BA","US.LMT","US.UNH","US.JNJ","US.V","US.ABBV","US.MRK","US.MA","US.HON","US.KO",
         "US.PEP","US.DIS","US.VZ","US.COST","US.WMT","US.SPOT","US.XOM"]

from futu import OpenQuoteContext, RET_OK, KLType, AuType

ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
done = skip = fail = 0
t0 = time.time()
try:
    for sym in WATCH:
        out_fp = OUT_DIR / (sym.replace(".", "_") + ".json")
        if out_fp.exists():
            try:
                if json.loads(out_fp.read_text())[-1][0][:10] >= END:
                    skip += 1
                    print(f"[skip] {sym} 已有完整文件", flush=True)
                    continue
            except Exception:
                pass
        bars = []
        page_key = None
        pages = 0
        ok = True
        while True:
            for attempt in range(4):
                ret, df, page_key = ctx.request_history_kline(
                    sym, ktype=KLType.K_1M, start=START, end=END,
                    max_count=1000, page_req_key=page_key, autype=AuType.QFQ)
                if ret == RET_OK:
                    break
                if "频率" in str(df) or "too" in str(df).lower():
                    print(f"  [限频] {sym} 退避32s (attempt {attempt+1})", flush=True)
                    time.sleep(32)
                else:
                    print(f"  [fail] {sym}: {str(df)[:80]}", flush=True)
                    ok = False
                    break
            if not ok:
                break
            if df is not None:
                for _, r in df.iterrows():
                    bars.append([str(r["time_key"])[:19], float(r["open"]), float(r["high"]),
                                 float(r["low"]), float(r["close"]), float(r["volume"])])
            pages += 1
            time.sleep(0.6)
            if page_key is None:
                break
        if ok and bars:
            # 去重 (页间可能重叠) + 排序
            seen = {}
            for b in bars:
                seen[b[0]] = b
            bars = [seen[k] for k in sorted(seen)]
            tmp = out_fp.with_suffix(".tmp")
            tmp.write_text(json.dumps(bars))
            tmp.rename(out_fp)
            done += 1
            print(f"[done] {sym} {len(bars)}根 ({bars[0][0][:10]}~{bars[-1][0][:10]}) "
                  f"{pages}页 累计{(time.time()-t0)/60:.1f}min", flush=True)
        else:
            fail += 1
            print(f"[FAIL] {sym} pages={pages}", flush=True)
finally:
    ctx.close()
print(f"\n完成: done={done} skip={skip} fail={fail} 总耗时{(time.time()-t0)/60:.1f}min")
