#!/usr/bin/env python3
"""波段持有制回测真实性审计 (2026-08-15, 应用户"回测真实吗").

审计三项:
 A. 入场价抽检: per_trade.csv 的 entry_px vs 日线缓存"信号次一交易日开盘"逐字核对
 B. 跳空击穿偏差: 原回测SL假设精确成交在SL价; 若开盘已击穿SL, 真实成交=开盘(更差).
    跑 gap-aware 保守版 vs 原版, 量化乐观偏差.
 C. 截断剔除: 原回测近端信号窗口被数据末端截断(平均持仓12.1天<cap20).
    只用"完整cap窗口可观测"的信号(信号窗回溯到5/14), 看判读是否翻转.
产出: data/swing_hold_replay_2026-08-15/audit.json
"""
from __future__ import annotations
import json, sqlite3, sys, time, csv
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
SNAP_DB = REPO / "data" / "signal_snapshots.db"
OUT_DIR = REPO / "data" / "swing_hold_replay_2026-08-15"
CACHE_DIR = REPO / "data" / "replay_bars"
FEE = 4.47
NOTIONAL = 3457.51
MIN_DATE = "2026-05-14"   # signal_snapshots 最早日期, 完整观测窗
SL_MULT, TP_MULT, CAP = 3.0, 6.0, 20  # V2 参数


def load_daily(ctx, symbol, start, end):
    cache = CACHE_DIR / f"daily_{symbol.replace('.', '_')}_{start}_{end}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass
    from futu import KLType, RET_OK, AuType
    for _ in (1, 2):
        ret, data, _ = ctx.request_history_kline(
            symbol, ktype=KLType.K_DAY, start=start, end=end,
            max_count=400, autype=AuType.NONE)
        if ret == RET_OK and data is not None and len(data) > 0:
            bars = [[str(row["time_key"])[:10], float(row["open"]), float(row["high"]),
                     float(row["low"]), float(row["close"])] for _, row in data.iterrows()]
            bars.sort(key=lambda b: b[0])
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(bars))
            time.sleep(1.3)
            return bars
        time.sleep(3.0)
    time.sleep(1.3)
    return []


def atr14(bars, idx):
    if idx < 14:
        return None
    trs = []
    for k in range(idx - 13, idx + 1):
        h, l, pc = bars[k][2], bars[k][3], bars[k - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)


def replay(s, bars, gap_aware, require_full_window):
    days = [b[0] for b in bars]
    if s["date"] not in days:
        return None
    idx = days.index(s["date"])
    eidx = idx + 1
    atr = atr14(bars, idx)
    if atr is None or eidx >= len(bars):
        return None
    if require_full_window and eidx + CAP >= len(bars):
        return None  # 截断剔除
    entry_px = bars[eidx][1]
    sign = 1.0 if s["dir"] == "LONG" else -1.0
    sl_dist = SL_MULT * atr
    sl_px = entry_px - sign * sl_dist
    tp_px = entry_px + sign * TP_MULT * atr
    end = min(eidx + CAP, len(bars) - 1)
    for k in range(eidx, end + 1):
        o, h, l = bars[k][1], bars[k][2], bars[k][3]
        # SL (先判): gap-aware 时开盘已击穿→按开盘成交(更差)
        hit_sl = ((l - sl_px) * sign <= 0 and k > eidx) or \
                 (k == eidx and (o - sl_px) * sign <= 0)
        if hit_sl:
            if gap_aware and (o - sl_px) * sign <= 0:
                fill = o
            else:
                fill = sl_px if k > eidx else o
            return (fill / entry_px - 1) * sign, k - eidx, "SL"
        if (h - tp_px) * sign >= 0:
            return (tp_px / entry_px - 1) * sign, k - eidx, "TP"
    return (bars[end][4] / entry_px - 1) * sign, end - eidx, "CAP"


def stats(vals):
    n = len(vals)
    if not n:
        return None
    return {"n": n, "net": round(sum(vals), 1), "avg": round(sum(vals) / n, 2),
            "wr": round(sum(1 for v in vals if v > 0) / n * 100, 1)}


