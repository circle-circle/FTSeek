"""[DEB-20261117 P1] 迟成交证据回灌 (exec_gate_audit + execution_tca 双账本)。

病灶 (2026-11-17 实盘实证, 当日 6 笔成交里 4 笔中招): `auto_execute_flow` 只在**本轮
下单调用返回的那一刻**收口两份影子账本, 而 SUBMITTED→confirmed 的成交证据是稍后由
`PositionSync._reconcile_playbook_execution_states` 从券商订单回执补录的 ⇒

  · `exec_gate_audit` 该指令终局停在 `action='submitted_unconfirmed'`
    (11-17: ELV 5647 / CAT 5650 / UBER 5652 / BWXT 5654, 四笔事后全部 BOOKED 成交),
    而 `scripts/gate_ledger.py:106` 按 `if "executed" in actions` 判每指令终局
    ⇒ 执行率被系统性低估 (当日 2/6 而非 6/6)。
  · `execution_tca` 对应行 `fill_price=null` / `planned_to_fill_bps=null`,
    而 `scripts/execution_tca_report.py:211` 只取 `success is True and fill_price>0`
    ⇒ "计划→成交"滑点样本 6 笔只剩 2 笔 (BA/COIN), 缺口 67%。

设计约束:
  · **只追加, 不改写**: 两份账本都是 append-only JSONL, 历史行一个字节都不动;
    回灌行自带来源标记 (`evidence_source` / `late_fill`), 报表可分离两种收口路径。
  · **entry/size/notional 沿用指令计划值**, 与该指令那行 `submitted_unconfirmed`
    同口径, 真实成交证据只进 extra (`execution_price` / `dealt_qty` / `execution_time`)
    ⇒ 不污染以 entry/size/notional 为分母的既有统计。
  · **单位契约 (两份账本不同, 勿混)**: `exec_gate_audit.atr_pct` 是**分数**口径
    (0.0558 = 5.58%, 沿用 `auto_execute_flow:2182` 直传 DB 原值的惯例);
    `execution_tca.atr_pct` 是**百分数**口径 (5.58), 故本模块对 TCA 侧 ×100
    (同 `auto_execute_flow:1837`)。价格一律美元绝对价, bps 一律 1bp=0.0001。
  · **去重**: 同一 inst_id 已有 `executed` / `success=true` 行则不再补, 防重启竞态双计。

零交易行为变更: 不碰 playbook_instructions / trade_records / open_positions, 不下单,
不改任何闸门判定; 全程 try/except, 度量故障绝不变成交易故障。
回滚: 删掉 position_sync 里的调用点即可, 已写数据保留。
"""
from __future__ import annotations

import json
import logging
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
_AUDIT_PATH = _ROOT / "data" / "shadow" / "exec_gate_audit.jsonl"
_TCA_PATH = _ROOT / "data" / "shadow" / "execution_tca.jsonl"

# 当日审计行历史峰值 ~450 (2026-08-18), 4000 行尾巴足以覆盖单日且不受文件增长影响。
_TAIL_LINES = 4000


