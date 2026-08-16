#!/usr/bin/env python3
"""MAE/MFE 退出校准刷新 — L-E3 前置 (DEB-20260816-long-expectancy)

2026-08-16 重建说明: 原 app/risk/exit_calibration_analyzer.py + scripts/run_exit_calibration.py
于 768e443f 删除 (0产出+0消费方), 且其反馈环 (ExitMonitor 启动时合并 exit_calibration.json
进 policy_dict) 已随 subtraction 移除。本脚本只恢复**分析侧**:
  - 分类阈值沿用旧版 (_MAE_NOISE 2% / _GIVEBACK_RATIO 2.0), 保持与 2026-06-06 历史快照可比
  - 数据源改用现行 trade_records.mfe_pct / mae_pct 直列 (旧版从 metadata_json 反推)
  - 输出: config/exit_calibration.json (同 schema 供历史对比)
         + data/exit_rule_expectancy_latest.json (exit_reason×方向 期望值表, 供 L-E3)
  - **不**写任何退出参数 — 红线① (调参需预注册判据, DEB-long-expectancy §五)

用法: ./venv/bin/python scripts/exit_calibration_refresh.py [--days 30] [--all-sources]
"""
import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.utils.timezone import now_et  # noqa: E402

DB_PATH = ROOT / "data" / "ftseek_us.db"
OUT_CONFIG = ROOT / "config" / "exit_calibration.json"
OUT_RULES = ROOT / "data" / "exit_rule_expectancy_latest.json"

_MAE_NOISE_THRESHOLD = 0.02
_GIVEBACK_RATIO = 2.0


