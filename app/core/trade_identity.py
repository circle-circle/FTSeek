"""Minimal trade identity shared by push and persistence."""
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional


_PROJECT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_REAL_MODES = {"live", "real", "real_money"}
_SIM_MODES = {"sim", "simulate", "simulated", "paper"}
_PHASE_ALIASES = {
    "premarket": "premarket",
    "pre-market": "premarket",
    "pre_market": "premarket",
    "regular": "regular",
    "intraday": "regular",
    "normal": "regular",
    "afterhours": "afterhours",
    "after-hours": "afterhours",
    "after_hours": "afterhours",
}


def is_test_symbol(symbol: Optional[str]) -> bool:
    normalized = str(symbol or "").strip().upper()
    return normalized in {"TEST", "US.TEST"} or normalized.endswith(".TEST")


def trade_identity(
    symbol: str,
    source_type: Optional[str] = None,
    trade_mode: Optional[str] = None,
    session: Optional[str] = None,
) -> Dict[str, str]:
    source = str(source_type or "").strip().upper()
    mode = str(trade_mode or "").strip().lower()
    if is_test_symbol(symbol):
        trade_env = "test"
    elif "LIVE" in source or mode in _REAL_MODES:
        trade_env = "real"
    elif "SIM" in source or mode in _SIM_MODES:
        trade_env = "sim"
    else:
        trade_env = "unknown"

    identity: Dict[str, str] = {"trade_env": trade_env}
    phase = _PHASE_ALIASES.get(str(session or "").strip().lower())
    if phase:
        identity["trade_phase"] = phase
    return identity


def summarize_trade_identity(rows: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    groups = {}
    for row in rows:
        metadata = row.get("metadata_json")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (TypeError, ValueError):
                metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        identity = trade_identity(
            row.get("symbol"),
            row.get("source_type"),
            row.get("trade_mode") or row.get("mode") or row.get("account_type"),
            row.get("session")
            or row.get("exit_session")
            or row.get("entry_session")
            or metadata.get("session"),
        )
        key = (
            identity["trade_env"],
            str(row.get("source_type") or "UNKNOWN").upper(),
            identity.get("trade_phase", "unknown"),
        )
        group = groups.setdefault(
            key, {"trade_env": key[0], "source_type": key[1], "trade_phase": key[2],
                  "n": 0, "wins": 0, "pnl": 0.0}
        )
        pnl = row.get("pnl_net")
        if pnl is None:
            pnl = row.get("realized_pnl") or 0.0
        group["n"] += 1
        group["wins"] += int(float(pnl) > 0)
        group["pnl"] += float(pnl)
    for group in groups.values():
        group["pnl"] = round(group["pnl"], 2)
    return sorted(groups.values(), key=lambda item: (-item["n"], item["source_type"]))


def ensure_test_symbol_not_in_production_db(
    symbol: Optional[str], db_path: Any
) -> None:
    if not is_test_symbol(symbol):
        return
    try:
        resolved = Path(db_path).resolve()
        is_production_db = resolved == _PROJECT_DATA_DIR or _PROJECT_DATA_DIR in resolved.parents
    except (TypeError, OSError):
        return
    if is_production_db:
        raise ValueError(
            f"test symbol {symbol!r} cannot be written to production DB: {resolved}"
        )
