#!/usr/bin/env python3
"""盘前指数快照采集器（record-only）。**不接任何门、不写生产库、不被任何交易代码消费**。

为什么存在: DEB-20261106 §P3 的失败原因是"08:35 ET 时手里只有前收盘，没有当日盘前指数"。
本采集器把这个缺口补上 —— 只在盘前记录 QQQ/SMH/SPY/IWM 的 premarket 相对昨收，
产物落 data/shadow/premarket_index/。P3 重开的前提就是它有 ≥N 个冲击日样本。

限制（必须知道）:
  ① **无法回补历史**：券商不提供历史盘前报价，只能从装上之后向前累积。
  ② 券商指数行情订阅只覆盖 QQQ/SPY/IWM/SMH（SMH 日K 拉不到但快照有 pre_price；
     SOXX/XLK 完全无订阅）⇒ 半导体只用 SMH 代理，无板块广度。
  ③ 只在盘前窗口内有意义：窗口外 pre_volume=0 / pre_price 仍是上一个交易日盘前的残值
     ⇒ `session_valid=false`，这类行只证"采集器跑过"，不得当样本。

用法:
  venv/bin/python3.11 scripts/premarket_index_snapshot.py            # 采一次
  venv/bin/python3.11 scripts/premarket_index_snapshot.py --status   # 只看已采样本数
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "data" / "shadow" / "premarket_index"
JSONL = OUT_DIR / "snapshots.jsonl"
SYMBOLS = ["US.QQQ", "US.SMH", "US.SPY", "US.IWM"]
ET = ZoneInfo("America/New_York")
PREMARKET_OPEN = dtime(4, 0)
PREMARKET_CLOSE = dtime(9, 30)


def capture() -> dict:
    from futu import RET_OK, OpenQuoteContext

    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        ret, data = ctx.get_market_snapshot(SYMBOLS)
    finally:
        ctx.close()
    if ret != RET_OK:
        return {"ok": False, "error": str(data)[:300]}

    now_et = datetime.now(ET)
    rows = {}
    for _, r in data.iterrows():
        code = str(r["code"])
        pre_vol = float(r.get("pre_volume") or 0.0)
        rows[code] = {
            "prev_close": float(r.get("prev_close_price") or 0.0),
            "last_price": float(r.get("last_price") or 0.0),
            "pre_price": float(r.get("pre_price") or 0.0),
            "pre_change_val": float(r.get("pre_change_val") or 0.0),
            "pre_change_rate": float(r.get("pre_change_rate") or 0.0),
            "pre_volume": pre_vol,
            "pre_turnover": float(r.get("pre_turnover") or 0.0),
            "update_time": str(r.get("update_time")),
        }

    return _evaluate(rows, now_et)


def _evaluate(rows: dict, now_et: datetime) -> dict:
    """纯判定: 给定快照行与"当前 ET 时刻", 输出是否可当有效样本。不碰网络。"""
    in_window = PREMARKET_OPEN <= now_et.time() < PREMARKET_CLOSE and now_et.weekday() < 5

    # 残值陷阱: 窗口外快照仍带着**上一个交易日盘前**的 pre_price/pre_volume（实测 11-06 00:45 ET
    # 仍显示 11-05 盘前的 -1.215%）。判"今日新鲜"只看 update_time: 必须是今天且 >= 04:00 ET。
    stale = []
    for code, v in rows.items():
        try:
            ut = datetime.strptime(v["update_time"], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=ET)
        except Exception:
            stale.append(code + ":unparsable")
            continue
        if ut.date() != now_et.date() or ut.time() < PREMARKET_OPEN:
            stale.append(f"{code}:{v['update_time']}")
    valid = bool(in_window and not stale and all(v["pre_volume"] > 0 for v in rows.values()))
    rates = [v["pre_change_rate"] for v in rows.values() if v["pre_volume"] > 0]
    return {
        "ok": True,
        "captured_at_et": now_et.isoformat(timespec="seconds"),
        "trade_date_et": now_et.strftime("%Y-%m-%d"),
        "in_premarket_window": in_window,
        "session_valid": valid,
        "stale_symbols": stale,
        "composite_pre_change_rate": round(sum(rates) / len(rates), 4) if rates else None,
        "symbols": rows,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="只统计已采有效样本")
    ap.add_argument("--force", action="store_true",
                    help="窗口外也落盘（仅调试用；默认窗口外静默跳过，避免 DST 双季调度产生噪音行）")
    args = ap.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.status:
        n = nv = 0
        days = set()
        if JSONL.exists():
            for line in JSONL.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except Exception as exc:
                    print(f"[premarket_index_snapshot] 坏行情行跳过: {type(exc).__name__}: {exc}", file=sys.stderr)
                    continue
                n += 1
                if rec.get("session_valid"):
                    nv += 1
                    days.add(rec.get("trade_date_et"))
        print(json.dumps({"records": n, "valid_records": nv, "valid_days": sorted(days)},
                         ensure_ascii=False))
        return 0

    rec = capture()
    if not rec.get("ok"):
        print(json.dumps(rec, ensure_ascii=False), file=sys.stderr)
        return 1
    if not rec["session_valid"] and not args.force:
        # 双季调度会有一半的触发落在窗口外 —— 静默跳过，不污染样本
        print(json.dumps({"skipped": "outside_premarket_window_or_stale",
                          "captured_at_et": rec["captured_at_et"]}, ensure_ascii=False),
              file=sys.stderr)
        return 0
    with JSONL.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    day_fp = OUT_DIR / f"{rec['trade_date_et']}.json"
    if rec["session_valid"]:
        day_fp.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: rec[k] for k in ("captured_at_et", "trade_date_et",
                                          "in_premarket_window", "session_valid",
                                          "composite_pre_change_rate")}, ensure_ascii=False),
          "|", json.dumps({c: v["pre_change_rate"] for c, v in rec["symbols"].items()},
                          ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())