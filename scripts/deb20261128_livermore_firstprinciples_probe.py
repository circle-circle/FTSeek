#!/usr/bin/env python3
"""DEB-20261128-livermore-firstprinciples-strategy-review 只读复现探针.

只读承诺: sqlite `mode=ro`, 不写库 / 不下单 / 不改任何文件 / 不 import app.*
(避免 db_utils 全局 sqlite3.connect monkey-patch 副作用; 与 scripts/deb20261030_*_replay.py
同款约定)。本脚本逐字复现 DEB 正文的诊断表, 供独立核对。

口径 (与 docs/ADJUDICATED-CRITERIA.md 一致):
  A10 现役时代谓词 = app/core/db_utils.ERA_CLEAN_PERFORMANCE_WHERE 逐字副本
      (trade_date>=2026-07-01 ∧ data_quality∈{clean,real} ∧ source_type∉{POSITION_SYNC,MANUAL});
      此处硬编码以避免引入 app 依赖, 字面与 SSOT 一致 (改边界=重裁)。
  A9  首跑测试批排除 = COALESCE(json_extract(metadata_json,'$.exclusion_reason'),'')=''。
  人群 = source_type='PLAYBOOK_SIM' ∧ exit_price>0 ∧ 净额可测; DA = decision_id LIKE 'DA-%'。

单位合同 (新字段基数+单位律):
  pnl_pct / net_pnl_pct = 分数 (0.0085 = 0.85%); mfe_pct / mae_pct = 百分数 (1.6 = 1.6%)。
  capture% = pnl_pct×100 / mfe_pct ×100; giveback(pp) = mfe_pct − pnl_pct×100。
  netbps = 10000 × Σ净额 / Σ入场名义额 (名义加权); 名义额 = entry_price×quantity。

局限: SIM _only, fees_est 为佣金/监管费估算 (借券/资金/数据/模型成本未计), 非实盘盈利证明。
复现: PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. ./venv/bin/python \
        scripts/deb20261128_livermore_firstprinciples_probe.py
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")

# A10 SSOT 逐字副本 (app/core/db_utils.ERA_CLEAN_PERFORMANCE_WHERE)。
ERA = (
    "AND trade_date >= '2026-07-01' "
    "AND COALESCE(data_quality,'') IN ('clean','real') "
    "AND COALESCE(source_type,'') NOT IN ('POSITION_SYNC','MANUAL')"
)
# A9 首跑测试批排除。
A9 = "AND COALESCE(json_extract(metadata_json,'$.exclusion_reason'),'')=''"


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fetch(conn, where: str):
    q = (
        "SELECT * FROM trade_records WHERE source_type='PLAYBOOK_SIM' "
        "AND exit_price>0 AND COALESCE(pnl_net,realized_pnl) IS NOT NULL "
        f"{ERA} {A9} {where}"
    )
    return [dict(r) for r in conn.execute(q)]


def _et(ts):
    """把库内混存偏移 (−04/−05/+08/无) 的 ISO 时戳统一转 America/New_York.

    无偏移的朴素时戳按 ET 解释 (库内 ET 锚); 解析失败返回 None。
    避免 B23 警示的「写死 −4h 让 11 月行偏 1 小时」陷阱。
    """
    s = str(ts or "")
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ET)
    return dt.astimezone(ET)


def stats(rs, label):
    n = len(rs)
    if n == 0:
        print(f"{label}: n=0")
        return
    net = [_f(r["pnl_net"]) for r in rs if _f(r["pnl_net"]) is not None]
    gross = [_f(r["realized_pnl"]) for r in rs if _f(r["realized_pnl"]) is not None]
    notional = sum(
        _f(r["entry_price"]) * _f(r["quantity"])
        for r in rs
        if _f(r["entry_price"]) and _f(r["quantity"])
    )
    wins = [x for x in net if x > 0]
    losses = [x for x in net if x <= 0]
    wr = len(wins) / n * 100
    aw = st.mean(wins) if wins else 0.0
    al = st.mean(losses) if losses else 0.0
    pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else float("inf")
    payoff = abs(aw / al) if al else 0.0
    print(
        f"{label}: n={n} net=${sum(net):.0f} gross=${sum(gross):.0f} "
        f"notional=${notional:.0f} netbps={10000 * sum(net) / notional:.2f} "
        f"WR={wr:.1f}% avgWin=${aw:.2f} avgLoss=${al:.2f} PF={pf:.2f} payoff={payoff:.2f}"
    )


def capture_block(rs, label):
    byexit = defaultdict(list)
    for r in rs:
        byexit[r["exit_reason"]].append(r)
    print(f"-- MFE capture / giveback ({label}; 正确单位) --")
    for k in ("SL_TRAIL_LOCK", "SL_BREAKEVEN", "PRE_CLOSE_EXIT", "TAKE_PROFIT", "SL_CERT_LOCK"):
        v = byexit.get(k, [])
        caps, give, mfes = [], [], []
        for r in v:
            mfe = _f(r["mfe_pct"])
            pp = _f(r["pnl_pct"])
            if mfe and mfe > 0.05 and pp is not None:
                realized_pct = pp * 100
                caps.append(realized_pct / mfe * 100)
                give.append(mfe - realized_pct)
                mfes.append(mfe)
        if caps:
            print(
                f"  {k}: n={len(v)} medMFE={st.median(mfes):.2f}% "
                f"capture_med={st.median(caps):.0f}% capture_mean={st.mean(caps):.0f}% "
                f"giveback_med={st.median(give):.2f}pp giveback_mean={st.mean(give):.2f}pp"
            )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=str(ROOT / "data" / "ftseek_us.db"))
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    da_nov = fetch(conn, "AND trade_date>='2026-11-01' AND decision_id LIKE 'DA-%'")
    da_all = fetch(conn, "AND decision_id LIKE 'DA-%'")

    print("======= DA NOV (>=2026-11-01, A9/A10) =======")
    stats(da_nov, "DA-Nov")
    print("--- by direction ---")
    for d in ("LONG", "SHORT"):
        stats([r for r in da_nov if r["direction"] == d], f"  {d}")
    print("--- by exit_reason (按净额升序) ---")
    byexit = defaultdict(list)
    for r in da_nov:
        byexit[r["exit_reason"]].append(r)
    for k, v in sorted(byexit.items(), key=lambda kv: sum(_f(r["pnl_net"]) or 0 for r in kv[1])):
        stats(v, f"  {k}")
    capture_block(da_nov, "Nov")

    print("--- holding hours buckets ---")
    for lo, hi in [(0, 1), (1, 4), (4, 8), (8, 24), (24, 200)]:
        stats(
            [r for r in da_nov if _f(r["holding_hours"]) is not None and lo <= _f(r["holding_hours"]) < hi],
            f"  {lo}-{hi}h",
        )

    print("--- entry hour ET buckets (zoneinfo 统一转 ET) ---")
    byhour = defaultdict(list)
    for r in da_nov:
        et = _et(r["entry_time"])
        if et is not None:
            byhour[f"{et.hour:02d}"].append(r)
    for h in sorted(byhour):
        stats(byhour[h], f"  hour {h}")

    print("--- entry minute band ET (09:30-09:45 vs rest) ---")
    early, rest = [], []
    for r in da_nov:
        et = _et(r["entry_time"])
        if et is None:
            continue
        hm = f"{et.hour:02d}:{et.minute:02d}"
        (early if "09:30" <= hm <= "09:45" else rest).append(r)
    stats(early, "  09:30-09:45")
    stats(rest, "  after 09:45 / other")

    print("--- LONG/SHORT overnight vs intraday (ET 日期跨夜) ---")
    for d in ("LONG", "SHORT"):
        sub = [r for r in da_nov if r["direction"] == d]
        ov, intra = [], []
        for r in sub:
            e, x = _et(r["entry_time"]), _et(r["exit_time"])
            if e is None or x is None:
                continue
            (ov if e.date() < x.date() else intra).append(r)
        stats(ov, f"  {d} overnight")
        stats(intra, f"  {d} intraday")

    print("--- market_regime distribution ---")
    byreg = defaultdict(list)
    for r in da_nov:
        byreg[r["market_regime"]].append(r)
    for k, v in sorted(byreg.items(), key=lambda kv: -len(kv[1])):
        net = sum(_f(r["pnl_net"]) or 0 for r in v)
        print(f"  {k}: n={len(v)} net=${net:.0f}")

    print()
    print("======= DA ALL ERA (>=2026-07-01, A9/A10) =======")
    stats(da_all, "DA-all")
    print("--- by exit_reason (按净额升序) ---")
    byexit2 = defaultdict(list)
    for r in da_all:
        byexit2[r["exit_reason"]].append(r)
    for k, v in sorted(byexit2.items(), key=lambda kv: sum(_f(r["pnl_net"]) or 0 for r in kv[1])):
        stats(v, f"  {k}")
    capture_block(da_all, "AllEra")

    tp_hit = [r for r in da_all if _f(r["mfe_pct"]) and _f(r["mfe_pct"]) >= 7.0]
    print(f"--- TP reachability: MFE>=7% = {len(tp_hit)}/{len(da_all)} ---")
    dead = [r for r in da_all if _f(r["mfe_pct"]) is not None and _f(r["mfe_pct"]) < 0.3]
    alive = [r for r in da_all if _f(r["mfe_pct"]) is not None and _f(r["mfe_pct"]) >= 0.3]
    stats(dead, "  DEAD (mfe<0.3)")
    stats(alive, "  ALIVE (mfe>=0.3)")

    tl = [r for r in da_all if r["exit_reason"] == "SL_TRAIL_LOCK"]
    for thr in (2.0, 3.0, 5.0):
        big = [r for r in tl if _f(r["mfe_pct"]) and _f(r["mfe_pct"]) >= thr]
        caps = [
            (_f(r["pnl_pct"]) * 100) / _f(r["mfe_pct"]) * 100
            for r in big
            if _f(r["pnl_pct"]) is not None
        ]
        capstr = f" capture_med={st.median(caps):.0f}%" if caps else ""
        print(f"  trail_lock MFE>={thr:.0f}%: {len(big)}/{len(tl)}{capstr}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
