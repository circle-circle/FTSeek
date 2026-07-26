#!/usr/bin/env python3
"""
只读诊断：IC 双脑对账 + PnL 归因缺口体检
================================================
不写任何生产数据，不改任何逻辑。仅读取并打印一张报告。

回答两个问题：
  1) 分析脑(claim IC, skill 侧) 与 执行脑(factor IC, FTSeek 侧) 意见是否一致？
  2) 成交结果能否归因回决策阶段(方向门/退出/sizing/论据)？缺口在哪？

用法: venv/bin/python3.11 scripts/diag_ic_brains_reconcile.py
"""
import json
import os
import sys
import glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = "/root/.hermes/skills/deep-stock-analysis/scripts"
FACTOR_IC_PATH = os.path.join(ROOT, "data", "factor_ic_latest.json")
CLAIM_IC_PATH = os.path.join(SKILL, "claim_ic_cache.json")
CLOSED_TRADES = os.path.join(ROOT, "data", "closed_trades.json")
PLAN_EFF_DIR = os.path.join(ROOT, "data", "plan_effectiveness")

EPS = 1e-6
BAR = "=" * 68
SUB = "-" * 68


def _sign(x):
    if x is None:
        return None
    if x > EPS:
        return "+"
    if x < -EPS:
        return "-"
    return "0"


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        return {"__error__": str(e)}


def _load_adapter_maps():
    """从 skill 侧 adapter 读实时映射表（#4 的 claim_type→condition 桥）。"""
    sys.path.insert(0, SKILL)
    try:
        import adapter  # noqa
        return dict(adapter._CLAIM_TYPE_TO_BULLISH_COND), dict(adapter._CLAIM_TYPE_TO_BEARISH_COND)
    except Exception as e:
        print(f"[WARN] 无法导入 adapter 映射表: {e}")
        return {}, {}


# ─────────────────────────────────────────────────────────────
def section_1_claim_ic_health(claim):
    print(BAR)
    print("[1] 分析脑体检 — claim IC (skill 侧, 喂 prompt + signal_gate)")
    print(BAR)
    if "__error__" in claim:
        print(f"  ✗ 读取失败: {claim['__error__']}")
        return {}
    n = claim.get("total_samples")
    print(f"  strategy_id = {claim.get('strategy_id')}   total_samples = {n}   "
          f"overall_ic_t1 = {claim.get('overall_ic_t1')}")
    if n and n < 100:
        print(f"  ⚠ 总样本 {n} < 100：单类摊薄后统计力极弱，以下判定多在噪声区间")
    claims = claim.get("claims", {})
    print(f"\n  {'claim_type':<22}{'count':>6}{'ic_t1':>9}{'avg_alpha_t1':>14}   flags")
    print(f"  {SUB}")
    conflicts, low_n = [], []
    for ct, v in sorted(claims.items(), key=lambda x: -(x[1].get("count") or 0)):
        cnt = v.get("count") or 0
        ic = v.get("ic_t1")
        al = v.get("avg_alpha_t1")
        flags = []
        # 内部矛盾: IC 符号 与 avg_alpha 符号 打架 → 该类判定不可信
        if _sign(ic) not in (None, "0") and _sign(al) not in (None, "0") and _sign(ic) != _sign(al):
            flags.append("SIGN-CONFLICT(IC↔alpha)")
            conflicts.append(ct)
        if cnt < 10:
            flags.append(f"LOW-N({cnt})")
            low_n.append(ct)
        ic_s = f"{ic:+.4f}" if isinstance(ic, (int, float)) else str(ic)
        al_s = f"{al*100:+.2f}%" if isinstance(al, (int, float)) else str(al)
        print(f"  {ct:<22}{cnt:>6}{ic_s:>9}{al_s:>14}   {' '.join(flags)}")
    print(f"\n  小结: {len(conflicts)} 类 IC↔alpha 符号自相矛盾, {len(low_n)}/{len(claims)} 类样本<10")
    if conflicts:
        print(f"         自相矛盾: {conflicts}  ← 这些类的'有效/无效'判定基本是噪声")
    return claims


# ─────────────────────────────────────────────────────────────
def section_2_factor_ic_health(factor):
    print("\n" + BAR)
    print("[2] 执行脑体检 — factor IC (data/factor_ic_latest.json, FTSeek 方向门真正读的)")
    print(BAR)
    if "__error__" in factor:
        print(f"  ✗ 读取失败: {factor['__error__']}")
        return {}
    print(f"  {'condition':<20}{'ic':>9}{'n':>5}{'trade_wr':>9}{'trade_pnl$':>11}{'tn':>4}   flags")
    print(f"  {SUB}")
    degenerate = []
    for c, v in sorted(factor.items(), key=lambda x: -(x[1].get("trade_n") or 0)):
        ic = v.get("ic")
        n = v.get("n")
        twr = v.get("trade_wr")
        tpnl = v.get("trade_avg_pnl_usd")
        tn = v.get("trade_n") or 0
        tic = v.get("trade_ic")
        flags = []
        if tic is not None and abs(tic) >= 0.999:
            flags.append("DEGEN-trade_ic=±1")
            degenerate.append(c)
        if tn < 10:
            flags.append(f"LOW-TN({tn})")
        ic_s = f"{ic:+.3f}" if isinstance(ic, (int, float)) else str(ic)
        twr_s = f"{twr*100:.0f}%" if isinstance(twr, (int, float)) else "-"
        tpnl_s = f"{tpnl:+.0f}" if isinstance(tpnl, (int, float)) else "-"
        print(f"  {c:<20}{ic_s:>9}{n:>5}{twr_s:>9}{tpnl_s:>11}{tn:>4}   {' '.join(flags)}")
    if degenerate:
        print(f"\n  ⚠ trade_ic=±1 的退化项 (n 太小, 该 IC 无意义): {degenerate}")
    print(f"  注: 此文件是唯一带'真实成交 PnL(trade_avg_pnl_usd)'的按维度归因来源")
    return factor


