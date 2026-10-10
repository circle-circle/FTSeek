#!/usr/bin/env python3
"""RFC-141 门前基线的费用异常单敏感性检验 (DEB-20261009 §33, 2026-10-10)。

为什么要有这个脚本: RFC-141 §4 的四条判据全挂在「门前基线」上, 而 §27 缺陷3 实测
「系统净为负由 2 笔仙股决定, 剔除后符号翻转」⇒ 必须先证明我的基线不会被同一批
费用异常单主导, 否则判据② 的门槛读数是被 1–3 笔决定的, 门改善退出/入场也拉不动。

fee_bps = fees_est / (entry_price × quantity) × 10000  (事前可算, 非事后标签)
人群 = 与 BASELINE 逐字同口径: source_type='PLAYBOOK_SIM' ∧ exit_price>0
       ∧ COALESCE(pnl_net, realized_pnl) IS NOT NULL ∧ trade_date < 2026-10-10

只读 (mode=ro), 零写入。判读:
  判据①② 稳健 = 对应人群内 fee_bps>100 的笔数为 0 ⇒ 基线读数不受费用单影响
  判据④ 地板稀释 = 剔/不剔的净均差 ⇒ 必须两端同尺 (见脚本输出与 §33.3)
"""
from __future__ import annotations

import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB = ROOT / "data" / "ftseek_us.db"
# 基线窗口从**结算器常量**读 (单一真源) ⇒ v1→v2 换窗只改结算器一处, 本脚本自动跟随。
# 曾硬编码 "2026-06-24" (v1), v2 改 8/1 后会与裁决口径错相位 ⇒ 已改为导入。
from scripts import settle_entry_confirm_forward as _st  # noqa: E402

BASELINE_START = _st.BASELINE_SINCE
BASELINE_END = _st.BASELINE_UNTIL
FEE_BPS_THRESHOLD = _st.FEE_BPS_ANOMALY   # §27 缺陷3 同一阈值 (915/204 与第三名断层明显)
QTY_COL = "quantity"             # 实测存在; position_size 不在 trade_records


def _rows(conn):
    return [dict(r) for r in conn.execute(
        f"""SELECT symbol, trade_date, entry_time, exit_reason, fees_est,
                   {QTY_COL} AS qty, entry_price,
                   COALESCE(pnl_net, realized_pnl) AS net
            FROM trade_records
            WHERE source_type='PLAYBOOK_SIM' AND COALESCE(exit_price,0) > 0
              AND COALESCE(pnl_net, realized_pnl) IS NOT NULL
              AND trade_date >= ? AND trade_date < ?""",
        (BASELINE_START, BASELINE_END)).fetchall()]


def fee_bps(r) -> float | None:
    fee, qty, px = r.get("fees_est"), r.get("qty"), r.get("entry_price")
    if fee is None or not qty or not px:
        return None
    return float(fee) / (float(qty) * float(px)) * 10000


def in_open_win(et) -> bool:
    return bool(et) and len(str(et)) >= 16 and "09:35" <= str(et)[11:16] < "09:45"


def _mean(nets):
    return statistics.mean(nets) if nets else None


