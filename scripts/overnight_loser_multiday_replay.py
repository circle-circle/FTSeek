#!/usr/bin/env python3
"""DEB-20260815 §34 前置回放: EOD水下仓"多日持有等SL" vs 当日15:52平仓.

用户三问 Q3 追加 (2026-08-15): "隔夜最长两日太少 — 要么等SL, 要么留足时间(约一周)".
本回放裁决两个变体:
  W_SL   : 持有直到 SL 触碰 (daily bar high/low 检测) 或 5 交易日上限收盘
  W_WEEK : 持有直到 EOD 转正落袋 或 SL 触碰 或 5 交易日上限收盘 (smart)
基线: 当日 15:52 PRE_CLOSE 平仓 (exit_price).
费用: 多日持有不增加往返结构 (同一 round trip), delta 为纯价差.
方向: LONG delta=s*(exit-baseline_close)*qty; SHORT s=-1.

SL 来源: trade_records.metadata_json.stop_loss (16/18 有真值);
  缺失 2 笔 (XLF/IWM) 用 vol_bucket 逻辑按 entry-day ATR14 现算 (生产同款公式).
已知局限: 日bar粒度, SL触碰按当日极值近似 (open gap-through 单独处理);
  EOD转正判定用收盘价近似.
"""
import json, sqlite3
from pathlib import Path
from collections import defaultdict

REPO = Path(__file__).resolve().parents[1]
CACHE = REPO / "data" / "replay_bars"
DB = REPO / "data" / "ftseek_us.db"
MIN = "2026-06-01"
MAX_HOLD_DAYS = 5  # 约一周交易日

