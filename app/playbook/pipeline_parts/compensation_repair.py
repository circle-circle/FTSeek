"""入场簿记失败后的补偿退出重建/收口辅助（仅账本，无券商调用）。"""
from __future__ import annotations

import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def _table_columns(conn, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def resolve_compensation_trade_id(dual, conn, owner: dict, entry_id: str,
                                  intent_id: str, receipt: dict, now_et):
    """返回可统一平仓的 trade_id；旧/最小 schema 保留独立回执兼容路径。"""
    entry_id = owner.get("entry_order_id") or ""
    candidates = conn.execute(
        "SELECT * FROM trade_records WHERE (?<>'' AND futu_order_id=?) "
        "OR (symbol=? AND entry_time=?)",
        (entry_id, entry_id, owner["symbol"], owner["entry_time"]),
    ).fetchall()
    matching = []
    for candidate in candidates:
        candidate = dict(candidate)
        candidate_meta = json.loads(candidate.get("metadata_json") or "{}")
        identity_fields = ("symbol", "direction", "source_type", "entry_time", "entry_price", "quantity")
        if (not isinstance(candidate_meta, dict)
                or any(candidate[key] != owner[key] for key in identity_fields)
                or candidate_meta.get("playbook_instruction_id") != owner["instruction_id"]
                or str(candidate.get("futu_order_id") or "") != entry_id):
            raise ValueError("compensation entry identity conflict")
        matching.append(candidate)
    if len(matching) > 1:
        raise ValueError("compensation entry identity ambiguous")
    if matching:
        return int(matching[0]["id"])

    instruction_columns = _table_columns(conn, "playbook_instructions")
    trade_columns = _table_columns(conn, "trade_records")
    required_instruction = {
        "decision_id", "confidence", "market_regime", "vix_at_generation",
        "stop_loss", "take_profit",
    }
    required_trade = {
        "trade_date", "symbol", "direction", "entry_time", "exit_time", "entry_price",
        "exit_price", "quantity", "realized_pnl", "pnl_pct", "exit_reason",
        "holding_hours", "signal_strength", "signal_confidence", "futu_order_id",
        "strategy_id", "trace_id", "created_at", "source_type", "entry_reason",
        "metadata_json", "owner_strategy", "mode", "market_regime", "trade_mode",
        "signal_id", "decision_id", "config_version", "entry_vix", "entry_session",
    }
    if not required_instruction.issubset(instruction_columns) \
            or not required_trade.issubset(trade_columns):
        # 旧版/最小测试库不具备完整重建字段时，维持原独立回执，不虚构交易行。
        receipt.update(instruction_id=owner["instruction_id"],
                       execution_attempt=owner["execution_attempt"],
                       entry_order_id=entry_id)
        if not dual.acknowledge_close(intent_id, receipt):
            logger.error("[playbook] compensation receipt acknowledgement pending: %s", intent_id)
        return None
    return reconstruct_compensation_entry(conn, owner, entry_id, now_et)


def _effective_reprice_levels(instruction_metadata: str | dict | None,
                              fallback_sl, fallback_tp) -> tuple[float, float]:
    """执行审计优先：reprice SL/TP 缺失时才回退指令原始值。"""
    metadata = instruction_metadata
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata or "{}")
        except (TypeError, ValueError):
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    audit = metadata.get("reprice_audit") or {}
    if not isinstance(audit, dict):
        audit = {}
    stop_loss = audit.get("reprice_stop_loss")
    take_profit = audit.get("reprice_take_profit")
    return (
        float(stop_loss if stop_loss not in (None, "") else (fallback_sl or 0)),
        float(take_profit if take_profit not in (None, "") else (fallback_tp or 0)),
    )


def align_existing_reprice_metadata(conn, owner: dict, trade_id: int) -> bool:
    """已闭环补偿单的 SL/TP 展示对齐执行审计，带身份 CAS，不触碰盈亏。"""
    inst = conn.execute(
        "SELECT stop_loss,take_profit,metadata_json FROM playbook_instructions WHERE id=?",
        (owner["instruction_id"],),
    ).fetchone()
    if inst is None:
        raise ValueError("compensation source instruction missing")
    effective_sl, effective_tp = _effective_reprice_levels(inst[2], inst[0], inst[1])
    row = conn.execute("SELECT metadata_json FROM trade_records WHERE id=?", (trade_id,)).fetchone()
    if row is None:
        raise ValueError("compensation trade missing")
    metadata = json.loads(row[0] or "{}")
    if not isinstance(metadata, dict) or metadata.get("playbook_instruction_id") != owner["instruction_id"]:
        raise ValueError("compensation trade identity conflict")
    if (float(metadata.get("stop_loss") or 0) == effective_sl
            and float(metadata.get("take_profit") or 0) == effective_tp):
        return False
    metadata["stop_loss"] = effective_sl
    metadata["take_profit"] = effective_tp
    metadata["reprice_metadata_aligned"] = True
    cursor = conn.execute(
        "UPDATE trade_records SET metadata_json=? WHERE id=? "
        "AND json_extract(metadata_json,'$.playbook_instruction_id')=?",
        (json.dumps(metadata, ensure_ascii=False), trade_id, owner["instruction_id"]),
    )
    conn.commit()
    if cursor.rowcount != 1:
        raise ValueError("compensation reprice metadata CAS failed")
    return True


