#!/usr/bin/env python3
"""十月裁决窗口统计功效前置计算 (2026-08-24, OKR-2026-09 S5, 复查路线图第1项).

为何前置: 8月末局教训是 n=321 攒够了却分不清噪声 (宪章第9律: pooled 显著性被同日
聚集夸大 ~8 倍)。十月 E2 裁决若不提前反推"能分辨多小的效应", 大概率重演空手而归。
本脚本在冻结期第一天用历史实跑数据回答三个问题:
  Q1 当前日聚类波动下, 十月窗口(~22个交易日)最多能分辨多大的净效应?
  Q2 要分辨 δ* ($/笔 或 bps) 至少需要多少个独立交易日?
  Q3 提高日内供给(笔数)能不能替代拉长时间窗? —— 不能: 日聚类口径下 MDE 只随
     独立天数收缩, 同日加仓只降截面噪声不增独立信息 (这正是宪章第9律的本意)。

口径 (与 ic_tracker._day_clustered_ic / signal_ic_tracker 完全一致):
  每日值 = 当日全部平仓单 pnl_net 均值; t = mean/(sd/√n);
  MDE(n) = 2.8 × sd / √n   [双侧 α=0.05, 功效 80%, z_{α/2}+z_β ≈ 2.80]
  双臂对比 (如 tripwire-on vs DA 对照组) 用保守上界 MDE_2arm(n) = 2.8 × sd × √(2/n)

只读 DB, 零行为变更, 冻结期内可随时重跑。
用法: PYTHONPATH=. venv/bin/python scripts/october_power_analysis.py [--json]
"""
import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "ftseek_us.db"
Z_CRIT_PLUS_POWER = 2.8          # 双侧 α=0.05 + 功效 80%
OCT_TRADING_DAYS = 22            # 2026年10月常规交易日数 (无假日修正)


