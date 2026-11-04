#!/usr/bin/env python3
"""选股技能度量: 选中 (executed) vs 未选 (SKIP) 的前瞻收益对照 — 只读, 不写任何表.

为什么需要它: DA 的职责被描述为"选股+方向, 出场归规则", 但此前 SKIP 行 3400 条里
t3 填充 0 条 (ic_tracker.backfill_forward_returns 的 SQL 显式 `direction != 'SKIP'`)
⇒ 没有对照臂, "选出来的股票是否比没选的更好"无法回答.

口径 (与 ic_tracker 一致处 / 差异处):
  - 价格基准统一用 **指令日收盘** (close[D]) 对两组同口径比较 (选中组也不用成交价,
    否则两臂基准不同, 测的就不是选股而是执行)。
  - d1/d3 = close[D+1]/close[D] − 1, close[D+3]/close[D] − 1 (D+N 取窗口内实际第 N 根)。
  - 方向化收益 = sign × raw (BUY:+1 / SELL_SHORT:−1), 与 fwd_return_t1 契约同号。
  - 未选组没有方向 ⇒ 同时报 "若全做多" 与 "若全做空" 两个反事实, 不做择优.
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB = ROOT / "data" / "ftseek_us.db"


def _stats(vals):
    n = len(vals)
    if n < 3:
        return None
    m = sum(vals) / n
    sd = st.pstdev(vals)
    t = m / (sd / n ** 0.5) if sd else 0.0
    return n, m * 100, sum(1 for v in vals if v > 0) / n * 100, t


def _fmt(a, label):
    if a is None:
        print(f"  {label:26s} n<3")
        return
    print(f"  {label:26s} n={a[0]:>4} 均={a[1]:>+8.3f}% 命中={a[2]:>5.1f}% t={a[3]:>+6.2f}")


def run(since: str, until: str, max_symbols: int | None):
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT date, symbol, direction, COALESCE(executed,0) ex, confidence,
                  COALESCE(row_kind,'') rk
           FROM playbook_instructions WHERE date BETWEEN ? AND ?""", (since, until)
    ).fetchall()
    print(f"[输入] 指令行 {len(rows)} 条, 区间 {since}..{until}")

    # 去重: 同一 (date, symbol) 在同一臂里只算一次
    picked, skipped = {}, {}
    for r in rows:
        key = (r["date"], r["symbol"])
        if r["ex"] == 1 and r["direction"] in ("BUY", "SELL_SHORT"):
            picked[key] = r["direction"]
        elif r["direction"] == "SKIP":
            skipped.setdefault(key, None)
    skipped = {k: v for k, v in skipped.items() if k not in picked}
    print(f"[分组] 选中 {len(picked)} 个 (date,symbol) | 未选 {len(skipped)} 个 | "
          f"重叠 {len(set(picked) & set(skipped))} (已从未选剔除)")
    print(f"[口径] 基准=指令日收盘; d1/d3 = 后续第 1/3 根收盘; 两臂同基准")

    import json as _json
    cache_path = Path("/tmp/sel_bars_cache.json")
    disk = {}
    if cache_path.exists():
        try:
            disk = _json.loads(cache_path.read_text())
        except Exception:
            disk = {}
    from app.data.market_data_router import MarketDataRouter
    md = MarketDataRouter()
    symbols = sorted({s for _, s in list(picked) + list(skipped)})
    if max_symbols:
        symbols = symbols[:max_symbols]
    bars_cache: dict[str, dict] = {}
    fail = []
    import time as _time
    for i, sym in enumerate(symbols, 1):
        if sym in disk and disk[sym]:
            bars_cache[sym] = disk[sym]
            continue
        try:
            bars = md.get_daily_bars(sym, count=60) or []
        except Exception:
            bars = []
        # Futu 频控 60 次/30s (≈2/s): 逐标的 0.6s 间隔, 避免与服务端抢通道
        _time.sleep(0.6)
        d = {}
        for b in bars:
            t, c = b.get("time"), b.get("close")
            if t and c:
                d[t] = float(c)
        if d:
            bars_cache[sym] = d
        else:
            fail.append(sym)
        if i % 50 == 0:
            print(f"  …价格 {i}/{len(symbols)} (失败 {len(fail)}, 缓存命中 {sum(1 for x in symbols[:i] if x in disk)})", flush=True)
    try:
        cache_path.write_text(_json.dumps(bars_cache))
    except Exception as _e:
        print("[warn] 缓存写入失败:", _e)
    print(f"[价格] 拿到 {len(bars_cache)} / {len(symbols)} 个标的"
          + (f", 失败样例 {fail[:5]}" if fail else ""))

    def rets(sym, day):
        d = bars_cache.get(sym)
        if not d:
            return None
        days = sorted(d)
        if day not in d:
            return None
        i = days.index(day)
        base = d[day]
        out = {}
        for h in (1, 3):
            if i + h < len(days):
                out[h] = d[days[i + h]] / base - 1.0
        return out or None

    agg = defaultdict(lambda: defaultdict(list))  # month -> metric -> vals
    for (day, sym), direction in picked.items():
        r = rets(sym, day)
        if not r:
            continue
        m = day[:7]
        sign = 1 if direction == "BUY" else -1
        for h, v in r.items():
            agg[m][f"picked_raw_d{h}"].append(v)
            agg[m][f"picked_dir_d{h}"].append(sign * v)
    for (day, sym) in skipped:
        r = rets(sym, day)
        if not r:
            continue
        m = day[:7]
        for h, v in r.items():
            agg[m][f"skip_raw_d{h}"].append(v)
            agg[m][f"skip_short_d{h}"].append(-v)

    # ── 日聚类 Δ (同一天的多笔高度相关; 行级 t 会虚高) ──
    day_p, day_s = defaultdict(list), defaultdict(list)   # key=(date, h)
    for (day, sym), direction in picked.items():
        r = rets(sym, day)
        if not r:
            continue
        sign = 1 if direction == "BUY" else -1
        for h, v in r.items():
            day_p[(day, h)].append(sign * v)
    for (day, sym) in skipped:
        r = rets(sym, day)
        if not r:
            continue
        for h, v in r.items():
            day_s[(day, h)].append(v)
    print("\n" + "=" * 92)
    print("日聚类 Δ (选中方向化 − 未选做多): 同一交易日多笔高度相关, 行级 t 会虚高")
    print("=" * 92)
    for h in (1, 3):
        for m in sorted({k[0][:7] for k in day_p}):
            ds = []
            for (d, hh), pv in day_p.items():
                if d[:7] != m or hh != h:
                    continue
                sv = day_s.get((d, h)) or []
                if pv and sv:
                    ds.append(sum(pv) / len(pv) - sum(sv) / len(sv))
            a = _stats(ds) if len(ds) >= 3 else None
            if a:
                print(f"  d{h} {m}: 交易日={a[0]:>3} Δ均={a[1]:>+7.3f}pp Δ中位="
                      f"{st.median(ds)*100:>+7.3f}pp 正日占比={a[2]:>5.1f}% t={a[3]:>+6.2f}")

    # ── beta 混淆检验: 若 Δ 只在"大盘跌日"为负、"涨日"为正 ⇒ 是 beta 不是技能 ──
    print("\n" + "=" * 92)
    print("beta 混淆检验 (d1): 按'未选组当日均值'的符号分涨跌日, 看 Δ 是否随市场翻转")
    print("=" * 92)
    up, dn = [], []
    for (d, hh), pv in day_p.items():
        if hh != 1:
            continue
        sv = day_s.get((d, 1)) or []
        if not pv or not sv:
            continue
        delta = sum(pv) / len(pv) - sum(sv) / len(sv)
        (up if sum(sv) / len(sv) > 0 else dn).append(delta)
    for label, vals in (("大盘涨日", up), ("大盘跌日", dn)):
        a = _stats(vals)
        if a:
            print(f"  {label}: 天数={a[0]:>3} Δ均={a[1]:>+7.3f}pp 正日占比={a[2]:>5.1f}% t={a[3]:>+6.2f}")
        else:
            print(f"  {label}: 天数不足")

    total = defaultdict(list)
    print("\n" + "=" * 92)
    print("月度: 选中(方向化) vs 未选(若做多) — 差异 = 选股+方向 是否加值")
    print("=" * 92)
    for m in sorted(agg):
        print(f"\n-- {m} --")
        for k in ("picked_raw_d1", "picked_dir_d1", "skip_raw_d1"):
            a = _stats(agg[m][k])
            _fmt(a, k)
            total[k].extend(agg[m][k])
        a_p, a_s = _stats(agg[m]["picked_dir_d1"]), _stats(agg[m]["skip_raw_d1"])
        if a_p and a_s:
            print(f"  {'Δ 选中方向化 − 未选做多':26s} {a_p[1] - a_s[1]:>+8.3f} pp")
        for k in ("picked_dir_d3", "skip_raw_d3"):
            a = _stats(agg[m][k]); _fmt(a, k)
            total[k].extend(agg[m][k])

    print("\n" + "=" * 92)
    print("合计 (全窗口)")
    print("=" * 92)
    for k in ("picked_raw_d1", "picked_dir_d1", "skip_raw_d1", "skip_short_d1",
              "picked_dir_d3", "skip_raw_d3"):
        _fmt(_stats(total[k]), k)
    p1, s1 = _stats(total["picked_dir_d1"]), _stats(total["skip_raw_d1"])
    if p1 and s1:
        print(f"\n  选股+方向 vs 未选做多 (d1): {p1[1] - s1[1]:+.3f} pp")
    pr, sr = _stats(total["picked_raw_d1"]), _stats(total["skip_raw_d1"])
    if pr and sr:
        print(f"  纯选股 (同做多, d1)        : {pr[1] - sr[1]:+.3f} pp")
    pd, pr2 = _stats(total["picked_dir_d1"]), _stats(total["picked_raw_d1"])
    if pd and pr2:
        print(f"  方向加值 (d1)              : {pd[1] - pr2[1]:+.3f} pp")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-01")
    ap.add_argument("--until", default="2026-11-03")
    ap.add_argument("--max-symbols", type=int, default=None)
    a = ap.parse_args()
    run(a.since, a.until, a.max_symbols)