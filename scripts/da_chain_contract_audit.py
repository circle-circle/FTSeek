#!/usr/bin/env python3
"""DA 链契约只读审计 (2026-10-23).

对应 `docs/specs/da-execution-contract-20261023.md` 的 P1 证据面:
  - 价格基准: snapshot.prev_close 与上一已完成日 K 线收盘是否一致;
  - 来源时效/重复: 新闻/研报/社区的发布时间、渲染时间、跨栏目同稿;
  - 裁决状态: 分析师/挑战者方向、verdict、agreed 的规范分类;
  - 阶段参数: 计划 / 提交 / 成交三阶段的 entry、SL、TP、RR;
  - 记忆口径: strategy_memory 的展示分组是否等于真实 30 日窗口、毛额是否被当净额。

只读约束:
  - 所有 SQLite 连接使用 `file:...?mode=ro`; 不写任何表;
  - 默认只打印, 不写文件; `--out` 才显式写 JSON 报告;
  - 不 import 生产模块 (避免 db_utils 的全局 connect hook 副作用);
  - 不调用模型/券商/通知.

用法:
  PYTHONPATH=. ./venv/bin/python scripts/da_chain_contract_audit.py --date 2026-10-22
  PYTHONPATH=. ./venv/bin/python scripts/da_chain_contract_audit.py --date 2026-10-22 --json
  PYTHONPATH=. ./venv/bin/python scripts/da_chain_contract_audit.py --date 2026-10-22 --out /tmp/da_audit.json
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CKPT_ROOT = ROOT / "data" / "da_checkpoint"
DEFAULT_DB = ROOT / "data" / "ftseek_us.db"
DEFAULT_MEMORY_DB = ROOT / "data" / "strategy_memory.db"
CURRENT_ERA_START = "2026-07-01"

VALID_DIRS = ("BUY", "SELL_SHORT", "SKIP")
VALID_VERDICTS = ("AGREE", "PARTIAL_AGREE", "DISAGREE")

# 与 strategy_memory._strength_bucket 同口径; 这里是审计副本, 不 import 生产模块.
_BUCKETS = ((80, "80+"), (70, "70-80"), (60, "60-70"), (50, "50-60"))


def _f(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _norm_dir(v: Any) -> str:
    d = str(v or "").strip().upper()
    return {"LONG": "BUY", "SHORT": "SELL_SHORT", "SELL": "SELL_SHORT"}.get(d, d)


def _bucket(strength: Any) -> str:
    try:
        s = int(float(strength))
    except (TypeError, ValueError):
        return "unknown"
    for floor, label in _BUCKETS:
        if s >= floor:
            return label
    return "<50"


def compute_rr(direction: Any, entry: Any, sl: Any, tp: Any) -> Optional[float]:
    """方向感知 RR; 几何无效/缺价返回 None. BUY=(tp-entry)/(entry-sl), SHORT=(entry-tp)/(sl-entry)."""
    d = _norm_dir(direction)
    e, s, t = _f(entry), _f(sl), _f(tp)
    if d not in ("BUY", "SELL_SHORT") or None in (e, s, t):
        return None
    if d == "BUY":
        risk, reward = e - s, t - e
    else:
        risk, reward = s - e, e - t
    if risk <= 0 or reward <= 0:
        return None
    return round(reward / risk, 6)


def classify_debate(
    analyst_direction: Any,
    challenger_direction: Any,
    verdict: Any,
    challenger_available: bool = True,
) -> Dict[str, Any]:
    """契约 §6.1 的文档级状态机; 按优先级首个匹配. 仅审计分类, 不改任何分级."""
    a = _norm_dir(analyst_direction)
    c = _norm_dir(challenger_direction)
    v = str(verdict or "").strip().upper()
    if a == "SKIP":
        return {"state": "ANALYST_SKIP", "priority": 1,
                "reason": "analyst_skip_preserved"}
    if not challenger_available:
        return {"state": "UNAVAILABLE", "priority": 2,
                "reason": "challenger_unavailable"}
    if a not in VALID_DIRS or c not in VALID_DIRS or v not in VALID_VERDICTS:
        return {"state": "CONFLICT", "priority": 3,
                "reason": "invalid_direction_or_verdict"}
    if c == "SKIP":
        return {"state": "ABSTAIN", "priority": 4,
                "reason": "challenger_abstained"}
    if a == c and v == "AGREE":
        return {"state": "AGREE", "priority": 5, "reason": "same_direction_agree"}
    if a == c and v == "PARTIAL_AGREE":
        return {"state": "PARTIAL_AGREE", "priority": 6,
                "reason": "same_direction_partial"}
    if a != c and v == "DISAGREE":
        return {"state": "DISAGREE", "priority": 7,
                "reason": "opposite_direction_disagree"}
    return {"state": "CONFLICT", "priority": 8,
            "reason": "verdict_direction_mismatch"}


def _bar_date(bar: Dict[str, Any]) -> str:
    return str(bar.get("time") or bar.get("date") or "")[:10]


def classify_price_basis(
    snapshot: Dict[str, Any],
    kline: List[Dict[str, Any]],
    trade_date: str,
    tolerance_pct: float = 0.10,
    rate_tolerance_pct: float = 0.20,
) -> Dict[str, Any]:
    """比较 prev_close 与上一已完成日 K 线收盘.

    - 只在 premarket/regular 检查 (afterhours 的 prev_close 语义随源变化);
    - 选取 trade_date 之前最后一个日 K;
    - 不做任何修正/覆盖, 只分类与量化偏差.
    """
    session = str(snapshot.get("session") or "").lower()
    if not snapshot or snapshot.get("_error"):
        return {"status": "snapshot_error", "session": session,
                "reason": str(snapshot.get("_error") or "snapshot_missing")[:200]}
    if session not in ("premarket", "regular"):
        return {"status": "not_checked", "reason": "session_not_premarket_or_regular",
                "session": session}

    bars = []
    for b in kline or []:
        bd = _bar_date(b)
        if bd and bd < trade_date and _f(b.get("close")) is not None:
            bars.append((bd, b))
    if not bars:
        return {"status": "no_daily_bar", "reason": "no_completed_bar_before_trade_date",
                "session": session}
    bar_date, bar = max(bars, key=lambda x: x[0])
    bar_close = _f(bar.get("close"))
    prev = _f(snapshot.get("prev_close_price"))
    if prev is None:
        prev = _f(snapshot.get("prev_close"))
    if prev is None or bar_close is None or bar_close <= 0:
        return {"status": "missing_price", "reason": "prev_close_or_bar_close_missing",
                "bar_date": bar_date, "session": session}

    mismatch_pct = (prev - bar_close) / bar_close * 100.0
    stale_days = None
    try:
        stale_days = (date.fromisoformat(trade_date) - date.fromisoformat(bar_date)).days
    except ValueError:
        pass

    result: Dict[str, Any] = {
        "session": session,
        "bar_date": bar_date,
        "bar_close": round(bar_close, 4),
        "prev_close": round(prev, 4),
        "mismatch_pct": round(mismatch_pct, 4),
        "bar_gap_days": stale_days,
        "status": "ok" if abs(mismatch_pct) <= tolerance_pct else "prev_close_mismatch",
    }

    pre = _f(snapshot.get("pre_price"))
    rate = _f(snapshot.get("pre_change_rate"))
    if session == "premarket" and pre and pre > 0 and rate is not None:
        implied = pre / (1.0 + rate / 100.0) if abs(rate) > 1e-12 else None
        result["pre_price"] = round(pre, 4)
        result["pre_change_rate"] = rate
        if implied and implied > 0:
            implied_mismatch = (implied - bar_close) / bar_close * 100.0
            result["implied_prev_close"] = round(implied, 4)
            result["implied_vs_bar_pct"] = round(implied_mismatch, 4)
            if abs(implied_mismatch) > rate_tolerance_pct:
                result["pre_rate_mismatch"] = True
                if result["status"] == "ok":
                    result["status"] = "pre_rate_mismatch"
    return result


def _epoch_seconds(v: Any) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f > 1e12:  # 毫秒时间戳
        f /= 1000.0
    return f if f > 0 else None


def _pub_date(v: Any, trade_date: str) -> Optional[date]:
    sec = _epoch_seconds(v)
    if sec is not None:
        return datetime.fromtimestamp(sec, timezone.utc).date()
    s = str(v or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m-%d", "%Y/%m/%d"):
        try:
            d = datetime.strptime(s[:10] if fmt != "%m-%d" else s[:5], fmt).date()
            if fmt == "%m-%d":
                d = d.replace(year=int(trade_date[:4]))
                if d > date.fromisoformat(trade_date):
                    d = d.replace(year=d.year - 1)
            return d
        except ValueError:
            continue
    return None


def _dedupe_key(item: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    nid = item.get("news_id") or item.get("id")
    if nid not in (None, ""):
        return ("id", str(nid))
    url = str(item.get("url") or "").strip()
    if url:
        parsed = urlparse(url)
        # Finnhub 新闻把真实 id 放在 query; 只按 host/path 归一会把所有条目误并成一条.
        qid = parse_qs(parsed.query).get("id")
        if qid:
            return ("url_id", str(qid[0]))
        clean = parsed._replace(query="", fragment="").geturl().rstrip("/")
        return ("url", clean)
    title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip().lower()
    return ("title", title) if title else None


def source_stats(collected: Dict[str, Any], trade_date: str) -> Dict[str, Any]:
    """新闻/研报/社区的时间与同稿统计. 只描述, 不修改 prompt 输入."""
    section_rows = {
        "news": collected.get("news") or [],
        "news_v2": collected.get("news_v2") or [],
        "research": collected.get("research") or [],
    }
    items: List[Dict[str, Any]] = []
    for section, rows in section_rows.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            pub = row.get("publish_time")
            items.append({
                "section": section,
                "news_id": row.get("news_id") or row.get("id"),
                "url": row.get("url"),
                "title": row.get("title"),
                "publish_time": pub,
                "time": row.get("time"),
            })

    keys: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for item in items:
        key = _dedupe_key(item)
        if key:
            keys[key].append(item)
    dup_groups = {k: v for k, v in keys.items() if len(v) > 1}
    dup_items = sum(len(v) for v in dup_groups.values())
    dup_extra_items = sum(len(v) - 1 for v in dup_groups.values())
    cross_groups = {k: v for k, v in dup_groups.items()
                    if len({i["section"] for i in v}) > 1}
    cross_extra_items = sum(len(v) - 1 for v in cross_groups.values())

    pub_missing = Counter()
    stale = Counter()
    render_missing = Counter()
    for item in items:
        section = item["section"]
        pub = item.get("publish_time")
        d = _pub_date(pub, trade_date)
        if d is None:
            pub_missing[section] += 1
        elif (date.fromisoformat(trade_date) - d).days > 7:
            stale[section] += 1
        # 2026-10-23: research now renders publish_time (LLM Prompt fix); a
        # timestamp in either field means the model can see an age. Only a
        # record with neither field is a real render-time gap.
        if section == "research" and not (item.get("time") or item.get("publish_time")):
            render_missing[section] += 1

    sentiment = collected.get("sentiment") or []
    sent = [s for s in sentiment if isinstance(s, dict)]
    sent_pub = [_pub_date(s.get("publish_time"), trade_date) for s in sent]
    sent_dates = [d for d in sent_pub if d is not None]
    latest_age = None
    if sent_dates:
        latest_age = (date.fromisoformat(trade_date) - max(sent_dates)).days

    return {
        "items": len(items),
        "by_section": {k: len(v) for k, v in section_rows.items() if isinstance(v, list)},
        "publish_time_missing": dict(pub_missing),
        "render_time_missing": dict(render_missing),
        "stale_over_7d": dict(stale),
        "duplicate_groups": len(dup_groups),
        "duplicate_items": dup_items,
        "duplicate_extra_items": dup_extra_items,
        "cross_section_duplicate_groups": len(cross_groups),
        "cross_section_extra_items": cross_extra_items,
        "sentiment": {
            "n": len(sent),
            "publish_time_missing": len(sent) - len(sent_dates),
            "latest_age_days": latest_age,
        },
        "duplicate_examples": [
            {"key": list(k), "sections": sorted({i["section"] for i in v}),
             "n": len(v), "title": str(v[0].get("title") or "")[:80]}
            for k, v in list(dup_groups.items())[:5]
        ],
    }


def aggregate_source_stats(per_symbol: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "symbols": len(per_symbol),
        "items": 0,
        "publish_time_missing": Counter(),
        "render_time_missing": Counter(),
        "stale_over_7d": Counter(),
        "duplicate_groups": 0,
        "duplicate_items": 0,
        "duplicate_extra_items": 0,
        "cross_section_duplicate_groups": 0,
        "cross_section_extra_items": 0,
        "sentiment_n": 0,
        "sentiment_publish_missing": 0,
    }
    for stats in per_symbol.values():
        out["items"] += stats.get("items", 0)
        for k, v in (stats.get("publish_time_missing") or {}).items():
            out["publish_time_missing"][k] += v
        for k, v in (stats.get("render_time_missing") or {}).items():
            out["render_time_missing"][k] += v
        for k, v in (stats.get("stale_over_7d") or {}).items():
            out["stale_over_7d"][k] += v
        out["duplicate_groups"] += stats.get("duplicate_groups", 0)
        out["duplicate_items"] += stats.get("duplicate_items", 0)
        out["duplicate_extra_items"] += stats.get("duplicate_extra_items", 0)
        out["cross_section_duplicate_groups"] += stats.get("cross_section_duplicate_groups", 0)
        out["cross_section_extra_items"] += stats.get("cross_section_extra_items", 0)
        s = stats.get("sentiment") or {}
        out["sentiment_n"] += s.get("n", 0)
        out["sentiment_publish_missing"] += s.get("publish_time_missing", 0)
    for key in ("publish_time_missing", "render_time_missing", "stale_over_7d"):
        out[key] = dict(out[key])
    return out


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def audit_checkpoints(ckpt_dir: Path, trade_date: str) -> Dict[str, Any]:
    raw_files = sorted(ckpt_dir.glob("*.raw.json"))
    analysis_files = sorted(p for p in ckpt_dir.glob("*.json") if not p.name.endswith(".raw.json"))

    price: Dict[str, Dict[str, Any]] = {}
    sources: Dict[str, Dict[str, Any]] = {}
    for raw_path in raw_files:
        raw = _read_json(raw_path) or {}
        collected = raw.get("collected") or {}
        symbol = str(raw.get("symbol") or raw_path.name.replace(".raw.json", ""))
        snap = collected.get("snapshot") or {}
        kline = collected.get("kline") or []
        price[symbol] = classify_price_basis(snap, kline, trade_date)
        sources[symbol] = source_stats(collected, trade_date)

    debates: Dict[str, Dict[str, Any]] = {}
    analyses: Dict[str, Dict[str, Any]] = {}
    for path in analysis_files:
        obj = _read_json(path) or {}
        symbol = str(obj.get("symbol") or path.name.replace(".json", ""))
        llm = obj.get("_llm_output") or {}
        debate = llm.get("_debate") or {}
        if llm:
            analyses[symbol] = {
                "decision_id": obj.get("_ic_decision_id") or llm.get("_decision_id"),
                "direction": llm.get("direction"),
                "score": llm.get("score"),
                "plan": llm.get("execution_plan") or {},
                "gate": obj.get("_signal_gate") or {},
            }
        if llm:
            available = bool(debate) and bool(debate.get("challenger_direction"))
            cls = classify_debate(
                llm.get("direction"),
                debate.get("challenger_direction"),
                debate.get("challenger_verdict"),
                available,
            )
            # 2026-10-23 起 llm_synth 落 challenger_verdict_state; 有则优先读,
            # 同时保留本地复算作交叉核验 (旧 checkpoint 无此字段).
            _recorded_state = str(debate.get("challenger_verdict_state") or "").upper()
            if _recorded_state:
                cls["state_recorded"] = _recorded_state
                cls["state_match"] = (_recorded_state == cls["state"])
            cls.update({
                "symbol": symbol,
                "challenger_available": available,
                "challenger_agreed": debate.get("challenger_agreed"),
                "challenger_agreed_raw": debate.get("challenger_agreed_raw",
                                                     debate.get("challenger_agreed")),
                "challenger_verdict": debate.get("challenger_verdict"),
                "challenger_direction": debate.get("challenger_direction"),
                "challenger_self_debate": bool(debate.get("challenger_self_debate")),
                "debate_outcome": llm.get("debate_outcome"),
                "raw_consistent": not (
                    str(debate.get("challenger_verdict") or "").upper() == "PARTIAL_AGREE"
                    and debate.get("challenger_agreed") is True
                ),
            })
            debates[symbol] = cls

    state_counts = Counter(v["state"] for v in debates.values())
    partial_true = [v for v in debates.values()
                    if v["state"] == "PARTIAL_AGREE"
                    and v.get("challenger_agreed_raw") is True]
    return {
        "raw_files": len(raw_files),
        "analysis_files": len(analysis_files),
        "price": price,
        "sources": sources,
        "source_aggregate": aggregate_source_stats(sources),
        "debates": debates,
        "debate_states": dict(state_counts),
        "partial_agreed_true": partial_true,
        "analyses": analyses,
    }


def _db_ro(path: Path) -> Optional[sqlite3.Connection]:
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def stage_audit(
    analyses: Dict[str, Dict[str, Any]],
    db_path: Path,
    trade_date: str,
) -> Dict[str, Any]:
    conn = _db_ro(db_path)
    if conn is None:
        return {"status": "db_missing_or_unreadable", "db": str(db_path)}
    try:
        inst_rows = [dict(r) for r in conn.execute(
            "SELECT id, date, symbol, direction, entry_price, stop_loss, take_profit, "
            "risk_reward, decision_id, execution_state, execution_price "
            "FROM playbook_instructions WHERE date=? AND direction IN ('BUY','SELL_SHORT')",
            (trade_date,),
        )]
        by_decision = {r["decision_id"]: r for r in inst_rows if r.get("decision_id")}
        by_id = {r["id"]: r for r in inst_rows}

        trade_rows = []
        for r in conn.execute(
            "SELECT id, trade_date, symbol, direction, entry_price, quantity, "
            "realized_pnl, pnl_net, metadata_json, decision_id "
            "FROM trade_records WHERE trade_date=?",
            (trade_date,),
        ):
            row = dict(r)
            try:
                row["_meta"] = json.loads(row.get("metadata_json") or "{}")
            except Exception:
                row["_meta"] = {}
            trade_rows.append(row)
        by_inst = {}
        for tr in trade_rows:
            iid = (tr["_meta"] or {}).get("playbook_instruction_id")
            if iid is not None:
                by_inst[int(iid)] = tr
    finally:
        conn.close()

    rows = []
    for symbol, a in analyses.items():
        plan = a.get("plan") or {}
        inst = by_decision.get(a.get("decision_id"))
        if not inst:
            # 兜底: 同日唯一同标的指令; 多指令时保持 ambiguous
            cands = [r for r in inst_rows if r["symbol"] == symbol]
            inst = cands[0] if len(cands) == 1 else None
        entry = _f(plan.get("entry_price"))
        sl = _f(plan.get("stop_loss_price"))
        tp = _f(plan.get("take_profit_price"))
        plan_rr = compute_rr(a.get("direction"), entry, sl, tp)
        row: Dict[str, Any] = {
            "symbol": symbol,
            "decision_id": a.get("decision_id"),
            "analyst_direction": _norm_dir(a.get("direction")),
            "instruction_id": inst.get("id") if inst else None,
            "match": "decision_id" if inst and inst.get("decision_id") == a.get("decision_id")
                     else ("ambiguous" if inst else "no_instruction"),
            "plan": {"entry": entry, "sl": sl, "tp": tp, "rr": plan_rr},
            "submitted": None,
            "filled": None,
        }
        if inst:
            sub = {
                "entry": _f(inst.get("entry_price")),
                "sl": _f(inst.get("stop_loss")),
                "tp": _f(inst.get("take_profit")),
                "rr_db": _f(inst.get("risk_reward")),
                "state": inst.get("execution_state"),
            }
            sub["rr"] = compute_rr(inst.get("direction"), sub["entry"], sub["sl"], sub["tp"])
            row["submitted"] = sub
            tr = by_inst.get(int(inst["id"]))
            if tr:
                meta = tr.get("_meta") or {}
                tp_gate = meta.get("_tp_rr_gate") or {}
                f_entry = _f(tr.get("entry_price"))
                f_sl = _f(tp_gate.get("initial_sl")) or _f(meta.get("stop_loss"))
                f_tp = _f(tp_gate.get("tp")) or _f(meta.get("take_profit"))
                row["filled"] = {
                    "trade_id": tr.get("id"),
                    "entry": f_entry,
                    "sl": f_sl,
                    "tp": f_tp,
                    "rr": compute_rr(tr.get("direction"), f_entry, f_sl, f_tp),
                    "tp_swallowed": bool(tp_gate.get("swallowed")),
                    "tp_rr_at_execution": _f(tp_gate.get("rr_at_execution")),
                    "qty": _f(tr.get("quantity")),
                }
                if f_entry and sub.get("entry"):
                    row["entry_gap_pct"] = round((sub["entry"] - f_entry) / f_entry * 100.0, 4)
        rows.append(row)

    filled = [r for r in rows if r.get("filled")]
    gaps = [abs(r["entry_gap_pct"]) for r in filled if r.get("entry_gap_pct") is not None]
    rr_mismatch = [
        r for r in rows
        if (r.get("plan") or {}).get("rr") is not None
        and (r.get("filled") or {}).get("rr") is not None
        and abs(r["plan"]["rr"] - r["filled"]["rr"]) > 0.05
    ]
    db_rr_mismatch = [
        r for r in rows
        if (r.get("submitted") or {}).get("rr_db") is not None
        and (r.get("submitted") or {}).get("rr") is not None
        and abs(r["submitted"]["rr_db"] - r["submitted"]["rr"]) > 0.05
    ]
    actionable = [r for r in rows if r["analyst_direction"] in ("BUY", "SELL_SHORT")]
    return {
        "status": "ok",
        "instructions": len(inst_rows),
        "matched": sum(1 for r in rows if r["match"] == "decision_id"),
        "ambiguous": sum(1 for r in rows if r["match"] == "ambiguous"),
        "no_instruction": sum(1 for r in rows if r["match"] == "no_instruction"),
        "actionable_analyses": len(actionable),
        "actionable_without_instruction": sum(
            1 for r in actionable if r["match"] == "no_instruction"),
        "filled": len(filled),
        "entry_gap_gt_1pct": sum(1 for g in gaps if g > 1.0),
        "entry_gap_median_pct": round(sorted(gaps)[len(gaps) // 2], 4) if gaps else None,
        "plan_vs_filled_rr_mismatch_gt_0_05": len(rr_mismatch),
        "db_rr_vs_geometry_mismatch_gt_0_05": len(db_rr_mismatch),
        "tp_swallowed": sum(1 for r in filled if r["filled"].get("tp_swallowed")),
        "rows": rows,
    }


def compare_memory_patterns(
    pattern_rows: Iterable[Dict[str, Any]],
    trade_rows: Iterable[Dict[str, Any]],
    cutoff: str,
    era_start: str = CURRENT_ERA_START,
) -> Dict[str, Any]:
    """比较 strategy_memory 展示分组与真实滚动窗口/净额.

    pattern_rows: strategy_memory.pattern_memory 行 (total_trades/total_pnl/last_trade_date).
    trade_rows: trade_records 行 (trade_date/direction/exit_reason/signal_strength/realized_pnl/pnl_net).
    """
    all_pat: Dict[Tuple[str, str, str], Dict[str, float]] = defaultdict(
        lambda: {"n": 0, "gross": 0.0, "net": 0.0})
    win_pat: Dict[Tuple[str, str, str], Dict[str, float]] = defaultdict(
        lambda: {"n": 0, "gross": 0.0, "net": 0.0})
    old_rows = 0
    net_missing = 0
    for t in trade_rows:
        td = str(t.get("trade_date") or "")
        key = (_norm_dir(t.get("direction")), str(t.get("exit_reason") or "unknown"),
               _bucket(t.get("signal_strength")))
        gross = _f(t.get("realized_pnl")) or 0.0
        net = _f(t.get("pnl_net"))
        if net is None:
            net_missing += 1
            net = gross
        for bucket in (all_pat,):
            bucket[key]["n"] += 1
            bucket[key]["gross"] += gross
            bucket[key]["net"] += net
        if td >= cutoff:
            win_pat[key]["n"] += 1
            win_pat[key]["gross"] += gross
            win_pat[key]["net"] += net
        if td and td < era_start:
            old_rows += 1

    displayed = []
    for p in pattern_rows:
        key = (_norm_dir(p.get("direction")), str(p.get("exit_reason") or "unknown"),
               str(p.get("strength_bucket") or "unknown"))
        if int(p.get("total_trades") or 0) < 2:
            continue
        if str(p.get("last_trade_date") or "") < cutoff:
            continue
        displayed.append({
            "key": key,
            "displayed_n": int(p.get("total_trades") or 0),
            "displayed_pnl": _f(p.get("total_pnl")) or 0.0,
            "last_trade_date": p.get("last_trade_date"),
            "true_window_n": int(win_pat[key]["n"]),
            "true_window_net": round(win_pat[key]["net"], 2),
            "true_all_net": round(all_pat[key]["net"], 2),
        })
    n_mismatch = sum(1 for r in displayed if r["displayed_n"] != r["true_window_n"])
    gross_net_mismatch = sum(
        1 for r in displayed if abs(r["displayed_pnl"] - r["true_all_net"]) > 0.01)
    return {
        "cutoff": cutoff,
        "displayed_groups": len(displayed),
        "displayed_n_sum": sum(r["displayed_n"] for r in displayed),
        "true_window_n_sum": sum(r["true_window_n"] for r in displayed),
        "window_n_mismatch_groups": n_mismatch,
        "gross_vs_net_mismatch_groups": gross_net_mismatch,
        "old_era_rows": old_rows,
        "net_missing_rows": net_missing,
        "examples": sorted(
            displayed,
            key=lambda r: abs(r["displayed_n"] - r["true_window_n"]),
            reverse=True,
        )[:5],
    }


def memory_audit(memory_db: Path, db_path: Path, trade_date: str,
                 window_days: int = 30) -> Dict[str, Any]:
    conn = _db_ro(memory_db)
    if conn is None:
        return {"status": "memory_db_missing_or_unreadable", "db": str(memory_db)}
    try:
        pats = [dict(r) for r in conn.execute(
            "SELECT direction, exit_reason, strength_bucket, total_trades, total_pnl, "
            "last_trade_date FROM pattern_memory")]
    except sqlite3.Error as exc:
        conn.close()
        return {"status": "memory_schema_error", "error": str(exc)}
    conn.close()

    conn = _db_ro(db_path)
    if conn is None:
        return {"status": "trade_db_missing_or_unreadable", "db": str(db_path)}
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT trade_date, symbol, direction, exit_reason, signal_strength, "
            "realized_pnl, pnl_net FROM trade_records WHERE realized_pnl IS NOT NULL "
            "AND strategy_id NOT IN ('manual','manual_sync','reconciler','futu_sync','broker_reconcile') "
            "AND source_type NOT IN ('SCALPING','ORB_ENGINE','PLAYBOOK_SHADOW','MAIN_PIPELINE')")]
    finally:
        conn.close()

    cutoff = (date.fromisoformat(trade_date) - timedelta(days=window_days)).isoformat()
    out = compare_memory_patterns(pats, rows, cutoff)
    out.update({"status": "ok", "window_days": window_days,
                "memory_db": str(memory_db), "trade_db": str(db_path)})
    return out


def summarize(price: Dict[str, Dict[str, Any]], debate: Dict[str, Any],
              stages: Dict[str, Any], memory: Dict[str, Any]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    hard_price_states = ("prev_close_mismatch", "pre_rate_mismatch",
                         "missing_price", "no_daily_bar")
    bad_price = {s: v for s, v in price.items()
                 if v.get("status") in hard_price_states}
    if bad_price:
        findings.append({
            "severity": "high",
            "area": "price_basis",
            "message": "prev_close 与上一已完成日 K 线不一致或不可核验",
            "count": len(bad_price),
            "symbols": sorted(bad_price)[:20],
        })
    snapshot_errors = {s: v for s, v in price.items() if v.get("status") == "snapshot_error"}
    if snapshot_errors:
        findings.append({
            "severity": "medium",
            "area": "snapshot_error",
            "message": "快照缺失/超时, 价格基准无法审计",
            "count": len(snapshot_errors),
            "symbols": sorted(snapshot_errors)[:20],
        })
    agg = debate.get("source_aggregate") or {}
    if agg.get("render_time_missing"):
        findings.append({
            "severity": "high",
            "area": "source_time",
            "message": "研报无任何发布时间 (模型无法判断时效)",
            "count": agg["render_time_missing"].get("research", 0),
        })
    if agg.get("duplicate_extra_items"):
        findings.append({
            "severity": "medium",
            "area": "source_duplicate",
            "message": "跨栏目/同栏目同稿重复, 不能当独立证据",
            "count": agg["duplicate_extra_items"],
            "cross_section_extra_items": agg.get("cross_section_extra_items", 0),
        })
    # 2026-10-23: challenger_agreed_raw is intentionally retained for audit;
    # canonical classification is challenger_verdict_state. Do not re-flag
    # PARTIAL_AGREE + raw true (that pair is expected after the state fix).
    if stages.get("status") == "ok":
        if stages.get("entry_gap_gt_1pct"):
            findings.append({
                "severity": "info",
                "area": "stage_gap",
                "message": "提交入场价与成交价偏离 >1%",
                "count": stages["entry_gap_gt_1pct"],
            })
        if stages.get("plan_vs_filled_rr_mismatch_gt_0_05"):
            findings.append({
                "severity": "info",
                "area": "stage_rr",
                "message": "计划 RR 与成交几何 RR 差异 >0.05 (不能互相解释)",
                "count": stages["plan_vs_filled_rr_mismatch_gt_0_05"],
            })
        if stages.get("tp_swallowed"):
            findings.append({
                "severity": "info",
                "area": "tp_gate",
                "message": "成交几何触发固定 TP 门, 实际由 trailing/EOD 管理",
                "count": stages["tp_swallowed"],
            })
    if memory.get("status") == "ok":
        if memory.get("window_n_mismatch_groups"):
            findings.append({
                "severity": "high",
                "area": "memory_window",
                "message": "展示分组不是真实滚动窗口 (last_trade_date 筛全史累计)",
                "count": memory["window_n_mismatch_groups"],
                "displayed_groups": memory["displayed_groups"],
            })
        if memory.get("gross_vs_net_mismatch_groups"):
            findings.append({
                "severity": "high",
                "area": "memory_net",
                "message": "展示 PnL 与净额不一致 (毛额被当净额)",
                "count": memory["gross_vs_net_mismatch_groups"],
                "displayed_groups": memory["displayed_groups"],
            })
        if memory.get("old_era_rows"):
            findings.append({
                "severity": "medium",
                "area": "memory_era",
                "message": "记忆聚合包含 2026-07-01 之前的旧时代行 (A10 口径不一致)",
                "count": memory["old_era_rows"],
            })
    return findings


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="DA 链契约只读审计")
    ap.add_argument("--date", required=True, help="ET 交易日 YYYY-MM-DD")
    ap.add_argument("--checkpoint-root", default=str(DEFAULT_CKPT_ROOT))
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--memory-db", default=str(DEFAULT_MEMORY_DB))
    ap.add_argument("--json", action="store_true", help="输出完整 JSON")
    ap.add_argument("--out", default=None, help="显式写 JSON 报告路径 (默认不写)")
    args = ap.parse_args(argv)

    raw_dir = Path(args.checkpoint_root) / args.date
    if not raw_dir.is_dir():
        print(f"checkpoint dir missing: {raw_dir}")
        return 2

    base = audit_checkpoints(raw_dir, args.date)
    stages = stage_audit(base["analyses"], Path(args.db), args.date)
    memory = memory_audit(Path(args.memory_db), Path(args.db), args.date)
    findings = summarize(base["price"], base, stages, memory)
    report = {
        "schema": "da_chain_contract_audit/v1",
        "date": args.date,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "read_only": True,
        "price": base["price"],
        "price_summary": dict(Counter(v.get("status") for v in base["price"].values())),
        "sources": base["source_aggregate"],
        "source_by_symbol": base["sources"],
        "debate_states": base["debate_states"],
        "partial_agreed_true": base["partial_agreed_true"],
        "debates": base["debates"],
        "stages": stages,
        "memory": memory,
        "findings": findings,
    }

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"DA contract audit {args.date} (read-only)")
    print("price:", json.dumps(report["price_summary"], ensure_ascii=False))
    s = report["sources"]
    print("sources:", json.dumps({k: s.get(k) for k in (
        "items", "publish_time_missing", "render_time_missing", "stale_over_7d",
        "duplicate_groups", "duplicate_extra_items",
        "cross_section_duplicate_groups", "cross_section_extra_items")},
        ensure_ascii=False))
    print("debate:", json.dumps(report["debate_states"], ensure_ascii=False),
          "raw_agreed_true=", len(report["partial_agreed_true"]),
          "(canonical state is challenger_verdict_state)")
    if stages.get("status") == "ok":
        print("stages:", json.dumps({k: stages.get(k) for k in (
            "instructions", "matched", "ambiguous", "no_instruction",
            "actionable_analyses", "actionable_without_instruction", "filled",
            "entry_gap_gt_1pct", "entry_gap_median_pct",
            "plan_vs_filled_rr_mismatch_gt_0_05",
            "db_rr_vs_geometry_mismatch_gt_0_05", "tp_swallowed")}, ensure_ascii=False))
    else:
        print("stages:", stages.get("status"))
    if memory.get("status") == "ok":
        print("memory:", json.dumps({k: memory.get(k) for k in (
            "displayed_groups", "displayed_n_sum", "true_window_n_sum",
            "window_n_mismatch_groups", "gross_vs_net_mismatch_groups",
            "old_era_rows", "net_missing_rows")}, ensure_ascii=False))
    else:
        print("memory:", memory.get("status"))
    print("findings:")
    for f in findings:
        print(f"  [{f['severity']}] {f['area']}: {f['message']} n={f.get('count')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