# ─────────────────────────────────────────────────────────────
def section_3_reconcile(claims, factor, bull_map, bear_map):
    print("\n" + BAR)
    print("[3] 双脑对账 — 分析脑(claim IC) vs 执行脑(factor IC)")
    print(BAR)
    # 2026-07-10: factor_ic_backfill 归一化后, 执行脑 factor IC 已直接包含 claim_type 词汇,
    #   可直接对账(无需 adapter 映射桥). 比较 claim 侧 avg_alpha_t1 符号 vs factor 侧 ic 符号.
    shared = [ct for ct in claims if ct in factor]
    print(f"  两脑共享 claim_type 维度: {len(shared)} 个 (归一化后可直接比较)\n")
    print(f"  {'claim_type':<22}{'A:alpha':>10}{'A:n':>5}{'B:ic':>9}{'B:n':>5}   判定")
    print(f"  {SUB}")
    agree = conflict = na = 0
    conflict_list = []
    for ct in sorted(shared):
        a, b = claims[ct], factor[ct]
        a_alpha, a_n = a.get("avg_alpha_t1"), a.get("count")
        b_ic, b_n = b.get("ic"), b.get("n")
        asg, bsg = _sign(a_alpha), _sign(b_ic)
        a_s = f"{a_alpha*100:+.2f}%" if isinstance(a_alpha, (int, float)) else "-"
        b_s = f"{b_ic:+.3f}" if isinstance(b_ic, (int, float)) else "-"
        if asg in (None, "0") or bsg in (None, "0"):
            verd = "N/A(某边≈0)"
            na += 1
        elif asg == bsg:
            verd = "AGREE ✓"
            agree += 1
        else:
            verd = "CONFLICT ✗ 两脑反向"
            conflict += 1
            conflict_list.append(ct)
        print(f"  {ct:<22}{a_s:>10}{a_n:>5}{b_s:>9}{b_n:>5}   {verd}")
    print(f"\n  小结: AGREE={agree}  CONFLICT={conflict}  N/A={na}  (共享={len(shared)})")
    if conflict_list:
        print(f"  ⚠ CONFLICT: {conflict_list} — 两脑对同一论据给出相反 alpha 方向, 需人工复核样本")
    # 说明: claim 侧口径=avg_alpha_t1(剥beta超额), factor 侧=Spearman(signed_strength, fwd_ret),
    #   两者定义不同, 符号一致性是弱一致性检验, 非严格等价.


# ─────────────────────────────────────────────────────────────
def section_4_pnl_gap():
    print("\n" + BAR)
    print("[4] PnL 归因账本 — 成交能否追回决策？(读真账本, 非陈旧 closed_trades.json)")
    print(BAR)
    import sqlite3
    db = os.path.join(ROOT, "data", "ftseek_us.db")
    try:
        c = sqlite3.connect(db)
        for tbl, dfields in (
            ("trade_records", ("entry_reason", "exit_reason", "decision_reason",
                               "strategy_id", "signal_confidence", "realized_pnl")),
            ("trade_attribution", ("entry_thesis", "exit_reason", "signal_confidence",
                                   "owner_strategy", "realized_pnl")),
        ):
            try:
                cols = [r[1] for r in c.execute(f"PRAGMA table_info({tbl})").fetchall()]
                n = c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                cov = {}
                for f in [x for x in dfields if x in cols]:
                    try:
                        nn = c.execute(
                            f"SELECT COUNT(*) FROM {tbl} WHERE {f} IS NOT NULL AND {f}!=''"
                        ).fetchone()[0]
                        cov[f] = f"{nn}/{n}"
                    except Exception:
                        pass
                print(f"  {tbl}: {n} 行")
                print(f"    决策字段非空覆盖 = {cov}")
            except Exception as e:
                print(f"  {tbl}: {e}")
        c.close()
    except Exception as e:
        print(f"  DB 读取失败: {e}")
    # plan_effectiveness
    fs = sorted(glob.glob(os.path.join(PLAN_EFF_DIR, "*.json")))
    print(f"\n  plan_effectiveness/: {len(fs)} 个日文件 (计划 vs 成交 日聚合)")
    if fs:
        d = _load_json(fs[-1])
        print(f"    最新 {os.path.basename(fs[-1])}: hit_rate={d.get('hit_rate')} "
              f"avg_pnl_of_planned={d.get('avg_pnl_of_planned')} total_trades={d.get('total_trades')}")
    print("\n  结论:")
    print("    · 真账本 trade_records / trade_attribution 已在填 → 可按 exit_reason/strategy 归因")
    print("    · 仍缺: skill 的 claim_type 未作独立列进 trade_records(埋在 metadata_json.conditions)")
    print("      → 想按 claim_type 做成交后归因, 需从 metadata_json 解析或加列")


def main():
    print("\n" + BAR)
    print(" IC 双脑对账 + PnL 归因缺口体检 (只读, 不改任何生产逻辑)")
    print(BAR)
    claim = _load_json(CLAIM_IC_PATH)
    factor = _load_json(FACTOR_IC_PATH)
    bull_map, bear_map = _load_adapter_maps()

    claims = section_1_claim_ic_health(claim)
    factor_d = section_2_factor_ic_health(factor)
    if claims and factor_d:
        section_3_reconcile(claims, factor_d, bull_map, bear_map)
    section_4_pnl_gap()
    print("\n" + BAR)
    print(" 体检完成。以上为现状快照, 未做任何写入。")
    print(BAR + "\n")


if __name__ == "__main__":
    main()