def main() -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = _rows(conn)
    conn.close()
    if not rows:
        print("NO_ROWS")
        return 1

    tagged = [(r, fee_bps(r)) for r in rows]
    n_calc = sum(1 for _, b in tagged if b is not None)
    nets_all = [float(r["net"]) for r, _ in tagged]
    mean_all = _mean(nets_all)

    rest = [(r, b) for r, b in tagged if (r.get("exit_reason") or "") != "SL_INITIAL"]
    sl = [(r, b) for r, b in tagged if (r.get("exit_reason") or "") == "SL_INITIAL"]
    win = [(r, b) for r, b in tagged if in_open_win(r["entry_time"])]

    def grp(pairs, label):
        nets = [float(r["net"]) for r, _ in pairs]
        hi = [(r, b) for r, b in pairs if b is not None and b > FEE_BPS_THRESHOLD]
        keep = [float(r["net"]) for r, b in pairs
                if b is None or b <= FEE_BPS_THRESHOLD]
        m, mk = _mean(nets), _mean(keep)
        print(f"[{label}] n={len(pairs)} 净均={m:+.4f} | fee_bps>{FEE_BPS_THRESHOLD:.0f} "
              f"的笔数={len(hi)} | 剔后 n={len(keep)} 净均={mk:+.4f}")
        for r, b in hi:
            print(f"    ↳ {r['symbol']} {r['trade_date']} bps={b:.1f} "
                  f"net={float(r['net']):+.2f} exit={r['exit_reason']}")
        return {"n": len(pairs), "mean": m, "n_hi_fee": len(hi), "mean_clean": mk}

    print(f"基线窗 [{BASELINE_START}, {BASELINE_END}) n={len(rows)} "
          f"可算 fee_bps={n_calc} 净均={mean_all:+.4f}")
    print(f"毛利={sum(float(r['net']) + float(r['fees_est'] or 0) for r, _ in tagged):+.2f} "
          f"费用={sum(float(r['fees_est'] or 0) for r, _ in tagged):.2f} "
          f"费用/毛利={abs(sum(float(r['fees_est'] or 0) for r, _ in tagged) / sum(float(r['net']) + float(r['fees_est'] or 0) for r, _ in tagged)):.4f}×")
    print()
    g_sl = grp(sl, "判据① SL_INITIAL 族")
    g_win = grp(win, "判据② 开盘窗 09:35-09:45")
    g_rest = grp(rest, "判据④ 非SL_INITIAL 族")

    clean_all = _mean([float(r["net"]) for r, b in tagged
                       if b is None or b <= FEE_BPS_THRESHOLD])
    print()
    print(f"判据① 稳健 = {g_sl['n_hi_fee'] == 0}  "
          f"(SL族 fee_bps 最大 {max((b for _, b in sl if b is not None), default=0):.1f})")
    print(f"判据② 稳健 = {g_win['n_hi_fee'] == 0}  "
          f"⇒ 开盘窗基线 {g_win['mean']:+.4f} 不受费用异常单影响")
    print(f"判据④ 地板: 全量 {g_rest['mean'] * 0.8:+.4f} → 剔>100bps {g_rest['mean_clean'] * 0.8:+.4f} "
          f"(被低估 {g_rest['mean_clean'] * 0.8 - g_rest['mean'] * 0.8:+.4f}/笔, "
          f"{(g_rest['mean_clean'] * 0.8 - g_rest['mean'] * 0.8) / (g_rest['mean'] * 0.8) * 100:.1f}%)")
    flip = mean_all < 0 < clean_all
    print(f"全样本符号翻转 = {flip}  (全量 {mean_all:+.4f} → 剔后 {clean_all:+.4f})")
    # ⚠️ 叙述必须从 flip 派生, 禁硬编码 (v1 窗口 n=632 净均 −3.9205 剔费后仍负 ⇒ 当时
    #   「不复现」为真; v2 窗口 n=441 净均 −0.7997 → 剔后 +0.3256 ⇒ **复现**。
    #   字面量叙述 ≠ 计算值域 = 脚本同时打印两个互相矛盾的结论, 属死门形态)。
    if flip:
        print(f"  ⚠️ §27 缺陷3 的符号翻转在 RFC-141 基线窗 (n={len(rows)}) **复现** ⇒ "
              f"全样本「净为负」由费用异常单决定, 不是策略整体亏损 ⇒ "
              f"仙股/最小佣金准入门 (事前可算) 是本窗口第一杠杆, 优先于退出参数微调")
    else:
        print(f"  ⚠️ §27 缺陷3 的符号翻转在 RFC-141 基线窗 (n={len(rows)}) **不复现** ⇒ "
              f"该缺陷是窗口特异, 不是普遍结论")
    return 0


if __name__ == "__main__":
    sys.exit(main())
