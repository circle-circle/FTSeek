#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UE 宇宙扩张影子结算评估器 (2026-09-26, 用户裁「补判据」).

为什么需要它: `universe_expansion_shadow` 自 7/14 建起就有写者 (capture/score) 与 cron,
但 registry 里 min_n=0 且 graduation_criteria="未预注册", no_adjudication_reason=
"NO_CRITERIA: 补登不得事后造判据" ⇒ 影子攒了 96 行 / 321 条评分却【永远没有裁决日】。
本器补上判据载体。

判据来源 = **逐字借用** `scripts/ue_midday_shadow_eval.py` (2026-08-14 用户裁 §13.3),
不自创阈值 (阈值购物禁令: 不得用已读到的数据反推门槛):
    ≥10 交易日 ∧ n≥30 ∧ 命中率(方向调整后前瞻 ≥ +0.5%) ≥ 55% ∧ 均值 ≥ +0.5%
    通过 → 提请用户裁决开启 inject; 否则 → 退役 (零残留)。
⚠️ 单位换算: midday 脚本的 fwd 是【小数比率】(HIT_THRESHOLD=0.005), 本表 fwd_pct 是
   【百分数】(写者 HIT_PCT=1.0 即 +1.0%) ⇒ 本器阈值取 0.5 (百分点)。
⚠️ 命中率一律按 fwd_pct >= 0.5 自算, **不用写者的 hit 字段** (写者阈值 1.0% ≠ 借用判据 0.5%)。

━━ 核心口径纪律: 必须分 horizon 批, 否则判据会被长窗回填批假阳性通过 ━━
写者 9/23 注释 (用户裁「按建议执行」) 逐字: "8/26 首启 score 时一次性回填了 26 个
capture 日 (horizon 1..43 天), 与日常批 (1-3 天) 混在同一文件里 → 下游按 scored_on
聚合会把长窗走势当 1 日读数 (实测把 LONG/SHORT 的方向结论整个翻转)"。
本轮实测复现该翻转:
    混批  LONG +2.089% vs SHORT −2.108% ⇒ LONG 占优
    h=1   LONG −2.043% vs SHORT +0.312% ⇒ SHORT 占优  (符号相反)
且【h>=5 长窗批 LONG 会全过借用判据】(命中率 58.1% / 均值 +3.453%), 而 h=1 LONG
仅 12.1% / −2.043% ⇒ 判据若不分批, 上线即被污染批假阳性通过。故本器:
    · 裁决口径 = h==1 日常批 (唯一与"1 日前瞻"判据同语义)
    · h 2-3 / h>=5 只作披露, **不参与裁决** (防多重比较挪门柱)
    · horizon 优先取写者 summary.horizon_days 标记 (9/23 后才有, 现覆盖 25/321),
      缺失时用 scored_on − capture_date 自算 (与写者标记 25/25 一致, 已验证)

挂载: crontab 周六 (与 ue_tier_gate_shadow_eval 05:52 同窗, 避开交易日)。
产物: data/shadow/universe_expansion_eval_latest.json (atomic write)。
用法: venv/bin/python scripts/universe_expansion_shadow_eval.py [--min-days 10] [--min-n 30]

🔴 **裁决适用范围 (2026-09-26 同日自纠, 撤回原 overclaim)** — 读 verdict 前必看:
本器测的人群 = 写者「板块轮入 → 拉成分股 → **宇宙外浮现名**」(61 只, 板块成分蓝筹为主:
ABBV/ABT/BA/COST/CVX/DIS/DOW…), 而**实盘 UE 通道成交的是另一批票** (ADBE/ARM/ASML/AVGO/
CRWD/DDOG/BIDU…)。symbol 归一化 (去 `US.` 前缀) 后实测重叠仅 **9 只 = 占 UE 14% / 占影子 15%**,
按笔数 **20/86 = 23.3%**; 且重叠子集 LONG −$180.88 (n=13) vs **非重叠** LONG −$660.37 (n=38)
⇒ 实盘 UE LONG 的出血主要在影子**没浮现过**的标的上。
⇒ 本器 verdict=FAIL 只证明「**宇宙外扩张浮现能力**」无预测力 (前瞻命中率 38.8% < 55%,
   LONG 腿 12.1% / −2.043% / p=1.0), **不能**外推成「UE 通道该停」或「UE LONG 该禁」。
   后者须用 `decision_id LIKE 'universe_expansion%'` 归属的 86 笔实盘盈亏单独走判据
   (已在 ue_tier_gate 台账留「方向分层只并列呈报不另设门」的备证, 防多重比较挪门柱)。
⚠️ 归属口径: 查 UE 通道**必须**用 `decision_id LIKE 'universe_expansion%'`;
   用 `strategy_id LIKE 'universe_expansion%'` 得 **0 笔** (strategy_id 实为 playbook_sim /
   extended_hours_entry 落库标签, memory 已有此纪律: 归属先看 decision_id 前缀)。
⚠️ symbol join 必须归一化去 `US.` 前缀 — 未归一化时重叠误得 0%, 会得出「两人群完全无关」的错结论。

