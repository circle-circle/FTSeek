#!/usr/bin/env python3
"""
Daily IC Analysis — 按 LLM 特征分组统计信号质量
===============================================

每日 08:00 ET (盘前) 运行，输出:
1. 总体 IC (by trade_mode, strategy, direction)
2. 因子级 IC (f1_trend ~ f5_lifecycle)
3. LLM 特征分组 IC:
   - debate_consensus: Hermes+Bull 同向的信号
   - debate_conflict: 方向冲突的信号
   - high_confidence: confidence > 0.7
   - enrichment_cited: rationale 中引用了数据的信号
   - causal_chain: rationale 含因果链的信号
4. Regime 分组 IC
5. 趋势报告 (最近 7 天 IC 变化)

输出: data/ic_analysis_latest.json + 飞书/钉钉通知

用法:
    python3 scripts/daily_ic_analysis.py [--notify] [--days 30]
"""

import json
import logging
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

logger = logging.getLogger("daily_ic_analysis")

PROJECT_ROOT = Path(__file__).parent.parent
DB_SIGNALS = PROJECT_ROOT / "data" / "signal_snapshots.db"
DB_TRADES = PROJECT_ROOT / "data" / "ftseek_us.db"
DB_LIVE = PROJECT_ROOT / "data" / "ftseek_us_live.db"
OUTPUT_PATH = PROJECT_ROOT / "data" / "ic_analysis_latest.json"

# ═══════════════════════════════════════════════════════════════
# IC Calculation
# ═══════════════════════════════════════════════════════════════

def spearman_ic(strengths, returns):
    """Spearman rank IC between signal values and forward returns"""
    pairs = [(s, r) for s, r in zip(strengths, returns) if s is not None and r is not None]
    if len(pairs) < 5:
        return None, None, len(pairs)
    s_vals, r_vals = zip(*pairs)
    s_ranks = np.argsort(np.argsort(s_vals)).astype(float)
    r_ranks = np.argsort(np.argsort(r_vals)).astype(float)
    n = len(s_ranks)
    d2 = np.sum((s_ranks - r_ranks) ** 2)
    ic = 1 - (6 * d2) / (n * (n**2 - 1)) if n > 1 else 0
    wins = sum(1 for s, r in pairs
               if (s > np.median(s_vals) and r > 0) or
                  (s <= np.median(s_vals) and r <= 0))
    wr = wins / len(pairs)
    return round(float(ic), 4), round(float(wr), 4), len(pairs)


# ═══════════════════════════════════════════════════════════════
# Data Loading
# ═══════════════════════════════════════════════════════════════

