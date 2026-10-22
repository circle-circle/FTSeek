#!/usr/bin/env python3
"""只读审计: stop_loss 与结构化 invalidation_triggers 的相对位置.

2026-10-22 指令复查发现 TSLA/ARM 的 stop 与 thesis 失效位不在同一侧;
本脚本只记录这个错位, 不改任何入场/出场/仓位行为, 也不写 playbook 表.

口径:
  - 只比较 metric=收盘价 的结构化 trigger; 量比/5日涨跌/盘前涨跌幅只记录.
  - trigger 为 OR 语义, 取最先触发的一条: SHORT 取最小 trigger, LONG 取最大 trigger.
  - SHORT: stop < trigger => stop_before_invalidation;
           stop > trigger => invalidation_before_stop.
  - LONG:  stop > trigger => stop_before_invalidation;
           stop < trigger => invalidation_before_stop.
  - 相等 (<=1e-9) => aligned; 无收盘价 trigger => no_price_trigger.
  - gap_pct 符号统一: 正=stop 更紧 (止损先于失效), 负=失效位更紧 (thesis 先失效).

消费方:
  - 未执行指令: scripts/tripwire_checker.py (crontab 每 30min) 会按 trigger 核销.
  - 已执行持仓: invalidation trigger 目前只有 app/playbook/invalidation_shadow.py 只读影子,
    stop 是实盘唯一硬出场; invalidation_before_stop 的持仓可能撑过 thesis 失效点.

用法:
  PYTHONPATH=. ./venv/bin/python scripts/stop_invalidation_audit.py
  PYTHONPATH=. ./venv/bin/python scripts/stop_invalidation_audit.py --date 2026-10-21 --no-write
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "ftseek_us.db"
DEFAULT_OUT = ROOT / "data" / "shadow" / "stop_invalidation_audit.jsonl"

STOP_BEFORE = "stop_before_invalidation"
INVALIDATION_BEFORE = "invalidation_before_stop"
ALIGNED = "aligned"
NO_PRICE_TRIGGER = "no_price_trigger"


def _today_et() -> str:
    from app.utils.timezone import now_et
    return now_et().strftime("%Y-%m-%d")


def _trigger_parser():
    # SSOT: 与 tripwire_checker 共用同一白名单/解析, 避免口径漂移.
    from scripts.tripwire_checker import _get_structured_triggers
    return _get_structured_triggers


def _raw_dict(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _is_short(direction: str) -> bool:
    return str(direction or "").upper() in ("SHORT", "SELL_SHORT")


def classify_stop_vs_triggers(
    direction: str, stop_loss: Optional[float], triggers: Iterable[Dict[str, Any]]
) -> Dict[str, Any]:
    """纯函数: 返回 classification / trigger / gap_pct / other_triggers."""
    try:
        stop = float(stop_loss) if stop_loss not in (None, "") else None
    except (TypeError, ValueError):
        stop = None
    if stop is None or stop <= 0:
        return {"classification": "no_stop", "trigger": None,
                "gap_pct": None, "other_triggers": list(triggers or [])}

    short = _is_short(direction)
    price_triggers = [
        t for t in (triggers or [])
        if isinstance(t, dict) and str(t.get("metric") or "") == "收盘价"
    ]
    at = _trigger_parser()
    # 复用 tripwire_checker 的白名单/float 归一 (它只接受 raw dict 里的 list).
    parsed = at({"raw_instruction_json": {"invalidation_triggers": price_triggers}})
    relevant = []
    ignored = []
    for t in parsed:
        op = t["operator"]
        if (short and op == ">") or ((not short) and op == "<"):
            relevant.append(t)
        else:
            ignored.append(t)

    if not relevant:
        return {"classification": NO_PRICE_TRIGGER, "trigger": None,
                "gap_pct": None, "other_triggers": list(triggers or []),
                "ignored_price_triggers": ignored}

    trigger = min(relevant, key=lambda t: t["value"]) if short else max(
        relevant, key=lambda t: t["value"])
    value = float(trigger["value"])
    raw_gap = (value - stop) / stop * 100.0
    if abs(value - stop) <= 1e-9:
        classification = ALIGNED
        gap_pct = 0.0
    elif (short and stop < value) or ((not short) and stop > value):
        classification = STOP_BEFORE
        gap_pct = abs(raw_gap)
    else:
        classification = INVALIDATION_BEFORE
        gap_pct = -abs(raw_gap)
    return {"classification": classification, "trigger": trigger,
            "gap_pct": round(gap_pct, 4), "other_triggers": list(triggers or []),
            "ignored_price_triggers": ignored}


def load_rows(db_path: Path, date: str, include_skip: bool = False) -> List[sqlite3.Row]:
    sql = (
        "SELECT id, date, symbol, direction, entry_price, stop_loss, "
        "invalidation, raw_instruction_json "
        "FROM playbook_instructions WHERE date=?"
    )
    if not include_skip:
        sql += " AND direction IN ('BUY','SELL_SHORT')"
    sql += " ORDER BY id"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, (date,)).fetchall()
    finally:
        conn.close()


def audit_rows(rows: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        raw = _raw_dict(row["raw_instruction_json"])
        triggers = raw.get("invalidation_triggers") or []
        result = classify_stop_vs_triggers(row["direction"], row["stop_loss"], triggers)
        out.append({
            "date": row["date"],
            "instruction_id": row["id"],
            "symbol": row["symbol"],
            "direction": row["direction"],
            "entry_price": row["entry_price"],
            "stop_loss": row["stop_loss"],
            "classification": result["classification"],
            "trigger": result.get("trigger"),
            "gap_pct": result.get("gap_pct"),
            "invalidation": row["invalidation"],
            "other_triggers": result.get("other_triggers") or [],
            "ignored_price_triggers": result.get("ignored_price_triggers") or [],
        })
    return out


def _summary(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    by: Dict[str, List[Dict[str, Any]]] = {}
    for r in records:
        by.setdefault(r["classification"], []).append(r)
    return {
        "total": len(records),
        "counts": {k: len(v) for k, v in sorted(by.items())},
        "stop_before_invalidation": [
            {"symbol": r["symbol"], "direction": r["direction"],
             "stop": r["stop_loss"], "trigger": (r["trigger"] or {}).get("value"),
             "gap_pct": r["gap_pct"]}
            for r in by.get(STOP_BEFORE, [])
        ],
        "invalidation_before_stop": [
            {"symbol": r["symbol"], "direction": r["direction"],
             "stop": r["stop_loss"], "trigger": (r["trigger"] or {}).get("value"),
             "gap_pct": r["gap_pct"]}
            for r in by.get(INVALIDATION_BEFORE, [])
        ],
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="stop vs invalidation trigger 只读审计")
    ap.add_argument("--date", default=None, help="交易日 YYYY-MM-DD (默认今日 ET)")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--no-write", action="store_true", help="只打印, 不写 jsonl")
    ap.add_argument("--include-skip", action="store_true",
                    help="同时审计 SKIP 行 (默认只看 BUY/SELL_SHORT)")
    args = ap.parse_args(argv)

    date = args.date or _today_et()
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[stop-invalidation] DB 不存在: {db_path}")
        return 2
    rows = load_rows(db_path, date, include_skip=args.include_skip)
    records = audit_rows(rows)
    summary = _summary(records)
    print(json.dumps({"date": date, **summary}, ensure_ascii=False, indent=2))

    if not args.no_write:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        existing = set()
        bad_lines = 0
        if out.exists():
            for line in out.read_text(encoding="utf-8").splitlines():
                try:
                    old = json.loads(line)
                    existing.add((old.get("date"), old.get("instruction_id")))
                except Exception:
                    bad_lines += 1  # 坏 JSONL 行跳过, 汇总留痕
        if bad_lines:
            print(f"[stop-invalidation] warn: {bad_lines} 行坏 JSONL 已跳过 ({out})")
        new_rows = [r for r in records
                    if (r.get("date"), r.get("instruction_id")) not in existing]
        with open(out, "a", encoding="utf-8") as f:
            for r in new_rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"[stop-invalidation] wrote {len(new_rows)} new rows "
              f"(skipped {len(records) - len(new_rows)} existing) -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