def reconstruct_compensation_entry(conn, owner: dict, entry_id: str, now_et) -> int:
    """用已验证的 compensation owner + source instruction 重建未平入场行。

    调用方负责后续统一平仓；失败时须用 discard_unclosed_reconstruction 清理。
    这里不读取账户聚合仓位、不推算 PnL，身份冲突由调用方先行拒绝。
    """
    inst = conn.execute(
        "SELECT decision_id, confidence, market_regime, vix_at_generation, "
        "stop_loss, take_profit, metadata_json FROM playbook_instructions WHERE id=?",
        (owner["instruction_id"],),
    ).fetchone()
    if inst is None:
        raise ValueError("compensation source instruction missing")
    confidence = float(inst[1] or 0)
    trace_id = f"playbook-compensation-{owner['instruction_id']}-{owner['execution_attempt']}"
    source_type = owner["source_type"]
    mode_label = "live" if source_type == "PLAYBOOK_LIVE" else "futu_simulate"
    effective_sl, effective_tp = _effective_reprice_levels(inst[6], inst[4], inst[5])
    metadata = {
        "playbook_instruction_id": owner["instruction_id"],
        "stop_loss": effective_sl,
        "take_profit": effective_tp,
        "peak_price": float(owner["entry_price"]),
        "trough_price": float(owner["entry_price"]),
        "entry_price_source": "broker_deal_confirm",
        "compensation_repair": True,
        "repair_schema": "entry_bookkeeping_compensation_v1",
    }
    created_at = datetime.now(now_et()).isoformat()
    cursor = conn.execute(
        "INSERT INTO trade_records ("
        "trade_date, symbol, direction, entry_time, exit_time, entry_price, "
        "exit_price, quantity, realized_pnl, pnl_pct, exit_reason, "
        "holding_hours, signal_strength, signal_confidence, futu_order_id, "
        "strategy_id, trace_id, created_at, source_type, entry_reason, "
        "metadata_json, owner_strategy, mode, market_regime, trade_mode, "
        "signal_id, decision_id, config_version, entry_vix, entry_session) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'v2',?,?)",
        (
            str(owner["entry_time"])[:10], owner["symbol"], owner["direction"],
            owner["entry_time"], None, float(owner["entry_price"]), None,
            int(owner["quantity"]), None, 0.0, "", 0.0,
            int(confidence), confidence / 100.0, entry_id,
            "playbook_live" if source_type == "PLAYBOOK_LIVE" else "playbook_sim",
            trace_id, created_at, source_type,
            "playbook_entry_bookkeeping_compensation",
            json.dumps(metadata, ensure_ascii=False),
            "playbook_live" if source_type == "PLAYBOOK_LIVE" else "playbook_sim",
            mode_label, inst[2] or "UNKNOWN",
            "live" if source_type == "PLAYBOOK_LIVE" else "simulate",
            trace_id, inst[0] or "", float(inst[3] or 0), "",
        ),
    )
    conn.commit()  # 统一平仓函数要求 clean connection；后续失败由调用方删除未平行。
    trade_id = int(cursor.lastrowid)
    logger.warning(
        "[playbook] compensation entry reconstructed from broker evidence: %s "
        "trade_id=%s instruction=%s order=%s",
        owner["symbol"], trade_id, owner["instruction_id"], entry_id)
    owner["_compensation_reconstructed"] = True
    return trade_id


def mark_compensation_instruction_booked(conn, owner: dict) -> None:
    """将补偿入场指令按既有 CAS 状态机收口，拒绝跳过未确认成交证据。"""
    required = {"execution_state", "execution_attempt", "metadata_json",
                "broker_order_id", "executed", "execution_price", "execution_time",
                "skip_reason", "updated_at"}
    if not required.issubset(_table_columns(conn, "playbook_instructions")):
        return  # 旧/最小 schema 没有执行 CAS 字段，保留既有独立回执语义。
    state = conn.execute(
        "SELECT execution_state FROM playbook_instructions WHERE id=?",
        (owner["instruction_id"],),
    ).fetchone()
    if state is None:
        raise ValueError("compensation instruction state missing")
    if state[0] == "BOOKED":
        return
    from app.utils.timezone import now_utc
    marked = conn.execute(
        "UPDATE playbook_instructions SET execution_state='BOOKED', executed=1, "
        "execution_price=COALESCE(?, execution_price), "
        "execution_time=COALESCE(?, execution_time), skip_reason='', updated_at=? "
        "WHERE id=? AND execution_attempt=? AND execution_state='FILLED_UNBOOKED' "
        "AND json_extract(metadata_json, '$.entry_fill.confirmed')=1 "
        "AND json_extract(metadata_json, '$.entry_fill.attempt')=execution_attempt "
        "AND json_extract(metadata_json, '$.entry_fill.order_id')=broker_order_id",
        (float(owner["entry_price"]), owner["entry_time"], now_utc().isoformat(),
         owner["instruction_id"], owner["execution_attempt"]),
    )
    conn.commit()
    if marked.rowcount != 1:
        raise ValueError("compensation instruction booking CAS failed")


def discard_unclosed_reconstruction(conn, trade_id: int | None) -> None:
    """后续平仓失败时，仅删除仍未平的补建行；已闭环记录保留供回执重放。"""
    if trade_id is None:
        return
    conn.rollback()
    conn.execute(
        "DELETE FROM trade_records WHERE id=? "
        "AND (exit_time IS NULL OR exit_time='') "
        "AND (exit_price IS NULL OR exit_price=0)",
        (trade_id,),
    )
    conn.commit()
