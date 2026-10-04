#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RFC-065 entry_band_widen 影子结算器 (2026-10-04, 用户裁「按建议执行」— NO_EVAL_SCRIPT 缺口填补).

背景: `data/entry_band_widen_shadow.jsonl` (pipeline._check_intraday_trigger 内
_emit_widen_shadow 采集, 8/19 起) 记录 "widen 带能触发而 flat 带未触发" 的双事件
(new = flat 全天未触发 / changed = flat 后来触发但更晚更差)。注册表条目
`entry_band_widen` 判据已预注册 (RFC-065 §29.3 + shadow_registry graduation_criteria)
但 2026-09-27 [RFC-115] 摘除错轴 eval_script 后一直 NO_EVAL_SCRIPT 挂账 —
本脚本是缺失的判分侧。纯读 (DB mode=ro), 零交易行为变更, 零影子文件写入。

配对口径 (预注册「单侧安全门: widen 组不显著劣于 flat」的落地):
  flat 臂 = 真实世界结果:
    · changed ∧ executed → trade_records.pnl_net (按 decision_id join, 生产净额权威)
    · changed ∧ 未成交   → 0 (flat 触发但流单 = 漏单, 无净额)
    · new                → 0 (flat 全天未触发)
  widen 臂 = 反事实: 以影子行 (ts, current_price) 为入场, 1分K 轮询级重放
    生产退出栈 (参数**运行时读生产 config 单一真源**, 禁硬编码 — skill T10-7):
      初始 SL: dist = max(β × |anchor−instSL|/anchor, sl_tighten_min_pct)
               (百分比距离以回放自身入场价为基准应用 — skill T10-6)
      保本:    mfe ≥ breakeven_at_pct 激活, 线 = 成本 ± (fee_cushion+slip_cushion)
      锁利:    SIM 臂走 adaptive_trailing (MFE 自适应回撤, clamp 0.4~1.2,
               与生产 adaptive_trailing.py 同函数), 非 SIM 回落 drawdown_from_peak_pct
      死信号:  持仓 ≥ dead_signal_exit_minutes 且峰值浮盈 < dead_signal_exit_mfe_pct
      TP:      指令 take_profit 触及
      EOD:     以上皆未触发 → 收盘最后一根 bar 收盘平
    轮询语义: 每 bar 取 (o+c)/2 与 c 两采样点 (与 settle_shadow_portfolio 同款,
    不用 bar 内瞬时极值 — 那会系统性乐观)。
  δ = widen_net − flat_net (逐事件配对; 往返费只扣 widen 臂 — flat 臂为 0 时
    其真实世界未发生交易即无费用; flat 臂非 0 时 pnl_net 已是净口径)

预注册判据 (见数前写死, 逐字抄自 RFC-065 §13/§29.3 + shadow_registry):
  样本门:  widen 专属事件已结算 n ≥ 30 (RFC-065 原文; 注册表 min_n=60 是 READY
           迁移锚 — 两轴并存如实并报, 不互相冒充)
  主门:    配对符号翻转置换 (sign-flip permutation), B=10000, 单侧 α=0.025
           — 安全门 = 「widen 显著劣于 flat」才 FAIL (obs Σδ<0 且 p_worse<0.025)
  双条件:  mean δ > 0 且 bootstrap CI95 下界 > 0 (正向证据, 判分协议模板同款)
  共门:    符号检验 (δ>0 计数, 单侧精确二项 p<0.025) — 防 top5 幅度集中
  分组事先固定: new / changed 两事件组分别报读数, 不事后重组
  分时间窗: 按日期对半切, 两半 mean δ 符号并列 (诊断披露, 不一致如实报)
  裁决输出: GRADUATION_LINE_HIT 只提示**提请翻开关裁决** (另立 DEB + 用户裁 +
           重启), 本脚本不自动生效任何生产开关 — 与 B19 同款措辞纪律。

诚实护栏:
  · mean/median/日聚类 t 三者并列 (T18/T39); 尾部单笔主导诊断 (top1 占比)
  · 缺 bars 行不静默丢: 计数 + 逐行打印清单 (T34)
  · flat 臂 join 不上 trade_records 的 changed+executed 行: 单列计数, 不混入 0 桶
  · 判据读数一律带 n; n<30 只报方向不判决 (宪章第 9 律)

产物: data/reports/entry_band_widen_verdict_latest.json (atomic write)
     + data/reports/entry_band_widen_verdict_history.jsonl (追加)
挂载: eval_on_demand=True (裁点/n 达门触发, 照 short_funnel_shadow_eval 惯例,
     不挂 cron — 影子 36 行未达 min_n=60, 周期跑无增量)
