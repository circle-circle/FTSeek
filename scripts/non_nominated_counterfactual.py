#!/usr/bin/env python3
"""未提名候选 · 反事实跟踪 (record-only, 零交易行为) — DEB-20261105 §执行项.

要回答的唯一问题
────────────────
**bridge 提名这一步, 到底有没有"选择性"?**
每天扫描 ~70-77 只, DA 只提名 16-24 只 (占位 66-79%)。若被提名的那批在事后
前瞻收益上并不优于被丢掉的那批, 那么"提高提名率"就不是提高收益的杠杆 ——
反之若明显更优, 提名率低就是真瓶颈, 且可以用同一把尺度量"扩多少合适"。

口径 (冻结, 读数前写定)
────────────────
· 人群: `playbook_instructions` 中 row_kind='supply_placeholder' (未提名) vs 其余 (提名)。
· 入场价 = **当日开盘价** (pipeline 盘前决策, 用开盘价可避免前视); 出场 = D+N 收盘。
  N ∈ {1,3,5}。这是"裸持有"口径: 不含 SL/TP/费用/盘中退出 ⇒ **不等于策略收益**,
  只用于比较两组的**选择性**, 不得当成策略 PnL 引用。
· 方向: 提名行按 direction 归一 (SHORT 取负); 未提名行无方向 ⇒ 只报多头口径 +
  绝对波动 (双向理论最优上界, 不可实现)。
· 显著性: 按交易日聚类 bootstrap (同日候选不独立)。
· **判据 (纯统计, 不设调参阈值)**: 差值 (提名 − 未提名) 的 95% CI
  含 0 ⇒ 不认定提名有选择性; CI 下界 > 0 ⇒ 提名有选择性; CI 上界 < 0 ⇒ 提名为负选择。
· 数据覆盖 < 80% ⇒ 只报覆盖, 不出结论 (避免样本选择偏差伪装成结论)。

数据源: data/shadow/bars_cache.json + data/replay_bars/daily_*.json (同格式, 后者优先)。
用法:
  PYTHONPATH=. venv/bin/python scripts/non_nominated_counterfactual.py           # capture+report
  PYTHONPATH=. venv/bin/python scripts/non_nominated_counterfactual.py --report  # 只读汇总
"""
from __future__ import annotations

import argparse
import glob
import json
import random
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "ftseek_us.db"
BARS = REPO / "data" / "shadow" / "bars_cache.json"
REPLAY = REPO / "data" / "replay_bars" / "daily_*.json"
OUT_JSONL = REPO / "data" / "shadow" / "non_nominated_counterfactual.jsonl"
OUT_LATEST = REPO / "data" / "shadow" / "non_nominated_counterfactual_latest.json"
# 本脚本自己的日线缓存 (只追加自己抓的, 不写别人的 bars_cache.json)
CCF_BARS = REPO / "data" / "shadow" / "ccf_bars.json"
HORIZONS = (1, 3, 5)
MIN_COVERAGE = 0.80


def load_bars() -> dict:
    """symbol → {date: (open, high, low, close)}; 优先级 ccf_bars > replay_bars > bars_cache。"""
    out = {}
    if BARS.exists():
        for sym, rows in json.loads(BARS.read_text(encoding="utf-8")).items():
            for r in rows:
                try:
                    out.setdefault(sym, {})[r[0]] = (float(r[1]), float(r[2]), float(r[3]), float(r[4]))
                except Exception:
                    continue
    for p in glob.glob(str(REPLAY)):
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception:
            continue
        for r in data:
            # 文件名形如 daily_US_AAPL_<start>_<end>.json ⇒ 从文件名取 symbol
            sym = Path(p).name.split("_")[1] + "." + Path(p).name.split("_")[2]
            try:
                out.setdefault(sym, {})[r[0]] = (float(r[1]), float(r[2]), float(r[3]), float(r[4]))
            except Exception:
                continue
    if CCF_BARS.exists():                       # 自抓数据优先级最高
        try:
            for sym, dd in json.loads(CCF_BARS.read_text(encoding="utf-8")).items():
                for d, ohlc in dd.items():
                    out.setdefault(sym, {})[d] = tuple(ohlc)
        except Exception:
            pass
    return out


