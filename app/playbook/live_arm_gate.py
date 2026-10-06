"""Single authority for the LIVE engineering-probe entry gate."""

from __future__ import annotations

import sqlite3
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from app.utils.timezone import now_et


_RTH_SESSION = "RTH"
_TRADING_PHASE = "TRADING"
_HALT_FILE = Path(__file__).resolve().parents[2] / "data" / "live_arm_halt.json"
# 四锁破例预注册载体 (2026-10-06 用户裁 A; RFC-135)。
# 为什么需要: live_arm_gate 要求 ALLOW_REAL_TRADING=true (L99), 而四锁/周检
# 要求 false ⇒ 判据互相矛盾, 探针在四锁体系下结构性不可运行。破例期内
# 审计侧静默 (见 scripts/okr_2026_09_scoring.py + ops/hermes/ftseek_weekly_checkpoint.py),
# 本模块负责**到期自动失效** — 与"到期自动复响"对称。
_WAIVER_FILE = Path(__file__).resolve().parents[2] / "data" / "live_probe_lock_waiver.json"


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


def read_live_probe_waiver(path: Optional[Path] = None) -> Dict[str, Any]:
    """四锁破例预注册状态 (RFC-135). fail-close: 文件缺失/坏 JSON/无到期 = 未授权.

    审计侧 (四锁 + 周检) 与本 gate 共用本函数 = 单一判定源, 不在两处各写一份
    到期解析逻辑 (那会分叉, 同 test_lock_literals_match_weekly_checkpoint 所防的病)。

    Args:
        path: 破例文件路径。None ⇒ 用生产路径 _WAIVER_FILE (gate 走这条);
              审计器传 repo_root/data/... 以保持合成仓测试的 hermetic 性。

    Returns:
      {"active": bool, "reason": str, "expires": str|None, "authorization_id": str|None,
       "locks_waived": dict, "days_left": int|None, "raw": dict}
    """
    src = Path(path) if path is not None else _WAIVER_FILE
    try:
        raw = json.loads(src.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"active": False, "reason": "waiver_file_absent", "expires": None,
                "authorization_id": None, "locks_waived": {}, "days_left": None,
                "raw": {}}
    except Exception as exc:                       # noqa: BLE001
        # 坏文件不静默当作"无破例"(那会让探针莫名停用), 但也不 fail-open
        return {"active": False, "reason": "waiver_unreadable",
                "raw_error": f"{type(exc).__name__}: {exc}", "expires": None,
                "authorization_id": None, "locks_waived": {}, "days_left": None,
                "raw": {}}

    if not isinstance(raw, dict):
        return {"active": False, "reason": "waiver_not_an_object", "expires": None,
                "authorization_id": None, "locks_waived": {}, "days_left": None,
                "raw": {}}

    expires_raw = str(raw.get("expires") or "").strip()
    auth_id = str(raw.get("authorization_id") or "").strip()
    waived = raw.get("locks_waived")
    if not isinstance(waived, dict) or not waived:
        return {"active": False, "reason": "waiver_missing_locks_waived",
                "expires": expires_raw or None, "authorization_id": auth_id or None,
                "locks_waived": {}, "days_left": None, "raw": raw}
    if not expires_raw:
        return {"active": False, "reason": "waiver_missing_expires",
                "expires": None, "authorization_id": auth_id or None,
                "locks_waived": waived, "days_left": None, "raw": raw}

    try:
        expires = datetime.fromisoformat(expires_raw)
    except ValueError:
        return {"active": False, "reason": "waiver_expires_unparsable",
                "expires": expires_raw, "authorization_id": auth_id or None,
                "locks_waived": waived, "days_left": None, "raw": raw}

    now = now_et()
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=now.tzinfo)
    days_left = int((expires - now).total_seconds() // 86400)
    if now >= expires:
        return {"active": False, "reason": "waiver_expired", "expires": expires_raw,
                "authorization_id": auth_id or None, "locks_waived": waived,
                "days_left": days_left, "raw": raw}

    return {"active": True, "reason": "waiver_active", "expires": expires_raw,
            "authorization_id": auth_id or None, "locks_waived": waived,
            "days_left": days_left, "raw": raw}


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

    # 破例到期即停 (RFC-135, 2026-10-06 用户裁 A): ALLOW_REAL_TRADING=true 是四锁
    # 判定的"松动"状态, 必须有**有效的预注册授权**覆盖才能开真钱单。到期/文件缺失/
    # 坏 JSON ⇒ 拒。与审计侧"到期自动复响"对称, 且不需要人工记得去关开关。
    # 只在 allow_real_trading=True 时判 — False 时上面 LIVE_PREREQ_INCOMPLETE 已拒。
    if allow_real_trading:
        _waiver = read_live_probe_waiver()
        if not _waiver.get("active"):
            reasons.append(_reason(
                "LIVE_PROBE_WAIVER_INACTIVE",
                "ALLOW_REAL_TRADING=true requires an active pre-registered lock waiver "
                f"(data/live_probe_lock_waiver.json); got {_waiver.get('reason')!r}",
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
