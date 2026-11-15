"""「只跑正 edge 子集」净 bps 重算 — A10 口径分层 + 可实施性子集 + 实验可行性算术。

只读: sqlite mode=ro, 不写库、不下单、不改配置。
口径: A10 = trade_date>=2026-07-01 ∧ data_quality∈(clean,real) ∧ source_type∉(POSITION_SYNC,MANUAL)
      ∧ exit_time/pnl_net 非空 (SSOT = app/core/db_utils.ERA_CLEAN_PERFORMANCE_WHERE)。
      bps = Σ值 / Σ(quantity*entry_price) * 1e4 (ratio-of-sums, 不报均值比)。
      时段由 entry_time 自身偏移转 America/New_York (库内混存 -04:00/-05:00/+08:00/无偏移;
      固定 -4h 会让 11 月行偏 1 小时)。
实施性标签: entry=入场时可知(可作门) / eod=收盘前决策可知 / post=平仓后才有(禁作门, T19)。
t 值一律日聚类 (先按 trade_date 聚合 Σnet/Σnotional 再算), 逐笔 t 会低估同日相关性。

用法: python scripts/positive_edge_subset_recompute.py
"""
import sqlite3
import statistics
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DB = "data/ftseek_us.db"
TAIL = {"OVERNIGHT_GAP_STOP", "SL_INITIAL"}   # 尾部族: 0% 胜率事件

SQL = """
SELECT trade_date, symbol, direction, entry_time, exit_time, entry_session, owner_strategy,
       strategy_id, decision_id, exit_reason, quantity, entry_price, realized_pnl, fees_est,
       pnl_net, mfe_pct, holding_hours
FROM trade_records
WHERE exit_time IS NOT NULL AND pnl_net IS NOT NULL
  AND trade_date >= '2026-07-01'
  AND COALESCE(data_quality,'') IN ('clean','real')
  AND COALESCE(source_type,'') NOT IN ('POSITION_SYNC','MANUAL')
"""


def load():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(SQL)]
    bad = 0
    for r in rows:
        dt = datetime.fromisoformat(r["entry_time"])
        dt = dt.replace(tzinfo=ET) if dt.tzinfo is None else dt
        r["mod"] = (lambda e: e.hour * 60 + e.minute)(dt.astimezone(ET))
        r["notional"] = r["quantity"] * r["entry_price"]
        r["mfe"] = r["mfe_pct"] or 0.0
        if abs((r["realized_pnl"] or 0) - (r["fees_est"] or 0) - r["pnl_net"]) > 0.02:
            bad += 1
    if bad:
        raise SystemExit(f"毛−费≠净 行数 {bad}: 口径不自洽, 停止")
    return rows


def bps_row(sub):
    no = sum(r["notional"] for r in sub)
    g = sum(r["realized_pnl"] for r in sub)
    f = sum(r["fees_est"] for r in sub)
    net = sum(r["pnl_net"] for r in sub)
    return no, g, f, net


def day_t(sub):
    by = {}
    for r in sub:
        d = by.setdefault(r["trade_date"], [0.0, 0.0])
        d[0] += r["pnl_net"]
        d[1] += r["notional"]
    xs = [v[0] / v[1] * 1e4 for v in by.values() if v[1]]
    n = len(xs)
    if n < 2:
        return n, None
    m = sum(xs) / n
    sd = statistics.stdev(xs)
    return n, (m / (sd / n ** 0.5) if sd else None)


def show(label, sub, tag=""):
    if not sub:
        print(f"  {label:46s} n=0")
        return
    no, g, f, net = bps_row(sub)
    wr = sum(1 for r in sub if r["pnl_net"] > 0) / len(sub) * 100
    dn, t = day_t(sub)
    tv = "  n/a" if t is None else f"{t:+5.2f}"
    print(f"  {label:46s} n={len(sub):4d} WR={wr:5.1f}% net=${net:8.0f} "
          f"{g/no*1e4:7.2f}/{f/no*1e4:6.2f}/{net/no*1e4:8.2f}bps t={tv} ({dn}d) {tag}")


def in_open45(r):
    return 570 <= r["mod"] < 585


