#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""meta_audit loop v2 — findings adjudication and convergence loop.

v1 proved that detector output can be persisted. v2 closes the missing half:
every finding must either disappear from a healthy source, or receive an
explicit human/maintenance decision. ``LOOP_DONE`` therefore means "no
unresolved finding", not "the script ran" and not "somebody has a deadline".

Physical boundary: this loop remains L1 read-only. It reads source artifacts,
writes only ``data/loops/meta_audit/``, and never changes trading parameters,
restarts services, or places orders.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(os.environ.get("FTSEEK_REPO", Path(__file__).resolve().parent.parent))
LOOP_DIR = BASE / "data" / "loops" / "meta_audit"
STATE_MD = LOOP_DIR / "STATE.md"
FINDINGS = LOOP_DIR / "findings.json"
HISTORY = LOOP_DIR / "findings_history.jsonl"
LAST_DIGEST = LOOP_DIR / "last_digest.json"
DECISIONS = LOOP_DIR / "decisions.jsonl"
LOCK_FILE = LOOP_DIR / "run.lock"

MAX_WIP = 3
MAX_DIGEST_ITEMS = 8

# ── 盘中健康源的根因归并 (2026-09-22, RFC-091 v2.1) ──────────────────────────
# 病灶 (实测 2026-09-22): 签名键 = 日志文本哈希 ⇒ 违反计数/耗时每天微变 = 每天"新签名"
#   ⇒ 每天新生一条 P0 且当日到期; 同时同一根因在结构化源里已有一条 (P0+P1 双记)。
# 修法 (做减法, 不新增门): ① 数字归一化得稳定"族键" ⇒ 复现落在同一条 finding;
#                          ② evidence 点名了已在册结构化根因 (W7_/W6_/W1_/I4_ ...) ⇒ 不新建,
#                             追加到该条 evidence (一因一条)。
INTRA_FAMILY_RE = re.compile(r"intraday_health:(?:ERROR:)?[0-9a-f]{8,}")
STRUCTURED_RC_RE = re.compile(r"\b(?:I|W|D)\d{1,2}_[A-Za-z0-9_]+(?::[A-Za-z0-9_]+)*")
STRUCTURED_SOURCES = ("invariant", "watchdog", "gate_liveness")
APPEND_MARK = "@append@"


def _intraday_family(evidence: str) -> str:
    """同根因家族键: 数字归一化 (计数值/耗时/序号每天都变), 只认文本骨架。"""
    norm = re.sub(r"\d+", "#", evidence or "")
    return hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()[:10]


def _structured_roots(evidence: str) -> list:
    """evidence 里点名的结构化根因名 (按首次出现顺序去重)。"""
    seen, out = set(), []
    for m in STRUCTURED_RC_RE.finditer(evidence or ""):
        name = m.group(0)
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _record_merged(host: dict, note: str):
    """把归并来的签名挂到宿主条上; 同一签名 (前 120 字) 只记一次。"""
    rec = host.setdefault("merged_signatures", [])
    head = note[:120]
    if any(head in old for old in rec):
        return
    host["merged_signatures"] = rec[-4:] + [note]


def _store_structured_keys() -> list:
    """已在册的结构化源键 (即使本轮产物没报它, 也认它是"已在册")。"""
    try:
        store = _load().get("findings") or {}
    except Exception:
        return []
    return [k for k in store if k.split(":", 1)[0] in STRUCTURED_SOURCES]

ACTIVE_STATUSES = {
    "OPEN", "UNVERIFIABLE", "DECIDED", "ACTION_PENDING",
    "VERIFY_PENDING", "REVIEW_DUE", "BACKLOG",
}
WIP_STATUSES = {"DECIDED", "ACTION_PENDING", "VERIFY_PENDING"}
CLOSE_DECISIONS = {"RETIRE", "OUT_OF_SCOPE", "INVALID_SOURCE"}

SEVERITY_SLA = {
    "P0": {"decide_days": 0, "action_days": 0, "review_days": 1},
    "P1": {"decide_days": 1, "action_days": 3, "review_days": 7},
    "P2": {"decide_days": 7, "action_days": 14, "review_days": 30},
    "P3": {"decide_days": 30, "action_days": 90, "review_days": 90},
}
SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

DEFAULT_OWNER = {
    "gate_liveness": "用户(裁决)",
    "watchdog": "维护批",
    "invariant": "维护批",
    "config_census": "维护批",
    "intraday_health": "用户(裁决)",
    "detector": "维护批",
}
KEY_OWNER = {
    "invariant:W7_chg_denominator_divergence": "用户(裁决)",
}


def _now() -> datetime:
    return datetime.now()


def _today() -> str:
    return _now().strftime("%Y-%m-%d")


def _date_plus(days: int) -> str:
    return (_now() + timedelta(days=days)).strftime("%Y-%m-%d")


def _age_h(ts: float) -> float:
    return (datetime.now().timestamp() - ts) / 3600.0


def _parse_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp is empty")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _source_timestamp(name: str, doc: dict):
    field = {
        "gate_liveness": "generated_at_et",
        "watchdog": "at",
        "invariant": "checked_at",
        "config_census": "generated_at",
        "intraday_health": "generated_at",
    }[name]
    return _parse_timestamp(doc.get(field))


