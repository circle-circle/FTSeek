#!/usr/bin/env python3
"""
存量 MFE/MAE 回填 (2026-07-11) — 退出引擎优化方案 §工作块A

背景: trade_records.mfe_pct/mae_pct 存量 91% 为 0 (mfe 359/394, mae 391/394 为 0),
阻塞 TRAILING_STOP 调优 (giveback 无法量化)。go-forward 采集已在 pipeline.py 修复, 本脚本回填存量。
注: 列内量纲是"百分比数值"(如 2.79 表示 2.79%), 与 pnl_pct 的"分数"(0.0279) 不同;
    列内 max=3.82, 实际无 >50 的脏值 (SANE_MAX 夹取纯为 go-forward 防御, 存量清 0 笔)。

数据源 (A1 调查确认):
- peak_price: 48/103 单持久化在 metadata_json → 可反算 mfe_pct。
- trough_price: 存量 0 单 (谷值追踪 7/11 才上线) → mae 无法从峰值反算, 仅清洗脏值。

规则 (与 pipeline.py 反算处完全一致):
- mfe 需修复条件: NULL / 0 / >SANE_MAX(脏)。
- 反算: LONG=(peak-entry)/entry*100, SHORT=(entry-peak)/entry*100; 兜底 mfe≥pnl_pct*100。
- 夹取: 结果 >SANE_MAX 判脏; 无可信源时脏值归 0 (0=视同缺失, 不塞假数据)。
- mae: 仅把脏值 (>SANE_MAX) 清 0 (无 trough 源无法回填, 保持诚实缺失)。

安全: 默认 dry-run 只预览; --commit 才写库。改前建议先 sqlite3 .backup。
"""
import argparse
import json
import sqlite3
import sys

SANE_MAX = 50.0  # 日内单笔 MFE/MAE 合理上限%; 超过判脏 (与 pipeline.py _MFE_MAE_SANE_MAX 一致)
DB = "data/ftseek_us.db"


def _calc_mfe(direction, entry, peak):
    if not (entry and entry > 0 and peak and peak > 0):
        return None
    if (direction or "LONG").upper() == "LONG":
        v = (peak - entry) / entry * 100
    else:
        v = (entry - peak) / entry * 100
    return round(v, 4) if v > 0 else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="真正写库 (默认 dry-run 只预览)")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, symbol, direction, entry_price, pnl_pct, mfe_pct, mae_pct, metadata_json "
        "FROM trade_records WHERE exit_price > 0"
    ).fetchall()

    stat = {"scanned": 0, "mfe_from_peak": 0, "mfe_from_realized": 0,
            "mfe_cleaned": 0, "mfe_no_source": 0, "mae_cleaned": 0}
    updates = []  # (id, new_mfe, new_mae)

    for r in rows:
        stat["scanned"] += 1
        cur_mfe = r["mfe_pct"]
        cur_mae = r["mae_pct"]
        entry = r["entry_price"]
        pnl_pct = r["pnl_pct"] or 0
        direction = r["direction"]

        peak = None
        if r["metadata_json"]:
            try:
                m = json.loads(r["metadata_json"])
                peak = float(m.get("peak_price", 0) or 0) or None
                if not entry:
                    entry = float(m.get("entry_price", 0) or 0) or None
            except Exception:
                pass

        # ── mfe 决策 ──
        mfe_dirty = (cur_mfe is None) or (cur_mfe == 0) or (cur_mfe > SANE_MAX)
        new_mfe = cur_mfe
        if mfe_dirty:
            calc = _calc_mfe(direction, entry, peak)
            realized = round(pnl_pct * 100, 4)  # 兜底: 已落袋有利变动
            cand = None
            src = None
            if calc is not None and calc > 0:
                cand = calc
                src = "peak"
            if realized > (cand or 0):
                cand = realized
                src = "realized" if src != "peak" else "peak+realized"
            if cand is not None and 0 < cand <= SANE_MAX:
                new_mfe = cand
                stat["mfe_from_peak" if "peak" in (src or "") else "mfe_from_realized"] += 1
            elif cur_mfe is not None and cur_mfe > SANE_MAX:
                new_mfe = 0.0  # 原脏值且无可信源 → 清 0
                stat["mfe_cleaned"] += 1
            else:
                new_mfe = cur_mfe  # 无源 (亏损单/无peak), 保持原状
                stat["mfe_no_source"] += 1

        # ── mae: 仅清脏 (无 trough 源) ──
        new_mae = cur_mae
        if cur_mae is not None and cur_mae > SANE_MAX:
            new_mae = 0.0
            stat["mae_cleaned"] += 1

        if new_mfe != cur_mfe or new_mae != cur_mae:
            updates.append((r["id"], new_mfe, new_mae, r["symbol"], cur_mfe, cur_mae))

    print(f"=== MFE/MAE 回填 ({'COMMIT' if args.commit else 'DRY-RUN'}) ===")
    print(f"扫描 {stat['scanned']} 单, 拟更新 {len(updates)} 单")
    print(f"  mfe 由 peak 反算   : {stat['mfe_from_peak']}")
    print(f"  mfe 由 realized 兜底: {stat['mfe_from_realized']}")
    print(f"  mfe 脏值清 0       : {stat['mfe_cleaned']}")
    print(f"  mfe 无源保持       : {stat['mfe_no_source']}")
    print(f"  mae 脏值清 0       : {stat['mae_cleaned']}")
    print()
    print("样本 (前 15 条变更):")
    for u in updates[:15]:
        print(f"  #{u[0]:<5} {u[3]:<10} mfe {u[4]} → {u[1]}  mae {u[5]} → {u[2]}")

    if args.commit and updates:
        for u in updates:
            conn.execute("UPDATE trade_records SET mfe_pct=?, mae_pct=? WHERE id=?",
                         (u[1], u[2], u[0]))
        conn.commit()
        print(f"\n✅ 已写库: {len(updates)} 单")
    elif not args.commit:
        print("\n(dry-run, 未写库; 加 --commit 执行)")
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
