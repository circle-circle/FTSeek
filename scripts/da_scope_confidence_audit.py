#!/usr/bin/env python3
"""DA 范围 / 双 LLM / 置信门 耦合审计 (只读复算器) — DEB-20261027.

用途: 复算 `docs/debates/DEB-20261027-da-scope-and-confidence-coupling.md` 里每条读数。
只读: 读 sqlite(ro) / checkpoint json / candidate_funnel.jsonl / 日志文件; 零写入、零下单。

用法:
  ./venv/bin/python scripts/da_scope_confidence_audit.py funnel                 # 候选漏斗分阶段
  ./venv/bin/python scripts/da_scope_confidence_audit.py debate --since 2026-10-19
  ./venv/bin/python scripts/da_scope_confidence_audit.py trend  --date 2026-10-26
  ./venv/bin/python scripts/da_scope_confidence_audit.py capacity --date 2026-10-26
  ./venv/bin/python scripts/da_scope_confidence_audit.py confgate --date 2026-10-26 --log logs/ftseek_us.log-20261027
  ./venv/bin/python scripts/da_scope_confidence_audit.py first-night --date 2026-11-17 --expected-model qwen4-max
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(REPO, "data", "ftseek_us.db")
FUNNEL = os.path.join(REPO, "data", "shadow", "candidate_funnel.jsonl")
SCHED = os.path.join(REPO, "data", "shadow", "da_main_schedule.jsonl")
CKPT = os.path.join(REPO, "data", "da_checkpoint")
PIPELINE_LOG = os.path.join(REPO, "logs", "da_pipeline_stderr.log")
DEBATE_STATE = "/root/.hermes/skills/deep-stock-analysis/scripts"
ET = ZoneInfo("America/New_York")


def _db():
    return sqlite3.connect("file:%s?mode=ro" % DB, uri=True)


def _ckpts(date: str):
    return [p for p in sorted(glob.glob(os.path.join(CKPT, date, "US.*.json")))
            if not p.endswith(".raw.json")]


def _load(p):
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def _classify(a, c, v):
    """与生产同一实现 (skill 仓 debate_state.py)。"""
    if DEBATE_STATE not in sys.path:
        sys.path.insert(0, DEBATE_STATE)
    from debate_state import classify_debate
    return classify_debate(a, c, v)["state"]


def _jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _batch_day(batch):
    days = {str(c.get("observed_at") or "")[:10]
            for c in (batch.get("candidates") or []) if c.get("observed_at")}
    if len(days) > 1:
        raise ValueError("batch %s has mixed observed dates: %s" % (
            batch.get("batch_id"), sorted(days)))
    return next(iter(days), "")


def _funnel_batches(date="", batch_id=""):
    batches = [b for b in _jsonl(FUNNEL) if b.get("phase") == "da_selection"]
    if batch_id:
        batches = [b for b in batches if b.get("batch_id") == batch_id]
    if date:
        batches = [b for b in batches if _batch_day(b) == date]
    if not batches:
        raise ValueError("no DA selection batch for date=%s batch_id=%s" % (
            date or "*", batch_id or "*"))
    if not date and not batch_id:
        return batches[-1]
    if len(batches) > 1:
        raise ValueError("multiple batches %s; pass --batch-id" % [
            b.get("batch_id") for b in batches])
    return batches[0]


def _published_candidates(date):
    path = os.path.join(REPO, "data", "opportunity_scan_%s.json" % date)
    if not os.path.exists(path):
        return []
    return _load(path).get("candidates") or []


def _confidence_traces(date):
    docs = {doc.get("symbol"): doc for doc in map(_load, _ckpts(date))}
    traces = []
    for cand in _published_candidates(date):
        gate = (docs.get(cand.get("symbol"), {}).get("_signal_gate") or {})
        semantic = cand.get("confidence_semantic")
        final = cand.get("confidence")
        adjustment = (round(final - semantic, 4)
                      if isinstance(final, (int, float))
                      and isinstance(semantic, (int, float)) else None)
        traces.append({"symbol": cand.get("symbol"), "decision_id": cand.get("decision_id"),
                       "gate_decision": gate.get("decision"),
                       "gate_conf": gate.get("decision_conf"),
                       "semantic_conf": semantic, "warn_adjustment": adjustment,
                       "final_conf": final})
    return traces


def cmd_funnel(args):
    batch = _funnel_batches(args.date, args.batch_id)
    cands = batch.get("candidates") or []
    symbols = [c.get("symbol") for c in cands]
    print("batch=%s date=%s phase=%s policy=%s rows=%d unique_symbols=%d" % (
        batch.get("batch_id"), _batch_day(batch) or "unknown", batch.get("phase"),
        batch.get("policy_version"), len(cands), len(set(symbols))))
    print("stage=%s" % dict(collections.Counter(c.get("stage") for c in cands)))
    print("eligibility=%s" % dict(collections.Counter(c.get("status") for c in cands)))
    print("selection=%s" % dict(collections.Counter(c.get("selection_status") for c in cands)))
    print("reasons=%s" % dict(collections.Counter(
        x for c in cands for x in (c.get("reasons") or []))))
    print("stage_x_selection=%s" % dict(collections.Counter(
        (c.get("stage"), c.get("selection_status")) for c in cands)))


def cmd_debate(args):
    print("date analysis proposed gate_publishable db_da_rows filled_trades "
          "states_current_classifier raw_agreed db_conf(DA decision_id)")
    db = _db()
    days = [args.date] if args.date else sorted(os.listdir(CKPT))
    for day in days:
        if not args.date and (day < args.since or not day.startswith("2026-")):
            continue
        files = _ckpts(day)
        if not files:
            continue
        states = collections.Counter()
        agreed = proposed = publishable = 0
        for p in files:
            doc = _load(p)
            out = doc.get("_llm_output") or {}
            deb = out.get("_debate") or {}
            agreed += bool(deb.get("challenger_agreed"))
            active = doc.get("action") in ("BUY", "SELL_SHORT")
            proposed += active
            decision = (doc.get("_signal_gate") or {}).get("decision")
            publishable += active and decision in ("PASS", "WARN")
            try:
                states[_classify(out.get("direction"), deb.get("challenger_direction"),
                                 deb.get("challenger_verdict"))] += 1
            except Exception as exc:
                states["CLASSIFY_ERR:%s" % type(exc).__name__] += 1
        db_rows = db.execute(
            "select confidence from playbook_instructions "
            "where date=? and decision_id like 'DA-%'", (day,)).fetchall()
        conf = collections.Counter(r[0] for r in db_rows)
        filled = db.execute(
            "select count(*) from trade_records where trade_date=? "
            "and decision_id like 'DA-%'", (day,)).fetchone()[0]
        print("%s analysis=%d proposed=%d gate_publishable=%d db_da_rows=%d "
              "filled_trades=%d states=%s raw_agreed=%d conf=%s" % (
                  day, len(files), proposed, publishable, len(db_rows), filled,
                  dict(states), agreed, dict(conf)))
        for trace in _confidence_traces(day):
            print("  conf_trace %(symbol)s gate=%(gate_decision)s/%(gate_conf)s "
                  "semantic=%(semantic_conf)s warn_adjustment=%(warn_adjustment)s "
                  "final=%(final_conf)s did=%(decision_id)s" % trace)


def cmd_trend(args):
    cross = collections.Counter()
    for p in _ckpts(args.date):
        doc = _load(p)
        trend = (doc.get("technical") or {}).get("trend") or "?"
        cross[(trend, doc.get("action"))] += 1
    print("action_x_trend (%s):" % args.date)
    for k, v in sorted(cross.items(), key=lambda x: -x[1]):
        print("   %s %s -> %d" % (k[0], k[1], v))


def cmd_capacity(args):
    files = _ckpts(args.date)
    stamps = sorted((os.path.getmtime(p), os.path.basename(p)) for p in files)
    fmt = lambda t: datetime.fromtimestamp(t).strftime("%H:%M:%S")
    print("checkpoints=%d first=%s %s last=%s %s" % (
        len(files), fmt(stamps[0][0]), stamps[0][1], fmt(stamps[-1][0]), stamps[-1][1]))
    lat = []
    for p in files:
        v = ((_load(p).get("_llm_output") or {}).get("_llm_latency_ms"))
        if isinstance(v, (int, float)) and v > 0:
            lat.append(float(v))
    if lat:
        lat.sort()
        print("analyst_latency_ms n=%d p50=%.0f p90=%.0f max=%.0f" % (
            len(lat), lat[len(lat) // 2], lat[int(len(lat) * 0.9)], lat[-1]))
    if os.path.exists(SCHED):
        prog = [e.get("analyses_done") for e in
                (json.loads(l) for l in open(SCHED, encoding="utf-8") if l.strip())
                if str(e.get("date")) == args.date and e.get("status") == "heartbeat"]
        print("heartbeat_analyses_done=%s" % prog)


def cmd_confgate(args):
    db = _db()
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "select id,symbol,direction,confidence,decision_id,raw_instruction_json,"
        "skip_reason,executed,execution_state from playbook_instructions "
        "where date=? and decision_id like 'DA-%' order by symbol,id", (args.date,)).fetchall()
    conf_events, refused_events = [], []
    with open(args.log, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "门槛=" in line and " → SKIP" in line:
                conf_events.append(line.split("[playbook] ")[-1].split(" ")[0])
            elif "覆盖仲裁拒绝" in line:
                refused_events.append(line.split("覆盖仲裁拒绝: ")[-1].split(" ")[0])
    print("db_da_rows=%d conf_gate_events=%d legacy_arbitration_events=%d" % (
        len(rows), len(conf_events), len(refused_events)))
    print("生成日志缺少decision_id；只按唯一symbol关联，同日重跑不视为独立指令；多行标ambiguous。")
    for row in rows:
        raw = json.loads(row["raw_instruction_json"] or "{}")
        symbol_rows = [r for r in rows if r["symbol"] == row["symbol"]]
        joinable = len(symbol_rows) == 1
        decision_rows = [r for r in rows if r["decision_id"] == row["decision_id"]
                         and r["symbol"] == row["symbol"]]
        filled = ("ambiguous" if len(decision_rows) > 1 else db.execute(
            "select count(*) from trade_records where decision_id=? and symbol=?",
            (row["decision_id"], row["symbol"])).fetchone()[0])
        print("  id=%d %-9s published=%s current=%s conf=%s did=%s "
              "gate_events=%s refused_events=%s log_join=%s filled=%s "
              "executed=%s execution_state=%s skip_reason=%s" % (
                  row["id"], row["symbol"], raw.get("direction") or "unknown",
                  row["direction"], row["confidence"], row["decision_id"],
                  conf_events.count(row["symbol"]) if joinable else "ambiguous",
                  refused_events.count(row["symbol"]) if joinable else "ambiguous",
                  "symbol_unique" if joinable else "ambiguous", str(filled), row["executed"],
                  row["execution_state"], row["skip_reason"]))


def _funnel_by_day():
    out = collections.defaultdict(list)
    for batch in _jsonl(FUNNEL):
        if batch.get("phase") != "da_selection":
            continue
        day = _batch_day(batch)
        if day:
            out[day].append(batch)
    return out


def cmd_overlap(args):
    batches = _funnel_by_day()
    days = sorted(set(batches) | {
        d for d in os.listdir(CKPT) if d.startswith("2026-") and _ckpts(d)})
    days = [d for d in days if (not args.since or d >= args.since)
            and (not args.date or d <= args.date)]
    analyzed = {}
    print("date | batches | selected | analyzed | extras_share | retention | jaccard | intersection")
    pairs = []
    previous = None
    for day in days:
        group = batches.get(day, [])
        analyzed[day] = len(_ckpts(day))
        current = None
        selected = []
        if len(group) == 1:
            selected = [c for c in (group[0].get("candidates") or [])
                        if c.get("selection_status") == "selected"]
            current = {c.get("symbol") for c in selected if c.get("symbol")}
        extras = sum(c.get("stage") == "da_extra_watchlist" for c in selected)
        share = "%.1f%%" % (100.0 * extras / len(selected)) if selected else "n/a"
        if current is not None and previous is not None:
            inter = len(current & previous)
            union = len(current | previous)
            retention = inter / len(current) if current else None
            jaccard = inter / union if union else None
            pairs.append((day, retention, jaccard))
            retention_text = "%.2f" % retention if retention is not None else "n/a"
            jaccard_text = "%.2f" % jaccard if jaccard is not None else "n/a"
            pair_text = "%s | %s | %d/%d" % (
                retention_text, jaccard_text, inter, len(current))
        else:
            pair_text = "- | - | -"
        print("%s | %d | %s | %d | %s | %s" % (
            day, len(group), len(current) if current is not None else "unknown",
            analyzed[day], share, pair_text))
        previous = current
    if pairs:
        valid_retention = [p[1] for p in pairs if p[1] is not None]
        valid_jaccard = [p[2] for p in pairs if p[2] is not None]
        print("pairs=%d mean_retention=%s mean_jaccard=%s" % (
            len(pairs),
            "%.2f" % (sum(valid_retention) / len(valid_retention)) if valid_retention else "n/a",
            "%.2f" % (sum(valid_jaccard) / len(valid_jaccard)) if valid_jaccard else "n/a"))
    else:
        print("pairs=0; missing or ambiguous batch evidence is not treated as zero overlap")


def cmd_arbitration(args):
    """按写入尝试聚合仲裁；事件数不等于独立指令数或实际拦截率。"""
    path = os.path.join(REPO, "data", "shadow", "instruction_arbitration.jsonl")
    if not os.path.exists(path):
        print("no arbitration shadow yet: %s" % path)
        return
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    if args.date:
        rows = [r for r in rows if str(r.get("date")) == args.date]
    by_reason = collections.Counter(r.get("reason") for r in rows)
    by_day = collections.Counter(str(r.get("date")) for r in rows)
    kept_ids = {(r.get("date"), r.get("symbol"), (r.get("kept") or {}).get("id")
                 or (r.get("kept") or {}).get("did"))
                for r in rows if r.get("kept")}
    print("rows=%d unique_kept_instructions=%d reasons=%s" % (
        len(rows), len(kept_ids), dict(by_reason)))
    print("by_date=%s" % dict(sorted(by_day.items())))
    for r in rows[-10:]:
        attempted = r.get("attempted") or {}
        kept = r.get("kept") or {}
        print("  %s %-9s reason=%-20s outcome=%s attempted=%s/%s skip=%s kept=%s/%s id=%s" % (
            str(r.get("ts"))[:19], r.get("symbol"), r.get("reason"),
            r.get("write_outcome") or "legacy_unknown",
            attempted.get("direction"), attempted.get("confidence"),
            attempted.get("skip_reason") or "unknown",
            kept.get("direction"), kept.get("confidence"), kept.get("id")))


def _latency_summary(values):
    values = sorted(float(v) for v in values)
    if not values:
        return {"n": 0, "p50": None, "p90": None, "max": None}
    return {"n": len(values), "p50": round(values[len(values) // 2], 1),
            "p90": round(values[min(len(values) - 1, int(len(values) * 0.9))], 1),
            "max": round(values[-1], 1)}


def _log_health(path):
    if not path:
        return {"status": "unknown", "reason": "log_not_provided",
                "http_429": None, "timeout": None}
    log_path = Path(path)
    if not log_path.exists():
        return {"status": "unknown", "reason": "log_missing", "path": str(log_path),
                "http_429": None, "timeout": None}
    text = log_path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return {"status": "unknown", "reason": "log_empty", "path": str(log_path),
                "http_429": None, "timeout": None}
    lines = text.splitlines()
    return {
        "status": "ok", "path": str(log_path), "scope": "entire_explicit_log_file",
        "lines": len(lines),
        "http_429": sum("429" in line or "too many requests" in line.lower()
                        or "rate limit" in line.lower() for line in lines),
        "timeout": sum("timeout" in line.lower() or "timed out" in line.lower()
                       for line in lines),
    }


def _schedule_health(date, path):
    if not Path(path).exists():
        return {"status": "unknown", "terminal": None,
                "deadline_passed": None, "artifacts_ok": None}
    rows = [row for row in _jsonl(path) if str(row.get("date")) == date]
    terminals = [row for row in rows if row.get("status") != "heartbeat"]
    terminal = terminals[-1] if terminals else None
    if terminal is None:
        return {"status": "missing_terminal", "terminal": None,
                "deadline_passed": None, "artifacts_ok": None}
    status = str(terminal.get("status") or "unknown")
    finished_at = terminal.get("finished_at")
    deadline_passed = None
    if finished_at:
        try:
            finished = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=ET)
            deadline = datetime.fromisoformat(date + "T08:57:00").replace(tzinfo=ET)
            deadline_passed = finished.astimezone(ET) > deadline
        except ValueError:
            deadline_passed = None
    return {
        "status": status, "terminal": terminal, "finished_at": finished_at,
        "deadline_et": date + "T08:57:00", "deadline_passed": deadline_passed,
        "artifacts_ok": bool((terminal.get("artifacts") or {}).get("ok")),
    }


def _instruction_model_health(date, expected_model, db_path, repo):
    out = {"bridge_model": None, "bridge_candidates": None, "bridge_error": None,
           "instruction_models": {}, "instruction_count": 0,
           "instruction_error": None, "mismatches": []}
    bridge_path = Path(repo) / "data" / ("opportunity_scan_%s.json" % date)
    if not bridge_path.exists():
        out["bridge_error"] = "missing"
    else:
        try:
            bridge = _load(bridge_path)
            out["bridge_model"] = bridge.get("model_used")
            candidates = bridge.get("candidates")
            if not isinstance(candidates, list):
                out["bridge_error"] = "candidates_missing_or_invalid"
            else:
                out["bridge_candidates"] = len(candidates)
            if bridge.get("model_used") != expected_model:
                out["mismatches"].append("bridge:%s" % bridge.get("model_used"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            out["bridge_error"] = str(exc)[:200]
    if not db_path or not Path(db_path).exists():
        out["instruction_error"] = "db_missing"
    else:
        conn = None
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
            rows = conn.execute(
                "SELECT model_used,count(*) FROM playbook_instructions "
                "WHERE date=? AND decision_id LIKE 'DA-%' GROUP BY model_used",
                (date,)).fetchall()
            out["instruction_models"] = {str(model): count for model, count in rows}
            out["instruction_count"] = sum(count for _, count in rows)
            out["mismatches"].extend(
                "instruction:%s" % model for model, _ in rows if model != expected_model)
        except sqlite3.Error as exc:
            out["instruction_error"] = str(exc)
        finally:
            if conn is not None:
                conn.close()
    return out


def first_night_report(date, expected_model, thinking_budget=0, log_path=None,
                       ckpt_root=None, schedule_path=None, db_path=None, repo=None):
    ckpt_root = ckpt_root or CKPT
    schedule_path = schedule_path or SCHED
    db_path = db_path or DB
    repo = repo or REPO
    paths = [p for p in sorted(Path(ckpt_root, date).glob("US.*.json"))
             if not p.name.endswith(".raw.json")]
    report = {
        "schema": "da_first_night_health_v1", "date": date,
        "expected_model": expected_model, "checkpoints": len(paths),
        "model_counts": {}, "reported_model_counts": {},
        "model_mismatches": [], "malformed": [],
        "latency_ms": {}, "missing_latency": [], "prompt_tokens": {},
        "reasoning_tokens": {}, "missing_usage": [], "missing_prompt_tokens": [],
        "missing_reasoning_tokens": [], "missing_finish_reason": [], "missing_contract": [],
        "cap_check": "not_applicable" if thinking_budget == 0 else "pending",
        "cap_hits": [], "finish_not_stop": [], "main_degraded": [],
        "challenger_fallback": [], "challenger_primary_errors": [],
        "contract_repaired": [], "contract_unresolved": [],
        "log": _log_health(log_path), "schedule": _schedule_health(date, schedule_path),
    }
    latencies, prompt_tokens, reasoning_tokens = [], [], []
    for path in paths:
        symbol = path.name.removesuffix(".json")
        try:
            doc = _load(path)
            llm = doc.get("_llm_output") or {}
            if not llm:
                raise ValueError("missing _llm_output")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            report["malformed"].append({"symbol": symbol, "error": str(exc)[:200]})
            continue
        ckpt_model = doc.get("_ckpt_model")
        reported_model = llm.get("_model_reported")
        report["model_counts"][str(ckpt_model)] = report["model_counts"].get(str(ckpt_model), 0) + 1
        report["reported_model_counts"][str(reported_model)] = report["reported_model_counts"].get(str(reported_model), 0) + 1
        for label, value in (("checkpoint", ckpt_model), ("reported", reported_model)):
            if value != expected_model:
                report["model_mismatches"].append("%s:%s=%s" % (symbol, label, value))
        latency = llm.get("_llm_latency_ms")
        if isinstance(latency, (int, float)) and latency > 0:
            latencies.append(latency)
        else:
            report["missing_latency"].append(symbol)
        usage = llm.get("_usage") or {}
        if not usage:
            report["missing_usage"].append(symbol)
        prompt = usage.get("prompt_tokens")
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        if isinstance(prompt, (int, float)):
            prompt_tokens.append(prompt)
        else:
            report["missing_prompt_tokens"].append(symbol)
        if isinstance(reasoning, (int, float)):
            reasoning_tokens.append(reasoning)
            if thinking_budget > 0 and reasoning >= thinking_budget:
                report["cap_hits"].append(symbol)
        else:
            report["missing_reasoning_tokens"].append(symbol)
        if llm.get("_finish_reason") is None:
            report["missing_finish_reason"].append(symbol)
        elif llm.get("_finish_reason") != "stop":
            report["finish_not_stop"].append("%s:%s" % (symbol, llm.get("_finish_reason")))
        summary = str(llm.get("executive_summary") or "")
        risks = " ".join(str(v) for v in (llm.get("risks") or []))
        if llm.get("_error") or "LLM分析降级" in summary or "LLM分析不可用" in risks:
            report["main_degraded"].append(symbol)
        debate = llm.get("_debate") or {}
        if debate.get("challenger_fallback") or debate.get("challenger_tier") not in (None, "primary"):
            report["challenger_fallback"].append("%s:%s" % (symbol, debate.get("challenger_tier")))
        if debate.get("challenger_primary_error"):
            report["challenger_primary_errors"].append(symbol)
        contract = llm.get("_contract") or {}
        if not contract:
            report["missing_contract"].append(symbol)
        unresolved = contract.get("violations_after") if "violations_after" in contract else contract.get("violations")
        if unresolved:
            report["contract_unresolved"].append({"symbol": symbol, "violations": unresolved})
        elif (contract.get("repair") or {}).get("applied"):
            report["contract_repaired"].append(symbol)
    report["latency_ms"] = _latency_summary(latencies)
    report["prompt_tokens"] = _latency_summary(prompt_tokens)
    report["reasoning_tokens"] = _latency_summary(reasoning_tokens)
    if thinking_budget > 0:
        if report["missing_reasoning_tokens"] or not reasoning_tokens:
            report["cap_check"] = "unknown"
        else:
            report["cap_check"] = "hit" if report["cap_hits"] else "no_hit"
    report["downstream_models"] = _instruction_model_health(date, expected_model, db_path, repo)
    terminal = report["schedule"].get("terminal") or {}
    observation = terminal.get("batch_observation") or {}
    expected_analyses = terminal.get("analyses_count")
    if expected_analyses is None:
        expected_analyses = observation.get("analysed_count", observation.get("selected_count"))
    report["coverage"] = {
        "checkpoint_count": len(paths), "expected_analyses": expected_analyses,
        "batch_observation_state": observation.get("state"),
        "missing_symbols": observation.get("missing_symbols") or [],
        "unreadable_checkpoints": observation.get("unreadable_checkpoints") or [],
        "unexpected_symbols": observation.get("unexpected_symbols") or [],
        "gate_unknown_symbols": observation.get("gate_unknown_symbols") or [],
    }

    failures = []
    warnings = []
    if not paths:
        failures.append("no_checkpoints")
    for key, label in (("malformed", "malformed_checkpoints"),
                       ("model_mismatches", "model_mismatch"),
                       ("main_degraded", "main_analysis_degraded"),
                       ("finish_not_stop", "finish_not_stop"),
                       ("contract_unresolved", "unresolved_contract")):
        if report[key]:
            failures.append("%s:%d" % (label, len(report[key])))
    failures.extend("downstream_model_mismatch:%s" % value
                    for value in report["downstream_models"]["mismatches"])
    schedule = report["schedule"]
    if schedule["status"] != "complete":
        failures.append("terminal_status:%s" % schedule["status"])
    if schedule["artifacts_ok"] is not True:
        failures.append("artifacts_not_ok")
    if schedule["deadline_passed"] is not False:
        failures.append("deadline_unknown" if schedule["deadline_passed"] is None else "deadline_missed")
    if schedule["status"] == "complete":
        if not observation:
            failures.append("batch_observation_missing")
        else:
            if observation.get("state") != "complete":
                failures.append("batch_observation:%s" % observation.get("state"))
            for key in ("missing_symbols", "unreadable_checkpoints", "unexpected_symbols", "gate_unknown_symbols"):
                if report["coverage"][key]:
                    failures.append("%s:%d" % (key, len(report["coverage"][key])))
        if expected_analyses is None:
            failures.append("expected_analyses_unknown")
        elif int(expected_analyses) != len(paths):
            failures.append("analysis_count_mismatch:%s!=%d" % (expected_analyses, len(paths)))
    downstream = report["downstream_models"]
    if downstream["bridge_error"]:
        failures.append("bridge_error:%s" % downstream["bridge_error"])
    if downstream["instruction_error"]:
        failures.append("instruction_error:%s" % downstream["instruction_error"])
    if (downstream["bridge_candidates"] is not None
            and downstream["instruction_count"] != downstream["bridge_candidates"]):
        failures.append("instruction_count_mismatch:%d!=%d" % (
            downstream["instruction_count"], downstream["bridge_candidates"]))
    for key, label in (("missing_latency", "missing_latency"),
                       ("missing_usage", "missing_usage"),
                       ("missing_prompt_tokens", "missing_prompt_tokens"),
                       ("missing_reasoning_tokens", "missing_reasoning_tokens"),
                       ("missing_finish_reason", "missing_finish_reason"),
                       ("missing_contract", "missing_contract"),
                       ("challenger_fallback", "challenger_fallback"),
                       ("challenger_primary_errors", "challenger_primary_error")):
        if report[key]:
            warnings.append("%s:%d" % (label, len(report[key])))
    if report["cap_hits"]:
        warnings.append("reasoning_cap_hits:%d" % len(report["cap_hits"]))
    if report["log"]["status"] != "ok":
        warnings.append("log_unknown")
    else:
        if report["log"]["http_429"]:
            warnings.append("http_429:%d" % report["log"]["http_429"])
        if report["log"]["timeout"]:
            warnings.append("timeout:%d" % report["log"]["timeout"])
    report["failures"] = failures
    report["warnings"] = warnings
    report["status"] = "FAIL" if failures else ("WARN" if warnings else "PASS")
    report["quality_or_edge_proven"] = False
    return report


def cmd_first_night(args):
    report = first_night_report(
        args.date, args.expected_model, thinking_budget=args.thinking_budget,
        log_path=args.log, ckpt_root=args.checkpoint_root,
        schedule_path=args.schedule, db_path=args.db)
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out:
        with Path(args.out).open("x", encoding="utf-8") as handle:
            handle.write(payload)
    print(payload, end="")
    return 1 if report["status"] == "FAIL" else 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("funnel")
    p.add_argument("--date", default="")
    p.add_argument("--batch-id", default="")
    p = sub.add_parser("debate")
    p.add_argument("--date", default="")
    p.add_argument("--since", default="2026-10-01")
    for name in ("trend", "capacity", "confgate"):
        p = sub.add_parser(name)
        p.add_argument("--date", required=True)
        if name == "confgate":
            p.add_argument("--log", required=True)
    p = sub.add_parser("overlap")
    p.add_argument("--since", default="")
    p.add_argument("--date", default="", help="截止日（含），与--since共同限定窗口")
    p = sub.add_parser("arbitration")
    p.add_argument("--date", default="")
    p = sub.add_parser("first-night")
    p.add_argument("--date", required=True)
    p.add_argument("--expected-model", required=True)
    p.add_argument("--thinking-budget", type=int, default=0,
                   help="0=无帽，reasoning 撞顶检查记 not_applicable")
    p.add_argument("--log", default=PIPELINE_LOG,
                   help="按整个显式日志文件统计；缺文件/空文件记 unknown")
    p.add_argument("--checkpoint-root", default=CKPT)
    p.add_argument("--schedule", default=SCHED)
    p.add_argument("--db", default=DB)
    p.add_argument("--out", default="", help="可选新 JSON 路径；已存在则拒绝覆盖")
    args = ap.parse_args()
    if args.cmd == "first-night" and args.thinking_budget < 0:
        raise SystemExit("thinking-budget 必须 >=0")
    return {"funnel": cmd_funnel, "debate": cmd_debate, "trend": cmd_trend,
            "capacity": cmd_capacity, "confgate": cmd_confgate,
            "overlap": cmd_overlap, "arbitration": cmd_arbitration,
            "first-night": cmd_first_night}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main() or 0)