#!/usr/bin/env python3
"""盘前/盘后空跑指标 (只读, 2026-10-18).

用法: PYTHONPATH=. venv/bin/python scripts/empty_run_metrics.py --days 10
输出: 盘前可执行单来源、DA 完成余量、延长时段空跑、EventStream 零事件率。
不写库、不调用模型、不改变任何交易行为。
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sqlite3
import statistics
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"


def _open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="ignore")
    return path.open(encoding="utf-8", errors="ignore")


def _dated_logs(pattern: str, days: int) -> list[Path]:
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    out = []
    for path in sorted(ROOT.glob(pattern)):
        match = re.search(r"(\d{8})", path.name)
        if match and match.group(1) < cutoff:
            continue
        out.append(path)
    return out


def _playbook_actionables(days: int) -> list[dict]:
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT date, direction, raw_instruction_json FROM playbook_instructions "
            "WHERE date >= ? AND direction IN ('BUY','SELL_SHORT') ORDER BY date",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    by_date: dict[str, dict] = {}
    for row in rows:
        try:
            source = str((json.loads(row["raw_instruction_json"] or "{}") or {}).get("source") or "")
        except Exception:
            source = ""
        item = by_date.setdefault(row["date"], {"actionable": 0, "bridge": 0, "other": 0})
        item["actionable"] += 1
        if source == "deep_analysis":
            item["bridge"] += 1
        else:
            item["other"] += 1
    return [{"date": date, **vals} for date, vals in sorted(by_date.items())]


def _da_margins(days: int) -> dict:
    path = ROOT / "data" / "shadow" / "da_main_schedule.jsonl"
    margins = []
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except Exception:
        rows = []
    for row in rows:
        if row.get("status") != "complete" or not row.get("finished_at"):
            continue
        try:
            stamp = datetime.fromisoformat(str(row["finished_at"]))
        except Exception:
            continue
        minutes = stamp.hour * 60 + stamp.minute
        margins.append(9 * 60 - minutes)  # 正数 = 距 09:00 ET 的余量
    margins = margins[-days:]
    if not margins:
        return {"n": 0, "min": None, "median": None}
    return {"n": len(margins), "min": min(margins), "median": round(statistics.median(margins), 1)}


def _eth_metrics(days: int) -> dict:
    runs = outside = session_runs = no_positions = collector_ok = 0
    for path in _dated_logs("logs/extended_hours_exit_cron.log*", days):
        for line in _open_text(path):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            runs += 1
            if row.get("skip") == "outside_extended_hours":
                outside += 1
            if row.get("session") in ("premarket", "afterhours"):
                session_runs += 1
            if row.get("collector") == "skipped_no_positions":
                no_positions += 1
            elif "无持仓" in str(row.get("stdout_tail") or ""):
                no_positions += 1
            if row.get("collector") == "ok":
                collector_ok += 1
    return {
        "runs": runs,
        "outside": outside,
        "session_runs": session_runs,
        "no_positions": no_positions,
        "collector_ok": collector_ok,
    }


def _ingest_metrics(days: int) -> dict:
    source_runs = zero_events = 0
    pattern = re.compile(r"status=\S+ latency=\S+ events=(\d+)")
    for path in _dated_logs("logs/event_stream_ingest.log*", days):
        for line in _open_text(path):
            match = pattern.search(line)
            if not match:
                continue
            source_runs += 1
            if int(match.group(1)) == 0:
                zero_events += 1
    ratio = round(zero_events / source_runs * 100, 1) if source_runs else None
    return {"source_runs": source_runs, "zero_events": zero_events, "zero_pct": ratio}


def main() -> int:
    parser = argparse.ArgumentParser(description="盘前/盘后空跑指标 (只读)")
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    data = {
        "days": args.days,
        "playbook_actionables": _playbook_actionables(args.days),
        "da_completion_margin_min": _da_margins(args.days),
        "extended_hours_exit": _eth_metrics(args.days),
        "event_stream_ingest": _ingest_metrics(args.days),
    }
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    print(f"空跑指标 (最近 {args.days} 天)")
    print("\n[盘前可执行单来源]")
    for row in data["playbook_actionables"]:
        print(f"  {row['date']} actionable={row['actionable']:>2} "
              f"bridge={row['bridge']:>2} other={row['other']:>2}")
    margin = data["da_completion_margin_min"]
    print(f"\n[DA 完成余量] n={margin['n']} min={margin['min']}min median={margin['median']}min "
          f"(相对 09:00 ET)")
    eth = data["extended_hours_exit"]
    print(f"\n[ETH 退出 cron] runs={eth['runs']} outside={eth['outside']} "
          f"session={eth['session_runs']} no_positions={eth['no_positions']} "
          f"collector_ok={eth['collector_ok']}")
    ing = data["event_stream_ingest"]
    print(f"\n[EventStream] source_runs={ing['source_runs']} zero_events={ing['zero_events']} "
          f"({ing['zero_pct']}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
