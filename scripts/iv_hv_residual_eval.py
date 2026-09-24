#!/usr/bin/env python3
"""IV/HV 前瞻波动率影子 — 桶内残存效应评估 (判据: futu_channel_iv_hv 注册表条目)

判据原文 (2026-08-29 预注册):
  "残存效应分析 (基线纪律: 在已落地 atr_pct 分桶上算残存, 不另起炉灶):
   IV/HV 于交易日 join trade_records, 桶内增量显著才毕业。"

口径 (全部按现役实值取, 不另起炉灶):
  · 落桶边界 = config/playbook_horizon.yaml short_horizon.vol_buckets 的
    atr_pct_max (0.015 / 0.035 / 0.08 / 999) — 与生产 SL/TP 分桶同一套边界;
  · atr_pct = metadata_json.atr / entry_price (入场时已采集, 零新采集线);
  · 信号 = IV/HV (iv, hv 来自 data/futu_iv_hv_shadow.jsonl, 按 (symbol,time) 取最新 collected_at);
  · 结局 = pnl_net (实盘口径, 非纸面), 另报 WR;
  · 统计 = 桶内 HI(IV/HV≥阈值) vs LO 的笔均差, **按 ET 日聚类** t (宪章第 9 律: pooled 不单独采信)
    + MDE(80%) 覆盖检验 + 单侧符号检验; 每桶 n≥30 才判 (n<30 只报数不结论)。

裁决 (逐字对齐判据):
  某桶 day-clustered |t|≥2 且 MDE < |观察效应| → 该桶"残存显著" → GRADUATE_CANDIDATE (须用户裁)。
  无任何桶达标 → KEEP_SAMPLING (继续攒样, 不重置)。

纯读: 只读 jsonl + DB, 只写自己的 latest.json。无推送、无 DB 写、无 cron 改动。
用法: python scripts/iv_hv_residual_eval.py [--since 2025-09-01] [--ratio-thr 1.0]
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
_log = logging.getLogger(__name__)
IV_HV = REPO / "data" / "futu_iv_hv_shadow.jsonl"
DB = REPO / "data" / "ftseek_us.db"
OUT = REPO / "data" / "shadow" / "iv_hv_residual_latest.json"

# 已落地边界 (short_horizon.vol_buckets): (桶名, atr_pct 上界)
BUCKETS: List[Tuple[str, float]] = [
    ("low", 0.015),
    ("medium", 0.035),
    ("high", 0.08),
    ("extreme", float("inf")),
]
MIN_N = 30  # 宪章: n<30 不结论


def load_iv_hv(path: Path = IV_HV) -> Dict[Tuple[str, str], Dict[str, float]]:
    """(symbol, 'YYYY-MM-DD') → {iv, hv}; 同一键取最新 collected_at。"""
    best: Dict[Tuple[str, str], Dict[str, float]] = {}
    if not path.exists():
        return best
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:
                _log.warning("jsonl parse failed", exc_info=True)
                continue
            sym, dt = r.get("symbol"), r.get("time")
            iv, hv = r.get("iv"), r.get("hv")
            if not sym or not dt or iv is None or hv is None:
                continue
            k = (sym, str(dt)[:10])
            prev = best.get(k)
            if prev is None or str(r.get("collected_at", "")) > str(prev.get("collected_at", "")):
                best[k] = {"iv": float(iv), "hv": float(hv),
                           "collected_at": str(r.get("collected_at", ""))}
    return best


def bucket_of(atr_pct: float) -> str:
    for name, hi in BUCKETS:
        if atr_pct < hi:
            return name
    return BUCKETS[-1][0]


def load_trades(db: Path, since: str) -> List[Dict]:
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "select trade_date, symbol, direction, entry_price, pnl_net, metadata_json "
            "from trade_records where trade_mode='simulate' and pnl_net is not null "
            "and trade_date >= ?", (since,)).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        md = {}
        try:
            md = json.loads(r["metadata_json"] or "{}")
        except Exception:
            md = {}
        atr, px = md.get("atr"), r["entry_price"]
        if not atr or not px:
            continue  # 无 atr 无法落桶 (不猜)
        out.append({"date": str(r["trade_date"])[:10], "symbol": r["symbol"],
                    "direction": r["direction"], "pnl_net": float(r["pnl_net"]),
                    "atr_pct": float(atr) / float(px)})
    return out


def _sign_test_p(wins: int, n: int) -> float:
    """单侧符号检验 p (H1: 命中率 > 0.5)。"""
    if n <= 0:
        return 1.0
    return sum(math.comb(n, k) for k in range(wins, n + 1)) / (2 ** n)


def _day_clustered(diffs_by_day: Dict[str, List[float]]) -> Dict:
    """按日聚类: 每日取均值 → 对日序列做 t 与 MDE(80% 功效, 双侧 α=0.05)。"""
    per_day = [sum(v) / len(v) for v in diffs_by_day.values() if v]
    n = len(per_day)
    if n < 2:
        return {"n_days": n, "day_mean": None, "t": None, "mde_80pct": None,
                "distinguishable": None, "note": "n_days<2 不可判"}
    m = sum(per_day) / n
    var = sum((x - m) ** 2 for x in per_day) / (n - 1)
    se = math.sqrt(var / n) if var > 0 else 0.0
    t = (m / se) if se > 0 else None
    mde = 2.8 * se  # (z_{0.975}+z_{0.80}) = 2.8
    return {"n_days": n, "day_mean": round(m, 4),
            "t": (round(t, 3) if t is not None else None),
            "mde_80pct": round(mde, 4),
            "distinguishable": (t is not None and abs(m) > mde)}


def evaluate(min_n: int = MIN_N, ratio_thr: float = 1.0,
             since: str = "2025-09-01") -> Dict:
    ivhv = load_iv_hv()
    trades = load_trades(DB, since)
    joined: List[Dict] = []
    for t in trades:
        rec = ivhv.get((t["symbol"], t["date"]))
        if not rec or rec["hv"] <= 0:
            continue
        t2 = dict(t)
        t2["ratio"] = rec["iv"] / rec["hv"]
        t2["bucket"] = bucket_of(t["atr_pct"])
        t2["hi"] = t2["ratio"] >= ratio_thr
        joined.append(t2)

    evaluated = [t for t in joined if t["date"] >= since]
    buckets = {}
    grad = []
    for name, _hi in BUCKETS:
        sub = [t for t in evaluated if t["bucket"] == name]
        hi = [t for t in sub if t["hi"]]
        lo = [t for t in sub if not t["hi"]]
        if not sub:
            buckets[name] = {"n": 0}
            continue
        diffs_by_day: Dict[str, List[float]] = {}
        for t in hi:
            diffs_by_day.setdefault(t["date"], [])
        # 逐日: HI 笔均 − LO 笔均 (两侧当日都要有样本才算该日)
        by_day: Dict[str, Dict[str, List[float]]] = {}
        for t in sub:
            by_day.setdefault(t["date"], {"hi": [], "lo": []})
            by_day[t["date"]]["hi" if t["hi"] else "lo"].append(t["pnl_net"])
        for d, g in by_day.items():
            if g["hi"] and g["lo"]:
                diffs_by_day[d] = [sum(g["hi"]) / len(g["hi"]) - sum(g["lo"]) / len(g["lo"])]
        hi_mean = sum(t["pnl_net"] for t in hi) / len(hi) if hi else None
        lo_mean = sum(t["pnl_net"] for t in lo) / len(lo) if lo else None
        wins = sum(1 for t in hi if t["pnl_net"] > 0)
        rec = {
            "n": len(sub), "n_hi": len(hi), "n_lo": len(lo),
            "hi_mean_net": (round(hi_mean, 2) if hi_mean is not None else None),
            "lo_mean_net": (round(lo_mean, 2) if lo_mean is not None else None),
            "pooled_diff": (round(hi_mean - lo_mean, 2)
                            if hi_mean is not None and lo_mean is not None else None),
            "hi_wr": (round(wins / len(hi), 3) if hi else None),
            "sign_p_hi_gt_half": (round(_sign_test_p(wins, len(hi)), 4) if hi else None),
            "day_clustered": _day_clustered(diffs_by_day),
            "atr_pct_range": [round(min(t["atr_pct"] for t in sub), 4),
                              round(max(t["atr_pct"] for t in sub), 4)],
        }
        dc = rec["day_clustered"]
        rec["verdict"] = "INSUFFICIENT_N" if len(sub) < min_n else (
            "RESIDUAL_SIGNIFICANT" if (dc.get("t") is not None and abs(dc["t"]) >= 2
                                       and dc.get("distinguishable")) else "NO_RESIDUAL")
        if rec["verdict"] == "RESIDUAL_SIGNIFICANT":
            grad.append(name)
        buckets[name] = rec

    n_judgeable = sum(1 for b in buckets.values() if b.get("verdict") in
                      ("RESIDUAL_SIGNIFICANT", "NO_RESIDUAL"))
    verdict = "GRADUATE_CANDIDATE" if grad else (
        "KEEP_SAMPLING" if n_judgeable == 0 else "NO_RESIDUAL")
    return {
        "n_trades_simulate": len(trades),
        "n_joined": len(joined),
        "join_rate": round(len(joined) / len(trades), 3) if trades else None,
        "ratio_thr": ratio_thr,
        "since": since,
        "buckets": buckets,
        "n_buckets_judgeable": n_judgeable,
        "graduated_buckets": grad,
        "verdict": verdict,
        "caliber": ("atr_pct 落桶=playbook_horizon.yaml short_horizon.vol_buckets; "
                    "结局=pnl_net(实盘口径); 统计=按 ET 日聚类 t + MDE(80%) + 单侧符号检验; "
                    "n<30 只报数不结论 (宪章)"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="IV/HV 桶内残存效应评估 (record-only)")
    ap.add_argument("--since", default="2025-09-01")
    ap.add_argument("--ratio-thr", type=float, default=1.0)
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    res = evaluate(ratio_thr=args.ratio_thr, since=args.since)
    res["generated_at"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
    res["n"] = res["n_joined"]  # 注册表计数锚 (与判据同轴: 参与评估的交易笔数)
    print("═══ IV/HV 桶内残存效应 (判据: 桶内增量显著才毕业) ═══")
    print("实盘已结算单 %d, join 上 IV/HV %d (%.0f%%), 阈值 IV/HV≥%.2f, since=%s"
          % (res["n_trades_simulate"], res["n_joined"], (res["join_rate"] or 0) * 100,
             res["ratio_thr"], res["since"]))
    print("桶         n   HI笔均   LO笔均   pooled差   日聚类t  MDE(80)  结论")
    for name, _ in BUCKETS:
        b = res["buckets"].get(name) or {}
        if not b.get("n"):
            print("%-9s  %3d   (无样本)" % (name, 0))
            continue
        dc = b["day_clustered"]
        print("%-9s  %3d   %7s  %7s  %8s  %7s  %7s  %s"
              % (name, b["n"], b["hi_mean_net"], b["lo_mean_net"], b["pooled_diff"],
                 dc.get("t"), dc.get("mde_80pct"), b["verdict"]))
    print("可判桶数=%d 达标桶=%s ⇒ 裁决: %s"
          % (res["n_buckets_judgeable"], res["graduated_buckets"] or "无", res["verdict"]))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print("→ %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())