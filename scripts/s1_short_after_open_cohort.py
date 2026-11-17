#!/usr/bin/env python3
"""S1 臂前瞻判据评估器 (B23 / s1_short_after_open) — 只读。

判据 (预注册 2026-11-15, 用户裁「同意」; 改判据/换切片 = 重新裁决):
  人群 S1 = A10 口径 ∧ direction='SHORT' ∧ ET 入场时刻 >= 09:45 ∧ 已平仓
        (A10 = trade_date>=2026-07-01 ∧ data_quality∈clean/real ∧ source_type∉POSITION_SYNC/MANUAL,
         SSOT = app/core/db_utils.ERA_CLEAN_PERFORMANCE_WHERE)
  前瞻窗口 = trade_date >= FORWARD_START (判据落档后的首个交易日), 落档前的 88 笔只作参照。
  判据 (前瞻已平 n>=40 时机械判, 四条全过才算过):
    ① 净 bps >= 该窗实测费 bps × 1.5
    ② Σ净 > 0
    ③ 剔最大单笔后 Σ净 仍 > 0
    ④ 对照臂不自相矛盾: 同窗 `SHORT ∧ <09:45` 与 `LONG ∧ ≥09:45` 不得同时显著为正
       (同时为正 ⇒ 增益来自全局 regime 而非「方向×时段」, 判据降级为观察)
  停止条件 (上线同批; 任一条触发即当日回滚, 不等攒样):
    S1 方向性 = 前瞻 n>=15 且 净 bps < 0
    S2 经济性 = 前瞻 Σ净 < -$300
    S3 结构性 = 真实滑点中位 (exit_slippage_bps + 入场追价 bps) > 15bps —— 需实盘数据, 本脚本不判
  纪律: 本脚本**不写交易库、不下单、不改配置**; 只产出 data/s1_cohort/latest.json 供周检读。

用法: python scripts/s1_short_after_open_cohort.py [--json]
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import sqlite3

REPO = Path(__file__).resolve().parents[1]
DB_PATH = REPO / "data" / "ftseek_us.db"
OUT_PATH = REPO / "data" / "s1_cohort" / "latest.json"
# --db live 写另一份产物: 注册表 s1_short_after_open 的 data_path 锚定 latest.json
# (count_mode=json_key:forward_n), 若让 live 读数覆盖它, 影子看板会把真钱 cohort 与
# sim 历史参照混成一格。签署日要让看板跟真钱 cohort, 须显式改 data_path (RFC-212 §5 P6)。
OUT_PATH_LIVE = REPO / "data" / "s1_cohort" / "latest_live.json"

ET = ZoneInfo("America/New_York")
FORWARD_START = "2026-11-16"   # 判据落档日 (2026-11-15) 之后的首个交易日; 改它 = 重新裁决
MIN_N = 40                     # 判据样本门 (前瞻已平笔数)
OPEN_MIN = 585                 # 09:45 ET == 585 分钟

SQL = """
SELECT trade_date, symbol, direction, entry_time, exit_time, realized_pnl, fees_est,
       pnl_net, quantity, entry_price, metadata_json
FROM trade_records
WHERE exit_time IS NOT NULL AND pnl_net IS NOT NULL
  AND trade_date >= '2026-07-01'
  AND COALESCE(data_quality,'') IN ('clean','real')
  AND COALESCE(source_type,'') NOT IN ('POSITION_SYNC','MANUAL')