def _load_trades(db_path: str, days: int, source_filter: bool):
    cutoff = (now_et() - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT trade_date, symbol, direction, source_type, exit_reason,
               realized_pnl, pnl_net, fees_est, pnl_pct, mfe_pct, mae_pct,
               signal_confidence, holding_hours
        FROM trade_records
        WHERE exit_price IS NOT NULL AND exit_price > 0
          AND trade_date >= ?
          AND realized_pnl IS NOT NULL
    """
    params = [cutoff]
    if source_filter:
        sql += " AND source_type = 'PLAYBOOK_SIM'"
        params = [cutoff]
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    trades = []
    for r in rows:
        t = dict(r)
        # 列单位: mfe_pct/mae_pct 存百分数 (4.854=4.85%), pnl_pct 存分数 — 统一归一为分数
        t["mae_pct"] = float(t.get("mae_pct") or 0) / 100.0
        t["mfe_pct"] = float(t.get("mfe_pct") or 0) / 100.0
        trades.append(t)
    return trades


def _classify(t: dict) -> str:
    """沿用 2026-06-06 旧版三分类 (口径连续性, 阈值不变)"""
    mae = float(t.get("mae_pct") or 0)
    mfe = float(t.get("mfe_pct") or 0)
    pnl = float(t.get("pnl_pct") or 0)
    if mae > _MAE_NOISE_THRESHOLD and t.get("realized_pnl", 0) > 0:
        return "noise_shakeout"
    if mfe > 0 and pnl > 0 and mfe > _GIVEBACK_RATIO * pnl:
        return "profit_giveback"
    if mae >= 0 or mfe >= 0:
        return "well_calibrated"
    return "unclassified"


def _bucket_conf(c: float) -> str:
    if not c or c <= 0:
        return "unknown"
    if c < 0.45:
        return "low"
    if c < 0.75:
        return "mid"
    return "high"


def build_report(trades: list, days: int) -> dict:
    cats = [_classify(t) for t in trades]
    n = len(trades)
    analyzed = [t for t, c in zip(trades, cats) if c != "unclassified"]

    def _avg(key, subset):
        vals = [float(t.get(key) or 0) for t in subset]
        return sum(vals) / len(vals) if vals else 0.0

    wins = [t for t in analyzed if float(t["realized_pnl"] or 0) > 0]
    report = {
        "generated_at": now_et().isoformat(timespec="seconds"),
        "period_days": days,
        "source_filter": "PLAYBOOK_SIM",
        "total_trades": n,
        "analyzed_trades": len(analyzed),
        "noise_shakeout_count": cats.count("noise_shakeout"),
        "profit_giveback_count": cats.count("profit_giveback"),
        "well_calibrated_count": cats.count("well_calibrated"),
        "unclassified_count": cats.count("unclassified"),
        "avg_mae_pct": round(_avg("mae_pct", analyzed), 4),
        "avg_mfe_pct": round(_avg("mfe_pct", analyzed), 4),
        "avg_pnl_pct": round(_avg("pnl_pct", analyzed), 4),
        "win_rate": round(len(wins) / len(analyzed), 4) if analyzed else 0.0,
    }
    report["noise_shakeout_pct"] = round(
        100 * report["noise_shakeout_count"] / len(analyzed), 2) if analyzed else 0.0
    report["profit_giveback_pct"] = round(
        100 * report["profit_giveback_count"] / len(analyzed), 2) if analyzed else 0.0
    report["well_calibrated_pct"] = round(
        100 * report["well_calibrated_count"] / len(analyzed), 2) if analyzed else 0.0

    # ── 建议 1: 退出原因审查 (净期望为负且 n≥3 的规则) ──
    rule_review = []
    for key, stats in _rule_table(trades).items():
        reason, direction = key
        if stats["n"] >= 3 and stats["net"] < 0:
            rule_review.append({
                "reason": reason, "direction": direction, "n": stats["n"],
                "net": stats["net"], "win_rate": stats["win_rate"],
            })
    rule_review.sort(key=lambda x: x["net"])

    # ── 建议 2: conf 分层 (校准反向监测, DEB §2.7) ──
    tiers = defaultdict(lambda: {"count": 0, "wins": 0, "pnl": 0.0})
    for t in trades:
        b = _bucket_conf(float(t.get("signal_confidence") or 0))
        tiers[b]["count"] += 1
        tiers[b]["wins"] += 1 if float(t["realized_pnl"] or 0) > 0 else 0
        tiers[b]["pnl"] += float(t.get("pnl_net") or t.get("realized_pnl") or 0)
    conf_stats = {
        k: {"count": v["count"],
            "win_rate": round(v["wins"] / v["count"], 2) if v["count"] else 0.0,
            "net_pnl": round(v["pnl"], 0)}
        for k, v in sorted(tiers.items())
    }

    report["recommendations"] = {
        "exit_reason_review": {
            "action": "review" if rule_review else "ok",
            "bad_reasons": rule_review,
            "reason": "净期望为负且 n≥3 的退出规则 (L-E3 复查候选, 仅分析不调参)",
        },
        "confidence_tier_stats": conf_stats,
    }
    return report


def _rule_table(trades: list) -> dict:
    agg = defaultdict(lambda: {"n": 0, "gross": 0.0, "net": 0.0, "wins": 0,
                               "mfe": 0.0, "mae": 0.0, "hold": 0.0})
    for t in trades:
        key = (t.get("exit_reason") or "unknown", t.get("direction") or "?")
        a = agg[key]
        a["n"] += 1
        a["gross"] += float(t["realized_pnl"] or 0)
        a["net"] += float(t.get("pnl_net") or t.get("realized_pnl") or 0)
        a["wins"] += 1 if float(t["realized_pnl"] or 0) > 0 else 0
        a["mfe"] += float(t.get("mfe_pct") or 0)
        a["mae"] += float(t.get("mae_pct") or 0)
        a["hold"] += float(t.get("holding_hours") or 0)
    out = {}
    for key, a in agg.items():
        n = a["n"]
        out[key] = {
            "n": n,
            "gross": round(a["gross"], 0),
            "net": round(a["net"], 0),
            "win_rate": round(a["wins"] / n, 2),
            # 百分数单位 (4.85=4.85%), 供 L-E3 人读
            "avg_mfe_pct": round(a["mfe"] / n * 100, 3),
            "avg_mae_pct": round(a["mae"] / n * 100, 3),
            "avg_hold_hours": round(a["hold"] / n, 1),
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--all-sources", action="store_true",
                    help="不过滤 PLAYBOOK_SIM (含退役源, 仅诊断用)")
    args = ap.parse_args()

    trades = _load_trades(args.db, args.days, source_filter=not args.all_sources)
    if not trades:
        print(f"[exit-calibration] 最近 {args.days} 天无已平仓交易, 不刷新")
        return

    report = build_report(trades, args.days)
    OUT_CONFIG.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    rules = {f"{k[0]}|{k[1]}": v for k, v in sorted(
        _rule_table(trades).items(), key=lambda kv: kv[1]["net"])}
    OUT_RULES.write_text(json.dumps({
        "generated_at": report["generated_at"],
        "period_days": args.days,
        "source_filter": "ALL" if args.all_sources else "PLAYBOOK_SIM",
        "rules": rules,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[exit-calibration] 刷新完成 ({report['generated_at']}) "
          f"n={report['analyzed_trades']}/{report['total_trades']} "
          f"胜率={report['win_rate']:.0%} "
          f"MAE={report['avg_mae_pct']:.2%} MFE={report['avg_mfe_pct']:.2%}")
    print(f"  → {OUT_CONFIG.relative_to(ROOT)}")
    print(f"  → {OUT_RULES.relative_to(ROOT)} ({len(rules)} 条规则)")
    bad = report["recommendations"]["exit_reason_review"]["bad_reasons"]
    for b in bad[:5]:
        print(f"  [review] {b['direction']:5s} {b['reason']:22s} n={b['n']:3d} "
              f"net=${b['net']:.0f} 胜率={b['win_rate']:.0%}")


if __name__ == "__main__":
    main()