回滚: 删本脚本 + 两份产物 + shadow_registry eval_script 改回 None。

用法: venv/bin/python scripts/settle_entry_band_widen.py [--fetch] [--json-only]
  --fetch: 缺 bars 时连 OpenD 拉取 (复用 settle_shadow_portfolio.fetch_bars)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHADOW = ROOT / "data" / "entry_band_widen_shadow.jsonl"
DB = ROOT / "data" / "ftseek_us.db"
OUT = ROOT / "data" / "reports" / "entry_band_widen_verdict_latest.json"
HIST = ROOT / "data" / "reports" / "entry_band_widen_verdict_history.jsonl"

# ── 预注册判据常量 (见文件头; 改这里 = 改判据 = 须先改 RFC-065/注册表留痕) ──
MIN_N_RFC = 30          # RFC-065 §13: widen 专属事件 n≥30
B = 10_000              # sign-flip 置换次数
ALPHA = 0.025           # 单侧
SEED = 20261004         # 固定 seed 确定性


def _prod_exit_params():
    """运行时读生产 config 单一真源 (禁硬编码, skill T10-7)。"""
    sys.path.insert(0, str(ROOT))
    from app.playbook.config import get_playbook_config
    pb = get_playbook_config()
    rules = pb.get_horizon_top("factor_exit_rules", {}) or {}
    dflt = rules.get("default", {}) or {}
    return {
        "be_pct": float(dflt.get("breakeven_at_pct", 0.4)),
        "dd_pct": float(dflt.get("drawdown_from_peak_pct", 0.8)),
        "cushion_pct": float(pb.get("breakeven_fee_cushion_pct", 0.15))
        + float(pb.get("breakeven_slip_cushion_pct", 0.2)),
        "ds_minutes": int(pb.get("dead_signal_exit_minutes", 60)),
        "ds_mfe": float(pb.get("dead_signal_exit_mfe_pct", 0.2)),
        "sl_beta": float(pb.get("sl_tighten_beta", 1.0)),
        "sl_floor": float(pb.get("sl_tighten_min_pct", 1.5)),
        "adaptive_enabled": bool(pb.get("adaptive_trailing_enabled", False)),
        "_pb": pb,
    }


def _adaptive_dd(mfe_pct, P):
    """SIM 臂 MFE 自适应回撤 — 直接调生产函数 (不手抄公式)。"""
    if not P["adaptive_enabled"]:
        return P["dd_pct"]
    try:
        from app.playbook.adaptive_trailing import adaptive_trailing_drawdown_pct
        v = adaptive_trailing_drawdown_pct(mfe_pct, P["_pb"], "PLAYBOOK_SIM")
        return float(v) if v is not None else P["dd_pct"]
    except Exception as e:  # noqa: BLE001 — 留痕不静默 (棘轮纪律)
        print(f"[warn] adaptive_trailing 读取失败, 回落静态 dd: {e}", file=sys.stderr)
        return P["dd_pct"]


def settle_widen(direction, entry_price, bars, sl_dist_pct, tp_price, entry_hm, P):
    """轮询级退出重放 (语义同 settle_shadow_portfolio.settle, 参数生产同源)。

    返回 (exit_price, reason, mfe_pct) 或 (None, 'NO_BARS', 0.0)。
    """
    is_long = direction == "LONG"
    base_sl = (entry_price * (1 - sl_dist_pct / 100) if is_long
               else entry_price * (1 + sl_dist_pct / 100))
    evo = 0.0
    peak = entry_price
    held = 0
    started = False
    mfe = 0.0
    for ts, o, h, l, c in bars:
        if not started:
            if ts[11:16] >= entry_hm:
                started = True
            else:
                continue
        peak = max(peak, h) if is_long else min(peak, l)
        mfe = ((peak - entry_price) / entry_price * 100 if is_long
               else (entry_price - peak) / entry_price * 100)
        if mfe >= P["be_pct"]:
            # 保本线 (含费用+滑点垫)
            _be = (entry_price * (1 + P["cushion_pct"] / 100) if is_long
                   else entry_price * (1 - P["cushion_pct"] / 100))
            evo = max(evo, _be) if is_long else (min(evo, _be) if evo > 0 else _be)
            # 锁利线: SIM 臂 MFE 自适应回撤
            _dd = _adaptive_dd(mfe, P)
            _tr = peak * (1 - _dd / 100) if is_long else peak * (1 + _dd / 100)
            evo = max(evo, _tr) if is_long else min(evo, _tr)
        eff = (max(base_sl, evo) if is_long
               else (min(base_sl, evo) if evo > 0 else base_sl))
        for px in ((o + c) / 2, c):
            if is_long and px <= eff:
                return px, ("SL_TIGHT" if eff == base_sl else "BE_TRAIL"), mfe
            if not is_long and px >= eff:
                return px, ("SL_TIGHT" if eff == base_sl else "BE_TRAIL"), mfe
            if tp_price and ((is_long and px >= tp_price) or (not is_long and px <= tp_price)):
                return px, "TP", mfe
        held += 1
        if held >= P["ds_minutes"] and mfe < P["ds_mfe"]:
            return c, "DEAD_SIGNAL", mfe
    return (bars[-1][4], "EOD", mfe) if started else (None, "NO_BARS", 0.0)


