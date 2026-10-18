#!/usr/bin/env python3
"""UE premarket fresh events → same-day DA incremental candidate batch.

[2026-10-18 用户裁「DA 为主」] 生产 crontab 已摘除本脚本的串接段, 当日写库停用;
  保留脚本供人工影子/回滚, 勿在无新裁的情况下重新挂 cron (见 RFC-148).

Default mode is an isolated shadow run: DA analyzes UE candidates but writes to
`data/shadow/da_fresh_event/<ET date>/` and skips production DB/pool writes.

--execute (2026-10-14 转正, 用户裁决; RFC-136 附录): 生产触点**仅** playbook_instructions
DB 直写 (走 DA 统一执行轨道)。clobber 防护: --output-dir 两种模式恒隔离 — Stage 6b 的
opportunity_scan bridge 文件/deep_sector_rotation/dynamic_blacklist 都随 output_dir 落盘,
execute 不隔离会拿 1-3 候选覆盖 DA 主链当日同名生产文件 (15+ 消费方)。pool 合入生产
需改 pipeline pool 路径语义, 另裁 (恒 --no-pool-write)。当日已有指令的标的进批前剔除
(UPSERT by date+symbol 防覆盖 + 本批定位=新事件扩标的)。回滚 = crontab 删 --execute。
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.playbook.ue_batch import latest_valid_batch
from app.utils.timezone import now_et

SKILL_SCRIPTS = Path("/root/.hermes/skills/deep-stock-analysis/scripts")
DEFAULT_PIPELINE = SKILL_SCRIPTS / "pipeline.py"
DEFAULT_SCAN_PATH = REPO_ROOT / "data/shadow/llm_universe_scan.jsonl"
DEFAULT_EVENT_PATH = REPO_ROOT / "data/shadow/ue_fresh_da_candidate_pool.jsonl"
DEFAULT_DB_PATH = REPO_ROOT / "data/ftseek_us.db"
DEFAULT_MAX_CANDIDATES = 3
DEFAULT_MAX_AGE_HOURS = 1.0


def _read_jsonl(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    rows = []
    n_bad = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
        except json.JSONDecodeError:
            n_bad += 1  # 2026-10-08 棘轮清剿: 坏行计数留痕 (原为静默 continue)
            continue
    if n_bad:
        print(f"[warn] {path.name}: 跳过坏行 {n_bad} 条", file=sys.stderr)
    return rows


def _append_event(path: Path, event: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _completed_today(event_path: Path, date: str, mode: str) -> bool:
    return any(
        row.get("type") == "fresh_event_batch"
        and row.get("date") == date
        and row.get("mode") == mode
        and row.get("status") == "complete"
        for row in _read_jsonl(event_path)
    )


def _is_trading_day(day) -> bool:
    try:
        from app.utils.holiday_calendar import get_holiday_calendar
        return bool(get_holiday_calendar().is_trading_day("US", day))
    except Exception:
        return True


def load_fresh_scan(
    scan_path: Path = DEFAULT_SCAN_PATH,
    now: Optional[datetime] = None,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
) -> Tuple[Optional[Dict], str]:
    now = now or now_et()
    today = now.strftime("%Y-%m-%d")
    return latest_valid_batch(
        _read_jsonl(scan_path), today, now, max_age_hours, window="premarket")


def same_day_instruction_symbols(db_path: Path = DEFAULT_DB_PATH,
                                 trade_date: str = "") -> Optional[FrozenSet[str]]:
    """[2026-10-14 execute转正] 当日 playbook_instructions 已有指令行的裸 symbol 集合。

    用途: fresh-event 候选进批前剔除当日已被 DA 主链 (或上一批 fresh-event) 裁决过
    的标的 — Stage 6b 按 date+symbol UPSERT, 同标的会**覆盖**既有指令行 (方向可翻转、
    triggered/executed 状态被冲掉 ⇒ 已成交仓位的指令行显示未执行 → 重复执行风险)。
    且本批定位 = 新事件驱动**扩标的**, 重判存量指令不扩量只添撞车。
    查询失败返回 None: execute 调用方必须 fail-closed 跳过本批 (不能绕过 UPSERT
    防覆盖前置过滤); shadow 可降级为空集继续攒样。
    """
    if not trade_date:
        return frozenset()
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT symbol FROM playbook_instructions WHERE date=?",
                (trade_date,)).fetchall()
        finally:
            con.close()
    except Exception as exc:
        print(f"[ERROR] 当日已有指令查询失败: {exc}", file=sys.stderr)
        return None
    return frozenset(str(r[0]).upper().replace("US.", "") for r in rows if r and r[0])


def select_fresh_candidates(
    scan: Optional[Dict], max_candidates: int = DEFAULT_MAX_CANDIDATES,
    exclude_symbols: Optional[FrozenSet[str]] = None,
) -> List[Dict]:
    if not scan:
        return []
    selected = []
    for row in scan.get("surfaced", []):
        direction = str(row.get("direction") or "").upper()
        if direction not in ("LONG", "SHORT"):
            continue
        symbol = str(row.get("symbol") or "").upper().strip()
        if not symbol:
            continue
        # [2026-10-14] 当日已有指令标的剔除 (UPSERT 防覆盖; 见 same_day_instruction_symbols)
        if exclude_symbols and symbol.replace("US.", "") in exclude_symbols:
            continue
        selected.append({
            "symbol": symbol if "." in symbol else f"US.{symbol}",
            "direction": direction,
            "confidence": row.get("confidence"),
            "thesis": row.get("thesis", ""),
            "ue_batch_id": scan.get("batch_id", ""),
        })
        if len(selected) >= max_candidates:
            break
    return selected


def build_command(
    python: str,
    pipeline: Path,
    execute: bool,
    output_dir: Path,
    context_dir: Path,
) -> List[str]:
    # [2026-10-14 execute转正·clobber防护] --output-dir **两种模式都传**: Stage 6b 的
    #   opportunity_scan_<date>.json (bridge)、deep_sector_rotation.json、
    #   dynamic_blacklist.json、da_gate_decisions.jsonl、da_checkpoint/ 全部随
    #   output_dir 落盘。execute 若不传会落生产 data/ 根目录 ⇒ 1-3 个候选的
    #   bridge 文件**覆盖** DA 主链当日同名文件 (scheduler_core/_try_load_bridge、
    #   da_supply_reconcile_check、rubric、dashboard 等 15+ 消费方读到残缺上下文),
    #   空 sector_rotation/blacklist 同样 clobber 生产。隔离目录 = 零 clobber;
    #   execute 的唯一生产触点 = DB 指令 (save_instruction 走 adapter 的真 DB 路径,
    #   不随 output_dir)。gate jsonl 留在本批目录 = 结算器路径模式与影子期连续。
    #   --no-pool-write 两种模式恒开: pool 写入同样落 output_dir (隔离目录=孤儿文件
    #   无人读), 合入生产池需改 pipeline pool 路径语义 → 另裁 (RFC-136 附录)。
    command = [
        python,
        str(pipeline),
        "--mode", "full",
        "--top", "0",
        "--fresh-event",
        "--format", "json",
        "--fresh-context-dir", str(context_dir),
        "--output-dir", str(output_dir),
        "--no-pool-write",
    ]
    if not execute:
        command.append("--no-db-write")
    return command


def run(args: argparse.Namespace) -> int:
    now = now_et()
    date = now.strftime("%Y-%m-%d")
    mode = "execute" if args.execute else "shadow"
    base_event = {
        "type": "fresh_event_batch",
        "date": date,
        "mode": mode,
        "started_at": now.isoformat(),
    }

    if not args.force and not _is_trading_day(now.date()):
        _append_event(args.event_path, {**base_event, "status": "skipped_non_trading_day"})
        print(f"[fresh-event] {date} non-trading day, skipped")
        return 0

    minute_of_day = now.hour * 100 + now.minute
    if not args.force and not 900 <= minute_of_day <= 929:
        _append_event(args.event_path, {
            **base_event, "status": "skipped_outside_window", "et_minute": minute_of_day,
        })
        print(f"[fresh-event] ET {minute_of_day:04d} outside 0900-0929 start window, skipped")
        return 0

    if _completed_today(args.event_path, date, mode):
        _append_event(args.event_path, {**base_event, "status": "skipped_idempotent"})
        print(f"[fresh-event] {date} {mode} already complete")
        return 0

    scan, reason = load_fresh_scan(
        args.scan_path, now=now, max_age_hours=args.max_age_hours)
    if scan is None:
        _append_event(args.event_path, {
            **base_event, "status": "complete_no_scan", "reason": reason,
        })
        print(f"[fresh-event] no valid premarket scan: {reason}")
        return 0

    # [2026-10-14 execute转正·UPSERT防覆盖] 当日已有指令标的剔除 (shadow/execute 同口径:
    #   重复裁决浪费 DA LLM 额度并污染攒样; execute 下还会覆盖既有指令行)。
    existing_today = same_day_instruction_symbols(args.db, date)
    if existing_today is None:
        if args.execute:
            _append_event(args.event_path, {
                **base_event,
                "status": "dedup_query_failed",
                "ue_batch_id": scan.get("batch_id", ""),
            })
            print("[fresh-event] execute skipped: same-day instruction query failed",
                  file=sys.stderr)
            return 2
        existing_today = frozenset()
    deduped = sorted({
        str(row.get("symbol") or "")
        for row in scan.get("surfaced", [])
        if str(row.get("direction") or "").upper() in ("LONG", "SHORT")
        and str(row.get("symbol") or "").upper().replace("US.", "") in existing_today
    })
    candidates = select_fresh_candidates(scan, args.max_candidates,
                                         exclude_symbols=existing_today)
    if not candidates:
        _append_event(args.event_path, {
            **base_event, "status": "complete_no_candidates",
            "ue_batch_id": scan.get("batch_id", ""),
            "same_day_db_excluded": deduped,
        })
        print("[fresh-event] no LONG/SHORT fresh candidates"
              + (f" (当日已有指令剔除: {','.join(deduped)})" if deduped else ""))
        return 0

    output_dir = args.output_dir if args.output_dir else (
        REPO_ROOT / "data/shadow/da_fresh_event" / date)
    output_dir.mkdir(parents=True, exist_ok=True)
    command = build_command(
        args.python, args.pipeline, args.execute, output_dir,
        args.context_dir)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + (
        ":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=args.timeout,
            check=False,
        )
        status = "complete" if completed.returncode == 0 else "pipeline_failed"
        event = {
            **base_event,
            "status": status,
            "ue_batch_id": scan.get("batch_id", ""),
            "scanned_at": scan.get("scanned_at", ""),
            "candidates": candidates,
            "same_day_db_excluded": deduped,
            "command": command,
            "returncode": completed.returncode,
            "stdout_tail": completed.stdout[-4000:],
            "stderr_tail": completed.stderr[-4000:],
            "finished_at": now_et().isoformat(),
        }
        _append_event(args.event_path, event)
        if completed.stdout:
            print(completed.stdout)
        if completed.stderr:
            print(completed.stderr, file=sys.stderr)
        print(f"[fresh-event] {status}: {len(candidates)} candidates")
        return completed.returncode
    except subprocess.TimeoutExpired as exc:
        _append_event(args.event_path, {
            **base_event,
            "status": "pipeline_timeout",
            "ue_batch_id": scan.get("batch_id", ""),
            "candidates": candidates,
            "command": command,
            "timeout": args.timeout,
            "finished_at": now_et().isoformat(),
        })
        print(f"[fresh-event] pipeline timeout after {args.timeout}s: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true",
                        help="write production DA instructions (default is isolated shadow)")
    parser.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES)
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--pipeline", type=Path, default=DEFAULT_PIPELINE)
    parser.add_argument("--scan-path", type=Path, default=DEFAULT_SCAN_PATH)
    parser.add_argument("--event-path", type=Path, default=DEFAULT_EVENT_PATH)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH,
                        help="playbook_instructions DB (当日已有指令去重, 只读)")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--context-dir", type=Path, default=REPO_ROOT / "data")
    parser.add_argument("--force", action="store_true",
                        help="skip ET window/trading-day/idempotency guards (testing)")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
