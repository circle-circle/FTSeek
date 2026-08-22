#!/usr/bin/env python3
"""证伪门反事实尺子 — 执行准确性蓝图 P0-2 (docs/design/2026-08-22_execution_accuracy_blueprint.md)

问题: 证伪门拦掉的候选, 如果放行会怎样? 本尺子只读回答 —
读 data/intraday_reentry_shadow.jsonl 的 action=rejected 行 (run_scan 已落,
零交易路径改动), 用 data/replay_bars_daily 日K (QFQ) 算被拒候选的
T+5 方向带符号收益, 按拒因(门)聚合 → 门质量遥测:
  命中率高 = 该门在拦"本来会赢的交易" (over-blocking 嫌疑)
  命中率低 = 该门在拦真亏损交易 (有效证伪)

口径: 锚日=被拒日, T+5=其后第5个交易日收盘, sign=+1 LONG / -1 SHORT
(与 ic_tracker 方向带符号约定一致, 正=该方向会赚钱)。

预注册边界: 描述性证据, 供 P1 门 A/B 判读参考; 不构成门的启停判据。
删失显式计数 (无价/窗口未到/K线缺), 不静默丢弃。

cron: 56 6 * * 2-6 (t5cf 06:53 之后, K线缓存新鲜; 回滚 = 删本行 + 本脚本 + data/gate_cf_*)
"""
from __future__ import annotations

import json
import os
import statistics
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import sys
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))  # 使 app.utils.timezone(now_et) 可导入, 免 UTC fallback
SHADOW_JSONL = PROJECT_ROOT / "data" / "intraday_reentry_shadow.jsonl"
DAILY_DIR = PROJECT_ROOT / "data" / "replay_bars_daily"
OUT_LATEST = PROJECT_ROOT / "data" / "gate_cf_latest.json"
OUT_HISTORY = PROJECT_ROOT / "data" / "gate_cf_history.jsonl"

T_HORIZON = 5  # T+5 个交易日


def _now_et_str() -> str:
    try:
        from app.utils.timezone import now_et
        return now_et().isoformat()
    except Exception:
        return datetime.utcnow().isoformat() + "+00:00(utc-fallback)"


def _cache_path(symbol: str) -> Path:
    return DAILY_DIR / f"{symbol.replace('.', '_')}.json"


def _load_bars(symbol: str):
    """[[date,o,h,l,c]] 升序, 只读缓存不补拉 (补拉是 t5cf 06:53 的职责, 免重复打 Futu)。"""
    cp = _cache_path(symbol)
    if not cp.exists():
        return None
    try:
        rows = json.loads(cp.read_text())
        return rows or None
    except Exception:
        return None


def _sign(direction: str) -> int | None:
    d = (direction or "").upper()
    if d in ("BUY", "LONG"):
        return 1
    if d in ("SELL_SHORT", "SHORT"):
        return -1
    return None


def _gate_names(reasons: list) -> list[str]:
    out = []
    for r in reasons or []:
        name = str(r).split("(")[0].strip()
        if name and name not in out:
            out.append(name)
    return out


def _close_t5(bars: list[list], anchor_date: str):
    """锚日后第 T_HORIZON 个交易日收盘; 窗口未到返回 None (右缘删失)。"""
    after = [b for b in bars if b[0] > anchor_date]
    if len(after) < T_HORIZON:
        return None, len(after)
    return after[T_HORIZON - 1][4], T_HORIZON


def main() -> int:
    if not SHADOW_JSONL.exists():
        print(f"[gate_cf] {SHADOW_JSONL} 不存在 — 无遥测样本")
        return 0

    rejects = []
    for line in SHADOW_JSONL.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("action") == "rejected":
            rejects.append(r)
    print(f"[gate_cf] rejected_lines={len(rejects)}")

    per_gate: dict[str, dict] = defaultdict(lambda: {
        "n_rejects": 0, "n_eval": 0, "censored_no_price": 0,
        "censored_window": 0, "censored_bars": 0, "wins": 0, "signed": []})
    overall = {"n_rejects": len(rejects), "n_eval": 0, "censored_no_price": 0,
               "censored_window": 0, "censored_bars": 0}
    detail_rows = []

    for r in rejects:
        gates = _gate_names(r.get("reasons") or []) or ["<no_reason>"]
        price = r.get("price")
        sign = _sign(r.get("direction"))
        sym = r.get("symbol")
        date = r.get("date")

        # 先定性本条记录: evaluated / 三类删失之一
        cf_pct = None
        if price is None or sign is None or not sym or not date:
            status = "censored_no_price" if price is None else "censored_other"
        else:
            bars = _load_bars(sym)
            if not bars:
                status = "censored_bars"
            else:
                close5, _avail = _close_t5(bars, date)
                if close5 is None:
                    status = "censored_window"
                else:
                    status = "evaluated"
                    cf_pct = sign * (close5 / float(price) - 1) * 100
                    detail_rows.append({"date": date, "symbol": sym, "direction":
                                        r.get("direction"), "gates": gates,
                                        "price": price, "cf_t5_pct": round(cf_pct, 3)})

        # 按门聚合 (一条多拒因 → 各门各记一次)
        for g in gates:
            pg = per_gate[g]
            pg["n_rejects"] += 1
            if status == "evaluated":
                pg["n_eval"] += 1
                pg["signed"].append(cf_pct)
                pg["wins"] += 1 if cf_pct > 0 else 0
            elif status in ("censored_no_price", "censored_window", "censored_bars"):
                pg[status] += 1

        # overall
        overall["n_eval" if status == "evaluated" else status] = \
            overall.get("n_eval" if status == "evaluated" else status, 0) + 1

    gate_report = {}
    for g, pg in sorted(per_gate.items(), key=lambda kv: -kv[1]["n_rejects"]):
        s = pg["signed"]
        entry = {k: pg[k] for k in ("n_rejects", "n_eval", "censored_no_price",
                                    "censored_window", "censored_bars", "wins")}
        if s:
            entry["hit_rate"] = round(pg["wins"] / len(s), 3)
            entry["mean_signed_t5_pct"] = round(statistics.mean(s), 3)
            entry["median_signed_t5_pct"] = round(statistics.median(s), 3)
        gate_report[g] = entry

    report = {
        "generated_at": _now_et_str(),
        "preregistered_boundary": "描述性证据, 供P1门A/B判读参考; 不构成门的启停判据。"
                                  "命中率=被拒候选若放行T+5方向带符号收益>0的占比。",
        "overall": overall,
        "gates": gate_report,
        "n_detail_rows": len(detail_rows),
    }
    OUT_LATEST.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    with OUT_HISTORY.open("a") as fh:
        fh.write(json.dumps({**report, "detail": detail_rows}, ensure_ascii=False) + "\n")

    print(f"[gate_cf] evaluated={overall['n_eval']} "
          f"censored(no_price/window/bars)={overall['censored_no_price']}/"
          f"{overall['censored_window']}/{overall['censored_bars']}")
    for g, e in list(gate_report.items())[:12]:
        hr = f" hit={e['hit_rate']:.0%}" if "hit_rate" in e else ""
        mean = f" mean={e['mean_signed_t5_pct']:+.2f}%" if "mean_signed_t5_pct" in e else ""
        nr = str(e["n_rejects"])
        ne = str(e["n_eval"])
        print(f"  {g:<28} n={nr:<4} eval={ne:<4}{hr}{mean}")
    print(f"[gate_cf] → {OUT_LATEST.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
