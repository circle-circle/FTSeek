#!/usr/bin/env python3
"""方案1 动量EOD臂 存量重放 (DEB-20260815 §18.2/§19; 2026-08-15 用户批准豁免).

信号体: signal_snapshots 方向化信号中 factors_json 含动量标签
  (momentum/rsi/overbought/oversold/breakout/earnings_beat, 镜像
   playbook_service.py:1350 f2_momentum 定义), 按 (symbol,trade_date,direction) 去重.
口径 (预注册, §18.3 + §19 修正2):
  - 入场 = 信号日的【次一交易日开盘价】(保守口径, 不含信号日当日漂移)
  - 退出变体: H1=入场当日尾盘收盘(≈PRE_CLOSE_EXIT) / H2=T+2尾盘 / H3=T+3尾盘
              B=EOD止损: 任一持仓日收盘<入场×0.97 → 次日开盘平 (继承E2变体B)
  - 费率 $4.47/笔 (7/1+ 实测), 名义本金 $3457.51
  - alpha = 毛收益 - SPY同期收益; 判读只看 alpha
  - 双窗: 按日期中位数切前后窗, 同号才通过
  - 费率占比 = Σ费 / Σ|毛盈亏|, >25% 判败
  - 通过: alpha净均笔>0 AND n≥30 AND 双窗同号 AND 费率占比≤25%
产出: data/momentum_eod_replay_2026-08-15/summary.json + per_trade.csv
"""
from __future__ import annotations
import json, sqlite3, sys, time, csv
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SNAP_DB = REPO / "data" / "signal_snapshots.db"
OUT_DIR = REPO / "data" / "momentum_eod_replay_2026-08-15"
CACHE_DIR = REPO / "data" / "replay_bars"
FEE = 4.47
NOTIONAL = 3457.51
EOD_SL = 0.03
HOLDS = (1, 2, 3)          # H_k: 入场当日(T+1)起第 k 日尾盘收盘
MOM_KEYS = ("momentum", "rsi", "overbought", "oversold", "breakout", "earnings_beat")


def is_momentum(fj_raw):
    if not fj_raw:
        return False
    try:
        tags = json.loads(fj_raw)
    except Exception:
        return False
    if not isinstance(tags, list):
        return False
    return any(any(k in t.lower() for k in MOM_KEYS)
               for t in tags if isinstance(t, str))


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


def stats(sub, key):
    n = len(sub)
    if not n:
        return None
    net = sum(r[key] for r in sub)
    w = sum(1 for r in sub if r[key] > 0)
    return {"n": n, "net": round(net, 1), "avg": round(net / n, 2),
            "wr": round(w / n * 100, 1)}


