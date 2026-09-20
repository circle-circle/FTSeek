#!/opt/ftseek/FTSeek-US/venv/bin/python3
"""T3 fee gate shadow diagnostic report and PushHub dispatcher.

The report links the shadow counterfactual to the production admission gate:
the forward proposal must preserve the current price floor and only test the
notional floor. It also classifies the dominant adjustment direction and the
measurement chain, then pushes only on new samples, state transitions, or
chain/data-quality anomalies.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from app.push import Category, Level, PushMessage, get_push_hub
from scripts.fee_gate_shadow import _arm_stats

ROOT = Path(__file__).resolve().parents[1]
JSONL_PATH = ROOT / "data" / "fee_gate_shadow.jsonl"
LATEST_PATH = ROOT / "data" / "fee_gate_shadow_latest.json"
STATE_PATH = ROOT / "data" / "fee_gate_shadow_push_state.json"
CONFIG_PATH = ROOT / "config" / "unified_config.yaml"
MIN_N = 30
STALE_HOURS = 26
CANDIDATE_NOTIONALS = (4000.0, 5000.0)
CANDIDATE_PRICE = 1.0
ET = ZoneInfo("America/New_York")

URGENT_STATUSES = {
    "CHAIN_BROKEN",
    "CHAIN_STALE",
    "CURRENT_GATE_NOT_ENFORCED",
    "CURRENT_GATE_BYPASS",
    "DATA_QUALITY_WARNING",
    "UNSAFE_CANDIDATE_LOOSENS_CURRENT",
}

logger = logging.getLogger("ftseek.fee_gate_shadow_report")


def load_current_gate(config_path: Path = CONFIG_PATH) -> dict[str, Any]:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    admission = config.get("playbook", {}).get("admission", {}) or {}
    return {
        "enabled": bool(admission.get("enabled", False)),
        "enforce": bool(admission.get("enforce", False)),
        "min_entry_price": float(admission.get("min_entry_price", 0.0) or 0.0),
        "min_notional_usd": float(admission.get("min_notional_usd", 0.0) or 0.0),
    }


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("Skipping malformed fee gate report row")
    return rows


def _entry_session(entry_time: str | None) -> str:
    if not entry_time:
        return "unknown"
    try:
        parsed = datetime.fromisoformat(entry_time)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ET)
        et = parsed.astimezone(ET)
        hhmm = et.hour * 100 + et.minute
        if 400 <= hhmm < 930:
            return "premarket"
        if 930 <= hhmm < 1600:
            return "regular"
        if 1600 <= hhmm <= 2000:
            return "afterhours"
        return "outside"
    except ValueError:
        return "unknown"


def _parse_updated_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _hours_since(updated_at: datetime | None, now: datetime) -> float | None:
    if updated_at is None:
        return None
    return max(0.0, (now - updated_at).total_seconds() / 3600.0)


def _classify_rows(rows: list[dict[str, Any]], gate: dict[str, Any]) -> dict[str, Any]:
    active = bool(gate["enabled"] and gate["enforce"])
    min_price = gate["min_entry_price"]
    min_notional = gate["min_notional_usd"]

    current_blocked = []
    current_kept = []
    safe_by_floor = {
        str(int(floor)): {"blocked": [], "kept": []}
        for floor in CANDIDATE_NOTIONALS
    }
    unsafe_by_floor = {
        str(int(floor)): [] for floor in CANDIDATE_NOTIONALS
    }
    sessions = {}

    for row in rows:
        price = float(row["entry_price"])
        notional = float(row["notional"])
        current_block = active and (price < min_price or notional < min_notional)
        if current_block:
            current_blocked.append(row)
        else:
            current_kept.append(row)
        for floor in CANDIDATE_NOTIONALS:
            candidate_block = price < CANDIDATE_PRICE or notional < floor
            safe_block = active and (price < min_price or notional < floor)
            if safe_block:
                safe_by_floor[str(int(floor))]["blocked"].append(row)
            else:
                safe_by_floor[str(int(floor))]["kept"].append(row)
            if active and current_block and not candidate_block:
                unsafe_by_floor[str(int(floor))].append(row)

        session = _entry_session(row.get("entry_time"))
        sessions[session] = sessions.get(session, 0) + 1

    return {
        "current_active": active,
        "current_blocked": _arm_stats(current_blocked),
        "current_kept": _arm_stats(current_kept),
        "safe_incremental_by_floor": {
            floor: {
                "blocked": _arm_stats(arms["blocked"]),
                "kept": _arm_stats(arms["kept"]),
            }
            for floor, arms in safe_by_floor.items()
        },
        "safe_incremental_blocked": _arm_stats(safe_by_floor["4000"]["blocked"]),
        "safe_incremental_kept": _arm_stats(safe_by_floor["4000"]["kept"]),
        "unsafe_loosened_by_floor": {
            floor: _arm_stats(rows) for floor, rows in unsafe_by_floor.items()
        },
        "unsafe_loosened": _arm_stats(unsafe_by_floor["4000"]),
        "unsafe_loosened_rows": unsafe_by_floor["4000"],
        "current_bypass_rows": current_blocked,
        "sessions": sessions,
    }


def _status(report: dict[str, Any]) -> str:
    if report["chain_broken"]:
        return "CHAIN_BROKEN"
    if report["chain_stale"]:
        return "CHAIN_STALE"
    if not report["gate"]["enabled"] or not report["gate"]["enforce"]:
        return "CURRENT_GATE_NOT_ENFORCED"
    if report["classification"]["current_blocked"]["n"] > 0:
        return "CURRENT_GATE_BYPASS"
    if report["excluded_rows"] > 0:
        return "DATA_QUALITY_WARNING"
    if any(
        arms["n"] > 0
        for arms in report["classification"]["unsafe_loosened_by_floor"].values()
    ):
        return "UNSAFE_CANDIDATE_LOOSENS_CURRENT"
    if report["n"] < MIN_N:
        return "INSUFFICIENT_N"

    baseline = report["baseline"]
    for arms in report["classification"]["safe_incremental_by_floor"].values():
        blocked = arms["blocked"]
        kept = arms["kept"]
        if (
            kept["exp_per_trade"] is not None
            and baseline["exp_per_trade"] is not None
            and blocked["exp_per_trade"] is not None
            and kept["exp_per_trade"] > baseline["exp_per_trade"]
            and blocked["exp_per_trade"] < kept["exp_per_trade"]
            and kept["fee_bps"] is not None
            and baseline["fee_bps"] is not None
            and kept["fee_bps"] < baseline["fee_bps"]
        ):
            return "CANDIDATE_SUPPORTED"
    return "CANDIDATE_NOT_SUPPORTED"


def _diagnosis(report: dict[str, Any]) -> dict[str, str]:
    classification = report["classification"]
    bypass_rows = classification["current_bypass_rows"]
    if bypass_rows:
        low_price = [row for row in bypass_rows if float(row["entry_price"]) < report["gate"]["min_entry_price"]]
        if low_price:
            return {
                "label": "SETTLEMENT_FEE_TOXIC / CURRENT_GATE_BYPASS",
                "direction": "修复现行价格门绕过，先不新增任何门",
                "chain": "trade_records → fee_gate_shadow → admission/enforcement check",
            }
        return {
            "label": "CURRENT_NOTIONAL_GATE_BYPASS",
            "direction": "修复现行名义额门绕过，先不新增任何门",
            "chain": "trade_records → fee_gate_shadow → admission/enforcement check",
        }
    if report["n"] < MIN_N:
        return {
            "label": "INSUFFICIENT_N",
            "direction": "继续攒样，不改参数",
            "chain": "trade_records → fee_gate_shadow.py → report → PushHub",
        }
    if classification["safe_incremental_blocked"]["n"] > 0:
        return {
            "label": "FEE_DOMINANT",
            "direction": "保持现行价格门，测试名义额 2500→4000/5000",
            "chain": "trade_records → fee_gate_shadow → admission.min_notional_usd",
        }
    return {
        "label": "NO_INCREMENTAL_FEE_LEAK",
        "direction": "费用门不是当前主漏损，转向退出/信号质量",
        "chain": "trade_records → fee_gate_shadow → exit/signal scorecards",
    }


def build_report(
    jsonl_path: Path = JSONL_PATH,
    latest_path: Path = LATEST_PATH,
    config_path: Path = CONFIG_PATH,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    chain_broken = not jsonl_path.exists() or not latest_path.exists()
    latest = {} if chain_broken else _read_json(latest_path)
    rows = [] if chain_broken else _read_rows(jsonl_path)
    gate = load_current_gate(config_path)
    updated_at = _parse_updated_at(latest.get("updated_at"))
    age_hours = _hours_since(updated_at, now)

    report = {
        "schema_version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "updated_at": latest.get("updated_at"),
        "age_hours": round(age_hours, 2) if age_hours is not None else None,
        "chain_broken": chain_broken,
        "chain_stale": age_hours is None or age_hours > STALE_HOURS,
        "excluded_rows": int(latest.get("excluded_rows", 0) or 0),
        "n": len(rows),
        "min_n": MIN_N,
        "gate": gate,
        "baseline": _arm_stats(rows),
        "classification": _classify_rows(rows, gate),
        "candidate": {
            "price_floor": CANDIDATE_PRICE,
            "notional_floors": list(CANDIDATE_NOTIONALS),
            "safe_semantics": "保持现行生产价格门，只测试上调名义额门",
        },
    }
    report["status"] = _status(report)
    report["diagnosis"] = _diagnosis(report)
    return report


def render_report(report: dict[str, Any]) -> str:
    gate = report["gate"]
    classification = report["classification"]
    diagnosis = report["diagnosis"]
    unsafe = classification["unsafe_loosened"]
    lines = [
        "🧮 T3 费用门影子 · 净利诊断",
        (
            f"样本 {report['n']}/{report['min_n']} | 排除行 {report['excluded_rows']} | "
            f"更新 {report['updated_at'] or 'missing'}"
        ),
        (
            f"现行门 price≥${gate['min_entry_price']:g} & notional≥${gate['min_notional_usd']:g} "
            f"({'active' if classification['current_active'] else 'inactive'})"
        ),
        "安全增量: 保持现行价格门，只测试名义额门",
        *[
            (
                f"  ${int(floor)}: 会拦 {arms['blocked']['n']} 笔 / "
                f"保留 {arms['kept']['n']} 笔"
            )
            for floor, arms in classification["safe_incremental_by_floor"].items()
        ],
    ]
    if unsafe["n"]:
        lines.append(
            f"⚠️ 直接候选门会放宽现行价格门 {unsafe['n']} 笔 — 禁止按 price≥$1 直接转正"
        )
    for floor, arms in classification["safe_incremental_by_floor"].items():
        if arms["blocked"]["n"]:
            lines.append(
                f"  ${int(floor)} 反事实: 被拦 exp {arms['blocked']['exp_per_trade']} / "
                f"保留 exp {arms['kept']['exp_per_trade']} | "
                f"保留 fee {arms['kept']['fee_bps']}bps "
                f"vs 基线 {report['baseline']['fee_bps']}bps"
            )
    lines.extend([
        f"诊断 {report['status']} · {diagnosis['label']}",
        f"方向 {diagnosis['direction']}",
        f"链路 {diagnosis['chain']}",
        f"下一步 {'继续攒样，不改参数' if report['n'] < MIN_N else '进入10/11终裁，不自动生效'}",
    ])
    return "\n".join(lines)


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def should_push(report: dict[str, Any], state: dict[str, Any], force: bool = False) -> bool:
    if force:
        return True
    if not state:
        return True
    if report["status"] in URGENT_STATUSES:
        return state.get("last_status") != report["status"]
    return report["n"] != state.get("last_n") or report["status"] != state.get("last_status")


def _message(report: dict[str, Any]) -> PushMessage:
    level = Level.HIGH if report["status"] in URGENT_STATUSES else Level.NORMAL
    return PushMessage(
        title="T3 费用门影子诊断",
        body=render_report(report),
        level=level,
        category=Category.SHADOW_COMPARE,
        dedup_key=f"fee_gate_shadow:{report['n']}:{report['status']}",
        payload={
            "status": report["status"],
            "n": report["n"],
            "diagnosis": report["diagnosis"]["label"],
        },
    )


def run_report(
    jsonl_path: Path = JSONL_PATH,
    latest_path: Path = LATEST_PATH,
    config_path: Path = CONFIG_PATH,
    state_path: Path = STATE_PATH,
    push: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    report = build_report(
        jsonl_path=jsonl_path,
        latest_path=latest_path,
        config_path=config_path,
    )
    state = _load_state(state_path)
    should_send = should_push(report, state, force=force)
    pushed = False
    if push and should_send:
        try:
            pushed = bool(get_push_hub().push(_message(report), sync=True))
        except Exception as exc:
            logger.error("[fee_gate_shadow_report] PushHub dispatch failed: %s", exc)
            pushed = False
        if pushed:
            _write_state(state_path, {
                "last_n": report["n"],
                "last_status": report["status"],
                "last_pushed_at": report["generated_at"],
                "last_push_key": _message(report).dedup_key,
            })
    return {
        "report": report,
        "rendered": render_report(report),
        "should_push": should_send,
        "pushed": pushed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="T3 fee gate shadow diagnostic report")
    parser.add_argument("--push", action="store_true", help="dispatch through PushHub")
    parser.add_argument("--force", action="store_true", help="push even without new state")
    args = parser.parse_args()
    result = run_report(push=args.push, force=args.force)
    print(result["rendered"])
    if args.push:
        print(f"pushed={result['pushed']}")
    return 0


if __name__ == "__main__":
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        )
    sys.exit(main())
