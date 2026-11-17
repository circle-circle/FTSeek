#!/usr/bin/env python3
"""RFC-209 DA 降频 Pivot 前瞻结算器（record-only，零交易行为变更）。

只读承诺：sqlite `mode=ro`（复用 pnl_expectancy_pivot_analysis._connect/_load），
仅写只读分析产物 data/shadow/da_frequency_cap_pivot_latest.json；不下单、不改生产闸/配置。

预注册（RFC-209 §3，窗内冻结，改=重裁）：
  cohort 起点 2026-11-16（前瞻，不复用探索窗）；人群 owner_strategy=playbook_sim ∧ A9 干净；
  动作=每日按 entry_time 取前 N 笔，N∈{2,3}（固定）；对照=不限频；
  样本门 n≥30 交易日；毕业=任一 N 的 capped 绝对笔均净 bps>0，且「capped−uncapped」
  日聚类 80% CI 下界>0、mean>MDE80 → 提请设生产频帽（另立 DEB+用户裁）；
  两 N 的差值 CI 均显著为负 → FREQUENCY_CAP_HARMFUL；其余不可分辨。
人群精化（2026-11-14 复查修正，cohort 未起 days=0）：恢复补录的 MFE/regime 虽受污染，
但其真实成交顺序与净盈亏仍属实际 DA 组合，降频对照必须保留；仅排除补偿立即平仓与
人工收养行。死单率/edge 特征研究仍按 scripts/utils/da_sample.py 分层，不混用尺子。

日聚类口径与仓内同源（risk_size_tier_forward_eval._day_cluster）：
  Z80=1.2815515655446004；MDE80=(Z80+Z_POWER80)×se；等权日均。

用法：PYTHONPATH=. venv/bin/python scripts/settle_da_frequency_cap_pivot.py
回滚：删产物 + 注册表项 da_frequency_cap_pivot_shadow；无需重启。
"""
import importlib.util
import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.utils.da_sample import split_frequency_population

ROOT = Path(__file__).resolve().parents[1]
ANALYZER = ROOT / "scripts" / "pnl_expectancy_pivot_analysis.py"
OUTPUT = ROOT / "data" / "shadow" / "da_frequency_cap_pivot_latest.json"

COHORT_START = "2026-11-16"
SOURCE = "playbook_sim"
CAPS = (2, 3)
MIN_DAYS = 30
Z80 = 1.2815515655446004
Z_POWER80 = 0.8416212335729143

_SPEC = importlib.util.spec_from_file_location("pnl_expectancy_pivot_analysis", ANALYZER)
_A = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_A)


def _day_cluster(daily_delta):
    values = list(daily_delta.values())
    if not values:
        return {"n_days": 0, "mean_delta": None, "ci80": None, "mde80": None}
    mean = statistics.mean(values)
    if len(values) < 2:
        return {"n_days": 1, "mean_delta": round(mean, 4), "ci80": None, "mde80": None}
    se = statistics.stdev(values) / len(values) ** 0.5
    return {
        "n_days": len(values),
        "mean_delta": round(mean, 4),
        "ci80": [round(mean - Z80 * se, 4), round(mean + Z80 * se, 4)],
        "mde80": round((Z80 + Z_POWER80) * se, 4),
    }


def settle(rows):
    signal, excluded = split_frequency_population(rows)
    recovery_included = sum((r.get("entry_reason") or "") == "sync_recovery_backfill" for r in signal)
    by_day = defaultdict(list)
    for row in signal:
        by_day[row["trade_date"]].append(row)
    uncapped_day_bps = {d: sum(_A._bps(r) for r in v) / len(v) for d, v in by_day.items()}
    uncapped_day_usd = {d: sum(_A._net(r) for r in v) for d, v in by_day.items()}
    days = len(by_day)
    caps = []
    for cap in CAPS:
        delta, cap_usd, capped_day_bps = {}, {}, {}
        kept = 0
        for day, trades in by_day.items():
            taken = trades[:cap]
            kept += len(taken)
            cap_mean = sum(_A._bps(r) for r in taken) / len(taken)
            capped_day_bps[day] = cap_mean
            delta[day] = cap_mean - uncapped_day_bps[day]
            cap_usd[day] = sum(_A._net(r) for r in taken)
        cluster = _day_cluster(delta)
        ci80 = cluster["ci80"]
        cap_day_mean_bps = (
            round(statistics.mean(capped_day_bps.values()), 4) if capped_day_bps else None)
        criteria = {
            "sample_days>=30": days >= MIN_DAYS,
            "cap_day_mean_bps>0": bool(cap_day_mean_bps is not None and cap_day_mean_bps > 0),
            "day_cluster_ci80_lower>0": bool(ci80 and ci80[0] > 0),
            "day_cluster_mean>mde80": bool(
                cluster["mde80"] is not None and cluster["mean_delta"] > cluster["mde80"]),
        }
        caps.append({
            "cap": cap, "kept_trades": kept,
            "n_days": cluster["n_days"], "mean_delta_bps": cluster["mean_delta"],
            "cap_day_mean_bps": cap_day_mean_bps,
            "ci80": ci80, "mde80": cluster["mde80"],
            "cap_day_mean_usd": round(sum(cap_usd.values()) / days, 2) if days else None,
            "criteria": criteria,
            "pass": all(criteria.values()),
        })
    if days < MIN_DAYS:
        verdict = "TOO_EARLY"
    elif any(c["pass"] for c in caps):
        verdict = "GRADUATION_CANDIDATE"
    elif all(c["ci80"] and c["ci80"][1] < 0 for c in caps):
        verdict = "FREQUENCY_CAP_HARMFUL"
    else:
        verdict = "INDISTINGUISHABLE"
    return {
        "schema_version": 1,
        "cohort_start": COHORT_START,
        "source": SOURCE,
        "as_of": datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d"),
        "uncapped": {
            "days": days,
            "n_trades": len(signal),
            "day_mean_bps": round(sum(uncapped_day_bps.values()) / days, 2) if days else None,
            "day_mean_usd": round(sum(uncapped_day_usd.values()) / days, 2) if days else None,
        },
        "n_artifact_excluded": len(excluded),
        "n_recovery_included": recovery_included,
        "caps": caps,
        "verdict": verdict,
        "note": "RFC-209 record-only 前瞻反事实；人群=真实信号+恢复补录真实PnL(剔除补偿/人工收养)；n<30 只观察；毕业须 capped>0 且改善显著，另立 DEB+用户裁。",
    }


def main():
    conn = _A._connect()
    rows = _A._load(conn, COHORT_START, SOURCE)
    report = settle(rows)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    tmp.replace(OUTPUT)
    print("verdict=%s days=%d caps=%s" % (
        report["verdict"], report["uncapped"]["days"],
        [(c["cap"], c["cap_day_mean_bps"], c["mean_delta_bps"], c["ci80"]) for c in report["caps"]]))
    print("wrote", OUTPUT)


if __name__ == "__main__":
    main()
