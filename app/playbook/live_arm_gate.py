"""Single authority for the LIVE engineering-probe entry gate."""

from __future__ import annotations

import sqlite3
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, Optional

from app.utils.timezone import now_et


_RTH_SESSION = "RTH"
_TRADING_PHASE = "TRADING"
_HALT_FILE = Path(__file__).resolve().parents[2] / "data" / "live_arm_halt.json"


def _reason(code: str, message: str) -> Dict[str, str]:
    return {"code": code, "message": message}


def count_open_live_positions(live_ledger: Path) -> Optional[int]:
    """Read the LIVE open-position count; return None to force a fail-closed gate."""
    try:
        ledger_path = Path(live_ledger)
        if not ledger_path.exists():
            return 0
        connection = sqlite3.connect(f"file:{ledger_path.resolve()}?mode=ro", uri=True, timeout=3)
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM trade_records "
                "WHERE source_type='PLAYBOOK_LIVE' "
                "AND (exit_time IS NULL OR exit_time='')"
            ).fetchone()
            return int(row[0] or 0) if row else 0
        finally:
            connection.close()
    except Exception:
        return None


def read_live_arm_halt() -> Dict[str, Any]:
    """Read the persistent HALT_NEW_LIVE state without creating a file."""
    try:
        return json.loads(_HALT_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        return {"halted": True, "reason": "halt_state_unreadable", "raw_error": "invalid JSON"}


def halt_live_new_positions(reason: str) -> Dict[str, Any]:
    """Persist HALT_NEW_LIVE; existing positions remain under the exit monitor."""
    state = {
        "halted": True,
        "reason": str(reason),
        "created_at": now_et().isoformat(timespec="seconds"),
    }
    _HALT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _HALT_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def clear_live_arm_halt(reason: str = "manual_clear") -> None:
    """Manual-only recovery after a LIVE halt has been investigated."""
    state = {"halted": False, "reason": reason,
             "cleared_at": now_et().isoformat(timespec="seconds")}
    _HALT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _HALT_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def evaluate_live_arm_gate(
    arm_config: Mapping[str, Any],
    *,
    allow_real_trading: bool,
    real_account_id: str,
    lane_db_name: str,
    instruction_direction: str,
    market_phase: str,
    open_live_positions: Optional[int],
    halted: bool = False,
) -> Dict[str, Any]:
    """Evaluate every LIVE engineering-probe precondition without side effects."""
    reasons = []
    arm = arm_config if isinstance(arm_config, Mapping) else {}
    probe = arm.get("probe") if isinstance(arm.get("probe"), Mapping) else {}

    if not bool(arm.get("enabled", False)):
        reasons.append(_reason("LIVE_ARM_NOT_ENABLED", "LIVE arm is not armed"))

    if halted:
        reasons.append(_reason("LIVE_NEW_POSITIONS_HALTED", "LIVE new positions are halted by a prior safety event"))

    mode = str(arm.get("mode") or "engineering_probe")
    if mode != "engineering_probe":
        reasons.append(_reason("LIVE_MODE_UNSUPPORTED", f"Unsupported LIVE mode: {mode!r}"))

    if not (allow_real_trading and str(real_account_id or "").strip() and lane_db_name == "ftseek_us_live"):
        reasons.append(_reason(
            "LIVE_PREREQ_INCOMPLETE",
            "ALLOW_REAL_TRADING, REAL_ACCOUNT_ID, or LIVE instruction lane is incomplete",
        ))

    if not bool(probe.get("authorized", False)) or not str(probe.get("authorization_id") or "").strip():
        reasons.append(_reason("LIVE_PROBE_NOT_AUTHORIZED", "Engineering probe authorization is missing"))

    probe_direction = str(probe.get("direction") or "LONG").upper()
    instruction_direction = str(instruction_direction or "").upper()
    if probe_direction != "LONG" or instruction_direction != "BUY":
        reasons.append(_reason(
            "LIVE_PROBE_DIRECTION_REJECTED",
            "First engineering probe accepts LONG/BUY only",
        ))

    try:
        max_order_qty = int(arm.get("max_order_qty", 1) or 0)
        max_open_positions = int(arm.get("max_open_positions", 1) or 0)
    except (TypeError, ValueError):
        max_order_qty = 0
        max_open_positions = 0
    if max_order_qty != 1 or max_open_positions != 1:
        reasons.append(_reason(
            "LIVE_PROBE_LIMIT_INVALID",
            "Engineering probe requires max_order_qty=1 and max_open_positions=1",
        ))

    session = str(arm.get("session") or _RTH_SESSION).upper()
    market_phase = str(market_phase or "").upper()
    if session != _RTH_SESSION:
        reasons.append(_reason("LIVE_SESSION_UNSUPPORTED", f"Unsupported LIVE session: {session!r}"))
    if market_phase != _TRADING_PHASE:
        reasons.append(_reason("LIVE_MARKET_PHASE_REJECTED", f"LIVE probe requires RTH trading phase, got {market_phase!r}"))

    if open_live_positions is None:
        reasons.append(_reason(
            "LIVE_OPEN_COUNT_UNAVAILABLE",
            "LIVE open-position count failed, HALT_NEW_LIVE",
        ))
    elif open_live_positions >= max_open_positions:
        reasons.append(_reason(
            "LIVE_PROBE_OPEN_CAP_REACHED",
            f"Open LIVE positions={open_live_positions}, cap={max_open_positions}",
        ))

    return {"allowed": not reasons, "reasons": reasons}