def refresh_bars(symbols, start: str, end: str, verbose=True) -> dict:
    """用 Futu K_DAY 补齐 ccf_bars.json (只读行情; 失败逐只跳过, 不抛)。"""
    out = {}
    if CCF_BARS.exists():
        try:
            out = json.loads(CCF_BARS.read_text(encoding="utf-8"))
        except Exception:
            out = {}
    try:
        from futu import OpenQuoteContext, KLType, AuType
    except Exception as e:
        print(f"[refresh] futu 不可用, 跳过: {e}")
        return out
    ctx = None
    ok = fail = 0
    try:
        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
        for s in symbols:
            try:
                ret, data, _ = ctx.request_history_kline(
                    s, start=start, end=end, ktype=KLType.K_DAY, autype=AuType.QFQ, max_count=1000)
                if ret != 0 or data is None or len(data) == 0:
                    fail += 1
                    continue
                for _, row in data.iterrows():
                    d = str(row["time_key"])[:10]
                    out.setdefault(s, {})[d] = [round(float(row["open"]), 4), round(float(row["high"]), 4),
                                                round(float(row["low"]), 4), round(float(row["close"]), 4)]
                ok += 1
            except Exception:
                fail += 1
    finally:
        try:
            if ctx is not None:
                ctx.close()
        except Exception:
            pass
    CCF_BARS.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    if verbose:
        print(f"[refresh] 成功 {ok} / 失败 {fail} 只 → {CCF_BARS.name} ({len(out)} symbols)")
    return out


def load_instructions() -> list:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(
            "SELECT date, symbol, direction, row_kind, COALESCE(confidence,0) AS confidence "
            "  FROM playbook_instructions "
            " WHERE date >= '2026-07-01' AND COALESCE(direction,'') <> ''")]
    finally:
        con.close()


def compute(bars, rows):
    dates_by_sym = {s: sorted(dd) for s, dd in bars.items()}
    idx = {s: {d: i for i, d in enumerate(ds)} for s, ds in dates_by_sym.items()}
    out = []
    for r in rows:
        sym, d = r["symbol"], r["date"]
        i = idx.get(sym, {}).get(d)
        if i is None:
            continue
        ds = dates_by_sym[sym]
        open_px = bars[sym][d][0]
        if not open_px:
            continue
        rec = {"date": d, "symbol": sym,
               "kind": "placeholder" if r.get("row_kind") == "supply_placeholder" else "nominated",
               "direction": r.get("direction") if r.get("row_kind") != "supply_placeholder" else None,
               "confidence": r.get("confidence")}
        for h in HORIZONS:
            if i + h < len(ds):
                close_px = bars[sym][ds[i + h]][3]
                rec[f"r{h}"] = round((close_px - open_px) / open_px * 100, 4)
            else:
                rec[f"r{h}"] = None
        out.append(rec)
    return out


def _ci(vals_by_day, iters=2000, seed=20261105):
    ks = list(vals_by_day)
    if len(ks) < 3:
        return None
    random.seed(seed)
    ms = []
    for _ in range(iters):
        s = [v for k in random.choices(ks, k=len(ks)) for v in vals_by_day[k]]
        ms.append(sum(s) / len(s))
    ms.sort()
    return [round(ms[int(0.025 * iters)], 4), round(ms[int(0.975 * iters)], 4)]


def _group_stats(recs, h, direction_adj):
    by_day = defaultdict(list)
    for r in recs:
        v = r.get(f"r{h}")
        if v is None:
            continue
        if direction_adj and str(r.get("direction") or "").upper() in ("SELL_SHORT", "SHORT"):
            v = -v
        by_day[r["date"]].append(v)
    vals = [v for vs in by_day.values() for v in vs]
    if not vals:
        return None
    return {"n": len(vals), "days": len(by_day),
            "mean": round(st.mean(vals), 4), "median": round(st.median(vals), 4),
            "pos_rate": round(sum(1 for v in vals if v > 0) / len(vals), 4),
            "ci95": _ci(by_day)}


