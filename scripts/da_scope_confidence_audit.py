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

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(REPO, "data", "ftseek_us.db")
FUNNEL = os.path.join(REPO, "data", "shadow", "candidate_funnel.jsonl")
SCHED = os.path.join(REPO, "data", "shadow", "da_main_schedule.jsonl")
CKPT = os.path.join(REPO, "data", "da_checkpoint")
DEBATE_STATE = "/root/.hermes/skills/deep-stock-analysis/scripts"


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
        filled = db.execute("select count(*) from trade_records where decision_id=?",
                            (row["decision_id"],)).fetchone()[0]
        print("  id=%d %-9s published=%s current=%s conf=%s did=%s "
              "gate_events=%s refused_events=%s log_join=%s filled=%d "
              "executed=%s execution_state=%s skip_reason=%s" % (
                  row["id"], row["symbol"], raw.get("direction") or "unknown",
                  row["direction"], row["confidence"], row["decision_id"],
                  conf_events.count(row["symbol"]) if joinable else "ambiguous",
                  refused_events.count(row["symbol"]) if joinable else "ambiguous",
                  "symbol_unique" if joinable else "ambiguous", filled, row["executed"],
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
    args = ap.parse_args()
    return {"funnel": cmd_funnel, "debate": cmd_debate, "trend": cmd_trend,
            "capacity": cmd_capacity, "confgate": cmd_confgate,
            "overlap": cmd_overlap, "arbitration": cmd_arbitration}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main() or 0)