def _num(value: Any) -> Optional[float]:
    try:
        if value in (None, "", "null"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _tail_rows(path: Path, limit: int = _TAIL_LINES) -> List[Dict[str, Any]]:
    """读账本尾部若干行 (append-only ⇒ 当日行必在尾部), 坏行静默跳过。"""
    try:
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as handle:
            tail = deque(handle, maxlen=limit)
        rows = []
        for line in tail:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except Exception:
                continue
            if isinstance(parsed, dict):
                rows.append(parsed)
        return rows
    except Exception as error:
        logger.warning("[LateFill] 账本尾部读取失败 %s: %s", path.name, error)
        return []


def _as_dict(instruction: Any) -> Dict[str, Any]:
    """sqlite3.Row / Mapping / dict 统一成 dict (Row 不是 Mapping 实例)。"""
    if isinstance(instruction, dict):
        return instruction
    try:
        return {key: instruction[key] for key in instruction.keys()}
    except Exception:
        return {}


def _fill_instant(execution_ts: Any):
    """成交瞬间的 aware datetime; 不自描述时区的一律回退 now_et()。

    账本 `ts`/`date` 必须是**成交时刻**而非 reconcile 时刻 —— 跨时段/跨日补录会把
    样本记错交易日 (与 `playbook_instructions.entry_time` 取券商订单时间同一纪律,
    见 position_sync RFC-169 复查注释)。

    ⚠️ 刻意**不**用 `parse_iso_safe` / `parse_ledger_ts` 兜底 naive 串: 前者把 naive 当
    UTC, 后者当 CST, 而券商订单时间是 ET —— 两个都会算错 4~12 小时 (DEB-20260905 §十三
    的尺度混装病灶)。宁可退回 reconcile 时刻 (同日、tz 正确), 也不写一个错尺度的瞬间。
    """
    text = str(execution_ts or "").strip()
    # 自描述时区 = 日期段之后出现 '+' / '-' 偏移或 'Z' 结尾; naive 串 (含券商那种
    # 'YYYY-MM-DD HH:MM:SS') 日期段之后只有 ':' 和空格, 不会被误判成 aware。
    _tail = text[10:] if len(text) > 10 else ""
    if text and (text.endswith("Z") or "+" in _tail or "-" in _tail):
        try:
            from app.utils.timezone import parse_iso_safe
            parsed = parse_iso_safe(text)
            if parsed is not None and parsed.tzinfo is not None:
                return parsed
        except Exception:
            pass
    try:
        from app.utils.timezone import now_et
        return now_et()
    except Exception:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc)


def record_late_fill(
    instruction: Any,
    *,
    fill_price: Any,
    dealt_qty: Any,
    execution_ts: Any = None,
    evidence_source: str = "sync_reconcile",
    audit_path: Optional[Path] = None,
    tca_path: Optional[Path] = None,
) -> Dict[str, bool]:
    """把一笔"下单轮未确认、事后由 sync 补录成交"的证据补进两份影子账本。

    Returns: {"audit": 是否补了行, "tca": 是否补了行} — 仅供调用方打日志/测试断言,
    任何异常都吞掉并返回 False, 绝不上抛。
    """
    outcome = {"audit": False, "tca": False}
    try:
        inst = _as_dict(instruction)
        symbol = str(inst.get("symbol") or "")
        if not symbol or "TEST" in symbol.upper():
            return outcome
        fill = _num(fill_price)
        qty = _num(dealt_qty)
        if fill is None or fill <= 0 or qty is None or qty <= 0:
            return outcome
        inst_id = inst.get("id")
        entry = _num(inst.get("entry_price"))
        size = _num(inst.get("position_size")) or qty
        audit_target = Path(audit_path) if audit_path else _AUDIT_PATH
        tca_target = Path(tca_path) if tca_path else _TCA_PATH

        outcome["audit"] = _backfill_audit(
            inst, audit_target, entry=entry if entry is not None else fill,
            size=size, evidence_source=evidence_source, fill=fill, qty=qty,
            execution_ts=execution_ts, inst_id=inst_id)
        outcome["tca"] = _backfill_tca(
            inst, tca_target, fill=fill, qty=qty, execution_ts=execution_ts,
            evidence_source=evidence_source, inst_id=inst_id)
        if any(outcome.values()):
            logger.info(
                "[LateFill] %s inst=%s 迟成交证据回灌 audit=%s tca=%s fill=%.4f qty=%d (%s)",
                symbol, inst_id, outcome["audit"], outcome["tca"], fill, int(qty),
                evidence_source)
    except Exception as error:
        logger.warning("[LateFill] 证据回灌失败 (不影响交易): %s", error, exc_info=True)
    return outcome


