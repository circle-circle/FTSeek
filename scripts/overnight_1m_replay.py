#!/usr/bin/env python3
"""RFC-067 后续裁决回放: 1分K粒度重放"隔夜水下仓多日持有" — 解决日K同日内序歧义.

样本: 同前序回放 (6/1起 EOD水下强平仓, PRE_CLOSE_EXIT pnl_net<=0, n=15).
臂 (基线=当日15:52平仓):
  W_WEEK_1M : 转正EOD(15:52 bar)落袋 / SL / 5日上限 (1分K精化现行口径)
  W_DD08    : + peak回撤0.8%锁利 (因子default drawdown_from_peak_pct)
  W_DD05    : + profit_override档 (peak≥1.5%强制0.5%回撤锁利, 与0.8取紧者)
  W_DD08_BE : DD08 + 保本地板 (peak>0.4%武装后锁利线不低于 entry+0.1%费垫)
1分K内序约定: 每根1M bar 用"截至上一bar的peak"算锁利线先判退出, 再更新peak
  (bar内先后序仍不可知, 但歧义从"整日"缩到"1分钟", 忽略不计).
数据: Futu request_history_kline K_1M autype=NONE, 缓存 data/replay_1m/.
"""
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "ftseek_us.db"
CACHE1M = REPO / "data" / "replay_1m"
CACHE1M.mkdir(exist_ok=True)
MIN = "2026-06-01"
MAX_HOLD_DAYS = 5
BE_AT = 0.4
PO_AT = 1.5
BE_PAD = 0.1
PRE_CLOSE_HHMM = "15:52"

VOL_BUCKETS = {
    "low":     {"atr_pct_max": 0.015, "sl_multiplier": 1.2, "sl_floor_pct": 2.0, "sl_ceil_pct": 2.5, "short_widen_pct": 0.2},
    "medium":  {"atr_pct_max": 0.035, "sl_multiplier": 1.5, "sl_floor_pct": 2.5, "sl_ceil_pct": 4.5, "short_widen_pct": 0.3},
    "high":    {"atr_pct_max": 0.08,  "sl_multiplier": 1.5, "sl_floor_pct": 3.5, "sl_ceil_pct": 5.0, "short_widen_pct": 0.5},
    "extreme": {"atr_pct_max": 999.0, "sl_multiplier": 1.2, "sl_floor_pct": 3.5, "sl_ceil_pct": 5.0, "short_widen_pct": 0.8},
}


def compute_sl_pct(atr_pct, direction):
    for name in sorted(VOL_BUCKETS, key=lambda k: VOL_BUCKETS[k]["atr_pct_max"]):
        b = VOL_BUCKETS[name]
        if atr_pct < b["atr_pct_max"]:
            sl = max(b["sl_floor_pct"], min(b["sl_ceil_pct"], atr_pct * 100 * b["sl_multiplier"]))
            if direction == "SHORT":
                sl += b["short_widen_pct"]
            return sl
    return None