"""


def _entry_minute(ts: str):
    """entry_time → ET 当日分钟数。库内混存 -04:00/-05:00/+08:00/无偏移, 禁写死 −4h。"""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    et = dt.astimezone(ET)
    return et.hour * 60 + et.minute


def _resolve_db(arm: str) -> Path:
    """按臂别解析账本, 与 `app/playbook/s1_arm.py::_ledger` 同一 SSOT (防两处漂移)。

    P6/P8: 原 DB_PATH 写死 sim 研究库 ⇒ 签署后前瞻 cohort 读空 (真钱空单落
    `ftseek_us_live.db`)。默认 arm='sim' 与落档以来同库, 数值不变。
    """
    from app.core.paths import get_trade_records_db_for_arm

    return Path(get_trade_records_db_for_arm(arm))


def load_rows(db_path: Path = DB_PATH):
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = []
        for r in conn.execute(SQL):
            d = dict(r)
            d["mod"] = _entry_minute(d["entry_time"])
            d["notional"] = (d["quantity"] or 0) * (d["entry_price"] or 0)
            rows.append(d)
        return rows
    finally:
        conn.close()


def _stats(rows):
    n = len(rows)
    if not n:
        return {"n": 0, "notional": 0.0, "gross": 0.0, "fees": 0.0, "net": 0.0,
                "gross_bps": None, "fee_bps": None, "net_bps": None,
                "win_rate": None, "net_ex_top1": None, "net_ex_top1_bps": None}
    no = sum(r["notional"] for r in rows)
    g = sum(r["realized_pnl"] or 0 for r in rows)
    f = sum(r["fees_est"] or 0 for r in rows)
    net = sum(r["pnl_net"] for r in rows)
    srt = sorted(rows, key=lambda r: -r["pnl_net"])
    ex1 = net - srt[0]["pnl_net"]
    no1 = no - srt[0]["notional"]
    return {
        "n": n,
        "notional": round(no, 2),
        "gross": round(g, 2),
        "fees": round(f, 2),
        "net": round(net, 2),
        "gross_bps": round(g / no * 1e4, 2) if no else None,
        "fee_bps": round(f / no * 1e4, 2) if no else None,
        "net_bps": round(net / no * 1e4, 2) if no else None,
        "win_rate": round(sum(1 for r in rows if r["pnl_net"] > 0) / n * 100, 1),
        "net_ex_top1": round(ex1, 2),
        "net_ex_top1_bps": round(ex1 / no1 * 1e4, 2) if no1 else None,
        "top1_symbol": srt[0]["symbol"],
        "top1_share_pct": round(srt[0]["pnl_net"] / net * 100, 1) if net else None,
    }


def evaluate(rows, forward_start: str = FORWARD_START):
    s1 = [r for r in rows if r["direction"] == "SHORT" and r["mod"] is not None and r["mod"] >= OPEN_MIN]
    s1_fwd = [r for r in s1 if r["trade_date"] >= forward_start]
    s1_hist = [r for r in s1 if r["trade_date"] < forward_start]
    # 对照臂 (同前瞻窗): 时段对照 + 方向对照
    ctrl_slot = [r for r in rows if r["direction"] == "SHORT" and r["mod"] is not None
                 and r["mod"] < OPEN_MIN and r["trade_date"] >= forward_start]
    ctrl_dir = [r for r in rows if r["direction"] == "LONG" and r["mod"] is not None
                and r["mod"] >= OPEN_MIN and r["trade_date"] >= forward_start]

    fwd = _stats(s1_fwd)
    hist = _stats(s1_hist)
    cs = _stats(ctrl_slot)
    cd = _stats(ctrl_dir)

    verdict = "KEEP_SAMPLING"
    reasons = []
    if fwd["n"] >= MIN_N:
        thr = (fwd["fee_bps"] or 0) * 1.5
        c1 = fwd["net_bps"] is not None and fwd["net_bps"] >= thr
        c2 = fwd["net"] > 0
        c3 = (fwd["net_ex_top1"] or 0) > 0
        both_ctrl_pos = all(x["net_bps"] is not None and x["net_bps"] > 0 for x in (cs, cd))
        c4 = not both_ctrl_pos
        reasons = [
            "① 净 %.2fbps >= 费×1.5=%.2fbps: %s" % (fwd["net_bps"] or 0, thr, "PASS" if c1 else "FAIL"),
            "② Σ净 $%.2f > 0: %s" % (fwd["net"], "PASS" if c2 else "FAIL"),
            "③ 剔最大单笔 $%.2f > 0: %s" % (fwd["net_ex_top1"] or 0, "PASS" if c3 else "FAIL"),
            "④ 对照臂不同时转正: %s" % ("PASS" if c4 else "FAIL (方向×时段被 regime 混杂)"),
        ]
        verdict = "PASS_S1" if (c1 and c2 and c3 and c4) else "FAIL_S1"
    else:
        reasons = ["攒样中: 前瞻已平 %d/%d 笔 (自 %s 起)" % (fwd["n"], MIN_N, forward_start)]

    stop = []
    if fwd["n"] >= 15 and (fwd["net_bps"] or 0) < 0:
        stop.append("S1 方向性触发: 前瞻 n=%d 且净 %.2fbps < 0 → 当日回滚" % (fwd["n"], fwd["net_bps"]))
    if fwd["net"] < -300:
        stop.append("S2 经济性触发: 前瞻 Σ净 $%.2f < -$300 → 当日回滚" % fwd["net"])

    return {
        "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
        "criterion": "B23 / s1_short_after_open",
        "forward_start": forward_start,
        "min_n": MIN_N,
        "forward_n": fwd["n"],
        "verdict": verdict,
        "reasons": reasons,
        "stop_conditions_triggered": stop,
        "forward": fwd,
        "historical_reference": hist,
        "control_pre_open_short": cs,
        "control_long_after_open": cd,
    }


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    arm = "sim"
    if "--db" in argv:
        i = argv.index("--db")
        if i + 1 >= len(argv):
            print("--db 需要一个值 (sim|live)", file=sys.stderr)
            return 2
        arm = argv[i + 1].strip().lower()
    if arm not in ("sim", "live", "real"):
        print("--db 只接受 sim|live (收到 %r)" % arm, file=sys.stderr)
        return 2
    db_path = _resolve_db(arm)
    out_path = OUT_PATH if arm == "sim" else OUT_PATH_LIVE
    res = evaluate(load_rows(db_path))
    # 产物自带账本出处: 否则 sim 历史参照与真钱前瞻 cohort 在 JSON 里不可区分
    res["arm"] = arm
    res["ledger"] = str(db_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    if "--json" in argv:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    f, h = res["forward"], res["historical_reference"]
    print("S1 臂前瞻判据 (B23) — 前瞻起点 %s, 样本门 n>=%d | 账本 arm=%s %s"
          % (res["forward_start"], res["min_n"], arm, db_path))
    print("  前瞻: n=%d 净 $%.2f  毛 %s / 费 %s / 净 %s bps"
          % (f["n"], f["net"], f["gross_bps"], f["fee_bps"], f["net_bps"]))
    print("  历史参照(不入判据): n=%d 净 $%.2f 毛 %s / 费 %s / 净 %s bps"
          % (h["n"], h["net"], h["gross_bps"], h["fee_bps"], h["net_bps"]))
    print("  判定: %s" % res["verdict"])
    for r in res["reasons"]:
        print("    - %s" % r)
    for s in res["stop_conditions_triggered"]:
        print("  ⚠️ %s" % s)
    print("  产物: %s" % out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())