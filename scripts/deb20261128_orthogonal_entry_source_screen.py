#!/usr/bin/env python3
"""DEB-20261128-livermore §16 只读「正交入场源」筛查探针 (record-only, 零交易行为变更).

用途 (用户「继续」→ 按 §15 门筛查现有数据里有没有真正正交、毛 edge>0 的候选入场源):
  把 clean-era PLAYBOOK_SIM 已平仓成交按 **入场源类别** (decision_id 前缀) 切片,
  **毛 edge 优先** (费前 gross bps 先于净) + 日聚类 t/MDE80 + 对 DA 正交性 (符号-日重叠)
  + **P0.5 撞车分类** (落入 C8–C18 哪条封闭家族) + EXT-盘前腿 §15/C13 复活门逐项检验。
  与既有 scripts/edge_discovery_screener.py 的区别: 后者对现有成交流做 **净额** 切片稳健筛
  (本质=「保留正期望子集」=选择轴 C12 已封); 本探针做 **毛 edge 优先 + 入场源类别撞车分类
  + 对 DA 增量** 的 **源类别** 筛查, 回答「有没有可直接立项的新入场源」, 非选 DA 子集。

只读承诺: sqlite `mode=ro`, 不写库 / 不下单 / 不改任何文件 / 不 import app.*
  (避免 db_utils 全局 sqlite3.connect monkey-patch 副作用; 与 deb20261128_livermore_probe 同款)。
  仅 import scripts.utils.da_sample (纯 stdlib 只读分层助手, 零 app 依赖零写)。默认仅控制台。

口径 (与 docs/ADJUDICATED-CRITERIA.md / §3 探针一致):
  A10 现役时代谓词 = ERA_CLEAN_PERFORMANCE_WHERE 逐字硬编码副本 (改边界=重裁)。
  A9  首跑测试批排除 = COALESCE(json_extract(metadata_json,'$.exclusion_reason'),'')=''。
  人群 = source_type='PLAYBOOK_SIM' ∧ exit_price>0 ∧ 净额可测; 入场源 = decision_id 前缀。
  收养/补录记账行 (sync_recovery_backfill / bookkeeping_compensation / adopt_to_playbook)
    经 SSOT split_signal_artifact 分出单列 (其 MFE/ATR/regime 为收养时近似值, 会造虚假切片),
    不静默丢弃 (§16 与 edge_discovery_screener 同纪律)。
  gross_bps = 10000×Σrealized_pnl/Σ入场名义额 (费前, 名义加权); net_bps 同口径用 pnl_net。
  日聚类 t = 逐笔 gross_bps 的日等权均值 / (日间 stdev/√天数); MDE80 = 2.802×se。
  per-trade net mean% = mean(net_pnl_pct)×100 (C13 复活门 0.30% 地板用逐笔均值口径)。

局限 (必读): SIM_only, fees_est 为佣金/监管费估算 (借券/资金/盘前流动性滑点/数据/模型成本未计),
  非实盘盈利证明; 历史回放非前瞻; 多重检验 (筛了多源/多子切片, 单源 t 高须 Harvey-Liu-Zhu≥3
  且前瞻确认); n<30 不下结论; 效应<MDE 写「不可分辨」非「已证伪」。撞车分类是治理判定非统计判定:
  落入 C8–C18 封闭家族的源即便统计强显著也 **不是自由开放新源**, 须先过复活门 + 用户裁。

复现: PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. ./venv/bin/python \
        scripts/deb20261128_orthogonal_entry_source_screen.py
"""
from __future__ import annotations

import json
import sqlite3
import statistics as st
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.utils.da_sample import split_signal_artifact

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "ftseek_us.db"
ET = ZoneInfo("America/New_York")
Z80 = 1.2815515655446004
MDE_Z = 2.802

# A10 SSOT 逐字副本 (app/core/db_utils.ERA_CLEAN_PERFORMANCE_WHERE)。
ERA = (
    "AND trade_date >= '2026-07-01' "
    "AND COALESCE(data_quality,'') IN ('clean','real') "
    "AND COALESCE(source_type,'') NOT IN ('POSITION_SYNC','MANUAL')"
)
A9 = "AND COALESCE(json_extract(metadata_json,'$.exclusion_reason'),'')=''"