def report(recs, coverage):
    out = {"generated_at": datetime.now().isoformat(timespec="seconds"),
           "criterion": "差值 CI 含 0 ⇒ 不认定提名有选择性 (纯统计判据, 无调参阈值)",
           "coverage": coverage, "horizons": {}}
    for h in HORIZONS:
        ph = [r for r in recs if r["kind"] == "placeholder"]
        nm = [r for r in recs if r["kind"] == "nominated"]
        a, b = _group_stats(ph, h, False), _group_stats(nm, h, True)
        if not a or not b:
            continue
        # 差值: 按"日配对"取两组的日均差, 再对日均差做 bootstrap
        pa = defaultdict(list)
        pb = defaultdict(list)
        for r in ph:
            if r.get(f"r{h}") is not None:
                pa[r["date"]].append(r[f"r{h}"])
        for r in nm:
            v = r.get(f"r{h}")
            if v is None:
                continue
            if str(r.get("direction") or "").upper() in ("SELL_SHORT", "SHORT"):
                v = -v
            pb[r["date"]].append(v)
        diffs = {d: st.mean(pb[d]) - st.mean(pa[d]) for d in set(pa) & set(pb)}
        out["horizons"][f"T+{h}"] = {
            "placeholder": a, "nominated": b,
            "diff_nominated_minus_placeholder": (round(st.mean(diffs.values()), 4) if diffs else None),
            "diff_ci95": _ci({d: [v] for d, v in diffs.items()}) if diffs else None,
            "paired_days": len(diffs),
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--refresh-bars", action="store_true",
                    help="先用 Futu K_DAY 补齐 ccf_bars.json (只读行情)")
    ap.add_argument("--days", type=int, default=10, help="refresh 覆盖的最近天数")
    ap.add_argument("--min-n", type=int, default=200)
    args = ap.parse_args()

    rows = load_instructions()
    if args.refresh_bars:
        bars = load_bars()
        need = sorted({r["symbol"] for r in rows
                       if r["date"] >= (datetime.now().strftime("%Y-%m-%d")[:8] + "01")})
        # 只补"库里最近 N 天内没有数据"的标的, 避免每轮全量拉
        cutoff = sorted({r["date"] for r in rows})[-args.days:][0] if rows else "2026-01-01"
        missing = [s for s in need if not any(d >= cutoff for d in bars.get(s, {}))]
        refresh_bars(missing or need, start=cutoff, end=datetime.now().strftime("%Y-%m-%d"))
        bars = load_bars()
    if not args.report:
        bars = bars if args.refresh_bars else load_bars()
        recs = compute(bars, rows)
        known = {(r["date"], r["symbol"], r["kind"]) for r in
                 ([json.loads(l) for l in OUT_JSONL.read_text(encoding="utf-8").splitlines() if l.strip()]
                  if OUT_JSONL.exists() else [])}
        new = [r for r in recs if (r["date"], r["symbol"], r["kind"]) not in known]
        if new:
            with open(OUT_JSONL, "a", encoding="utf-8") as f:
                for r in new:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"capture: 新写入 {len(new)} 行 (总可算 {len(recs)})")

    recs = [json.loads(l) for l in OUT_JSONL.read_text(encoding="utf-8").splitlines() if l.strip()] \
        if OUT_JSONL.exists() else []
    if not recs:
        print("零行: 无可用 bars 覆盖 ⇒ TOO_EARLY/DEAD, 不得据此下结论")
        return

    total = len(rows)
    coverage = round(len(recs) / total, 4) if total else 0.0
    rep = report(recs, {"rows_total": total, "rows_with_bars": len(recs), "rate": coverage})
    OUT_LATEST.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"覆盖 {len(recs)}/{total} = {coverage*100:.0f}%")
    if coverage < MIN_COVERAGE:
        print(f"⚠️ 覆盖率 <{MIN_COVERAGE*100:.0f}% ⇒ 按预注册只报覆盖, 不出结论")
    for h, d in rep["horizons"].items():
        ph, nm = d["placeholder"], d["nominated"]
        print(f"{h}: 未提名 n={ph['n']:<4} 均值{ph['mean']:+.2f}% CI{ph['ci95']} | "
              f"提名 n={nm['n']:<4} 均值{nm['mean']:+.2f}% CI{nm['ci95']} | "
              f"差 {d['diff_nominated_minus_placeholder']:+.2f}pp CI{d['diff_ci95']}")
    if coverage >= MIN_COVERAGE:
        d5 = rep["horizons"].get("T+5") or {}
        ci = d5.get("diff_ci95") or [None, None]
        if ci[0] is not None:
            verdict = ("提名为正选择" if ci[0] > 0 else
                       ("提名为负选择" if ci[1] < 0 else "无证据表明提名有选择性"))
            print(f"裁决 (T+5): {verdict}")
    print(f"→ {OUT_LATEST}")


if __name__ == "__main__":
    main()