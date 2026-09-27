#!/usr/bin/env python3
"""亏损离场后的价格回翻分析 (2026-09-28, 用户问: 原本亏损退出的, 是否回撤/多久翻转盈利).

问题: 止损/亏损离场后, 股价有没有涨回入场价 (翻正)? 距离多久?
口径:
  · 人群 = trade_records 8/1 起已平仓且 pnl_net<0 的真仓 (按 exit_reason 分组)
  · 回翻 = 离场后第 1..N 个交易日内 close >= entry_price (毛口径; 净口径 = entry*(1+0.1%) 并列印)
  · ttr_close = 首个 close>=entry 的交易日序号 (离场次日=1); ttr_touch = 首个 high>=entry
  · bar 源 = 富途日K (K_DAY autype=NONE, 与 e1_preview_replay/b19 回放同配方), 缓存 data/replay_bars/
  · 右删失: 离场太近窗口不足 k 日的笔, 在 "≤k日" 桶里只算可判定的 (印 n_eff)
只读: sqlite mode=ro, 只写 bar 缓存 + 产物 json。零交易行为变更。
已知偏差 (印在输出里, 不藏): ①9月大盘上行期回翻率天然偏高 ②BE/费用级小亏离场的回翻是平凡的
(价格本来就在入场价附近) ⇒ 按亏损幅度分桶: small(<1%) / mid(1-3%) / deep(>3%)。
"""
import datetime as dt
import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "data/ftseek_us.db"
CACHE = REPO / "data/replay_bars"
SINCE = "2026-08-01"
TODAY = dt.date(2026, 9, 28)
WINDOWS = (5, 10, 20)          # 交易日桶
NET_PAD = 0.001                # 净口径垫 0.1% (往返费用线量级)
_N_BAD = [0]                   # R5: 坏缓存/坏行计数留痕


def fetch_daily(symbol, start, end):
    """富途日K, 与 b19_wweek_miskill_replay.fetch_daily 同配方 (K_DAY/autype=NONE/1.2s限流)。"""
    fp = CACHE / f"postexit_{symbol.replace('.', '_')}_{start}_{end}.json"
    if fp.exists():
        try:
            b = json.loads(fp.read_text())
            if b and (b[-1][0] >= end or len(b) > 5):
                return b
        except Exception as e:
            _N_BAD[0] += 1
            print(f"    [bad-cache] {fp.name}: {e}")
    from futu import AuType, KLType, RET_OK, OpenQuoteContext
    q = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        ret, df, _ = q.request_history_kline(
            symbol, start=start, end=end, ktype=KLType.K_DAY,
            autype=AuType.NONE, max_count=1000)
        if ret != RET_OK:
            print(f"    [kline-fail] {symbol}: {df}")
            return None
        bars = [[r["time_key"][:10], r["open"], r["high"], r["low"], r["close"]]
                for _, r in df.iterrows()]
    finally:
        q.close()
        time.sleep(1.2)
    if bars:
        fp.write_text(json.dumps(bars))
    return bars