def load_daily_stats():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT trade_date, pnl_net, realized_pnl
        FROM trade_records
        WHERE source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE')
          AND exit_price > 0 AND symbol NOT LIKE '%TEST%'
    """).fetchall()
    conn.close()

    by_date = {}
    notionals = []
    for r in rows:
        by_date.setdefault(r["trade_date"], []).append(r["pnl_net"])
    return rows, by_date


def _mde(sd: float, n: int, arms: int = 1) -> float:
    return Z_CRIT_PLUS_POWER * sd * math.sqrt(arms / n)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="追加输出 JSON 路径行")
    args = ap.parse_args()

    rows, by_date = load_daily_stats()
    if len(by_date) < 3:
        print("样本不足 (<3 个交易日)")
        return 1

    dates = sorted(by_date)
    daily_means = [sum(v) / len(v) for v in (by_date[d] for d in dates)]
    n_days = len(daily_means)
    n_trades = len(rows)
    mean_dm = sum(daily_means) / n_days
    sd_dm = math.sqrt(sum((x - mean_dm) ** 2 for x in daily_means) / (n_days - 1))
    t_stat = mean_dm / (sd_dm / math.sqrt(n_days)) if sd_dm > 0 else 0.0

    med_notional = _median_notional(rows)
    mde_oct = _mde(sd_dm, OCT_TRADING_DAYS)
    mde_oct_bps = mde_oct / med_notional * 1e4 if med_notional else float("nan")

    print("=" * 72)
    print("十月裁决窗口统计功效前置计算 (日聚类口径, 宪章第9律)")
    print("=" * 72)
    print(f"样本: {n_trades} 笔平仓 PLAYBOOK_SIM/LIVE · {n_days} 个交易日 "
          f"({dates[0]} → {dates[-1]}) · 供给 {n_trades / n_days:.1f} 笔/日")
    print(f"日均值: mean=${mean_dm:+.2f}/笔  sd=${sd_dm:.2f}  t={t_stat:+.2f} "
          f"(当前窗口{'不可分辨' if abs(mean_dm) <= _mde(sd_dm, n_days) else '可分辨'})")

    # ── Q1: 各窗口长度下的可分辨效应 ──
    print("\n── Q1 单样本 (净期望 vs 0): 各窗口 MDE ──")
    print(f"{'交易日':>6} {'MDE $/笔':>12} {'MDE (bps)':>10}")
    for nd in [5, 10, 15, OCT_TRADING_DAYS, 30, 60]:
        m = _mde(sd_dm, nd)
        print(f"{nd:>6} {m:>12.2f} {m / med_notional * 1e4:>10.1f}")

    # ── Q2: 分辨 δ* 所需最少交易日 ──
    print("\n── Q2 分辨目标效应所需最少交易日 (ceil((2.8·sd/δ)²)) ──")
    print(f"{'δ $/笔':>8} {'需交易日':>8} {'≈月数':>6}")
    for delta in [1, 2, 5, 10, 20]:
        need = math.ceil((Z_CRIT_PLUS_POWER * sd_dm / delta) ** 2) if delta > 0 else 0
        print(f"{delta:>8} {need:>8} {need / OCT_TRADING_DAYS:>6.1f}")
    print(f"{'δ bps':>8} {'需交易日':>8} {'≈月数':>6}")
    for bps_d in [2, 5, 10, 20]:
        delta_usd = bps_d * med_notional / 1e4
        need = math.ceil((Z_CRIT_PLUS_POWER * sd_dm / delta_usd) ** 2)
        print(f"{bps_d:>8} {need:>8} {need / OCT_TRADING_DAYS:>6.1f}")

    # ── Q3: 双臂对比 (保守独立两臂上界) 与供给问题 ──
    m2 = _mde(sd_dm, OCT_TRADING_DAYS, arms=2)
    print(f"\n── Q3 双臂对比 (10月, 独立两臂保守上界) ──")
    print(f"MDE_2arm(22日) = ${m2:.2f}/笔 ({m2 / med_notional * 1e4:.1f} bps) "
          f"— 配对设计可再收窄, 需实验后以配对差值 sd 重估")

    # ── 方差分解 (聚类随机化设计效应): "加大日内供给能救功效吗?" ──
    # 日均值方差 ≈ τ²(日间效应) + σ²w/k̄(日内截面噪声)。若日间项主导, 翻倍供给
    # 对 MDE 几乎无改善 —— 这决定"攒笔数"是不是一条死路。
    k_bar = n_trades / n_days
    ss_w, n_w = 0.0, 0
    for d in dates:
        v = by_date[d]
        if len(v) > 1:
            m_d = sum(v) / len(v)
            ss_w += sum((x - m_d) ** 2 for x in v)
            n_w += len(v) - 1
    var_w = ss_w / n_w if n_w > 0 else float("nan")
    tau2 = max(0.0, sd_dm ** 2 - var_w / k_bar)
    print(f"\n── 日内供给翻倍的设计效应推演 ──")
    print(f"日内截面方差 σ²w={var_w:.0f} · 推算日间效应方差 τ²={tau2:.0f} "
          f"(占比 {tau2 / sd_dm ** 2 * 100:.0f}%)")
    for mult_k in [2, 4]:
        sd_new = math.sqrt(tau2 + var_w / (k_bar * mult_k))
        m_new = _mde(sd_new, OCT_TRADING_DAYS)
        print(f"供给 ×{mult_k} (≈{k_bar * mult_k:.0f} 笔/日): "
              f"sd→${sd_new:.0f}, MDE→${m_new:.2f}/笔 "
              f"(仅改善 {(1 - m_new / mde_oct) * 100:.0f}%)")

    verdict = (
        f"结论: 十月单样本可分辨 ≥${mde_oct:.2f}/笔 ({mde_oct_bps:.1f} bps); "
        f"若裁决阈值低于此, 必须延长窗口或改配对/分层设计, 加大日内供给无效"
    )
    print("\n" + verdict)

    if args.json:
        out = {
            "asof_trade_date": dates[-1], "n_trades": n_trades, "n_days": n_days,
            "daily_mean": round(mean_dm, 4), "daily_sd": round(sd_dm, 4),
            "t": round(t_stat, 3), "median_notional": round(med_notional, 2),
            "october_mde_usd": round(mde_oct, 4),
            "october_mde_bps": round(mde_oct_bps, 2),
            "october_mde_2arm_usd": round(m2, 4),
        }
        p = ROOT / "data" / "october_power_report.json"
        p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON → {p}")
    return 0


def _median_notional(rows):
    """名义额中位数: pnl_net 无对应名义列, 用 |pnl_pct| 反推不可靠 → 直接查入场额."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    vals = [r[0] for r in conn.execute(
        "SELECT entry_price * quantity FROM trade_records "
        "WHERE source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') AND exit_price>0 "
        "AND symbol NOT LIKE '%TEST%' AND entry_price*quantity > 0")]
    conn.close()
    if not vals:
        return float("nan")
    vals.sort()
    return vals[len(vals) // 2]


if __name__ == "__main__":
    raise SystemExit(main())
