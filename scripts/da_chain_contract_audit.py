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
import hashlib
import sys
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

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
    except ValueError as _sd_e:
        # 2026-10-28 except:pass 清剿: 日期坏 ⇒ stale_days 缺项, 审计结论会乐观。
        print(f"[warn] stale_days 计算失败 ({trade_date} / {bar_date}): {_sd_e}", file=sys.stderr)

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
        except ValueError as _pd_e:
            # 2026-10-28 except:pass 清剿: 格式不匹配的候选静默跳, 留痕以免"没找到"被误读。
            print(f"[warn] 发布日期解析失败, 试下一种格式: {_pd_e} | {s!r}", file=sys.stderr)
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


def input_visibility(collected: Dict[str, Any], user_prompt: str,
                     event: Dict[str, Any]) -> Dict[str, Any]:
    sections = {}
    parts = re.split(r"^##[ \t]+([^\n]+)\n?", user_prompt.replace("\r\n", "\n"),
                     flags=re.MULTILINE)
    for index in range(1, len(parts), 2):
        sections[parts[index].strip()] = parts[index + 1]
    prompt_available = bool(user_prompt.strip())
    distribution = collected.get("capital_distribution")
    numeric_fields = [
        f"capital_{side}_{size}"
        for side in ("in", "out") for size in ("super", "big", "mid", "small")
        if isinstance(distribution, dict)
        and not isinstance(distribution.get(f"capital_{side}_{size}"), bool)
        and _f(distribution.get(f"capital_{side}_{size}")) is not None
    ]
    if isinstance(distribution, dict) and "_error" in distribution:
        capital_state = "failed"
    elif len(numeric_fields) == 8:
        capital_state = "complete"
    elif numeric_fields:
        capital_state = "partial"
    else:
        capital_state = "missing"
    distribution_section = sections.get("资金面", "")
    intraday_section = sections.get("资金面 (分时)", "")
    distribution_visible = all(re.search(
        rf"^- {label}:[ \t]*[+-]?\d+(?:\.\d+)?{unit}[ \t]*$",
        distribution_section, re.MULTILINE,
    ) for label, unit in (("主力净流入", "亿"), ("超大单净", ""),
                          ("大单净", ""), ("主力占比", "%")))
    capital_mode = ("unknown" if not prompt_available else
                    "distribution" if distribution_visible else
                    "intraday" if intraday_section else
                    "unrecognized" if "资金面" in sections else "missing")
    earnings = collected.get("earnings_calendar")
    if isinstance(earnings, dict) and "_error" in earnings:
        earnings_state = "failed"
    elif isinstance(earnings, dict) and earnings.get("next_earnings"):
        earnings_state = "upcoming"
    elif (isinstance(earnings, dict) and earnings.get("_quality") == "ok"
          and earnings.get("next_earnings") is None and earnings.get("all_upcoming") == []):
        earnings_state = "healthy_empty"
    else:
        earnings_state = "unknown"
    quality = collected.get("_quality")
    quality = quality if isinstance(quality, dict) else {}
    statuses = collected.get("_source_status")
    statuses = statuses if isinstance(statuses, dict) else {}
    explicit_count = sum(value in ("ok", "empty", "failed", "disabled")
                         for value in statuses.values())
    total, ok = _f(quality.get("total")), _f(quality.get("ok"))
    return {
        "basis": "saved_user_prompt_sections",
        "prompt_available": prompt_available,
        "capital": {
            "collected_state": capital_state,
            "numeric_field_count": len(numeric_fields),
            "prompt_mode": capital_mode,
            "distribution_not_rendered": (prompt_available and capital_state == "complete"
                                          and not distribution_visible),
            "intraday_main_missing": bool(re.search(
                r"^- 最新主力流入:\s*N/A\s*$", intraday_section, re.MULTILINE)),
        },
        "earnings": {
            "collected_state": earnings_state,
            "lookahead_days": earnings.get("lookahead_days") if isinstance(earnings, dict) else None,
            "section_present": "财报日历" in sections if prompt_available else None,
            "healthy_empty_not_rendered": (prompt_available and earnings_state == "healthy_empty"
                                          and "财报日历" not in sections),
        },
        "coverage": {
            "reported_task_total": total,
            "reported_task_ok": ok,
            "explicit_status_count": explicit_count,
            "source_statuses": statuses,
            "reported_full_with_untracked_sources": bool(
                total is not None and total > 0 and ok == total and explicit_count < total),
            "rendered_coverage_pct": None,
        },
        "event": {
            "injected": event.get("injected") if isinstance(event.get("injected"), bool) else None,
            "reason": event.get("reason"),
            "brief_id": event.get("brief_id"),
        },
    }


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
        "basis": "collected_before_render",
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
        "basis": "collected_before_render",
        "rendered_items": None,
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
    visibility: Dict[str, Dict[str, Any]] = {}
    raw_context: Dict[str, Dict[str, Any]] = {}
    for raw_path in raw_files:
        raw = _read_json(raw_path) or {}
        collected = raw.get("collected") or {}
        symbol = str(raw.get("symbol") or raw_path.name.replace(".raw.json", ""))
        snap = collected.get("snapshot") or {}
        kline = collected.get("kline") or []
        price[symbol] = classify_price_basis(snap, kline, trade_date)
        sources[symbol] = source_stats(collected, trade_date)
        user_prompt = str(raw.get("user_prompt") or "")
        event = raw.get("event_context") or raw.get("_event_context") or {}
        event = event if isinstance(event, dict) else {}
        visibility[symbol] = input_visibility(collected, user_prompt, event)
        raw_context[symbol] = {
            "raw_saved_at": raw.get("saved_at"),
            "event_as_of": event.get("as_of"),
            "event_injection_hash": event.get("injection_hash"),
            "input_version": raw.get("input_version") or raw.get("_input_version"),
            "user_prompt_sha256": hashlib.sha256(user_prompt.encode("utf-8")).hexdigest(),
            "user_prompt_chars": len(user_prompt),
        }

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
                "checkpoint_model": obj.get("_ckpt_model"),
                "reported_model": llm.get("_model_reported"),
                "prompt_hash": llm.get("_prompt_hash"),
                **raw_context.get(symbol, {}),
                "event_as_of": (raw_context.get(symbol, {}).get("event_as_of")
                                or (obj.get("_event_context") or {}).get("as_of")),
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
        "input_visibility": visibility,
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
    from app.playbook.instruction_attribution import (
        count_sources, resolve_trade_instruction_ids,
    )

    conn = _db_ro(db_path)
    if conn is None:
        return {"status": "db_missing_or_unreadable", "db": str(db_path)}
    try:
        inst_rows = [dict(r) for r in conn.execute(
            "SELECT id, date, symbol, direction, entry_price, stop_loss, take_profit, "
            "risk_reward, decision_id, execution_state, execution_price, model_used, skip_reason "
            "FROM playbook_instructions WHERE date=? AND decision_id LIKE 'DA-%' ORDER BY id",
            (trade_date,),
        )]
        by_decision: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for inst in inst_rows:
            if inst.get("decision_id"):
                by_decision[inst["decision_id"]].append(inst)
        by_id = {r["id"]: r for r in inst_rows}

        trade_rows = []
        for r in conn.execute(
            "SELECT id, trade_date, symbol, direction, entry_price, quantity, "
            "realized_pnl, pnl_net, fees_est, fees_source, exit_time, exit_price, exit_reason, "
            "metadata_json, decision_id FROM trade_records WHERE trade_date=? ORDER BY id",
            (trade_date,),
        ):
            row = dict(r)
            try:
                row["_meta"] = json.loads(row.get("metadata_json") or "{}")
            except (TypeError, ValueError):
                row["_meta"] = {}
            trade_rows.append(row)
    finally:
        conn.close()

    attribution_sources: List[str] = []
    resolved = resolve_trade_instruction_ids(trade_rows, inst_rows, attribution_sources)
    by_inst: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    da_sources = []
    for tr, iid, source in zip(trade_rows, resolved, attribution_sources):
        tr["_attribution_source"] = source
        linked = isinstance(iid, int) and iid in by_id
        if linked:
            by_inst[iid].append(tr)
        if linked or str(tr.get("decision_id") or "").startswith("DA-"):
            da_sources.append(source)

    def fill_view(tr):
        meta = tr.get("_meta") or {}
        tp_gate = meta.get("_tp_rr_gate") or {}
        f_entry = _f(tr.get("entry_price"))
        f_sl = _f(tp_gate.get("initial_sl")) or _f(meta.get("stop_loss"))
        f_tp = _f(tp_gate.get("tp")) or _f(meta.get("take_profit"))
        return {
            "trade_id": tr.get("id"), "entry": f_entry, "sl": f_sl, "tp": f_tp,
            "rr": compute_rr(tr.get("direction"), f_entry, f_sl, f_tp),
            "tp_swallowed": bool(tp_gate.get("swallowed")),
            "tp_rr_at_execution": _f(tp_gate.get("rr_at_execution")),
            "qty": _f(tr.get("quantity")),
            "attribution_source": tr.get("_attribution_source"),
        }

    def outcome_view(fills):
        closed = [tr for tr in fills
                  if tr.get("exit_time") or (_f(tr.get("exit_price")) or 0) > 0]

        def complete_sum(key):
            values = [_f(tr.get(key)) for tr in fills]
            return round(sum(values), 6) if values and all(v is not None for v in values) else None

        return {
            "fill_count": len(fills), "closed_count": len(closed),
            "open_count": len(fills) - len(closed),
            "realized_pnl": complete_sum("realized_pnl"),
            "pnl_net": complete_sum("pnl_net"), "fees_est": complete_sum("fees_est"),
            "fees_sources": sorted({str(tr.get("fees_source") or "unknown") for tr in fills}),
            "exit_reasons": sorted({str(tr.get("exit_reason")) for tr in closed}),
            "attribution_sources": sorted({tr.get("_attribution_source") for tr in fills}),
            "trade_ids": [tr.get("id") for tr in fills],
        }

    rows = []
    for symbol, a in analyses.items():
        plan = a.get("plan") or {}
        did = a.get("decision_id")
        decision_matches = [r for r in (by_decision.get(did, []) if did else [])
                            if r.get("symbol") == symbol]
        candidate_insts = decision_matches
        match = "no_instruction"
        inst = None
        if len(decision_matches) == 1:
            inst, match = decision_matches[0], "decision_id"
        elif len(decision_matches) > 1:
            match = "ambiguous"
        else:
            candidates = [r for r in inst_rows if r["symbol"] == symbol]
            candidate_insts = candidates
            if len(candidates) == 1:
                inst, match = candidates[0], "symbol_unique"
            elif len(candidates) > 1:
                match = "ambiguous"
        entry = _f(plan.get("entry_price"))
        sl = _f(plan.get("stop_loss_price"))
        tp = _f(plan.get("take_profit_price"))
        models = {
            "checkpoint": a.get("checkpoint_model"),
            "reported": a.get("reported_model"),
            "instruction": inst.get("model_used") if inst else None,
        }
        known_models = {str(value) for value in models.values() if value}
        row: Dict[str, Any] = {
            "symbol": symbol, "decision_id": did,
            "analyst_direction": _norm_dir(a.get("direction")),
            "instruction_id": inst.get("id") if inst else None, "match": match,
            "candidate_instruction_ids": ([r["id"] for r in candidate_insts]
                                           if match == "ambiguous" else []),
            "models": models, "model_mismatch": len(known_models) > 1,
            "prompt": {
                "production_hash": a.get("prompt_hash"),
                "user_prompt_sha256": a.get("user_prompt_sha256"),
                "user_prompt_chars": a.get("user_prompt_chars"),
                "input_version": a.get("input_version"),
            },
            "as_of": {"raw_saved_at": a.get("raw_saved_at"),
                      "event_as_of": a.get("event_as_of"),
                      "event_injection_hash": a.get("event_injection_hash")},
            "gate": {"decision": (a.get("gate") or {}).get("decision"),
                     "reasons": (a.get("gate") or {}).get("reasons") or []},
            "plan": {"entry": entry, "sl": sl, "tp": tp,
                     "rr": compute_rr(a.get("direction"), entry, sl, tp)},
            "submitted": None, "filled": None, "fills": [], "outcome": None,
            "ambiguous_outcomes": [],
        }
        if inst:
            sub = {
                "entry": _f(inst.get("entry_price")), "sl": _f(inst.get("stop_loss")),
                "tp": _f(inst.get("take_profit")), "rr_db": _f(inst.get("risk_reward")),
                "state": inst.get("execution_state"), "direction": inst.get("direction"),
                "skip_reason": inst.get("skip_reason"),
            }
            sub["rr"] = compute_rr(inst.get("direction"), sub["entry"], sub["sl"], sub["tp"])
            row["submitted"] = sub
            trades = by_inst.get(int(inst["id"]), [])
            row["fills"] = [fill_view(tr) for tr in trades]
            if trades:
                row["filled"] = row["fills"][0]
                row["outcome"] = outcome_view(trades)
                for fill in row["fills"]:
                    f_entry = fill["entry"]
                    if f_entry and sub.get("entry"):
                        fill["entry_gap_pct"] = round((sub["entry"] - f_entry) / f_entry * 100.0, 4)
                row["entry_gap_pct"] = row["fills"][0].get("entry_gap_pct")
        elif match == "ambiguous":
            trade_groups = [(candidate, by_inst.get(int(candidate["id"]), []))
                            for candidate in candidate_insts]
            trade_groups = [(candidate, trades) for candidate, trades in trade_groups if trades]
            if len(trade_groups) == 1:
                trades = trade_groups[0][1]
                row["fills"] = [fill_view(tr) for tr in trades]
                row["filled"] = row["fills"][0]
                row["outcome"] = outcome_view(trades)
            elif len(trade_groups) > 1:
                row["ambiguous_outcomes"] = [{
                    "instruction_id": candidate["id"],
                    "fills": [fill_view(tr) for tr in trades],
                    "outcome": outcome_view(trades),
                } for candidate, trades in trade_groups]
        rows.append(row)

    filled = [r for r in rows if r.get("filled")]
    fills = [fill for r in rows for fill in r.get("fills", [])]
    gaps = [abs(fill["entry_gap_pct"]) for fill in fills if fill.get("entry_gap_pct") is not None]
    rr_mismatch = [
        fill for r in rows for fill in r.get("fills", [])
        if (r.get("plan") or {}).get("rr") is not None
        and fill.get("rr") is not None
        and abs(r["plan"]["rr"] - fill["rr"]) > 0.05
    ]
    db_rr_mismatch = [
        r for r in rows
        if (r.get("submitted") or {}).get("rr_db") is not None
        and (r.get("submitted") or {}).get("rr") is not None
        and abs(r["submitted"]["rr_db"] - r["submitted"]["rr"]) > 0.05
    ]
    actionable = [r for r in rows if r["analyst_direction"] in ("BUY", "SELL_SHORT")]
    return {
        "status": "ok", "instructions": len(inst_rows),
        "matched": sum(1 for r in rows if r["match"] == "decision_id"),
        "symbol_unique": sum(1 for r in rows if r["match"] == "symbol_unique"),
        "ambiguous": sum(1 for r in rows if r["match"] == "ambiguous"),
        "no_instruction": sum(1 for r in rows if r["match"] == "no_instruction"),
        "actionable_analyses": len(actionable),
        "actionable_without_instruction": sum(
            1 for r in actionable if r["match"] == "no_instruction"),
        "filled": len(filled), "fill_events": len(fills),
        "ambiguous_outcome_groups": sum(len(r["ambiguous_outcomes"]) for r in rows),
        "model_mismatch": sum(r["model_mismatch"] for r in rows),
        "trade_attribution": count_sources(da_sources),
        "entry_gap_gt_1pct": sum(1 for g in gaps if g > 1.0),
        "entry_gap_median_pct": round(sorted(gaps)[len(gaps) // 2], 4) if gaps else None,
        "plan_vs_filled_rr_mismatch_gt_0_05": len(rr_mismatch),
        "db_rr_vs_geometry_mismatch_gt_0_05": len(db_rr_mismatch),
        "tp_swallowed": sum(bool(fill.get("tp_swallowed")) for fill in fills),
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


def compare_feedback_clocks(trade_rows: Iterable[Dict[str, Any]],
                            trade_date: str, window_days: int = 30,
                            metadata_available: bool = True) -> Dict[str, Any]:
    """Compare clocks and missing inputs, not returns or a replacement memory.

    The window is explicitly inclusive [D-(N-1), D]. Naive exit timestamps
    have a SQLite calendar date but no provable ET date. Source labels use
    the actual decision_id prefix, never PLAYBOOK_SIM or strategy_id as DA.
    """
    end = date.fromisoformat(trade_date)
    start = (end - timedelta(days=window_days - 1)).isoformat()
    et = ZoneInfo("America/New_York")
    groups = defaultdict(list)
    sources = defaultdict(list)
    fees = Counter()
    for row in trade_rows:
        rid = row["id"]
        entry_day = str(row.get("trade_date") or "")
        exit_value = row.get("exit_time")
        sqlite_day = et_day = None
        time_unknown = False
        try:
            stamp = datetime.fromisoformat(str(exit_value).replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                sqlite_day, time_unknown = stamp.date().isoformat(), True
            else:
                sqlite_day = stamp.astimezone(timezone.utc).date().isoformat()
                et_day = stamp.astimezone(et).date().isoformat()
        except (TypeError, ValueError):
            time_unknown = True
        in_entry = start <= entry_day <= trade_date
        in_et = bool(et_day and start <= et_day <= trade_date)
        in_sqlite = bool(sqlite_day and start <= sqlite_day <= trade_date)
        if not (in_entry or in_et or in_sqlite):
            continue
        groups["scope_ids"].append(rid)
        if in_entry:
            groups["entry_window_ids"].append(rid)
        if in_et:
            groups["exit_et_window_ids"].append(rid)
        if in_sqlite:
            groups["exit_sqlite_window_ids"].append(rid)
        if entry_day == trade_date:
            groups["entry_day_ids"].append(rid)
        if et_day == trade_date:
            groups["exit_et_day_ids"].append(rid)
        # Reproduce the active query's NULL-time fallback separately; do not
        # promote an imputed day into an observed exit event.
        fallback = exit_value is None and entry_day == trade_date and row.get("exit_price") is not None
        if sqlite_day == trade_date or fallback:
            groups["live_exit_day_ids"].append(rid)
        if fallback:
            groups["live_missing_time_fallback_ids"].append(rid)
        if time_unknown:
            groups["exit_time_unknown_ids"].append(rid)
        if et_day and sqlite_day != et_day:
            groups["exit_utc_et_date_mismatch_ids"].append(rid)
        if et_day and et_day > trade_date:
            groups["entry_cohort_exited_after_asof_ids"].append(rid)
        if _f(row.get("pnl_net")) is None:
            groups["net_missing_ids"].append(rid)
        if _f(row.get("fees_est")) is None:
            groups["fees_missing_ids"].append(rid)
        fees[str(row.get("fees_source") or "unknown")] += 1
        did = str(row.get("decision_id") or "")
        source = ("DA" if did.startswith("DA-") else
                  "EXT" if did.startswith(("EXT-", "EXT:")) else
                  "UE" if did.startswith(("universe_expansion", "UE-", "UE:")) else
                  "OTHER" if did else "UNKNOWN")
        sources[source].append(rid)
        if not metadata_available:
            groups["a9_metadata_unknown_ids"].append(rid)
        else:
            try:
                raw_metadata = row.get("metadata_json")
                meta = {} if raw_metadata is None else json.loads(raw_metadata)
                if not isinstance(meta, dict):
                    raise ValueError("metadata is not an object")
                if meta.get("exclusion_reason") not in (None, ""):
                    groups["a9_excluded_ids"].append(rid)
            except (TypeError, ValueError):
                groups["a9_metadata_unknown_ids"].append(rid)
        # Diagnostic mirror of ERA_CLEAN_PERFORMANCE_WHERE, not a new filter.
        if (entry_day < CURRENT_ERA_START or row.get("data_quality") not in ("clean", "real")
                or row.get("source_type") in ("POSITION_SYNC", "MANUAL")):
            groups["a10_excluded_ids"].append(rid)
    keys = ("scope_ids", "entry_window_ids", "exit_et_window_ids", "exit_sqlite_window_ids",
            "entry_day_ids", "exit_et_day_ids", "live_exit_day_ids", "exit_time_unknown_ids",
            "live_missing_time_fallback_ids", "entry_cohort_exited_after_asof_ids",
            "exit_utc_et_date_mismatch_ids", "net_missing_ids", "fees_missing_ids", "a9_excluded_ids",
            "a9_metadata_unknown_ids", "a10_excluded_ids")
    views = {key: sorted(groups[key]) for key in keys}
    live, actual = set(views["live_exit_day_ids"]), set(views["exit_et_day_ids"])
    return {
        "status": "ok", "read_only": True, "date": trade_date,
        "window_start": start, "window_end": trade_date, "window_days": window_days,
        "scope": "active_feedback_source_filters; clock-window union; not performance",
        "source_basis": "trade_records.decision_id_prefix_only; missing_is_unknown",
        "views": views, "counts": {key.removesuffix("_ids"): len(v) for key, v in views.items()},
        "live_day_not_observed_et_ids": sorted(live - actual),
        "observed_et_day_missing_live_ids": sorted(actual - live),
        "source_ids": dict(sorted(sources.items())),
        "fees_source_counts": dict(sorted(fees.items())),
        "a9_including_count": len(views["scope_ids"]),
        "a9_excluding_count": (len(views["scope_ids"]) - len(views["a9_excluded_ids"])
                               - len(views["a9_metadata_unknown_ids"])),
        "a9_unknown_count": len(views["a9_metadata_unknown_ids"]),
        "memory_written": False, "prompt_changed": False,
    }


def feedback_clock_audit(db_path: Path, trade_date: str) -> Dict[str, Any]:
    """Read trade records directly; importing/calling feedback writers is forbidden."""
    conn = _db_ro(db_path)
    if conn is None:
        return {"status": "trade_db_missing_or_unreadable", "read_only": True}
    fields = ("id", "trade_date", "exit_time", "exit_price", "realized_pnl", "pnl_net",
              "fees_est", "fees_source", "data_quality", "source_type", "decision_id",
              "metadata_json")
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(trade_records)")}
        required = {"id", "trade_date", "exit_time", "realized_pnl", "strategy_id", "source_type"}
        if not required <= columns:
            return {"status": "trade_schema_incomplete", "read_only": True,
                    "missing_columns": sorted(required - columns)}
        select = ", ".join(f if f in columns else f"NULL AS {f}" for f in fields)
        rows = [dict(r) for r in conn.execute(
            f"SELECT {select} FROM trade_records WHERE realized_pnl IS NOT NULL "
            "AND strategy_id NOT IN ('manual','manual_sync','reconciler','futu_sync','broker_reconcile') "
            "AND source_type NOT IN ('SCALPING','ORB_ENGINE','PLAYBOOK_SHADOW','MAIN_PIPELINE')")]
        out = compare_feedback_clocks(
            rows, trade_date, metadata_available="metadata_json" in columns)
        out["missing_columns"] = sorted(set(fields) - columns)
        return out
    except sqlite3.Error as exc:
        return {"status": "trade_schema_error", "read_only": True, "error": str(exc)}
    finally:
        conn.close()


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
            "basis": "collected_before_render",
            "message": "采集层研报缺少可识别时间字段，未证明实际 prompt 缺少时效信息",
            "count": agg["render_time_missing"].get("research", 0),
        })
    if agg.get("duplicate_extra_items"):
        findings.append({
            "severity": "medium",
            "area": "source_duplicate",
            "basis": "collected_before_render",
            "message": "采集层存在同稿重复，未证明实际 prompt 重复；同稿不能当独立证据",
            "count": agg["duplicate_extra_items"],
            "cross_section_extra_items": agg.get("cross_section_extra_items", 0),
        })
    visibility = debate.get("input_visibility") or {}
    for area, section, flag, message in (
        ("capital_render_gap", "capital", "distribution_not_rendered",
         "已采集完整资金分布，但保存的 prompt 未检出预期格式的资金分布数值"),
        ("earnings_render_gap", "earnings", "healthy_empty_not_rendered",
         "财报日历来源健康且窗口为空，但保存的 prompt 未呈现财报日历栏目"),
        ("source_status_gap", "coverage", "reported_full_with_untracked_sources",
         "任务成功数满额，但显式来源状态未覆盖任务数；不能代表实际输入完整"),
    ):
        symbols = sorted(s for s, v in visibility.items() if v[section][flag])
        if symbols:
            findings.append({
                "severity": "medium", "area": area,
                "basis": ("collected_source_status" if section == "coverage"
                          else "collected_vs_saved_user_prompt_sections"),
                "message": message, "count": len(symbols), "symbols": symbols,
            })
    rejected = {s: v["event"] for s, v in visibility.items()
                if v["event"]["injected"] is False}
    if rejected:
        findings.append({
            "severity": "info", "area": "event_not_injected",
            "basis": "saved_event_context_metadata",
            "message": "共享事件未注入，按保存的原因留痕；日期拒绝不等于闸门故障",
            "count": len(rejected), "symbols": sorted(rejected),
            "reasons": dict(Counter(v["reason"] or "unknown" for v in rejected.values())),
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
    feedback_clock = feedback_clock_audit(Path(args.db), args.date)
    findings = summarize(base["price"], base, stages, memory)
    visibility = base.get("input_visibility", {})
    visibility_summary = {
        "symbols": len(visibility),
        "prompt_available": sum(v["prompt_available"] for v in visibility.values()),
        "capital_prompt_modes": dict(Counter(v["capital"]["prompt_mode"]
                                             for v in visibility.values())),
        "distribution_not_rendered": sum(v["capital"]["distribution_not_rendered"]
                                         for v in visibility.values()),
        "intraday_main_missing": sum(v["capital"]["intraday_main_missing"]
                                     for v in visibility.values()),
        "healthy_empty_calendar_not_rendered": sum(v["earnings"]["healthy_empty_not_rendered"]
                                                   for v in visibility.values()),
        "reported_full_with_untracked_sources": sum(v["coverage"]["reported_full_with_untracked_sources"]
                                                    for v in visibility.values()),
        "event_not_injected_reasons": dict(Counter(v["event"]["reason"] or "unknown"
                                                   for v in visibility.values()
                                                   if v["event"]["injected"] is False)),
        "rendered_coverage_pct": None,
    }
    report = {
        "schema": "da_chain_contract_audit/v1",
        "date": args.date,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "read_only": True,
        "price": base["price"],
        "price_summary": dict(Counter(v.get("status") for v in base["price"].values())),
        "sources": base["source_aggregate"],
        "source_by_symbol": base["sources"],
        "input_visibility": visibility,
        "input_visibility_summary": visibility_summary,
        "debate_states": base["debate_states"],
        "partial_agreed_true": base["partial_agreed_true"],
        "debates": base["debates"],
        "stages": stages,
        "memory": memory,
        "feedback_clock": feedback_clock,
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
    print("sources (collected, not rendered):", json.dumps({k: s.get(k) for k in (
        "basis", "rendered_items", "items", "publish_time_missing", "render_time_missing", "stale_over_7d",
        "duplicate_groups", "duplicate_extra_items",
        "cross_section_duplicate_groups", "cross_section_extra_items")},
        ensure_ascii=False))
    print("input_visibility:", json.dumps(visibility_summary, ensure_ascii=False))
    print("debate:", json.dumps(report["debate_states"], ensure_ascii=False),
          "raw_agreed_true=", len(report["partial_agreed_true"]),
          "(canonical state is challenger_verdict_state)")
    if stages.get("status") == "ok":
        print("stages:", json.dumps({k: stages.get(k) for k in (
            "instructions", "matched", "symbol_unique", "ambiguous", "no_instruction",
            "actionable_analyses", "actionable_without_instruction", "filled", "fill_events",
            "ambiguous_outcome_groups", "model_mismatch", "trade_attribution",
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
    if feedback_clock.get("status") == "ok":
        counts = feedback_clock["counts"]
        print("feedback_clock:", json.dumps({
            "window": [feedback_clock["window_start"], feedback_clock["window_end"]],
            "sources": {k: len(v) for k, v in feedback_clock["source_ids"].items()},
            "live_day_not_observed_et": len(feedback_clock["live_day_not_observed_et_ids"]),
            "exit_time_unknown": counts["exit_time_unknown"],
            "utc_et_date_mismatch": counts["exit_utc_et_date_mismatch"],
            "net_missing": counts["net_missing"],
            "a9_excluded": counts["a9_excluded"],
            "a9_unknown": feedback_clock["a9_unknown_count"],
            "a10_excluded": counts["a10_excluded"],
        }, ensure_ascii=False))
    else:
        print("feedback_clock:", feedback_clock.get("status"))
    print("findings:")
    for f in findings:
        print(f"  [{f['severity']}] {f['area']}: {f['message']} n={f.get('count')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