def main():
    rows = load()
    print(f"A10 样本 n={len(rows)} 分界 2026-07-01\n")
    print("第 1 层 · 单轴 (毛/费/净 bps)")
    show("A10 基线", rows)
    for v in ("SHORT", "LONG"):
        show(f"方向={v}", [r for r in rows if r["direction"] == v], "[entry]")
    show("EXT 旁路", [r for r in rows if r["owner_strategy"] == "extended_hours_entry"], "[entry]")
    show("剔除 09:30-09:45", [r for r in rows if not in_open45(r)], "[entry]")
    show("仅 09:30-09:45", [r for r in rows if in_open45(r)], "[entry]")
    show("名义>=4200 ∧ 价>=26 (费用线)", [r for r in rows if r["notional"] >= 4200 and r["entry_price"] >= 26], "[entry]")
    show("剔尾部族 (post 上限)", [r for r in rows if r["exit_reason"] not in TAIL], "[post]")
    show("仅尾部族", [r for r in rows if r["exit_reason"] in TAIL], "[post]")
    show("剔死单 mfe<0.3 (post 上限)", [r for r in rows if r["mfe"] >= 0.3], "[post]")
    show("同日平仓", [r for r in rows if (r["exit_time"] or "")[:10] == r["trade_date"]], "[eod]")
    show("跨夜仓", [r for r in rows if (r["exit_time"] or "")[:10] != r["trade_date"]], "[eod]")

    print("\n第 2 层 · 可实施组合")
    S1 = [r for r in rows if r["direction"] == "SHORT" and r["mod"] >= 585]
    S2 = [r for r in S1 if r["notional"] >= 4200 and r["entry_price"] >= 26]
    DA = lambda r: (r["decision_id"] or "").startswith("DA-")
    show("S1 = SHORT ∧ ET>=09:45", S1, "[entry]")
    show("S2 = S1 ∧ 费用地板", S2, "[entry]")
    show("S1 ∩ DA- 前缀", [r for r in S1 if DA(r)], "[entry]")
    show("SHORT 开盘45min内 (对照臂)", [r for r in rows if r["direction"] == "SHORT" and in_open45(r)], "[entry]")
    show("LONG ∧ ET>=09:45 (对照臂)", [r for r in rows if r["direction"] == "LONG" and r["mod"] >= 585], "[entry]")

    print("\n第 3 层 · 稳健性")
    months = sorted({r["trade_date"][:7] for r in rows})
    for name, sub in (("S1", S1), ("S2", S2)):
        cells = []
        for m in months:
            s = [r for r in sub if r["trade_date"][:7] == m]
            cells.append(f"{m[5:]}:{bps_row(s)[3]/bps_row(s)[0]*1e4:6.1f}({len(s):2d})" if s else f"{m[5:]}:   — ")
        print(f"  {name} 月度净bps  " + "  ".join(cells))
    for name, sub in (("S1", S1), ("S2", S2)):
        srt = sorted(sub, key=lambda r: -r["pnl_net"])
        tot = sum(r["pnl_net"] for r in sub)
        no1 = sum(r["notional"] for r in srt[1:])
        print(f"  {name} 剔top1后 {tot-srt[0]['pnl_net']:8.0f} ({(tot-srt[0]['pnl_net'])/no1*1e4:7.2f}bps) "
              f"top1 占 {srt[0]['pnl_net']/tot*100:4.0f}% top3 占 {sum(r['pnl_net'] for r in srt[:3])/tot*100:4.0f}%")
    no, g, f, net = bps_row([r for r in rows if in_open45(r) and r["exit_reason"] not in TAIL])
    print(f"  09:30-09:45 窗内剔尾部族: n={sum(1 for r in rows if in_open45(r) and r['exit_reason'] not in TAIL)} "
          f"net=${net:8.0f} {net/no*1e4:7.2f}bps  ← 窗内亏损是否只在尾部")

    print("\n第 4 层 · 实验可行性算术")
    no_s1, g_s1, f_s1, net_s1 = bps_row(S1)
    xs = [r["pnl_net"] / r["notional"] * 1e4 for r in S1]
    sd = statistics.stdev(xs)
    ndays = len({r["trade_date"] for r in rows})
    per_mo = len(S1) / ndays * 21
    no_mo = no_s1 / ndays * 21
    nbps = net_s1 / no_s1 * 1e4
    print(f"  S1 逐笔净bps sd={sd:.0f}; 月均 {per_mo:.1f} 笔 / 名义 ${no_mo:,.0f}; "
          f"按实测 {nbps:.1f}bps → ${nbps/1e4*no_mo:,.0f}/月")
    for delta in (5, 10, 20):
        need = 15.7 * (sd / delta) ** 2
        print(f"    以净 bps 检出 δ={delta}bps 需 n≈{need:,.0f} 笔 ≈ {need/per_mo:.0f} 个月")
    print("  ⇒ 净 bps 无法作实盘实验判据; 判据应为成交质量(滑点/追价 bps) + 引擎臂同号。")


if __name__ == "__main__":
    main()