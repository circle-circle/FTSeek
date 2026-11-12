#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.event_stream.ingest_service import IngestService
from app.event_stream.store import EventStore

STATE_PATH = ROOT / "data" / ".event_stream_ingest_state.json"


def _now_et() -> datetime:
    try:
        from app.utils.timezone import now_et
        return now_et()
    except Exception:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))


def _is_us_trading_day(day) -> bool:
    try:
        from app.utils.holiday_calendar import get_holiday_calendar
        return not bool(get_holiday_calendar().is_us_holiday(day))
    except Exception:
        return day.weekday() < 5


def cadence_minutes(now_et: datetime, trading_day: bool) -> int:
    """分时采频: 关键窗 5m / 交易日盘中 15m / 其余 20m.

    2026-10-18 空跑治理: 原 cron 每 5 分钟全量拉 4 源, 实测 10/16 的 1,168 个
    source-run 中 809 个 events=0 (69%), FR 源长期零产出, 全天约 24 分钟
    花在空抓上。关键窗保留 5 分钟以服务 07:40/09:10 UE、confirm 与观察
    (09:10 fresh-event 当日写库已于 2026-10-18 按「DA 为主」停用)。
    """
    minute_of_day = now_et.hour * 60 + now_et.minute
    if trading_day and 7 * 60 + 30 <= minute_of_day < 9 * 60 + 35:
        return 5
    if trading_day and 4 * 60 <= minute_of_day < 20 * 60:
        return 15
    return 20


def _read_last_success(path: Path | None = None) -> Optional[datetime]:
    path = path or STATE_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        stamp = datetime.fromisoformat(str(data.get("last_success_at") or ""))
        return stamp if stamp.tzinfo is not None else None
    except Exception:
        return None


def _write_last_success(stamp: datetime, path: Path | None = None) -> None:
    path = path or STATE_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"last_success_at": stamp.isoformat()}), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        print(f"[event_stream] cadence_state_write_failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)


def should_run(now_et: datetime, last_success: Optional[datetime],
               interval_minutes: int, force: bool = False) -> bool:
    if force or last_success is None:
        return True
    try:
        age = (now_et - last_success).total_seconds()
    except Exception:
        return True
    return age < 0 or age >= interval_minutes * 60


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest Phase 1 event sources into EventStore.")
    parser.add_argument("--db", type=Path, default=None, help="SQLite path; default uses project data dir")
    parser.add_argument("--source", action="append", choices=[
        "SEC_EDGAR_8K_ATOM",
        "FEDERAL_REGISTER_API",
        "GOOGLE_NEWS_RSS",
        "FINNHUB_GENERAL_NEWS",
    ], help="Source to ingest; repeatable; default all")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--cadence", action="store_true",
                        help="按 ET 分时采频跳过非必要轮次 (cron 用)")
    parser.add_argument("--force", action="store_true", help="绕过分时采频, 立即采集")
    args = parser.parse_args()

    now_et = _now_et() if args.cadence else None
    if args.cadence and not args.force:
        interval = cadence_minutes(now_et, _is_us_trading_day(now_et.date()))
        last = _read_last_success()
        if not should_run(now_et, last, interval):
            print(f"[event_stream] cadence_skip interval={interval}m "
                  f"last={last.isoformat() if last else None}", file=sys.stderr)
            return 0

    store = EventStore(args.db)
    service = IngestService(store)
    summary = service.ingest(args.source)
    if now_et is not None and summary and all(
            str(info.get("status")) == "OK" for info in summary.values()):
        _write_last_success(now_et)
    print(f"[event_stream] total_latency_ms={service.last_total_latency_ms}", file=sys.stderr)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        for source_id, info in summary.items():
            status = info.get("status", "?")
            latency = info.get("latency_ms", "?")
            events = info.get("events_inserted", 0)
            print(f"{source_id:<27} status={status} latency={latency}ms events={events}")
    # 2026-10-18: 新鲜度/不健康折叠进 ingest 日志 (原先只靠未挂 cron 的独立检查).
    try:
        now = datetime.now(timezone.utc)
        warnings = []
        for row in store.get_source_health_rows():
            source_id = str(row.get("source_id") or "?")
            status = str(row.get("status") or "?")
            if status == "PARTIAL":
                warnings.append(f"{source_id}:PARTIAL")
            elif status != "OK":
                warnings.append(f"{source_id}:{status}")
                continue
            last = row.get("last_success_at")
            if last:
                try:
                    stamp = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    age = int((now - stamp).total_seconds())
                    if age > 1800:
                        warnings.append(f"{source_id}:stale{age}s")
                except ValueError:
                    warnings.append(f"{source_id}:bad_ts")
        if warnings:
            print(f"[event_stream] health_warning: {'; '.join(warnings)}", file=sys.stderr)
    except Exception as exc:
        print(f"[event_stream] health_warning_check_failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
