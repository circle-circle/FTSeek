#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EXT 在途持仓事件再评估 · 连续 record-only 影子

定位
----
`scripts/ext_event_risk_shadow.py` (RFC-205) 是**单笔已平仓交易**的离线回看器:
每次手工指定 trade_id + as_of, 输出 NO_CHANGE / REVIEW / INVALIDATE。
本脚本把它**连续化**: 每轮只扫 `trade_records` 里当前仍未平仓的 EXT 持仓
(`exit_time IS NULL AND decision_id LIKE 'EXT-%'`), 逐个调用同一个
`evaluate()`，并在**实盘口径**上做三件收窄:

  1. **实盘输出只有两态** `NO_CHANGE` / `REVIEW`。底层 `INVALIDATE` 一律降级为
     `REVIEW`, 但保留 `official_invalidate_candidate=true` 作为证据标记 — 本影子
     不是自动平仓器, 官方一手反证也必须由人复核。
  2. **不产生任何动作**: 不下单、不改仓、不写交易库、不计算“避免了多少亏损”。
     `counterfactual_status=not_computed`, `avoided_pnl_usd=null`。
  3. **幂等**: 以 (持仓, 事件/判决内容) 的 sha256 作为身份。只有内容**变化**时
     才写一份不可覆盖的复核证据 (`reviews/…json`); `NO_CHANGE` 不写逐仓证据。
     `latest.json` 每轮原子覆盖 (计数 + as_of + 全部持仓 + 错误), 供评估器读。

开关
----
`EXT_EVENT_RISK_SHADOW` (默认 false, 从进程环境或 `.env.us` 读取, 每轮现读)。
关闭时: 打印 `DISABLED`、返回 0、**不打开任何数据库、不创建任何目录/文件**。

硬边界
------
  - 两个 SQLite 都以 `file:...?mode=ro` + `PRAGMA query_only=ON` 打开 (复用
    `ext_event_risk_shadow._open_ro`);
  - 无 registry / cron / config / .env 改动, 无生产调用方, 无自动动作;
  - 产物只落在 `--output-dir` (默认 `data/shadow/ext_event_risk/live/`) 下。

产物
----
    <output-dir>/latest.json                                  每轮原子覆盖
    <output-dir>/reviews/ext-risk-live-<trade>-<hash>.json    仅 REVIEW 且内容变化时新增

用法
----
    ./venv/bin/python scripts/ext_event_risk_live_shadow.py
    ./venv/bin/python scripts/ext_event_risk_live_shadow.py --as-of 2026-11-13T15:00:00Z
    ./venv/bin/python scripts/ext_event_risk_live_shadow.py \
        --trade-db data/ftseek_us.db --event-db data/event_stream.db \
        --output-dir /tmp/ext-live
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import shadow_runtime  # noqa: E402
from scripts.ext_event_risk_shadow import (  # noqa: E402
    MATERIALITY_THRESHOLD,
    SCHEMA as UNDERLYING_SCHEMA,
    _iso_utc,
    _open_ro,
    _parse_ts,
    evaluate,
)

SCHEMA = "ext_event_risk_live_shadow.v1"
ENV_FLAG = "EXT_EVENT_RISK_SHADOW"

DEFAULT_TRADE_DB = ROOT / "data" / "ftseek_us.db"
DEFAULT_EVENT_DB = ROOT / "data" / "event_stream.db"
DEFAULT_OUTPUT_DIR = ROOT / "data" / "shadow" / "ext_event_risk" / "live"
DEFAULT_ENV_FILE = shadow_runtime.DEFAULT_ENV_FILE

LIVE_NO_CHANGE = "NO_CHANGE"
LIVE_REVIEW = "REVIEW"
LIVE_VERDICTS = (LIVE_NO_CHANGE, LIVE_REVIEW)

#: 唯一持仓来源。只从 trade_records 取未平仓 EXT, 不碰 open_positions / 内存态。
OPEN_EXT_POSITION_SQL = (
    "SELECT id, decision_id, symbol, direction, entry_time, exit_time, quantity, "
    "mode, trade_mode, strategy_id "
    "FROM trade_records "
    "WHERE exit_time IS NULL AND decision_id LIKE 'EXT-%' "
    "ORDER BY id"
)

