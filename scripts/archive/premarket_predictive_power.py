#!/usr/bin/env python3
"""
盘前趋势预测力验证 (Pre-Market Predictive Power)

问题: 盘前的成交趋势/方向, 能否预测开盘后的真实方向?

方法:
  对每个标的的每个交易日, 用含扩展时段的 1 分钟线:
    - 昨日常规收盘 (prev_regular_close, 15:59 ET 收盘)
    - 盘前最后价 (pre_last, 09:29 ET)
    - 开盘价 (open_930)
    - 开盘后 30/60 分钟收盘, 全天收盘 (15:59)
  定义:
    gap_pct        = pre_last / prev_regular_close - 1      # 盘前相对昨收的趋势
    ret_open_30    = close_1000 / open_930 - 1              # 开盘后30min真实走势
    ret_open_60    = close_1100 / open_930 - 1
    ret_open_eod   = close_eod  / open_930 - 1
    ret_prevclose_eod = close_eod / prev_regular_close - 1  # 从昨收看全天(含跳空)
  统计:
    - 方向命中率: sign(gap) == sign(ret_open_*)
    - Spearman/Pearson 相关
    - gap-fill: 开盘后是否朝盘前反方向回补
"""
import sys
import os
import time
import json
from datetime import datetime, timedelta

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from futu import OpenQuoteContext, KLType  # noqa: E402

HOST = os.environ.get("FUTU_OPEND_HOST", "127.0.0.1")
PORT = int(os.environ.get("FUTU_OPEND_PORT", "11111"))

SYMBOLS = [
    "US.SPY", "US.QQQ", "US.AAPL", "US.MSFT", "US.NVDA", "US.AMZN",
    "US.META", "US.TSLA", "US.AMD", "US.AVGO", "US.GOOG", "US.MU",
    "US.BABA", "US.JD", "US.PDD", "US.NIO", "US.COIN", "US.UBER",
    "US.JPM", "US.BAC", "US.GS", "US.XOM", "US.WMT", "US.ORCL", "US.CRM",
]

DAYS_BACK = 45


def hhmm(tk: str) -> int:
    # tk = "2026-07-16 09:30:00" -> 930
    t = tk[11:16]
    return int(t[:2]) * 100 + int(t[3:5])


def analyze_symbol(ctx, symbol):
    """返回该标的每个交易日的一行记录 dict 列表"""
    end = datetime.now()
    start = end - timedelta(days=DAYS_BACK)
    rows_all = []
    page_key = None
    # futu 单次 max_count 1000 (约 1.4 个交易日的分钟线), 需要翻页
    while True:
        ret, data, page_key = ctx.request_history_kline(
            symbol,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            ktype=KLType.K_1M,
            max_count=1000,
            extended_time=True,
            page_req_key=page_key,
        )
        if ret != 0:
            print(f"  [{symbol}] ERR: {data}")
            return []
        for _, r in data.iterrows():
            rows_all.append((str(r["time_key"]), float(r["close"]), float(r["volume"])))
        if page_key is None:
            break
        time.sleep(0.3)

    # 按日期分组
    by_day = {}
    for tk, close, vol in rows_all:
        d = tk[:10]
        by_day.setdefault(d, []).append((tk, close, vol))

    days = sorted(by_day.keys())
    results = []
    for i in range(1, len(days)):
        prev_d = days[i - 1]
        d = days[i]
        prev_bars = by_day[prev_d]
        bars = by_day[d]

        # 昨日常规收盘: prev_d 中 hhmm<=1559 的最后一根
        prev_reg = [b for b in prev_bars if hhmm(b[0]) <= 1559 and hhmm(b[0]) >= 930]
        if not prev_reg:
            continue
        prev_close = prev_reg[-1][1]

        # 盘前最后价: d 中 hhmm < 930 的最后一根
        pre_bars = [b for b in bars if hhmm(b[0]) < 930]
        pre_last = pre_bars[-1][1] if pre_bars else None
        pre_vol = sum(b[2] for b in pre_bars)

        reg_bars = [b for b in bars if 930 <= hhmm(b[0]) <= 1559]
        if not reg_bars:
            continue
        # 开盘价 ~ 09:30 第一根的收盘 (近似)
        open_930 = reg_bars[0][1]

        def price_at_or_after(target):
            for b in reg_bars:
                if hhmm(b[0]) >= target:
                    return b[1]
            return reg_bars[-1][1]

        c1000 = price_at_or_after(1000)
        c1100 = price_at_or_after(1100)
        ceod = reg_bars[-1][1]

        if prev_close <= 0 or open_930 <= 0 or pre_last is None:
            continue

        gap = pre_last / prev_close - 1.0
        ret_open_30 = c1000 / open_930 - 1.0
        ret_open_60 = c1100 / open_930 - 1.0
        ret_open_eod = ceod / open_930 - 1.0
        ret_prevclose_eod = ceod / prev_close - 1.0

        results.append({
            "symbol": symbol, "date": d,
            "gap": gap, "pre_vol": pre_vol,
            "ret_open_30": ret_open_30,
            "ret_open_60": ret_open_60,
            "ret_open_eod": ret_open_eod,
            "ret_prevclose_eod": ret_prevclose_eod,
        })
    return results


