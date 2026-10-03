"""Record-only shadow for ETH-held exits that remain blocked after RTH opens.

Ground truth (2026-10-01): US.ACN SHORT 23 shares @212.529 (sim). The 09:25 ET
pre-close flatten LIMIT 23@215.62 (order 8219159) was cancelled after 60s with
zero fills; the durable intent 7b149410... entered ``exit_eth_partial_fill_held``
(ETH deliberately does not market-chase). Every later exit path stayed blocked:
``request`` rejects a new RTH intent for the same close_key (session conflict),
the symbol-busy guard rejects other close_keys, and ``_resume_playbook_exits``
adds the symbol to ``blocked_exits`` so the regular-hours SL evaluation is
skipped entirely. DEB-20260910 §356 predicted this class ("long-lived HELD
needs a manual reconciliation process"); today was its first live occurrence.

RFC-127 stage 0 (this module) only *observes*: when a HELD ETH exit would be
eligible for session escalation (regular market open + remaining quantity),
append one deterministic event. Exit behavior, alerts and ledger arbitration
are unchanged.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger("ftseek.playbook.eth_held_exit_shadow")

_ETH_HELD_EXIT_SHADOW_PATH = (
    Path(__file__).resolve().parents[2]
    / "data" / "shadow" / "eth_held_exit_escalation.jsonl"
)
_FTSEEK_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "ftseek_us.db"
_ETH_HELD_ERROR = "exit_eth_partial_fill_held"

# 方向归一: intent 表与 open_positions 表历史上出现过 SHORT/SELL_SHORT 两套写法
# (见 ftseek-extended-hours-trading 枚举错配坑) ⇒ 比对前一律归一, 防值域漂移
# 把"有持仓"误判成"无持仓"而静默丢真样本 (丢样本是本族最贵的失败)。
_DIRECTION_ALIASES = {
    "SHORT": "SHORT", "SELL_SHORT": "SHORT", "SELL": "SHORT",
    "LONG": "LONG", "BUY": "LONG",
}


def _canon_direction(value: Any) -> str:
    return _DIRECTION_ALIASES.get(str(value or "").strip().upper(), "")


def _position_still_open(symbol: str, direction: str) -> bool | None:
    """本地 open_positions 是否仍有该 symbol+方向的持仓 (只读, 零券商调用)。

    返回 None = 查询失败 ⇒ 守卫 fail-open (照常落样本 + WARNING), 绝不因守卫
    自身故障静默丢真样本。
    """
    canon = _canon_direction(direction)
    if not symbol or not canon:
        return None
    try:
        import sqlite3

        conn = sqlite3.connect(f"file:{_FTSEEK_DB_PATH}?mode=ro", uri=True,
                               timeout=5.0)
        try:
            rows = conn.execute(
                "SELECT direction, COALESCE(size, 0) FROM open_positions "
                "WHERE symbol = ?", (symbol,)).fetchall()
        finally:
            conn.close()
    except Exception as error:  # 守卫故障不得吞样本
        logger.warning(
            "[eth-held-exit-shadow] 持仓守卫查询失败 (fail-open 照常落样本): %s",
            error, exc_info=True)
        return None
    return any(_canon_direction(row[0]) == canon and float(row[1] or 0.0) > 0
               for row in rows)


def _regular_market_open() -> bool:
    """RTH check reuses the production market-hours SSOT (holiday/half-day aware)."""
    try:
        from app.utils.timezone import is_market_open
        return bool(is_market_open())
    except Exception:
        return False


def _et_today() -> str:
    try:
        from app.utils.timezone import now_et
        return now_et().strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def build_eth_held_rth_event(
    intent: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    rth_open: bool | None = None,
    position_open: bool | None = None,
) -> dict[str, Any] | None:
    """Return one escalation-eligibility event, or None when not eligible.

    Eligibility (stage 1 candidate, pre-registered in RFC-127):
      - the intent is an ETH exit stuck in ``exit_eth_partial_fill_held``;
      - the regular session is open (so a MARKET completion leg is legal);
      - remaining quantity > 0;
      - **the position actually still exists** (``position_open`` guard).

    幽灵样本守卫 (2026-10-02 落地, 由 10/1 US.ACN 真实事件驱动):
      ``remaining_qty`` 来自 ``result.dealt_qty`` = **系统自己那条已撤腿的成交量**,
      不是券商真实持仓。当仓位被外部渠道平掉 (App 手动单 / 券商侧成交) 时,
      `position_sync` 的 `external_close` 对账只写 `trade_records` + DELETE
      `open_positions`, **完全不碰 `exit_intents`** ⇒ 意图会永久卡在 PENDING/HELD,
      于是本影子每轮都会落一条"剩余 N 股可升级"的**幽灵样本** (实际已无仓可平)。
      RFC-127 阶段1 判据2 要求"命中意图 100% 满足剩余>0", 幽灵样本会把它顶成假 PASS
      ⇒ 判据可信度直接受损。守卫取本地 `open_positions` (零券商调用, 不碰钱链):
      无持仓 ⇒ 不落样本。查询失败一律 **fail-open** (照常落 + WARNING),
      绝不因守卫自身故障静默丢真样本。
    """
    if str(result.get("error") or "") != _ETH_HELD_ERROR:
        return None
    if str(intent.get("session") or "") != "ETH":
        return None
    if rth_open is None:
        rth_open = _regular_market_open()
    if not rth_open:
        return None
    quantity = float(intent.get("quantity") or 0.0)
    dealt_qty = float(result.get("dealt_qty") or 0.0)
    remaining_qty = max(0.0, quantity - dealt_qty)
    if remaining_qty <= 0:
        return None
    symbol = str(intent.get("symbol") or "")
    direction = str(intent.get("direction") or "")
    if position_open is None:
        position_open = _position_still_open(symbol, direction)
    if position_open is False:  # None = 查询失败 ⇒ fail-open, 不丢样本
        logger.warning(
            "[eth-held-exit-shadow] 幽灵样本守卫: %s %s 意图仍在 HELD 但本地已无持仓 "
            "(external_close 不释放 intent) ⇒ 跳过记录, 不计入 RFC-127 判据",
            symbol, direction,
        )
        return None

    now_ts = datetime.now(timezone.utc).timestamp()
    created_at = float(intent.get("created_at") or 0.0)
    event = {
        "schema_version": "eth_held_exit_escalation_v1",
        "ts": datetime.now(timezone.utc).isoformat(),
        "date": _et_today(),
        "intent_id": str(intent.get("intent_id") or ""),
        "symbol": str(intent.get("symbol") or ""),
        "direction": str(intent.get("direction") or ""),
        "target_env": str(intent.get("target_env") or ""),
        "session": "ETH",
        "exit_state": str(result.get("exit_state") or "HELD"),
        "error": _ETH_HELD_ERROR,
        "quantity": quantity,
        "dealt_qty": dealt_qty,
        "remaining_qty": remaining_qty,
        "futu_order_id": str(result.get("futu_order_id") or ""),
        "intent_age_seconds": (
            round(max(0.0, now_ts - created_at), 1) if created_at > 0 else None
        ),
        "rth_open": True,
        "rth_source": "app.utils.timezone.is_market_open",
        "position_open_guard": "injected"
        if position_open is not None else "open_positions(ro)",
        "proposed_resolution": "escalate_to_rth_market_leg_for_remaining_qty",
        "policy": "record-only; exit behavior unchanged (RFC-127 stage 0)",
    }
    signature = json.dumps(
        {"intent_id": event["intent_id"], "schema_version": event["schema_version"]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    event["event_id"] = (
        "ETH-HELD-RTH:"
        + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]
    )
    return event


def append_event(event: Mapping[str, Any], output_path: Path | str) -> bool:
    """Append one event once. Returns False when the event_id already exists."""
    path = Path(output_path)
    event_id = str(event.get("event_id") or "")
    if event_id:
        if path.exists():
            bad_lines = 0
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    if str(json.loads(line).get("event_id")) == event_id:
                        return False
                except json.JSONDecodeError:
                    bad_lines += 1
            if bad_lines:
                logger.warning(
                    "[eth-held-exit-shadow] 台账 %s 有 %d 行坏 JSON, 去重扫描已跳过这些行",
                    path, bad_lines,
                )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output_file:
        output_file.write(
            json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
        )
    return True


def emit_eth_held_exit_shadow(
    intent: Mapping[str, Any],
    result: Mapping[str, Any],
) -> None:
    """Best-effort record-only telemetry; never changes exit recovery behavior."""
    try:
        event = build_eth_held_rth_event(intent, result)
        if event is None:
            return
        appended = append_event(event, _ETH_HELD_EXIT_SHADOW_PATH)
        if appended:
            logger.warning(
                "[eth-held-exit-shadow] %s %s ETH出场HELD且RTH已开盘 "
                "(record-only, RFC-127阶段0): 剩余%.0f股 intent_age=%.0fs "
                "order=%s — 现行行为仍为继续HELD",
                event["symbol"], event["direction"], event["remaining_qty"],
                float(event["intent_age_seconds"] or 0.0), event["futu_order_id"],
            )
    except Exception as error:
        logger.warning(
            "[eth-held-exit-shadow] 影子写入失败 (不影响退出恢复): %s",
            error, exc_info=True,
        )
