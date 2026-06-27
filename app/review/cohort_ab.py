"""RFC-061 cohort A/B 退出实验度量 (可被周复盘/脚本/import 复用).

对应 docs/ic_report_2026-06-24.md 附录 B.3/B.4.
实验: cohort B 盈利仓跳过 EOD 强平持夜到 ~T+5 (capture 周级 alpha, T+5 IC +0.265);
      cohort A 维持日内 EOD 全平 (对照).

核心 API:
  compute_cohort_report(since, min_n) -> dict   # 度量 + 决策门禁
  format_cohort_text(report) -> str             # 飞书友好简短文本
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path


def _get_db() -> Path:
    try:
        from app.core.paths import get_trade_records_db
        p = Path(get_trade_records_db())
        if p.exists():
            return p
    except Exception:
        pass
    # 兜底: 仓库根 data/ 常见位置
    root = Path(__file__).resolve().parent.parent.parent  # app/review/.. → repo root
    for cand in (root / "data" / "ftseek_us.db", root / "data" / "trade_records.db"):
        if cand.exists():
            return cand
    raise FileNotFoundError("找不到 trade_records db")


def _cohort_of(meta_json: str) -> str:
    if not meta_json:
        return "PRE_EXPERIMENT"  # RFC-061 上线前 (2026-06-24 前) 无 cohort
    try:
        m = json.loads(meta_json)
        c = m.get("cohort")
        return c if c in ("A", "B") else "PRE_EXPERIMENT"
    except Exception:
        return "PRE_EXPERIMENT"


def _hold_bucket(h: float) -> str:
    if h is None or h <= 0:
        return "0_instant"
    if h < 1:
        return "<1h"
    if h < 4:
        return "1-4h"
    if h < 8:
        return "4-8h"
    if h < 24:
        return "overnight<1d"
    return "1-5d+"


def _fetch(db: Path, since: str | None):
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    where = "WHERE exit_time IS NOT NULL AND exit_time != '' AND exit_reason != ''"
    params: list = []
    if since:
        where += " AND trade_date >= ?"
        params.append(since)
    rows = con.execute(
        f"""SELECT symbol, direction, realized_pnl, pnl_pct, exit_reason,
                  holding_hours, entry_time, exit_time, source_type,
                  metadata_json, trade_date
            FROM trade_records {where}""", params
    ).fetchall()
    # 未平仓 (B 组持夜中, 附录 B.3 关键观测项)
    open_rows = con.execute(
        "SELECT symbol, direction, entry_time, source_type, metadata_json "
        "FROM trade_records WHERE (exit_time IS NULL OR exit_time='') "
        "AND source_type IN ('PLAYBOOK_SHADOW','PLAYBOOK_SIM','MAIN_PIPELINE','ORB_ENGINE')"
    ).fetchall()
    con.close()
    return rows, open_rows


def _aggregate(rows):
    n = len(rows)
    if n == 0:
        return {"n": 0}
    pnls = [r["realized_pnl"] or 0 for r in rows]
    pcts = [r["pnl_pct"] or 0 for r in rows]
    holds = [r["holding_hours"] or 0 for r in rows]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    decided = wins + losses
    return {
        "n": n,
        "sum_pnl": round(sum(pnls), 2),
        "avg_pnl_pct": round(sum(pcts) / n, 3),
        "avg_hold_h": round(sum(holds) / n, 2),
        "win_rate_pct": round(wins / decided * 100, 1) if decided else None,
        "wins": wins,
        "losses": losses,
        "exit_reasons": dict(Counter(r["exit_reason"] for r in rows).most_common()),
        "hold_buckets": dict(Counter(_hold_bucket(r["holding_hours"]) for r in rows).most_common()),
    }


def _decision_gate(a: dict, b: dict, min_n: int) -> str:
    """附录 B.4: PASS B 组 sum_pnl>A 且 win_rate 不降; FAIL B 更差; INCONCLUSIVE 样本不足."""
    na, nb = a.get("n", 0), b.get("n", 0)
    if na < min_n or nb < min_n:
        return f"INCONCLUSIVE (n: A={na}, B={nb}, 需每组≥{min_n}; 实验 2026-06-24 启动, 需 ~2 周)"
    b_pnl = b.get("sum_pnl", 0)
    a_pnl = a.get("sum_pnl", 0)
    b_wr = b.get("win_rate_pct")
    a_wr = a.get("win_rate_pct")
    pnl_better = b_pnl > a_pnl
    wr_ok = (b_wr is not None and a_wr is not None and b_wr >= a_wr)
    if pnl_better and wr_ok:
        return "PASS — B 组 PnL 与胜率均 ≥ A, 推广: 主线 EOD 强平改为盈利仓可选过夜"
    if (not pnl_better) and (b_wr is not None and a_wr is not None and b_wr < a_wr - 5):
        return "FAIL — B 组因隔夜跳空亏损, 信号/持仓周期不匹配, 回日内另寻 alpha"
    return "INCONCLUSIVE — 指标方向不一, 延长到 4 周 / 100 笔"


def compute_cohort_report(since: str | None = None, min_n: int = 30) -> dict:
    """核心度量: 返回 cohort A/B 报告 dict.

    Returns: {db, since, closed_total, open_total, cohorts:{A,B,PRE_EXPERIMENT},
              open_positions, decision_gate}
    """
    db = _get_db()
    rows, open_rows = _fetch(db, since)
    by_cohort: dict[str, list] = {"A": [], "B": [], "PRE_EXPERIMENT": []}
    for r in rows:
        by_cohort[_cohort_of(r["metadata_json"])].append(r)

    agg = {k: _aggregate(v) for k, v in by_cohort.items()}
    gate = _decision_gate(agg.get("A", {}), agg.get("B", {}), min_n)
    open_by_cohort = Counter(_cohort_of(r["metadata_json"]) for r in open_rows)

    return {
        "db": str(db),
        "since": since,
        "closed_total": len(rows),
        "open_total": len(open_rows),
        "cohorts": agg,
        "open_positions": dict(open_by_cohort),
        "decision_gate": gate,
    }


def format_cohort_text(report: dict) -> str:
    """格式化为飞书友好的简短文本 (周复盘推送用)."""
    a = (report.get("cohorts") or {}).get("A", {}) or {}
    b = (report.get("cohorts") or {}).get("B", {}) or {}
    lines = ["【RFC-061 退出 A/B 实验】(cohort B 盈利仓持夜到 T+5 vs A 日内全平)"]
    for tag, s in (("A", a), ("B", b)):
        if not s or s.get("n", 0) == 0:
            lines.append(f"  cohort {tag}: n=0")
            continue
        lines.append(
            f"  cohort {tag}: n={s['n']} PnL=${s.get('sum_pnl', 0)} "
            f"WR={s.get('win_rate_pct')}% hold={s.get('avg_hold_h')}h"
        )
    lines.append(f"  持夜中(未平): {report.get('open_positions')} ← B>0=实验生效")
    lines.append(f"  门禁: {report.get('decision_gate')}")
    return "\n".join(lines)
