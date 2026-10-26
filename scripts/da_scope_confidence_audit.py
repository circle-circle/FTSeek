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


def cmd_funnel(args):
    rows = [json.loads(l) for l in open(FUNNEL, encoding="utf-8") if l.strip()]
    batch = rows[-1]
    cands = batch.get("candidates") or []
    print("batch=%s phase=%s policy=%s n=%d" % (
        batch.get("batch_id"), batch.get("phase"), batch.get("policy_version"), len(cands)))
    print("stage=%s" % dict(collections.Counter(c.get("stage") for c in cands)))
    print("eligibility=%s" % dict(collections.Counter(c.get("status") for c in cands)))
    print("selection=%s" % dict(collections.Counter(c.get("selection_status") for c in cands)))
    print("reasons=%s" % dict(collections.Counter(
        x for c in cands for x in (c.get("reasons") or []))))
    print("stage_x_selection=%s" % dict(collections.Counter(
        (c.get("stage"), c.get("selection_status")) for c in cands)))


def cmd_debate(args):
    print("date | n | states | agreed=True | DB conf (model_used!=deterministic)")
    db = _db()
    for day in sorted(os.listdir(CKPT)):
        if day < args.since or not day.startswith("2026-"):
            continue
        files = _ckpts(day)
        if not files:
            continue
        states = collections.Counter()
        agreed = 0
        for p in files:
            doc = _load(p)
            out = doc.get("_llm_output") or {}
            deb = out.get("_debate") or {}
            if deb.get("challenger_agreed"):
                agreed += 1
            try:
                states[_classify(out.get("direction"), deb.get("challenger_direction"),
                                 deb.get("challenger_verdict"))] += 1
            except Exception as exc:  # 生产实现不可导入时不编造
                states["CLASSIFY_ERR:%s" % type(exc).__name__] += 1
        conf = collections.Counter(r[0] for r in db.execute(
            "select confidence from playbook_instructions "
            "where date=? and model_used!='deterministic'", (day,)))
        print("%s | %d | %s | %d | %s" % (day, len(files), dict(states), agreed, dict(conf)))


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
    rows = {r[0]: (r[1], r[2], r[3]) for r in db.execute(
        "select symbol, direction, confidence, coalesce(decision_id,'') "
        "from playbook_instructions where date=? and model_used!='deterministic'",
        (args.date,))}
    skipped, refused = {}, {}
    with open(args.log, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "门槛=" in line and " → SKIP" in line:
                sym = line.split("[playbook] ")[-1].split(" ")[0]
                skipped[sym] = line.split("[playbook] ")[-1].strip()
            elif "覆盖仲裁拒绝" in line:
                sym = line.split("覆盖仲裁拒绝: ")[-1].split(" ")[0]
                refused[sym] = True
    print("db_da_rows=%d  conf_gate_skip=%d  arbitration_refused=%d" % (
        len(rows), len(skipped), len(refused)))
    for sym, info in sorted(rows.items()):
        print("  %-9s db_dir=%-10s conf=%-3s did=%s  gate_skip=%s  refused=%s" % (
            sym, info[0], info[1], info[2][:28], "Y" if sym in skipped else "-",
            "Y" if sym in refused else "-"))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("funnel")
    for name in ("debate", "trend", "capacity", "confgate"):
        p = sub.add_parser(name)
        p.add_argument("--date", default="")
        p.add_argument("--since", default="2026-10-01")
        p.add_argument("--log", default="")
    args = ap.parse_args()
    return {"funnel": cmd_funnel, "debate": cmd_debate, "trend": cmd_trend,
            "capacity": cmd_capacity, "confgate": cmd_confgate}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main() or 0)