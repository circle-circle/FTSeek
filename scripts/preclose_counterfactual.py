#!/usr/bin/env python3
"""PRE_CLOSE VWAP 反事实尺子 (DEB-20260804 任务#3, 只读).

用途:
  1) 对 MFE_STALL_EXIT 历史样本, 验证"持到收盘"反事实 (日K收盘价) 是否虚高 —
     PRE_CLOSE 窗口 (15:52-16:00 ET) 的真实可成交价用 1-min K 成交量加权 VWAP 估计;
  2) 给 P2 (stall 浅亏持有→PRE_CLOSE 收尾) 观察期提供尺子: 每周五用同一函数
     复核浅亏持有仓实际成交价 vs VWAP 估计.

数据源: Futu request_history_kline K_1M (含 volume), autype=NONE 与回放口径一致.
缓存: data/preclose_cf_cache/{SYM}_{date}.json (含 volume, 与 replay_bars 的无成交量
缓存分离). 配额保护 ≤25次/30s → 每笔抓取 sleep 1.2s.

输出: data/preclose_counterfactual_latest.json + stdout 判决表.
"""
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv
load_dotenv(".env.us")

DB = ROOT / "data" / "ftseek_us.db"
CACHE_DIR = ROOT / "data" / "preclose_cf_cache"
OUT = ROOT / "data" / "preclose_counterfactual_latest.json"

WINDOW_START = "15:52:00"  # ET, PRE_CLOSE 退出窗口 (含)
WINDOW_END = "15:59:59"    # ET (含; 15:59 bar 覆盖 15:59-16:00)


def dir_sign(d: str) -> float:
    return -1.0 if str(d).upper() in ("SHORT", "SELL_SHORT") else 1.0