def _validate_source(name: str, doc) -> list[str]:
    if not isinstance(doc, dict):
        return ["source must be a JSON object"]
    errors = []
    try:
        timestamp = _source_timestamp(name, doc)
        if timestamp > _now() + timedelta(minutes=5):
            errors.append("source timestamp is in the future")
    except Exception as exc:
        errors.append(f"invalid source timestamp: {type(exc).__name__}: {exc}")

    if name == "gate_liveness":
        if not isinstance(doc.get("gates"), dict):
            errors.append("gates must be an object")
    elif name == "watchdog":
        if not isinstance(doc.get("alarms"), list):
            errors.append("alarms must be a list")
    elif name == "invariant":
        if not isinstance(doc.get("violations"), dict):
            errors.append("violations must be an object")
        if not isinstance(doc.get("examples"), dict):
            errors.append("examples must be an object")
    elif name == "config_census":
        for field in ("undeclared_literal", "undeclared_second_surface"):
            if not isinstance(doc.get(field), list):
                errors.append(f"{field} must be a list")
    elif name == "intraday_health":
        if doc.get("schema_version") != 1:
            errors.append("schema_version must be 1")
        if not isinstance(doc.get("no_findings"), bool):
            errors.append("no_findings must be boolean")
        if not isinstance(doc.get("findings"), list):
            errors.append("findings must be a list")
        if not isinstance(doc.get("counts"), dict):
            errors.append("counts must be an object")
        if doc.get("no_findings") is True and doc.get("findings"):
            errors.append("no_findings=true with non-empty findings")
        if doc.get("no_findings") is False and not doc.get("findings"):
            errors.append("no_findings=false with empty findings")
    return errors


def _extract_gate_liveness(doc: dict):
    out = []
    for gate, info in (doc.get("gates") or {}).items():
        status = (info or {}).get("status")
        if status in ("dead", "stale"):
            out.append((gate, f"死门/陈旧门: {gate}",
                        f"status={status} | {str((info or {}).get('detail') or '')[:220]}"))
    return out


def _extract_watchdog(doc: dict):
    out = []
    for alarm in doc.get("alarms") or []:
        kind = str(alarm.get("kind") or "")
        key = str(alarm.get("key") or "")
        check = key if key.startswith(kind + ":") else f"{kind}:{key}"
        out.append((check, f"载体异常 {check}",
                    f"{str(alarm.get('detail') or '')[:180]} | {str(alarm.get('action') or '')[:120]}"))
    return out


def _extract_invariant(doc: dict):
    out = []
    examples = doc.get("examples") or {}
    for check, count in (doc.get("violations") or {}).items():
        if not count:
            continue
        out.append((check, f"不变量违反 {check} ×{count}",
                    f"{count} | 例: {str(examples.get(check) or '')[:220]}"))
    return out


def _extract_config_census(doc: dict):
    out = []
    for kind in ("undeclared_literal", "undeclared_second_surface"):
        for item in doc.get(kind) or []:
            path = item.get("path") if isinstance(item, dict) else item
            out.append((str(path or "?"), f"配置键未声明/第二源 ({kind})", str(path)[:200]))
    return out


def _extract_intraday_health(doc: dict):
    out = []
    for item in doc.get("findings") or []:
        key = str(item.get("key") or "").split(":", 1)[-1]
        out.append((key, str(item.get("title") or "盘中异常"), str(item.get("evidence") or "")))
    return out


SOURCES = [
    {
        "name": "gate_liveness",
        "path": "data/gate_liveness_latest.json",
        "max_age_h": 96,
        "extract": _extract_gate_liveness,
        "consumer": None,
    },
    {
        "name": "watchdog",
        "path": "data/watchdog_liveness_latest.json",
        "max_age_h": 96,
        "extract": _extract_watchdog,
        "consumer": None,
    },
    {
        "name": "invariant",
        "path": "data/invariant_check_latest.json",
        "max_age_h": 96,
        "extract": _extract_invariant,
        "consumer": None,
    },
    {
        "name": "config_census",
        "path": "data/config_key_reverse_census_latest.json",
        "max_age_h": 192,
        "extract": _extract_config_census,
        "consumer": "tests/test_config_key_reverse_census.py",
    },
    {
        "name": "intraday_health",
        "path": "data/intraday_health_latest.json",
        "max_age_h": 30,
        "extract": _extract_intraday_health,
        "consumer": None,
    },
]


def _severity_for(key: str, source: str) -> str:
    if source == "intraday_health":
        return "P0"
    if source == "detector":
        return "P0"
    if key == "D1:open_positions:never_fed":
        return "P0"
    if key == "W1_kr2_cohort_broken":
        return "P0"
    if key == "W6_except_stack_grew":
        return "P2"
    return "P1"


def _pnl_path_for(key: str, source: str, severity: str) -> str:
    if source == "intraday_health":
        return "capital_preservation"
    if severity == "P0":
        return "capital_preservation"
    if key == "W6_except_stack_grew":
        return "cost_reduction"
    return "measurement_validity"


