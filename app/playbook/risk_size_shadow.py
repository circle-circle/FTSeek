"""RFC-155 P2 前瞻记录影子: 只记录风险尺寸乘数, 不改实际下单数量.

每条走到 execute_instruction 的指令落一行 data/shadow/risk_size_tier_forward.jsonl:
  flags (ATR>=5% / 开盘0-15min / UE) + 六种规则的 would_multiplier +
  actual_size/actual_notional + counterfactual_size/notional + 执行结果.
零交易行为变更; 所有异常只 warn, 不上抛.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
_FORWARD_PATH = _ROOT / "data" / "shadow" / "risk_size_tier_forward.jsonl"
SCHEMA = "risk_size_tier_forward_v1"
RULES = ("atr5", "open15", "ue", "ge2", "any", "product")
STOP_LIKE = {"SL_INITIAL", "OVERNIGHT_GAP_STOP", "SL_FIXED_PCT"}


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _source(decision_id: Any) -> str:
    value = str(decision_id or "")
    if value.startswith("universe_expansion"):
        return "UE"
    if value.startswith("EXT-"):
        return "EXT"
    if value.startswith("DA-"):
        return "DA"
    return "OTHER"


def flags_for(row: Dict[str, Any]) -> Dict[str, bool]:
    atr = row.get("atr_pct")
    return {
        "atr5": atr is not None and atr >= 5.0,
        "open15": 0 <= row.get("session_min", -1) < 15,
        "ue": row.get("source") == "UE",
    }


def multiplier(rule: str, flags: Dict[str, bool]) -> float:
    count = sum(1 for value in flags.values() if value)
    if rule == "atr5":
        return 0.5 if flags["atr5"] else 1.0
    if rule == "open15":
        return 0.5 if flags["open15"] else 1.0
    if rule == "ue":
        return 0.5 if flags["ue"] else 1.0
    if rule == "ge2":
        return 0.5 if count >= 2 else 1.0
    if rule == "any":
        return 0.5 if count >= 1 else 1.0
    if rule == "product":
        return max(0.25, 0.5 ** count)
    return 1.0


def _archetype(inst: Dict[str, Any]) -> str:
    raw = inst.get("raw_instruction_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except Exception:
            raw = {}
    if isinstance(raw, dict):
        arch = raw.get("entry_archetype")
        return str(arch).upper() if arch else ""
    return ""


def risk_shadow_enabled(pb_cfg=None) -> bool:
    try:
        if pb_cfg is None:
            from app.playbook.config import PlaybookConfig
            pb_cfg = PlaybookConfig()
        value = pb_cfg.get("risk_size_shadow_enabled", None)
        return True if value is None else bool(value)
    except Exception as error:
        logger.warning(f"[RiskSizeShadow] 配置读取失败 → 影子关闭: {error}",
                       exc_info=True)
        return False


def build_row(
    *,
    inst: Dict[str, Any],
    direction: Any,
    atr_pct: Optional[float],
    size: Any,
    notional: Any,
    entry_price: Any,
    now_et,
    cfg: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if cfg is not None and not cfg.get("enabled", True):
        return None
    symbol = str(inst.get("symbol") or "")
    if not symbol or "TEST" in symbol.upper():
        return None
    session_min = now_et.hour * 60 + now_et.minute - 570
    source = _source(inst.get("decision_id"))
    flags = flags_for({"atr_pct": atr_pct, "session_min": session_min, "source": source})
    multipliers = {rule: multiplier(rule, flags) for rule in RULES}
    actual_size = _num(size)
    actual_notional = _num(notional)
    return {
        "schema": SCHEMA,
        "ts_et": now_et.isoformat(timespec="seconds"),
        "date": now_et.strftime("%Y-%m-%d"),
        "inst_id": inst.get("id"),
        "decision_id": str(inst.get("decision_id") or ""),
        "symbol": symbol,
        "direction": "SHORT" if str(direction).upper() in ("SHORT", "SELL_SHORT", "SELL") else "LONG",
        "source": source,
        "archetype": _archetype(inst),
        "session_min": session_min,
        "atr_pct": round(float(atr_pct), 6) if atr_pct is not None else None,
        "entry_price": _num(entry_price),
        "flags": flags,
        # 2026-10-22 (注册表 n 轴对齐 RFC-155 §3.2): 判据轴 = 「受影响笔 (≥1 旗标真)」,
        # 顶层布尔供注册表 count_mode=where:affected=True 直读 (嵌套 flags 无法表达)。
        "affected": any(flags.values()),
        "multipliers": multipliers,
        "r1_multiplier": multipliers["atr5"],
        "r4_multiplier": multipliers["ge2"],
        "actual_size": actual_size,
        "actual_notional": actual_notional,
        "cf_sizes": {rule: round(actual_size * mult, 4)
                     for rule, mult in multipliers.items()},
        "cf_notionals": {rule: round(actual_notional * mult, 2)
                         for rule, mult in multipliers.items()},
        "success": None,
        "executed": None,
        "fill_price": None,
        "message": None,
        "skip_reason": inst.get("skip_reason"),
        "prior_skip_reason": None,
    }


def finalize_and_record(
    row: Optional[Dict[str, Any]],
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> bool:
    if not isinstance(row, dict):
        return False
    try:
        if isinstance(result, dict):
            success = bool(result.get("success"))
            row["success"] = success
            row["executed"] = success
            if success:
                if row.get("skip_reason"):
                    row["prior_skip_reason"] = row.get("skip_reason")
                    row["skip_reason"] = None
                row["fill_price"] = _num(result.get("execution_price"))
            else:
                row["message"] = str(result.get("message") or "")[:500]
            if error:
                row["error"] = str(error)[:500]
        else:
            row["success"] = False
            row["executed"] = False
            row["error"] = str(error or "no_result")[:500]
        _append_row(row)
        return True
    except Exception as _error:
        logger.warning(f"[RiskSizeShadow] 收口写盘失败 (不影响交易): {_error}",
                       exc_info=True)
        return False


def _append_row(row: Dict[str, Any]) -> None:
    _FORWARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_FORWARD_PATH, "a", encoding="utf-8") as target:
        target.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
