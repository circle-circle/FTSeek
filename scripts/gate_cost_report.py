#!/usr/bin/env python3
"""门成本报告 (DEB-round3 #12, 2026-07-19) — 各入场/失效门拦截成本与 VWAP 门价值终审.

背景: 辩论实证 "VWAP 门拦的 89.4% 是真逆侧" (#9 证伪), 但样本来自单次快照;
本脚本提供可持续复跑的终审口径 — 用 ic_tracker 回填的 fwd_return_intraday
对比 "被门拦下" vs "被门放行" 两组的事后收益, 量化每个门的真实过滤价值.

数据源 (全部只读, stdlib only):
  - data/trigger_miss_telemetry.jsonl   — 触发未中遥测 (reason 分桶)
  - data/entry_timing_shadow.jsonl      — 入场时机门影子 (VWAP 门 block/allow + 微结构特征)
  - data/shadow/exit_param_shadow.jsonl — 退出变体反事实 (abort_30m / dead_signal)
  - data/ftseek_us.db                   — playbook_instructions(fwd_return_intraday) + trade_records

用法:
  python3 scripts/gate_cost_report.py [--days 14]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = REPO_ROOT / "data" / "ftseek_us.db"
TRIGGER_MISS_PATH = REPO_ROOT / "data" / "trigger_miss_telemetry.jsonl"
ENTRY_SHADOW_PATH = REPO_ROOT / "data" / "entry_timing_shadow.jsonl"
EXIT_SHADOW_PATH = REPO_ROOT / "data" / "shadow" / "exit_param_shadow.jsonl"


def _load_jsonl(path: Path, cutoff: datetime) -> list:
    """读 JSONL, 按 ts 字段过滤 >= cutoff. 文件不存在/坏行静默跳过."""
    rows = []
    if not path.exists():
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue
            ts_raw = rec.get("ts") or rec.get("date") or ""
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).replace(tzinfo=None)
            except (ValueError, TypeError):
                continue
            if ts >= cutoff:
                rows.append(rec)
    return rows


def _mean(vals: list):
    vals = [v for v in vals if v is not None]
    return round(statistics.mean(vals), 4) if vals else None


def _load_fwd_returns(db_path: Path) -> dict:
    """inst_id → fwd_return_intraday (ic_tracker 回填, ratio 量纲)."""
    out = {}
    if not db_path.exists():
        return out
    conn = sqlite3.connect(str(db_path))
    try:
        for iid, fwd in conn.execute(
            "SELECT id, fwd_return_intraday FROM playbook_instructions "
            "WHERE fwd_return_intraday IS NOT NULL"
        ):
            out[iid] = fwd
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return out


def report_trigger_gates(miss_rows: list) -> None:
    """§1 各门拦截总览 — trigger_miss reason 分桶."""
    print("=" * 72)
    print("§1 入场/失效门拦截总览 (trigger_miss_telemetry)")
    print("=" * 72)
    if not miss_rows:
        print("  (无数据)\n")
        return
    by_reason = Counter(r.get("reason", "unknown") for r in miss_rows)
    for reason, n in by_reason.most_common():
        gaps = [r.get("gap_pct") for r in miss_rows if r.get("reason") == reason and r.get("gap_pct")]
        print(f"  {reason:<28} {n:>4} 次   mean_gap={_mean(gaps)}%")
    print()


def report_vwap_gate(shadow_rows: list, fwd_map: dict) -> None:
    """§2 VWAP 门价值终审 — blocked vs allowed 的 fwd_return_intraday 对比."""
    print("=" * 72)
    print("§2 VWAP 门价值终审 (entry_timing_shadow × fwd_return_intraday)")
    print("=" * 72)
    # 同一 inst 多条评估 → 取 ts 最新一条 (最终决策)
    latest = {}
    for r in shadow_rows:
        if not r.get("gate_enabled"):
            continue
        iid = r.get("inst_id")
        if iid is None:
            continue
        if iid not in latest or str(r.get("ts", "")) > str(latest[iid].get("ts", "")):
            latest[iid] = r
    blocked, allowed = [], []
    for iid, r in latest.items():
        fwd = fwd_map.get(iid)
        if fwd is None:
            continue
        rec = {"inst_id": iid, "symbol": r.get("symbol"), "fwd": fwd,
               "vwap_dev": r.get("vwap_dev"), "gate_reason": r.get("gate_reason")}
        (blocked if r.get("gate_block") else allowed).append(rec)

    b_fwd = [r["fwd"] for r in blocked]
    a_fwd = [r["fwd"] for r in allowed]
    print(f"  样本: blocked={len(blocked)} allowed={len(allowed)} (有 fwd_return 回填)")
    print(f"  blocked 组 mean_fwd_intraday = {_mean(b_fwd)}")
    print(f"  allowed 组 mean_fwd_intraday = {_mean(a_fwd)}")
    if b_fwd and a_fwd:
        bm, am = statistics.mean(b_fwd), statistics.mean(a_fwd)
        if bm < am:
            print(f"  ✓ 终审: 被拦组事后收益显著更差 ({bm:+.4f} vs {am:+.4f}) → 门拦的是真逆侧, 保留")
        else:
            print(f"  ⚠ 终审: 被拦组事后收益不差 ({bm:+.4f} vs {am:+.4f}) → 门误杀, 复审阈值")

    # >1% dev 子集 (辩论验证集: |vwap_dev|>1% 的 161 条)
    dev_sub = [r for r in blocked
               if r.get("vwap_dev") is not None and abs(float(r["vwap_dev"])) > 1.0]
    if dev_sub:
        print(f"\n  |vwap_dev|>1% 被拦子集: n={len(dev_sub)}, mean_fwd={_mean([r['fwd'] for r in dev_sub])}")
        by_sym = Counter(r["symbol"] for r in dev_sub)
        print(f"  高频被拦: {', '.join(f'{s}×{n}' for s, n in by_sym.most_common(5))}")
    print()


def report_exit_shadow(exit_rows: list) -> None:
    """§3 退出影子反事实摘要 — abort_30m / dead_signal."""
    print("=" * 72)
    print("§3 退出影子反事实 (exit_param_shadow)")
    print("=" * 72)
    if not exit_rows:
        print("  (无数据 — 30min-abort 影子 2026-07-19 起采集, 2 周后有样本)\n")
        return
    abort_hits, abort_saved = 0, []
    ds_hits, ds_total = 0, 0
    for r in exit_rows:
        ab = (r.get("abort_30m") or {}).get("abort_30m_mfe0.1_loss") or {}
        if ab.get("would_abort"):
            abort_hits += 1
            if ab.get("counterfactual_saved_pct") is not None:
                abort_saved.append(ab["counterfactual_saved_pct"])
        for name, ds in (r.get("dead_signal") or {}).items():
            if isinstance(ds, dict) and ds.get("mfe_at_check") is not None:
                ds_total += 1
                if ds.get("triggered"):
                    ds_hits += 1
    print(f"  样本: {len(exit_rows)} 笔平仓")
    print(f"  30min-abort: would_abort={abort_hits} 笔, "
          f"mean_saved={_mean(abort_saved)}% (正=abort更优; >0 且稳定才考虑实单化)")
    if ds_total:
        print(f"  dead_signal: 触发率 {ds_hits}/{ds_total} = {ds_hits/ds_total:.1%}")
    print()


def report_live_outcomes(db_path: Path, days: int) -> None:
    """§4 门后实盘表现 — PLAYBOOK_SIM/LIVE 近 N 天 (排除 ghost/void)."""
    print("=" * 72)
    print(f"§4 门后实盘表现 (近 {days} 天, 排除 GHOST/ORPHAN/PHANTOM)")
    print("=" * 72)
    if not db_path.exists():
        print("  (db 不存在)\n")
        return
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT realized_pnl, exit_reason, holding_hours FROM trade_records "
            "WHERE source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') "
            "AND exit_time IS NOT NULL AND exit_time != '' "
            "AND substr(exit_time,1,10) >= date('now', ?) "
            "AND exit_reason NOT LIKE '%GHOST%' "
            "AND exit_reason NOT LIKE '%ORPHAN%' "
            "AND exit_reason NOT LIKE '%PHANTOM%'",
            (f"-{days} days",),
        ).fetchall()
    except sqlite3.Error as e:
        print(f"  (查询失败: {e})\n")
        return
    finally:
        conn.close()
    if not rows:
        print("  (无数据)\n")
        return
    pnls = [float(r[0] or 0) for r in rows]
    wins = sum(1 for p in pnls if p > 0)
    print(f"  笔数={len(pnls)}  胜率={wins/len(pnls):.1%}  总PnL=${sum(pnls):+,.2f}  "
          f"mean=${statistics.mean(pnls):+,.2f}")
    by_reason = Counter(r[1] for r in rows)
    print(f"  退出原因: {', '.join(f'{k}×{v}' for k, v in by_reason.most_common(6))}")
    quick_loss = [r for r in rows if (r[2] or 99) <= 0.1 and (r[0] or 0) < 0]
    if quick_loss:
        print(f"  ⚠ 分钟级亏损平仓: {len(quick_loss)} 笔 "
              f"(熔断 shadow 告警阈值 ≥2/日, 详见 pipeline._close_trade_record)")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="门成本报告 (DEB-round3 #12)")
    ap.add_argument("--days", type=int, default=14, help="回溯天数 (默认 14)")
    args = ap.parse_args()
    cutoff = datetime.now() - timedelta(days=args.days)

    print(f"\n门成本报告 — 回溯 {args.days} 天 (cutoff={cutoff:%Y-%m-%d %H:%M})\n")

    miss_rows = _load_jsonl(TRIGGER_MISS_PATH, cutoff)
    shadow_rows = _load_jsonl(ENTRY_SHADOW_PATH, cutoff)
    exit_rows = _load_jsonl(EXIT_SHADOW_PATH, cutoff)
    fwd_map = _load_fwd_returns(DB_PATH)

    report_trigger_gates(miss_rows)
    report_vwap_gate(shadow_rows, fwd_map)
    report_exit_shadow(exit_rows)
    report_live_outcomes(DB_PATH, args.days)


if __name__ == "__main__":
    main()