def collect():
    findings, source_state = [], {}
    for source in SOURCES:
        name = source["name"]
        path = BASE / source["path"]
        on_demand = bool(source["consumer"])
        carrier_alive = (not on_demand) or (BASE / source["consumer"]).exists()

        if on_demand and not carrier_alive:
            source_state[name] = {
                "fresh": False, "carrier_alive": False, "artifact_fresh": False,
                "on_demand": True,
                "schema_valid": False, "n": 0, "consumer": source["consumer"],
                "why": f"按需源消费者缺失 ({source['consumer']})",
            }
            findings.append((
                "detector:" + name, f"按需源消费者消失: {name}",
                f"判定载体 {source['consumer']} 不存在", "detector",
            ))
            continue

        if not path.exists():
            if on_demand:
                source_state[name] = {
                    "fresh": True, "carrier_alive": True, "artifact_fresh": False,
                    "schema_valid": True, "n": 0, "consumer": source["consumer"], "on_demand": True,
                    "why": f"按需源: 消费者 {source['consumer']} 存在, 无产物不判停更",
                }
                continue
            source_state[name] = {
                "fresh": False, "carrier_alive": True, "artifact_fresh": False,
                "schema_valid": False, "n": 0, "why": f"产物缺失 ({source['path']})",
            }
            findings.append((
                "detector:" + name, f"探测器产物缺失: {name}",
                f"期望产物 {source['path']} 不存在", "detector",
            ))
            continue

        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            schema_errors = _validate_source(name, doc)
            if schema_errors:
                raise ValueError("; ".join(schema_errors))
            source_time = _source_timestamp(name, doc)
            age = (_now() - source_time).total_seconds() / 3600.0
        except Exception as exc:
            source_state[name] = {
                "fresh": False, "carrier_alive": True, "artifact_fresh": False,
                "schema_valid": False, "n": 0, "why": f"产物不可用: {type(exc).__name__}: {exc}",
            }
            findings.append((
                "detector:" + name, f"探测器产物 schema invalid: {name}",
                f"{source['path']} 解析/契约失败: {exc}", "detector",
            ))
            continue

        artifact_fresh = age <= source["max_age_h"]
        take_items = artifact_fresh or not on_demand
        items = source["extract"](doc) if take_items else []
        if on_demand:
            source_state[name] = {
                "fresh": True, "carrier_alive": True, "artifact_fresh": artifact_fresh,
                "schema_valid": True, "n": len(items), "consumer": source["consumer"], "on_demand": True,
                "why": f"消费者存在; 产物 age={age:.1f}h"
                       + ("" if artifact_fresh else " (过期读数不取)"),
            }
        else:
            source_state[name] = {
                "fresh": artifact_fresh, "carrier_alive": True,
                "artifact_fresh": artifact_fresh, "schema_valid": True,
                "n": len(items), "why": f"age={age:.1f}h (阈值 {source['max_age_h']}h)",
            }

        if name == "intraday_health":
            rc_index = {k.split(":", 1)[1]: k for k, _, _, src in findings if src in STRUCTURED_SOURCES}
            for extra in _store_structured_keys():
                rc_index.setdefault(extra.split(":", 1)[1], extra)
            mapped = []
            for key, title, evidence in items:
                hits = [rc_index[n] for n in _structured_roots(evidence) if n in rc_index]
                if hits:
                    # 根因已在结构化源在册 ⇒ 不新建, 把日志签名挂到该条 (一因一条, 不升 P0)
                    note = f"另见盘中日志签名 ({_today()}): 命中 {', '.join(hits)} | {evidence[:400]}"
                    mapped.append((APPEND_MARK + hits[0], title, note))
                else:
                    mapped.append((f"rc{_intraday_family(evidence)}", title, evidence))
            items = mapped

        for key, title, evidence in items:
            findings.append((f"{name}:{key}", title, evidence, name))

        if not artifact_fresh and not on_demand:
            findings.append((
                "detector:" + name, f"探测器产物停更: {name}",
                f"{source['path']} age={age:.1f}h > {source['max_age_h']}h", "detector",
            ))

    return findings, source_state


def _load():
    if not FINDINGS.exists():
        return {"findings": {}, "applied_decisions": {}}
    try:
        state = json.loads(FINDINGS.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"findings.json 损坏, 拒绝静默重建: {exc}") from exc
    if not isinstance(state, dict) or not isinstance(state.get("findings"), dict):
        raise RuntimeError("findings.json 结构损坏: findings 必须为 object")
    state.setdefault("applied_decisions", {})
    return state


