#!/usr/bin/env python3
"""MFE 口径并列披露 (DEB-20261104 §D2/E6)

为什么有这个脚本
----------------
落库 `trade_records.mfe_pct` = max(tick峰, bar峰, 已落袋)，**出场时刻重算** ⇒ 含隔夜尖峰。
在线闸门 (`app/playbook/exit_engine.py`) 读的是 **tick 峰**；死信号/豁免判定读的是
**评估时刻的 peak**。三者语义不同（实测：DA 133 笔中落库≠tick 占 84 笔 = 63%；
豁免事件对齐后落库>决策峰值占 91/111 = 82%）。

本脚本是新增两列 (`mfe_tick_pct` / `mfe_gate_pct`) 的**唯一消费方**：
只做并列披露 + 基数自检，**不改任何判据、不改任何门禁读列**（DEB §D2 纪律）。

判据口径锚: 判据字典 **A11** —— KR2 死单率 = **落库 `mfe_pct` < 0.3%**。
本脚本输出的另外两个口径**仅作披露**，不参与达标判定。

用法:
    venv/bin/python scripts/mfe_caliber_report.py [--from 2026-10-06] [--to 2026-11-04]
                                       [--json] [--source PLAYBOOK_SIM]
退出码: 0 正常; 1 新列出现基数塌缩 (distinct<2 且 n≥10) 或填充率异常
"""
import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DB = _ROOT / "data" / "ftseek_us.db"

# 判据锚 (A11): 死单定义 = 落库 mfe_pct < 0.3% (百分比单位, 3.72 = 3.72%)
DEAD_MFE_PCT = 0.3
# 填充率下限 (DEB §V2): 新列填充率过低 ⇒ 采集链断档, 需告警
MIN_FILL_RATIO = 0.90
# 基数自检 (军规: 新字段必带基数自检) —— n≥10 且 distinct<2 ⇒ 塌缩
CARD_MIN_N = 10
CARD_MIN_DISTINCT = 2


def _resolve_db() -> Path:
    """按 TRADE_ENV 解析库 (与仓内其它脚本同口径; 解析失败静默回退默认库)."""
    try:
        sys.path.insert(0, str(_ROOT))
        from app.core.db_utils import get_main_db  # noqa: PLC0415
        return Path(get_main_db())
    except Exception:
        return _DB


def _has_columns(conn, cols) -> bool:
    have = {r[1] for r in conn.execute("PRAGMA table_info(trade_records)")}
    return all(c in have for c in cols)


def _stats(values) -> dict:
    vals = [v for v in values if v is not None]
    n = len(vals)
    distinct = len(set(round(float(v), 4) for v in vals))
    top_share = (max(Counter(round(float(v), 4) for v in vals).values()) / n) if n else 0.0
    return {"n": n, "distinct": distinct, "max_top_share": round(top_share, 4)}


