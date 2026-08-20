#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""增量拉取单日 1分K 并合并进 data/replay_bars_1m/{sym}.json.

用法: PYTHONPATH=. venv/bin/python scripts/fetch_minute_bars_incremental.py [YYYY-MM-DD]
默认当天 (ET). 只请求指定日期 (≤390根/票, 单页), 与已有文件去重合并后原子写回.
供影子结算/回放补当日数据用; 全量回补仍用 fetch_minute_bars.py.
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT_DIR = ROOT / "data" / "replay_bars_1m"

from futu import OpenQuoteContext, RET_OK, KLType, AuType  # noqa: E402

DAY = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")

ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
done = skip = fail = 0
try:
    for fp in sorted(OUT_DIR.glob("US_*.json")):
        sym = fp.stem.replace("_", ".")
        bars = []
        page_key = None
        ok = True
        while True:
            ret, df, page_key = ctx.request_history_kline(
                sym, ktype=KLType.K_1M, start=DAY, end=DAY,
                max_count=1000, page_req_key=page_key, autype=AuType.QFQ)
            if ret == RET_OK:
                if df is not None:
                    for _, r in df.iterrows():
                        bars.append([str(r["time_key"])[:19], float(r["open"]), float(r["high"]),
                                     float(r["low"]), float(r["close"]), float(r["volume"])])
                time.sleep(0.6)
                if page_key is None:
                    break
                continue
            if "频率" in str(df) or "too" in str(df).lower():
                print(f"  [限频] {sym} 退避32s", flush=True)
                time.sleep(32)
                continue
            print(f"  [fail] {sym}: {str(df)[:80]}", flush=True)
            ok = False
            break
        if not ok:
            fail += 1
            continue
        if not bars:
            skip += 1
            print(f"[skip] {sym} {DAY} 无数据", flush=True)
            continue
        try:
            old = json.loads(fp.read_text())
        except Exception:
            old = []
        seen = {b[0]: b for b in old}
        for b in bars:
            seen[b[0]] = b
        merged = [seen[k] for k in sorted(seen)]
        tmp = fp.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged))
        tmp.rename(fp)
        done += 1
        print(f"[done] {sym} +{len(bars)}根 → 共{len(merged)} (末根 {merged[-1][0]})", flush=True)
finally:
    ctx.close()
print(f"\n完成: done={done} skip={skip} fail={fail}")
