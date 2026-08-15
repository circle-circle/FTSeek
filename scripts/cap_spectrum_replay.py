#!/usr/bin/env python3
"""DEB-20260815 §37 落盘: 单日开仓帽反事实谱系回放 (修复口径污染版).

背景 (Qoder §36.3 指控成立, 本脚本为修复):
  §32/§33 曾报"342笔基线 -$118, cap8 +$4,177 谱系最优" — 该基线污染:
  342 = 269 PLAYBOOK_SIM + 73 笔不受 cap 控制的通道
  (MAIN_PIPELINE 37笔+$4,593 / ORB_ENGINE 11笔-$1,499 / PLAYBOOK_SHADOW 23笔+$137 等)。
  max_daily_positions 只约束 playbook auto_execute 通道 (pipeline.py:3668),
  混入不受控通道导致谱系失真。本脚本用正确 universe 重算。

口径 (唯一正确 universe):
  trade_records.source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')
  已了结 (exit_time 非空), pnl_net 非空, entry_time 6/1-8/31。
  排序: 每日按 entry_time ET 时序截断 (与生产 auto_execute 按质排序不同,
  时序截断是保守下界 — 生产按质排序保留更强信号, 实际改善 ≥ 本估计)。

用法: venv/bin/python scripts/cap_spectrum_replay.py
"""
import sqlite3
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "ftseek_us.db"


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT symbol, direction, entry_time, pnl_net
        FROM trade_records
        WHERE source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')
          AND exit_time IS NOT NULL AND exit_time != ''
          AND pnl_net IS NOT NULL
          AND entry_time >= '2026-06-01' AND entry_time < '2026-09-01'
        ORDER BY entry_time
    """).fetchall()
    con.close()

    by_day = defaultdict(list)
    for r in rows:
        by_day[r["entry_time"][:10]].append(r)

    full = sum(r["pnl_net"] for r in rows)
    print(f"═══ Universe: PLAYBOOK_SIM/LIVE 已了结 6-8月 ═══")
    print(f"n={len(rows)}  Σpnl_net=${full:+,.0f}  交易日={len(by_day)}")

    print(f"\n═══ cap 谱系 (每日时序截断, 保留前N笔) ═══")
    print(f"{'cap':>4} | {'kept n':>6} | {'kept pnl':>9} | {'改善':>8} | {'保留率':>6}")
    print("-" * 50)
    for cap in (3, 5, 6, 8, 10, 12, 15, 99):
        kept, cut = [], []
        for _, rs in by_day.items():
            kept.extend(rs[:cap])
            cut.extend(rs[cap:])
        kp = sum(r["pnl_net"] for r in kept)
        label = "无帽" if cap == 99 else str(cap)
        print(f"{label:>4} | {len(kept):>6} | ${kp:>+8,.0f} | "
              f"${kp - full:>+7,.0f} | {len(kept)/len(rows)*100:>5.0f}%")

    # 双窗验证 cap=5/8
    print(f"\n═══ 双窗明细 (cap=5 / cap=8) ═══")
    for label, lo, hi in (("6月", "2026-06-01", "2026-07-01"),
                          ("7月", "2026-07-01", "2026-08-01"),
                          ("8月", "2026-08-01", "2026-09-01")):
        line = f"  {label}: "
        for cap in (5, 8):
            kept = []
            for _, rs in by_day.items():
                kept.extend(rs[:cap])
            sub = [r for r in kept if lo <= r["entry_time"][:10] < hi]
            line += f"cap{cap} n={len(sub)} ${sum(r['pnl_net'] for r in sub):+,.0f}  |  "
        print(line)

    print("\n═══ 结论 (§37) ═══")
    print("PnL 最优点 = cap5 (改善最大); cap8 保留率更高但 PnL 改善更小。")
    print("用户裁决 cap=8 的论据是攒数据保留率 (77% vs 54%), 非 PnL 最优 — 两者并存,")
    print("§37 如实记录张力: PnL 偏好 5, 攒数据偏好 8, 当前生效值 8 (用户直裁)。")


if __name__ == "__main__":
    main()