LIMITATIONS = (
    "record-only: no orders, no position changes, no DB writes, no production consumer.",
    "Live verdicts are restricted to NO_CHANGE/REVIEW; an underlying INVALIDATE is "
    "downgraded to REVIEW and only preserved as official_invalidate_candidate.",
    "Verdicts are lexical/entity-based via ext_event_risk_shadow.evaluate(); they are "
    "shadow evidence, not a production exit rule.",
    "first_seen_at is ingestion visibility time, not publication time.",
    "No counterfactual/avoided PnL is computed or claimed.",
    "Idempotency key covers position + event/verdict content, not as_of: a stable "
    "verdict does not produce a new artifact on every run.",
)


# ---------------------------------------------------------------------------
# 时间 / 小工具
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _position_context(position: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "trade_id": position.get("id"),
        "decision_id": position.get("decision_id"),
        "symbol": position.get("symbol"),
        "direction": position.get("direction"),
        "entry_time": position.get("entry_time"),
        "quantity": position.get("quantity"),
        "mode": position.get("mode"),
        "trade_mode": position.get("trade_mode"),
        "strategy_id": position.get("strategy_id"),
    }


def _assessment_event_summary(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    event = result.get("assessment_event")
    if not isinstance(event, dict):
        return None
    keys = (
        "event_id",
        "source_id",
        "source_tier",
        "event_type",
        "first_seen_at",
        "headline",
        "evidence_tier",
        "materiality",
    )
    return {key: event.get(key) for key in keys}


# ---------------------------------------------------------------------------
# 持仓读取 (只读)
# ---------------------------------------------------------------------------


def load_open_ext_positions(trade_db: Path) -> List[Dict[str, Any]]:
    """只读查询未平仓 EXT 持仓; 任何写语句都会被 `query_only` 拒绝。"""
    conn = _open_ro(trade_db)
    try:
        rows = conn.execute(OPEN_EXT_POSITION_SQL).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 实盘判决映射
# ---------------------------------------------------------------------------


def map_live_verdict(underlying_verdict: Any) -> Dict[str, Any]:
    """底层三态 → 实盘两态。INVALIDATE 只能变成带标记的 REVIEW。

    未知底层判决一律落到 REVIEW (fail-safe: 宁可让人看, 不可当无事发生)。
    """
    verdict = str(underlying_verdict or "").strip().upper()
    if verdict == "INVALIDATE":
        return {
            "live_verdict": LIVE_REVIEW,
            "official_invalidate_candidate": True,
            "downgraded_from_invalidate": True,
        }
    if verdict == "REVIEW":
        return {
            "live_verdict": LIVE_REVIEW,
            "official_invalidate_candidate": False,
            "downgraded_from_invalidate": False,
        }
    if verdict == "NO_CHANGE":
        return {
            "live_verdict": LIVE_NO_CHANGE,
            "official_invalidate_candidate": False,
            "downgraded_from_invalidate": False,
        }
    return {
        "live_verdict": LIVE_REVIEW,
        "official_invalidate_candidate": False,
        "downgraded_from_invalidate": False,
        "unknown_underlying_verdict": underlying_verdict,
    }


def content_hash(
    *,
    position: Dict[str, Any],
    underlying_verdict: Any,
    live_verdict: str,
    official_invalidate_candidate: bool,
    assessment_event: Optional[Dict[str, Any]],
    material_event_count: int,
) -> str:
    """幂等身份 = 持仓 + 事件/判决内容 (刻意不含 as_of)。"""
    event = assessment_event or {}
    return shadow_runtime.sha256_json(
        {
            "schema": SCHEMA,
            "trade_id": position.get("id"),
            "decision_id": position.get("decision_id"),
            "symbol": position.get("symbol"),
            "underlying_verdict": underlying_verdict,
            "live_verdict": live_verdict,
            "official_invalidate_candidate": bool(official_invalidate_candidate),
            "primary_event_id": event.get("event_id"),
            "primary_event_first_seen_at": event.get("first_seen_at"),
            "material_event_count": int(material_event_count),
        }
    )


def review_artifact_path(output_dir: Path, trade_id: Any, digest: str) -> Path:
    return output_dir / "reviews" / f"ext-risk-live-{trade_id}-{digest[:16]}.json"


# ---------------------------------------------------------------------------
# 单仓评估
# ---------------------------------------------------------------------------


def _summarize_underlying(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "script": "scripts/ext_event_risk_shadow.py",
        "schema": UNDERLYING_SCHEMA,
        "verdict": result.get("verdict"),
        "verdict_reason": result.get("verdict_reason"),
        "evaluation_sha256": result.get("evaluation_sha256"),
        "material_event_count": result.get("material_event_count"),
        "window": result.get("window"),
        "assessment_event": _assessment_event_summary(result),
    }


def evaluate_position(
    position: Dict[str, Any],
    *,
    trade_db: Path,
    event_db: Path,
    as_of_dt: datetime,
    materiality_threshold: float = MATERIALITY_THRESHOLD,
) -> Dict[str, Any]:
    """调用底层 evaluate() 一次, 返回实盘两态判决 (不写任何文件)。"""
    result = evaluate(
        trade_db=trade_db,
        event_db=event_db,
        trade_id=int(position["id"]),
        decision_id=position.get("decision_id"),
        as_of_override=_iso_utc(as_of_dt),
        extra_aliases=(),
        materiality_threshold=materiality_threshold,
    )
    mapped = map_live_verdict(result.get("verdict"))
    assessment_event = _assessment_event_summary(result)
    digest = content_hash(
        position=position,
        underlying_verdict=result.get("verdict"),
        live_verdict=mapped["live_verdict"],
        official_invalidate_candidate=mapped["official_invalidate_candidate"],
        assessment_event=assessment_event,
        material_event_count=int(result.get("material_event_count") or 0),
    )
    return {
        "live_verdict": mapped["live_verdict"],
        "official_invalidate_candidate": mapped["official_invalidate_candidate"],
        "downgraded_from_invalidate": mapped["downgraded_from_invalidate"],
        "unknown_underlying_verdict": mapped.get("unknown_underlying_verdict"),
        "assessment_event": assessment_event,
        "material_event_count": int(result.get("material_event_count") or 0),
        "underlying": _summarize_underlying(result),
        "content_hash": digest,
    }


def _artifact_payload(
    position: Dict[str, Any],
    evaluation: Dict[str, Any],
    *,
    as_of_dt: datetime,
    generated_at: datetime,
) -> Dict[str, Any]:
    downgrade = None
    if evaluation["downgraded_from_invalidate"]:
        downgrade = {
            "from": "INVALIDATE",
            "to": LIVE_REVIEW,
            "reason": (
                "live shadow never acts on its own; an official/primary-source "
                "contradiction is surfaced for human review only"
            ),
        }
    return {
        "schema": SCHEMA,
        "kind": "review_artifact",
        "mode": "live_record_only",
        "script": "scripts/ext_event_risk_live_shadow.py",
        "production_write": False,
        "auto_action": False,
        "orders_placed": 0,
        "counterfactual_status": "not_computed",
        "avoided_pnl_usd": None,
        "content_hash": evaluation["content_hash"],
        "first_observed_as_of": _iso_utc(as_of_dt),
        "generated_at": _iso_utc(generated_at),
        "position": _position_context(position),
        "live_verdict": evaluation["live_verdict"],
        "official_invalidate_candidate": evaluation["official_invalidate_candidate"],
        "downgrade": downgrade,
        "material_event_count": evaluation["material_event_count"],
        "assessment_event": evaluation["assessment_event"],
        "underlying": evaluation["underlying"],
        "limitations": list(LIMITATIONS),
    }


def _write_review_artifact(
    output_dir: Path,
    position: Dict[str, Any],
    evaluation: Dict[str, Any],
    *,
    as_of_dt: datetime,
    generated_at: datetime,
) -> Tuple[Path, bool]:
    """内容未变 → 既有证据原样保留 (不覆盖); 变化 → 新增一份。

    返回 (路径, 是否本次新写)。`write_json_new` 用独占创建, 并发下也不会互相覆盖。
    """
    path = review_artifact_path(output_dir, position.get("id"), evaluation["content_hash"])
    payload = _artifact_payload(
        position, evaluation, as_of_dt=as_of_dt, generated_at=generated_at
    )
    try:
        shadow_runtime.write_json_new(path, payload)
    except FileExistsError:
        return path, False
    return path, True


# ---------------------------------------------------------------------------
# 一轮运行
# ---------------------------------------------------------------------------


def _latest_payload(
    *,
    as_of_dt: datetime,
    as_of_source: str,
    generated_at: datetime,
    trade_db: Path,
    event_db: Path,
    output_dir: Path,
    positions: List[Dict[str, Any]],
    errors: List[Dict[str, Any]],
    fatal_error: Optional[str],
) -> Dict[str, Any]:
    counts = {
        "positions_total": len(positions),
        "evaluated": sum(1 for item in positions if item.get("status") == "OK"),
        "no_change": sum(
            1 for item in positions if item.get("live_verdict") == LIVE_NO_CHANGE
        ),
        "review": sum(1 for item in positions if item.get("live_verdict") == LIVE_REVIEW),
        "official_invalidate_candidates": sum(
            1 for item in positions if item.get("official_invalidate_candidate")
        ),
        "new_review_artifacts": sum(
            1 for item in positions if item.get("artifact_written")
        ),
        "errors": len(errors),
    }
    return {
        "schema": SCHEMA,
        "mode": "live_record_only",
        "status": "ERROR" if fatal_error or errors else "OK",
        "script": "scripts/ext_event_risk_live_shadow.py",
        "env_flag": ENV_FLAG,
        "enabled": True,
        "production_write": False,
        "auto_action": False,
        "orders_placed": 0,
        "counterfactual_status": "not_computed",
        "avoided_pnl_usd": None,
        "generated_at": _iso_utc(generated_at),
        "as_of": _iso_utc(as_of_dt),
        "as_of_source": as_of_source,
        "db_paths": {
            "trade_db": str(Path(trade_db).expanduser().resolve()),
            "event_db": str(Path(event_db).expanduser().resolve()),
        },
        "output_dir": str(Path(output_dir).expanduser().resolve()),
        "position_source": {
            "table": "trade_records",
            "sql": OPEN_EXT_POSITION_SQL,
        },
        "counts": counts,
        "positions": positions,
        "errors": errors,
        "fatal_error": fatal_error,
        "limitations": list(LIMITATIONS),
    }


def run(
    *,
    trade_db: Path,
    event_db: Path,
    output_dir: Path,
    as_of_dt: datetime,
    as_of_source: str,
    materiality_threshold: float = MATERIALITY_THRESHOLD,
) -> Tuple[Dict[str, Any], int]:
    """已启用时的一轮: 逐仓评估 → 幂等证据 → 原子写 latest。

    返回 (latest, exit_code)。单仓失败不中断整轮, 但会进 `errors` 并使退出码为 2
    (与 latest.status=ERROR 对齐; 未被评估的持仓是观测盲区, 不得静默);
    持仓表本身打不开视为致命错, 仍写 latest 留痕并返回 2。
    """
    generated_at = _now_utc()
    positions_out: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    fatal_error: Optional[str] = None

    try:
        open_positions = load_open_ext_positions(trade_db)
    except (FileNotFoundError, sqlite3.Error) as exc:
        fatal_error = f"{type(exc).__name__}: {exc}"
        open_positions = []
        print(f"error: cannot read open EXT positions: {fatal_error}", file=sys.stderr)

    for position in open_positions:
        context = _position_context(position)
        entry = {
            **context,
            "status": "OK",
            "live_verdict": None,
            "underlying_verdict": None,
            "official_invalidate_candidate": False,
            "downgraded_from_invalidate": False,
            "material_event_count": 0,
            "assessment_event": None,
            "content_hash": None,
            "artifact": None,
            "artifact_written": False,
            "error": None,
        }
        try:
            evaluation = evaluate_position(
                position,
                trade_db=trade_db,
                event_db=event_db,
                as_of_dt=as_of_dt,
                materiality_threshold=materiality_threshold,
            )
        except (FileNotFoundError, LookupError, ValueError, sqlite3.Error) as exc:
            message = f"{type(exc).__name__}: {exc}"
            entry["status"] = "ERROR"
            entry["error"] = message
            errors.append({**context, "error": message})
            print(
                f"error: trade_id={context.get('trade_id')} evaluation failed: {message}",
                file=sys.stderr,
            )
            positions_out.append(entry)
            continue

        entry.update(
            {
                "live_verdict": evaluation["live_verdict"],
                "underlying_verdict": evaluation["underlying"]["verdict"],
                "official_invalidate_candidate": evaluation[
                    "official_invalidate_candidate"
                ],
                "downgraded_from_invalidate": evaluation["downgraded_from_invalidate"],
                "material_event_count": evaluation["material_event_count"],
                "assessment_event": evaluation["assessment_event"],
                "content_hash": evaluation["content_hash"],
            }
        )
        if evaluation["live_verdict"] == LIVE_REVIEW:
            try:
                path, written = _write_review_artifact(
                    output_dir,
                    position,
                    evaluation,
                    as_of_dt=as_of_dt,
                    generated_at=generated_at,
                )
            except OSError as exc:
                message = f"{type(exc).__name__}: {exc}"
                entry["status"] = "ERROR"
                entry["error"] = f"artifact write failed: {message}"
                errors.append(
                    {**context, "error": f"artifact write failed: {message}"}
                )
                print(
                    f"error: trade_id={context.get('trade_id')} artifact failed: {message}",
                    file=sys.stderr,
                )
                positions_out.append(entry)
                continue
            entry["artifact"] = str(path)
            entry["artifact_written"] = written
        positions_out.append(entry)

    latest = _latest_payload(
        as_of_dt=as_of_dt,
        as_of_source=as_of_source,
        generated_at=generated_at,
        trade_db=trade_db,
        event_db=event_db,
        output_dir=output_dir,
        positions=positions_out,
        errors=errors,
        fatal_error=fatal_error,
    )
    latest_path = Path(output_dir) / "latest.json"
    try:
        shadow_runtime.atomic_write_json(latest_path, latest)
    except OSError as exc:
        print(
            f"error: failed to write latest.json: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return latest, 2

    counts = latest["counts"]
    print(
        "EXT event risk live shadow: "
        f"as_of={latest['as_of']} positions={counts['positions_total']} "
        f"no_change={counts['no_change']} review={counts['review']} "
        f"new_artifacts={counts['new_review_artifacts']} errors={counts['errors']}"
    )
    print(f"latest: {latest_path}")
    print("record-only: no orders, no DB writes, no avoided PnL")
    return latest, (2 if fatal_error or errors else 0)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Continuous record-only event-risk shadow for open EXT positions "
            "(two-state: NO_CHANGE/REVIEW; no orders, no DB writes)."
        )
    )
    parser.add_argument("--trade-db", type=Path, default=DEFAULT_TRADE_DB)
    parser.add_argument("--event-db", type=Path, default=DEFAULT_EVENT_DB)
    parser.add_argument(
        "--as-of",
        default=None,
        help="UTC ISO timestamp; default = now (UTC)",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--materiality-threshold",
        type=float,
        default=MATERIALITY_THRESHOLD,
        help="passed through to ext_event_risk_shadow.evaluate() (default 0.40)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help="fallback env file for EXT_EVENT_RISK_SHADOW (default .env.us)",
    )
    parser.add_argument("--print-json", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if not shadow_runtime.env_flag(ENV_FLAG, env_file=args.env_file):
        print(f"DISABLED: {ENV_FLAG} is false (no DB read, no file written)")
        return 0

    if args.as_of:
        try:
            as_of_dt = _parse_ts(args.as_of, field="--as-of")
        except ValueError as exc:
            print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        as_of_source = "cli"
    else:
        as_of_dt = _now_utc()
        as_of_source = "now"

    latest, exit_code = run(
        trade_db=args.trade_db,
        event_db=args.event_db,
        output_dir=args.output_dir,
        as_of_dt=as_of_dt,
        as_of_source=as_of_source,
        materiality_threshold=args.materiality_threshold,
    )
    if args.print_json:
        print(json.dumps(latest, ensure_ascii=False, indent=2, default=str))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())