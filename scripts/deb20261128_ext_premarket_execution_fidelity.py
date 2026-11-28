#!/usr/bin/env python3
"""DEB-20261128-ext-premarket-revival-application §5 · M2 盘前执行保真一审 (只读, record-only).

用途 (用户裁「EXT 盘前正式复活, 做好治理和优化调整」→ 委托 §17.3 步骤4 执行保真审计的第一遍):
  仅用 trade_records 既有 metadata (零新数据拉取) 量化 EXT-盘前腿 n37 的:
    (1) 人群结构精化 (gate_mode strict/research × 财报元数据覆盖 × 多空);
    (2) 入场 arrival(decision_px) vs fill(entry_price) 方向感知滑点;
    (3) 出场 exit_slippage_bps (系统记录口径) + exit_price_source;
    (4) 费拖 fee_breakdown.total vs fees_est (毛/净 bps 对账 §16);
    (5) 流动性参与率 entry_position_usd / ext_turnover_usd (盘前成交额占比);
    (6) SHORT 借券可得性 = metadata 无字段 ⇒ 缺口旗标 (不下结论)。

只读承诺: sqlite mode=ro, 不写库/不下单/不改文件/不 import app.*; 仅 import
  scripts.utils.da_sample (纯 stdlib 零写)。默认仅控制台。与 §16 探针同款口径。

口径: A10 ERA + A9 首跑排除逐字副本; 人群 = PLAYBOOK_SIM 已平仓 ∧ signal (artifact 经
  SSOT split_signal_artifact 单列) ∧ decision_id 前缀 EXT ∧ entry_session='premarket' (§16 SSOT,
  非 entry_time 窗过滤)。gross_bps=10000×Σrealized_pnl/Σ入场名义; net 同口径用 pnl_net。

诚实边界 (必读): 本审计量的是 **SIM 自身成交记账 + 流动性上下文**, **非实盘可执行性证明**。
  入场/出场滑点若系统性偏有利 = SIM 成交模型乐观伪影, 净 edge 是 **上界**, 须前瞻实盘
  arrival-vs-fill 保真测量扣减后才可谈转正; fees_est 不含借券/资金/盘前冲击成本;
  n37 小样本; 参与率低只证明 $5000 票面在盘前成交额里占比极小, 不证明放大后仍可执行。
  本脚本不下「可复活/可转正」结论, 仅供复活裁决引用。

复现: PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. ./venv/bin/python \
        scripts/deb20261128_ext_premarket_execution_fidelity.py
"""
from __future__ import annotations

import json
import sqlite3
import statistics as st
from collections import Counter, defaultdict
from pathlib import Path

from scripts.utils.da_sample import split_signal_artifact

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "ftseek_us.db"

ERA = (
    "AND trade_date >= '2026-07-01' "
    "AND COALESCE(data_quality,'') IN ('clean','real') "
    "AND COALESCE(source_type,'') NOT IN ('POSITION_SYNC','MANUAL')"
)
A9 = "AND COALESCE(json_extract(metadata_json,'$.exclusion_reason'),'')=''"


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _meta(r, key):
    try:
        return json.loads(r.get("metadata_json") or "{}").get(key)
    except Exception:
        return None


def _gross(r):
    return _f(r.get("realized_pnl")) or 0.0


def _net(r):
    pn = _f(r.get("pnl_net"))
    return pn if pn is not None else _gross(r) - (_f(r.get("fees_est")) or 0.0)


def _notional(r):
    return (_f(r.get("entry_price")) or 0.0) * (_f(r.get("quantity")) or 0)


def _bps(rs, valfn):
    notl = sum(_notional(r) for r in rs)
    return 10000 * sum(valfn(r) for r in rs) / notl if notl else 0.0


