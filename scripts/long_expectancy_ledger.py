#!/usr/bin/env python3
"""L-E1 期望值台账 — DEB-20260816-long-expectancy 主实验 (§四, §九验收后落地)

口径 SSOT (§九 9.4 验收): source_type='PLAYBOOK_SIM' + 净额 pnl_net, 现役时代 2026-07-01 起,
多空同台账 (红线⑤)。只读: 计算并报告预注册判据状态, 不采取任何行动 (判据触发 → 走辩论)。

预注册判据 (§四; 2026-08-17 §十二修订 v2, 触发前修订, 依据 §十一 A2/A3/B1/B3):
  1. 供给调整辩论触发: 现役 LONG 累计 n≥300 且 **日级聚类 t < -2.0**
     (日级 = 按 trade_date 聚合当日 LONG 净额之和, t 为日净额序列的 t 值;
      交易级 t/CI 作共报诊断 — 同日多笔相关, 交易级 CI 伪重复偏窄)
  2. 正桶保护条款: 任一分桶 n≥60 且 净期望 t≥1.5 → 该桶豁免于无差别削减
     (桶级判别读毛额指标 avg_gross_per_trade, net 仅总账 — 费∝仓位, B3)
  3. 红线⑤: SHORT 同表产出, 防只审多头偏倚
  v1 (2026-08-16): 判据1 为交易级 CI上界<0; v2 改日级聚类 (§十一 A2), 修订时
  判据未触发 (n=214<300), 属触发前修订非事后择优。

维度: 月 × policy_period × exit_reason × conf桶(ROUND 0.1) × conf×source ×
entry_session × market_regime, 各分方向。
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
N_TRIGGER = 300                    # 判据1: 供给辩论样本门槛 (交易数)
DAY_T_TRIGGER = -2.0               # 判据1 v2: 日级聚类 t 门槛 (§十一 A2, 触发前修订)
CI_Z = 1.96                        # 共报诊断: 交易级双侧95% (保守取法)
PROTECT_N = 60                     # 判据2: 正桶保护样本门槛
PROTECT_T = 1.5                    # 判据2: 正桶保护 t 门槛
# 政策断裂点 (§十一 A3): 8/12 禁触窗 / 8/15 W_WEEK / 8/16晚 退出几何修复
POLICY_BREAKS = [("2026-08-12", "P1_no_trigger_window"),
                 ("2026-08-15", "P2_w_week"),
                 ("2026-08-17", "P3_post_exit_fix")]


def _policy_period(trade_date):
    period = "P0_base"
    for start, name in POLICY_BREAKS:
        if trade_date >= start:
            period = name
    return period


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
    """聚合一个分桶: n/毛/净/胜率/几何/t (net 每笔)。桶级判别读毛额 (B3)。"""
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
        "avg_gross_per_trade": round(sum(t["gross"] for t in trades) / len(trades), 2) if trades else None,
        "avg_mfe_pct": round(sum(mfe) / len(mfe) / 100, 4) if trades else None,  # 列为百分数→分数
        "avg_mae_pct": round(sum(mae) / len(mae) / 100, 4) if trades else None,
        "avg_net_per_trade": s["mean"],
        "t": s["t"],
        "ci_upper_net_per_trade": s["ci_upper"],
    }


def _bucket_key(trade, dim):
    if dim == "month":
        return trade["trade_date"][:7]
    if dim == "policy_period":
        return _policy_period(trade["trade_date"])
    if dim == "exit_reason":
        return trade["exit_reason"] or "unknown"
    if dim == "conf_bucket":
        # 2026-10-16 (DEB-20261016 L0-3): 精确值分桶, 不再 ROUND(.,1) 并桶 — 并桶会把
        # 0.68 并入 0.7 档产生伪读数 (审计曾因此把 9 月 0.7 档读成 +15.31, 实为 +20.24)。
        return str(float(trade["signal_confidence"] or 0))
    if dim == "conf_x_source":
        return f"{float(trade['signal_confidence'] or 0)}|{trade.get('source') or 'unknown'}"
    if dim == "entry_hour_policy":
        # §十四14.4: 入场时点类指纹必须带政策期拆分 (8/12 禁触窗是强断点)
        return f"{_policy_period(trade['trade_date'])}|{(trade.get('entry_time') or '????')[11:13]}h"
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
        "signal_confidence, realized_pnl, pnl_net, mfe_pct, mae_pct, metadata_json, entry_time "
        "FROM trade_records WHERE source_type='PLAYBOOK_SIM' AND trade_date>=? "
        "AND exit_price>0", (since,)).fetchall()
    # B1: 信号源 (deep_analysis/universe_expansion/...) 经 instruction 关联取回
    instr_source = {}
    try:
        for r in conn.execute("SELECT id, raw_instruction_json FROM playbook_instructions"):
            try:
                instr_source[r["id"]] = (json.loads(r["raw_instruction_json"] or "{}").get("source") or "")
            except Exception:
                instr_source[r["id"]] = ""
    except Exception:
        pass
    conn.close()
    trades = []
    for r in rows:
        gross = float(r["realized_pnl"] or 0)
        net = float(r["pnl_net"] if r["pnl_net"] is not None else gross)
        source = ""
        try:
            iid = json.loads(r["metadata_json"] or "{}").get("playbook_instruction_id")
            if iid:
                source = instr_source.get(iid, "")
        except Exception:
            pass
        trades.append({
            "trade_date": r["trade_date"], "direction": r["direction"] or "?",
            "exit_reason": r["exit_reason"], "entry_session": r["entry_session"],
            "market_regime": r["market_regime"], "entry_time": r["entry_time"] or "",
            "signal_confidence": float(r["signal_confidence"] or 0),
            "source": source or "unlinked",
            "gross": gross, "net": net,
            "mfe_pct": float(r["mfe_pct"] or 0), "mae_pct": float(r["mae_pct"] or 0),
        })
    return trades


def day_cluster_t(trades):
    """A2: 按交易日聚合当日净额之和, 返回日净额序列的 t 值 (聚类稳健口径)."""
    by_day = defaultdict(float)
    for t in trades:
        by_day[t["trade_date"]] += t["net"]
    return _stats(list(by_day.values()))


def build_ledger(trades):
    ledger = {"generated_at": now_et().isoformat(timespec="seconds"),
              "era_start": ERA_START,
              "criteria_version": "v2 (2026-08-17 §十二, 触发前修订, 依据 §十一 A2/A3/B1/B3)",
              "criteria": {"supply_debate": f"LONG n>={N_TRIGGER} 且 日级聚类t<{DAY_T_TRIGGER} (日净额SUM)",
                           "trade_level_diagnostic": f"交易级 net/笔 95%CI上界<0 (z={CI_Z}) 仅共报不触发",
                           "bucket_protection": f"任一桶 n>={PROTECT_N} 且 t>={PROTECT_T} (判别读毛额)"},
              "overall": {}, "dimensions": {}, "protected_buckets": []}

    for direction in ("LONG", "SHORT"):
        dt = [t for t in trades if t["direction"] == direction]
        if not dt:
            continue
        cell = _cell(dt)
        cell["day_cluster_t"] = day_cluster_t(dt)
        ledger["overall"][direction] = cell
        dims = {}
        for dim in ("month", "policy_period", "exit_reason", "conf_bucket",
                    "conf_x_source", "entry_hour_policy", "entry_session", "market_regime"):
            groups = defaultdict(list)
            for t in dt:
                groups[_bucket_key(t, dim)].append(t)
            dims[dim] = {k: _cell(v) for k, v in sorted(groups.items())}
        ledger["dimensions"][direction] = dims

    # 判据1 v2: 供给辩论触发 (仅 LONG; SHORT 触发另案辩论, 不在此判)
    long_overall = ledger["overall"].get("LONG", {})
    n_ok = long_overall.get("n", 0) >= N_TRIGGER
    day_t = (long_overall.get("day_cluster_t") or {}).get("t")
    day_t_ok = day_t is not None and day_t < DAY_T_TRIGGER
    ledger["supply_debate_trigger"] = {
        "n": long_overall.get("n", 0), "n_threshold": N_TRIGGER, "n_met": n_ok,
        "day_cluster_t": day_t, "day_t_threshold": DAY_T_TRIGGER, "day_t_met": day_t_ok,
        "trade_level_t": long_overall.get("t"),
        "trade_level_ci_upper": long_overall.get("ci_upper_net_per_trade"),
        "triggered": bool(n_ok and day_t_ok),
        "note": "触发≠执行: 供给调整须另开辩论+预注册方案 (红线①); 日级聚类=当日净额SUM的t (§十一A2)",
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

    print(f"[L-E1 ledger v2] {ledger['generated_at']} 现役 {ERA_START} 起, 已平仓 {len(trades)} 笔")
    for d, o in ledger["overall"].items():
        dt2 = (o.get("day_cluster_t") or {})
        print(f"  {d:5s} n={o['n']:3d} 毛={o['gross']:8.0f} 净={o['net']:8.0f} "
              f"胜率={o['win_rate']:.0%} net/笔={o['avg_net_per_trade'] or 0:6.1f} "
              f"t={o['t']} CI上界={o['ci_upper_net_per_trade']} "
              f"日级t={dt2.get('t')}(n={dt2.get('n')}天)")
    trig = ledger["supply_debate_trigger"]
    print(f"  判据1 供给辩论(v2): n_met={trig['n_met']} ({trig['n']}/{trig['n_threshold']}) "
          f"day_t_met={trig['day_t_met']} (日级t={trig['day_cluster_t']} vs {trig['day_t_threshold']}) "
          f"[交易级诊断 t={trig['trade_level_t']} CI上界={trig['trade_level_ci_upper']}] "
          f"→ triggered={trig['triggered']}")
    if ledger["protected_buckets"]:
        for p in ledger["protected_buckets"]:
            print(f"  判据2 正桶保护: {p['direction']} {p['dim']}={p['bucket']} "
                  f"n={p['n']} t={p['t']} net={p['net']}")
    else:
        print("  判据2 正桶保护: 暂无满足 n≥60 且 t≥1.5 的桶")
    print(f"  → {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
