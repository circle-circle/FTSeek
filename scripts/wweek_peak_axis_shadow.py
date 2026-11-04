#!/usr/bin/env python3
"""W_WEEK 隔夜豁免 · peak 轴 vs 深度轴 纯观测影子 (DEB-20261104 §D4/E7)

为什么有这个影子
----------------
多模型辩论 (GLM-5.3 / Kimi-K3.1-Pro) 的实证结论: 被 W_WEEK 水下豁免放行隔夜的仓里,
有一档"**当日完全没走方向**"(入场后 60 分钟 peak 仅 0.03~0.08%) 的单在隔夜跳空里各亏 5~6%
(日志实锤: 2026-10-09 SNDK/AMD、10-27 CRM)。豁免的裁决基础假设"水下 = 方向对但回吐了",
而 peak≈0 的单是"从没对过" ⇒ 两类仓被混为一谈。

但**不能**直接据此改豁免:
  - C11/C14/C15/C16/C17 五条红线已把"隔夜跳空/减半/偏斜/形态/取消隔夜"全部终局证伪;
  - **B19 `eod_depth_gate` 已终裁 GATE_NOT_WORTH** (2026-10-14) ⇒ 同族换判据轴
    不产生新信息;
  - B15 已存在隔夜臂判罪线 (修复率<40% ⇒ 提请裁排除 loser_overnight_enabled) ⇒ 本影子
    挂在 B15 下作子集细化, 不另开平行判据。
⇒ 本影子**纯观测**: 只记账分档, **不改任何豁免行为、不设毕业门、不进任何 live 路径**。

两轴对照 (本影子的核心读数)
--------------------------
  - **深度轴** (B19 同轴): EOD 判定时刻净水深 `underwater_pct_net` ≤ -2.5% (结果变量, 时点晚);
  - **peak 轴** (本议题): 闸门评估时刻 peak `mfe_gate_pct` < 0.1% (过程变量, 时点早) ——
    机制上可能触及深度轴触及不到的笔 (深度在 EOD 才看, peak 在入场后 60 分钟就看)。
**预注册关闭条件** (DEB §D4): 若 peak 轴命中集与 B19 深度轴命中集**完全重合** ⇒ 本议题关闭
(不产生新信息, 不得再以同族理由重开)。本脚本每次运行都输出该重合度实时读数。

口径
----
  人群: trade_records.source_type='PLAYBOOK_SIM' ∧ metadata_json.overnight_arm.source='w_week'
        (被 W_WEEK 豁免授予隔夜的仓; EOD 判定时刻写入)
  产出: data/shadow/wweek_peak_axis.jsonl (每笔一行, 幂等按 trade_id 去重)
        data/shadow/wweek_peak_axis_latest.json (汇总)
  全程只读 (mode=ro); 不写主库; 不改豁免/出场任何参数。

用法:
    venv/bin/python scripts/wweek_peak_axis_shadow.py [--from 2026-07-01] [--json]
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DB = _ROOT / "data" / "ftseek_us.db"
_OUT_DIR = _ROOT / "data" / "shadow"
_OUT_JSONL = _OUT_DIR / "wweek_peak_axis.jsonl"
_OUT_SUMMARY = _OUT_DIR / "wweek_peak_axis_latest.json"

# 轴阈值 (预注册冻结; 改 = 重新裁决, 不得事后挪门柱)
PEAK_AXIS_PCT = 0.1      # peak 轴: 闸门评估时刻 peak < 0.1% ⇒ "当日完全没走方向"
DEPTH_AXIS_PCT = -2.5     # 深度轴 (B19 同轴): EOD 判定时刻净水深 ≤ -2.5%
PEAK_BUCKETS = ((0.1, "<0.1%"), (0.3, "0.1-0.3%"), (1.0, "0.3-1%"), (float("inf"), ">=1%"))


def _resolve_db() -> Path:
    try:
        sys.path.insert(0, str(_ROOT))
        from app.core.db_utils import get_main_db  # noqa: PLC0415
        return Path(get_main_db())
    except Exception:
        return _DB


def _peak_bucket(v) -> str:
    if v is None:
        return "NA"
    for edge, label in PEAK_BUCKETS:
        if v < edge:
            return label
    return ">=1%"


def _depth_bucket(v) -> str:
    if v is None:
        return "NA"
    if v <= DEPTH_AXIS_PCT:
        return "<=-2.5%"
    if v < 0:
        return "-2.5~0%"
    return ">=0%"


def _columns(conn) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(trade_records)")}


def collect(conn, date_from: str) -> list:
    # 新列未迁移时降级为 NULL (绝不崩, 也不猜) —— 与仓内"缺列 fail-open"同族取舍
    have = _columns(conn)
    col = lambda name: name if name in have else "NULL AS " + name
    rows = conn.execute(
        "SELECT id, trade_date, symbol, direction, exit_reason, realized_pnl, pnl_net, "
        "       mfe_pct, " + col("mfe_tick_pct") + ", " + col("mfe_gate_pct") +
        ", holding_hours, entry_time, exit_time, metadata_json "
        "FROM trade_records "
        "WHERE source_type='PLAYBOOK_SIM' AND exit_price>0 "
        "  AND trade_date>=? "
        "  AND COALESCE(json_extract(metadata_json,'$.exclusion_reason'),'')='' "
        "  AND json_extract(metadata_json,'$.overnight_arm.source')='w_week' "
        "ORDER BY id",
        (date_from,),
    ).fetchall()
    out = []
    for r in rows:
        try:
            meta = json.loads(r["metadata_json"] or "{}")
        except Exception:
            meta = {}
        arm = meta.get("overnight_arm") or {}
        depth = arm.get("underwater_pct_net")
        gate = r["mfe_gate_pct"]
        peak_hit = gate is not None and gate < PEAK_AXIS_PCT
        depth_hit = depth is not None and depth <= DEPTH_AXIS_PCT
        out.append({
            "trade_id": r["id"],
            "trade_date": r["trade_date"],
            "symbol": r["symbol"],
            "direction": r["direction"],
            "entry_time": r["entry_time"],
            "exit_time": r["exit_time"],
            "exit_reason": r["exit_reason"],
            "holding_hours": r["holding_hours"],
            "gross": r["realized_pnl"],
            "net": r["pnl_net"] if r["pnl_net"] is not None else r["realized_pnl"],
            "mfe_pct_db": r["mfe_pct"],
            "mfe_pct_tick": r["mfe_tick_pct"],
            "mfe_pct_gate": gate,
            "underwater_pct_net": depth,
            "night_n": arm.get("night_n"),
            "peak_bucket": _peak_bucket(gate),
            "depth_bucket": _depth_bucket(depth),
            "peak_axis_hit": bool(peak_hit),
            "depth_axis_hit": bool(depth_hit),
            "is_gap_stop": r["exit_reason"] == "OVERNIGHT_GAP_STOP",
        })
    return out


def summarize(rows: list) -> dict:
    def agg(sel):
        if not sel:
            return {"n": 0, "net": 0.0, "gap_stops": 0}
        return {
            "n": len(sel),
            "net": round(sum(float(r["net"] or 0) for r in sel), 2),
            "gap_stops": sum(1 for r in sel if r["is_gap_stop"]),
        }
    peak_set = {r["trade_id"] for r in rows if r["peak_axis_hit"]}
    depth_set = {r["trade_id"] for r in rows if r["depth_axis_hit"]}
    overlap = peak_set & depth_set
    only_peak = peak_set - depth_set
    only_depth = depth_set - peak_set
    return {
        "population": "overnight_arm.source=w_week (被 W_WEEK 豁免授予隔夜的仓)",
        "n": len(rows),
        "axis_thresholds": {"peak_axis_pct": PEAK_AXIS_PCT, "depth_axis_pct": DEPTH_AXIS_PCT},
        "peak_axis": agg([r for r in rows if r["peak_axis_hit"]]),
        "depth_axis_b19": agg([r for r in rows if r["depth_axis_hit"]]),
        "no_axis": agg([r for r in rows if not r["peak_axis_hit"] and not r["depth_axis_hit"]]),
        "peak_buckets": {label: agg([r for r in rows if r["peak_bucket"] == label])
                         for _, label in PEAK_BUCKETS},
        "depth_buckets": {label: agg([r for r in rows if r["depth_bucket"] == label])
                          for label in ("<=-2.5%", "-2.5~0%", ">=0%", "NA")},
        "axis_overlap": {
            "peak_n": len(peak_set),
            "depth_n": len(depth_set),
            "overlap_n": len(overlap),
            "peak_only_n": len(only_peak),
            "depth_only_n": len(only_depth),
            "closure_condition_triggered": bool(peak_set) and peak_set <= depth_set,
            "closure_note": (
                "peak 轴尚无样本 (mfe_gate_pct 列未迁移或闸门未评估到) ⇒ 不可判定, 继续观测"
                if not peak_set else
                ("peak 命中集完全含于 depth 命中集 ⇒ 本议题关闭 (DEB §D4 预注册)"
                 if peak_set <= depth_set else
                 "peak 轴触及 depth 轴触及不到的笔 ⇒ 两轴非同一集合, 继续观测")
            ),
        },
        "gap_stop_population": agg([r for r in rows if r["is_gap_stop"]]),
    }


def _write_jsonl(rows: list) -> None:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    seen = set()
    if _OUT_JSONL.exists():
        for line in _OUT_JSONL.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(json.loads(line)["trade_id"])
            except Exception:
                continue
    fresh = [r for r in rows if r["trade_id"] not in seen]
    if fresh:
        with _OUT_JSONL.open("a", encoding="utf-8") as fh:
            for r in fresh:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="W_WEEK peak 轴纯观测影子 (DEB-20261104 D4)")
    ap.add_argument("--from", dest="date_from", default="2026-07-01")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    db = _resolve_db()
    if not db.exists():
        print(f"[wweek_peak_axis_shadow] 库不存在: {db}", file=sys.stderr)
        return 0
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = collect(conn, args.date_from)
    finally:
        conn.close()

    _write_jsonl(rows)
    summary = summarize(rows)
    summary["generated_at"] = None
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    _OUT_SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"W_WEEK peak 轴影子  人群 n={summary['n']}")
        for label, agg_ in summary["peak_buckets"].items():
            print(f"  peak档 {label:9s} n={agg_['n']:3d} net=${agg_['net']:>10,.2f} 跳空={agg_['gap_stops']}")
        for label, agg_ in summary["depth_buckets"].items():
            print(f"  深水档 {label:9s} n={agg_['n']:3d} net=${agg_['net']:>10,.2f} 跳空={agg_['gap_stops']}")
        ov = summary["axis_overlap"]
        print(f"  轴重合: peak={ov['peak_n']} depth={ov['depth_n']} overlap={ov['overlap_n']} "
              f"peak_only={ov['peak_only_n']} depth_only={ov['depth_only_n']}")
        print(f"  关闭条件触发: {ov['closure_condition_triggered']} — {ov['closure_note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
