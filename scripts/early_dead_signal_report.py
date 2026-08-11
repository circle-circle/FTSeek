#!/usr/bin/env python3
"""early_dead_signal_shadow 结算报告 (DEB-20260811-dead-order-early-detection).

对 data/early_dead_signal_shadow.jsonl 中未结算行, 配对 trade_records 最终结局:
  - 命中定义: 30min 检查点 MFE<0.2% (主规则) / 15min 检查点 MFE<0.15% (辅助)
  - 死单定义: exit_reason ∈ {TIME_EXIT, MFE_STALL_EXIT}
  - 差集对照: 命中行中 DEAD_SIGNAL_EXIT(60min) 未触发者 = "提前量独立价值"样本
裁决预注册 (攒样≥30 命中后评估):
  ①双窗口死单率均≥55% ②命中组实际净PnL<0 ③差集结局为负
用法: PYTHONPATH=. venv/bin/python scripts/early_dead_signal_report.py
"""
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHADOW = ROOT / "data" / "early_dead_signal_shadow.jsonl"
DB = ROOT / "data" / "ftseek_us.db"
DEAD = {"TIME_EXIT", "MFE_STALL_EXIT"}
THRESH = {15: 0.15, 30: 0.2}


def main():
    if not SHADOW.exists():
        print("shadow 文件尚不存在 (无交易越过检查点), 无结算。")
        return
    rows = [json.loads(l) for l in SHADOW.read_text().splitlines() if l.strip()]
    pending = [r for r in rows if not r.get("settled")]
    if not pending:
        print("无未结算行。")

    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row

    settled_ct = 0
    for r in pending:
        # 按 (symbol, entry_time) 配对 trade_records
        tr = conn.execute("""
            SELECT id, pnl_net, exit_reason, metadata_json
            FROM trade_records
            WHERE symbol = ? AND entry_time LIKE ? || '%'
              AND exit_time IS NOT NULL AND exit_time != ''
            ORDER BY id DESC LIMIT 1
        """, (r["symbol"], str(r.get("entry_time", ""))[:19])).fetchone()
        if not tr:
            continue  # 交易未平仓, 留待下次
        md = {}
        try:
            md = json.loads(tr["metadata_json"] or "{}")
        except Exception:
            pass
        r["trade_id"] = tr["id"]
        r["final_pnl_net"] = tr["pnl_net"]
        r["final_exit_reason"] = tr["exit_reason"] or ""
        r["is_dead"] = (tr["exit_reason"] or "") in DEAD
        r["dead_signal_60m_fired"] = "dead_signal_exit" in md or tr["exit_reason"] == "DEAD_SIGNAL_EXIT"
        r["settled"] = True
        settled_ct += 1

    if settled_ct:
        with open(SHADOW, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 汇总
    done = [r for r in rows if r.get("settled")]
    print(f"\n=== early_dead_signal_shadow (已结算 {len(done)} 行, 本次新结算 {settled_ct}) ===")
    for cp in (15, 30):
        seg = [r for r in done if r.get("checkpoint") == cp]
        if not seg:
            continue
        thr = THRESH[cp]
        hit = [r for r in seg if r.get("mfe_pct", 99) < thr]
        n_hit = len(hit)
        dead_hit = sum(1 for r in hit if r["is_dead"])
        net_hit = sum(r["final_pnl_net"] or 0 for r in hit)
        diff_set = [r for r in hit if not r["dead_signal_60m_fired"]]
        net_diff = sum(r["final_pnl_net"] or 0 for r in diff_set)
        print(f"\n[{cp}min 检查点] 总行 {len(seg)} | 命中(MFE<{thr}%) {n_hit}")
        if n_hit:
            print(f"  死单率: {dead_hit}/{n_hit} = {100*dead_hit/n_hit:.0f}%  (裁决线 ≥55%)")
            print(f"  命中组净PnL: {net_hit:+.1f}$  (裁决线 <0)")
            print(f"  差集(30min命中但60min DEAD_SIGNAL未触发): {len(diff_set)}笔 净{net_diff:+.1f}$")
    if settled_ct == 0 and not done:
        print("(全部未平仓, 待交易结束后再跑)")


if __name__ == "__main__":
    main()