# vol_bucket SL 计算 (生产同款, instruction_generator.py:168-204)
VOL_BUCKETS = {  # short_horizon, config/playbook_horizon.yaml:387
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


def atr14_pct(bars, idx):
    """ATR14 占收盘价比例 (entry-day 之前 14 根)."""
    if idx < 15:
        return None
    trs = []
    for k in range(idx - 14, idx):
        o, h, l, c = bars[k][1], bars[k][2], bars[k][3], bars[k][4]
        pc = bars[k - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return (sum(trs) / len(trs)) / bars[idx - 1][4] if bars[idx - 1][4] else None


def load_bars(symbol):
    best = []
    for f in CACHE.glob(f"daily_{symbol.replace('.', '_')}_*.json"):
        try:
            b = json.loads(f.read_text())
        except Exception:
            continue
        if len(b) > len(best):
            best = b
    return best


def simulate_multiday(bars, i, direction, entry_price, sl_price, qty, max_days):
    """从 exit_day (bars[i]) 收盘后开始, 模拟最多 max_days 个交易日.
    返回 (exit_price, exit_day_offset, exit_tag)."""
    s = 1.0 if direction == "LONG" else -1.0
    for d in range(1, max_days + 1):
        j = i + d
        if j >= len(bars):
            # bars 不够 → 用最后一根收盘兜底
            return bars[-1][4], d - 1, "bars_exhausted"
        o, h, l, c = bars[j][1], bars[j][2], bars[j][3], bars[j][4]
        # gap-through: 开盘已越 SL
        if direction == "LONG" and o <= sl_price:
            return o, d, "SL_gap"
        if direction == "SHORT" and o >= sl_price:
            return o, d, "SL_gap"
        # 盘中 SL 触碰
        if direction == "LONG" and l <= sl_price:
            return sl_price, d, "SL_touch"
        if direction == "SHORT" and h >= sl_price:
            return sl_price, d, "SL_touch"
        # EOD 转正 → 落袋 (W_WEEK 语义)
        if s * (c - entry_price) > 0:
            return c, d, "EOD_profit"
    # 到上限仍水下 → 最后一日收盘强平
    return bars[i + max_days][4], max_days, "max_hold"


def main():
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
    print(f"═══ 样本: EOD水下强平仓 n={len(rows)} ({MIN}起) ═══\n")

    _bc = {}
    w_sl, w_week = [], []
    detail = []
    skipped = defaultdict(int)

    for r in rows:
        sym = r["symbol"]
        if sym not in _bc:
            _bc[sym] = load_bars(sym)
        bars = _bc[sym]
        if not bars:
            skipped["no_bars"] += 1
            continue
        days = [b[0] for b in bars]
        ex_date = r["exit_time"][:10]
        if ex_date not in days:
            skipped["exit_day_missing"] += 1
            continue
        i = days.index(ex_date)
        if i + 1 >= len(bars):
            skipped["no_next_day"] += 1
            continue

        entry_price = float(r["entry_price"])
        qty = float(r["quantity"] or 1)
        direction = r["direction"]
        s = 1.0 if direction == "LONG" else -1.0
        baseline_close = float(r["exit_price"])  # 当日15:52平仓价

        # SL: metadata 优先, 缺失用 vol_bucket 现算
        sl_price = None
        try:
            md = json.loads(r["metadata_json"] or "{}")
            sl_price = float(md.get("stop_loss") or 0) or None
        except Exception:
            pass
        sl_src = "meta"
        if not sl_price:
            ap = atr14_pct(bars, i)
            if ap:
                sl_pct = compute_sl_pct(ap, direction)
                if sl_pct:
                    sl_price = entry_price * (1 - sl_pct / 100) if direction == "LONG" \
                        else entry_price * (1 + sl_pct / 100)
                    sl_src = "volbucket"
        if not sl_price:
            skipped["no_sl"] += 1
            continue

        # W_SL: 等SL (不做 EOD 落袋, 纯等SL或上限)
        # 为对比, W_SL 用同一 simulate 但不做 EOD_profit 早退 → 用 max_hold 逻辑
        # 这里直接调 simulate (含 EOD_profit) 得 W_WEEK; W_SL 单独模拟
        exit_w, off_w, tag_w = simulate_multiday(bars, i, direction, entry_price, sl_price, qty, MAX_HOLD_DAYS)
        # W_SL: 只有 SL 或上限, 无 EOD 落袋
        exit_sl, off_sl, tag_sl = None, None, None
        for d in range(1, MAX_HOLD_DAYS + 1):
            j = i + d
            if j >= len(bars):
                exit_sl, off_sl, tag_sl = bars[-1][4], d - 1, "bars_exhausted"
                break
            o, h, l, c = bars[j][1], bars[j][2], bars[j][3], bars[j][4]
            if direction == "LONG" and o <= sl_price:
                exit_sl, off_sl, tag_sl = o, d, "SL_gap"; break
            if direction == "SHORT" and o >= sl_price:
                exit_sl, off_sl, tag_sl = o, d, "SL_gap"; break
            if direction == "LONG" and l <= sl_price:
                exit_sl, off_sl, tag_sl = sl_price, d, "SL_touch"; break
            if direction == "SHORT" and h >= sl_price:
                exit_sl, off_sl, tag_sl = sl_price, d, "SL_touch"; break
        if exit_sl is None:
            exit_sl, off_sl, tag_sl = bars[i + MAX_HOLD_DAYS][4], MAX_HOLD_DAYS, "max_hold"

        d_sl = s * (exit_sl - baseline_close) * qty
        d_wk = s * (exit_w - baseline_close) * qty
        w_sl.append(d_sl)
        w_week.append(d_wk)
        detail.append({
            "date": ex_date, "symbol": sym, "direction": direction,
            "day_pnl": float(r["pnl_net"]), "sl": sl_price, "sl_src": sl_src,
            "w_sl_delta": d_sl, "w_sl_days": off_sl, "w_sl_tag": tag_sl,
            "w_week_delta": d_wk, "w_week_days": off_w, "w_week_tag": tag_w,
        })

    n = len(w_sl)
    print(f"可回放 n={n} | 跳过: {dict(skipped)}\n")
    if n == 0:
        print("样本不足")
        return

    print(f"═══ 反事实: EOD水下仓多日持有 (上限{MAX_HOLD_DAYS}交易日) vs 当日平仓 ═══")
    for k, v in (("W_SL  等SL/上限", w_sl), ("W_WEEK 落袋+SL/上限", w_week)):
        wins = sum(1 for x in v if x > 0)
        print(f"  {k:<22}: Σdelta=${sum(v):+8.0f}  均笔${sum(v)/n:+7.2f}  "
              f"修复率={wins/n*100:5.1f}%")

    # 退出原因分布
    print("\n═══ W_WEEK 退出原因分布 ═══")
    tags = defaultdict(list)
    for d in detail:
        tags[d["w_week_tag"]].append(d["w_week_delta"])
    for t, v in sorted(tags.items()):
        print(f"  {t:<14}: n={len(v)}  Σ${sum(v):+8.0f}")

    print("\n═══ 分方向 (W_WEEK) ═══")
    for dirn in ("LONG", "SHORT"):
        sub = [d for d in detail if d["direction"] == dirn]
        if sub:
            print(f"  {dirn:<6} n={len(sub)}  Σ${sum(x['w_week_delta'] for x in sub):+8.0f}")

    print("\n═══ 逐笔明细 (W_WEEK) ═══")
    for d in detail:
        print(f"  {d['date']} {d['symbol']:<10} {d['direction']:<6} "
              f"当日${d['day_pnl']:+7.1f} SL={d['sl']:>8.2f}({d['sl_src']}) "
              f"→ 持{d['w_week_days']}天[{d['w_week_tag']:<11}] Δ${d['w_week_delta']:+7.1f}")

    out = REPO / "data" / "overnight_loser_multiday_2026-08-15.json"
    out.write_text(json.dumps({
        "sample_n": len(rows), "replayed_n": n, "skipped": dict(skipped),
        "max_hold_days": MAX_HOLD_DAYS,
        "W_SL_total": sum(w_sl), "W_WEEK_total": sum(w_week),
        "detail": detail,
    }, indent=1, ensure_ascii=False))
    print(f"\n落盘: {out}")


if __name__ == "__main__":
    main()
