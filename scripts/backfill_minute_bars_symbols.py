#!/usr/bin/env python3
"""按指定标的补 1 分钟 K 到独立 replay 缓存 (2026-10-18).

用途: entry_second_chance_replay 的缺失 bar 补齐。写到
`data/replay_bars_1m_extra/`, 不碰生产 `data/replay_bars_1m/` (该目录只有
45 票全历史文件, 混入 5 日碎片会让其他消费方误读)。

口径与 `fetch_minute_bars.py` 一致: Futu K_1M, AuType.QFQ, [time,o,h,l,c,v]。
纯数据补齐, 不写交易库、不下单、不 import 交易路径。
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from futu import AuType, KLType, OpenQuoteContext, RET_OK

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "replay_bars_1m_extra"


def _fetch(ctx, code: str, start: str, end: str) -> list[list]:
    bars: list[list] = []
    page_key = None
    while True:
        for attempt in range(4):
            ret, df, page_key = ctx.request_history_kline(
                code, ktype=KLType.K_1M, start=start, end=end,
                max_count=1000, page_req_key=page_key, autype=AuType.QFQ)
            if ret == RET_OK:
                break
            text = str(df)
            if "频率" in text or "too" in text.lower():
                print(f"  [限频] {code} 退避32s ({attempt + 1}/4)", flush=True)
                time.sleep(32)
                continue
            raise RuntimeError(f"{code}: {text[:200]}")
        if df is not None:
            for _, row in df.iterrows():
                bars.append([
                    str(row["time_key"])[:19], float(row["open"]), float(row["high"]),
                    float(row["low"]), float(row["close"]), float(row["volume"]),
                ])
        time.sleep(0.6)
        if page_key is None:
            break
    return bars


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True, help="逗号分隔, 如 ACN,AEM,AMD")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    args = ap.parse_args()

    symbols = [s.strip().upper().replace("US.", "")
               for s in args.symbols.split(",") if s.strip()]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        for sym in symbols:
            code = f"US.{sym}"
            path = OUT_DIR / f"US_{sym}.json"
            try:
                new_bars = _fetch(ctx, code, args.start, args.end)
            except Exception as exc:
                print(f"[fail] {code}: {exc}", flush=True)
                continue
            if not new_bars:
                print(f"[skip] {code}: 无数据", flush=True)
                continue
            old: list[list] = []
            if path.exists():
                try:
                    old = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    old = []
            merged = {str(b[0]): b for b in old}
            merged.update({str(b[0]): b for b in new_bars})
            out = [merged[k] for k in sorted(merged)]
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(out), encoding="utf-8")
            tmp.rename(path)
            print(f"[done] {code} +{len(new_bars)} → {len(out)} ({args.start}~{args.end})",
                  flush=True)
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