# P0.5 撞车分类 (治理判定, 引 docs/ADJUDICATED-CRITERIA.md 终裁原文; 非统计判定)。
# 落入封闭家族 ⇒ 即便统计强显著也不是自由开放新源 (§15 门)。
COLLISION = {
    "DA": "基线 scalp 类别 (参照通道本身, 非新源); 毛 edge≈0 (§3/§14)",
    "universe_expansion:2026": "C18 封闭 (UE-inject 重开禁); 毛 edge 负",
    "EXT": "既有受治理通道; 盘前腿=C13 盘前顺势家族 + C10 财报方向邻接; "
           "研究臂 RFC-149 预注册 11-13 到期未转正; OKR/DEB-20261105/20261111 裁「只观察/不放大/不转正」",
    "DEC": "早时代通用 playbook 决策流 (与 DA 同类别非正交); 仅 4 个早时代交易日, 日聚类不可靠",
    "intraday_reentry": "DA 家族日内再入场变体 (73% 与 DA 符号-日重叠, 非正交); 毛 edge 负",
    "TFB": "小样本突破家族 (C16 入场形态邻接); 毛 edge 负, n=10 不结论",
    "MOM": "信号样本 n=0 (仅收养/补录记账行)",
    "NULL": "收养/补录记账行 (sync_recovery_backfill), 非真实信号源",
}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _pfx(r):
    d = str(r.get("decision_id") or "")
    return d.split("-")[0] if d else "NULL"


def _gross(r):
    return _f(r.get("realized_pnl")) or 0.0


def _net(r):
    pn = _f(r.get("pnl_net"))
    if pn is not None:
        return pn
    return _gross(r) - (_f(r.get("fees_est")) or 0.0)


def _notional(r):
    return (_f(r.get("entry_price")) or 0.0) * (_f(r.get("quantity")) or 0)


def _meta(r, key):
    try:
        return json.loads(r.get("metadata_json") or "{}").get(key)
    except Exception:
        return None


def _et(ts):
    s = str(ts or "")
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return dt.astimezone(ET)


def _cluster(rows, valfn):
    """日聚类: 逐笔 bps 的日等权均值 → t / MDE80 / 天数。"""
    bd = defaultdict(list)
    for r in rows:
        d = _notional(r)
        bd[r["trade_date"]].append(valfn(r) / d * 1e4 if d else 0.0)
    daily = [sum(v) / len(v) for v in bd.values()]
    nd = len(daily)
    if nd < 2 or st.stdev(daily) == 0:
        return None, None, nd, daily
    mean = sum(daily) / nd
    se = st.stdev(daily) / nd ** 0.5
    return mean / se, MDE_Z * se, nd, daily


def _panel(rs, label):
    n = len(rs)
    if n == 0:
        print(f"{label:26s} n=0")
        return
    notl = sum(_notional(r) for r in rs)
    g = sum(_gross(r) for r in rs)
    net = sum(_net(r) for r in rs)
    tg, mg, dg, _ = _cluster(rs, _gross)
    tn, mn, dn, _ = _cluster(rs, _net)
    wins = [_net(r) for r in rs if _net(r) > 0]
    loss = [_net(r) for r in rs if _net(r) <= 0]
    wr = 100 * len(wins) / n
    aw = st.mean(wins) if wins else 0.0
    al = st.mean(loss) if loss else 0.0
    payoff = abs(aw / al) if al else 0.0
    gb = 10000 * g / notl if notl else 0.0
    nb = 10000 * net / notl if notl else 0.0
    fmt = lambda x: f"{x:5.2f}" if x is not None else "  NA"
    print(
        f"{label:26s} n={n:3d} d={dg:2d} gross_bps={gb:7.2f} net_bps={nb:7.2f} "
        f"gross_t={fmt(tg)} net_t={fmt(tn)} MDE80={fmt(mg)} WR={wr:4.1f}% "
        f"payoff={payoff:4.2f} gross$={g:7.0f}"
    )