零行为变更: 只读 jsonl (不连 DB), 不改写者、不改 config、不动执行链。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from math import comb
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHADOW = ROOT / "data" / "shadow" / "universe_expansion_shadow.jsonl"
OUT = ROOT / "data" / "shadow" / "universe_expansion_eval_latest.json"

# ── 借用判据 (ue_midday_shadow_eval.py, 2026-08-14 用户裁; 单位已换算为百分点) ──
MIN_DAYS = 10          # ≥10 交易日
MIN_N = 30             # n≥30
HIT_PP = 0.5           # 命中: 方向调整后前瞻 ≥ +0.5% (midday HIT_THRESHOLD=0.005 比率)
PASS_HIT_RATE = 0.55   # 命中率 ≥ 55%
PASS_MEAN_PP = 0.5     # 均值 ≥ +0.5% (midday PASS_MEAN=0.005 比率)
ADJ_DAYS = 1           # 裁决口径 horizon (日常批 1 日前瞻)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _d(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def _load() -> tuple[list, list]:
    if not SHADOW.exists():
        return [], []
    scores, rows = [], []
    for ln in SHADOW.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        if rec.get("type") != "score":
            continue
        # 过滤 pytest 直调写入的 TEST 行 (skill 纪律: 统计影子 jsonl 前先过滤)
        scores.append(rec)
        summ = rec.get("summary") or {}
        marked = summ.get("horizon_days")
        cd, so = _d(rec.get("capture_date")), _d(rec.get("scored_on"))
        h_self = (so - cd).days if (cd and so) else None
        for r in (rec.get("results") or []):
            sym = str(r.get("symbol") or "")
            if sym.upper().startswith("TEST"):
                continue
            rr = dict(r)
            rr["_h_marked"] = marked
            rr["_h_self"] = h_self
            # 标记优先, 缺失用自算 (两者在 25 条重叠样本上 25/25 一致)
            rr["_h"] = marked if isinstance(marked, int) else h_self
            rr["_cd"] = rec.get("capture_date")
            rows.append(rr)
    return scores, rows