def _stats(deltas, dates):
    """mean/median/日聚类t/sign-flip p/bootstrap CI/符号检验 — 一次算齐。"""
    import numpy as np
    d = np.asarray(deltas, dtype=float)
    n = len(d)
    out = {"n": n, "sum": round(float(d.sum()), 2),
           "mean": round(float(d.mean()), 3),
           "median": round(float(np.median(d)), 3)}
    # 日聚类 t (重抽单位=交易日, 宪章第 9 律)
    by_day = defaultdict(list)
    for v, dt in zip(deltas, dates):
        by_day[dt].append(v)
    day_means = np.asarray([float(np.mean(v)) for v in by_day.values()])
    out["n_days"] = len(day_means)
    if len(day_means) >= 2 and day_means.std(ddof=1) > 0:
        out["day_t"] = round(float(day_means.mean() / (day_means.std(ddof=1) / math.sqrt(len(day_means)))), 3)
    else:
        out["day_t"] = None
    # sign-flip 置换 (H0: δ 对称于 0)
    rng = np.random.default_rng(SEED)
    signs = rng.choice([-1.0, 1.0], size=(B, n))
    perm = signs @ d
    obs = float(d.sum())
    out["perm_p_worse"] = round(float((perm <= obs).mean()), 5)   # 显著更差侧
    out["perm_p_better"] = round(float((perm >= obs).mean()), 5)
    # bootstrap CI95 (mean)
    idx = rng.integers(0, n, size=(B, n))
    means = d[idx].mean(axis=1)
    out["ci95_lo"] = round(float(np.percentile(means, 2.5)), 3)
    out["ci95_hi"] = round(float(np.percentile(means, 97.5)), 3)
    # 符号检验 (精确二项, 单侧 δ>0)
    k = int((d > 0).sum())
    log_half = -n * math.log(2)
    p_sign = sum(math.comb(n, i) for i in range(k, n + 1)) * math.exp(log_half)
    out["sign_pos"] = k
    out["sign_p"] = round(float(min(p_sign, 1.0)), 5)
    # 尾部主导诊断 (T18)
    if n >= 3 and obs != 0:
        top = float(np.max(np.abs(d)))
        out["top1_abs_share_pct"] = round(top / abs(obs) * 100, 1)
        srt = np.sort(d)
        drop3 = float(srt[3:].sum()) if obs < 0 else float(srt[:-3].sum())
        out["sum_drop_top3"] = round(drop3, 2)
    return out


