#!/usr/bin/env python3
"""入场价可达性审计 (DEB-20261106 §38 / P1)。

问题：DA 给的 entry 有一半当天根本没被触及 ⇒ 论点无法落地。
本脚本用**历史真实卡片**（`data/da_checkpoint/*/US.*.json` 的 `_llm_output`）+
**真实 1min bar** 做三个入场规则的同口径对比，回答「不可达的那一半，改按盘前价直接进会赚还是亏」。

三个臂（**出场逻辑完全相同**，只有入场价不同）：
  card   现况：按卡片 entry 限价，未触及则不入场（不可达=0 收益，不计入胜率）
  market 盘前价：以盘前快照价直接进（假设开盘成交，用当日首根 bar 开盘价填充）
  hybrid 折中：entry 向盘前价收敛 50%（`(card+premkt)/2`），未触及同样不入场

出场：卡片自己的 stop/tp，按 1min bar 顺序走；同一根 bar 内先判止损再判止盈（保守）；
      都没打到则收盘平仓。费用按 7.6bps 双边扣除（与 §32 实测中位一致）。

输出：每臂 n / 可达率 / 触发率 / 命中率 / 平均收益 / 累计 bps / 中位 RR。
性质：只读；无 API 调用；不改任何交易逻辑。

用法: venv/bin/python scripts/entry_reach_audit.py [--json]
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
CKPT = ROOT / "data" / "da_checkpoint"
BARS_DIRS = (ROOT / "data" / "replay_bars", ROOT / "data" / "replay_bars_1m")
FEE_PCT = 0.076          # 双边费用，百分比 (7.6bps)
ERA_SPLIT = "2026-09-01"


def bars_of(symbol: str, day: str) -> Optional[List[list]]:
    sym = symbol.replace("US.", "")
    for d in BARS_DIRS:
        for name in (f"US_{sym}_{day}.json", f"{sym}_{day}.json"):
            p = d / name
            if p.exists():
                return json.loads(p.read_text())
    return None


def num(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return f if f > 0 else None
    except Exception:
        return None


def load_cards() -> List[dict]:
    out = []
    for p in sorted(glob.glob(str(CKPT / "*" / "*.json"))):
        if p.endswith(".raw.json") or p.endswith(".bak"):
            continue
        try:
            j = json.loads(Path(p).read_text())
        except Exception:
            continue
        lo = j.get("_llm_output") or {}
        ep = lo.get("execution_plan") or {}
        direction = str(lo.get("direction") or "").upper()
        if direction not in ("BUY", "SELL_SHORT"):
            continue
        entry = num(ep.get("entry_price"))
        stop = num(ep.get("stop_loss_price"))
        tp = num(ep.get("take_profit_price"))
        premkt = num((j.get("snapshot") or {}).get("price")) or num((j.get("technical") or {}).get("price"))
        day = Path(p).parent.name
        if not (entry and stop and tp and premkt):
            continue
        out.append({"day": day, "symbol": j.get("symbol"), "direction": direction,
                    "entry": entry, "stop": stop, "tp": tp, "premkt": premkt,
                    "rr": abs(tp - entry) / abs(entry - stop) if abs(entry - stop) > 0 else None})
    return out


def exit_pct(bars: List[list], direction: str, entry: float, stop: float, tp: float,
             start_idx: int = 0) -> Optional[float]:
    """按 1min bar 顺序模拟出场，返回扣除费用后的百分比收益。"""
    sign = 1 if direction == "BUY" else -1
    for b in bars[start_idx:]:
        hi, lo, close = b[2], b[3], b[4]
        if sign > 0:
            if lo <= stop:
                return sign * (stop - entry) / entry * 100 - FEE_PCT
            if hi >= tp:
                return sign * (tp - entry) / entry * 100 - FEE_PCT
        else:
            if hi >= stop:
                return sign * (stop - entry) / entry * 100 - FEE_PCT
            if lo <= tp:
                return sign * (tp - entry) / entry * 100 - FEE_PCT
    close = bars[-1][4]
    return sign * (close - entry) / entry * 100 - FEE_PCT


def run_arm(cards: List[dict], bars_map: Dict[str, List[list]], arm: str,
            timeout_min: int = 0) -> dict:
    recs = []
    for c in cards:
        b = bars_map.get(f"{c['symbol']}|{c['day']}")
        if not b or len(b) < 30:
            continue
        sign = 1 if c["direction"] == "BUY" else -1
        lo_all = min(x[3] for x in b)
        hi_all = max(x[2] for x in b)
        if arm == "card":
            entry = c["entry"]
        elif arm == "hybrid":
            entry = (c["entry"] + c["premkt"]) / 2
        else:  # market: 开盘价成交
            entry = b[0][1] if c["premkt"] else c["premkt"]
        # 停止/目标按入场价平移 (保持卡片的风险距离), 否则 market 臂的 RR 会被扭曲
        shift = entry - c["entry"]
        stop = c["stop"] + shift if arm != "card" else c["stop"]
        tp = c["tp"] + shift if arm != "card" else c["tp"]
        reachable = lo_all <= entry <= hi_all
        if not reachable:
            # timeout-then-market: 前 timeout_min 根 bar 未触及限价 → 按该 bar 收盘价市价进
            if timeout_min and len(b) > timeout_min:
                entry = b[timeout_min][4]
                shift = entry - c["entry"]
                stop = c["stop"] + shift
                tp = c["tp"] + shift
                reachable = True
            else:
                recs.append({"day": c["day"], "symbol": c["symbol"], "reach": False, "pct": None})
                continue
        # 从触及该价的第一根 bar 起算出场 (简化: 用第一根 high/low 命中的 bar)
        start = 0
        for i, x in enumerate(b):
            if (x[3] <= entry <= x[2]):
                start = i
                break
        pct = exit_pct(b, c["direction"], entry, stop, tp, start)
        recs.append({"day": c["day"], "symbol": c["symbol"], "reach": True, "pct": pct})
    got = [r for r in recs if r["reach"]]
    pcts = [r["pct"] for r in got if r["pct"] is not None]
    return {"arm": arm, "n": len(recs), "reach": len(got),
            "reach_pct": round(100 * len(got) / len(recs), 1) if recs else 0,
            "hit_pct": round(100 * sum(1 for p in pcts if p > 0) / len(pcts), 1) if pcts else 0,
            "mean_pct": round(statistics.mean(pcts), 3) if pcts else None,
            "median_pct": round(statistics.median(pcts), 3) if pcts else None,
            "sum_bps": round(sum(pcts) * 100, 1) if pcts else 0,
            "recs": recs}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    cards = load_cards()
    bars_map = {}
    for c in cards:
        b = bars_of(c["symbol"], c["day"])
        if b:
            bars_map[f"{c['symbol']}|{c['day']}"] = b
    print("可用卡片 %d 张 (有 bar %d 张)" % (len(cards), len(bars_map)))
    res = {arm: run_arm(cards, bars_map, arm) for arm in ("card", "market", "hybrid")}
    for t in (15, 30, 60):
        res[f"wait{t}m"] = run_arm(cards, bars_map, "card", timeout_min=t)

    if a.json:
        print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "recs"} for k, v in res.items()},
                         ensure_ascii=False, indent=2))
        return 0

    print("\n%-8s %5s %7s %7s %7s %9s %9s" % ("入场规则", "n", "可达%", "命中%", "均收益%", "中位%", "累计bps"))
    for arm in ("card", "hybrid", "wait15m", "wait30m", "wait60m", "market"):
        r = res[arm]
        print("%-8s %5d %6.1f%% %6.1f%% %8s %9s %9s" % (
            arm, r["n"], r["reach_pct"], r["hit_pct"],
            ("%.3f" % r["mean_pct"]) if r["mean_pct"] is not None else "n/a",
            ("%.3f" % r["median_pct"]) if r["median_pct"] is not None else "n/a", r["sum_bps"]))
    print("\n注: market/hybrid 臂的 stop/tp 随入场价平移, 保持卡片原始风险距离; "
          "费用已扣 7.6bps 双边。")

    # 关键子集: 卡片不可达、但 market 能进的单 —— 它们本来是亏还是赚
    miss = [c for c in cards
            if f"{c['symbol']}|{c['day']}" in bars_map
            and not (min(x[3] for x in bars_map[f"{c['symbol']}|{c['day']}"])
                     <= c["entry"] <= max(x[2] for x in bars_map[f"{c['symbol']}|{c['day']}"]))]
    print("\n卡片不可达 %d 张 ⇒ 若按盘前价进场的结果:" % len(miss))
    if miss:
        sub = run_arm(miss, bars_map, "market")
        print("  可达 %d/%d | 命中 %s%% | 均收益 %s%% | 累计 %s bps" % (
            sub["reach"], sub["n"], sub["hit_pct"], sub["mean_pct"], sub["sum_bps"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())