def sign_hit(a, b):
    if a == 0:
        return None
    return 1 if (a > 0) == (b > 0) else 0


def summarize(rows, gap_key, ret_key, label, min_gap=0.0):
    xs, ys = [], []
    hits = []
    for r in rows:
        g = r[gap_key]
        if abs(g) < min_gap:
            continue
        y = r[ret_key]
        xs.append(g)
        ys.append(y)
        h = sign_hit(g, y)
        if h is not None:
            hits.append(h)
    if len(xs) < 5:
        print(f"  {label:28s} 样本不足 (n={len(xs)})")
        return
    xs = np.array(xs)
    ys = np.array(ys)
    # pearson
    pear = np.corrcoef(xs, ys)[0, 1]
    # spearman
    rx = np.argsort(np.argsort(xs))
    ry = np.argsort(np.argsort(ys))
    spear = np.corrcoef(rx, ry)[0, 1]
    hr = 100.0 * sum(hits) / len(hits)
    print(f"  {label:28s} n={len(xs):4d}  命中率={hr:5.1f}%  Pearson={pear:+.3f}  Spearman={spear:+.3f}")


def main():
    print("连接 OpenD...")
    ctx = OpenQuoteContext(host=HOST, port=PORT)
    all_rows = []
    try:
        for sym in SYMBOLS:
            print(f"拉取 {sym} ...")
            rows = analyze_symbol(ctx, sym)
            print(f"  -> {len(rows)} 交易日")
            all_rows.extend(rows)
            time.sleep(0.5)
    finally:
        ctx.close()

    print(f"\n总样本 (标的×交易日): {len(all_rows)}")
    out = os.path.join(os.path.dirname(__file__), "..", "data", "premarket_predictive_power.json")
    json.dump(all_rows, open(out, "w"), indent=2)
    print(f"原始数据已存: {out}\n")

    print("=" * 78)
    print("盘前 Gap 方向  vs  开盘后真实方向 (全样本)")
    print("=" * 78)
    summarize(all_rows, "gap", "ret_open_30", "gap -> 开盘后30min")
    summarize(all_rows, "gap", "ret_open_60", "gap -> 开盘后60min")
    summarize(all_rows, "gap", "ret_open_eod", "gap -> 开盘后全天")
    summarize(all_rows, "gap", "ret_prevclose_eod", "gap -> 昨收算全天(含跳空)")

    print("\n" + "=" * 78)
    print("仅显著跳空 (|gap| >= 0.5%) 时的预测力")
    print("=" * 78)
    summarize(all_rows, "gap", "ret_open_30", "gap -> 开盘后30min", min_gap=0.005)
    summarize(all_rows, "gap", "ret_open_60", "gap -> 开盘后60min", min_gap=0.005)
    summarize(all_rows, "gap", "ret_open_eod", "gap -> 开盘后全天", min_gap=0.005)

    print("\n" + "=" * 78)
    print("仅大幅跳空 (|gap| >= 1.0%) 时的预测力")
    print("=" * 78)
    summarize(all_rows, "gap", "ret_open_30", "gap -> 开盘后30min", min_gap=0.010)
    summarize(all_rows, "gap", "ret_open_60", "gap -> 开盘后60min", min_gap=0.010)
    summarize(all_rows, "gap", "ret_open_eod", "gap -> 开盘后全天", min_gap=0.010)

    # gap-fill 统计: 显著跳空后, 开盘30min是否朝反方向 (回补)
    print("\n" + "=" * 78)
    print("跳空回补倾向 (|gap|>=0.5%: 开盘后30min朝gap反方向的比例)")
    print("=" * 78)
    for lo, hi, name in [(0.005, 0.01, "小跳空 0.5-1%"), (0.01, 999, "大跳空 >1%")]:
        cont, fill = 0, 0
        for r in all_rows:
            g = r["gap"]
            if lo <= abs(g) < hi:
                y = r["ret_open_30"]
                if y == 0:
                    continue
                if (g > 0) == (y > 0):
                    cont += 1
                else:
                    fill += 1
        tot = cont + fill
        if tot:
            print(f"  {name:14s} n={tot:4d}  延续={100*cont/tot:5.1f}%  回补={100*fill/tot:5.1f}%")


if __name__ == "__main__":
    main()