def _bucket(sub: list) -> dict:
    fv = [r["fwd_pct"] for r in sub if isinstance(r.get("fwd_pct"), (int, float))]
    if not fv:
        return {"n": 0, "capture_days": 0, "hit_rate": None, "mean_pct": None,
                "median_pct": None, "p_one_sided": None}
    n = len(fv)
    hits = sum(1 for v in fv if v >= HIT_PP)     # 自算命中率, 不用写者 hit 字段
    hr = hits / n
    mean = sum(fv) / n
    srt = sorted(fv)
    # 单侧二项检验 (H0: 命中率 50%); n 大时用正态近似防溢出
    if n <= 200:
        p = sum(comb(n, i) * 0.5 ** n for i in range(hits, n + 1))
    else:
        z = (hr - 0.5) / (0.5 / n ** 0.5)
        p = 0.5 * (1 - _erf(z / 2 ** 0.5)) if z > 0 else 1.0
    return {"n": n, "hits": hits, "capture_days": len(set(r["_cd"] for r in sub)),
            "hit_rate": round(hr, 4), "mean_pct": round(mean, 4),
            "median_pct": round(srt[len(srt) // 2], 4), "p_one_sided": round(p, 4)}


def _erf(x: float) -> float:
    # Abramowitz-Stegun 7.1.26
    s = 1.0 if x >= 0 else -1.0
    x = abs(x)
    t = 1.0 / (1.0 + 0.3275911 * x)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t
                - 0.284496736) * t + 0.254829592) * t * pow(2.718281828, -x * x)
    return s * y


def _verdict(b: dict, min_days: int, min_n: int) -> dict:
    """三态判定 (逐条判, 不合并判 — skill: 防"总账幻觉")。

    ⚠️ 为什么必须三态而非「全过→PASS / 否则→KEEP_SAMPLING」:
    借用判据原文是二值处置 — "通过 → 开 inject; **否则 → 退役 (零残留)**"。
    若把「样本足但效果不达标」也写成 KEEP_SAMPLING, 该项会【永久停在攒样】,
    掩盖"判据已可裁决为不通过"这个事实 = 用无限攒样规避裁决 (与僵尸尺子同族)。
    故: 样本不足 → KEEP_SAMPLING (真的还需攒); 样本足 → 按效果判 PASS/FAIL。
    """
    sample = {
        "n>=min_n": (b["n"] >= min_n, f"{b['n']}/{min_n}"),
        "capture_days>=min_days": (b["capture_days"] >= min_days, f"{b['capture_days']}/{min_days}"),
    }
    effect = {
        "hit_rate>=55%": (b["hit_rate"] is not None and b["hit_rate"] >= PASS_HIT_RATE,
                          f"{(b['hit_rate'] or 0)*100:.1f}%/55%"),
        "mean>=+0.5%": (b["mean_pct"] is not None and b["mean_pct"] >= PASS_MEAN_PP,
                        f"{b['mean_pct'] if b['mean_pct'] is not None else 0:+.3f}%/+0.5%"),
    }
    sample_ok = all(v for v, _ in sample.values())
    effect_ok = all(v for v, _ in effect.values())
    if not sample_ok:
        verdict = "KEEP_SAMPLING"            # 样本不足 ⇒ 继续攒, 不裁决
    elif effect_ok:
        verdict = "PASS_TO_USER_ADJUDICATION"  # 全过 ⇒ 提请用户裁开 inject
    else:
        verdict = "FAIL_CRITERIA_MET"        # 样本足而效果不达标 ⇒ 判据已可裁, 提请退役裁决
    return {"criteria_sample": {k: {"pass": v, "value": s} for k, (v, s) in sample.items()},
            "criteria_effect": {k: {"pass": v, "value": s} for k, (v, s) in effect.items()},
            "sample_ok": sample_ok, "effect_ok": effect_ok, "verdict": verdict}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-days", type=int, default=MIN_DAYS)
    ap.add_argument("--min-n", type=int, default=MIN_N)
    args = ap.parse_args()

    scores, rows = _load()
    if not rows:
        print(f"[ue_shadow_eval] 无 score 行 — {SHADOW}")
        return 0

    adj = [r for r in rows if r["_h"] == ADJ_DAYS]
    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": str(SHADOW.relative_to(ROOT)),
        "prereg": ("逐字借用 scripts/ue_midday_shadow_eval.py (2026-08-14 用户裁 §13.3): "
                   f"≥{args.min_days}交易日 ∧ n≥{args.min_n} ∧ 命中率(fwd≥+{HIT_PP}%)≥{PASS_HIT_RATE*100:.0f}% "
                   f"∧ 均值≥+{PASS_MEAN_PP}% → 提请用户裁决开 inject; 否则退役零残留。"
                   " 裁决口径 = horizon==1 日常批 (混批会被 8/26 长窗回填批翻转方向结论)。"),
        "caliber_note": ("命中率按 fwd_pct>=0.5 自算 (写者 HIT_PCT=1.0 与借用判据 0.5 不同, "
                         "hit 字段不可直接用); horizon 取 summary.horizon_days, 缺失用 "
                         "scored_on-capture_date 自算。"),
        "n_score_rows": len(scores),
        "n_results": len(rows),
        "horizon_coverage": {
            "marked": sum(1 for r in rows if r["_h_marked"] is not None),
            "self_computed": sum(1 for r in rows if r["_h_marked"] is None and r["_h_self"] is not None),
            "unknown": sum(1 for r in rows if r["_h"] is None),
        },
        # 裁决桶 (h=1)
        "adjudication": {
            "horizon_days": ADJ_DAYS,
            "ALL": _bucket(adj),
            "LONG": _bucket([r for r in adj if r.get("direction") == "LONG"]),
            "SHORT": _bucket([r for r in adj if r.get("direction") == "SHORT"]),
        },
        # 披露桶 (不参与裁决)
        "disclosure_only": {
            "h_2_3": {"ALL": _bucket([r for r in rows if r["_h"] in (2, 3)])},
            "h_ge_5_backfill": {
                "ALL": _bucket([r for r in rows if (r["_h"] or 0) >= 5]),
                "LONG": _bucket([r for r in rows if (r["_h"] or 0) >= 5 and r.get("direction") == "LONG"]),
                "_warning": ("长窗回填批 (8/26 一次性回填 26 个 capture 日, horizon 5..43 天)。"
                             "不可当 1 日前瞻读数; 实测该批 LONG 会全过借用判据 ⇒ 若纳入裁决即假阳性。"),
            },
            "mixed_all_horizons": {
                "ALL": _bucket(rows),
                "_warning": "混批口径 = report 子命令现输出; 实测方向结论被翻转, 仅供对照, 禁用于裁决。",
            },
        },
    }
    out["adjudication"]["verdict_ALL"] = _verdict(out["adjudication"]["ALL"], args.min_days, args.min_n)
    out["verdict"] = out["adjudication"]["verdict_ALL"]["verdict"]

    _atomic_write(OUT, json.dumps(out, ensure_ascii=False, indent=1))

    a = out["adjudication"]
    print(f"[ue_shadow_eval] score行={len(scores)} results={len(rows)} "
          f"裁决桶(h={ADJ_DAYS}) n={a['ALL']['n']}/{a['ALL']['capture_days']}日 → {OUT.name}")
    for k in ("ALL", "LONG", "SHORT"):
        b = a[k]
        if b["n"]:
            print(f"  {k:6s} n={b['n']:4d} 日={b['capture_days']:3d} 命中率={b['hit_rate']*100:5.1f}% "
                  f"均值={b['mean_pct']:+7.3f}% 中位={b['median_pct']:+7.3f}% p={b['p_one_sided']:.4f}")
    v = out["adjudication"]["verdict_ALL"]
    print(f"  判据 样本{'✓' if v['sample_ok'] else '✗'} 效果{'✓' if v['effect_ok'] else '✗'}"
          f" → verdict: {v['verdict']}")
    for grp in ("criteria_sample", "criteria_effect"):
        for k, c in v[grp].items():
            print(f"    {'✓' if c['pass'] else '✗'} {k:26s} {c['value']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