def _atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _append_history(rows):
    if not rows:
        return
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_decisions():
    if not DECISIONS.exists():
        return []
    decisions = []
    for line_number, line in enumerate(DECISIONS.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            decision = json.loads(line)
        except Exception as exc:
            raise RuntimeError(f"decisions.jsonl 第 {line_number} 行损坏: {exc}") from exc
        required = {"schema_version", "key", "disposition", "decided_by", "decided_at", "rationale"}
        missing = sorted(required - set(decision))
        if missing:
            raise RuntimeError(f"decisions.jsonl 第 {line_number} 行缺字段: {missing}")
        if decision["schema_version"] != 2:
            raise RuntimeError(f"decisions.jsonl 第 {line_number} 行 schema_version 必须为 2")
        canonical = json.dumps(decision, ensure_ascii=False, sort_keys=True)
        decision["_id"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        decisions.append(decision)
    return decisions


def _is_active(finding: dict) -> bool:
    return finding.get("status") in ACTIVE_STATUSES


def _due_for(finding: dict) -> str:
    """展示轴 = 逾期轴 (2026-09-30 修复): 此前 STATE/digest 用 action_due_by 优先,
    逾期计算用 decide_by 优先 ⇒ 同一条 finding 看板显示的 due 与逾期判定不同轴
    (实测 rc64370b7228 重开后显示首开旧值 due=9/22, 显得逾期 8 天)。"""
    return (finding.get("decide_by") or finding.get("action_due_by")
            or finding.get("review_by") or "-")


def _is_overdue(finding: dict) -> bool:
    return _is_active(finding) and _due_for(finding) < _today()


def _bundle_blocks(store: dict, finding: dict) -> list[str]:
    if finding.get("closure_mode") != "ALL" or not finding.get("bundle_id"):
        return []
    linked = finding.get("linked_keys") or []
    return sorted(
        key for key in linked
        if key != finding.get("key") and key in store and _is_active(store[key])
    )


def _apply_decisions(state: dict, decisions: list, history: list):
    store = state["findings"]
    applied = state.setdefault("applied_decisions", {})
    for decision in decisions:
        decision_id = decision["_id"]
        if applied.get(decision_id):
            continue
        key = decision["key"]
        if key not in store:
            raise RuntimeError(f"decision 引用不存在的 finding: {key}")
        finding = store[key]
        if decision.get("severity") in SEVERITY_ORDER:
            finding["severity"] = decision["severity"]
        if decision.get("pnl_path"):
            finding["pnl_path"] = decision["pnl_path"]
        if decision.get("action_owner"):
            finding["owner"] = decision["action_owner"]
        if decision.get("bundle_id"):
            finding["bundle_id"] = decision["bundle_id"]
            finding["closure_mode"] = decision.get("closure_mode") or "ALL"
        if decision.get("linked_keys"):
            finding["linked_keys"] = decision["linked_keys"]
            for linked_key in decision["linked_keys"]:
                if linked_key not in store:
                    raise RuntimeError(f"bundle 引用不存在的 finding: {linked_key}")
                store[linked_key]["bundle_id"] = finding["bundle_id"]
                store[linked_key]["closure_mode"] = finding["closure_mode"]

        disposition = decision["disposition"]
        finding["last_decision"] = {
            "id": decision_id, "disposition": disposition,
            "decided_by": decision["decided_by"], "decided_at": decision["decided_at"],
            "rationale": decision["rationale"],
        }
        if disposition in CLOSE_DECISIONS:
            blockers = _bundle_blocks(store, finding)
            if blockers:
                finding["status"] = "DECIDED"
                finding["close_blocked_by"] = blockers
            else:
                finding["status"] = "CLOSED"
                finding["closed_at"] = _today()
                finding["close_evidence"] = (
                    f"用户/维护裁决 {disposition}: {decision['rationale']} | "
                    f"decision_id={decision_id[:12]}"
                )
                finding["suppression"] = disposition
                finding.pop("close_blocked_by", None)
        elif disposition in ("ACCEPT_RISK", "DEFER"):
            finding["status"] = "CONTROLLED"
            finding["review_by"] = decision.get("review_by") or _date_plus(
                SEVERITY_SLA[finding.get("severity", "P1")]["review_days"]
            )
            finding["validation_metric"] = decision.get("validation_metric")
            finding["validation_result"] = decision.get("validation_result")
        elif disposition == "FIX_NOW":
            finding["status"] = "DECIDED"
            finding["action_due_by"] = decision.get("action_due_by") or _date_plus(
                SEVERITY_SLA[finding.get("severity", "P1")]["action_days"]
            )
            finding["review_by"] = decision.get("review_by") or _date_plus(
                SEVERITY_SLA[finding.get("severity", "P1")]["review_days"]
            )
        elif disposition == "ACTION_DONE":
            finding["status"] = "VERIFY_PENDING"
            finding["validation_result"] = decision.get("validation_result")
        elif disposition == "VERIFY_PASS":
            finding["status"] = "VERIFY_PENDING"
            finding["validation_result"] = "pass"
        elif disposition == "VERIFY_FAIL":
            finding["status"] = "OPEN"
            finding["recurrence_count"] = int(finding.get("recurrence_count") or 0) + 1
            finding["validation_result"] = "fail"
        else:
            raise RuntimeError(f"未知 disposition: {disposition}")

        applied[decision_id] = _now().isoformat(timespec="seconds")
        history.append({
            "ts": _now().isoformat(timespec="seconds"), "key": key,
            "event": "DECISION", "disposition": disposition,
            "decision_id": decision_id, "rationale": decision["rationale"],
        })


def _migrate_intraday_keys(state: dict, history: list, collected: list = ()):
    """旧签名键 → 族键/归并形 (2026-09-22 RFC-091 v2.1)。

    不做这一步的话: 键改名会被 reconcile 读成"源不再报该项" ⇒ **假关闭** (把改名
    当修好), 正是本仓最忌讳的错。故: 根因命中结构化源 ⇒ 显式关成 MERGED_DUPLICATE
    并写明"非修复"; 无结构化根因 ⇒ 原地改名为稳定族键, 生命周期照常。
    """
    store = state["findings"]
    # 目标索引: 在册 + 本轮即将建的结构化条 (二者都算"已在册", 否则首轮会退化成改名而非归并)
    target_index = {k.split(":", 1)[1]: k for k in store if k.split(":", 1)[0] in STRUCTURED_SOURCES}
    for k, _, _, src in collected:
        if src in STRUCTURED_SOURCES:
            target_index.setdefault(k.split(":", 1)[1], k)
    for key in list(store):
        if not INTRA_FAMILY_RE.match(key) or key.startswith("intraday_health:rc"):
            continue
        finding = store[key]
        if finding.get("status") == "CLOSED" and finding.get("suppression") == "MERGED_DUPLICATE":
            # 幂等跳过 (2026-09-30 修复): 已归并的旧键每轮被重新关闭 ⇒ closed_at 刷成当天
            # ⇒ 进签名 ⇒ hash 天天变 ⇒ 每日假推送 (实测 9/26~9/30 与昨日实质同板仍推)。
            continue
        evidence = finding.get("evidence") or ""
        roots = _structured_roots(evidence)
        hosts = [target_index[h] for h in roots if h in target_index]
        if hosts:
            target = hosts[0]
            host = store.get(target)
            if host is None:
                # 宿主本轮才建 ⇒ 记录归并去向 (证据不丢); **仍必须显式关成归并** —
                # 不关的话本段之后会被读成"源不再报该项" ⇒ 自我制造的假关闭。
                history.append({"ts": _now().isoformat(timespec="seconds"), "key": key,
                                "event": "MERGE_PENDING", "to": target,
                                "evidence": evidence[:400]})
                state.setdefault("pending_merges", []).append([key, target])
            else:
                host["merged_from_keys"] = (host.get("merged_from_keys") or [])[-4:] + [key]
                _record_merged(host, evidence)
            finding["status"] = "CLOSED"
            finding["closed_at"] = _today()
            finding["suppression"] = "MERGED_DUPLICATE"
            finding["close_evidence"] = (
                f"根因去重 (RFC-091 v2.1): 该日志签名与 {target} 同根因 ⇒ 不重复记账; "
                f"这是**归并, 不是修复** — 根因未消, 仍由 {target} 跟踪")
            history.append({"ts": _now().isoformat(timespec="seconds"), "key": key,
                            "event": "MERGED_DUPLICATE", "to": target,
                            "evidence": finding["close_evidence"]})
            continue
        new_key = f"intraday_health:rc{_intraday_family(evidence)}"
        if new_key in store:
            finding["status"] = "CLOSED"
            finding["closed_at"] = _today()
            finding["suppression"] = "MERGED_DUPLICATE"
            finding["close_evidence"] = f"与 {new_key} 同族重复 ⇒ 归并, 非修复"
            history.append({"ts": _now().isoformat(timespec="seconds"), "key": key,
                            "event": "MERGED_DUPLICATE", "to": new_key})
            continue
        finding["renamed_from"] = key
        finding["key"] = new_key          # 键改名必须同步改 finding["key"], 否则摘要/STATE 仍印旧名
        del store[key]
        store[new_key] = finding
        history.append({"ts": _now().isoformat(timespec="seconds"), "key": new_key,
                        "event": "RENAMED", "from": key})


def _migrate_finding(finding: dict):
    finding.setdefault("status", "OPEN")
    finding.setdefault("first_seen", _today())
    finding.setdefault("last_seen", _today())
    finding.setdefault("recurrence_count", 0)
    finding.setdefault("closed_at", None)
    finding.setdefault("close_evidence", None)
    source = finding.get("source", "")
    key = finding.get("key", "")
    severity = finding.get("severity") or _severity_for(key.split(":", 1)[-1], source)
    finding["severity"] = severity
    finding["pnl_path"] = finding.get("pnl_path") or _pnl_path_for(
        key.split(":", 1)[-1], source, severity
    )
    finding["owner"] = finding.get("owner") or KEY_OWNER.get(key) or DEFAULT_OWNER.get(source, "维护批")
    if finding["status"] in ACTIVE_STATUSES:
        if not finding.get("decide_by"):
            finding["decide_by"] = _date_plus(SEVERITY_SLA[severity]["decide_days"])
        if not finding.get("action_due_by"):
            finding["action_due_by"] = _date_plus(SEVERITY_SLA[severity]["action_days"])
        if not finding.get("review_by"):
            finding["review_by"] = _date_plus(SEVERITY_SLA[severity]["review_days"])
    return finding


def _new_finding(key: str, title: str, evidence: str, source: str) -> dict:
    severity = _severity_for(key.split(":", 1)[-1], source)
    return {
        "key": key, "source": source, "title": title, "evidence": evidence,
        "status": "OPEN", "severity": severity,
        "pnl_path": _pnl_path_for(key.split(":", 1)[-1], source, severity),
        "owner": KEY_OWNER.get(key) or DEFAULT_OWNER.get(source, "维护批"),
        "first_seen": _today(), "last_seen": _today(),
        "decide_by": _date_plus(SEVERITY_SLA[severity]["decide_days"]),
        "action_due_by": _date_plus(SEVERITY_SLA[severity]["action_days"]),
        "review_by": _date_plus(SEVERITY_SLA[severity]["review_days"]),
        "recurrence_count": 0, "closed_at": None, "close_evidence": None,
    }


def _source_for_key(store: dict, key: str):
    if key.startswith("detector:"):
        target = key.split("detector:", 1)[1]
        for source in SOURCES:
            if source["name"] == target:
                return source["name"]
    finding = store.get(key)
    return finding.get("source") if finding else None


def _promote_backlog(store: dict, history: list):
    wip = sum(1 for item in store.values() if item.get("status") in WIP_STATUSES)
    for key, finding in sorted(store.items(), key=lambda item: (
        SEVERITY_ORDER.get(item[1].get("severity"), 9), item[1].get("first_seen", ""), item[0]
    )):
        if finding.get("status") != "BACKLOG" or wip >= MAX_WIP:
            continue
        finding["status"] = "OPEN"
        wip += 1
        history.append({"ts": _now().isoformat(timespec="seconds"), "key": key,
                        "event": "BACKLOG_PROMOTED"})


def reconcile(state: dict, collected: list, source_state: dict, decisions: list):
    store = state["findings"]
    for finding in store.values():
        _migrate_finding(finding)

    history = []
    _apply_decisions(state, decisions, history)
    _migrate_intraday_keys(state, history, collected)
    seen = {key for key, _, _, _ in collected if APPEND_MARK not in key}

    for key, title, evidence, source in collected:
        if APPEND_MARK in key:
            # 归并项永不入库: 只把签名挂到已在册的结构化条 (否则会被当成"新条"创建,
            # 再被下一段读成"源不再报该项" ⇒ 自我制造的假关闭)
            continue
        if key not in store:
            store[key] = _new_finding(key, title, evidence, source)
            history.append({"ts": _now().isoformat(timespec="seconds"), "key": key,
                            "event": "OPEN", "title": title, "evidence": evidence})
            continue

        finding = store[key]
        finding["last_seen"] = _today()
        finding["title"] = title
        finding["evidence"] = evidence
        if finding.get("status") == "CLOSED" and finding.get("suppression") in CLOSE_DECISIONS:
            continue
        if finding.get("status") in ("CONTROLLED", "DECIDED", "ACTION_PENDING", "VERIFY_PENDING"):
            continue
        if finding.get("status") == "UNVERIFIABLE":
            finding["status"] = finding.pop("pre_unverifiable_status", "OPEN")
        elif finding.get("status") == "CLOSED":
            finding["status"] = "OPEN"
            finding["first_seen"] = _today()
            # REOPEN 必须重置全部 SLA 轴 (2026-09-30 修复): 此前只重置 decide_by,
            # action_due_by/review_by 留首开旧值 ⇒ 看板显示 due 早于重开日, 复发项
            # 看似逾期 N 天 (实测 rc64370b7228 9/29 重开却显示 due=9/22)。
            _sla = SEVERITY_SLA[finding["severity"]]
            finding["decide_by"] = _date_plus(_sla["decide_days"])
            finding["action_due_by"] = _date_plus(_sla["action_days"])
            finding["review_by"] = _date_plus(_sla["review_days"])
            finding["closed_at"] = None
            finding["close_evidence"] = None
            finding["recurrence_count"] = int(finding.get("recurrence_count") or 0) + 1
            history.append({"ts": _now().isoformat(timespec="seconds"), "key": key,
                            "event": "REOPEN", "title": title})

    for key, finding in list(store.items()):
        if key in seen or not _is_active(finding):
            continue
        source = _source_for_key(store, key)
        source_health = source_state.get(source) or {}
        is_detector = key.startswith("detector:")
        if source_health.get("on_demand") and is_detector:
            source_usable = (
                source_health.get("fresh") is True
                and source_health.get("schema_valid") is True
                and source_health.get("carrier_alive") is True
            )
        else:
            source_usable = (
                source_health.get("fresh") is True
                and source_health.get("schema_valid") is True
                and (source_health.get("artifact_fresh") is not False)
            )
        if not source_usable:
            if finding.get("status") != "UNVERIFIABLE":
                finding["pre_unverifiable_status"] = finding["status"]
                finding["status"] = "UNVERIFIABLE"
                history.append({"ts": _now().isoformat(timespec="seconds"), "key": key,
                                "event": "UNVERIFIABLE", "why": "源不可用, 禁止自动关闭"})
            continue

        blockers = _bundle_blocks(store, finding)
        if blockers:
            finding["close_blocked_by"] = blockers
            continue

        finding["status"] = "CLOSED"
        finding["closed_at"] = _today()
        finding["close_evidence"] = (
            f"源侧回读: {source} 健康产物不再报该项 | 关闭日 {_today()}"
        )
        history.append({"ts": _now().isoformat(timespec="seconds"), "key": key,
                        "event": "CLOSED", "evidence": finding["close_evidence"]})

    # 归并项: 不新建条, 只把日志签名追加到已在册的结构化条上 (可回读, 不产生新 P0)
    for key, title, evidence, source in collected:
        if APPEND_MARK not in key:
            continue
        target = key.split(APPEND_MARK, 1)[1]
        host = store.get(target)
        if host is None:
            continue
        _record_merged(host, evidence)
        host["last_seen"] = _today()
        history.append({"ts": _now().isoformat(timespec="seconds"), "key": target,
                        "event": "EVIDENCE_MERGED", "evidence": evidence})

    # 归并来源回填 (宿主可能本轮才建 ⇒ 迁移期先记 pending_merges, 此处统一销账)
    rest = []
    for src_key, dst in (state.get("pending_merges") or []):
        host = store.get(dst)
        if host is None:
            rest.append([src_key, dst])
            continue
        known = host.get("merged_from_keys") or []
        if src_key not in known:
            host["merged_from_keys"] = known[-4:] + [src_key]
    state["pending_merges"] = rest

    for finding in store.values():
        if (finding.get("status") == "DECIDED"
                and (finding.get("last_decision") or {}).get("disposition") in CLOSE_DECISIONS
                and not _bundle_blocks(store, finding)):
            finding["status"] = "CLOSED"
            finding["closed_at"] = _today()
            finding["close_evidence"] = (
                f"用户/维护裁决 {finding['last_decision']['disposition']}: "
                f"{finding['last_decision']['rationale']} | bundle 已全部关闭"
            )
            finding["suppression"] = finding["last_decision"]["disposition"]
            finding.pop("close_blocked_by", None)
        if finding.get("status") == "CONTROLLED":
            if not finding.get("review_by") or finding["review_by"] <= _today():
                finding["status"] = "REVIEW_DUE"
            elif finding.get("validation_result") == "fail":
                finding["status"] = "OPEN"
        elif finding.get("status") == "DECIDED" and (
            not finding.get("action_due_by") or finding["action_due_by"] < _today()
        ):
            finding["status"] = "REVIEW_DUE"

    _promote_backlog(store, history)
    wip = sum(1 for item in store.values() if item.get("status") in WIP_STATUSES)
    for key, finding in store.items():
        if key in seen and finding.get("status") == "OPEN" and finding["severity"] in ("P2", "P3") and wip >= MAX_WIP:
            finding["status"] = "BACKLOG"
            history.append({"ts": _now().isoformat(timespec="seconds"), "key": key,
                            "event": "BACKLOG", "why": f"WIP >= {MAX_WIP}"})

    state["last_run"] = _now().isoformat(timespec="seconds")
    state["findings"] = store
    state["decision_count"] = len(decisions)
    active = [finding for finding in store.values() if _is_active(finding)]
    closed = [finding for finding in store.values() if finding.get("status") == "CLOSED"]
    overdue = [finding for finding in store.values() if _is_overdue(finding)]
    return active, closed, overdue, history


def _stop_condition(store: dict, source_state: dict):
    active = [finding for finding in store.values() if _is_active(finding)]
    closed = [finding for finding in store.values() if finding.get("status") == "CLOSED"]
    unverifiable = [finding for finding in store.values() if finding.get("status") == "UNVERIFIABLE"]
    waiting = [finding for finding in store.values() if finding.get("status") in (
        "DECIDED", "ACTION_PENDING", "VERIFY_PENDING", "BACKLOG"
    )]
    review_due = [finding for finding in store.values() if finding.get("status") == "REVIEW_DUE"]
    stale = [name for name, health in source_state.items() if not health.get("fresh")]
    counts = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
    for finding in active:
        counts[finding.get("severity", "P1")] += 1
    criteria = [
        ("无未决问题", not active),
        ("无不可验证", not unverifiable),
        ("无待行动/验证", not waiting),
        ("无到期复查", not review_due),
        ("探测器健康", not stale),
        ("CLOSED证据完整", all(finding.get("close_evidence") for finding in closed)),
    ]
    return {
        "done": all(value for _, value in criteria), "criteria": criteria,
        "active": len(active), "closed": len(closed), "unverifiable": len(unverifiable),
        "waiting": len(waiting), "review_due": len(review_due),
        "severity": counts, "stale_sources": stale,
        "wip": sum(1 for finding in store.values() if finding.get("status") in WIP_STATUSES),
        "backlog": sum(1 for finding in store.values() if finding.get("status") == "BACKLOG"),
    }


def _render_state_md(store, source_state, summary, previous_run="-"):
    lines = []
    lines.append("# meta_audit loop — STATE v2")
    lines.append("")
    lines.append("> L1 只读环。`LOOP_DONE` = 无未决问题; 不是脚本跑完, 也不是已分配 owner。")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 上轮运行 | {previous_run} |")
    lines.append(f"| LOOP_DONE | **{'TRUE' if summary['done'] else 'FALSE'}** |")
    lines.append("| 判据 | " + " / ".join(f"{name}={value}" for name, value in summary["criteria"]) + " |")
    lines.append(f"| 未决 | {summary['active']} "
                 f"(P0={summary['severity']['P0']} P1={summary['severity']['P1']} "
                 f"P2={summary['severity']['P2']} P3={summary['severity']['P3']}) |")
    lines.append(f"| WIP / BACKLOG | {summary['wip']}/{MAX_WIP} / {summary['backlog']} |")
    lines.append("")
    lines.append("## 探测器")
    lines.append("")
    lines.append("| 源 | 健康 | 产物新鲜 | schema | 读数 | 说明 |")
    lines.append("|---|---|---|---|---|---|")
    for name in sorted(source_state):
        health = source_state[name]
        lines.append(f"| {name} | {'✅' if health.get('fresh') else '❌'} | "
                     f"{'✅' if health.get('artifact_fresh') else '❌'} | "
                     f"{'✅' if health.get('schema_valid') else '❌'} | "
                     f"{health.get('n', 0)} | {health.get('why', '')} |")
    lines.append("")
    lines.append("## 未决")
    lines.append("")
    lines.append("| severity | key | 状态 | pnl_path | owner | due | evidence |")
    lines.append("|---|---|---|---|---|---|---|")
    active = sorted(
        (finding for finding in store.values() if _is_active(finding)),
        key=lambda item: (SEVERITY_ORDER.get(item.get("severity"), 9), item.get("key", "")),
    )
    for finding in active:
        due = _due_for(finding)
        evidence = str(finding.get("evidence") or "").replace("|", "/")[:180]
        lines.append(f"| {finding.get('severity')} | {finding.get('key')} | {finding.get('status')} | "
                     f"{finding.get('pnl_path')} | {finding.get('owner')} | {due} | {evidence} |")
    if not active:
        lines.append("| — | (无) | — | — | — | — | — |")
    lines.append("")
    lines.append("## CONTROLLED")
    lines.append("")
    lines.append("| key | review_by | validation_metric | result |")
    lines.append("|---|---|---|---|")
    controlled = [finding for finding in store.values() if finding.get("status") == "CONTROLLED"]
    for finding in sorted(controlled, key=lambda item: item.get("review_by") or ""):
        lines.append(f"| {finding.get('key')} | {finding.get('review_by')} | "
                     f"{finding.get('validation_metric') or '-'} | {finding.get('validation_result') or '-'} |")
    if not controlled:
        lines.append("| (无) | — | — | — |")
    lines.append("")
    lines.append("## CLOSED")
    lines.append("")
    lines.append("| key | 关闭日 | evidence |")
    lines.append("|---|---|---|")
    closed = [finding for finding in store.values() if finding.get("status") == "CLOSED"]
    for finding in sorted(closed, key=lambda item: item.get("key", "")):
        lines.append(f"| {finding.get('key')} | {finding.get('closed_at')} | "
                     f"{str(finding.get('close_evidence') or '').replace('|', '/')[:180]} |")
    if not closed:
        lines.append("| (无) | — | — |")
    lines.append("")
    lines.append("## 边界")
    lines.append("")
    lines.append("- 只消费结构化产物; `{}` 或缺 expected 字段一律 schema invalid。")
    lines.append("- 测试通过只进入 VERIFY_PENDING; CLOSED 必须有源侧回读或显式裁决。")
    lines.append("- 不改参、不重启、不下单; 维护 WIP 上限 3。")
    return "\n".join(lines) + "\n"


def _digest(summary, store):
    lines = []
    lines.append(
        f"[meta_audit] {_now():%Y-%m-%d %H:%M} | LOOP_DONE={str(summary['done']).upper()} | "
        f"未决 {summary['active']} (P0 {summary['severity']['P0']} / P1 {summary['severity']['P1']} / "
        f"P2 {summary['severity']['P2']} / P3 {summary['severity']['P3']}) | "
        f"WIP {summary['wip']}/{MAX_WIP} | BACKLOG {summary['backlog']}"
    )
    lines.append("[meta_audit] 判据: " + " / ".join(
        f"{name}={str(value).lower()}" for name, value in summary["criteria"]
    ))
    if summary["stale_sources"]:
        lines.append("[meta_audit] ❌ 探测器不健康: " + ", ".join(summary["stale_sources"]))
    active = sorted(
        (finding for finding in store.values() if _is_active(finding)),
        key=lambda item: (SEVERITY_ORDER.get(item.get("severity"), 9), item.get("key", "")),
    )
    for finding in active[:MAX_DIGEST_ITEMS]:
        due = _due_for(finding)
        marker = "🔥" if finding.get("severity") == "P0" else "⚠️"
        lines.append(
            f"[meta_audit] {marker} {finding.get('severity')} {finding.get('status')} {finding.get('key')} | "
            f"pnl_path={finding.get('pnl_path')} | owner={finding.get('owner')} | due={due}"
        )
    if len(active) > MAX_DIGEST_ITEMS:
        lines.append(f"[meta_audit] …其余 {len(active) - MAX_DIGEST_ITEMS} 项见 STATE.md")
    lines.append(f"[meta_audit] STATE={STATE_MD}")
    return "\n".join(lines)


def _status():
    state = _load()
    store = state.get("findings", {})
    rows = [finding for finding in store.values() if _is_active(finding)]
    print(f"[meta_audit] --status: 上轮 {state.get('last_run', '-')} | 未决 {len(rows)}")
    for finding in sorted(rows, key=lambda item: (
        SEVERITY_ORDER.get(item.get("severity"), 9), item.get("key", "")
    )):
        due = _due_for(finding)
        print(f"  {finding.get('severity')} {finding.get('status'):15} {finding.get('key'):60} "
              f"owner={finding.get('owner')} due={due}")
    return 0


def _acquire_lock():
    LOOP_DIR.mkdir(parents=True, exist_ok=True)
    handle = LOCK_FILE.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError("meta_audit loop 已有并发运行, 拒绝覆盖状态")
    handle.seek(0)
    handle.truncate()
    handle.write(_now().isoformat(timespec="seconds") + "\n")
    handle.flush()
    return handle


def main(argv):
    if "--status" in argv:
        return _status()
    if not BASE.exists():
        print(f"[meta_audit] 仓根不存在: {BASE}")
        return 3

    lock = _acquire_lock()
    try:
        collected, source_state = collect()
        state = _load()
        decisions = _read_decisions()
        _, _, _, history = reconcile(state, collected, source_state, decisions)
        summary = _stop_condition(state["findings"], source_state)

        _atomic_write(STATE_MD, _render_state_md(
            state["findings"], source_state, summary, state["last_run"]
        ))
        _atomic_write(FINDINGS, json.dumps(state, ensure_ascii=False, indent=1))
        _append_history(history)

        signature = {
            "active": sorted(
                f"{key}|{finding.get('status')}|{finding.get('severity')}|{finding.get('pnl_path')}|"
                f"{finding.get('decide_by')}|{finding.get('action_due_by')}|{finding.get('review_by')}"
                for key, finding in state["findings"].items() if _is_active(finding)
            ),
            "closed": sorted(
                f"{key}|{finding.get('closed_at')}" for key, finding in state["findings"].items()
                if finding.get("status") == "CLOSED"
            ),
            # 逾期是独立状态维度 (2026-09-30 修复): 日期本身不随天变 ⇒ 某项今天
            # 新变逾期时签名不变、永不推送 (此前被归并 closed_at 每日 churn 掩盖)。
            "overdue": sorted(
                key for key, finding in state["findings"].items() if _is_overdue(finding)
            ),
            "stale_sources": sorted(summary["stale_sources"]),
            "done": summary["done"],
            "wip": summary["wip"],
            "backlog": summary["backlog"],
        }
        digest_hash = hashlib.sha256(
            json.dumps(signature, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        previous_digest = {}
        if LAST_DIGEST.exists():
            try:
                previous_digest = json.loads(LAST_DIGEST.read_text(encoding="utf-8"))
            except Exception:
                previous_digest = {}
        changed = previous_digest.get("hash") != digest_hash
        _atomic_write(LAST_DIGEST, json.dumps({
            "hash": digest_hash, "ts": _now().isoformat(timespec="seconds"),
            "active": summary["active"], "wip": summary["wip"], "backlog": summary["backlog"],
        }, ensure_ascii=False))
        if changed or "--force-print" in argv:
            print(_digest(summary, state["findings"]))
        return 0
    finally:
        lock.close()


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:
        print(f"[meta_audit] 环自身异常 (不得读成零 finding): {type(exc).__name__}: {exc}")
        sys.exit(2)
