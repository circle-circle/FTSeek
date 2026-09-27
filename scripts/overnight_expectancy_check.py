#!/usr/bin/env python3
"""隔夜仓 vs 日内仓的期望值对照 — 「要不要禁水下仓持夜」的裁决输入.

═══ 为什么需要这个脚本 ═══
2026-09-27 追 8/25 起系统净亏 (净 −98.77 / 毛 +1,511.82 / 费 1,610.59) 时,
三堆分解显示出血堆 −4,286.94 (23笔) 里 **OVERNIGHT_GAP_STOP 6 笔 = −1,898.95
= 全窗净亏的 19 倍**。这是最大单一可行动出血口, 但直接提「禁隔夜」踩
`tail-risk-defense-audit` skill 禁止清单第②条 (未测伤害总量就禁隔夜 =
用已知小成本换未知损失), 故先做伤害/收益对照。

═══ ⛔ 口径陷阱 (首版实踩, 会让裁决数字翻转) ═══
**跨夜判定禁用 `timedelta.days`**: 它是 timedelta 的"整天分量", 不是日历夜数。
  SNDK id=1473 entry 09-09T13:29 → exit 09-10T09:38 = 跨了 1 个日历夜, 但持仓
  20h09m ⇒ `(xt-et).days == 0` ⇒ 被误判成"日内仓", 其 GAP 亏 −251.82 被错分。
  首版 3 笔 GAP (−920.01) 全部误分, 把隔夜桶净亏从 −1,722.59 低估成 −802.58。
  ⇒ 正解: 比 `date()` 之差 (日历夜), 不比 timedelta。
另: entry_time/exit_time 在 trade_records 是带 `-04:00` 偏移的 ISO 串, 解析后
  取 `.date()` 即 ET 日历日, 无需手工 −12h (MEMORY: 禁手写时区偏移)。

═══ ⛔ 本脚本**不是新判据**, 不得据它重开已裁决的事 ═══
「隔夜跳空是真出血口」这个结论**已经裁决过并归属明确**: `docs/ADJUDICATED-CRITERIA.md`
C8 行原文 — "真出血口在 E 档涨幅上端 + 隔夜跳空 (大额亏损 13 笔中 OVERNIGHT_GAP_STOP 6 笔),
归 **B19 `eod_depth_gate`** ... 阈值预注册冻结, 不得拿该单案例放宽 — 误杀预算已满 1/1"。
B19 的回溯证据也已做过 (8/1-9/8 回放 24 笔: −2.5% 阈值 3/3 命中开盘死亡灾难, ΣΔ=+$410,
误杀 0), 现状 = **影子攒样中** (data/shadow/eod_depth_gate_report_latest.json, n=2/min_n=8,
verdict=KEEP_SAMPLING)。
⇒ 本脚本只提供**背景量级** (隔夜桶整体期望值、按 source 分解), 供理解 B19 为何值得等;
  **不提供新的上线依据**, 也不得用它论证"放宽 −2.5% 到 −2%" (C8 明令禁止)。

═══ 本脚本只做什么 ═══
只读 DB ⇒ 零交易行为变更、零产物写入、零推送、**不调用任何结算器** (结算器带写开关会重建产物)。

用法: ./venv/bin/python scripts/overnight_expectancy_check.py [--since 2026-08-25]
"""
import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "data" / "ftseek_us.db"

GHOST = "exit_reason NOT LIKE 'BROKER_GHOST%' AND exit_reason != 'ORPHAN_CLEANUP'"


def fee_of(m: dict) -> float:
    fb = (m or {}).get("fee_breakdown") or {}
    return (float((fb.get("entry") or {}).get("total") or 0)
            + float((fb.get("exit") or {}).get("total") or 0))


