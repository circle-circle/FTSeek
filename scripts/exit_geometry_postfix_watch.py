#!/usr/bin/env python3
"""术后退出几何观察 — 退出几何修复 (P1-5/P1-10/P1-6) 生效前后对照

基线 = PLAYBOOK_SIM 已平仓, 2026-07-01 ≤ trade_date < 2026-08-17 (修复前现役时代,
即 DEB-20260816-long-expectancy §2.4 基线); 术后 = trade_date ≥ 2026-08-17
(8/16 空仓窗口重启后首个交易日)。

只读观察: 输出 data/exit_geometry_watch_latest.json + 控制台对照表。
参考线 (非触发器, 触发器归 DEB 预注册判据):
  - 止损/追踪族 (SL_* / TRAILING_*) 术后单笔均亏收敛至 ≤ $600
  - MFE 捕获率 (avg pnl / avg MFE) 不低于基线

用法: ./venv/bin/python scripts/exit_geometry_postfix_watch.py
"""
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.utils.timezone import now_et  # noqa: E402

DB_PATH = ROOT / "data" / "ftseek_us.db"
OUT = ROOT / "data" / "exit_geometry_watch_latest.json"

BASELINE_START = "2026-07-01"
POSTFIX_START = "2026-08-17"  # 退出几何修复重启 (2026-08-16 空仓窗口) 后首个交易日

STOP_TRAIL_FAMILY = (
    "SL_", "TRAILING_",
)
AVG_LOSS_REF = -600.0  # 止损/追踪族术后单笔均亏参考线 (非触发器)


def _agg(rows):
    a = defaultdict(lambda: {"n": 0, "gross": 0.0, "net": 0.0, "mfe": 0.0, "pnl_pct": 0.0})
    for t in rows:
        key = (t["exit_reason"] or "unknown", t["direction"] or "?")
        s = a[key]
        s["n"] += 1
        s["gross"] += float(t["realized_pnl"] or 0)
        s["net"] += float(t.get("pnl_net") or t.get("realized_pnl") or 0)
        s["mfe"] += float(t.get("mfe_pct") or 0)
        s["pnl_pct"] += float(t.get("pnl_pct") or 0)
    return {
        f"{k[0]}|{k[1]}": {
            "n": v["n"],
            "gross": round(v["gross"], 0),
            "net": round(v["net"], 0),
            "avg_gross": round(v["gross"] / v["n"], 0),
            "avg_mfe_pct": round(v["mfe"] / v["n"] / 100, 4),  # 列为百分数 → 分数
            "capture_ratio": round(
                (v["pnl_pct"] / v["n"]) / (v["mfe"] / v["n"] / 100), 3
            ) if v["mfe"] > 0 else None,
        }
        for k, v in a.items()
    }


def _family_stats(rules: dict, family_prefixes=tuple(STOP_TRAIL_FAMILY)):
    tot_n = tot_gross = 0
    for name, s in rules.items():
        reason = name.split("|")[0]
        if reason.startswith(family_prefixes):
            tot_n += s["n"]
            tot_gross += s["gross"]
    if tot_n == 0:
        return {"n": 0, "gross": round(tot_gross, 0), "avg_gross": None}
    return {"n": tot_n, "gross": round(tot_gross, 0), "avg_gross": round(tot_gross / tot_n, 0)}


def main():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    def _load(lo, hi=None):
        sql = ("SELECT trade_date, direction, exit_reason, realized_pnl, pnl_net, "
               "pnl_pct, mfe_pct FROM trade_records "
               "WHERE source_type='PLAYBOOK_SIM' AND exit_price>0 AND trade_date>=?")
        params = [lo]
        if hi:
            sql += " AND trade_date<?"
            params.append(hi)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    baseline_rows = _load(BASELINE_START, POSTFIX_START)
    postfix_rows = _load(POSTFIX_START)
    conn.close()

    baseline = _agg(baseline_rows)
    postfix = _agg(postfix_rows)
    fam_base = _family_stats(baseline)
    fam_post = _family_stats(postfix)

    flags = []
    if fam_post["n"] > 0 and fam_post["avg_gross"] is not None:
        if fam_post["avg_gross"] < AVG_LOSS_REF:
            flags.append(
                f"止损/追踪族术后均亏 ${fam_post['avg_gross']:.0f}/笔 仍低于参考线 "
                f"${AVG_LOSS_REF:.0f} (n={fam_post['n']}) — 继续观察, 触发裁决走 DEB 预注册判据")
        else:
            flags.append(
                f"止损/追踪族术后均亏 ${fam_post['avg_gross']:.0f}/笔 已收敛于参考线内 "
                f"(基线 ${fam_base['avg_gross'] if fam_base['avg_gross'] is not None else 'NA'}/笔)")

    result = {
        "generated_at": now_et().isoformat(timespec="seconds"),
        "baseline_window": f"{BASELINE_START}..{POSTFIX_START}",
        "postfix_window": f">={POSTFIX_START}",
        "baseline_n": len(baseline_rows),
        "postfix_n": len(postfix_rows),
        "baseline_rules": baseline,
        "postfix_rules": postfix,
        "stop_trail_family": {"baseline": fam_base, "postfix": fam_post,
                              "avg_loss_ref": AVG_LOSS_REF},
        "flags": flags,
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[postfix-watch] {result['generated_at']} 基线 n={len(baseline_rows)} "
          f"术后 n={len(postfix_rows)} (术后窗口 {POSTFIX_START} 起)")
    print(f"  止损/追踪族: 基线 {fam_base} → 术后 {fam_post} (参考线 均{AVG_LOSS_REF:.0f}/笔)")
    for f in flags:
        print(f"  [flag] {f}")
    if len(postfix_rows) == 0:
        print("  (术后窗口暂无平仓 — 8/17 首个交易日后复跑)")
    print(f"  → {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
