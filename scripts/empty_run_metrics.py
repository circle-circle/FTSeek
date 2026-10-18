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
UE_SCAN_PATH = ROOT / "data" / "shadow" / "llm_universe_scan.jsonl"
FRESH_EVENT_POOL = ROOT / "data" / "shadow" / "ue_fresh_da_candidate_pool.jsonl"
_BORROW_DROP_RE = re.compile(
    r"\[UE-FEED\]\s+(?:US\.)?([A-Z0-9.\-]+)\s+borrow_feasible=False")


def _bare_symbol(symbol) -> str:
    return str(symbol or "").upper().replace("US.", "").strip()


def _direction_kept(ue_direction: str, final_direction: str) -> bool:
    return (
        (ue_direction == "LONG" and final_direction == "BUY")
        or (ue_direction == "SHORT" and final_direction == "SELL_SHORT")
    )


def _parse_borrow_dropped(stderr_tail: str) -> list[str]:
    return sorted({m.group(1) for m in _BORROW_DROP_RE.finditer(stderr_tail or "")})


def _is_overwrite(created_at, scanned_at) -> bool | None:
    """最终行创建时间早于本批扫描时间 = 本批 UPSERT 覆盖了既存行。"""
    try:
        created = datetime.fromisoformat(str(created_at))
        scanned = datetime.fromisoformat(str(scanned_at))
        if created.tzinfo is None or scanned.tzinfo is None:
            return None
        return created < scanned
    except Exception:
        return None


def _load_jsonl(path: Path) -> list[dict]:
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
    except Exception:
        return []



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