def _verdict(main, n_settled):
    """预注册判据判决 (常量见文件头)。"""
    if n_settled < MIN_N_RFC:
        return "KEEP_SAMPLING", f"n={n_settled} < RFC-065 样本门 {MIN_N_RFC} — 只报方向不判决"
    worse = main["sum"] < 0 and main["perm_p_worse"] < ALPHA
    if worse:
        return "WIDEN_HARMFUL", (f"安全门 FAIL: Σδ={main['sum']} 显著更差 "
                                 f"(p_worse={main['perm_p_worse']} < {ALPHA}) — 提请退役 widen 影子 (另裁)")
    dual = main["mean"] > 0 and main["ci95_lo"] > 0
    gate = main["sign_p"] < ALPHA and main["sign_pos"] * 2 > main["n"]
    if dual and gate:
        return "GRADUATION_LINE_HIT", (f"双条件 (mean={main['mean']}>0 ∧ CI95下界={main['ci95_lo']}>0) "
                                       f"+ 符号共门 (p={main['sign_p']}) 全过 — **提请翻开关裁决** "
                                       f"(entry_band_vol_widen_enabled=true 属生产变更, 另立 DEB + 用户裁 + 重启; 本脚本不自动生效)")
    return "NOT_ESTABLISHED_KEEP_SAMPLING", ("安全门通过 (不显著劣于 flat) 但正向证据不足 "
                                             f"(双条件/共门未全过: mean={main['mean']}, CI95=[{main['ci95_lo']},{main['ci95_hi']}], "
                                             f"sign_p={main['sign_p']}) — RFC-065: 不满足则影子顺延, 不凭中途印象翻开关")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="缺 bars 时连 OpenD 拉取")
    ap.add_argument("--json-only", action="store_true", help="只写产物不打摘要")
    args = ap.parse_args()

    if not SHADOW.exists():
        print("无影子台账 (data/entry_band_widen_shadow.jsonl 不存在)")
        return 0
    rows, n_bad_json = [], 0
    for line in SHADOW.open(encoding="utf-8"):
        try:
            rows.append(json.loads(line))
        except Exception:  # noqa: BLE001 — 计数留痕不静默
            n_bad_json += 1
    # 幂等去重 (inst_id, date) — 写者已查重, 此处兜底
    seen, uniq = set(), []
    for r in rows:
        k = (r.get("inst_id"), r.get("date"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)

    sys.path.insert(0, str(ROOT))
    from scripts.settle_shadow_portfolio import load_bars, fetch_bars
    from app.playbook.admission import estimate_round_trip_fee

    P = _prod_exit_params()
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    events, n_no_bars, n_no_inst, n_flat_unmatched = [], 0, 0, 0
    no_bars_list = []
    for r in uniq:
        inst = conn.execute(
            "SELECT triggered, executed, decision_id, entry_price, stop_loss, "
            "take_profit, position_size FROM playbook_instructions WHERE id=?",
            (r.get("inst_id"),)).fetchone()
        if inst is None:
            n_no_inst += 1
            continue
        triggered, executed, did, anchor, inst_sl, inst_tp, qty = inst
        bars = load_bars(r["symbol"], r["date"])
        if bars is None and args.fetch:
            bars = fetch_bars(r["symbol"], r["date"])
        if not bars:
            n_no_bars += 1
            no_bars_list.append(f"{r['symbol']} {r['date']}")
            continue

        etype = "changed" if triggered else "new"
        # flat 臂
        flat_net, flat_src = 0.0, "no_trade"
        if triggered and executed and did:
            tr = conn.execute(
                "SELECT pnl_net, realized_pnl FROM trade_records WHERE decision_id=?",
                (did,)).fetchone()
            if tr is not None:
                flat_net = float(tr[0] if tr[0] is not None else (tr[1] or 0.0))
                flat_src = "trade_record"
            else:
                n_flat_unmatched += 1
                flat_src = "executed_but_unmatched"
        # widen 臂
        entry = float(r["current_price"])
        direction = "SHORT" if r["direction"] == "SHORT" else "LONG"
        anchor_f = float(anchor or entry)
        sl_f = float(inst_sl or 0)
        sl_pct = abs(anchor_f - sl_f) / anchor_f * 100 if sl_f > 0 and anchor_f > 0 else 3.0
        sl_dist = max(P["sl_beta"] * sl_pct, P["sl_floor"])
        tp = float(inst_tp or 0) or None
        q = int(qty or 0)
        if q <= 0:
            q = max(1, int(5500 / entry))
        ex, reason, mfe = settle_widen(direction, entry, bars, sl_dist, tp,
                                       r["ts"][11:16], P)
        if ex is None:
            n_no_bars += 1
            no_bars_list.append(f"{r['symbol']} {r['date']} (late_ts)")
            continue
        gross_pct = ((ex - entry) / entry * 100 if direction == "LONG"
                     else (entry - ex) / entry * 100)
        fee = estimate_round_trip_fee(q, entry, direction, r["date"])
        widen_net = gross_pct / 100 * entry * q - fee
        events.append({
            "date": r["date"], "symbol": r["symbol"], "direction": direction,
            "type": etype, "flat_net": round(flat_net, 2), "flat_src": flat_src,
            "widen_net": round(widen_net, 2), "delta": round(widen_net - flat_net, 2),
            "widen_exit": reason, "widen_mfe_pct": round(mfe, 3),
            "widen_entry": entry, "qty": q, "fee": round(fee, 2),
            "atr_pct": r.get("atr_pct"), "gap_pct": r.get("gap_pct"),
            "trigger_hhmm": r.get("trigger_hhmm"),
        })
    conn.close()

    if not events:
        print(f"无可结算事件 (rows={len(uniq)} 缺bars={n_no_bars} 缺指令={n_no_inst})")
        return 0

    deltas = [e["delta"] for e in events]
    dates = [e["date"] for e in events]
    main_stats = _stats(deltas, dates)
    verdict, why = _verdict(main_stats, len(events))

    # 分组事先固定: new / changed
    groups = {}
    for g in ("new", "changed"):
        sub = [e for e in events if e["type"] == g]
        if sub:
            groups[g] = _stats([e["delta"] for e in sub], [e["date"] for e in sub])
    # 分时间窗: 日期排序对半切
    srt = sorted(events, key=lambda e: e["date"])
    half = len(srt) // 2
    halves = {}
    for name, sub in (("first", srt[:half]), ("second", srt[half:])):
        if sub:
            h = _stats([e["delta"] for e in sub], [e["date"] for e in sub])
            h["window"] = f"{sub[0]['date']}→{sub[-1]['date']}"
            halves[name] = h

    reg_min_n = None
    try:
        from app.playbook.shadow_registry import REGISTRY
        item = next((s for s in REGISTRY if s.id == "entry_band_widen"), None)
        if item is not None:
            reg_min_n = item.min_n
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 注册表 min_n 读取失败: {e}", file=sys.stderr)

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "shadow_file": str(SHADOW.relative_to(ROOT)),
        "n_rows": len(uniq), "n_bad_json": n_bad_json,
        "n_settled": len(events), "n_no_bars": n_no_bars, "n_no_inst": n_no_inst,
        "n_flat_unmatched": n_flat_unmatched,
        "no_bars_list": no_bars_list,
        "registry_min_n": reg_min_n,
        "criteria": {
            "min_n_rfc065": MIN_N_RFC, "perm_B": B, "alpha_one_sided": ALPHA,
            "safety_gate": "sign-flip p_worse<α ∧ Σδ<0 ⇒ WIDEN_HARMFUL",
            "dual_condition": "mean δ>0 ∧ bootstrap CI95 lo>0",
            "sign_gate": "精确二项单侧 p<α ∧ 正号过半",
            "note": "判据逐字预注册于 RFC-065 §13/§29.3 + shadow_registry.graduation_criteria; 本脚本见数前写死",
        },
        "exit_params_source": {k: v for k, v in P.items() if k != "_pb"},
        "verdict": verdict, "verdict_why": why,
        "main": main_stats, "groups": groups, "time_halves": halves,
        "events": events,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, OUT)
    with HIST.open("a", encoding="utf-8") as f:
        f.write(json.dumps({k: report[k] for k in
                            ("generated_at", "n_settled", "verdict", "main")},
                           ensure_ascii=False) + "\n")

    if args.json_only:
        print(json.dumps({k: report[k] for k in ("n_settled", "verdict", "verdict_why", "main")},
                         ensure_ascii=False))
        return 0

    print(f"═══ RFC-065 entry_band_widen 影子结算 ({report['generated_at']}) ═══")
    print(f"台账 {len(uniq)} 行 → 已结算 {len(events)} (缺bars {n_no_bars}"
          + (f": {no_bars_list}" if no_bars_list else "") + f", 缺指令 {n_no_inst}, 坏行 {n_bad_json})")
    print(f"样本门: RFC-065 n≥{MIN_N_RFC} {'✅' if len(events) >= MIN_N_RFC else '✗'}"
          f" | 注册表 min_n={reg_min_n} (READY 迁移锚, 两轴并报不冒充)")
    m = main_stats
    print(f"主读数 (全事件): Σδ={m['sum']:+.2f} mean={m['mean']:+.3f} median={m['median']:+.3f} "
          f"日聚类t={m['day_t']} ({m['n_days']}日)")
    print(f"  sign-flip: p_worse={m['perm_p_worse']} p_better={m['perm_p_better']} | "
          f"CI95=[{m['ci95_lo']},{m['ci95_hi']}] | 符号 {m['sign_pos']}/{m['n']} p={m['sign_p']}")
    if "top1_abs_share_pct" in m:
        print(f"  尾部诊断: top1 |δ| 占Σ {m['top1_abs_share_pct']}% | 剔top3 Σ={m['sum_drop_top3']}")
    for g, s in groups.items():
        print(f"  组[{g}] n={s['n']} Σδ={s['sum']:+.2f} mean={s['mean']:+.3f} median={s['median']:+.3f} 日t={s['day_t']}")
    for hname, s in halves.items():
        print(f"  时间窗[{hname}] {s.get('window','')} n={s['n']} mean={s['mean']:+.3f} 日t={s['day_t']}")
    print(f"判决: {verdict} — {why}")
    print(f"产物: {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
