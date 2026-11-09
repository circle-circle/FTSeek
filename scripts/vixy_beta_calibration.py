#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VIXY→VIX 传导系数 (beta) 重新标定 —— 只读, 手动运行。

用途: `app/data/vix_estimator.py` 的 `BETA_DEFAULT` 会漂移 (滚动 60 日区间
1.48–2.60 实测)。每次改常量前先跑本脚本, 把 beta / R² / MAE 表贴进 DEB。

口径:
  - 真值: FRED VIXCLS 日收盘;
  - 代理: VIXY 日收盘 (Yahoo chart API, 与 Futu 行情同为 16:00 ET 收盘口径);
  - 收益: 简单日收益率; 剔除 |r|>60% 的异常点 (拆分/数据错);
  - 误差评估: 同日收盘预测, 三个候选 (昨收真值 / VIXY×0.85 / 真值锚定×beta)。

用法: venv/bin/python scripts/vixy_beta_calibration.py [--since 2025-06-01]
"""
from __future__ import annotations

import argparse
import json
import math
import urllib.request
from typing import Tuple

import pandas as pd


def _fred(series: str) -> pd.Series:
    txt = urllib.request.urlopen(
        f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}", timeout=30
    ).read().decode()
    rows = []
    for ln in txt.strip().splitlines()[1:]:
        parts = ln.split(",")
        if len(parts) < 2:
            continue
        try:
            rows.append((parts[0], float(parts[1])))
        except ValueError:
            continue
    s = pd.Series(dict(rows))
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def _yahoo(sym: str, p1: int, p2: int) -> pd.Series:
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
           f"?period1={p1}&period2={p2}&interval=1d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    d = json.load(urllib.request.urlopen(req, timeout=30))["chart"]["result"][0]
    idx = (pd.to_datetime(d["timestamp"], unit="s", utc=True)
           .tz_convert("America/New_York").normalize().tz_localize(None))
    return pd.Series(d["indicators"]["quote"][0]["close"], index=idx).dropna()


def _ols(x: pd.Series, y: pd.Series) -> Tuple[float, float]:
    mx, my = x.mean(), y.mean()
    beta = ((x - mx) * (y - my)).sum() / ((x - mx) ** 2).sum()
    alpha = my - beta * mx
    r2 = 1 - ((y - (alpha + beta * x)) ** 2).sum() / ((y - my) ** 2).sum()
    return float(beta), float(r2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2025-06-01")
    args = ap.parse_args()

    vix = _fred("VIXCLS")
    vixy = _yahoo("VIXY", 1748736000, 1794182400)
    j = pd.concat([vix.rename("vix"), vixy.rename("vixy")], axis=1, join="inner").dropna()
    j = j[j.index >= args.since]
    print(f"对齐样本: {len(j)} 日 ({j.index.min().date()} .. {j.index.max().date()})")

    r = j.pct_change().dropna()
    r = r[(r.vix.abs() < 0.6) & (r.vixy.abs() < 0.6)]
    beta, r2 = _ols(r.vixy, r.vix)
    print(f"beta={beta:.3f}  R²={r2:.3f}  corr={r.vixy.corr(r.vix):.3f}  n={len(r)}")

    rb = []
    for i in range(60, len(r)):
        w = r.iloc[i - 60:i]
        rb.append(_ols(w.vixy, w.vix)[0])
    if rb:
        s = pd.Series(rb)
        print(f"滚动60日 beta: 中位 {s.median():.2f} / 区间 {s.min():.2f}–{s.max():.2f} / 最新 {s.iloc[-1]:.2f}")

    t = j.index[1:]
    truth, prev_truth = j["vix"].loc[t], j["vix"].shift(1).loc[t]
    prev_vixy = j["vixy"].shift(1).loc[t]
    rx = j["vixy"].loc[t] / prev_vixy - 1
    cands = {
        "A 昨收真值(不更新)": prev_truth,
        "B VIXY×0.85 (旧代理)": j["vixy"].loc[t] * 0.85,
        f"C 真值锚定×beta={beta:.2f}": prev_truth * (1 + beta * rx),
        "D 真值锚定×beta=1": prev_truth * (1 + rx),
    }
    print(f"{'候选':28s} {'MAE':>7s} {'bias':>8s} {'RMSE':>7s}")
    for name, pred in cands.items():
        e = pred - truth
        print(f"{name:28s} {e.abs().mean():7.3f} {e.mean():+8.3f} {math.sqrt((e ** 2).mean()):7.3f}")
    print("\n若 BETA 需更新: 改 app/data/vix_estimator.py::BETA_DEFAULT 并在 DEB 记录本表。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())