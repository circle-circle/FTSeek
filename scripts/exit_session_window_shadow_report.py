#!/usr/bin/env python3
"""退出侧时段窗影子结算 (只读): 每条留痕若推迟到窗末再平, 差价多少。

输入: data/shadow/exit_session_window_shadow.jsonl (写方 app/playbook/
      exit_session_window_shadow.py, record-only, 平仓照常执行)
价格: Futu request_history_kline 1m (Futu 1m 首根为 09:31, 无 09:30 bar)
输出: 按延后档位 (09:31/09:35/09:40/09:45/10:00/10:30) 的 ΣΔ$/均Δ$/胜负,
      另给**日聚类 Δ**(同一交易日多条记录高度相关, 行级 t 会虚高)。

Δ 定义: sign × qty × (价_档位 − 价_判定时刻), LONG sign=+1 / SHORT sign=−1。
正 = 推迟更好 (即"退出侧套用 09:35 时段门"有正收益)。

预注册判据 (见 shadow_registry graduation_criteria):
  ≥10 个交易日 且 ≥20 条记录; 09:35 档 ΣΔ>0 且日聚类 t≥2 且正日占比≥60% ⇒ 提请上线。
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHADOW = ROOT / "data" / "shadow" / "exit_session_window_shadow.jsonl"
HORIZONS = ["09:31", "09:35", "09:40", "09:45", "10:00", "10:30"]
_SIGN = {"LONG": 1, "BUY": 1, "SHORT": -1, "SELL_SHORT": -1, "SELL": -1}


def _load_rows() -> list[dict]:
    if not SHADOW.exists():
        return []
    out = []
    for ln in SHADOW.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception as exc:
            print(f"[exit_session_window_shadow_report] 坏影子行跳过: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
    return out


def _fetch_1m(symbol: str, date: str) -> dict[str, float]:
    """返回 {HH:MM: 收盘价}; 失败返回 {}。"""
    try:
        from futu import OpenQuoteContext, KLType
    except Exception:
        return {}
    ctx = None
    try:
        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
        ret, data, _pk = ctx.request_history_kline(
            symbol, start=date, end=date, ktype=KLType.K_1M, max_count=500)
        if ret != 0:
            return {}
        out = {}
        for _, r in data.iterrows():
            out[str(r["time_key"])[11:16]] = float(r["close"])
        return out
    except Exception:
        return {}
    finally:
        try:
            ctx.close()
        except Exception as exc:
            print(f"[exit_session_window_shadow_report] ctx.close 失败: {type(exc).__name__}: {exc}", file=sys.stderr)


def run(use_fill: bool = False) -> int:
    rows = _load_rows()
    if not rows:
        print(f"[空] 尚无留痕: {SHADOW} (影子刚上线, 等首个盘前/开盘窗内的隔夜平仓)")
        return 0
    print(f"[输入] {len(rows)} 条留痕 | 交易日 {len({r.get('date_et') for r in rows})} 个")

    cache: dict[tuple, dict] = {}
    recs = []
    miss = 0
    for r in rows:
        key = (r.get("symbol"), r.get("date_et"))
        if key not in cache:
            cache[key] = _fetch_1m(r["symbol"], r["date_et"])
        bars = cache[key]
        if not bars:
            miss += 1
            continue
        sign = _SIGN.get(str(r.get("direction", "")).upper(), 1)
        qty = float(r.get("qty") or 0)
        px0 = float(r.get("price_at_decision") or 0)
        if not qty or not px0:
            miss += 1
            continue
        deltas = {}
        for h in HORIZONS:
            p = bars.get(h)
            if p:
                deltas[h] = sign * qty * (p - px0)
        if deltas:
            recs.append({**r, "_deltas": deltas})

    print(f"[价格] 可结算 {len(recs)} 条 | 缺价/缺量 {miss} 条")
    if not recs:
        return 0

    print(f"\n{'档位':6s} {'n':>4} {'ΣΔ$':>10} {'均Δ$':>8} {'正/负':>8} {'中位Δ$':>9}")
    for h in HORIZONS:
        ds = [x["_deltas"][h] for x in recs if h in x["_deltas"]]
        if len(ds) < 2:
            continue
        pos = sum(1 for v in ds if v > 0)
        print(f"{h:6s} {len(ds):>4} {sum(ds):>10.2f} {sum(ds)/len(ds):>8.2f} "
              f"{pos:>3}/{len(ds)-pos:<3} {st.median(ds):>9.2f}")

    # 日聚类 (同一天多条 ⇒ 合并成一天一个样本)
    print("\n日聚类 (每交易日取当日记录均值):")
    for h in ("09:35", "09:40", "10:00"):
        by_day = defaultdict(list)
        for x in recs:
            if h in x["_deltas"]:
                by_day[x["date_et"]].append(x["_deltas"][h])
        day_means = [sum(v) / len(v) for v in by_day.values()]
        n = len(day_means)
        if n < 3:
            print(f"  {h}: 交易日 {n} < 3, 不足以判 (继续攒)")
            continue
        m = sum(day_means) / n
        sd = st.pstdev(day_means)
        t = m / (sd / n ** 0.5) if sd else 0.0
        posd = sum(1 for v in day_means if v > 0) / n * 100
        print(f"  {h}: 交易日={n} Σ={sum(day_means):>+9.2f} 均={m:>+7.2f} 正日占比={posd:>5.1f}% t={t:>+6.2f}")

    print("\n按窗拆 (09:35 档):")
    for w in ("premarket", "rth_open"):
        ds = [x["_deltas"]["09:35"] for x in recs
              if x.get("window") == w and "09:35" in x["_deltas"]]
        if ds:
            pos = sum(1 for v in ds if v > 0)
            print(f"  {w:9s} n={len(ds):>3} Σ={sum(ds):>+9.2f} 均={sum(ds)/len(ds):>+7.2f} 正/负={pos}/{len(ds)-pos}")

    if use_fill:
        print("\n(实际成交价对照需 join trade_records, 本脚本不读交易表)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--use-fill", action="store_true")
    a = ap.parse_args()
    sys.exit(run(a.use_fill))