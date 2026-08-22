#!/usr/bin/env python3
"""趋势条件化隔夜豁免 — 回放验证 + 趋势判断精准度体检 (蓝图 P1 前置, 用户令"先做回放").

回答两个问题:
  A. weekly_bias 这个判据本身准不准? — 翻转矩阵 / 5日持有期内翻转率 / 分状态前瞻T+5收益。
     判据无内容或日内乱翻, 门就建在流沙上 (用户担忧的直接检验)。
  B. 把隔夜豁免从"方向盲"改为"LONG且bias=up才豁免, SHORT永不豁免",
     在真实 EOD 水下强平队列上比现行 W_WEEK 好多少?

口径对齐 overnight_loser_multiday_replay.py (同队列同SL来源同模拟器),
bias 复用 trend_state_recorder 的生产公式按"截至当日bars"计算 — 零未来函数。

预注册边界: 描述性证据供 P1 门设计参考, 不构成启停判据; 日bar粒度, SL触碰为近似;
费用两臂同结构 (同一往返), Δ 为纯价差不受费用影响。

回滚: 删本文件 + data/trend_gate_replay_*.json。
"""
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import importlib.util

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

_spec = importlib.util.spec_from_file_location(
    "tsr", REPO / "scripts" / "trend_state_recorder.py")
tsr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tsr)

DB = REPO / "data" / "ftseek_us.db"
DAILY_DIR = REPO / "data" / "replay_bars_daily"
MIN = "2026-06-01"
MAX_HOLD_DAYS = 5


def load_bars(symbol):
    cp = DAILY_DIR / f"{symbol.replace('.', '_')}.json"
    if cp.exists():
        try:
            rows = json.loads(cp.read_text())
            if rows:
                return rows
        except Exception:
            pass
    return tsr._load_bars(symbol, allow_fetch=True)


def bias_asof(bars, end_idx=None):
    """生产公式, 只用 bars[:end_idx+1] — 截至当日收盘, 无未来函数。"""
    sub = bars if end_idx is None else bars[:end_idx + 1]
    b, _ = tsr.compute_weekly_bias(sub)
    return b


# ══ A. 趋势判据精准度体检 ══════════════════════════════════════════════

def part_a():
    files = sorted(DAILY_DIR.glob("US_*.json"))
    trans = defaultdict(lambda: defaultdict(int))
    fwd = defaultdict(list)            # bias -> [T+5 close 收益%]  (多头视角)
    # 起始状态 × 持有天数 -> [flip, total] (必须按状态分桶, 否则打印的是全局混合率)
    hold_flips = {s: {d: [0, 0] for d in range(1, MAX_HOLD_DAYS + 1)} for s in ("up", "down", "flat")}
    n_sym = 0
    for f in files:
        try:
            bars = json.loads(f.read_text())
        except Exception:
            continue
        if not bars or len(bars) < tsr.BARS_MIN + MAX_HOLD_DAYS + 1:
            continue
        n_sym += 1
        start = tsr.BARS_MIN - 1
        for idx in range(start, len(bars) - MAX_HOLD_DAYS - 1):
            b0 = bias_asof(bars, idx)
            if b0 in ("insufficient_bars",):
                continue
            b1 = bias_asof(bars, idx + 1)
            trans[b0][b1] += 1
            c0, c5 = bars[idx][4], bars[idx + MAX_HOLD_DAYS][4]
            if c0:
                fwd[b0].append((c5 / c0 - 1) * 100)
            # 持有期内翻转率: 从 b0 出发, 第 d 天的 bias 是否仍 = b0 (按起始状态分桶)
            for d in range(1, MAX_HOLD_DAYS + 1):
                bd = bias_asof(bars, idx + d)
                hold_flips[b0][d][1] += 1
                if bd != b0:
                    hold_flips[b0][d][0] += 1

    print("═══ A. weekly_bias 判据精准度 (universe=%d 标的) ═══" % n_sym)
    states = ["up", "down", "flat"]
    tot_by_state = {s: sum(trans[s].values()) for s in states}
    print("\n[翻转矩阵] 行=今日状态, 列=次日状态 (概率):")
    header = "  " + "".join(f"{s:>10}" for s in states)
    for s in states:
        row_tot = tot_by_state[s]
        if not row_tot:
            continue
        cells = "".join(f"{trans[s][t] / row_tot * 100:>9.1f}%" for t in states)
        print(f"{s:>8}{cells}   (n={row_tot})")
    print("\n[5日持有期状态保持率] (从某状态出发, 第d天仍同状态的概率):")
    for s in states:
        if not tot_by_state[s] or not hold_flips[s][1][1]:
            continue
        keep = [f"d{d}:{(1 - hold_flips[s][d][0] / hold_flips[s][d][1]) * 100:.0f}%"
                for d in range(1, MAX_HOLD_DAYS + 1)]
        print(f"  {s:<6}: {' '.join(keep)}")
    print("\n[分状态前瞻T+5收益%] (close→close, 多头视角; 判据是否有内容的直接检验):")
    for s in states:
        v = fwd.get(s) or []
        if len(v) >= 30:
            print(f"  {s:<6}: n={len(v)}  mean={statistics.mean(v):+.3f}  "
                  f"median={statistics.median(v):+.3f}  hit={(sum(1 for x in v if x > 0)) / len(v) * 100:.1f}%")
        elif v:
            print(f"  {s:<6}: n={len(v)} (样本<30不下结论)")
    return trans, fwd, hold_flips


