#!/usr/bin/env python3
"""L-E1 期望值台账 — DEB-20260816-long-expectancy 主实验 (§四, §九验收后落地)

口径 SSOT (§九 9.4 验收): source_type='PLAYBOOK_SIM' + 净额 pnl_net, 现役时代 2026-07-01 起,
多空同台账 (红线⑤)。只读: 计算并报告预注册判据状态, 不采取任何行动 (判据触发 → 走辩论)。

预注册判据 (§四, 写于落地前, 不得随意改):
  1. 供给调整辩论触发: 现役 LONG 累计 n≥300 且 每笔净期望 95%CI 上界 (双侧1.96, 保守) < 0
  2. 正桶保护条款: 任一分桶 n≥60 且 净期望 t≥1.5 → 该桶豁免于无差别削减
  3. 红线⑤: SHORT 同表产出, 防只审多头偏倚

维度: 月 × exit_reason × conf桶(ROUND 0.1) × entry_session × market_regime, 各分方向。
输出: data/long_expectancy_ledger_latest.json + 控制台摘要。
用法: ./venv/bin/python scripts/long_expectancy_ledger.py
"""
import json
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.utils.timezone import now_et  # noqa: E402

DB_PATH = ROOT / "data" / "ftseek_us.db"
OUT = ROOT / "data" / "long_expectancy_ledger_latest.json"

ERA_START = "2026-07-01"          # 现役时代 (SSOT §2.3)
N_TRIGGER = 300                    # 判据1: 供给辩论样本门槛
CI_Z = 1.96                        # 判据1: 双侧95% (保守取法)
PROTECT_N = 60                     # 判据2: 正桶保护样本门槛
PROTECT_T = 1.5                    # 判据2: 正桶保护 t 门槛


def _stats(values):
    """均值/样本标准差/t/95%CI上界 (净额每笔美元)"""
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": None, "t": None, "ci_upper": None}
    mean = sum(values) / n
    if n < 2:
        return {"n": n, "mean": round(mean, 2), "t": None, "ci_upper": None}
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    se = math.sqrt(var / n)
    t = mean / se if se > 0 else None
    ci_upper = mean + CI_Z * se
    return {"n": n, "mean": round(mean, 2),
            "t": round(t, 2) if t is not None else None,
            "ci_upper": round(ci_upper, 2)}


def _cell(trades):
    """聚合一个分桶: n/毛/净/胜率/几何/t (net 每笔)"""
    nets = [t["net"] for t in trades]
    s = _stats(nets)
    wins = sum(1 for t in trades if t["gross"] > 0)
    mfe = [t["mfe_pct"] for t in trades]
    mae = [t["mae_pct"] for t in trades]
    return {
        "n": s["n"],
        "gross": round(sum(t["gross"] for t in trades), 0),
        "net": round(sum(t["net"] for t in trades), 0),
        "win_rate": round(wins / len(trades), 3) if trades else None,
        "avg_mfe_pct": round(sum(mfe) / len(mfe) / 100, 4) if trades else None,  # 列为百分数→分数
        "avg_mae_pct": round(sum(mae) / len(mae) / 100, 4) if trades else None,
        "avg_net_per_trade": s["mean"],
        "t": s["t"],
        "ci_upper_net_per_trade": s["ci_upper"],
    }


def _bucket_key(trade, dim):
    if dim == "month":
        return trade["trade_date"][:7]
    if dim == "exit_reason":
        return trade["exit_reason"] or "unknown"
    if dim == "conf_bucket":
        return str(round(float(trade["signal_confidence"] or 0), 1))
    if dim == "entry_session":
        return trade["entry_session"] or "unlabeled"
    if dim == "market_regime":
        return trade["market_regime"] or "unknown"
    raise ValueError(dim)