def load_samples():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT DISTINCT t.id, t.symbol, t.direction, t.entry_time, t.exit_time,
               t.entry_price, t.exit_price, t.quantity, t.pnl_net, t.metadata_json
        FROM trade_records t
        WHERE t.exit_reason = 'PRE_CLOSE_EXIT' AND t.pnl_net IS NOT NULL
          AND t.pnl_net <= 0 AND t.exit_time >= ?
        ORDER BY t.exit_time
    """, (MIN,)).fetchall()
    con.close()
    return rows


def fetch_1m(qctx, futu_symbol, start_date, end_date):
    """分页拉 1M bars → list of {ts, o, h, l, c}. 幂等缓存."""
    ck = CACHE1M / f"{futu_symbol.replace('.', '_')}_{start_date}_{end_date}.json"
    if ck.exists():
        try:
            return json.loads(ck.read_text())
        except Exception:
            pass
    from futu import KLType, AuType, RET_OK
    bars, page_key = [], None
    while True:
        kwargs = dict(code=futu_symbol, ktype=KLType.K_1M, autype=AuType.NONE,
                      start=start_date, end=end_date, max_count=1000)
        if page_key is not None:
            kwargs["page_req_key"] = page_key  # SDK 10.9: page_req_key (非 page_key)
        ret, data, page_key = qctx.request_history_kline(**kwargs)
        if ret != RET_OK:
            raise RuntimeError(f"history_kline {futu_symbol}: {data}")
        for _, row in data.iterrows():
            bars.append({"ts": str(row["time_key"]), "o": float(row["open"]),
                         "h": float(row["high"]), "l": float(row["low"]),
                         "c": float(row["close"])})
        if page_key is None:
            break
        time.sleep(1.2)  # 历史K线配额 30/30s
    ck.write_text(json.dumps(bars))
    return bars


def group_by_day(bars):
    out = {}
    for b in bars:
        hhmm = b["ts"][11:16]
        if hhmm < "09:30" or hhmm > "16:00":
            continue  # 只保留常规时段: 隔夜臂的 peak 只应计 RTH (15:55尾盘入场的仓
        # 次日盘前价不属于其持仓期; 且锁利武装用 RTH peak 才是实盘语义)
        d = b["ts"][:10]
        out.setdefault(d, []).append(b)
    return out


def trail_lock(entry_price, peak_profit_pct, direction, dd_pct, be_floor=False):
    if direction == "LONG":
        peak = entry_price * (1 + peak_profit_pct / 100)
        lock = peak * (1 - dd_pct / 100)
        if be_floor:
            lock = max(lock, entry_price * (1 + BE_PAD / 100))
        return lock
    peak = entry_price * (1 - peak_profit_pct / 100)
    lock = peak * (1 + dd_pct / 100)
    if be_floor:
        lock = min(lock, entry_price * (1 - BE_PAD / 100))
    return lock


def effective_lock(entry_price, peak_profit_pct, direction, dd_pct, be_floor):
    """实盘同款: 因子0.8% + profit_override 0.5%≥1.5% 取紧者; 未武装返回 None."""
    if peak_profit_pct <= BE_AT:
        return None
    lock = trail_lock(entry_price, peak_profit_pct, direction, dd_pct, be_floor)
    if peak_profit_pct >= PO_AT:
        lock_po = trail_lock(entry_price, peak_profit_pct, direction, 0.5, be_floor)
        lock = max(lock, lock_po) if direction == "LONG" else min(lock, lock_po)
    return lock


def replay_1m(day_bars_list, direction, entry_price, sl_price, max_days, dd_pct=None, be_floor=False):
    """day_bars_list: 按交易日顺序的 bar 列表 (第1个=退出次日). 返回 (exit_price, day_offset, tag)."""
    s = 1.0 if direction == "LONG" else -1.0
    peak_price = entry_price
    for d, bars in enumerate(day_bars_list[:max_days], start=1):
        if not bars:
            continue
        for bi, b in enumerate(bars):
            o, h, l, c = b["o"], b["h"], b["l"], b["c"]
            peak_profit_prev = s * (peak_price - entry_price) / entry_price * 100
            lock = effective_lock(entry_price, peak_profit_prev, direction, dd_pct, be_floor) if dd_pct else None
            refs = [x for x in (sl_price, lock) if x is not None]
            ref = max(refs) if direction == "LONG" else min(refs)
            # gap / 开盘即越线
            if direction == "LONG" and o <= ref:
                return o, d, ("SL_gap" if ref == sl_price else "TRAIL_gap")
            if direction == "SHORT" and o >= ref:
                return o, d, ("SL_gap" if ref == sl_price else "TRAIL_gap")
            # 盘中 SL 优先 (保守)
            if direction == "LONG" and l <= sl_price:
                return sl_price, d, "SL_touch"
            if direction == "SHORT" and h >= sl_price:
                return sl_price, d, "SL_touch"
            # 锁利线
            if lock is not None:
                if direction == "LONG" and l <= lock:
                    return lock, d, "TRAIL_lock"
                if direction == "SHORT" and h >= lock:
                    return lock, d, "TRAIL_lock"
            # 更新 peak (本bar极值)
            if direction == "LONG":
                peak_price = max(peak_price, h)
            else:
                peak_price = min(peak_price, l)
        # 日末: 15:52 bar 价格 (找不到用末bar收盘) 判转正落袋
        eod_price = bars[-1]["c"]
        for b in reversed(bars):
            if b["ts"][11:16] <= PRE_CLOSE_HHMM:
                eod_price = b["c"]
                break
        if s * (eod_price - entry_price) > 0:
            return eod_price, d, "EOD_profit"
    # 到上限仍水下: 最后一日 15:52 价强平
    last = day_bars_list[max_days - 1] if len(day_bars_list) >= max_days else day_bars_list[-1]
    price = last[-1]["c"]
    for b in reversed(last):
        if b["ts"][11:16] <= PRE_CLOSE_HHMM:
            price = b["c"]
            break
    return price, min(max_days, len(day_bars_list)), "max_hold"


def main():
    rows = load_samples()
    print(f"样本 n={len(rows)} — 拉取 1分K (缓存优先)...")
    from futu import OpenQuoteContext
    qctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    cache_daily = {}
    try:
        for r in rows:
            sym = r["symbol"]
            exit_date = r["exit_time"][:10]
            start = exit_date
            end = (datetime.fromisoformat(exit_date) + timedelta(days=9)).strftime("%Y-%m-%d")
            key = (sym, start, end)
            if key in cache_daily:
                continue
            try:
                bars = fetch_1m(qctx, sym, start, end)
                cache_daily[key] = group_by_day(bars)
                print(f"  {sym:<9} {start}→{end}: {len(bars)} bars / {len(cache_daily[key])} 交易日")
            except Exception as e:
                print(f"  {sym}: 拉取失败 {e}")
                cache_daily[key] = None
            time.sleep(1.2)
    finally:
        qctx.close()

    # 重放
    arms = {k: [] for k in ("W_WEEK_1M", "W_DD08", "W_DD05", "W_DD08_BE")}
    detail = []
    for r in rows:
        sym = r["symbol"]
        exit_date = r["exit_time"][:10]
        start = exit_date
        end = (datetime.fromisoformat(exit_date) + timedelta(days=9)).strftime("%Y-%m-%d")
        by_day = cache_daily.get((sym, start, end))
        if not by_day:
            print(f"  [skip] {sym} 无1分K")
            continue
        hold_days = [b for b in sorted(by_day) if b > exit_date]
        day_bars_list = [by_day[d] for d in hold_days[:MAX_HOLD_DAYS]]
        if not day_bars_list:
            print(f"  [skip] {sym} 无后续交易日")
            continue
        entry_price = float(r["entry_price"]); qty = float(r["quantity"] or 1)
        direction = r["direction"]
        s = 1.0 if direction == "LONG" else -1.0
        baseline_close = float(r["exit_price"])
        sl_price = None
        try:
            md = json.loads(r["metadata_json"] or "{}")
            sl_price = float(md.get("stop_loss") or 0) or None
        except Exception:
            pass
        if not sl_price:
            print(f"  [skip] {sym} SL元数据缺失 (旧仓时代), 排除出样本")
            continue
        variants = {"W_WEEK_1M": dict(), "W_DD08": dict(dd_pct=0.8),
                    "W_DD05": dict(dd_pct=0.8),  # DD05语义=PO档0.5, 在effective_lock里取紧
                    "W_DD08_BE": dict(dd_pct=0.8, be_floor=True)}
        tags = {}
        for k, kw in variants.items():
            if k == "W_DD05":
                ex, off, tg = replay_1m(day_bars_list, direction, entry_price, sl_price,
                                        MAX_HOLD_DAYS, dd_pct=0.5)
            else:
                ex, off, tg = replay_1m(day_bars_list, direction, entry_price, sl_price,
                                        MAX_HOLD_DAYS, **kw)
            d = s * (ex - baseline_close) * qty
            arms[k].append(d)
            tags[k] = (round(d, 1), tg, off, round(ex, 2))
        detail.append((exit_date, sym, direction, tags, entry_price, qty))

    n = len(arms["W_WEEK_1M"])
    print(f"\n═══ 1分K重放 n={n} ═══")
    print(f"{'臂':<11} {'Σdelta':>8} {'均笔':>8} {'胜率':>7} {'最差':>9}")
    for k in ("W_WEEK_1M", "W_DD08", "W_DD05", "W_DD08_BE"):
        v = arms[k]
        if not v:
            continue
        wins = sum(1 for x in v if x > 0)
        print(f"{k:<11} {sum(v):+8.0f} {sum(v)/n:+8.2f} {wins/n*100:6.1f}% {min(v):+9.1f}")
    print("\n逐笔:")
    for dt, sym, drn, t, ep, qty in detail:
        print(f"  {dt} {sym:<9} {drn:<5} entry={ep:<8.2f} qty={qty:<5}")
        for k in ("W_WEEK_1M", "W_DD08", "W_DD05", "W_DD08_BE"):
            d, tg, off, px = t[k]
            print(f"      {k:<10} ${d:+8.1f} [{tg:<11} {off}d] @ {px}")
    out = REPO / "data" / "overnight_1m_replay_2026-08-27.json"
    out.write_text(json.dumps({"n": n,
                               "totals": {k: round(sum(v), 1) for k, v in arms.items()},
                               "detail": [{"date": dt, "symbol": sym, "direction": drn,
                                           "entry": ep, "qty": qty, "arms": t}
                                          for dt, sym, drn, t, ep, qty in detail]},
                              indent=1, ensure_ascii=False))
    print(f"\n落盘: {out}")


if __name__ == "__main__":
    main()