def _playbook_ue_origins(days: int) -> list[dict]:
    """最终指令按 origin 分账: ue_fresh_event / ue_candidate / (none)."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT direction, triggered, executed, raw_instruction_json "
            "FROM playbook_instructions WHERE date >= ?",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, dict] = {}
    for row in rows:
        try:
            raw = json.loads(row["raw_instruction_json"] or "{}") or {}
        except Exception:
            continue
        origin = str(raw.get("origin") or "")
        batch = str(raw.get("ue_batch_id") or "")
        if not origin and not batch:
            continue
        key = origin or "(none)"
        item = out.setdefault(key, {
            "origin": key, "total": 0, "actionable": 0,
            "triggered": 0, "executed": 0, "batches": set(),
        })
        item["total"] += 1
        if row["direction"] in ("BUY", "SELL_SHORT"):
            item["actionable"] += 1
        item["triggered"] += int(row["triggered"] or 0)
        item["executed"] += int(row["executed"] or 0)
        if batch:
            item["batches"].add(batch)
    result = []
    for item in out.values():
        item["batches"] = len(item["batches"])
        result.append(item)
    return sorted(result, key=lambda x: (x["origin"],))


def _ue_batches(days: int, window: str) -> dict[str, dict]:
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    out: dict[str, dict] = {}
    for row in _load_jsonl(UE_SCAN_PATH):
        if row.get("type") != "scan" or row.get("scan_window") != window:
            continue
        date = str(row.get("date") or "")
        if not date:
            try:
                date = datetime.fromisoformat(
                    str(row.get("scanned_at") or "")).strftime("%Y-%m-%d")
            except Exception:
                date = ""
        if date < cutoff:
            continue
        batch_id = str(row.get("batch_id") or "")
        if not batch_id:
            continue
        surfaced = []
        for item in row.get("surfaced") or []:
            direction = str(item.get("direction") or "").upper()
            if direction in ("LONG", "SHORT"):
                surfaced.append({
                    "symbol": _bare_symbol(item.get("symbol")),
                    "direction": direction,
                })
        out[batch_id] = {
            "date": date,
            "batch_id": batch_id,
            "scanned_at": str(row.get("scanned_at") or ""),
            "surfaced": surfaced,
        }
    return out


def _ue_premarket_batches(days: int) -> dict[str, dict]:
    return _ue_batches(days, "premarket")


def _fresh_event_events(days: int) -> dict[str, dict]:
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    out: dict[str, dict] = {}
    for row in _load_jsonl(FRESH_EVENT_POOL):
        if row.get("type") != "fresh_event_batch" or row.get("mode") != "execute":
            continue
        date = str(row.get("date") or "")
        if date < cutoff or row.get("status") != "complete":
            continue
        batch_id = str(row.get("ue_batch_id") or "")
        if not batch_id:
            continue
        out[batch_id] = {
            "candidates": row.get("candidates") or [],
            "same_day_excluded": row.get("same_day_db_excluded") or [],
            "stderr_tail": row.get("stderr_tail") or "",
            "started_at": row.get("started_at") or "",
            "finished_at": row.get("finished_at") or "",
        }
    return out


def _final_ue_rows(days: int) -> dict[str, list[dict]]:
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT symbol, direction, skip_reason, triggered, executed, "
            "raw_instruction_json, created_at, updated_at "
            "FROM playbook_instructions WHERE date >= ?",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, list[dict]] = {}
    for row in rows:
        try:
            raw = json.loads(row["raw_instruction_json"] or "{}") or {}
        except Exception:
            continue
        batch_id = str(raw.get("ue_batch_id") or "")
        if not batch_id:
            continue
        out.setdefault(batch_id, []).append({
            "symbol": _bare_symbol(row["symbol"]),
            "direction": str(row["direction"] or ""),
            "skip_reason": str(row["skip_reason"] or ""),
            "triggered": int(row["triggered"] or 0),
            "executed": int(row["executed"] or 0),
            "origin": str(raw.get("origin") or ""),
            "ue_direction": str(raw.get("ue_direction") or "").upper(),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        })
    return out


def _ue_fresh_funnel(days: int) -> list[dict]:
    """09:10 premarket UE → DA fresh-event → 最终指令 的逐步漏斗."""
    scans = _ue_premarket_batches(days)
    events = _fresh_event_events(days)
    final = _final_ue_rows(days)
    result = []
    for batch_id, scan in sorted(scans.items(), key=lambda kv: kv[1]["date"], reverse=True):
        event = events.get(batch_id, {})
        rows = final.get(batch_id, [])
        surfaced = scan["surfaced"]
        surfaced_syms = {item["symbol"] for item in surfaced}
        final_syms = {row["symbol"] for row in rows}
        actionable = [row for row in rows if row["direction"] in ("BUY", "SELL_SHORT")]
        kept = [row for row in actionable
                if _direction_kept(row["ue_direction"], row["direction"])]
        flipped = [row for row in actionable
                   if row["ue_direction"] and not _direction_kept(
                       row["ue_direction"], row["direction"])]
        overwrites = [
            row for row in rows
            if row["origin"] == "ue_fresh_event"
            and _is_overwrite(row["created_at"], scan["scanned_at"])
        ]
        result.append({
            "date": scan["date"],
            "batch_id": batch_id,
            "scanned_at": scan["scanned_at"],
            "surfaced": surfaced,
            "wrapper_selected": [
                {"symbol": _bare_symbol(item.get("symbol")),
                 "direction": str(item.get("direction") or "").upper()}
                for item in event.get("candidates") or []
            ],
            "same_day_excluded": sorted(
                _bare_symbol(symbol) for symbol in event.get("same_day_excluded") or []),
            "borrow_dropped": _parse_borrow_dropped(event.get("stderr_tail") or ""),
            "final_rows": rows,
            "actionable": len(actionable),
            "triggered": sum(row["triggered"] for row in rows),
            "executed": sum(row["executed"] for row in rows),
            "kept": [row["symbol"] for row in kept],
            "flipped": [row["symbol"] for row in flipped],
            "da_skip": [row["symbol"] for row in rows if row["direction"] == "SKIP"],
            "overwrites": [row["symbol"] for row in overwrites],
            "no_final_symbols": sorted(surfaced_syms - final_syms),
        })
    return result


def _da_feed_stderr_path(date: str) -> Path | None:
    """DA 主链 stderr → da_feed 注入/借券丢弃读数 (rotation 后缀 = ET 日 +1 CST)."""
    try:
        suffix = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y%m%d")
    except Exception:
        return None
    candidates = [
        ROOT / "logs" / f"da_pipeline_stderr.log-{suffix}.gz",
        ROOT / "logs" / f"da_pipeline_stderr.log-{suffix}",
    ]
    if date == datetime.now().strftime("%Y-%m-%d"):
        candidates.append(ROOT / "logs" / "da_pipeline_stderr.log")
    for path in candidates:
        if path.exists():
            return path
    return None


def _parse_da_feed_injection(date: str) -> dict:
    """只读第一段 UE 注入 (da_feed 主链); fresh-event 的第二段不算。"""
    out = {
        "injected": [], "injected_count": None, "borrow_dropped": [],
        "streak_dropped": [], "overlap": None, "source": None,
    }
    path = _da_feed_stderr_path(date)
    if path is None:
        return out
    out["source"] = path.name
    try:
        for line in _open_text(path):
            match = re.search(
                r"\[Pipeline\] UE候选注入: \+(\d+) \(\[(.*?)\]\)", line)
            if match:
                out["injected_count"] = int(match.group(1))
                out["injected"] = re.findall(
                    r"US\.([A-Z0-9.\-]+)", match.group(2))
                overlap = re.search(r"重叠归因回填:\s*(\d+)", line)
                if overlap:
                    out["overlap"] = int(overlap.group(1))
                break
            borrow = re.search(
                r"\[UE-FEED\]\s+(?:US\.)?([A-Z0-9.\-]+)\s+borrow_feasible=False",
                line)
            if borrow:
                out["borrow_dropped"].append(borrow.group(1))
            streak = re.search(
                r"\[UE-FEED\]\s+(?:US\.)?([A-Z0-9.\-]+)\s+streak=(\d+)", line)
            if streak:
                out["streak_dropped"].append(streak.group(1))
    except Exception:
        pass
    return out


def _ue_da_feed_funnel(days: int) -> list[dict]:
    """08:00 UE da_feed → DA 主链 → 最终指令 的逐步漏斗."""
    scans = _ue_batches(days, "da_feed")
    final = _final_ue_rows(days)
    result = []
    for batch_id, scan in sorted(scans.items(), key=lambda kv: kv[1]["date"], reverse=True):
        rows = final.get(batch_id, [])
        surfaced = scan["surfaced"]
        surfaced_syms = {item["symbol"] for item in surfaced}
        final_syms = {row["symbol"] for row in rows}
        actionable = [row for row in rows if row["direction"] in ("BUY", "SELL_SHORT")]
        kept = [row for row in actionable
                if _direction_kept(row["ue_direction"], row["direction"])]
        flipped = [row for row in actionable
                   if row["ue_direction"] and not _direction_kept(
                       row["ue_direction"], row["direction"])]
        da = _parse_da_feed_injection(scan["date"])
        result.append({
            "date": scan["date"],
            "batch_id": batch_id,
            "scanned_at": scan["scanned_at"],
            "surfaced": surfaced,
            "da_injected": da["injected"],
            "da_injected_count": da["injected_count"],
            "borrow_dropped": da["borrow_dropped"],
            "streak_dropped": da["streak_dropped"],
            "overlap": da["overlap"],
            "stderr_source": da["source"],
            "final_rows": rows,
            "actionable": len(actionable),
            "triggered": sum(row["triggered"] for row in rows),
            "executed": sum(row["executed"] for row in rows),
            "kept": [row["symbol"] for row in kept],
            "flipped": [row["symbol"] for row in flipped],
            "da_skip": [row["symbol"] for row in rows if row["direction"] == "SKIP"],
            "no_final_symbols": sorted(surfaced_syms - final_syms),
        })
    return result


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
        "playbook_ue_origins": _playbook_ue_origins(args.days),
        "ue_da_feed_funnel": _ue_da_feed_funnel(args.days),
        "ue_fresh_event_funnel": _ue_fresh_funnel(args.days),
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

    print("\n[UE 来源最终指令]")
    for row in data["playbook_ue_origins"]:
        print(f"  {row['origin']}: total={row['total']} actionable={row['actionable']} "
              f"triggered={row['triggered']} executed={row['executed']} "
              f"batches={row['batches']}")

    print("\n[UE 08:00 da_feed → DA → 最终指令]")
    if not data["ue_da_feed_funnel"]:
        print("  无 da_feed 批次")
    for item in data["ue_da_feed_funnel"]:
        surfaced = ",".join(
            f"{x['symbol']}:{x['direction']}" for x in item["surfaced"]) or "-"
        finals = ", ".join(
            f"{r['symbol']}:{r['direction']}(ue={r['ue_direction'] or '-'},"
            f"origin={r['origin'] or '-'},skip={r['skip_reason'][:20] or '-'})"
            for r in item["final_rows"]
        ) or "-"
        print(f"  {item['date']} {item['batch_id']}")
        print(f"    surfaced={surfaced}")
        if item.get("stderr_source") is None:
            print("    DA注入数据不可用 (无 stderr 载体; 10/16 前历史日志缺失)")
        else:
            print(f"    DA注入={item['da_injected'] or '-'} (n={item['da_injected_count']}) "
                  f"borrow丢弃={item['borrow_dropped'] or '-'} "
                  f"streak丢弃={item['streak_dropped'] or '-'} overlap={item['overlap']}")
        print(f"    最终指令={finals}")
        print(f"    actionable={item['actionable']} triggered={item['triggered']} "
              f"executed={item['executed']} kept={item['kept'] or '-'} "
              f"flipped={item['flipped'] or '-'} 无最终指令={item['no_final_symbols'] or '-'}")

    print("\n[UE 09:10 → DA → 最终指令]")
    print("  (2026-10-18 起 fresh-event execute 已停用; 新日期无 wrapper/最终指令属预期)")
    if not data["ue_fresh_event_funnel"]:
        print("  无 premarket UE 批次")
    for item in data["ue_fresh_event_funnel"]:
        surfaced = ",".join(
            f"{x['symbol']}:{x['direction']}" for x in item["surfaced"]) or "-"
        selected = ",".join(
            f"{x['symbol']}:{x['direction']}" for x in item["wrapper_selected"]) or "-"
        finals = ", ".join(
            f"{r['symbol']}:{r['direction']}(ue={r['ue_direction'] or '-'},"
            f"origin={r['origin'] or '-'},skip={r['skip_reason'][:24] or '-'})"
            for r in item["final_rows"]
        ) or "-"
        print(f"  {item['date']} {item['batch_id']}")
        print(f"    surfaced={surfaced}")
        print(f"    wrapper选中={selected} same_day排除={item['same_day_excluded'] or '-'} "
              f"borrow丢弃={item['borrow_dropped'] or '-'}")
        print(f"    最终指令={finals}")
        print(f"    actionable={item['actionable']} triggered={item['triggered']} "
              f"executed={item['executed']} kept={item['kept'] or '-'} "
              f"flipped={item['flipped'] or '-'} overwrite={item['overwrites'] or '-'} "
              f"无最终指令={item['no_final_symbols'] or '-'}")

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
