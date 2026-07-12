#!/usr/bin/env python3
"""PLAYBOOK 真实成交退出遥测分析 (只读) — Section 3.

用富途模拟盘的真实平仓单, 按 config_version × regime × session × exit_reason 分层,
度量退出质量。不做影子, 券商真实成交为准。不改任何数据。

核心指标 (每个分组):
  - 笔数 / 胜率 / 平均已实现 pnl%
  - 平均 MFE% (最大浮盈) / 平均 MAE% (最大浮亏)
  - MFE 捕获率 = 已实现有利 / MFE (盈利单; 越高=退出越贴峰)
  - 回吐 = MFE - 已实现 (盈利单平均回吐了多少浮盈)
  - 亏损单中 MFE≈0 占比 (入场问题信号: 从未浮盈的亏损)

数据来源: ftseek_us.db::trade_records (mfe_pct/mae_pct 独立列已采集;
          exit_telemetry 在 metadata_json, 有则用于 trough/峰谷价位增强)。

用法:
    ./venv/bin/python scripts/playbook_exit_telemetry_report.py            # 全量
    ./venv/bin/python scripts/playbook_exit_telemetry_report.py --by regime,session
    ./venv/bin/python scripts/playbook_exit_telemetry_report.py --days 30
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
MFE_ZERO_EPS = 0.1  # MFE ≤ 0.1% 视为"从未浮盈" (入场问题)


def _cfg_layer(cv):
    return cv or "v1"


def _load_closed(days=None):
    db = str(get_trade_records_db())
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    q = """
        SELECT config_version, market_regime, entry_session, exit_reason,
               realized_pnl, pnl_pct, mfe_pct, mae_pct, entry_time, metadata_json
        FROM trade_records
        WHERE source_type IN ({})
          AND exit_time IS NOT NULL AND exit_time != ''
    """.format(",".join("?" * len(PLAYBOOK_SOURCES)))
    rows = conn.execute(q, PLAYBOOK_SOURCES).fetchall()
    conn.close()
    out = []
    cutoff = (datetime.now() - timedelta(days=days)) if days else None
    for r in rows:
        if cutoff is not None:
            try:
                if datetime.fromisoformat((r["entry_time"] or "")[:19]) < cutoff:
                    continue
            except Exception:
                pass
        out.append(r)
    return db, out


def _group_key(r, dims):
    parts = [_cfg_layer(r["config_version"])]
    if "regime" in dims:
        parts.append(r["market_regime"] or "UNKNOWN")
    if "session" in dims:
        parts.append(r["entry_session"] or "?")
    return " / ".join(parts)


def _new_agg():
    return {
        "n": 0, "wins": 0, "pnl_pct_sum": 0.0,
        "mfe_sum": 0.0, "mfe_n": 0, "mae_sum": 0.0, "mae_n": 0,
        "capture_sum": 0.0, "capture_n": 0,
        "giveback_sum": 0.0, "giveback_n": 0,
        "loss_n": 0, "loss_mfe_zero": 0,
        "telemetry_n": 0,
    }


def _fold(agg, r):
    pnl = r["realized_pnl"] or 0
    pnl_pct = float(r["pnl_pct"] or 0) * 100.0  # DB 存分数 → 百分比
    mfe = float(r["mfe_pct"] or 0)
    mae = float(r["mae_pct"] or 0)
    is_win = pnl > 0

    agg["n"] += 1
    agg["pnl_pct_sum"] += pnl_pct
    if is_win:
        agg["wins"] += 1
    agg["mfe_sum"] += mfe
    agg["mfe_n"] += 1
    agg["mae_sum"] += mae
    agg["mae_n"] += 1

    if is_win and mfe > 0:
        agg["capture_sum"] += max(0.0, min(pnl_pct, mfe)) / mfe
        agg["capture_n"] += 1
        agg["giveback_sum"] += max(0.0, mfe - pnl_pct)
        agg["giveback_n"] += 1
    if not is_win:
        agg["loss_n"] += 1
        if mfe <= MFE_ZERO_EPS:
            agg["loss_mfe_zero"] += 1

    try:
        if isinstance(json.loads(r["metadata_json"] or "{}").get("exit_telemetry"), dict):
            agg["telemetry_n"] += 1
    except Exception:
        pass


def _avg(s, n):
    return (s / n) if n else 0.0


def _fmt_group(title, agg):
    n = agg["n"]
    wr = 100.0 * agg["wins"] / n if n else 0
    lines = [f"\n■ {title}   (n={n}, 胜率 {wr:.1f}%, 平均 pnl {_avg(agg['pnl_pct_sum'], n):+.2f}%)"]
    lines.append(f"    MFE均 {_avg(agg['mfe_sum'], agg['mfe_n']):.2f}%   "
                 f"MAE均 {_avg(agg['mae_sum'], agg['mae_n']):.2f}%   "
                 f"telemetry {agg['telemetry_n']}/{n}")
    if agg["capture_n"]:
        lines.append(f"    MFE捕获率 {100 * _avg(agg['capture_sum'], agg['capture_n']):.1f}%   "
                     f"平均回吐 {_avg(agg['giveback_sum'], agg['giveback_n']):.2f}%  (盈利单 {agg['capture_n']})")
    if agg["loss_n"]:
        z = agg["loss_mfe_zero"]
        lines.append(f"    亏损单 {agg['loss_n']}, 其中 MFE≈0(从未浮盈) {z} "
                     f"({100.0 * z / agg['loss_n']:.0f}%) ← 入场问题信号")
    return "\n".join(lines)


def report(dims, days):
    db, rows = _load_closed(days)
    print("=" * 74)
    print("PLAYBOOK 退出遥测分析 (只读, 富途真实平仓)")
    print(f"DB: {db}   分层: config_version"
          + ("".join(f"×{d}" for d in dims) if dims else "")
          + (f"   近{days}天" if days else "   全量"))
    print("=" * 74)

    if not rows:
        print("\n[数据不足] 无已平仓 PLAYBOOK 单。等 v2 平仓单积累后重跑。")
        return 0

    # 分组 → exit_reason 明细
    groups = defaultdict(lambda: defaultdict(_new_agg))
    overall = defaultdict(_new_agg)
    for r in rows:
        gk = _group_key(r, dims)
        er = r["exit_reason"] or "(none)"
        _fold(groups[gk][er], r)
        _fold(overall[gk], r)

    for gk in sorted(groups.keys()):
        print(_fmt_group(f"{gk}  [合计]", overall[gk]))
        for er in sorted(groups[gk].keys(), key=lambda x: -groups[gk][x]["n"]):
            print(_fmt_group(f"    └ exit_reason={er}", groups[gk][er]))

    print("\n" + "-" * 74)
    print("读法: MFE捕获率高=退出贴峰; 回吐大=退出/锁利可优化(退出问题);")
    print("      亏损单 MFE≈0 占比高=入场信号无 edge(回去修入场, 不是退出)。")
    print("      样本 <8/exit_reason 时勿据此调参 (见方案 Section 5 门槛)。")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--by", default="", help="额外分层维度, 逗号分隔: regime,session")
    ap.add_argument("--days", type=int, default=None, help="仅统计近 N 天入场")
    a = ap.parse_args()
    dims = [d.strip() for d in a.by.split(",") if d.strip() in ("regime", "session")]
    sys.exit(report(dims, a.days))
