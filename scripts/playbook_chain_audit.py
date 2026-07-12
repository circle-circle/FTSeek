#!/usr/bin/env python3
"""PLAYBOOK 全链路记录贯通率审计 (只读).

测量优先重构 (2026-07-11) 的验收工具: 证明"筛选→生成→交易→记录→循环"每环
记录完整、可分层。不改任何数据, 只 SELECT + 打印。

链路:
  playbook_instructions (生成)
      └─(metadata.playbook_instruction_id / decision_id)─▶ trade_records (成交)
              └─(exit_time/exit_reason)─▶ 平仓 ─▶ metadata.exit_telemetry (退出画像)

按 config_version (v1<2026-07-01 / v2>=07-01) 分层, 输出各关键字段的填充率,
以及 生成→成交 的贯通率。用法:
    ./venv/bin/python scripts/playbook_chain_audit.py
"""
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.paths import get_trade_records_db

PLAYBOOK_SOURCES = ("PLAYBOOK_SIM", "PLAYBOOK_LIVE")


def _pct(n: int, d: int) -> str:
    return f"{(100.0 * n / d):5.1f}%" if d else "  n/a"


def _load_meta(raw):
    if not raw:
        return {}
    try:
        m = json.loads(raw)
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def audit():
    db = str(get_trade_records_db())
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT id, config_version, source_type, decision_id, signal_id,
               market_regime, entry_vix, entry_session,
               exit_time, exit_reason, mfe_pct, mae_pct, metadata_json
        FROM trade_records
        WHERE source_type IN ({})
        """.format(",".join("?" * len(PLAYBOOK_SOURCES))),
        PLAYBOOK_SOURCES,
    ).fetchall()

    # 分层统计容器
    stats = defaultdict(lambda: {
        "total": 0, "closed": 0,
        "decision_id": 0, "signal_id": 0, "instr_id": 0,
        "regime": 0, "entry_vix": 0, "entry_session": 0,
        "mfe": 0, "mae": 0, "telemetry": 0, "trough": 0,
        "exit_reasons": defaultdict(int),
    })

    for r in rows:
        cv = (r["config_version"] or "v1")
        s = stats[cv]
        s["total"] += 1
        meta = _load_meta(r["metadata_json"])
        is_closed = bool(r["exit_time"])
        if is_closed:
            s["closed"] += 1
            if (r["exit_reason"] or ""):
                s["exit_reasons"][r["exit_reason"]] += 1

        if (r["decision_id"] or ""):
            s["decision_id"] += 1
        if (r["signal_id"] or ""):
            s["signal_id"] += 1
        if meta.get("playbook_instruction_id"):
            s["instr_id"] += 1
        if (r["market_regime"] or "UNKNOWN") != "UNKNOWN":
            s["regime"] += 1
        if float(r["entry_vix"] or 0) > 0:
            s["entry_vix"] += 1
        if (r["entry_session"] or ""):
            s["entry_session"] += 1
        if float(meta.get("trough_price", 0) or 0) > 0:
            s["trough"] += 1
        # MFE/MAE/telemetry 只对已平仓有意义
        if is_closed:
            if float(r["mfe_pct"] or 0) > 0:
                s["mfe"] += 1
            if float(r["mae_pct"] or 0) >= 0 and r["mae_pct"] is not None:
                s["mae"] += 1
            if isinstance(meta.get("exit_telemetry"), dict):
                s["telemetry"] += 1

    # ── 生成→成交 贯通率 (playbook_instructions) ──
    instr_total = instr_executed = instr_outcome = 0
    try:
        irows = conn.execute(
            "SELECT id, outcome FROM playbook_instructions"
        ).fetchall()
        instr_total = len(irows)
        instr_outcome = sum(1 for i in irows if (i["outcome"] or "") in ("win", "loss"))
        instr_ids = {str(i["id"]) for i in irows}
        # 已成交 = trade_records.metadata 里出现的 instruction_id
        executed_ids = set()
        for r in rows:
            iid = _load_meta(r["metadata_json"]).get("playbook_instruction_id")
            if iid is not None and str(iid) in instr_ids:
                executed_ids.add(str(iid))
        instr_executed = len(executed_ids)
    except Exception as e:
        print(f"[warn] playbook_instructions 统计跳过: {e}")

    conn.close()

    # ── 输出 ──
    print("=" * 72)
    print("PLAYBOOK 全链路记录贯通率审计 (只读)")
    print(f"DB: {db}")
    print("=" * 72)

    for cv in sorted(stats.keys()):
        s = stats[cv]
        t, c = s["total"], s["closed"]
        print(f"\n■ config_version = {cv}   (总 {t} 笔, 已平仓 {c} 笔)")
        print("  入场记录填充率:")
        print(f"    decision_id      {_pct(s['decision_id'], t)}   signal_id       {_pct(s['signal_id'], t)}")
        print(f"    instruction_id   {_pct(s['instr_id'], t)}   market_regime   {_pct(s['regime'], t)}")
        print(f"    entry_vix>0      {_pct(s['entry_vix'], t)}   entry_session   {_pct(s['entry_session'], t)}")
        print(f"    trough_price>0   {_pct(s['trough'], t)}")
        print("  退出记录填充率 (分母=已平仓):")
        print(f"    mfe_pct>0        {_pct(s['mfe'], c)}   mae_pct 有值    {_pct(s['mae'], c)}")
        print(f"    exit_telemetry   {_pct(s['telemetry'], c)}")
        if s["exit_reasons"]:
            top = sorted(s["exit_reasons"].items(), key=lambda x: -x[1])
            reasons = "  ".join(f"{k}={v}" for k, v in top)
            print(f"  exit_reason 分布: {reasons}")

    print("\n" + "-" * 72)
    print("生成→成交 贯通率 (playbook_instructions):")
    print(f"  指令总数 {instr_total}   已成交(有匹配trade) {instr_executed}  "
          f"({_pct(instr_executed, instr_total)})   已定局(win/loss) {instr_outcome}")
    print("-" * 72)
    print("\n说明: entry_vix/entry_session/trough_price/exit_telemetry 是 2026-07-11 新增字段,")
    print("      仅对【本次改动后】新开/新平的单子生效; 历史单子这些列为空属预期。")


if __name__ == "__main__":
    sys.exit(audit())
