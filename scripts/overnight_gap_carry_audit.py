#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""隔夜 gap-carry 分解审计 (2026-08-25, 只读零交易路径)。

背景 (DEB-20260825 第4/5轮只审计了费用, 隔夜敞口未被覆盖):
  - 八月隔夜 9 笔 8负1微盈 净 -$1,075 — 单笔决策代价 -$119/笔,
    是费用问题 (-$4.81/笔) 的 ~25 倍; 费用审计两个月 $971 vs 隔夜一个月 $1,075。
  - 可疑机制: 弱日入场持过夜 → 次日早盘延续弱势打止损
    (8/18 入的 MU/FIX/CAT 在 8/19 早盘被 SL_INITIAL, 而 8/19 全天系统 +$443)。

方法 (无前视, 全部用已实现成交价 + Futu 日K):
  每笔多日持仓分解为:
    day0_intraday = (close[d0] − entry_price) × signed_qty        # 入场日日内段
    gap_i         = (open[di] − close[di−1]) × signed_qty          # 每个隔夜跳空
    hold_intraday = 中间日 (open→close) 与出场日 (open→exit_price)   # 后续日内段
  校验门: 分解合计 vs trade_records.realized_pnl 偏差 > $2/笔 → 该笔剔除并报告
  (与 20260823 五否决回放同款"先证明记账可复现"纪律)。

预注册提案触发判据 (写死于脚本, 防事后挪门柱):
  若主窗口内 Σgap < −$300 且 |Σgap| > |Σ后续日内段|:
    → 向 DEB 提案收紧/废除 W_WEEK 首夜豁免 (影子先行);
  否则维持现状, 仅归档证据。
  主窗口 = exit_time ≥ 2026-07-01 的全部多日持仓 (当前 regime); 全史按月仅作对照。

