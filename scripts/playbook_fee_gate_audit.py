#!/usr/bin/env python3
"""Audit the configured R06 fee gate against actual Playbook fills, read-only."""
from __future__ import annotations

import argparse
import ast
import json
import sqlite3
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.db_utils import a9_clean_where, era_clean_where  # noqa: E402
from scripts.fee_gate_shadow import arm_stats  # noqa: E402

DB_PATH = ROOT / "data" / "ftseek_us.db"
CONFIG_PATH = ROOT / "config" / "unified_config.yaml"
SOURCE_PREDICATES = {
    "DA": "t.decision_id LIKE 'DA-%'",
    "EXT": "t.decision_id LIKE 'EXT-%'",
    "ALL": "1 = 1",
}


def configured_gate(path: Path = CONFIG_PATH) -> dict:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    gate = (raw.get("playbook") or {}).get("fee_gate") or {}
    return {
        "enabled": bool(gate.get("enabled", True)),
        "max_fee_to_gross_ratio": float(gate.get("max_fee_to_gross_ratio", 0.25)),
        "rr_mult": float(gate.get("rr_mult", 2.0)),
    }


def evaluate_r06(entry_price: float, stop_loss: float | None, quantity: int,
                 fees_est: float, *, rr_mult: float, max_ratio: float) -> dict:
    if not stop_loss or stop_loss <= 0 or entry_price <= 0 or quantity <= 0 or fees_est < 0:
        return {"eligible": False, "would_block": False,
                "expected_gross_usd": None, "fee_ratio": None}
    expected_gross = abs(entry_price - stop_loss) * quantity * rr_mult
    if expected_gross <= 0:
        return {"eligible": False, "would_block": False,
                "expected_gross_usd": None, "fee_ratio": None}
    ratio = fees_est / expected_gross
    return {
        "eligible": True,
        "would_block": ratio > max_ratio,
        "expected_gross_usd": round(expected_gross, 4),
        "fee_ratio": round(ratio, 6),
    }


def find_playbook_consumers(root: Path = ROOT) -> list[str]:
    consumers = []
    for path in sorted((root / "app" / "playbook").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as exc:
            print(f"[playbook_fee_gate_audit] 不可解析模块跳过 {path}: {type(exc).__name__}", file=sys.stderr)
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "playbook.fee_gate":
                consumers.append(str(path.relative_to(root)))
                break
    return consumers


def fetch_rows(conn: sqlite3.Connection, date_from: str, date_to: str,
               source: str) -> list[dict]:
    where = [
        "t.source_type = 'PLAYBOOK_SIM'",
        "t.exit_price > 0",
        "COALESCE(t.pnl_net, t.realized_pnl) IS NOT NULL",
        "t.realized_pnl IS NOT NULL",
        "t.fees_est IS NOT NULL",
        "t.entry_price > 0",
        "t.quantity > 0",
        "t.trade_date >= ?",
        "t.trade_date <= ?",
        SOURCE_PREDICATES[source],
    ]
    suffix = era_clean_where(conn) + a9_clean_where(conn)
    sql = (
        "SELECT t.id AS trade_id, t.trade_date, t.symbol, t.decision_id,"
        " t.entry_price, t.quantity, t.fees_est,"
        " COALESCE(t.pnl_net, t.realized_pnl) AS pnl_net,"
        " ABS(t.entry_price * t.quantity) AS notional,"
        " (SELECT pi.stop_loss FROM playbook_instructions pi"
        "  WHERE pi.decision_id = t.decision_id ORDER BY pi.id DESC LIMIT 1) AS stop_loss"
        " FROM trade_records t WHERE " + " AND ".join(where) + suffix + " ORDER BY t.id"
    )
    return [dict(row) for row in conn.execute(sql, (date_from, date_to)).fetchall()]


def build_report(conn: sqlite3.Connection, date_from: str, date_to: str,
                 source: str, gate: dict) -> dict:
    rows = fetch_rows(conn, date_from, date_to, source)
    evaluated = []
    for row in rows:
        decision = evaluate_r06(
            float(row["entry_price"]),
            float(row["stop_loss"]) if row["stop_loss"] is not None else None,
            int(row["quantity"]),
            float(row["fees_est"]),
            rr_mult=gate["rr_mult"],
            max_ratio=gate["max_fee_to_gross_ratio"],
        )
        evaluated.append(dict(row, **decision))
    eligible = [row for row in evaluated if row["eligible"]]
    blocked = [row for row in eligible if row["would_block"]]
    kept = [row for row in eligible if not row["would_block"]]
    missing = [row for row in evaluated if not row["eligible"]]
    consumers = find_playbook_consumers()
    return {
        "window": {"from": date_from, "to": date_to, "source": source},
        "configured_gate": gate,
        "path_contract": {
            "playbook_consumers": consumers,
            "active_on_current_playbook_path": bool(consumers),
            "legacy_implementation": "app/pipeline/order_executor.py",
            "submission_path": "PlaybookService -> DualBrokerAdapter",
        },
        "baseline": arm_stats(rows),
        "r06_eligible": arm_stats(eligible),
        "blocked": arm_stats(blocked),
        "kept": arm_stats(kept),
        "missing_or_invalid_stop": arm_stats(missing),
        "blocked_winners": sum(float(row["pnl_net"]) > 0 for row in blocked),
        "counterfactual_boundary": "Observational only; no capital reallocation, changed queue position, or second-order effects.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="date_from", default="2026-10-06")
    parser.add_argument("--to", dest="date_to", default="2099-12-31")
    parser.add_argument("--source", choices=sorted(SOURCE_PREDICATES), default="DA")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    conn = sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        result = build_report(conn, args.date_from, args.date_to, args.source, configured_gate())
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"R06 费用门只读审计 [{args.date_from}~{args.date_to}] source={args.source}")
        print(f"现役 Playbook 消费方: {result['path_contract']['playbook_consumers']}")
        for name in ("baseline", "r06_eligible", "blocked", "kept", "missing_or_invalid_stop"):
            print(f"{name}: {result[name]}")
        print(f"被拦赢家: {result['blocked_winners']}")
        print(result["counterfactual_boundary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