def bucket(loss_pct):
    a = abs(loss_pct)
    return "small(<1%)" if a < 1 else ("mid(1-3%)" if a <= 3 else "deep(>3%)")


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id,symbol,entry_price,exit_price,exit_time,exit_reason,"
        "COALESCE(pnl_net,realized_pnl,0) net FROM trade_records "
        "WHERE date(exit_time)>=? AND COALESCE(pnl_net,realized_pnl,0)<0 "
        "AND entry_price>0 ORDER BY exit_time", (SINCE,)).fetchall()
    print(f"═══ 亏损离场人群: n={len(rows)} ({SINCE} 起) ═══")

    # 每 symbol 一次取 bar (最早离场日 → 今天)
    need = defaultdict(list)
    for r in rows:
        need[r["symbol"]].append(r)
    bars_by_sym = {}
    for sym, rs in sorted(need.items()):
        start = min(str(x["exit_time"])[:10] for x in rs)
        bars = fetch_daily(sym, start, TODAY.isoformat())
        if bars:
            bars_by_sym[sym] = bars
    print(f"  bar 覆盖: {len(bars_by_sym)}/{len(need)} symbols (R5 坏缓存={_N_BAD[0]})")

    out = []
    for r in rows:
        bars = bars_by_sym.get(r["symbol"])
        if not bars:
            continue
        ex_d = str(r["exit_time"])[:10]
        post = [b for b in bars if b[0] > ex_d]           # 离场次日起
        ep = float(r["entry_price"])
        loss_pct = (float(r["exit_price"]) / ep - 1) * 100 if r["exit_price"] else None
        ttr_c = ttr_t = None
        for i, b in enumerate(post, 1):
            if ttr_c is None and b[4] >= ep:
                ttr_c = i
            if ttr_t is None and b[2] >= ep:
                ttr_t = i
            if ttr_c is not None and ttr_t is not None:
                break
        max_close = max((b[4] for b in post), default=None)
        out.append(dict(
            tid=r["id"], symbol=r["symbol"], exit_date=ex_d,
            reason=r["exit_reason"] or "?", net=round(float(r["net"]), 2),
            loss_pct=round(loss_pct, 2) if loss_pct is not None else None,
            bkt=bucket(loss_pct) if loss_pct is not None else "?",
            win_avail=len(post), ttr_close=ttr_c, ttr_touch=ttr_t,
            max_close_gain_pct=round((max_close / ep - 1) * 100, 2) if max_close else None,
            net_recov=bool(ttr_c is not None and any(
                b[4] >= ep * (1 + NET_PAD) for b in post[:ttr_c + 5])) if ttr_c else False,
        ))

    # ── 聚合: 全体 / 按亏损幅度桶 / 按 exit_reason ──
    def agg(group, label):
        n = len(group)
        if not n:
            return
        line = [f"\n  【{label}】 n={n}"]
        for k in WINDOWS:
            det = [x for x in group if x["win_avail"] >= k]   # 右删失: 只算窗口够的
            rec = [x for x in det if x["ttr_close"] is not None and x["ttr_close"] <= k]
            rate = len(rec) / len(det) * 100 if det else float("nan")
            line.append(f"    ≤{k:>2d}交易日回翻: {len(rec):>3d}/{len(det):<3d} = {rate:5.1f}%")
        ttrs = sorted(x["ttr_close"] for x in group if x["ttr_close"] is not None)
        never = [x for x in group if x["ttr_close"] is None and x["win_avail"] >= 20]
        line.append(f"    回翻耗时: 中位 {ttrs[len(ttrs)//2] if ttrs else '-'} 日 "
                    f"(n={len(ttrs)}); ≥20日窗口内从未回翻 {len(never)} 笔")
        if never:
            mg = sorted(x["max_close_gain_pct"] for x in never if x["max_close_gain_pct"] is not None)
            line.append(f"    从未回翻笔的窗内最高收盘: 中位 {mg[len(mg)//2]:+.2f}% "
                        f"(距入场价还差多少)")
        print("\n".join(line))

    print("\n═══ ① 全体亏损离场 ═══")
    agg(out, "全部")
    print("\n═══ ② 按亏损幅度分桶 (BE/费用级小亏的回翻是平凡的, 分开看) ═══")
    byb = defaultdict(list)
    for x in out:
        byb[x["bkt"]].append(x)
    for b in ("small(<1%)", "mid(1-3%)", "deep(>3%)"):
        agg(byb.get(b, []), b)
    print("\n═══ ③ 按 exit_reason ═══")
    byr = defaultdict(list)
    for x in out:
        byr[x["reason"]].append(x)
    for rs, g in sorted(byr.items(), key=lambda kv: -len(kv[1])):
        if len(g) >= 3:
            agg(g, f"{rs} (n={len(g)})")
        else:
            print(f"\n  【{rs}】 n={len(g)} <3 不聚合 (逐笔: "
                  + ", ".join(f"{x['symbol']} ttr={x['ttr_close']}" for x in g) + ")")

    print("\n═══ ⚠️ 判读边界 ═══")
    print("  · 9月窗口大盘上行, 回翻率天然偏高 — 这是描述统计, 不构成'该扛不砍'的裁决依据")
    print("  · 毛口径 close>=entry; 净口径(+0.1%费用垫)差异极小, net_recov 字段在产物里")
    print("  · 右删失已处理: ≤k日桶只算窗口≥k日的笔 (n_eff 印在分母)")
    fp = REPO / f"data/loss_exit_recovery_{TODAY.isoformat()}.json"
    fp.write_text(json.dumps(dict(generated_at=dt.datetime.now().isoformat(timespec="seconds"),
                                  n=len(out), windows=WINDOWS, net_pad=NET_PAD,
                                  r5_bad=_N_BAD[0], trades=out),
                             ensure_ascii=False, indent=1))
    print(f"\n[saved] {fp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
