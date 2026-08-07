#!/usr/bin/env python3
"""盘中再入场 T+1 评估 (只读, 不碰任何交易逻辑)。

回答一个问题: 再入场生成器 (app/playbook/intraday_reentry.py) 是增益还是噪音 —
  armed 组 (真实成交的再入场单, 毛/净双列) vs rejected 组 (被拒候选的反事实轨迹)。

口径与局限 (务必如实看待):
  - armed 成交单: trade_records 真实 realized_pnl / pnl_net (费用已扣), 精确。
  - rejected 反事实: 以"当日首次被拒时点价格"为入场, 轨迹源优先级 ① replay_bars
    本地 1-min 缓存 (data/replay_bars/US_<SYM>_<DATE>.json, 全日完整) ② entry_timing_shadow
    轮询 (注意: 再入场候选标的当日已成交过, 该轮询在首笔成交后结构性地停止 → 多数
    只能靠 ①); 以轨迹最后一根收盘出场 (LAST_BAR 口径)。**未计费用** (~13bps 往返,
    会系统性高估反事实) → 两组不可直接相减, 分别看符号与量级。
    bars 缓存缺失的 symbol-day 标 UNMEASURABLE 排除 (可先跑 gate_a_mfe_replay 补缓存)。
  - armed 但未成交 (门链拦截/未穿越): 单列 armed_no_fill, 不计盈亏。
  - 分桶: 前笔胜者延续 (prior pnl≥0) vs 平推单 (prior pnl<0) — 方向质量分层 (任务#5)。
  - 毕业判据 (预注册, 见数前定): armed n≥30 且净均值>0 且优于 rejected 反事实组,
    才谈"再入场有效"; n<30 一切结论只作方向性参考。

用法:
  ./venv/bin/python scripts/eval_intraday_reentry.py            # 全部样本
  ./venv/bin/python scripts/eval_intraday_reentry.py --days 5   # 最近5个交易日
"""
import argparse
import json
import os
import sqlite3
import statistics as st
import sys
from collections import defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHADOW = os.path.join(_ROOT, "data", "intraday_reentry_shadow.jsonl")
_ENTRY_SHADOW = os.path.join(_ROOT, "data", "entry_timing_shadow.jsonl")
_DB = os.path.join(_ROOT, "data", "ftseek_us.db")


def _norm(sym):
    return str(sym or "").replace("US.US.", "US.").replace("US.", "", 1)


