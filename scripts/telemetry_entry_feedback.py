#!/usr/bin/env python3
"""遥测回流入场闸门 — 数据产出器 (Section 6, 只读 + 只写一个报告 JSON).

把退出遥测的结论回流到【入场】: 对亏损单归因为
  - entry_problem : MFE≈0 (从未浮盈) → 入场信号没 edge, 应回去修筛选/闸门
  - exit_problem  : 曾浮盈≥阈值但最终亏损 (回吐光) → 退出/锁利问题, 不怪入场
按入场触发条件 (metadata.conditions / factor 列) 分组, 输出每个条件的
entry_problem vs exit_problem 计数, 供入场闸门决策参考。

⚠️ 本脚本【不改任何入场阈值】, 只产出 data/telemetry_entry_feedback.json 供人/后续闸门读取。
    参数/闸门调整须另行决策 (见方案 Section 5 门槛)。

用法:
    ./venv/bin/python scripts/telemetry_entry_feedback.py [--days 30] [--min-n 8]
"""
import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.paths import get_trade_records_db

PLAYBOOK_SOURCES = ("PLAYBOOK_SIM", "PLAYBOOK_LIVE")
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "telemetry_entry_feedback.json"
MFE_ZERO_EPS = 0.1   # ≤0.1% 视为从未浮盈
EXIT_PROB_MFE = 1.0  # 曾浮盈≥1% 却亏损 → 退出问题


def _conditions(meta):
    c = meta.get("conditions")
    if isinstance(c, list) and c:
        return [str(x) for x in c]
    return ["(unknown)"]


def build(days, min_n):
    db = str(get_trade_records_db())
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT config_version, realized_pnl, mfe_pct, entry_time, metadata_json
        FROM trade_records
        WHERE source_type IN ({})
          AND exit_time IS NOT NULL AND exit_time != ''
        """.format(",".join("?" * len(PLAYBOOK_SOURCES))),
        PLAYBOOK_SOURCES,
    ).fetchall()
    conn.close()

    cutoff = (datetime.now() - timedelta(days=days)) if days else None
    # 按 (config_version, condition) 分组
    stats = defaultdict(lambda: {"n": 0, "wins": 0, "losses": 0,
                                 "entry_problem": 0, "exit_problem": 0, "other_loss": 0})
    for r in rows:
        if cutoff is not None:
            try:
                if datetime.fromisoformat((r["entry_time"] or "")[:19]) < cutoff:
                    continue
            except Exception:
                pass
        try:
            meta = json.loads(r["metadata_json"] or "{}")
        except Exception:
            meta = {}
        cv = r["config_version"] or "v1"
        pnl = r["realized_pnl"] or 0
        mfe = float(r["mfe_pct"] or 0)
        is_win = pnl > 0
        for cond in _conditions(meta):
            s = stats[(cv, cond)]
            s["n"] += 1
            if is_win:
                s["wins"] += 1
            else:
                s["losses"] += 1
                if mfe <= MFE_ZERO_EPS:
                    s["entry_problem"] += 1
                elif mfe >= EXIT_PROB_MFE:
                    s["exit_problem"] += 1
                else:
                    s["other_loss"] += 1

    # 组装报告
    findings = []
    for (cv, cond), s in stats.items():
        n = s["n"]
        wr = round(100.0 * s["wins"] / n, 1) if n else 0
        # 归因标签: 亏损里 entry_problem 占多数 → 入场; exit_problem 占多数 → 退出
        loss = s["losses"]
        verdict = "insufficient"
        if n >= min_n and loss > 0:
            if s["entry_problem"] >= max(s["exit_problem"], 1) and s["entry_problem"] / loss >= 0.5:
                verdict = "entry_gate_candidate"   # 建议回去审入场闸门
            elif s["exit_problem"] / loss >= 0.5:
                verdict = "exit_tuning_candidate"   # 退出/锁利问题, 别动入场
            else:
                verdict = "mixed"
        findings.append({
            "config_version": cv, "condition": cond,
            "n": n, "win_rate_pct": wr,
            "losses": loss, "entry_problem": s["entry_problem"],
            "exit_problem": s["exit_problem"], "other_loss": s["other_loss"],
            "verdict": verdict,
        })
    findings.sort(key=lambda x: (x["config_version"], -x["n"]))

    report = {
        "generated_at": datetime.now().isoformat(),
        "source": "PLAYBOOK real fills (futu sim/live)",
        "note": "只读产出; 不改入场阈值。verdict=entry_gate_candidate 表示该条件的亏损多为"
                "从未浮盈(入场无edge); exit_tuning_candidate 表示曾浮盈却回吐(退出问题)。",
        "params": {"days": days, "min_n": min_n,
                   "mfe_zero_eps": MFE_ZERO_EPS, "exit_prob_mfe": EXIT_PROB_MFE},
        "findings": findings,
    }
    OUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # 控制台摘要
    print("=" * 74)
    print("遥测回流入场 — 归因报告 (只读, 不改阈值)")
    print(f"输出: {OUT_PATH}")
    print("=" * 74)
    if not findings:
        print("\n[数据不足] 无已平仓 PLAYBOOK 单。等 v2 平仓单积累后重跑。")
        return 0
    for f in findings:
        tag = {"entry_gate_candidate": "→入场闸门", "exit_tuning_candidate": "→退出调参",
               "mixed": "混合", "insufficient": "样本不足"}.get(f["verdict"], f["verdict"])
        print(f"\n[{f['config_version']}] {f['condition']:24s} n={f['n']:3d} WR={f['win_rate_pct']:5.1f}% "
              f"| 亏{f['losses']} (入场问题{f['entry_problem']}/退出问题{f['exit_problem']}) → {tag}")
    print("\n" + "-" * 74)
    print("下一步: verdict=→入场闸门 的条件, 回去审筛选/闸门; →退出调参 的条件留到")
    print("        Section 5 门槛满足后在 v2 数据上调退出。本报告仅供决策, 未自动改任何参数。")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--min-n", type=int, default=8, help="判定 verdict 的最小样本 (方案 Section 5)")
    a = ap.parse_args()
    sys.exit(build(a.days, a.min_n))