输出: data/overnight_gap_carry_audit.json + stdout 摘要。
"""

import json
import time
from collections import defaultdict
from pathlib import Path

BASE = Path("/opt/ftseek/FTSeek-US")
DB = BASE / "data" / "ftseek_us.db"
CACHE_DIR = BASE / "data" / "backtest_cache" / "overnight_kline_cache"
OUT = BASE / "data" / "overnight_gap_carry_audit.json"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

PRIMARY_SINCE = "2026-07-01"
GAP_PROPOSE_THRESHOLD = -300.0  # 预注册: Σgap 低于此值且占主导才提案

import sqlite3


def et_date(ts):
    """归一化到 ET 交易日日期。

    数据库存在两种时间戳形态:
      a) 带偏移 '-04:00' → 字符串前 10 位已是 ET 日期;
      b) 无偏移的裸时间戳 (旧数据) → 实为 CST (UTC+8), 减 12h 得 ET。
    直接对裸时间戳做字符串日期比较会把 'CST 清晨结算' 的同日仓误判成隔夜仓
    (2026-08-25 审计中发现: #976-986 八笔假隔夜, exit 04:01x CST = 前日 ET 收盘)。
    """
    ts = str(ts)
    has_offset = ts[-6] in "+-" and ts[-3] == ":"
    if has_offset:
        return ts[:10]
    import datetime as dt

    d = dt.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S") - dt.timedelta(hours=12)
    return d.date().isoformat()


def load_overnight_trades():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, symbol, direction, entry_time, exit_time,
                  entry_price, exit_price, quantity,
                  realized_pnl, pnl_net, fees_est, exit_reason, market_regime
           FROM trade_records
           WHERE exit_time >= ?
           ORDER BY exit_time""",
        ("2026-06-20",),  # buffer: 入场可早于主窗口
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        e_ts, x_ts = str(r["entry_time"]), str(r["exit_time"])
        e_naive, x_naive = _is_naive(e_ts), _is_naive(x_ts)
        if e_naive and x_naive and e_ts[:10] == x_ts[:10] \
                and "T09:00" <= e_ts[10:16] <= "T17:00" and "T09:00" <= x_ts[10:16] <= "T17:00":
            continue  # 裸 ET 同日仓 (如 #1090 EOG): 两端同日且均在 RTH 内 → 非隔夜
        d0, dx = et_date(e_ts), et_date(x_ts)
        if d0 >= dx or dx < PRIMARY_SINCE:  # 同日仓 或 出场在窗口外 → 不算隔夜
            continue
        t = dict(r)
        t["d0"], t["dX"] = d0, dx
        out.append(t)
    return out


def _is_naive(ts):
    return not (len(ts) > 10 and ts[-6] in "+-" and ts[-3] == ":")


def get_daily_kline(symbol, start, end):
    """Futu 日K, 本地文件缓存。返回 {date: {open, close}}"""
    cache_f = CACHE_DIR / f"{symbol.replace('.', '_')}_{start}_{end}.json"
    if cache_f.exists():
        return json.loads(cache_f.read_text())
    from futu import OpenQuoteContext, RET_OK

    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    ret, k, _ = ctx.request_history_kline(
        symbol, ktype="K_DAY", start=start, end=end, max_count=200
    )
    ctx.close()  # 可能挂起, 由外层 timeout 保护
    if ret != RET_OK:
        raise RuntimeError(f"{symbol} kline fail: {k}")
    out = {}
    for _, r in k.iterrows():
        d = str(r["time_key"])[:10]
        out[d] = {"open": float(r["open"]), "close": float(r["close"])}
    cache_f.write_text(json.dumps(out))
    time.sleep(0.35)
    return out


def decompose(trade, kline):
    """把一笔多日持仓拆成 day0 / 各隔夜 gap / 后续日内。返回 (legs, total, ok)。"""
    sym = trade["symbol"]
    sign = 1.0 if trade["direction"] in ("LONG", "BUY") else -1.0
    qty = float(trade["quantity"])
    sq = sign * qty
    entry_p, exit_p = float(trade["entry_price"]), float(trade["exit_price"])
    d0, dx = trade["d0"], trade["dX"]

    days = sorted(d for d in kline if d0 <= d <= dx)
    # 容忍 entry 日 K 缺失 (停牌等): 必须有 d0 与 dx 才可分解
    if not days or days[0] != d0 or days[-1] != dx or len(days) < 2:
        return None, 0.0, False

    legs = []
    total = 0.0
    # day0: entry → close[d0]
    v0 = (kline[d0]["close"] - entry_p) * sq
    legs.append({"leg": "day0_intraday", "date": d0, "pnl": round(v0, 2)})
    total += v0
    for prev, cur in zip(days, days[1:]):
        g = (kline[cur]["open"] - kline[prev]["close"]) * sq
        legs.append({"leg": "gap", "date": cur, "pnl": round(g, 2)})
        total += g
        if cur < dx:
            v = (kline[cur]["close"] - kline[cur]["open"]) * sq
            legs.append({"leg": "hold_intraday", "date": cur, "pnl": round(v, 2)})
            total += v
        else:  # 出场日: open → exit_price
            v = (exit_p - kline[cur]["open"]) * sq
            legs.append({"leg": "exit_day_to_exit_fill", "date": cur, "pnl": round(v, 2)})
            total += v
    return legs, total, True


def main():
    trades = load_overnight_trades()
    print(f"[audit] 主窗口隔夜仓 {len(trades)} 笔")

    need = sorted({t["symbol"] for t in trades})
    dates = [t["d0"] for t in trades] + [t["dX"] for t in trades]
    lo, hi = min(dates), max(dates)
    print(f"[audit] 符号 {len(need)} 只, 区间 {lo}..{hi}")

    klines = {}
    for s in need:
        klines[s] = get_daily_kline(s, lo, hi)

    results, excluded = [], []
    for t in trades:
        legs, total, ok = decompose(t, klines[t["symbol"]])
        realized = t["realized_pnl"]
        if realized is None:
            excluded.append((t["symbol"], t["id"], "realized_pnl NULL"))
            continue
        diff = abs(total - float(realized))
        if not ok or diff > 2.0:
            excluded.append((t["symbol"], t["id"], f"decomp={total:.2f} vs realized={realized} diff={diff:.2f}"))
            continue
        agg = defaultdict(float)
        for lg in legs:
            agg[lg["leg"]] += lg["pnl"]
        results.append({
            "trade_id": t["id"], "symbol": t["symbol"], "direction": t["direction"],
            "entry": t["d0"], "exit": t["dX"],
            "nights": sum(1 for l in legs if l["leg"] == "gap"),
            "day0": round(agg["day0_intraday"], 2),
            "gaps": round(agg["gap"], 2),
            "hold_intraday": round(agg["hold_intraday"] + agg["exit_day_to_exit_fill"], 2),
            "recon_total": round(total, 2), "realized": round(float(realized), 2),
            "fees": t["fees_est"], "net": t["pnl_net"], "exit_reason": t["exit_reason"],
            "legs": legs,
        })

    S = lambda xs: round(sum(xs), 2)
    rollup = {
        "window": f">= {PRIMARY_SINCE}",
        "n_valid": len(results),
        "n_excluded": len(excluded),
        "excluded": excluded,
        "sum_day0": S([r["day0"] for r in results]),
        "sum_gaps": S([r["gaps"] for r in results]),
        "sum_hold_intraday": S([r["hold_intraday"] for r in results]),
        "sum_realized": S([r["realized"] for r in results]),
        "sum_net": S([r["net"] for r in results if r["net"] is not None]),
        "gap_dominant_loss_trades": sum(
            1 for r in results if r["gaps"] < 0 and abs(r["gaps"]) > abs(r["day0"]) + abs(r["hold_intraday"])
        ),
        "negative_gap_trades": sum(1 for r in results if r["gaps"] < 0),
    }
    by_month = defaultdict(lambda: {"n": 0, "gaps": 0.0, "net": 0.0})
    for r in results:
        m = r["exit"][:7]
        by_month[m]["n"] += 1
        by_month[m]["gaps"] = round(by_month[m]["gaps"] + r["gaps"], 2)
        by_month[m]["net"] = round(by_month[m]["net"] + (r["net"] or 0), 2)
    rollup["by_month"] = dict(by_month)

    # 弱日入场→次日延续假设: day0 方向 与 首个 gap 方向 的一致率 (描述性, n 小仅记录)
    both = [r for r in results if any(l["leg"] == "gap" for l in r["legs"])]
    agree = sum(
        1 for r in both
        if (r["day0"] < 0) == (next(l["pnl"] for l in r["legs"] if l["leg"] == "gap") < 0)
    )
    rollup["day0_sign_vs_first_gap_sign_agree"] = f"{agree}/{len(both)}"

    verdict_propose = (
        rollup["sum_gaps"] < GAP_PROPOSE_THRESHOLD
        and abs(rollup["sum_gaps"]) > abs(rollup["sum_hold_intraday"])
    )
    report = {
        "generated": "2026-08-25",
        "script": __file__,
        "pre_registered_rule": (
            f"Σgap < {GAP_PROPOSE_THRESHOLD} 且 |Σgap|>|Σhold_intraday| → 提案收紧 W_WEEK 首夜豁免"
        ),
        "rollup": rollup,
        "trades": results,
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1))

    print(json.dumps(rollup, ensure_ascii=False, indent=1))
    print(f"\n[verdict] 提案触发 = {verdict_propose}")
    print(f"[out] {OUT}")


if __name__ == "__main__":
    main()