def _load_reentry(days=None):
    rows = []
    if not os.path.exists(_SHADOW):
        return rows
    with open(_SHADOW, encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    if days:
        dates = sorted({r.get("date") for r in rows if r.get("date")})[-days:]
        rows = [r for r in rows if r.get("date") in dates]
    return rows


def _load_trades():
    """(date, norm_symbol) -> [trade dicts] (当日全部 playbook 平仓单, 按时间序)."""
    out = defaultdict(list)
    try:
        c = sqlite3.connect(_DB)
        c.row_factory = sqlite3.Row
        for r in c.execute("""
            SELECT trade_date, symbol, direction, entry_price, exit_price,
                   realized_pnl, pnl_net, pnl_pct, fees_est, exit_reason, entry_time
            FROM trade_records
            WHERE source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE')
              AND exit_price > 0
            ORDER BY id
        """):
            out[(r["trade_date"], _norm(r["symbol"]))].append(dict(r))
        c.close()
    except Exception as e:
        print(f"[warn] 读取 trade_records 失败: {e}", file=sys.stderr)
    return out


def _load_entry_trajectories():
    """(date, norm_symbol) -> [(ts, price)] entry_timing_shadow 轮询轨迹."""
    out = defaultdict(list)
    if not os.path.exists(_ENTRY_SHADOW):
        return out
    with open(_ENTRY_SHADOW, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            ts, p = r.get("ts"), r.get("current_price")
            if ts and p:
                out[(r.get("ts", "")[:10], _norm(r.get("symbol")))].append((ts, float(p)))
    for k in out:
        out[k].sort()
    return out


_BAR_DIR = os.path.join(_ROOT, "data", "replay_bars")


def _load_replay_bars(dates_symbols):
    """(date, norm_symbol) -> [(iso_ts, close)] 本地 1-min bar 缓存 (全日完整, 首选轨迹源).

    文件: data/replay_bars/US_<SYM>_<DATE>.json, 行 [time ET naive, o, h, l, c]
    (gate_a_mfe_replay 同款缓存格式, 原始成交价口径禁前复权)。
    """
    out = {}
    for date, sym in dates_symbols:
        path = os.path.join(_BAR_DIR, f"US_{sym}_{date}.json")
        if not os.path.exists(path):
            continue
        try:
            bars = json.load(open(path, encoding="utf-8"))
            out[(date, sym)] = [(b[0].replace(" ", "T"), float(b[4])) for b in bars]
        except Exception:
            continue
    return out


def _cf_pnl(direction, entry_px, exit_px):
    if entry_px <= 0:
        return None
    long = str(direction).upper() in ("BUY", "LONG")
    return ((exit_px - entry_px) / entry_px * 100) if long else ((entry_px - exit_px) / entry_px * 100)


def _bucket(prior_pnl_pct):
    return "winner_cont" if (prior_pnl_pct or 0) >= 0 else "scratch"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    args = ap.parse_args()

    rows = _load_reentry(args.days)
    if not rows:
        print("无再入场影子数据 (data/intraday_reentry_shadow.jsonl)")
        return 0
    trades = _load_trades()
    traj = _load_entry_trajectories()

    # 每 (symbol,date) 只评首次被拒 (最早的"若入场"时点), 避免 300s 轮询重复计数
    first_rejected = {}
    armed_rows = []
    for r in rows:
        act = r.get("action")
        key = (r.get("date"), _norm(r.get("symbol")), r.get("direction"))
        if act == "rejected":
            if key not in first_rejected or (r.get("ts_et") or "") < (first_rejected[key].get("ts_et") or ""):
                first_rejected[key] = r
        elif act == "armed":
            armed_rows.append(r)

    print(f"样本: shadow 行 {len(rows)} | armed {len(armed_rows)} | 去重 rejected {len(first_rejected)}")

    # ── armed 组: 真实成交结果 (毛/净双列) ──
    arm_stats = defaultdict(list)   # bucket -> [net pnl $]
    arm_gross = defaultdict(list)
    no_fill = 0
    for r in armed_rows:
        key = (r.get("date"), _norm(r.get("symbol")))
        day_trades = trades.get(key, [])
        # 再入场单 = 当日第 2 笔及以后 (首笔是先决条件)
        re_trades = day_trades[1:] if len(day_trades) >= 2 else []
        b = _bucket(r.get("prior_pnl_pct"))
        if not re_trades:
            no_fill += 1
            continue
        for t in re_trades:
            arm_stats[b].append(float(t.get("pnl_net") or 0))
            arm_gross[b].append(float(t.get("realized_pnl") or 0))

    print(f"\n── armed 组 (真实成交, USD; armed但未成交 {no_fill} 条不计) ──")
    for b in ("winner_cont", "scratch"):
        g, n_ = arm_gross[b], arm_stats[b]
        if not n_:
            print(f"  {b}: 无成交样本")
            continue
        wins = sum(1 for x in n_ if x > 0)
        print(f"  {b}: n={len(n_)} 胜{wins} | 毛 合计${sum(g):+.2f} 均值${st.mean(g):+.2f} | "
              f"净 合计${sum(n_):+.2f} 均值${st.mean(n_):+.2f} 中位${st.median(n_):+.2f}")

    # ── rejected 组: 反事实轨迹 (bars 优先 / 轮询兜底, 未计费) ──
    cf_stats = defaultdict(list)
    unmeasurable = 0
    bars = _load_replay_bars({(k[0], k[1]) for k in first_rejected})
    n_bar_src = 0
    for key, r in first_rejected.items():
        date, sym, direction = key
        entry_px = float(r.get("price") or 0)
        poll_ts = (r.get("ts_et") or "")[:19]   # 截到秒, 与 bar ISO 字典序可比
        series = bars.get((date, sym))
        if series:
            n_bar_src += 1
            forward = [(t, p) for t, p in series if t > poll_ts]
        else:
            forward = [(t, p) for t, p in traj.get((date, sym), []) if (t or "")[:19] > poll_ts]
        if len(forward) < 2:
            unmeasurable += 1
            continue
        pnl = _cf_pnl(direction, entry_px, forward[-1][1])
        if pnl is not None:
            cf_stats[_bucket(r.get("prior_pnl_pct"))].append(pnl)

    print(f"\n── rejected 组反事实 (%, 轨迹源 bars={n_bar_src}/轮询={len(first_rejected) - n_bar_src - unmeasurable}, "
          f"未计~13bps费用, 不可测 {unmeasurable} 条已排除) ──")
    for b in ("winner_cont", "scratch"):
        v = cf_stats[b]
        if not v:
            print(f"  {b}: 无可测样本")
            continue
        wins = sum(1 for x in v if x > 0)
        print(f"  {b}: n={len(v)} 正{wins}/负{len(v) - wins} 合计{sum(v):+.2f}% "
              f"均值{st.mean(v):+.3f}% 中位{st.median(v):+.3f}%")

    total_armed = sum(len(v) for v in arm_stats.values())
    print(f"\n── 毕业判据 (预注册): armed 成交 n≥30 且净均值>0 且优于反事实组 ──")
    print(f"  当前 armed 成交 n={total_armed} → {'可评估' if total_armed >= 30 else '样本不足, 一切结论仅方向性'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