def parse_iso(s):
    """带 -04:00 偏移的 ISO 串 → naive datetime (ET 本地). 解析失败返回 None."""
    import datetime
    try:
        dt = datetime.datetime.fromisoformat(str(s)[:19])
        return dt
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-08-25",
                    help="现行体制起点 (禁混引旧体制灾难, 见 tail-risk skill 第1条)")
    a = ap.parse_args()

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        f"""SELECT id, symbol, COALESCE(pnl_net, realized_pnl, 0) AS net, exit_reason,
                   entry_time, exit_time, metadata_json
            FROM trade_records
            WHERE date(exit_time) >= ? AND exit_price > 0 AND {GHOST}""",
        (a.since,)).fetchall()
    con.close()

    # ── 按**日历夜**分桶 (不是 timedelta.days) ──
    agg = defaultdict(lambda: dict(n=0, net=0.0, fee=0.0, gross=0.0,
                                   gapn=0, gapnet=0.0, nongapn=0, nongapnet=0.0))
    skipped = 0
    night_detail = []
    for r in rows:
        m = json.loads(r["metadata_json"] or "{}")
        et, xt = parse_iso(r["entry_time"]), parse_iso(r["exit_time"])
        if et is None or xt is None:
            skipped += 1
            continue
        nights = (xt.date() - et.date()).days          # ← 日历夜
        key = "隔夜(≥1夜)" if nights >= 1 else "日内(0夜)"
        net = float(r["net"] or 0)
        b = agg[key]
        b["n"] += 1
        b["net"] += net
        f = fee_of(m)
        b["fee"] += f
        b["gross"] += net + f
        is_gap = "GAP" in (r["exit_reason"] or "")
        if is_gap:
            b["gapn"] += 1
            b["gapnet"] += net
        else:
            b["nongapn"] += 1
            b["nongapnet"] += net
        if nights >= 1:
            arm = m.get("overnight_arm") or {}
            night_detail.append(dict(
                id=r["id"], sym=r["symbol"], net=net, nights=nights,
                reason=r["exit_reason"], src=arm.get("source"),
                uw=arm.get("underwater_pct_net"), mae=float(m.get("mae_pct") or 0),
                cohort=m.get("cohort")))

    total_n = sum(b["n"] for b in agg.values())
    print(f"=== 隔夜 vs 日内 期望值对照 (现行体制, since={a.since}, n={total_n}"
          f"{f', 时间戳不可解析跳过 {skipped}' if skipped else ''}) ===")
    print(f"  {'类别':14s}{'n':>4s}{'净$':>11s}{'毛$':>11s}{'费$':>9s}"
          f"{'净/笔$':>9s}{'GAP笔':>7s}{'GAP净$':>11s}{'非GAP笔':>8s}{'非GAP净$':>11s}")
    for k in ("隔夜(≥1夜)", "日内(0夜)"):
        b = agg.get(k)
        if not b or not b["n"]:
            continue
        print(f"  {k:14s}{b['n']:>4d}{b['net']:>+11,.2f}{b['gross']:>+11,.2f}{b['fee']:>9,.2f}"
              f"{b['net']/b['n']:>+9,.2f}{b['gapn']:>7d}{b['gapnet']:>+11,.2f}"
              f"{b['nongapn']:>8d}{b['nongapnet']:>+11,.2f}")

    ov = agg.get("隔夜(≥1夜)", dict(n=0))
    dn = agg.get("日内(0夜)", dict(n=0))
    print(f"\n=== 「禁水下仓持夜」的伤害/收益对照 ===")
    if not ov.get("n"):
        print("  窗内无隔夜仓, 不可判。")
        return 0
    print(f"  收益 (避免 GAP 跳空止损)     : {abs(ov['gapnet']):>10,.2f}   ({ov['gapn']} 笔)")
    print(f"  代价 (砍掉非 GAP 的持夜收益) : {abs(ov['nongapnet']):>10,.2f}   ({ov['nongapn']} 笔)")
    print(f"  隔夜桶整体                   : {ov['net']:>+10,.2f}   净/笔 {ov['net']/ov['n']:+,.2f}")
    print(f"  日内桶整体                   : {dn.get('net', 0):>+10,.2f}   净/笔 "
          f"{dn.get('net', 0)/dn['n'] if dn.get('n') else 0:+,.2f}")
    net_gain = abs(ov["gapnet"]) - abs(ov["nongapnet"])
    print(f"\n  ⇒ 净差 = {net_gain:+,.2f} "
          f"({'避免的伤害 > 砍掉的收益 ⇒ 方向支持限制持夜' if net_gain > 0 else '⇒ 不支持'})")

    # ── 逐笔持夜明细 (看 GAP 是否集中在某一 source) ──
    print(f"\n=== 隔夜仓逐笔 (n={len(night_detail)}) ===")
    print(f"  {'id':>5s} {'sym':12s}{'夜':>3s}{'净$':>10s}{'MAE%':>6s} "
          f"{'水下%':>7s}{'source':22s}{'exit_reason':20s}")
    for d in sorted(night_detail, key=lambda x: x["net"]):
        uw = "" if d["uw"] is None else f"{float(d['uw']):+7.2f}"
        print(f"  {d['id']:>5d} {d['sym']:12s}{d['nights']:>3d}{d['net']:>+10,.2f}"
              f"{d['mae']:>6.2f} {uw:>7s} {str(d['src'] or ''):22s}{str(d['reason'] or ''):20s}")

    by_src = defaultdict(lambda: dict(n=0, net=0.0, gapn=0, gapnet=0.0))
    for d in night_detail:
        s = by_src[str(d["src"] or "(无 overnight_arm)")]
        s["n"] += 1
        s["net"] += d["net"]
        if "GAP" in str(d["reason"] or ""):
            s["gapn"] += 1
            s["gapnet"] += d["net"]
    print(f"\n=== 持夜来源分解 (overnight_arm.source) ===")
    print(f"  {'source':26s}{'n':>4s}{'净$':>11s}{'GAP笔':>7s}{'GAP净$':>11s}")
    for s in sorted(by_src, key=lambda x: by_src[x]["net"]):
        b = by_src[s]
        print(f"  {s:26s}{b['n']:>4d}{b['net']:>+11,.2f}{b['gapn']:>7d}{b['gapnet']:>+11,.2f}")

    print("\n" + "=" * 96)
    print("=== 判读 (归属已定, 本表只作背景量级) ===")
    print("=" * 96)
    print("  ① 「隔夜跳空是真出血口」已由 docs/ADJUDICATED-CRITERIA.md **C8 行裁决并归属 B19**")
    print("     `eod_depth_gate` (阈值 −2.5% 预注册冻结, 误杀预算已满 1/1, 明令禁止拿单案例放宽)。")
    print("  ② B19 现状 = 影子攒样中 (data/shadow/eod_depth_gate_report_latest.json: n=2 / min_n=8,")
    print("     trigger_sum_delta=+28.40, verdict=KEEP_SAMPLING)。⇒ **当前唯一正确动作是等攒样**,")
    print("     不是再提新门、更不是调 overnight_gap_stop_pct。")
    print("  ③ 反例锚 (勿提「一刀禁持夜」): 本窗非 GAP 的持夜仓 %d 笔净 %+.2f — 其中 MU id=1561"
          % (ov["nongapn"], ov["nongapnet"]))
    print("     (night_n=2) 是持夜豁免**救回** +15.67 的正面样本, 9/24 EOD 豁免裁决(三问Q3+W_WEEK)")
    print("     正是为它这类浅水修复仓而设 ⇒ 一刀禁会连修复路径一起杀。B19 的设计恰好只切深水下。")
    print("  ④ ⛔ overnight_gap_stop_pct(现 3.0) 的语义**不是跳空幅度门** — exit_engine.py L718:")
    print("     `_is_overnight_position(entry) and profit_pct <= -3.0`, 且规则2 注释(L302)明写")
    print("     「OVERNIGHT_GAP_STOP **名不副实** — 无 gap 校验, metadata 也不落 gap 字段,")
    print("     复盘不要去查 gap%」⇒ 判据是**触发时刻当前浮亏**, 本脚本表里的 MAE 是另一口径,")
    print("     不可用它论证门的松紧。收紧该阈值只减单笔亏损幅度、不减笔数, 且改交易行为。")
    print("  ⑤ 本脚本的「伤害/收益对照」是**桶级**期望值, 与 B19 的**仓级 Δ**(反事实EOD平−实际结局)")
    print("     不同轴, 不可互相替代引用 (B19 判据轴 = 触发组去重仓数, 不是本表的笔数)。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