def _backfill_audit(inst: Dict[str, Any], path: Path, *, entry: float, size: float,
                    evidence_source: str, fill: float, qty: float,
                    execution_ts: Any, inst_id: Any) -> bool:
    try:
        # [2026-11-18 事故修正] fail-close: 下游 `_emit_exec_gate_audit` (risk_gates.py:433)
        # **无 path 参数** (output_path 由 module_file 推导) ⇒ 写恒落生产账本。本函数的 path
        # 只作用于去重读, 故"path 指向别处"≠"写到别处", 放行会变成「去重读空文件(永不去重)
        # + 写生产」的静默陷阱 —— 已实证误写 4 行, 截除留痕见
        # data/audits/exec_gate_audit_excision_20261118T014102Z.jsonl。
        # 离线要跑通写入面必须 monkeypatch 模块常量 _AUDIT_PATH, 使去重读与写入面同源。
        if Path(path).resolve() != Path(_AUDIT_PATH).resolve():
            logger.warning(
                "[LateFill] audit_path=%s 与本模块生产路径 _AUDIT_PATH=%s 不一致 ⇒ **拒绝**回灌审计侧 "
                "(下游 emitter 无 path 参数, 继续会写进生产账本)。离线验证请 monkeypatch "
                "late_fill_evidence._AUDIT_PATH。inst=%s", path, _AUDIT_PATH, inst_id)
            return False
        already = any(row.get("action") == "executed" and row.get("inst_id") == inst_id
                      for row in _tail_rows(path))
        if already:
            return False
        from app.playbook.pipeline import _emit_exec_gate_audit
        _emit_exec_gate_audit(
            inst,
            action="executed",
            entry=entry,
            size=size,
            notional=round(entry * size, 2),
            extra={
                "vol_bucket": inst.get("vol_bucket"),
                # 分数口径 (与 auto_execute_flow:2182 一致), 勿 ×100
                "atr_pct": inst.get("atr_pct"),
                "late_fill": True,
                "evidence_source": evidence_source,
                "execution_price": round(fill, 4),
                "dealt_qty": int(qty),
                "execution_time": str(execution_ts or "") or None,
            },
        )
        return True
    except Exception as error:
        logger.warning("[LateFill] exec_gate_audit 回灌失败 %s: %s",
                         inst.get("symbol"), error, exc_info=True)
        return False


def _backfill_tca(inst: Dict[str, Any], path: Path, *, fill: float, qty: float,
                  execution_ts: Any, evidence_source: str, inst_id: Any) -> bool:
    try:
        already = any(row.get("inst_id") == inst_id and row.get("success") is True
                      for row in _tail_rows(path))
        if already:
            return False
        from app.playbook.execution_tca_shadow import (
            build_attempt_row, finalize_and_record, resolve_planned_entry, tca_config,
        )
        cfg = tca_config()
        if not cfg.get("enabled"):
            return False
        planned, planned_source = resolve_planned_entry(inst)
        atr_fraction = _num(inst.get("atr_pct"))
        row = build_attempt_row(
            inst=inst,
            direction=inst.get("direction", ""),
            planned_entry=planned,
            planned_entry_source=planned_source,
            execution_entry=inst.get("entry_price"),
            # 成交瞬间已过去, 到达报价无从追溯 ⇒ ctx=None, quote_source 如实标注,
            # bid/ask/spread/quote_age 一律 None (禁止拿事后报价伪造到达报价)。
            ctx=None,
            quote_source="late_fill_no_arrival_quote",
            now_et=_fill_instant(execution_ts),
            size=qty,
            notional=round(fill * qty, 2),
            # DB atr_pct 是分数口径; TCA 契约是百分数 ⇒ ×100 (同 auto_execute_flow:1837)
            atr_pct=(None if atr_fraction is None else atr_fraction * 100.0),
            cfg=cfg,
        )
        if not isinstance(row, dict):
            return False
        row["late_fill"] = True
        row["fill_evidence_source"] = evidence_source
        return bool(finalize_and_record(
            row, result={"success": True, "execution_price": fill}, path=path))
    except Exception as error:
        logger.warning("[LateFill] execution_tca 回灌失败 %s: %s",
                         inst.get("symbol"), error, exc_info=True)
        return False
