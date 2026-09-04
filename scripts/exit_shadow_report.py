#!/usr/bin/env python3
"""退出参数影子 capture 分布报告 (只读, 2026-07-13)。

读 data/shadow/exit_param_shadow.jsonl (finalize_exit_shadow 落盘), 聚合各变体
反事实 capture_ratio vs 实盘, 关注「更早保本/更紧锁利是否提升 capture」。

capture = 已实现有利% / 最大浮盈%。仅统计 max_fav_pct>0 的单 (摸到过浮盈,
capture 才有意义)。变体未触发 → 视同随实盘退出 (capture 与实盘一致)。

诚实边界: 只测【比现行更紧】的变体 (更紧必在实盘退出前触发, 反事实退出价落在
已观测价格路径内可评估); 更松的骑过实盘退出点、路径不可观测, 故不在评估范围。
样本达门槛 (≥20) 且某变体 capture 中位稳定 > baseline, 才提议改实盘 yaml (需确认)。

运行: PYTHONPATH=. ./venv/bin/python scripts/exit_shadow_report.py [--push] [--fee-pct 0.08]

2026-09-04 (M1, 费用垫判分): 新增 net-of-fee 口径 — capture/实现% 是毛口径, 而
  单笔往返费 ~8bps (9/3 实测: fees 63.56/15笔, 均名义~5.1k; HPE fees_est
  4.87/5931=8.2bps) 与 BE 类单的实现% (中位 0.16%=16bps) 同量级 → 不扣费的
  裁决会把「费用线边缘的单」误判为有效改善。净实现 = realized_pct - fee_pct;
  变体 vs 实盘的净差同时给出 (bps)。默认 0.08 可 --fee-pct 覆盖。
2026-09-04 (M5, baseline 漂移诊断): 对照现行 playbook_horizon.yaml default
  (be/dd) 与影子 baseline 变体定义, 漂移时显式告警 (P1-1 同款风险: 判分基线
  与现行配置脱节 → 毕业判定失真)。
"""
import argparse
import json
import statistics
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SHADOW = REPO / "data" / "shadow" / "exit_param_shadow.jsonl"
HORIZON_YAML = REPO / "config" / "playbook_horizon.yaml"
MIN_N = 20  # capture 中位可信的最小样本量
# 实测往返费率参考 (fee_model: 佣金$0.01/股 min$1.99 + 交收$0.003/股 双边,
# 典型名义 ~$5.1k → ~8bps)。影子记录无股数/名义, 只能统一 pct 假设。
DEFAULT_FEE_PCT = 0.08