def report(conn, date_from: str, date_to: str, source: str) -> dict:
    where = [
        "source_type = ?",
        "exit_price > 0",
        "COALESCE(pnl_net, realized_pnl) IS NOT NULL",
        "COALESCE(json_extract(metadata_json,'$.exclusion_reason'),'') = ''",
        "trade_date >= ?",
        "trade_date <= ?",
    ]
    params = [source, date_from, date_to]
    rows = conn.execute(
        "SELECT trade_date, symbol, owner_strategy, mfe_pct, mfe_tick_pct, "
        "mfe_t15_pct, mfe_gate_pct, "
        "exit_reason FROM trade_records WHERE " + " AND ".join(where) + " ORDER BY id",
        params,
    ).fetchall()
    total = len(rows)
    have_new = _has_columns(conn, ("mfe_tick_pct", "mfe_t15_pct", "mfe_gate_pct"))

    def dead_rate(getter) -> dict:
        vals = [getter(r) for r in rows]
        valid = [v for v in vals if v is not None]
        dead = [v for v in valid if v < DEAD_MFE_PCT]
        return {
            "n_with_value": len(valid),
            "n_dead": len(dead),
            "dead_rate": round(len(dead) / len(valid), 4) if valid else None,
        }

    out = {
        "window": {"from": date_from, "to": date_to, "source": source},
        "n_trades": total,
        "new_columns_present": have_new,
        "calibers": {},
        "divergence": {},
        "cardinality": {},
        "warnings": [],
    }
    if not total:
        return out

    out["calibers"]["db_mfe_pct"] = dead_rate(lambda r: r["mfe_pct"])
    if have_new:
        out["calibers"]["tick_mfe_pct"] = dead_rate(lambda r: r["mfe_tick_pct"])
        out["calibers"]["t15_mfe_pct"] = dead_rate(lambda r: r["mfe_t15_pct"])
        out["calibers"]["gate_mfe_pct"] = dead_rate(lambda r: r["mfe_gate_pct"])
        # 落库 vs tick 分歧 (DA 133 笔中 84 笔 = 63% 的可复现基准)
        pairs = [(r["mfe_pct"], r["mfe_tick_pct"]) for r in rows
                 if r["mfe_pct"] is not None and r["mfe_tick_pct"] is not None]
        diff = [1 for a, b in pairs if abs(float(a) - float(b)) > 1e-6]
        out["divergence"] = {
            "compared": len(pairs),
            "db_ne_tick": len(diff),
            "db_ne_tick_ratio": round(len(diff) / len(pairs), 4) if pairs else None,
            "db_gt_tick": sum(1 for a, b in pairs if float(a) > float(b) + 1e-6),
        }
        for name, col in (("mfe_tick_pct", "mfe_tick_pct"),
                          ("mfe_t15_pct", "mfe_t15_pct"),
                          ("mfe_gate_pct", "mfe_gate_pct")):
            st = _stats([r[col] for r in rows])
            st["fill_ratio"] = round(st["n"] / total, 4) if total else None
            out["cardinality"][name] = st
            if st["n"] >= CARD_MIN_N and st["distinct"] < CARD_MIN_DISTINCT:
                out["warnings"].append(
                    f"{name} 基数塌缩: n={st['n']} distinct={st['distinct']} (<{CARD_MIN_DISTINCT})")
            if st["fill_ratio"] is not None and st["fill_ratio"] < MIN_FILL_RATIO:
                out["warnings"].append(
                    f"{name} 填充率 {st['fill_ratio']:.2%} < {MIN_FILL_RATIO:.0%} (采集链可能断档)")
    else:
        out["warnings"].append(
            "新列 mfe_tick_pct / mfe_gate_pct 不存在 (未迁移?) —— 披露只有落库口径")
    return out


def _print_text(rep: dict) -> None:
    w = rep["window"]
    print(f"MFE 口径并列披露  [{w['from']} ~ {w['to']}]  source={w['source']}  n={rep['n_trades']}")
    print(f"判据锚: A11 (死单 = 落库 mfe_pct < {DEAD_MFE_PCT}%; 另两个口径仅披露)")
    for name, cal in rep["calibers"].items():
        rate = cal["dead_rate"]
        rate_s = f"{rate:.2%}" if rate is not None else "n/a"
        print(f"  {name:16s} 有值 n={cal['n_with_value']:4d}  死单 n={cal['n_dead']:4d}  死单率={rate_s}")
    dv = rep.get("divergence") or {}
    if dv.get("compared"):
        print(f"  落库≠tick: {dv['db_ne_tick']}/{dv['compared']} = {dv['db_ne_tick_ratio']:.1%}"
              f"  (其中落库>tick: {dv['db_gt_tick']})")
    for name, st in (rep.get("cardinality") or {}).items():
        print(f"  [基数] {name:16s} n={st['n']:4d} distinct={st['distinct']:4d} "
              f"max_top_share={st['max_top_share']:.2f} fill={st['fill_ratio']:.2%}")
    for msg in rep["warnings"]:
        print(f"  [WARN] {msg}")


def main() -> int:
    ap = argparse.ArgumentParser(description="MFE 口径并列披露 (DEB-20261104)")
    ap.add_argument("--from", dest="date_from", default="2026-10-06")
    ap.add_argument("--to", dest="date_to", default="2099-12-31")
    ap.add_argument("--source", default="PLAYBOOK_SIM")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    db = _resolve_db()
    if not db.exists():
        print(f"[mfe_caliber_report] 库不存在: {db}", file=sys.stderr)
        return 0
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rep = report(conn, args.date_from, args.date_to, args.source)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        _print_text(rep)
    return 1 if rep["warnings"] else 0


if __name__ == "__main__":
    sys.exit(main())
