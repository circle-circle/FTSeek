#!/usr/bin/env python3
"""延长时段**候选源回放** (2026-09-17, 用户令「需要」→ 先出数再开新源).

问题: 新源(财报超预期榜 / 自选股)值不值得开? 用户 8/14 偏好 —— 历史可回放就别默认攒样。
能力(实测): `request_history_kline(ktype=K_5M, extended_time=True)` 能取到**历史盘前 bar**
  (旧默认 extended_time=False 只有 RTH ⇒ 0 根盘前, 这是此前"只能前向攒样"的根因)。

口径与限制 (如实声明):
  1. `autype=None` 取**未复权**原始价 —— 否则 qfq 复权会让"昨收"与被测日的价格不同源。
  2. 历史盘前 bar 只到 5m 粒度 ⇒ 入场价用**入场时刻那根 5m bar 的收盘价**近似 (实际是限价单成交价)。
  3. 候选重建: 财报超预期榜只返回**当前**榜单带 released_date ⇒ 只能重建其窗口内的日期 (实测约 90 日);
     自选股是**当前**名单 ⇒ 存在选择性偏差 (当时的名单可能不同)。二者都属"代理回放", 不假装是逐日快照。
  4. 退出规则**直接复用** premarket_exit_shadow.decide() (同一套阈值), 不另写一套。
  5. 不做滑点建模: 薄量盘前的真实滑点未知 (DEB §15 已记为切实盘前必须补的项)。

用法:
  PYTHONPATH=. venv/bin/python3.11 scripts/replay_ext_entry_sources.py --days 10 [--source beat|watchlist|both]
产物: data/reports/replay_ext_entry_sources_latest.json + stdout 汇总表
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env.us")
except Exception:
    pass

REPORT = ROOT / "data" / "reports" / "replay_ext_entry_sources_latest.json"
FEE_PCT = 0.085          # 往返费用 (用户口径: 7.7bps~$4.27/笔$5000 ⇒ ~0.085%)
KLINE_MIN_INTERVAL = 0.35
ENTRY_HHMM = 405         # 入场时点 04:05 ET (与 cron 盘前首跑一致)
PREMARKET_END = 925      # 盘前收口 09:25 ET
_last_call = [0.0]


def _kline(ctx, code, start, end, ktype):
    """限频的历史K线请求 (富途约 60 次/30 秒预算, 见 DEB §14.6 教训)."""
    from futu import RET_OK
    wait = KLINE_MIN_INTERVAL - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()
    ret, kl, msg = ctx.request_history_kline(code, start=start, end=end, ktype=ktype,
                                             autype=None, max_count=1000, extended_time=True)
    if ret != RET_OK or kl is None or len(kl) == 0:
        return []
    out = []
    for _, r in kl.iterrows():
        out.append({"t": str(r["time_key"]), "o": float(r["open"]), "h": float(r["high"]),
                    "l": float(r["low"]), "c": float(r["close"]), "v": float(r["volume"])})
    return out


def _hhmm(t):
    try:
        return int(t[11:13]) * 100 + int(t[14:16])
    except Exception:
        return -1


def _candidate_days(rank_df, day, recent_days):
    """财报超预期榜 → 该日可见的候选 (released_date ∈ [day-recent_days, day])."""
    lo = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=recent_days)).strftime("%Y-%m-%d")
    out = []
    for _, r in rank_df.iterrows():
        sec = str(r.get("security", ""))
        if not sec.startswith("US."):
            continue
        rd = str(r.get("released_date", ""))
        if lo <= rd <= day:
            try:
                if float(r.get("market_cap", 0) or 0) < 1e9:
                    continue
            except Exception:
                continue
            out.append((sec, rd))
    return out


def _simulate(bars_day, prev_close, entry_bar_i, decide):
    """在当日 bar 序列上模拟: 入场 → 三条退出策略. 返回 dict."""
    entry = bars_day[entry_bar_i]["c"]
    res = {}
    pre = [b for b in bars_day if _hhmm(b["t"]) < 930]
    rth = [b for b in bars_day if 930 <= _hhmm(b["t"]) <= 1600]
    # A) 我们的规则: 逐 bar 调 decide() (与盘前影子同一套阈值), 触发即平
    act = "hold"
    exit_px, exit_t = None, None
    # 盘前**累计**量 (与影子 decide() 的 pre_volume 口径一致)
    cum, run = {}, 0.0
    for b in bars_day:
        run += float(b["v"])
        cum[b["t"]] = run
    for b in bars_day:
        if b["t"] <= bars_day[entry_bar_i]["t"]:
            continue
        pnl = (b["c"] / entry - 1) * 100
        a = decide(round(pnl, 3), cum.get(b["t"], 0.0))
        if a in ("lock_in", "breakeven_lock", "tighten"):
            act, exit_px, exit_t = a, b["c"], b["t"]
            break
    res["rule_action"] = act
    res["rule_exit_time"] = exit_t
    # B) 持有到盘前收口 (09:25)
    e2 = pre[-1]["c"] if pre else None
    # C) 持有到当日 RTH 收盘
    e3 = rth[-1]["c"] if rth else (bars_day[-1]["c"] if bars_day else None)
    for tag, px in (("rule", exit_px if exit_px else (e2 if e2 else entry)),
                    ("pre_close", e2), ("rth_close", e3)):
        if px:
            g = (px / entry - 1) * 100
            res[tag] = {"ret_pct": round(g, 3), "net_pct": round(g - FEE_PCT, 3)}
    # MFE (入场后至盘前收口的最高冲高)
    if pre:
        peak = max(b["h"] for b in pre if b["t"] > bars_day[entry_bar_i]["t"]) if len(pre) > entry_bar_i else None
        res["mfe_pct"] = round((peak / entry - 1) * 100, 3) if peak else None
    res["entry_px"] = round(entry, 4)
    res["prev_close"] = round(prev_close, 4)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10, help="回看交易日数")
    ap.add_argument("--source", default="both", choices=["beat", "watchlist", "both"])
    ap.add_argument("--recent-days", type=int, default=7, help="财报披露近因窗口")
    ap.add_argument("--min-chg", type=float, default=None, help="覆盖涨幅门槛(默认用现役 2.0)")
    ap.add_argument("--min-vol", type=float, default=None, help="覆盖量门槛(默认用现役 200000)")
    args = ap.parse_args()

    import scripts.extended_hours_entry_executor as E
    from scripts.premarket_exit_shadow import decide
    from futu import OpenQuoteContext, KLType, BeatType

    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPD_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPD_PORT", "11111")))
    try:
        rank_df = None
        if args.source in ("beat", "both"):
            r = ctx.get_earnings_beat_rank("US", BeatType.EPS, count=200)
            rank_df = r[1][1] if r[0] == 0 and isinstance(r[1], tuple) else None
        watch = []
        if args.source in ("watchlist", "both"):
            watch = E._watchlist_symbols(ctx)

        # 交易日序列: 以宽基 SPY 的日线为准 (最近 N+1 个交易日)
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=args.days * 2 + 15)).strftime("%Y-%m-%d")
        days_bars = _kline(ctx, "US.SPY", start, end, KLType.K_DAY)
        days = [b["t"][:10] for b in days_bars][-(args.days + 1):]
        print(f"[replay] 交易日窗口: {days[0]} → {days[-1]} ({len(days)} 日); 源={args.source} "
              f"(beat={len(rank_df) if rank_df is not None else 0} 行, watchlist={len(watch)} 只)")

        # 全窗口一次性预取 (原逐日取数 = days×symbols 次调用, 会被限频; 见 DEB §14.6)
        universe = {}
        for di, day in enumerate(days[1:], start=1):
            if rank_df is not None:
                for sec, rd in _candidate_days(rank_df, day, args.recent_days):
                    universe.setdefault(sec, f"beat:{rd}")
            for sec in watch:
                universe.setdefault(sec, "watchlist")
        print(f"[replay] 候选宇宙 {len(universe)} 只 → 预取 5m 全窗口 bars …")
        bars_by_sym = {}
        for sec in universe:
            bars_by_sym[sec] = _kline(ctx, sec, days[0], days[-1], KLType.K_5M)
        rows = []
        for di, day in enumerate(days[1:], start=1):
            prev_day = days[di - 1]
            for sec, src in universe.items():
                if rank_df is not None:
                    # 事件源按日重建: 只有 released_date 落在该日近因窗内才算当日候选
                    cand_days = {s2 for s2, _ in _candidate_days(rank_df, day, args.recent_days)}
                    if src.startswith("beat") and sec not in cand_days:
                        continue
                    if not src.startswith("beat") and args.source == "beat":
                        continue
                bars = bars_by_sym.get(sec) or []
                if not bars:
                    continue
                pc = [b for b in bars if b["t"][:10] == prev_day and 930 <= _hhmm(b["t"]) <= 1600]
                if not pc:
                    continue
                prev_close = pc[-1]["c"]
                today = [b for b in bars if b["t"][:10] == day]
                pre = [b for b in today if _hhmm(b["t"]) <= PREMARKET_END]
                if not pre:
                    continue
                idxs = [i for i, b in enumerate(today) if _hhmm(b["t"]) <= ENTRY_HHMM]
                if not idxs:
                    continue
                ei = idxs[-1]
                entry_px = today[ei]["c"]
                chg = (entry_px / prev_close - 1) * 100 if prev_close > 0 else 0
                vol = sum(b["v"] for b in today[:ei + 1])
                min_chg = args.min_chg if args.min_chg is not None else E.ENTRY_CHANGE_PCT
                min_vol = args.min_vol if args.min_vol is not None else E.MIN_VOLUME
                passed = E._price_in_range(entry_px) and chg >= min_chg and vol >= min_vol
                rec = {"day": day, "symbol": sec, "source": src, "entry_px": round(entry_px, 4),
                       "prev_close": round(prev_close, 4), "chg_pct": round(chg, 2),
                       "pre_vol": vol, "gates_passed": bool(passed)}
                if passed:
                    try:
                        rec.update(_simulate(today, prev_close, ei, decide))
                    except Exception as exc:
                        rec["sim_error"] = str(exc)[:80]
                rows.append(rec)

        passed = [r for r in rows if r["gates_passed"] and "ret_pct" in r.get("rule", {})]
        summary = {"generated_at": datetime.utcnow().isoformat() + "Z", "days": len(days) - 1,
                   "source": args.source, "min_chg": args.min_chg, "min_vol": args.min_vol,
                   "candidates_evaluated": len(rows),
                   "gates_passed": len(passed)}
        if passed:
            for tag, label in (("rule", "我们的规则(盘前 decide 触发即平)"),
                               ("pre_close", "持有到盘前收口 09:25"),
                               ("rth_close", "持有到当日收盘 16:00")):
                rr = [r[tag] for r in passed if tag in r]
                if not rr:
                    continue
                gross = sum(x["ret_pct"] for x in rr) / len(rr)
                net = sum(x["net_pct"] for x in rr) / len(rr)
                wr = sum(1 for x in rr if x["ret_pct"] > 0) / len(rr) * 100
                summary[tag] = {"n": len(rr), "avg_gross_pct": round(gross, 3),
                                "avg_net_pct": round(net, 3), "win_rate_pct": round(wr, 1)}
        mfe = [r["mfe_pct"] for r in passed if r.get("mfe_pct") is not None]
        if mfe:
            summary["avg_mfe_pct"] = round(sum(mfe) / len(mfe), 3)
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps({"summary": summary, "trades": passed,
                                      "all_candidates": rows}, ensure_ascii=False, indent=1))
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"[replay] 明细 → {REPORT}")
        for r in passed[:12]:
            print(f"   {r['day']} {r['symbol']:10s} {r['source']:22s} 入场{r['entry_px']:>9} "
                  f"chg{r['chg_pct']:>7}% 规则={r['rule']['net_pct']:>7}% "
                  f"盘前收口={r.get('pre_close',{}).get('net_pct')}% 收盘={r.get('rth_close',{}).get('net_pct')}%")
    finally:
        ctx.close()


if __name__ == "__main__":
    main()