def _load():
    if not SHADOW.exists():
        return []
    rows = []
    for line in SHADOW.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def _med(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return statistics.mean(vals) if vals else None


def _fmt(x, pct=False):
    if x is None:
        return "  n/a"
    return f"{x*100:+.1f}%" if pct else f"{x:+.3f}"


def _fmt_pct_unit(x):
    """显示已是 pct 单位的值 (realized_pct 等字段本身即百分数, 勿再 ×100)。"""
    return "  n/a" if x is None else f"{x:+.2f}%"


def _current_default_exit_cfg():
    """读现行 playbook_horizon.yaml factor_exit_rules.default (be/dd)。只读诊断, 读不到返回 None。"""
    try:
        import yaml
        cfg = yaml.safe_load(HORIZON_YAML.read_text(encoding="utf-8")) or {}
        d = ((cfg.get("factor_exit_rules") or {}).get("default") or {})
        return {k: d.get(k) for k in ("breakeven_at_pct", "drawdown_from_peak_pct")}
    except Exception:
        return None


def _baseline_drift_check(usable):
    """M5: 影子 baseline 变体 vs 现行 default 配置漂移诊断 (只报不改, 变体键名冻结不可变)。"""
    lines = ["── baseline 漂移诊断 (M5) ──"]
    cur = _current_default_exit_cfg()
    if cur is None or cur.get("breakeven_at_pct") is None:
        lines.append("  [warn] 读不到现行 default 配置, 跳过漂移检查")
        return lines
    be_now, dd_now = float(cur["breakeven_at_pct"]), float(cur["drawdown_from_peak_pct"])
    # 影子 baseline_be0.5_dd0.8 注释自称"现行 default 对照" (7/19 处方后)
    drift = not (abs(be_now - 0.5) < 1e-9 and abs(dd_now - 0.8) < 1e-9)
    if drift:
        lines.append(
            f"  ⚠️ 漂移: 现行 default be={be_now} dd={dd_now} ≠ 影子 baseline_be0.5_dd0.8。"
            "该 baseline 是历史对照非现行镜像; 判分以 live 臂为准 (所有对比均 vs live)。"
        )
    else:
        lines.append(f"  ✅ baseline_be0.5_dd0.8 与现行 default 一致 (be={be_now} dd={dd_now})")
    # live vs baseline capture 差: 基线若系统性偏离 live, 印证配置漂移已影响影子读数
    pairs = [(r["live"]["capture"], (r.get("variants", {}).get("baseline_be0.5_dd0.8") or {}).get("capture"))
             for r in usable]
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    if pairs:
        d = statistics.median([b - a for a, b in pairs])
        lines.append(f"  live vs baseline_be0.5_dd0.8 capture 中位差 = {d:+.3f} (n={len(pairs)})")
    return lines


def main(push=False, fee_pct=DEFAULT_FEE_PCT):
    print("=" * 72)
    print(f"退出参数影子 — capture 分布报告 (净口径 fee={fee_pct:.2f}%/往返)")
    print("=" * 72)
    rows = _load()
    # 仅保留摸到过浮盈的单 (capture 才有意义)
    usable = [r for r in rows if (r.get("max_fav_pct") or 0) > 0
              and r.get("live", {}).get("capture") is not None]
    print(f"\n累计落盘={len(rows)}  可用(max_fav>0 且有capture)={len(usable)}")
    if not usable:
        print("  ⏳ 尚无可用样本 (影子刚上线/无平仓单)。待新平仓单积累后复跑。\n")
        if push:
            _push(f"📊 退出影子: 累计{len(rows)}条, 尚无可用样本, 继续积累")
        return

    live_caps = [r["live"]["capture"] for r in usable]
    live_med = _med(live_caps)
    live_reals = [r["live"]["realized_pct"] for r in usable]
    mfe_med = _med([r.get("max_fav_pct") for r in usable])
    live_net_med = _med([p - fee_pct for p in live_reals])
    print(f"  实盘 capture 中位={_fmt(live_med)}  净实现中位={_fmt_pct_unit(live_net_med)}"
          f"  (毛实现中位={_fmt_pct_unit(_med(live_reals))}, fee={fee_pct:.2f}%)")
    if mfe_med:
        print(f"  MFE% 中位={mfe_med:+.2f}%")

    # 变体名从数据里取 (与 exit_shadow.EXIT_VARIANTS 对齐)
    names = []
    for r in usable:
        for k in r.get("variants", {}):
            if k not in names:
                names.append(k)

    print("\n── 各变体 capture vs 实盘 ──")
    print(f"  {'变体':<24}{'触发':>5}{'cap中位':>9}{'净实现中位':>10}{'净差中位':>9}{'净差均值':>9}{'毛实现中位':>10}{'cap均值':>9}")
    summary = []
    for name in names:
        caps, reals, uplifts, ntrig = [], [], [], 0
        for r in usable:
            v = r.get("variants", {}).get(name)
            if not v:
                continue
            if v.get("triggered"):
                ntrig += 1
            caps.append(v.get("capture"))
            reals.append(v.get("realized_pct"))
            lc = r["live"]["capture"]
            if v.get("capture") is not None and lc is not None:
                uplifts.append(v["capture"] - lc)
        cm, um = _med(caps), _med(uplifts)
        net_med = _med([p - fee_pct for p in reals])
        # 净差 (bps): 变体 realized - 实盘 realized 逐笔配对 (fee 双边同额相消, 差即净差)。
        #   未触发变体随实盘退出 → 差=0; 中位反映「多数笔是否有差异」, 均值反映
        #   「全员应用变体的期望净改善」(含未触发 0) — 后者是裁决口径。
        net_pairs = []
        for r in usable:
            v = r.get("variants", {}).get(name)
            if v and v.get("realized_pct") is not None and r["live"].get("realized_pct") is not None:
                net_pairs.append(v["realized_pct"] - r["live"]["realized_pct"])
        net_delta_med = round(_med(net_pairs) * 100, 1) if net_pairs else None
        net_delta_mean = round((sum(net_pairs) / len(net_pairs)) * 100, 1) if net_pairs else None
        print(f"  {name:<24}{ntrig:>5}{_fmt(cm):>9}"
              f"{_fmt_pct_unit(net_med):>10}"
              f"{(f'{net_delta_med:+.1f}bp' if net_delta_med is not None else 'n/a'):>9}"
              f"{(f'{net_delta_mean:+.1f}bp' if net_delta_mean is not None else 'n/a'):>9}"
              f"{_fmt_pct_unit(_med(reals)):>10}{_fmt(_mean(caps)):>9}")
        summary.append((name, ntrig, cm, um, net_med, net_delta_med, net_delta_mean))

    print("\n" + "\n".join(_baseline_drift_check(usable)))

    print(f"\n注: capture=已实现有利%/最大浮盈%; 未触发变体随实盘退出 (capture 同实盘)。")
    print(f"    净实现=毛实现-{fee_pct:.2f}%往返费; 净差=变体-实盘逐笔配对 (双边同费相消)。")
    print(f"    裁决口径: 净差均值 (全员应用期望) > 0 且样本 n≥{MIN_N} 才提议调参 (需确认)。")
    print("=" * 72)

    if push:
        parts = [f"📊 退出影子 capture (可用n={len(usable)}, MIN_N={MIN_N}, fee={fee_pct:.2f}%)",
                 f"实盘 cap中位 {_fmt(live_med)} 净实现中位 {_fmt_pct_unit(live_net_med)}"]
        for name, ntrig, cm, um, net_med, net_delta_med, net_delta_mean in summary:
            if name.startswith("baseline"):
                continue
            nb = f"净差均值{net_delta_mean:+.1f}bp" if net_delta_mean is not None else ""
            parts.append(f"{name}: cap中位{_fmt(cm)} vs实盘{_fmt(um)} 净实现{_fmt_pct_unit(net_med)} {nb} (触发{ntrig})")
        parts.append("✅ 样本已达门槛, 可评估调参"
                     if len(usable) >= MIN_N else f"⏳ 样本 {len(usable)}/{MIN_N}, 继续积累")
        _push("\n".join(parts))


def _push(msg):
    try:
        from app.notifier.feishu import send_text
        send_text(msg)
    except Exception:
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="退出参数影子 capture 分布报告 (含净口径)")
    ap.add_argument("--push", action="store_true", help="推送飞书摘要")
    ap.add_argument("--fee-pct", type=float, default=DEFAULT_FEE_PCT,
                    help=f"往返费用假设 pct (默认 {DEFAULT_FEE_PCT}, 实测 ~8bps)")
    _a = ap.parse_args()
    main(push=_a.push, fee_pct=_a.fee_pct)