def load_signals(days=90):
    """Load signal snapshots with forward returns"""
    conn = sqlite3.connect(str(DB_SIGNALS))
    conn.row_factory = sqlite3.Row
    try:
        from app.utils.timezone import now_et as _now_et
        cutoff = (_now_et() - timedelta(days=days)).strftime("%Y-%m-%d")
    except Exception:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute("""
        SELECT * FROM signal_snapshots
        WHERE fwd_ret_t1 IS NOT NULL
          AND trade_date >= ?
    """, (cutoff,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def load_trades(days=90):
    """Load trade records with PnL"""
    conn = sqlite3.connect(str(DB_TRADES))
    conn.row_factory = sqlite3.Row
    try:
        from app.utils.timezone import now_et as _now_et
        cutoff = (_now_et() - timedelta(days=days)).strftime("%Y-%m-%d")
    except Exception:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute("""
        SELECT * FROM trade_records
        WHERE pnl_pct IS NOT NULL
          AND created_at >= ?
          AND trade_mode != 'live'
    """, (cutoff,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def load_debate_logs(days=30):
    """Load debate engine logs"""
    debates_dir = PROJECT_ROOT / "data" / "debates"
    if not debates_dir.exists():
        return []
    cutoff = datetime.now() - timedelta(days=days)
    logs = []
    for f in debates_dir.glob("unified_*.json"):
        try:
            data = json.loads(f.read_text())
            ts = datetime.fromisoformat(data.get("timestamp", ""))
            if ts >= cutoff:
                logs.append(data)
        except Exception:
            pass
    # Also load old format debate files
    for f in debates_dir.glob("debate_*.json"):
        try:
            data = json.loads(f.read_text())
            ts_str = data.get("timestamp", "")
            if ts_str:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00").split("+")[0])
                if ts.replace(tzinfo=None) >= cutoff:
                    logs.append(data)
        except Exception:
            pass
    return logs


# ═══════════════════════════════════════════════════════════════
# Analysis Dimensions
# ═══════════════════════════════════════════════════════════════

def analyze_by_group(signals, key_fn, label):
    """Group signals and compute IC for each group"""
    groups = defaultdict(list)
    for s in signals:
        k = key_fn(s)
        if k is not None:
            groups[k].append(s)

    results = {}
    for k, g in sorted(groups.items(), key=lambda x: str(x[0])):
        strengths = [s["strength"] for s in g]
        rets_t1 = [s["fwd_ret_t1"] for s in g if s.get("fwd_ret_t1") is not None]
        ic_t1, wr_t1, n_t1 = spearman_ic(strengths[:len(rets_t1)], rets_t1) if rets_t1 else (None, None, 0)

        rets_t5 = [s["fwd_ret_t5"] for s in g if s.get("fwd_ret_t5") is not None]
        ic_t5, wr_t5, n_t5 = spearman_ic(strengths[:len(rets_t5)], rets_t5) if rets_t5 else (None, None, 0)

        results[str(k)] = {
            "n": len(g),
            "ic_t1": ic_t1, "wr_t1": wr_t1,
            "ic_t5": ic_t5, "wr_t5": wr_t5,
            "avg_ret_t1": round(float(np.mean(rets_t1)), 4) if rets_t1 else None,
        }
    return results


def analyze_factors(trades):
    """Factor-level IC from trade records"""
    factors = ["signal_strength", "signal_confidence", "consensus_level",
               "f1_trend", "f2_momentum", "f3_volume", "f4_orderflow", "f5_lifecycle"]
    results = {}
    for f in factors:
        vals = [t[f] for t in trades if t.get(f) is not None]
        rets = [t["pnl_pct"] for t in trades if t.get(f) is not None and t.get("pnl_pct") is not None]
        ic, wr, n = spearman_ic(vals, rets)
        results[f] = {"ic": ic, "wr": wr, "n": n}
    return results


def analyze_llm_features(signals, trades):
    """
    LLM 特征分组 IC:
    - debate_consensus: Hermes+Bear 同向
    - debate_conflict: 方向冲突
    - high_confidence: confidence > 0.7
    - long_only: LONG 方向信号
    - short_only: SHORT 方向信号
    """
    results = {}

    # By direction
    for d in ["LONG", "SHORT"]:
        group = [s for s in signals if s.get("direction") == d]
        if group:
            strengths = [s["strength"] for s in group]
            rets = [s["fwd_ret_t1"] for s in group if s.get("fwd_ret_t1") is not None]
            ic, wr, n = spearman_ic(strengths[:len(rets)], rets) if rets else (None, None, 0)
            results[f"direction_{d}"] = {
                "n": len(group), "ic_t1": ic, "wr_t1": wr,
                "avg_ret": round(float(np.mean(rets)), 4) if rets else None,
            }

    # By confidence
    for lo, hi, label in [(0, 0.4, "low_conf"), (0.4, 0.7, "mid_conf"), (0.7, 1.01, "high_conf")]:
        group = [s for s in signals if lo <= (s.get("confidence") or 0) < hi]
        if group:
            strengths = [s["strength"] for s in group]
            rets = [s["fwd_ret_t1"] for s in group if s.get("fwd_ret_t1") is not None]
            ic, wr, n = spearman_ic(strengths[:len(rets)], rets) if rets else (None, None, 0)
            results[f"confidence_{label}"] = {
                "n": len(group), "ic_t1": ic, "wr_t1": wr,
                "avg_ret": round(float(np.mean(rets)), 4) if rets else None,
            }

    # By strategy
    strats = set(s.get("strategy_id") or "unknown" for s in signals)
    for st in strats:
        group = [s for s in signals if (s.get("strategy_id") or "unknown") == st]
        if len(group) >= 5:
            strengths = [s["strength"] for s in group]
            rets = [s["fwd_ret_t1"] for s in group if s.get("fwd_ret_t1") is not None]
            ic, wr, n = spearman_ic(strengths[:len(rets)], rets) if rets else (None, None, 0)
            results[f"strategy_{st}"] = {
                "n": len(group), "ic_t1": ic, "wr_t1": wr,
                "avg_ret": round(float(np.mean(rets)), 4) if rets else None,
            }

    # Trade-level: by strategy PnL
    trade_by_strat = defaultdict(list)
    for t in trades:
        s = t.get("strategy_id") or t.get("source_type") or "unknown"
        trade_by_strat[s].append(t)

    for st, tg in trade_by_strat.items():
        pnls = [t["pnl_pct"] for t in tg if t.get("pnl_pct") is not None]
        if pnls:
            wr = sum(1 for p in pnls if p > 0) / len(pnls)
            results[f"trade_strategy_{st}"] = {
                "n": len(pnls),
                "wr": round(wr, 4),
                "avg_pnl": round(float(np.mean(pnls)), 4),
                "total_pnl": round(float(sum(pnls)), 4),
            }

    return results


def analyze_regime(trades):
    """PnL by market regime"""
    by_regime = defaultdict(list)
    for t in trades:
        r = t.get("market_regime") or "UNKNOWN"
        by_regime[r].append(t)

    results = {}
    for r, g in by_regime.items():
        pnls = [t["pnl_pct"] for t in g if t.get("pnl_pct") is not None]
        if pnls:
            wr = sum(1 for p in pnls if p > 0) / len(pnls)
            results[r] = {
                "n": len(pnls),
                "wr": round(wr, 4),
                "avg_pnl": round(float(np.mean(pnls)), 4),
                "total_pnl": round(float(sum(pnls)), 4),
            }
    return results


# ═══════════════════════════════════════════════════════════════
# Report Generation
# ═══════════════════════════════════════════════════════════════

def generate_report(days=90):
    """Run full IC analysis and generate report"""
    print(f"{'='*60}")
    print(f"  FTSeek-US Daily IC Analysis")
    print(f"  Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Window: {days} days")
    print(f"{'='*60}")

    signals = load_signals(days)
    trades = load_trades(days)
    debates = load_debate_logs(min(days, 30))

    print(f"\nData: {len(signals)} signals, {len(trades)} trades, {len(debates)} debate logs")

    report = {
        "generated_at": datetime.now().isoformat(),
        "window_days": days,
        "data_counts": {
            "signals": len(signals),
            "trades": len(trades),
            "debate_logs": len(debates),
        },
    }

    # 1. Overall IC by trade_mode
    print(f"\n{'─'*60}")
    print("1. IC by Trade Mode")
    print(f"{'─'*60}")
    mode_ic = analyze_by_group(signals, lambda s: s.get("trade_mode"), "trade_mode")
    report["ic_by_mode"] = mode_ic
    for k, v in mode_ic.items():
        print(f"  {k}: N={v['n']} IC_t1={v['ic_t1']} WR={v['wr_t1']} AvgRet={v['avg_ret_t1']}")

    # 2. IC by direction
    print(f"\n{'─'*60}")
    print("2. IC by Direction")
    print(f"{'─'*60}")
    dir_ic = analyze_by_group(signals, lambda s: s.get("direction"), "direction")
    report["ic_by_direction"] = dir_ic
    for k, v in dir_ic.items():
        print(f"  {k}: N={v['n']} IC_t1={v['ic_t1']} WR={v['wr_t1']} AvgRet={v['avg_ret_t1']}")

    # 3. Factor-level IC
    print(f"\n{'─'*60}")
    print("3. Factor-Level IC (trades)")
    print(f"{'─'*60}")
    factor_ic = analyze_factors(trades)
    report["factor_ic"] = factor_ic
    for k, v in factor_ic.items():
        print(f"  {k}: IC={v['ic']} WR={v['wr']} N={v['n']}")

    # 4. LLM feature groups
    print(f"\n{'─'*60}")
    print("4. LLM Feature Group IC")
    print(f"{'─'*60}")
    llm_ic = analyze_llm_features(signals, trades)
    report["llm_feature_ic"] = llm_ic
    for k, v in llm_ic.items():
        ic = v.get("ic_t1") or v.get("ic") or "—"
        wr = v.get("wr_t1") or v.get("wr") or "—"
        extra = f"AvgRet={v.get('avg_ret')}" if "avg_ret" in v else f"TotalPnL={v.get('total_pnl', '—')}"
        print(f"  {k}: N={v['n']} IC={ic} WR={wr} {extra}")

    # 5. Regime PnL
    print(f"\n{'─'*60}")
    print("5. PnL by Market Regime")
    print(f"{'─'*60}")
    regime_pnl = analyze_regime(trades)
    report["regime_pnl"] = regime_pnl
    for k, v in regime_pnl.items():
        print(f"  {k}: N={v['n']} WR={v['wr']:.1%} AvgPnL={v['avg_pnl']:.2%} Total={v['total_pnl']:.2%}")

    # 6. Key findings
    findings = []
    for k, v in llm_ic.items():
        ic = v.get("ic_t1") or v.get("ic")
        if ic is not None and abs(ic) > 0.1:
            direction = "positive ✅" if ic > 0 else "negative ❌"
            findings.append(f"{k}: IC={ic} ({direction})")
    for k, v in regime_pnl.items():
        if v["total_pnl"] > 10:
            findings.append(f"Regime {k}: profitable (+{v['total_pnl']:.1f}%)")
        elif v["total_pnl"] < -10:
            findings.append(f"Regime {k}: LOSING ({v['total_pnl']:.1f}%)")

    report["key_findings"] = findings
    print(f"\n{'─'*60}")
    print("6. Key Findings")
    print(f"{'─'*60}")
    for f in findings:
        print(f"  • {f}")

    # 7. Recommendations
    recs = []
    # LONG vs SHORT
    long_ic = llm_ic.get("direction_LONG", {}).get("ic_t1")
    short_ic = llm_ic.get("direction_SHORT", {}).get("ic_t1")
    if long_ic and long_ic > 0.1:
        recs.append("✅ LONG 信号有预测力 (IC={}), 应优先做多".format(long_ic))
    if short_ic and short_ic < -0.05:
        recs.append("❌ SHORT 信号 IC={}, 做空逻辑需要重构".format(short_ic))

    # HIGH_VOL
    hv = regime_pnl.get("HIGH_VOL", {})
    if hv and hv.get("total_pnl", 0) < -10:
        recs.append("🚨 HIGH_VOL regime 总亏 {}%, 必须在此 regime 下停止交易".format(hv["total_pnl"]))

    # voting_llm
    vl = llm_ic.get("trade_strategy_voting_llm", {})
    if vl and vl.get("total_pnl", 0) < -10:
        recs.append("🚨 voting_llm 策略总亏 {}%, LLM 投票逻辑需要停止或重构".format(vl["total_pnl"]))

    # Confidence calibration
    hc = llm_ic.get("confidence_high_conf", {})
    lc = llm_ic.get("confidence_low_conf", {})
    if hc.get("ic_t1", 0) > lc.get("ic_t1", 0):
        recs.append("✅ 高置信度信号 IC 高于低置信度, 信心度校准有效")

    report["recommendations"] = recs
    print(f"\n{'─'*60}")
    print("7. Recommendations")
    print(f"{'─'*60}")
    for r in recs:
        print(f"  • {r}")

    # Save
    with open(str(OUTPUT_PATH), "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n📁 Report saved: {OUTPUT_PATH}")

    return report


def format_notification(report):
    """Format report as concise notification"""
    findings = report.get("key_findings", [])
    recs = report.get("recommendations", [])

    lines = [
        f"📊 FTSeek-US 每日 IC 分析报告",
        f"📅 {report['generated_at'][:10]} | 窗口: {report['window_days']}天",
        f"📈 信号: {report['data_counts']['signals']} | 交易: {report['data_counts']['trades']}",
        "",
        "🔑 关键发现:",
    ]
    for f in findings[:5]:
        lines.append(f"  • {f}")

    if recs:
        lines.append("")
        lines.append("📋 行动建议:")
        for r in recs[:5]:
            lines.append(f"  • {r}")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90, help="Analysis window in days")
    parser.add_argument("--notify", action="store_true", help="Send notification")
    parser.add_argument("--json", action="store_true", help="Output JSON only")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    report = generate_report(args.days)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))

    if args.notify:
        msg = format_notification(report)
        print("\n" + "=" * 60)
        print(msg)
        # Try to send via Hermes notification system
        try:
            from app.notification.notifier import send_notification
            send_notification(msg, level="info")
        except Exception:
            pass
