"""PULLBACK_LIMIT 承接失败 record-only 影子（R5/R7）。

只读取现有指令、持仓与 K_1M，只追加 JSONL 和 metadata 去重状态。
模块没有券商/订单依赖，不能产生任何真实 EXIT/TIGHTEN。

预注册机制门（收益门不在本切片可达）：
- ≥30 个 FAILED_RECLAIM 独立仓且 ≥10 ET 日；先报覆盖率、触发率、相对
  early-tight/DEAD_SIGNAL 提前量，以及实际盈利仓误伤，再报 paired Δ。
- ACTIVE 臂内早期失败池仅 SL_INITIAL 10 笔 -$864.48；即便减少 30%–50%，
  约 $5–8/日、ITT $0.6–0.9/笔 ⇒ 本切片只允许机制结论，不允许盈利宣称。
- 触发过少/过高、覆盖不足、误伤或 paired Δ 为负即停止，不网格救参。
- R17：FAILED_RECLAIM 每仓只记一次终态；后续缺数据不得重新打开，前向路径照记。

开关：playbook.thesis_management_shadow.enabled（代码默认 false）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from app.playbook.invalidation_contract import compile_pullback_failure_rule
from app.playbook.pullback_failure import (
    ABSTAIN,
    FAILED_RECLAIM,
    bars_after_fill_window,
    decision_fingerprint,
    evaluate_pullback,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "thesis_management_shadow_v1"
DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "shadow" / "thesis_management_shadow.jsonl"
FORWARD_PATH = Path(__file__).resolve().parents[2] / "data" / "shadow" / "thesis_management_path.jsonl"
_DEFAULTS = {
    "enabled": False,
    "max_plan_gap_pct": 1.0,
    "max_bar_age_minutes": 15.0,
}
_INSTRUCTION_CACHE: Dict[str, Dict[str, Any]] = {}


def get_config() -> Dict[str, Any]:
    cfg = dict(_DEFAULTS)
    try:
        from app.core.config_container import get_config as _gc
        override = _gc().get_dotted("playbook.thesis_management_shadow", None)
    except Exception:
        override = None
    if isinstance(override, dict):
        for key, value in override.items():
            if key in cfg and value is not None:
                cfg[key] = value
    return cfg


def _raw_instruction(conn, instruction_id: Any) -> Dict[str, Any]:
    if instruction_id in (None, ""):
        return {}
    key = str(instruction_id)
    if key in _INSTRUCTION_CACHE:
        return _INSTRUCTION_CACHE[key]
    raw: Any = None
    try:
        row = conn.execute(
            "SELECT raw_instruction_json FROM playbook_instructions WHERE id = ?",
            (instruction_id,),
        ).fetchone()
        raw = row[0] if row else None
    except Exception as exc:
        logger.warning("[thesis-shadow] 指令读取失败 id=%s: %s", key, exc, exc_info=True)
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = {}
    result = raw if isinstance(raw, dict) else {}
    _INSTRUCTION_CACHE[key] = result
    return result


def _pivot(raw: Dict[str, Any]) -> Optional[float]:
    confirmation = raw.get("confirmation")
    if not isinstance(confirmation, dict):
        return None
    value = confirmation.get("price_level")
    try:
        value = float(str(value).replace(",", ""))
        return value if value > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _plan_hash(*, trade_id: Any, direction: str, pivot: float,
               fill: Any, hard_sl: Any, entry_time: Any) -> str:
    payload = {
        "schema": SCHEMA_VERSION,
        "trade_id": trade_id,
        "archetype": "PULLBACK_LIMIT",
        "direction": direction,
        "pivot": pivot,
        "fill": fill,
        "hard_sl": hard_sl,
        "entry_time": str(entry_time or ""),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _fetch_bars(data_router, symbol: str):
    futu = getattr(data_router, "futu", None)
    if futu is None or not hasattr(futu, "get_kline"):
        return []
    # 与 _track_bar_peak_trough 同源、同 unadjusted 口径；60s 缓存限制额外配额。
    return futu.get_kline(
        symbol, ktype="K_1M", count=800, autype="NONE", latency_sensitive=True,
    ) or []


def _append_forward_path(*, row, meta, bars_1m, observed_at, path) -> bool:
    """按 trade_id 追加新到达的 post-fill 1m bar；重启用 metadata 去重。"""
    boundary_bars = bars_after_fill_window(
        bars_1m, entry_time=row["entry_time"], observed_at=observed_at,
    )
    if not boundary_bars:
        return False
    previous = str(meta.get("_thesis_path_last_bar") or "")
    new_bars = [bar for bar in boundary_bars if bar["time"] > previous]
    if not new_bars:
        return False
    target = Path(path) if path is not None else FORWARD_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    ingest = "initial_snapshot" if not previous else "incremental"
    with target.open("a", encoding="utf-8") as handle:
        for bar in new_bars:
            handle.write(json.dumps({
                "schema_version": "thesis_management_path_v1",
                "trade_id": row["id"],
                "symbol": row["symbol"],
                "known_at": observed_at.isoformat() if hasattr(observed_at, "isoformat") else str(observed_at),
                "ingest": ingest,
                **bar,
            }, ensure_ascii=False) + "\n")
    meta["_thesis_path_last_bar"] = new_bars[-1]["time"]
    return True


def record_censored(*, conn, row, blocked: Dict[str, Any], observed_at: Any,
                    path: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    """在 blocked_exits 分支显式留 censored，避免冻结窗被误读为无触发。"""
    cfg = get_config()
    if not cfg.get("enabled"):
        return None
    if path is None and os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    try:
        meta = json.loads(row.get("metadata_json") or "{}")
        if not isinstance(meta, dict):
            meta = {}
        intent_id = str(blocked.get("intent_id") or "")
        marker = f"censored:{intent_id}"
        if meta.get("_thesis_censored_intent") == marker:
            return None
        result = {
            "schema_version": SCHEMA_VERSION,
            "trade_id": row["id"],
            "symbol": row["symbol"],
            "direction": row["direction"],
            "instruction_id": meta.get("playbook_instruction_id"),
            "inst_id": meta.get("playbook_instruction_id"),
            "decision": "CENSORED",
            "reason": "blocked_exits",
            "plan_status": "CENSORED",
            "known_at": observed_at.isoformat(),
            "blocked_intent_id": intent_id or None,
            "blocked_state": blocked.get("state"),
            "blocked_created_at": blocked.get("created_at"),
            "quote_price": None,
            "quote_age_s": None,
            "execution": "RECORD_ONLY",
        }
        target = Path(path) if path is not None else DEFAULT_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
        meta["_thesis_censored_intent"] = marker
        conn.execute("UPDATE trade_records SET metadata_json = ? WHERE id = ?",
                     (json.dumps(meta, ensure_ascii=False), row["id"]))
        conn.commit()
        row["metadata_json"] = json.dumps(meta, ensure_ascii=False)
        return result
    except Exception as exc:
        logger.warning("[thesis-shadow] censored 落盘失败（不影响恢复）: %s", exc, exc_info=True)
        return None


def record(
    *, conn, row: Dict[str, Any], meta: Dict[str, Any], data_router,
    current_price: Any, quote_source: str = "", quote_age_s: Any = None,
    old_exit_reason: str = "", observed_at: Any = None,
    path: Optional[Any] = None, bars_1m: Any = None,
    forward_path: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """评估并落一行状态变化；失败返回 None，不向调用方抛异常。"""
    cfg = get_config()
    if not cfg.get("enabled"):
        return None
    if path is None and os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    try:
        instruction_id = meta.get("playbook_instruction_id")
        raw = _raw_instruction(conn, instruction_id)
        if str(raw.get("entry_archetype") or "").upper() != "PULLBACK_LIMIT":
            return None
        pivot = _pivot(raw)
        if pivot is None:
            return None
        now = observed_at or datetime.now().astimezone()
        if bars_1m is None:
            bars_1m = _fetch_bars(data_router, row["symbol"])
        result = evaluate_pullback(
            direction=row["direction"], pivot=pivot,
            fill_price=row["entry_price"], entry_time=row["entry_time"],
            bars_1m=bars_1m, observed_at=now,
            max_plan_gap_pct=float(cfg["max_plan_gap_pct"]),
            max_bar_age_minutes=float(cfg["max_bar_age_minutes"]),
        )
        plan_hash = _plan_hash(
            trade_id=row["id"], direction=row["direction"], pivot=pivot,
            fill=row["entry_price"], hard_sl=meta.get("stop_loss"),
            entry_time=row["entry_time"],
        )
        structure_hash = hashlib.sha256(
            f"{plan_hash}|{decision_fingerprint(result)}".encode("utf-8")
        ).hexdigest()[:16]
        # 即使决策未变化，也持续记录新到达的 post-fill 1m 路径（paired Δ 结算底座）。
        path_changed = False
        if result.get("plan_status") == "ACTIVE":
            path_changed = _append_forward_path(
                row=row, meta=meta, bars_1m=bars_1m,
                observed_at=now, path=forward_path,
            )
        decision_key = f"{result['decision']}:{result['reason']}"
        previous = meta.get("_thesis_shadow_last")
        # R17：FAILED_RECLAIM 是每仓终态，不因后续缺 bar/stale/WAIT 重新打开。
        # 数据在完整/缺失间振荡时仍续记前向路径，但绝不再发第二个决策行。
        if isinstance(previous, dict) and previous.get("decision_key", "").startswith(
                f"{FAILED_RECLAIM}:"):
            if path_changed:
                return {"execution": "RECORD_ONLY", "path_only": True,
                        "trade_id": row["id"], "state_hash": structure_hash}
            return None
        if isinstance(previous, dict) and previous.get("decision_key") == decision_key:
            if path_changed:
                return {"execution": "RECORD_ONLY", "path_only": True,
                        "trade_id": row["id"], "state_hash": structure_hash}
            return None

        shadow_row = {
            "schema_version": SCHEMA_VERSION,
            "trade_id": row["id"],
            "symbol": row["symbol"],
            "direction": row["direction"],
            "instruction_id": instruction_id,
            "inst_id": instruction_id,  # shadow_registry distinct_where 的去重键
            "decision": result["decision"],
            "reason": result["reason"],
            "plan_status": result["plan_status"],
            "known_at": result["known_at"],
            "pivot": pivot,
            "fill": row["entry_price"],
            "hard_sl": meta.get("stop_loss"),
            "plan_gap_pct": result.get("plan_gap_pct"),
            "plan_hash": plan_hash,
            "state_hash": structure_hash,
            "bar1": result.get("bar1"),
            "bar2": result.get("bar2"),
            "bar_age_minutes": result.get("bar_age_minutes"),
            "quote_price": current_price,
            "quote_source": quote_source or None,
            "quote_age_s": quote_age_s,
            "old_exit_reason_same_tick": old_exit_reason or None,
            "contract_rule": compile_pullback_failure_rule(
                direction=row["direction"], pivot=pivot, known_at=result["known_at"],
            ),
            "execution": "RECORD_ONLY",
        }
        target = Path(path) if path is not None else DEFAULT_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(shadow_row, ensure_ascii=False) + "\n")
        meta["_thesis_shadow_last"] = {
            "trade_id": row["id"],
            "decision_key": decision_key,
            "state_hash": structure_hash,
            "known_at": result["known_at"],
        }
        return shadow_row
    except Exception as exc:
        logger.warning("[thesis-shadow] record-only 失败（不影响退出）: %s", exc, exc_info=True)
        return None