def _dist(xs):
    if not xs:
        return "n=0"
    return (
        f"n={len(xs)} mean={st.mean(xs):.2f} med={st.median(xs):.2f} "
        f"min={min(xs):.2f} max={max(xs):.2f}"
    )


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
    extpm = sorted(
        [r for r in signal
         if str(r.get("decision_id") or "").startswith("EXT")
         and (r.get("entry_session") or "") == "premarket"],
        key=lambda r: r["trade_date"],
    )

    print("== M2 EXT-盘前执行保真一审 | clean-era signal n=%d (artifact 单列 n=%d) =="
          % (len(extpm), len(artifact)))
    print("口径: entry_session='premarket' (§16 SSOT); SIM-only fees_est; 非实盘可执行性证明。\n")

    # (1) 人群结构精化
    print("-- (1) 人群结构精化 (复活撞车映射的关键) --")
    gm = Counter(str(_meta(r, "gate_mode")) for r in extpm)
    print(f"  gate_mode: {dict(gm)}")
    print(f"  direction: {dict(Counter(r.get('direction') for r in extpm))}")
    earn = [r for r in extpm if _meta(r, "earnings_report_date") is not None]
    print(f"  财报元数据 (earnings_report_date) 覆盖: {len(earn)}/{len(extpm)}")
    bygm_earn = defaultdict(int)
    for r in extpm:
        bygm_earn[(str(_meta(r, "gate_mode")),
                   _meta(r, "earnings_report_date") is not None)] += 1
    for k, v in sorted(bygm_earn.items()):
        print(f"    gate={k[0]:18s} 有财报元数据={k[1]}: n={v}")
    print(f"  earnings_report_phase: {dict(Counter(str(_meta(r,'earnings_report_phase')) for r in extpm))}")
    print("  ⇒ 财报催化人群仅 = strict 臂带财报元数据者; research_momentum 臂无财报门 = 通用盘前动量。")

    # 分臂毛/净 bps 对账 §16
    print("\n-- 分臂毛/净 bps (对账 §16: strict +56.86 / research +33.10) --")
    for g in ["strict", "research_momentum"]:
        rs = [r for r in extpm if str(_meta(r, "gate_mode")) == g]
        print(f"  {g:18s} n={len(rs):2d} gross_bps={_bps(rs,_gross):7.2f} net_bps={_bps(rs,_net):7.2f}")
    print(f"  {'ALL':18s} n={len(extpm):2d} gross_bps={_bps(extpm,_gross):7.2f} net_bps={_bps(extpm,_net):7.2f}")

    # (2) 入场 arrival vs fill
    print("\n-- (2) 入场 arrival(decision_px) vs fill(entry_price) 方向感知滑点 bps (+ = 不利) --")
    esl = []
    for r in extpm:
        dp = _f(_meta(r, "decision_px"))
        ep = _f(r.get("entry_price"))
        if dp and ep and dp > 0:
            s = (ep - dp) / dp * 1e4
            if r.get("direction") == "SHORT":
                s = -s
            esl.append(s)
    print(f"  {_dist(esl)}")
    if esl:
        print(f"  不利占比 (s>0) = {100*sum(1 for x in esl if x>0)/len(esl):.0f}%  "
              f"(0% = 全部成交价≥优于决策价 ⇒ SIM 成交模型乐观旗标)")

    # (3) 出场滑点
    print("\n-- (3) 出场 exit_slippage_bps (系统记录口径, 负=优于参考) + 成交来源 --")
    xsl = [_f(_meta(r, "exit_slippage_bps")) for r in extpm]
    xsl = [x for x in xsl if x is not None]
    print(f"  {_dist(xsl)}")
    print(f"  exit_price_source: {dict(Counter(str(_meta(r,'exit_price_source')) for r in extpm))}")

    # (4) 费拖
    print("\n-- (4) 费拖对账 (fee_breakdown.total vs fees_est; source) --")
    fbt = [_f((_meta(r, "fee_breakdown") or {}).get("total")) for r in extpm]
    fbt = [x for x in fbt if x is not None]
    fest = [_f(r.get("fees_est")) for r in extpm]
    fest = [x for x in fest if x is not None]
    print(f"  fee_breakdown.total $: {_dist(fbt)}")
    print(f"  fees_est $:            {_dist(fest)}")
    fb_bps = [t / _notional(r) * 1e4 for r, t in zip(extpm, fbt) if _notional(r)]
    print(f"  fee_breakdown bps/笔:  {_dist(fb_bps)}")
    src = Counter(str((_meta(r, "fee_breakdown") or {}).get("source")) for r in extpm)
    print(f"  fee source: {dict(src)}  (est=估算, 不含借券/资金/盘前冲击)")
    print(f"  毛-净 bps 差 (=费拖) = {_bps(extpm,_gross)-_bps(extpm,_net):.2f}")

    # (5) 流动性参与率
    print("\n-- (5) 流动性参与率 entry_position_usd / ext_turnover_usd --")
    to = [_f(_meta(r, "ext_turnover_usd")) for r in extpm]
    to = [x for x in to if x is not None]
    print(f"  盘前成交额 ext_turnover_usd $: {_dist(to)}")
    pr = []
    thin = []
    for r in extpm:
        t = _f(_meta(r, "ext_turnover_usd"))
        pu = _f(_meta(r, "entry_position_usd"))
        if t and pu and t > 0:
            pr.append(pu / t * 100)
            thin.append((t, r["symbol"], r["trade_date"]))
    print(f"  参与率 %: {_dist(pr)}")
    print("  最薄 5 名 (盘前成交额$):")
    for t, s, d in sorted(thin)[:5]:
        print(f"    {d} {s:9s} turnover=${t:,.0f}")
    print("  ⇒ 参与率极低 = $5000 票面占盘前成交额极小, 流动性非该票面约束; 放大须另立门。")

    # (6) SHORT 借券缺口
    print("\n-- (6) SHORT 借券可得性 --")
    sh = [r for r in extpm if r.get("direction") == "SHORT"]
    print(f"  SHORT n={len(sh)}; metadata 无借券可得性/费率字段 ⇒ 缺口, 须另测, 本审计不下结论。")

    print("\n== M2 诚实结论 (执行保真一审, 非复活/转正裁决) ==")
    print("1. 人群精化: n37 = strict 14 (带财报元数据 12 + 无财报 2) + research_momentum 23 (无财报门,")
    print("   全 LONG 普通股通用盘前动量) ⇒ 「财报催化」故事仅落在 12 笔 (<30 功效不足);")
    print("   research 臂是 C13 盘前顺势家族 (非 C10 财报), 须分别走复活门。")
    print("2. 入场滑点 0% 不利 (mean 偏有利) + 出场滑点 mean 偏有利 ⇒ SIM 成交模型乐观伪影;")
    print("   净 edge 是上界, 实盘盘前宽点差/逆向选择会扣减, 须前瞻 arrival-vs-fill 保真测量。")
    print("3. 参与率 <0.03% ⇒ 流动性非 $5000 票面约束 (大盘股), 但放大须另立门 (KR6 是 DA 专用)。")
    print("4. 费拖 ~8-9bps/笔 (est), 不含借券/资金/冲击; SHORT 9 笔借券可得性未测 = 缺口。")
    print("5. 本审计不宣称可复活/可转正/盈利; 仅量化执行侧风险与优化靶点, 供复活裁决引用。")
    print("\n局限: SIM-only fees_est≠live; n37 小样本; 历史非前瞻; 滑点为系统记录口径非实盘测量。")


if __name__ == "__main__":
    main()