def _loo_min_t(rs, valfn):
    bd = defaultdict(list)
    for r in rs:
        d = _notional(r)
        bd[r["trade_date"]].append(valfn(r) / d * 1e4 if d else 0.0)
    loo = []
    for day in bd:
        sub = {d: v for d, v in bd.items() if d != day}
        daily = [sum(v) / len(v) for v in sub.values()]
        if len(daily) >= 2 and st.stdev(daily) > 0:
            loo.append((sum(daily) / len(daily)) / (st.stdev(daily) / len(daily) ** 0.5))
    return min(loo) if loo else None


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    q = (
        "SELECT * FROM trade_records WHERE source_type='PLAYBOOK_SIM' "
        "AND exit_price>0 AND COALESCE(pnl_net,realized_pnl) IS NOT NULL "
        f"{ERA} {A9}"
    )
    rows = [dict(r) for r in conn.execute(q)]
    conn.close()
    signal, artifact = split_signal_artifact(rows)

    print("== §16 正交入场源筛查 | clean-era PLAYBOOK_SIM 已平仓 ==")
    print(f"raw n={len(rows)} | signal n={len(signal)} | artifact(收养/补录)单列 n={len(artifact)}")
    dates = sorted(r["trade_date"] for r in signal if r.get("trade_date"))
    print(f"signal 窗: {dates[0]} → {dates[-1]} | 独立交易日 {len(set(dates))}")
    print("口径: 毛 edge 优先 (费前 gross bps), 日聚类 t/MDE80, A9+A10, artifact 经 SSOT 分层。")
    print("纪律: 落入 C8–C18 封闭家族 ⇒ 即便统计强显著也不是自由开放新源 (§15 撞车门)。\n")

    byp = defaultdict(list)
    for r in signal:
        byp[_pfx(r)].append(r)

    print("-- 逐入场源 毛 edge 优先面板 (signal-only) + 撞车分类 --")
    order = ["DA", "universe_expansion:2026", "EXT", "DEC", "intraday_reentry", "TFB", "MOM", "NULL"]
    for k in order:
        _panel(byp.get(k, []), k)
        print(f"    撞车: {COLLISION.get(k, '?')}")
    _panel(signal, "ALL-SIGNAL")

    # DA 口径对账 (§3/scorer 用 raw n680; 本筛查用 signal only, artifact 卫生)。
    da_raw = [r for r in rows if _pfx(r) == "DA"]
    da_sig = byp.get("DA", [])
    print("\n-- DA 口径对账 (毛 edge≈0 结论两口径一致) --")
    _panel(da_raw, "DA-raw(§3/scorer口径)")
    _panel(da_sig, "DA-signal(本筛查口径)")
    print(f"    DA artifact 行 n={len(da_raw) - len(da_sig)} (收养/补录, 单列不抹账)")

    # 对 DA 正交性: 符号-日重叠 (重叠高 ⇒ 非正交, 是 DA 家族变体)。
    da_sd = {(r["symbol"], r["trade_date"]) for r in da_sig}
    print("\n-- 对 DA 正交性 (符号-日重叠; 高重叠=DA 家族变体非新源) --")
    for k in ["EXT", "DEC", "intraday_reentry", "TFB", "universe_expansion:2026"]:
        sub = byp.get(k, [])
        if not sub:
            continue
        ov = sum(1 for r in sub if (r["symbol"], r["trade_date"]) in da_sd)
        print(f"    {k:26s} n={len(sub):3d} 与DA符号-日重叠={ov:3d} ({100 * ov / len(sub):.0f}%)")

    # EXT-盘前腿 §15/C13 复活门逐项检验 (唯一统计强显著正 edge 子源)。
    extpm = sorted(
        [r for r in signal if _pfx(r) == "EXT" and (r.get("entry_session") or "") == "premarket"],
        key=lambda r: r["trade_date"],
    )
    print("\n== EXT-盘前腿 §15/C13 复活门逐项检验 (n=%d) ==" % len(extpm))
    if extpm:
        _panel(extpm, "EXT-premarket")
        # 逐月
        bym = defaultdict(list)
        for r in extpm:
            bym[r["trade_date"][:7]].append(r)
        print("  逐月 (双窗同号检验):")
        for m, rs in sorted(bym.items()):
            notl = sum(_notional(r) for r in rs)
            gb = 10000 * sum(_gross(r) for r in rs) / notl if notl else 0
            nb = 10000 * sum(_net(r) for r in rs) / notl if notl else 0
            print(f"    {m}: n={len(rs):2d} gross_bps={gb:7.2f} net_bps={nb:7.2f}")
        # 前后半段
        half = len(extpm) // 2
        for lab, rs in [("H1", extpm[:half]),("H2", extpm[half:])]:
            tg, mg, dg, _ = _cluster(rs, _gross)
            tn, _, _, _ = _cluster(rs, _net)
            notl = sum(_notional(r) for r in rs)
            gb = 10000 * sum(_gross(r) for r in rs) / notl if notl else 0
            print(f"    {lab}: n={len(rs)} d={dg} gross_bps={gb:7.2f} "
                  f"gross_t={tg if tg is None else round(tg, 2)} net_t={tn if tn is None else round(tn, 2)}")
        # 逐笔净均值 (C13 0.30% 地板)
        npp = [_f(r.get("net_pnl_pct")) for r in extpm if _f(r.get("net_pnl_pct")) is not None]
        if npp:
            print(f"  逐笔净均值 net_pnl_pct = {st.mean(npp) * 100:.4f}% (C13 地板 0.30%)")
        # LOO / top3 / 重叠
        loo = _loo_min_t(extpm, _gross)
        totnet = sum(_net(r) for r in extpm)
        top3 = sum(_net(r) for r in sorted(extpm, key=lambda r: -_net(r))[:3])
        ov = sum(1 for r in extpm if (r["symbol"], r["trade_date"]) in da_sd)
        print(f"  LOO 最小 gross_t = {loo if loo is None else round(loo, 2)} (须全>0)")
        print(f"  top3 净额集中度 = {100 * top3 / totnet:.1f}% (须<70%)" if totnet > 0 else "  top3 NA")
        print(f"  与 DA 符号-日重叠 = {ov}/{len(extpm)} (低重叠=对 DA 增量)")
        # 入场窗口 (price-in 核心: C13 是 open→close, EXT 盘前是否在开盘前入场)
        eh = []
        for r in extpm:
            e = _et(r.get("entry_time"))
            if e:
                eh.append(e.hour + e.minute / 60)
        if eh:
            print(f"  入场 ET 时刻: min={min(eh):.2f} med={sorted(eh)[len(eh) // 2]:.2f} "
                  f"max={max(eh):.2f} (开盘=9.5; <9.5=开盘前入场, 别于 C13 open→close)")
        ec = [_f(_meta(r, "ext_change_pct")) for r in extpm]
        ec = [x for x in ec if x is not None]
        print(f"  |ext_change|≥4% 占比 = {sum(1 for x in ec if abs(x) >= 4)}/{len(ec)} "
              f"(非 C13 纯≥4% 人群)")
        hh = [_f(r.get("holding_hours")) for r in extpm if _f(r.get("holding_hours")) is not None]
        if hh:
            print(f"  持有小时: med={sorted(hh)[len(hh) // 2]:.2f} max={max(hh):.2f}")
        # 分臂 (strict vs research_momentum; 研究臂=RFC-149 预注册)
        print("  分臂 (gate_mode):")
        bygm = defaultdict(list)
        for r in extpm:
            bygm[str(_meta(r, "gate_mode"))].append(r)
        for gm, rs in sorted(bygm.items(), key=lambda x: -len(x[1])):
            _panel(rs, f"    {gm}")
        # 多空两腿
        for d_ in ["LONG", "SHORT"]:
            rs = [r for r in extpm if r.get("direction") == d_]
            notl = sum(_notional(r) for r in rs)
            gb = 10000 * sum(_gross(r) for r in rs) / notl if notl else 0
            print(f"    {d_}: n={len(rs)} gross_bps={gb:.2f}")

    print("\n== §16 诚实结论 (筛查判定, 非盈利证明 / 非立项) ==")
    print("1. 7/8 源直接不过: DA 毛≈0(基线); UE 毛−14.47+C18 封; intraday_reentry 毛−16.64+73%DA 重叠;")
    print("   TFB 毛−80.60(n10); DEC 毛+22.26 但 t1.87<2+仅 4 早时代日+同类别非正交; MOM 信号 n0; NULL 收养行。")
    print("2. EXT-盘前是唯一统计强显著正毛+净 edge 子源 (gross+42.28/t4.87, net+32.72/t3.72, 双月双窗同号,")
    print("   逐笔净均 0.338%≥0.30%, LOO4.46, top3 43%, 97% 对 DA 增量, 多空皆正) —— 过 §15/C13 复活门统计判据。")
    print("3. 但它 NOT 自由开放正交新源: (a) 机制=C13 盘前顺势+C10 财报方向封闭家族; (b) 既有 EXT 通道,")
    print("   DEB-20261105/20261111+OKR 已裁「只观察/不放大/不转正」; (c) 研究臂 RFC-149 预注册 11-13 到期未转正;")
    print("   (d) strict 臂符号随窗敏感 (11 月-only n5 负 vs 10–11 月 n14 正) ⇒ 小样本谨慎。")
    print("4. ⇒ 现有数据无「自由开放+正交+毛>0+功效足+可直接立项」的新入场源。印证 §14/§15: 翻现有数据找正子集")
    print("   要么是 C12 选择轴(封), 要么是已受治理既有臂(不能自由放大)。真 C 须前瞻采集真正新源过撞车门+证据门。")
    print("5. EXT-盘前「开盘前入场避开 price-in+财报催化+管理出场」是别于 C13 open→close 的可信机制区分假设,")
    print("   值得正式裁决 (RFC-149 复活门+C13/C10 撞车裁+用户批+前瞻影子+盘前执行保真), 但 agent 不自动启动/不宣称盈利/不重开封闭轴。")
    print("\n局限: SIM_only+fees_est≠live; 盘前流动性薄/借券/滑点未计; 历史非前瞻; 多重检验须前瞻确认; n37 窄人群。")


if __name__ == "__main__":
    main()