def load_trades(db_path=str(DB_PATH), since=ERA_START):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT trade_date, direction, exit_reason, entry_session, market_regime, "
        "signal_confidence, realized_pnl, pnl_net, mfe_pct, mae_pct "
        "FROM trade_records WHERE source_type='PLAYBOOK_SIM' AND trade_date>=? "
        "AND exit_price>0", (since,)).fetchall()
    conn.close()
    trades = []
    for r in rows:
        gross = float(r["realized_pnl"] or 0)
        net = float(r["pnl_net"] if r["pnl_net"] is not None else gross)
        trades.append({
            "trade_date": r["trade_date"], "direction": r["direction"] or "?",
            "exit_reason": r["exit_reason"], "entry_session": r["entry_session"],
            "market_regime": r["market_regime"],
            "signal_confidence": float(r["signal_confidence"] or 0),
            "gross": gross, "net": net,
            "mfe_pct": float(r["mfe_pct"] or 0), "mae_pct": float(r["mae_pct"] or 0),
        })
    return trades


def build_ledger(trades):
    ledger = {"generated_at": now_et().isoformat(timespec="seconds"),
              "era_start": ERA_START,
              "criteria": {"supply_debate": f"LONG n>={N_TRIGGER} 且 net/笔 95%CI上界<0 (z={CI_Z})",
                           "bucket_protection": f"任一桶 n>={PROTECT_N} 且 t>={PROTECT_T} (net)"},
              "overall": {}, "dimensions": {}, "protected_buckets": []}

    for direction in ("LONG", "SHORT"):
        dt = [t for t in trades if t["direction"] == direction]
        if not dt:
            continue
        ledger["overall"][direction] = _cell(dt)
        dims = {}
        for dim in ("month", "exit_reason", "conf_bucket", "entry_session", "market_regime"):
            groups = defaultdict(list)
            for t in dt:
                groups[_bucket_key(t, dim)].append(t)
            dims[dim] = {k: _cell(v) for k, v in sorted(groups.items())}
        ledger["dimensions"][direction] = dims

    # 判据1: 供给辩论触发 (仅 LONG; SHORT 触发另案辩论, 不在此判)
    long_overall = ledger["overall"].get("LONG", {})
    n_ok = long_overall.get("n", 0) >= N_TRIGGER
    ci_ok = long_overall.get("ci_upper_net_per_trade") is not None \
        and long_overall["ci_upper_net_per_trade"] < 0
    ledger["supply_debate_trigger"] = {
        "n": long_overall.get("n", 0), "n_threshold": N_TRIGGER, "n_met": n_ok,
        "ci_upper": long_overall.get("ci_upper_net_per_trade"), "ci_met": ci_ok,
        "triggered": bool(n_ok and ci_ok),
        "note": "触发≠执行: 供给调整须另开辩论+预注册方案 (红线①)",
    }

    # 判据2: 正桶保护 (两方向所有维度统一扫描)
    protected = []
    for direction, dims in ledger["dimensions"].items():
        for dim, cells in dims.items():
            for key, c in cells.items():
                if c["n"] >= PROTECT_N and c["t"] is not None and c["t"] >= PROTECT_T:
                    protected.append({"direction": direction, "dim": dim, "bucket": key, **c})
    ledger["protected_buckets"] = protected
    return ledger


def main():
    trades = load_trades()
    ledger = build_ledger(trades)
    OUT.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[L-E1 ledger] {ledger['generated_at']} 现役 {ERA_START} 起, 已平仓 {len(trades)} 笔")
    for d, o in ledger["overall"].items():
        print(f"  {d:5s} n={o['n']:3d} 毛={o['gross']:8.0f} 净={o['net']:8.0f} "
              f"胜率={o['win_rate']:.0%} net/笔={o['avg_net_per_trade'] or 0:6.1f} "
              f"t={o['t']} CI上界={o['ci_upper_net_per_trade']}")
    trig = ledger["supply_debate_trigger"]
    print(f"  判据1 供给辩论: n_met={trig['n_met']} ({trig['n']}/{trig['n_threshold']}) "
          f"ci_met={trig['ci_met']} (上界={trig['ci_upper']}) → triggered={trig['triggered']}")
    if ledger["protected_buckets"]:
        for p in ledger["protected_buckets"]:
            print(f"  判据2 正桶保护: {p['direction']} {p['dim']}={p['bucket']} "
                  f"n={p['n']} t={p['t']} net={p['net']}")
    else:
        print("  判据2 正桶保护: 暂无满足 n≥60 且 t≥1.5 的桶")
    print(f"  → {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