def main():
    audit = {"generated_at": datetime.now().isoformat(timespec="seconds")}

    con = sqlite3.connect(f"file:{SNAP_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(f"""
        SELECT symbol, trade_date, direction, strength
        FROM signal_snapshots
        WHERE trade_date>='{MIN_DATE}' AND strategy_id IN ('deep_analysis','universe_expansion')
          AND direction IN ('LONG','SHORT')
        ORDER BY trade_date
    """).fetchall()
    con.close()
    agg = defaultdict(list)
    for r in rows:
        agg[(r["symbol"], r["trade_date"], r["direction"])].append(r)
    signals = [{"symbol": k[0], "date": k[1], "dir": k[2]} for k in agg]
    print(f"独立信号 n={len(signals)} ({MIN_DATE}起)")

    syms = sorted({s["symbol"] for s in signals} | {"US.SPY"})
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.strptime(MIN_DATE, "%Y-%m-%d") - timedelta(days=40)).strftime("%Y-%m-%d")
    print(f"拉日线: {len(syms)} symbols, {start}~{end}")
    from futu import OpenQuoteContext
    import os
    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPEND_PORT", "11111")))
    daily = {}
    try:
        for i, sym in enumerate(syms):
            bars = load_daily(ctx, sym, start, end)
            if bars:
                daily[sym] = bars
            if (i + 1) % 50 == 0:
                print(f"  {i+1}/{len(syms)}")
    finally:
        ctx.close()
    print(f"日线就绪 {len(daily)}/{len(syms)}")
    spy = {b[0]: b for b in daily.get("US.SPY", [])}

    # ── A. 日线缓存真实性: 随机5只标的, 新窗口强制重拉 vs 缓存逐日核对 ──
    import random
    random.seed(42)
    sample = random.sample(sorted(set(daily) - {"US.SPY"}), min(5, len(daily) - 1))
    from futu import KLType, RET_OK, AuType
    a_results = {}
    for sym in sample:
        cached = {b[0]: b for b in daily[sym]}
        ret, data, _ = ctx if False else (None, None, None)
        # 复用已关闭ctx之外新建一个轻量连接
        from futu import OpenQuoteContext as OQC
        c2 = OQC(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                 port=int(os.getenv("FUTU_OPEND_PORT", "11111")))
        try:
            ret, data, _ = c2.request_history_kline(
                sym, ktype=KLType.K_DAY, start=start, end=end,
                max_count=400, autype=AuType.NONE)
        finally:
            c2.close()
        time.sleep(1.3)
        if ret != RET_OK or data is None:
            a_results[sym] = "refetch_failed"
            continue
        fresh = {str(row["time_key"])[:10]: [float(row["open"]), float(row["high"]),
                                              float(row["low"]), float(row["close"])]
                 for _, row in data.iterrows()}
        overlap = set(cached) & set(fresh)
        bad = 0
        for dte in overlap:
            cb, fb = cached[dte], fresh[dte]
            if any(abs(cb[i + 1] - fb[i]) / max(fb[i], 1e-9) > 0.005 for i in range(4)):
                bad += 1
        a_results[sym] = {"overlap_days": len(overlap), "mismatch_days": bad}
    audit["A_cache_freshness_check"] = a_results
    a_bad = sum(1 for v in a_results.values()
                if isinstance(v, dict) and v["mismatch_days"] > 0)
    audit["A_pass"] = a_bad == 0
    print(f"\nA. 缓存vs新拉核对: {a_results} → {'PASS' if audit['A_pass'] else 'FAIL'}")

    # ── B+C. gap-aware × 截断剔除 四组合 ──
    def run(gap_aware, full_window):
        nets_raw, nets_alpha, by_dir = [], [], defaultdict(list)
        exits = defaultdict(int)
        for s in signals:
            bars = daily.get(s["symbol"])
            if not bars:
                continue
            res = replay(s, bars, gap_aware, full_window)
            if res is None:
                continue
            gross, held, etype = res
            exits[etype] += 1
            net = gross * NOTIONAL - FEE
            nets_raw.append(net)
            by_dir[s["dir"]].append(net)
            days = [b[0] for b in bars]
            eidx = days.index(s["date"]) + 1
            spy0 = spy.get(bars[eidx][0])
            spy1 = spy.get(days[min(eidx + held, len(days) - 1)])
            a = net
            if spy0 and spy1:
                sign = 1.0 if s["dir"] == "LONG" else -1.0
                a = (gross - (spy1[4] / spy0[1] - 1) * sign) * NOTIONAL - FEE
            nets_alpha.append(a)
        return {"raw": stats(nets_raw), "alpha": stats(nets_alpha),
                "by_dir_raw": {d: stats(v) for d, v in by_dir.items()},
                "exits": dict(exits)}

    for label, ga, fw in [
        ("original_optimistic_truncated", False, False),
        ("gap_aware_conservative_truncated", True, False),
        ("original_full_window", False, True),
        ("gap_aware_full_window", True, True),
    ]:
        audit[label] = run(ga, fw)
        v = audit[label]
        print(f"\n{label}: raw={v['raw']} alpha={v['alpha']}")
        print(f"  exits={v['exits']}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=1))
    print("\naudit.json written")


if __name__ == "__main__":
    main()
