#!/opt/ftseek/FTSeek-US/venv/bin/python3
"""
Exit Shadow Graduation Report — 影子变体毕业判定器 (只读)

用途 (2026-07-20 建立):
  自动化评估各退出参数影子变体是否满足毕业条件, 替代人工翻看 JSONL。
  每周运行一次 (建议周一盘前), 输出各变体的毕业状态: PASS / WAIT / FAIL。

毕业门禁 (三项全过 = PASS):
  1. 样本量: N ≥ 30 (统计显著性下限)
  2. 均值改善: 变体 capture > baseline capture (或 counterfactual_saved > 0)
  3. 一致性: 变体优于 baseline 的比例 ≥ 55% (非偶然)

变体分类:
  - EXIT_VARIANTS (be/dd 参数): 比 capture_ratio (已实现/最大浮盈)
  - DEAD_SIGNAL_VARIANTS: 比 triggered 组 vs 未 triggered 组的 realized_pct
  - ABORT_30M_VARIANTS: 比 counterfactual_saved_pct (正=abort更优)
  - EARLY_TIGHT_VARIANTS: 比 counterfactual_saved_pct (正=紧SL更优)

用法:
  python scripts/exit_shadow_graduation.py                # 全量数据
  python scripts/exit_shadow_graduation.py --days 14      # 近14天
  python scripts/exit_shadow_graduation.py --json         # JSON 输出
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = "/opt/ftseek/FTSeek-US"
sys.path.insert(0, PROJECT_ROOT)

SHADOW_PATH = Path(PROJECT_ROOT) / "data" / "shadow" / "exit_param_shadow.jsonl"
OUT_LATEST = Path(PROJECT_ROOT) / "data" / "exit_shadow_graduation_latest.json"

# 毕业门禁阈值
MIN_SAMPLES = 30
MIN_CONSISTENCY = 0.55  # 变体优于baseline的比例下限


def _load_records(days: int = 0) -> List[Dict[str, Any]]:
    """加载 shadow JSONL, 可选按天数过滤."""
    if not SHADOW_PATH.exists():
        return []
    cutoff = None
    if days > 0:
        # 2026-07-27: utcnow() 残留 → SSOT now_utc (写侧 exit_shadow.ts 同为 UTC, 口径自洽)
        from app.utils.timezone import now_utc
        cutoff = (now_utc() - timedelta(days=days)).isoformat()
    records = []
    for line in SHADOW_PATH.read_text(encoding="utf-8").strip().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        # 跳过测试数据
        if r.get("symbol") == "TEST":
            continue
        if cutoff and str(r.get("ts", "")) < cutoff:
            continue
        records.append(r)
    return records


def _assess_exit_variants(records: List[Dict]) -> Dict[str, Any]:
    """评估 EXIT_VARIANTS (be/dd 参数变体) 的毕业状态."""
    results = {}
    # 收集 baseline 和各变体的 capture
    baseline_caps = []
    variant_caps: Dict[str, List[float]] = {}
    variant_wins: Dict[str, int] = {}  # 变体 capture > baseline 的次数
    variant_total: Dict[str, int] = {}

    for r in records:
        variants = r.get("variants", {})
        # P1-1 (2026-07-20): 对照组切换为现行 default (be0.5/dd0.8);
        #   旧记录无新键时回退 LEGACY 键 (7/19 前 default), 保证历史样本可用.
        bl = variants.get("baseline_be0.5_dd0.8") or variants.get("baseline_be1.0_dd0.5", {})
        bl_cap = bl.get("capture")
        if bl_cap is None:
            continue
        baseline_caps.append(bl_cap)
        for name, v in variants.items():
            if name in ("baseline_be0.5_dd0.8", "baseline_be1.0_dd0.5"):
                continue
            v_cap = v.get("capture")
            if v_cap is None:
                continue
            variant_caps.setdefault(name, []).append(v_cap)
            variant_total[name] = variant_total.get(name, 0) + 1
            if v_cap > bl_cap:
                variant_wins[name] = variant_wins.get(name, 0) + 1

    n_baseline = len(baseline_caps)
    mean_baseline = sum(baseline_caps) / n_baseline if n_baseline else 0

    for name, caps in variant_caps.items():
        n = len(caps)
        mean_cap = sum(caps) / n if n else 0
        wins = variant_wins.get(name, 0)
        total = variant_total.get(name, 0)
        consistency = wins / total if total > 0 else 0
        improvement = mean_cap - mean_baseline

        # 毕业判定
        if n < MIN_SAMPLES:
            status = "WAIT"
            reason = f"样本不足 ({n}/{MIN_SAMPLES})"
        elif improvement <= 0:
            status = "FAIL"
            reason = f"均值无改善 (variant={mean_cap:.3f} ≤ baseline={mean_baseline:.3f})"
        elif consistency < MIN_CONSISTENCY:
            status = "FAIL"
            reason = f"一致性不足 ({consistency:.1%} < {MIN_CONSISTENCY:.0%})"
        else:
            status = "PASS"
            reason = f"均值+{improvement:.3f}, 一致性{consistency:.1%}"

        results[name] = {
            "status": status,
            "reason": reason,
            "n": n,
            "mean_capture": round(mean_cap, 4),
            "baseline_capture": round(mean_baseline, 4),
            "improvement": round(improvement, 4),
            "consistency": round(consistency, 3),
        }

    return {"baseline_n": n_baseline, "baseline_mean_capture": round(mean_baseline, 4),
            "variants": results}


def _assess_counterfactual_variants(records: List[Dict], field: str,
                                     variant_key: str) -> Dict[str, Any]:
    """评估 counterfactual 类变体 (abort_30m / early_tight)."""
    results = {}
    saved_by_variant: Dict[str, List[float]] = {}

    for r in records:
        section = r.get(field, {})
        for name, v in section.items():
            if not isinstance(v, dict):
                continue
            saved = v.get("counterfactual_saved_pct")
            if saved is None:
                continue
            saved_by_variant.setdefault(name, []).append(float(saved))

    for name, saveds in saved_by_variant.items():
        n = len(saveds)
        mean_saved = sum(saveds) / n if n else 0
        positive_count = sum(1 for s in saveds if s > 0)
        consistency = positive_count / n if n > 0 else 0

        if n < MIN_SAMPLES:
            status = "WAIT"
            reason = f"样本不足 ({n}/{MIN_SAMPLES})"
        elif mean_saved <= 0:
            status = "FAIL"
            reason = f"反事实均值≤0 ({mean_saved:.4f}%)"
        elif consistency < MIN_CONSISTENCY:
            status = "FAIL"
            reason = f"一致性不足 ({consistency:.1%} < {MIN_CONSISTENCY:.0%})"
        else:
            status = "PASS"
            reason = f"mean_saved=+{mean_saved:.4f}%, 一致性{consistency:.1%}"

        results[name] = {
            "status": status,
            "reason": reason,
            "n": n,
            "mean_saved_pct": round(mean_saved, 4),
            "positive_rate": round(consistency, 3),
        }

    return {"variant_key": variant_key, "variants": results}


def _assess_dead_signal(records: List[Dict]) -> Dict[str, Any]:
    """评估 dead_signal 变体: triggered 组 vs 未 triggered 组的 realized_pct."""
    results = {}
    triggered_pnl: Dict[str, List[float]] = {}
    not_triggered_pnl: Dict[str, List[float]] = {}

    for r in records:
        ds = r.get("dead_signal", {})
        live_pct = r.get("live", {}).get("realized_pct")
        if live_pct is None:
            continue
        for name, v in ds.items():
            if not isinstance(v, dict):
                continue
            if v.get("triggered") is True:
                triggered_pnl.setdefault(name, []).append(float(live_pct))
            elif v.get("triggered") is False:
                not_triggered_pnl.setdefault(name, []).append(float(live_pct))

    for name in set(list(triggered_pnl.keys()) + list(not_triggered_pnl.keys())):
        t_list = triggered_pnl.get(name, [])
        nt_list = not_triggered_pnl.get(name, [])
        n_triggered = len(t_list)
        n_total = n_triggered + len(nt_list)
        mean_t = sum(t_list) / n_triggered if n_triggered else 0
        mean_nt = sum(nt_list) / len(nt_list) if nt_list else 0
        # 毕业逻辑: triggered 组应该表现更差 (证明信号确实已死)
        # 即 mean_t < mean_nt → 变体有预测力
        separation = mean_nt - mean_t  # 正 = triggered组更差 = 变体有效

        if n_total < MIN_SAMPLES:
            status = "WAIT"
            reason = f"样本不足 ({n_total}/{MIN_SAMPLES})"
        elif separation <= 0:
            status = "FAIL"
            reason = f"无分离度 (triggered={mean_t:.3f}% ≥ not_triggered={mean_nt:.3f}%)"
        else:
            status = "PASS"
            reason = f"分离度={separation:.3f}pp (triggered={mean_t:.3f}% vs not={mean_nt:.3f}%)"

        results[name] = {
            "status": status,
            "reason": reason,
            "n_triggered": n_triggered,
            "n_total": n_total,
            "mean_triggered_pct": round(mean_t, 4),
            "mean_not_triggered_pct": round(mean_nt, 4),
            "separation_pp": round(separation, 4),
        }

    return {"variants": results}


def _era_block() -> Dict[str, Any]:
    """RFC-138: 当前指纹 vs 冻结裁决日指纹 (read_at 从 ADJUDICATED SSOT 读).

    返回 {"current", "adjudicated_at", "then", "applies", "note"}; 异常 fail-open
    ⇒ {"error": ...} (报告照常, 只标注不改三门判定)。
    """
    try:
        from app.playbook.shadow_registry import ADJUDICATED
        read_at = str((ADJUDICATED.get("exit_param_shadow") or {}).get("read_at") or "")
        from app.review.verdict_era import era_status
        st = era_status(read_at)
        return {
            "current": st["now"],
            "adjudicated_at": read_at,
            "then": st["then"],
            "applies": st["applies"],
            "note": st["note"],
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"era 查询失败 ({type(e).__name__}: {e}) — 不作适用性判定"}


def main():
    parser = argparse.ArgumentParser(description="Exit Shadow Graduation Report")
    parser.add_argument("--days", type=int, default=0, help="只看近N天 (0=全量)")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    records = _load_records(args.days)
    if not records:
        print("⚠️  无有效 shadow 数据 (exit_param_shadow.jsonl 为空或全为 TEST)")
        sys.exit(1)

    from app.utils.timezone import now_utc as _now_utc
    report = {
        "generated_at": _now_utc().isoformat(),
        "total_records": len(records),
        "window_days": args.days or "all",
        "graduation_criteria": {
            "min_samples": MIN_SAMPLES,
            "min_consistency": MIN_CONSISTENCY,
        },
        # RFC-138 借鉴①: era 块 = 当前退出参数指纹 vs 9/26 冻结裁决日指纹。
        # 裁决日从 shadow_registry ADJUDICATED 读 (SSOT, 不硬编码); fail-open:
        # 查询失败 ⇒ era={"error": ...}, 报告照常 (只标注不改判定)。
        "era": _era_block(),
        "exit_variants": _assess_exit_variants(records),
        "dead_signal": _assess_dead_signal(records),
        "abort_30m": _assess_counterfactual_variants(records, "abort_30m", "ABORT_30M_VARIANTS"),
        "early_tight": _assess_counterfactual_variants(records, "early_tight", "EARLY_TIGHT_VARIANTS"),
    }

    # 落盘
    OUT_LATEST.parent.mkdir(parents=True, exist_ok=True)
    OUT_LATEST.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    # ── 人类可读输出 ──
    print(f"{'='*60}")
    print(f"  EXIT SHADOW GRADUATION REPORT")
    print(f"  样本: {len(records)} 笔 | 窗口: {'全量' if not args.days else f'近{args.days}天'}")
    print(f"  门禁: N≥{MIN_SAMPLES}, 一致性≥{MIN_CONSISTENCY:.0%}")
    # RFC-138: era 行 — 漂移时显式 ⚠️ (三门判定不受影响, 只标注适用性)
    _era = report.get("era") or {}
    if _era.get("error"):
        print(f"  era: ⚠️ {_era['error']}")
    elif _era.get("applies") is False:
        print(f"  era: ⚠️ {_era.get('note')}")
    elif _era.get("applies") is True:
        print(f"  era: ✅ {_era.get('note')}")
    else:
        print(f"  era: ⏳ 不可判定 — {_era.get('note', '')}")
    print(f"{'='*60}")

    # EXIT_VARIANTS
    ev = report["exit_variants"]
    print(f"\n── BE/DD 参数变体 (baseline N={ev['baseline_n']}, capture={ev['baseline_mean_capture']:.3f}) ──")
    for name, v in ev["variants"].items():
        icon = {"PASS": "✅", "WAIT": "⏳", "FAIL": "❌"}[v["status"]]
        print(f"  {icon} {name}: {v['status']} | N={v['n']} cap={v['mean_capture']:.3f} "
              f"Δ={v['improvement']:+.3f} 一致性={v['consistency']:.1%} | {v['reason']}")

    # DEAD_SIGNAL
    ds = report["dead_signal"]
    print(f"\n── DEAD SIGNAL 变体 (60min MFE 信号失效) ──")
    if ds["variants"]:
        for name, v in ds["variants"].items():
            icon = {"PASS": "✅", "WAIT": "⏳", "FAIL": "❌"}[v["status"]]
            print(f"  {icon} {name}: {v['status']} | N={v['n_total']} "
                  f"triggered={v['mean_triggered_pct']:.3f}% not={v['mean_not_triggered_pct']:.3f}% "
                  f"分离={v['separation_pp']:+.3f}pp | {v['reason']}")
    else:
        print("  (无数据)")

    # ABORT_30M
    ab = report["abort_30m"]
    print(f"\n── 30MIN-ABORT 变体 (反事实止损) ──")
    if ab["variants"]:
        for name, v in ab["variants"].items():
            icon = {"PASS": "✅", "WAIT": "⏳", "FAIL": "❌"}[v["status"]]
            print(f"  {icon} {name}: {v['status']} | N={v['n']} "
                  f"mean_saved={v['mean_saved_pct']:+.4f}% positive={v['positive_rate']:.1%} | {v['reason']}")
    else:
        print("  (无数据)")

    # EARLY_TIGHT
    et = report["early_tight"]
    print(f"\n── EARLY_TIGHT_SL 变体 (入场期紧SL) ──")
    if et["variants"]:
        for name, v in et["variants"].items():
            icon = {"PASS": "✅", "WAIT": "⏳", "FAIL": "❌"}[v["status"]]
            print(f"  {icon} {name}: {v['status']} | N={v['n']} "
                  f"mean_saved={v['mean_saved_pct']:+.4f}% positive={v['positive_rate']:.1%} | {v['reason']}")
    else:
        print("  (无数据)")

    print(f"\n{'='*60}")
    print(f"  报告已落盘: {OUT_LATEST}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