def fetch_1m_with_volume(ctx, symbol: str, day: str):
    """返回 [{t,o,h,l,c,v}] 升序; 缓存优先; 无数据 None."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cp = CACHE_DIR / f"{symbol.replace('.', '_')}_{day}.json"
    if cp.exists():
        try:
            return json.loads(cp.read_text())
        except Exception:
            pass
    from futu import KLType, RET_OK, AuType
    data = None
    for attempt in (1, 2):
        ret, data, _ = ctx.request_history_kline(
            symbol, ktype=KLType.K_1M, start=day, end=day,
            max_count=1000, autype=AuType.NONE)
        if ret == RET_OK and data is not None and len(data) > 0:
            break
        if attempt == 1:
            time.sleep(3.0)
    if data is None or len(data) == 0:
        time.sleep(1.2)
        return None
    bars = []
    for _, row in data.iterrows():
        bars.append({
            "t": str(row.get("time_key", ""))[:19],
            "o": float(row["open"]), "h": float(row["high"]),
            "l": float(row["low"]), "c": float(row["close"]),
            "v": float(row.get("volume", 0) or 0),
        })
    bars.sort(key=lambda b: b["t"])
    cp.write_text(json.dumps(bars))
    time.sleep(1.2)  # kline 配额保护
    return bars


def preclose_window_stats(bars):
    """15:52-16:00 ET 窗口: VWAP(典型价×量) + 末bar收盘(≈日K收) + 窗口极值."""
    win = [b for b in bars if WINDOW_START <= b["t"][11:19] <= WINDOW_END]
    if not win:
        return None
    tot_v = sum(b["v"] for b in win)
    if tot_v > 0:
        vwap = sum(((b["h"] + b["l"] + b["c"]) / 3.0) * b["v"] for b in win) / tot_v
    else:
        vwap = sum(b["c"] for b in win) / len(win)  # 无量降级: 简单均价
    last_close = win[-1]["c"]
    return {
        "vwap": round(vwap, 4),
        "last_close": round(last_close, 4),
        "win_lo": min(b["l"] for b in win),
        "win_hi": max(b["h"] for b in win),
        "win_bars": len(win),
        "win_volume": tot_v,
    }


def main():
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    trades = con.execute(
        "SELECT id, trade_date, symbol, direction, quantity, entry_price, exit_price, "
        "realized_pnl, exit_time FROM trade_records "
        "WHERE exit_reason='MFE_STALL_EXIT' ORDER BY trade_date"
    ).fetchall()
    con.close()
    print(f"MFE_STALL_EXIT 样本: {len(trades)} 笔")

    import futu.common.ft_logger as _ftl
    import logging
    _ftl.logger.file_level = logging.WARNING
    _ftl.logger.console_level = logging.WARNING
    from futu import OpenQuoteContext, SysConfig
    SysConfig.enable_proto_encrypt(False)
    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPEND_PORT", "11111")))

    rows = []
    try:
        for tr in trades:
            s = dir_sign(tr["direction"])
            qty = tr["quantity"] or 0
            entry = tr["entry_price"] or 0.0
            bars = fetch_1m_with_volume(ctx, tr["symbol"], tr["trade_date"])
            rec = {
                "id": tr["id"], "date": tr["trade_date"], "symbol": tr["symbol"],
                "direction": tr["direction"], "entry": entry, "exit": tr["exit_price"],
                "pnl_actual": round(tr["realized_pnl"] or 0, 2),
                "exit_hm": str(tr["exit_time"])[11:16] if tr["exit_time"] else "?",
            }
            if not bars:
                rec["error"] = "no_bars"
                rows.append(rec)
                continue
            stats = preclose_window_stats(bars)
            day_close = bars[-1]["c"]  # 1-min 末bar ≈ 日K收盘
            if not stats:
                rec["error"] = "no_window_bars"
                rows.append(rec)
                continue
            cf_vwap = round(s * (stats["vwap"] - entry) * qty, 2)
            cf_close = round(s * (day_close - entry) * qty, 2)
            rec.update({
                "cf_vwap": cf_vwap,
                "cf_day_close": cf_close,
                "vwap": stats["vwap"],
                "day_close": round(day_close, 4),
                "vwap_vs_close_bps": round((stats["vwap"] / day_close - 1) * 10000, 2)
                if day_close else None,
            })
            rows.append(rec)
    finally:
        ctx.close()

    ok = [r for r in rows if "cf_vwap" in r]
    print(f"有效样本: {len(ok)}/{len(rows)}")
    print(f"\n{'date':<10} {'sym':<9} {'dir':<5} {'退@':<6} {'实际':>9} {'VWAP反事实':>10} "
          f"{'日K收反事实':>11} {'VWAP-收':>8} {'VWAP-实际':>9}")
    print("-" * 92)
    for r in ok:
        print(f"{r['date']:<10} {r['symbol']:<9} {r['direction'][:4]:<5} {r['exit_hm']:<6} "
              f"{r['pnl_actual']:>9.2f} {r['cf_vwap']:>10.2f} {r['cf_day_close']:>11.2f} "
              f"{r['cf_vwap'] - r['cf_day_close']:>+8.2f} {r['cf_vwap'] - r['pnl_actual']:>+9.2f}")

    def agg(key):
        vals = [r[key] for r in ok]
        real = [r["pnl_actual"] for r in ok]
        diff = [c - p for c, p in zip(vals, real)]
        better = sum(1 for d in diff if d > 0.01)
        return {"n": len(vals), "sum": round(sum(vals), 2),
                "actual_sum": round(sum(real), 2), "delta": round(sum(diff), 2),
                "better_count": better}

    a_vwap, a_close = agg("cf_vwap"), agg("cf_day_close")
    gap = round(a_close["sum"] - a_vwap["sum"], 2)
    print(f"\n=== 汇总 ===")
    print(f"实际:                {a_vwap['actual_sum']:+.2f}")
    print(f"反事实@日K收盘(旧):  {a_close['sum']:+.2f} (优于实际 {a_close['better_count']}笔)")
    print(f"反事实@PRE_CLOSE VWAP: {a_vwap['sum']:+.2f} (优于实际 {a_vwap['better_count']}笔)")
    print(f"日K收盘高估额 (旧-新): {gap:+.2f}")
    bps = [r["vwap_vs_close_bps"] for r in ok if r.get("vwap_vs_close_bps") is not None]
    if bps:
        print(f"VWAP vs 日K收盘: 均值 {sum(bps)/len(bps):+.2f}bps, "
              f"区间 [{min(bps):+.2f}, {max(bps):+.2f}]bps")

    out = {"generated_at": datetime.now().isoformat(),
           "window": f"{WINDOW_START[:5]}-{WINDOW_END[:5]} ET",
           "summary": {"vwap_counterfactual": a_vwap, "day_close_counterfactual": a_close,
                       "day_close_overstate": gap},
           "rows": rows}
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n已保存 {OUT}")


if __name__ == "__main__":
    main()
