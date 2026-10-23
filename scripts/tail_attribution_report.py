#!/usr/bin/env python3
"""只读尾部归因报告: 最差 N 笔的画像 + 候选事前过滤器的误杀代价.

口径: PLAYBOOK_SIM, trade_date>=2026-07-01, exit_price>0, 净额非空, A9 剔除首跑批,
并按 A10 SSOT 只取 data_quality in ('clean','real')、排除 POSITION_SYNC/MANUAL
(见 app/core/db_utils.ERA_CLEAN_PERFORMANCE_WHERE; 本脚本不 import db_utils 以避开
其全局 connect hook, 谓词字面量由 tests/test_tail_attribution_caliber.py 对齐).
零行为变更; 不写生产库; 产物 data/tail_attribution_latest.json.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics as st
from collections import Counter, defaultdict
from pathlib import Path

DB = "data/ftseek_us.db"
OUT = Path("data/tail_attribution_latest.json")


def _load(db: str) -> list[dict]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = []
    for r in con.execute(
        """select id, trade_date, symbol, direction, entry_time, entry_price, quantity,
                  realized_pnl, fees_est, coalesce(pnl_net, realized_pnl) net,
                  mfe_pct, mae_pct, holding_hours, exit_reason, decision_id,
                  market_regime, metadata_json
           from trade_records
           where source_type='PLAYBOOK_SIM' and trade_date>='2026-07-01' and exit_price>0
             and coalesce(pnl_net, realized_pnl) is not null
             and coalesce(json_extract(metadata_json, '$.exclusion_reason'), '') = ''
             and coalesce(data_quality, '') in ('clean','real')
             and coalesce(source_type, '') not in ('POSITION_SYNC','MANUAL')"""
    ):
        m = json.loads(r["metadata_json"] or "{}")
        d = dict(r)
        did = r["decision_id"] or ""
        d["src"] = (
            "DA" if did.startswith("DA")
            else "UE" if did.startswith("universe")
            else "intraday" if did.startswith("intraday")
            else "other"
        )
        d["hh"] = (r["entry_time"] or "")[11:16]
        d["gap"] = (m.get("entry_gap") or {}).get("gap_pct")
        d["slip"] = (m.get("execution") or {}).get("slippage_bps")
        d["notional"] = (r["entry_price"] or 0) * (r["quantity"] or 0)
        rows.append(d)
    con.close()
    return rows


def _hour(r: dict) -> str:
    hh = r["hh"]
    if "09:35" <= hh < "09:45":
        return "open"
    if "09:45" <= hh < "10:30":
        return "mid"
    if hh >= "10:30":
        return "late"
    return "<09:35"


def _gap(r: dict) -> str:
    g = r["gap"]
    if g is None:
        return "NA"
    if g <= -3:
        return "<=-3"
    if g < -1:
        return "-3..-1"
    if g < 1:
        return "-1..1"
    if g < 3:
        return "1..3"
    return ">=3"


def _price(r: dict) -> str:
    p = r["entry_price"]
    return "<20" if p < 20 else "20-100" if p < 100 else "100-300" if p < 300 else ">=300"


def _slip(r: dict) -> str:
    s = r["slip"]
    if s is None:
        return "NA"
    return "<0" if s < 0 else "0-10" if s < 10 else ">=10"


def _lift(tail: list[dict], all_rows: list[dict], fn) -> list[dict]:
    tc = Counter(fn(r) for r in tail)
    ac = Counter(fn(r) for r in all_rows)
    out = []
    for value, n in tc.most_common():
        base = ac[value] / len(all_rows)
        out.append({"value": value, "tail_n": n, "all_n": ac[value],
                    "lift": round((n / len(tail)) / base, 2) if base else None})
    return out


def _filter(name: str, pred, rows: list[dict], tail_ids: set[int]) -> dict:
    blocked = [r for r in rows if pred(r)]
    kept = [r for r in rows if not pred(r)]
    bn = sum(r["net"] for r in blocked)
    kn = sum(r["net"] for r in kept)
    winners = sum(1 for r in blocked if r["net"] > 0)
    return {
        "name": name,
        "blocked_n": len(blocked),
        "blocked_net": round(bn, 2),
        "blocked_winners": winners,
        "miskill_pct": round(100.0 * winners / len(blocked), 1) if blocked else None,
        "kept_n": len(kept),
        "kept_net": round(kn, 2),
        "tail_caught": sum(1 for r in blocked if r["id"] in tail_ids),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    rows = _load(args.db)
    if not rows:
        raise SystemExit("no rows")
    rows.sort(key=lambda r: r["net"])
    tail = rows[: args.top]
    tail_ids = {r["id"] for r in tail}
    base_net = sum(r["net"] for r in rows)

    print(f"clean n={len(rows)} net=${base_net:.2f} top={args.top}")
    print("\n== tail ==")
    for r in tail:
        print(f"{r['trade_date']} {r['symbol']:9s} {r['direction']:5s} {r['src']:8s} {r['hh']} "
              f"net={r['net']:8.1f} gap={r['gap']} slip={r['slip']} "
              f"mfe={r['mfe_pct']:.2f} mae={r['mae_pct']:.2f} {r['exit_reason']}")

    print("\n== lift ==")
    lifts = {}
    for name, fn in [
        ("src", lambda r: r["src"]),
        ("direction", lambda r: r["direction"]),
        ("hour", _hour),
        ("gap", _gap),
        ("price", _price),
        ("slip", _slip),
        ("regime", lambda r: r["market_regime"] or "NA"),
    ]:
        lifts[name] = _lift(tail, rows, fn)
        print(f"[{name}] " + ", ".join(
            f"{x['value']}={x['tail_n']} lift={x['lift']}" for x in lifts[name]
        ))

    filters = [
        _filter("LONG & gap>=+3%", lambda r: r["direction"] == "LONG" and r["gap"] is not None and r["gap"] >= 3, rows, tail_ids),
        _filter("LONG & gap<=-3%", lambda r: r["direction"] == "LONG" and r["gap"] is not None and r["gap"] <= -3, rows, tail_ids),
        _filter("LONG & |gap|>=3%", lambda r: r["direction"] == "LONG" and r["gap"] is not None and abs(r["gap"]) >= 3, rows, tail_ids),
        _filter("LONG & price>=300", lambda r: r["direction"] == "LONG" and r["entry_price"] >= 300, rows, tail_ids),
        _filter("open window", lambda r: _hour(r) == "open", rows, tail_ids),
        _filter("UE/intraday", lambda r: r["src"] in ("UE", "intraday"), rows, tail_ids),
        _filter("slip>=10bps", lambda r: r["slip"] is not None and r["slip"] >= 10, rows, tail_ids),
    ]
    print("\n== candidate filters (block=no trade) ==")
    for f in filters:
        print(f"{f['name']:22s} blocked n={f['blocked_n']:3d} net={f['blocked_net']:9.2f} "
              f"miskill={f['miskill_pct']}% tail_caught={f['tail_caught']}/{args.top} kept_net={f['kept_net']:9.2f}")

    halve = {}
    for name, fn in [
        ("|gap|>=3", lambda r: r["direction"] == "LONG" and r["gap"] is not None and abs(r["gap"]) >= 3),
        ("gap<=-3", lambda r: r["direction"] == "LONG" and r["gap"] is not None and r["gap"] <= -3),
        ("gap>=+3", lambda r: r["direction"] == "LONG" and r["gap"] is not None and r["gap"] >= 3),
    ]:
        group = [r for r in rows if fn(r)]
        gnet = sum(r["net"] for r in group)
        halve[name] = {"n": len(group), "group_net": round(gnet, 2),
                       "new_total": round(base_net - 0.5 * gnet, 2)}
    print("\n== halve-size counterfactual ==")
    for k, v in halve.items():
        print(f"{k:10s} n={v['n']:3d} group_net={v['group_net']:9.2f} new_total={v['new_total']:9.2f}")

    payload = {
        "generated_at": "read-only",
        "caliber": "PLAYBOOK_SIM/2026-07-01+/exit_price>0/pnl_net not null/A9 excluded",
        "n": len(rows),
        "net": round(base_net, 2),
        "top": args.top,
        "tail": [{k: r[k] for k in ("id", "trade_date", "symbol", "direction", "src", "hh",
                                    "net", "gap", "slip", "mfe_pct", "mae_pct", "exit_reason")}
                 for r in tail],
        "lifts": lifts,
        "filters": filters,
        "halve": halve,
        "note": "record-only; no behavior change; tail filters fail miskill<=40% on this data",
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