# ══ B. 趋势门政策对比回放 ══════════════════════════════════════════════

def simulate_week(bars, i, direction, entry_price, sl_price, max_days,
                  daily_bias_check=False):
    """W_WEEK 模拟 (与 overnight_loser_multiday_replay 同款);
    daily_bias_check=True 时每日收盘复检 bias, 非 up 即当日收盘离场 (仅 LONG 有意义)。"""
    s = 1.0 if direction == "LONG" else -1.0
    for d in range(1, max_days + 1):
        j = i + d
        if j >= len(bars):
            return bars[-1][4], d - 1, "bars_exhausted"
        o, h, l, c = bars[j][1], bars[j][2], bars[j][3], bars[j][4]
        if direction == "LONG" and o <= sl_price:
            return o, d, "SL_gap"
        if direction == "SHORT" and o >= sl_price:
            return o, d, "SL_gap"
        if direction == "LONG" and l <= sl_price:
            return sl_price, d, "SL_touch"
        if direction == "SHORT" and h >= sl_price:
            return sl_price, d, "SL_touch"
        if s * (c - entry_price) > 0:
            return c, d, "EOD_profit"
        if daily_bias_check and direction == "LONG" and bias_asof(bars, j) != "up":
            return c, d, "bias_flip_cut"
    return bars[i + max_days][4], max_days, "max_hold"