def main():
    # ── 信号体 ──
    con = sqlite3.connect(f"file:{SNAP_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT symbol, trade_date, direction, strength, factors_json, strategy_id
        FROM signal_snapshots
        WHERE direction IN ('LONG','SHORT') AND factors_json IS NOT NULL
        ORDER BY trade_date
    """).fetchall()
    con.close()

    agg = defaultdict(list)
    for r in rows:
        if not is_momentum(r["factors_json"]):
            continue
        agg[(r["symbol"], r["trade_date"], r["direction"])].append(r)
    signals = []
    for (sym, d, direction), grp in agg.items():
        signals.append({"symbol": sym, "date": d, "dir": direction,
                        "strength": sum(x["strength"] for x in grp) / len(grp),
                        "n_signals": len(grp),
                        "da_only": all((x["strategy_id"] or "") == "deep_analysis"
                                       for x in grp)})
    print(f"动量信号行 {len(rows)} → 独立信号 {len(signals)} (含DA子集)")

    # ── 日线 ──
    syms = sorted({s["symbol"] for s in signals} | {"US.SPY"})
    min_d = min(s["date"] for s in signals)
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.strptime(min_d, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
    print(f"拉日线: {len(syms)} symbols, {start}~{end} (缓存优先)")
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
                print(f"  {i+1}/{len(syms)} fetched")
    finally:
        ctx.close()
    print(f"日线就绪: {len(daily)}/{len(syms)}")

    spy = {b[0]: b for b in daily.get("US.SPY", [])}

    # ── 重放 ──
    results, excluded = [], []
    for s in signals:
        bars = daily.get(s["symbol"])
        if not bars:
            excluded.append(f"{s['symbol']} {s['date']} no_bars")
            continue
        days = [b[0] for b in bars]
        if s["date"] not in days:
            excluded.append(f"{s['symbol']} {s['date']} sig_date_missing")
            continue
        idx = days.index(s["date"])
        eidx = idx + 1                       # 次一交易日
        need = eidx + max(HOLDS)
        if need >= len(bars):
            excluded.append(f"{s['symbol']} {s['date']} window_truncated")
            continue
        entry_bar = bars[eidx]
        entry_px, entry_day = entry_bar[1], bars[eidx][0]
        sign = 1.0 if s["dir"] == "LONG" else -1.0
        # SPY 同期基准: 入场日开盘 → 各退出点
        spy0 = spy.get(entry_day)
        if not spy0:
            excluded.append(f"{s['symbol']} {s['date']} no_spy")
            continue
        rec = {"symbol": s["symbol"], "sig_date": s["date"], "dir": s["dir"],
               "strength": round(s["strength"], 1), "da_only": s["da_only"],
               "entry_day": entry_day, "entry_px": round(entry_px, 4)}
        ok = True
        for h in HOLDS:
            exit_bar = bars[eidx + h - 1]
            exit_px = exit_bar[4]            # 尾盘≈收盘
            gross = (exit_px / entry_px - 1) * sign
            net = gross * NOTIONAL - FEE
            sp1 = spy.get(exit_bar[0])
            if not sp1:
                excluded.append(f"{s['symbol']} {s['date']} no_spy_h{h}")
                ok = False
                break
            spy_ret = (sp1[4] / spy0[1] - 1) * sign
            rec[f"gross_h{h}"] = round(gross * 100, 4)
            rec[f"net_h{h}"] = round(net, 2)
            rec[f"alpha_h{h}"] = round((gross - spy_ret) * NOTIONAL, 2)
        if not ok:
            continue
        # 变体B: EOD止损 -3% (基于入场价), 最长持 H3
        net_b, exit_day_b = rec[f"net_h{HOLDS[-1]}"], bars[eidx + HOLDS[-1] - 1][0]
        gross_b = rec[f"gross_h{HOLDS[-1]}"] / 100.0
        for k in range(eidx + 1, eidx + HOLDS[-1] + 1):
            if k >= len(bars):
                break
            close_k = bars[k][4]
            if (close_k / entry_px - 1) * sign <= -EOD_SL:
                if k + 1 < len(bars):
                    exit_px_b = bars[k + 1][1]
                    exit_day_b = bars[k + 1][0]
                else:
                    exit_px_b = close_k
                    exit_day_b = bars[k][0]
                gross_b = (exit_px_b / entry_px - 1) * sign
                net_b = gross_b * NOTIONAL - FEE
                break
        sp1b = spy.get(exit_day_b)
        alpha_b = None
        if sp1b:
            alpha_b = round((gross_b - (sp1b[4] / spy0[1] - 1) * sign) * NOTIONAL, 2)
        rec["net_b"] = round(net_b, 2)
        rec["alpha_b"] = alpha_b
        results.append(rec)

    print(f"重放成功 {len(results)}, 剔出 {len(excluded)} (样例 {excluded[:5]})")

    # ── 判读 ──
    def judge(sub, alpha_key, raw_key):
        st = stats(sub, alpha_key)
        if not st or st["n"] < 30:
            return st, {"verdict": "FAIL_n", "reason": f"n={st['n'] if st else 0}<30"}
        # 双窗同号 (按 sig_date 中位数)
        dates = sorted(r["sig_date"] for r in sub)
        mid = dates[len(dates) // 2]
        w1 = stats([r for r in sub if r["sig_date"] <= mid], alpha_key)
        w2 = stats([r for r in sub if r["sig_date"] > mid], alpha_key)
        same_sign = w1 and w2 and (w1["avg"] > 0) == (w2["avg"] > 0)
        # 费率占比 (口径修正2026-08-15: gross美元 = alpha + FEE; 原实现误用百分比字段)
        gross_abs = sum(abs(r[alpha_key] + FEE) for r in sub)
        fee_ratio = (FEE * st["n"] / gross_abs * 100) if gross_abs else 999
        pass_ = st["avg"] > 0 and same_sign and fee_ratio <= 25
        return st, {"verdict": "PASS" if pass_ else "FAIL",
                    "w1_avg": w1["avg"] if w1 else None, "w2_avg": w2["avg"] if w2 else None,
                    "same_sign": same_sign, "fee_ratio_pct": round(fee_ratio, 1)}

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "preregistered": "entry=T+1开盘; alpha=毛-SPY同期; 通过=alpha均笔>0∧n≥30∧双窗同号∧费率占比≤25%",
        "fee": FEE, "notional": NOTIONAL,
        "n_signals": len(signals), "n_replayed": len(results),
        "n_excluded": len(excluded), "excluded_sample": excluded[:10],
        "variants": {}, "variants_da_only": {},
    }
    for label, sub in (("all", results),
                       ("da_only", [r for r in results if r["da_only"]])):
        for h in HOLDS:
            st, j = judge(sub, f"alpha_h{h}", f"gross_h{h}")
            out["variants" if label == "all" else "variants_da_only"][f"H{h}"] = {**st, **j}
        st, j = judge(sub, "alpha_b", None)
        if st:
            gross_abs = sum(abs(r.get("net_b", 0) + FEE) for r in sub)
            j["fee_ratio_pct"] = round(FEE * st["n"] / gross_abs * 100, 1) if gross_abs else None
        out["variants" if label == "all" else "variants_da_only"]["B_eod_sl3"] = {**st, **j}

    # 方向拆分 (主变体 H1)
    out["by_dir_H1"] = {d: stats([r for r in results if r["dir"] == d], "alpha_h1")
                        for d in ("LONG", "SHORT")}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "summary.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    with open(OUT_DIR / "per_trade.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()) if results else [])
        w.writeheader()
        for r in results:
            w.writerow(r)

    print("\n=== 方案1 动量EOD重放判读 (alpha口径) ===")
    for grp in ("variants", "variants_da_only"):
        print(f"\n[{grp}]")
        for k, v in out[grp].items():
            print(f"  {k:10s} n={v.get('n')} 净alpha={v.get('net')} 均笔={v.get('avg')} "
                  f"WR={v.get('wr')}% | {v.get('verdict')} w1={v.get('w1_avg')} w2={v.get('w2_avg')} "
                  f"同号={v.get('same_sign')} 费率占比={v.get('fee_ratio_pct')}%")
    print(f"\n方向拆分(H1): {out['by_dir_H1']}")


if __name__ == "__main__":
    main()
