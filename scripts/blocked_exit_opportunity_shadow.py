#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""blocked-exit 影子聚合 (2026-10-17 审计 P0-1 建议④; eval_on_demand, 无 cron).

输入: data/shadow/blocked_exit_opportunity.jsonl (exit_monitor 在 blocked_exits
      命中时按 60s 节流落下的快照)
输出: data/shadow/blocked_exit_opportunity_latest.json

回答三个定阈问题 (不改任何出场行为):
  1) 被冻结时长分布 — 用于定告警升级/受控重评的 N/M;
  2) 冻结期间最大不利位移 — 风险规模;
  3) 若按 SL 兜底 (stop_loss) 本应退出的 episode 数/占比 — 冻结的实际代价。

判据口径 (只认 SL 兜底, 不重建 TP/时间/因子规则): 一次快照的 price 越过
stop_loss (LONG: price<=stop; SHORT: price>=stop) 即记 stop_breach。快照最密
60s 一条 ⇒ 触发点可能晚于真实时点, 读数偏保守。

用法: PYTHONPATH=. venv/bin/python scripts/blocked_exit_opportunity_shadow.py
"""
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "data" / "shadow" / "blocked_exit_opportunity.jsonl"
OUT = ROOT / "data" / "shadow" / "blocked_exit_opportunity_latest.json"
ET = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)


def _pct(values, q):
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return round(ordered[idx], 4)


def _direction_sign(direction):
    return -1.0 if str(direction or "").upper() in (
        "SHORT", "SELL_SHORT", "SELL") else 1.0


def aggregate(rows):
    """按 intent_id 聚合快照。rows = 已解析的 jsonl 行; 坏行由调用方剔除。"""
    episodes = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = row.get("intent_id") or "|".join(str(row.get(k)) for k in (
            "target_env", "symbol", "intent_created_at"))
        try:
            ts = datetime.fromisoformat(str(row.get("ts")).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError) as error:
            logger.warning(
                f"[blocked-exit-shadow] 快照 ts 不可解析, 跳过该行: {row.get('ts')!r} ({error})",
                exc_info=True)
            continue
        ep = episodes.setdefault(key, {
            "intent_id": key,
            "symbol": row.get("symbol"),
            "target_env": row.get("target_env"),
            "direction": row.get("direction"),
            "entry_price": row.get("entry_price"),
            "stop_loss": row.get("stop_loss"),
            "intent_created_at": row.get("intent_created_at"),
            "last_state": row.get("intent_state"),
            "last_error": row.get("intent_error"),
            "n_observations": 0,
            "first_ts": ts,
            "last_ts": ts,
            "first_price": None,
            "last_price": None,
            "min_price": None,
            "max_price": None,
            "max_intent_age_s": 0.0,
        })
        ep["n_observations"] += 1
        ep["first_ts"] = min(ep["first_ts"], ts)
        ep["last_ts"] = max(ep["last_ts"], ts)
        ep["last_state"] = row.get("intent_state") or ep["last_state"]
        ep["last_error"] = row.get("intent_error") or ep["last_error"]
        try:
            ep["max_intent_age_s"] = max(
                ep["max_intent_age_s"], float(row.get("intent_age_s") or 0.0))
        except (TypeError, ValueError) as error:
            logger.warning(
                f"[blocked-exit-shadow] intent_age_s 不可解析, 按 0 处理: "
                f"{row.get('intent_age_s')!r} ({error})",
                exc_info=True)
        price = row.get("price")
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None
        if price is None or price <= 0:
            continue
        if ep["first_price"] is None:
            ep["first_price"] = price
        ep["last_price"] = price
        ep["min_price"] = price if ep["min_price"] is None else min(ep["min_price"], price)
        ep["max_price"] = price if ep["max_price"] is None else max(ep["max_price"], price)

    out = []
    for ep in episodes.values():
        sign = _direction_sign(ep["direction"])
        adverse = None
        if ep["first_price"] and ep["min_price"] is not None:
            if sign > 0:
                adverse = (ep["min_price"] - ep["first_price"]) / ep["first_price"] * 100
            else:
                adverse = (ep["first_price"] - ep["max_price"]) / ep["first_price"] * 100
        breach = False
        stop = ep["stop_loss"]
        try:
            stop = float(stop) if stop is not None else None
        except (TypeError, ValueError):
            stop = None
        if stop and stop > 0:
            if sign > 0:
                breach = ep["min_price"] is not None and ep["min_price"] <= stop
            else:
                breach = ep["max_price"] is not None and ep["max_price"] >= stop
        out.append({
            **ep,
            "blocked_seconds": round(max(0.0, ep["last_ts"] - ep["first_ts"]), 1),
            "adverse_move_pct": round(adverse, 4) if adverse is not None else None,
            "stop_breached": breach,
        })
    out.sort(key=lambda e: e["first_ts"])
    return out


def _load():
    if not LEDGER.exists():
        return [], 0
    rows, bad = [], 0
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            bad += 1
    return rows, bad


def main():
    rows, bad = _load()
    episodes = aggregate(rows)
    durations = [e["blocked_seconds"] for e in episodes]
    adverse = [e["adverse_move_pct"] for e in episodes
               if e["adverse_move_pct"] is not None]
    breaches = [e for e in episodes if e["stop_breached"]]
    payload = {
        "script": "blocked_exit_opportunity_shadow",
        "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
        "source": str(LEDGER.relative_to(ROOT)),
        "n_observations": len(rows),
        "n_bad_lines": bad,
        "n_episodes": len(episodes),
        "blocked_seconds": {
            "median": _pct(durations, 0.5),
            "p90": _pct(durations, 0.9),
            "max": round(max(durations), 1) if durations else None,
        },
        "adverse_move_pct": {
            "median": _pct(adverse, 0.5),
            "p90": _pct(adverse, 0.9),
            "max": round(max(adverse), 4) if adverse else None,
        },
        "stop_breach": {
            "n_episodes": len(breaches),
            "ratio": round(len(breaches) / len(episodes), 4) if episodes else None,
        },
        "decision_use": "只用于另立 DEB 定 N(告警升级)/M(受控重评); 本影子不改行为",
        "episodes": episodes,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"[blocked-exit-shadow] episodes={len(episodes)} observations={len(rows)} "
          f"bad={bad} stop_breach={len(breaches)}")
    print(f"[saved] {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
