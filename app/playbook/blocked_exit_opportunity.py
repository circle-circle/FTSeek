"""Record-only shadow: 出场意图未结算期间, 该标的退出规则被整体冻结 (2026-10-17 审计 P0-1).

`_resume_playbook_exits` 把每个未结算 intent 的 `(target_env, symbol)` 放进
`blocked_exits`; `exit_monitor_flow` 命中即 `continue` ⇒ 该标的的 SL/TP/时间/
因子规则**全部不评估**, 直到 intent 结清或人工释放。行为修复 (受控重评/重提交)
按审计建议须先用真实数据定 N/M, 故本模块只记录被冻结期间的评估机会快照:

  - 每个 (intent, symbol) 至多每 60s 一条 (进程内节流, 防 10s 快循环刷爆);
  - 记录当时的近似现价 (批量报价缓存, 零额外 I/O)、entry/stop、intent 年龄/状态;
  - 聚合 (scripts/blocked_exit_opportunity_shadow.py) 回答三个定阈问题:
    冻结时长分布 / 期间最大不利位移 / 若按 SL 兜底本应退出的次数。

零行为变更: 不释放 intent、不改 blocked_exits、不下单、不推送。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger("ftseek.playbook.blocked_exit_opportunity")

_PATH = (
    Path(__file__).resolve().parents[2]
    / "data" / "shadow" / "blocked_exit_opportunity.jsonl"
)
_THROTTLE_S = 60.0
# key = (target_env, symbol, intent_id) -> 上次落盘 epoch; 进程重启重记一条无害
_LAST_EMIT: dict[tuple, float] = {}

_SCHEMA = "blocked_exit_opportunity_v1"


def _direction_sign(direction: Any) -> float:
    return -1.0 if str(direction or "").strip().upper() in (
        "SHORT", "SELL_SHORT", "SELL") else 1.0


def _extract_price(quote: Any) -> float | None:
    if not isinstance(quote, dict):
        return None
    try:
        px = float(quote.get("last") or quote.get("price") or 0.0)
    except (TypeError, ValueError):
        return None
    return px if px > 0 else None


def _extract_stop_loss(metadata_json: Any) -> float | None:
    if not metadata_json:
        return None
    try:
        meta = json.loads(metadata_json) if isinstance(metadata_json, str) else metadata_json
        stop = float((meta or {}).get("stop_loss") or 0.0)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return stop if stop > 0 else None


def build_snapshot(
    row: Mapping[str, Any],
    blocked: Mapping[str, Any],
    quote: Any = None,
    *,
    source: str = "exit_monitor",
    now: float | None = None,
) -> dict:
    now = time.time() if now is None else float(now)
    created_at = blocked.get("created_at")
    try:
        created_at = float(created_at) if created_at is not None else None
    except (TypeError, ValueError):
        created_at = None
    return {
        "schema_version": _SCHEMA,
        "ts": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "date": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d"),
        "source": source,
        "symbol": str(row.get("symbol") or ""),
        "target_env": str(blocked.get("target_env") or ""),
        "direction": str(row.get("direction") or ""),
        "entry_price": float(row.get("entry_price") or 0.0) or None,
        "price": _extract_price(quote),
        "stop_loss": _extract_stop_loss(row.get("metadata_json")),
        "intent_id": str(blocked.get("intent_id") or ""),
        "intent_state": str(blocked.get("state") or ""),
        "intent_error": str(blocked.get("error") or ""),
        "intent_created_at": created_at,
        "intent_age_s": (
            round(max(0.0, now - created_at), 1) if created_at else None
        ),
        "policy": "record-only; exit rules remain blocked (audit P0-1)",
    }


def observe(
    row: Mapping[str, Any],
    blocked: Mapping[str, Any],
    quote: Any = None,
    *,
    source: str = "exit_monitor",
    now: float | None = None,
    path: Path | str | None = None,
    throttle_s: float = _THROTTLE_S,
) -> bool:
    """Append one throttled snapshot. Returns True when a line was written.

    Never raises: shadow failures must not disturb the exit monitor.
    """
    try:
        now = time.time() if now is None else float(now)
        key = (
            str(blocked.get("target_env") or ""),
            str(row.get("symbol") or ""),
            str(blocked.get("intent_id") or ""),
        )
        last = _LAST_EMIT.get(key)
        if last is not None and (now - last) < throttle_s:
            return False
        out = Path(path) if path is not None else _PATH
        event = build_snapshot(row, blocked, quote, source=source, now=now)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        _LAST_EMIT[key] = now
        if len(_LAST_EMIT) > 1024:
            # 防长跑无界增长: 只保留最近一小时仍活跃的键
            cutoff = now - 3600.0
            for k in [k for k, ts in _LAST_EMIT.items() if ts < cutoff]:
                _LAST_EMIT.pop(k, None)
        return True
    except Exception as error:
        logger.warning(
            "[blocked-exit-shadow] 快照写入失败 (不影响退出监控): %s",
            error, exc_info=True,
        )
        return False