def vol_bucket_sl(bars, i, direction):
    """与 multiday_replay 同款 vol_bucket SL (metadata 缺失时)。"""

    VOL_BUCKETS = [
        ("low", 0.015, 1.2, 2.0, 2.5, 0.2), ("medium", 0.035, 1.5, 2.5, 4.5, 0.3),
        ("high", 0.08, 1.5, 3.5, 5.0, 0.5), ("extreme", 999.0, 1.2, 3.5, 5.0, 0.8),
    ]
    if i < 15:
        return None
    trs = []
    for k in range(i - 14, i):
        o, h, l, c = bars[k][1], bars[k][2], bars[k][3], bars[k][4]
        pc = bars[k - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = (sum(trs) / len(trs)) / bars[i - 1][4] if bars[i - 1][4] else None
    if not atr:
        return None
    for _name, amax, mult, floor, ceil, swiden in VOL_BUCKETS:
        if atr < amax:
            sl = max(floor, min(ceil, atr * 100 * mult))
            if direction == "SHORT":
                sl += swiden
            return sl
    return None


def part_b():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
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
    print(f"\n═══ B. 政策对比: EOD水下强平队列 n={len(rows)} ({MIN}起) ═══")

    pol = {k: [] for k in ("WEEK", "TREND_ENTRY", "TREND_DAILY")}
    detail = []
    skipped = defaultdict(int)

    for r in rows:
        sym = r["symbol"]
        bars = load_bars(sym)
        if not bars:
            skipped["no_bars"] += 1
            continue
        days = [b[0] for b in bars]
        ex_date = r["exit_time"][:10]
        if ex_date not in days:
            skipped["exit_day_missing"] += 1
            continue
        i = days.index(ex_date)
        if i + MAX_HOLD_DAYS >= len(bars):
            skipped["window_censored"] += 1
            continue

        entry_price = float(r["entry_price"])
        qty = float(r["quantity"] or 1)
        # 脏数据防线: 六月 broker 错位时代有 backfilled 巨量记录 (实测 XLF 22404股/$114万,
        # 真实队列 99 分位 <$15k), 名义超限直接剔除并显式计数
        if entry_price * qty > 50_000:
            skipped["notional_implausible"] += 1
            continue
        direction = r["direction"]
        s = 1.0 if direction == "LONG" else -1.0
        baseline = float(r["exit_price"])

        sl_price = None
        try:
            md = json.loads(r["metadata_json"] or "{}")
            sl_price = float(md.get("stop_loss") or 0) or None
        except Exception:
            pass
        if not sl_price:
            sl_pct = vol_bucket_sl(bars, i, direction)
            if sl_pct:
                sl_price = entry_price * (1 - sl_pct / 100) if direction == "LONG" \
                    else entry_price * (1 + sl_pct / 100)
        if not sl_price:
            skipped["no_sl"] += 1
            continue

        bias0 = bias_asof(bars, i)

        ex_wk, off_wk, tag_wk = simulate_week(bars, i, direction, entry_price, sl_price, MAX_HOLD_DAYS)
        d_wk = s * (ex_wk - baseline) * qty

        # TREND_ENTRY: 决策时刻一次判定 — LONG 且 bias=up 才豁免持夜, 其余当日平仓 (Δ=0)
        if direction == "LONG" and bias0 == "up":
            d_te, off_te, tag_te = d_wk, off_wk, tag_wk
        else:
            d_te, off_te, tag_te = 0.0, 0, "no_exempt"

        # TREND_DAILY: LONG 且起始 up 才进入持有; 期间每日复检, 翻转即当日收盘离场
        if direction == "LONG" and bias0 == "up":
            ex_td, off_td, tag_td = simulate_week(bars, i, direction, entry_price,
                                                  sl_price, MAX_HOLD_DAYS, daily_bias_check=True)
            d_td = s * (ex_td - baseline) * qty
        else:
            d_td, off_td, tag_td = 0.0, 0, "no_exempt"

        for k, dv in (("WEEK", d_wk), ("TREND_ENTRY", d_te), ("TREND_DAILY", d_td)):
            pol[k].append(dv)
        detail.append({"date": ex_date, "symbol": sym, "direction": direction,
                       "bias0": bias0, "week": round(d_wk, 2),
                       "trend_entry": round(d_te, 2), "trend_daily": round(d_td, 2)})

    n = len(pol["WEEK"])
    print(f"可回放 n={n} | 跳过: {dict(skipped)}")
    if not n:
        return
    print(f"\n{'政策':<16}{'Σdelta$':>12}{'均笔$':>10}{'中位数$':>10}{'修复率':>9}{'灾难(<-$200)':>13}")
    for k, v in pol.items():
        wins = sum(1 for x in v if x > 0)
        disasters = sum(1 for x in v if x < -200)
        print(f"{k:<16}{sum(v):>+12.0f}{sum(v) / n:>+10.2f}{statistics.median(v):>+10.2f}"
              f"{wins / n * 100:>8.1f}%{disasters:>13}")
    top = sorted(detail, key=lambda d: -d["week"])[:5]
    print("WEEK臂最大贡献: " + "; ".join(
        f"{d['symbol']}{d['date'][5:]} {d['direction']} bias={d['bias0']} week={d['week']:+.0f}$"
        for d in top))
    print("\n分方向 Σdelta$:")
    for dirn in ("LONG", "SHORT"):
        sub = [d for d in detail if d["direction"] == dirn]
        if sub:
            line = f"  {dirn:<6} n={len(sub)}"
            for k in pol:
                line += f"  {k}={sum(x[k.lower() if k != 'WEEK' else 'week'] for x in sub):+.0f}"
            print(line)
    # TREND 相对 WEEK 的逐笔差分解 (钱从哪来)
    diff = [d["trend_entry"] - d["week"] for d in detail]
    gave_up = sum(x for x in diff if x < 0)
    avoided = sum(x for x in diff if x > 0)
    print(f"\nTREND_ENTRY − WEEK 分解: 放弃的盈利={gave_up:+.0f}$, 避开的亏损={avoided:+.0f}$, 净={sum(diff):+.0f}$")

    out = REPO / "data" / "trend_gate_replay_latest.json"
    out.write_text(json.dumps({
        "generated_note": "描述性回放, 预注册边界内嵌于脚本头; 非启停判据",
        "since": MIN, "max_hold_days": MAX_HOLD_DAYS,
        "cohort_n": len(rows), "replayed_n": n, "skipped": dict(skipped),
        "policies": {k: {"total": round(sum(v), 2), "mean": round(sum(v) / n, 2),
                         "repair_rate": round(sum(1 for x in v if x > 0) / n, 3)}
                     for k, v in pol.items()},
        "detail": detail,
    }, indent=1, ensure_ascii=False))
    print(f"\n落盘: {out}")


if __name__ == "__main__":
    part_a()
    part_b()
