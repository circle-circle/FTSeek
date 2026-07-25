#!/usr/bin/env python3
"""退出参数影子 capture 分布报告 (只读, 2026-07-13)。

读 data/shadow/exit_param_shadow.jsonl (finalize_exit_shadow 落盘), 聚合各变体
反事实 capture_ratio vs 实盘, 关注「更早保本/更紧锁利是否提升 capture」。

capture = 已实现有利% / 最大浮盈%。仅统计 max_fav_pct>0 的单 (摸到过浮盈,
capture 才有意义)。变体未触发 → 视同随实盘退出 (capture 与实盘一致)。

诚实边界: 只测【比现行更紧】的变体 (更紧必在实盘退出前触发, 反事实退出价落在
已观测价格路径内可评估); 更松的骑过实盘退出点、路径不可观测, 故不在评估范围。
样本达门槛 (≥20) 且某变体 capture 中位稳定 > baseline, 才提议改实盘 yaml (需确认)。

运行: PYTHONPATH=. ./venv/bin/python scripts/exit_shadow_report.py [--push]
"""
import json
import statistics
from pathlib import Path

SHADOW = Path(__file__).resolve().parents[1] / "data" / "shadow" / "exit_param_shadow.jsonl"
MIN_N = 20  # capture 中位可信的最小样本量


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


def main(push=False):
    print("=" * 72)
    print("退出参数影子 — capture 分布报告")
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
    mfe_med = _med([r.get("max_fav_pct") for r in usable])
    print(f"  实盘 capture 中位={_fmt(live_med)}  MFE% 中位={mfe_med:+.2f}%" if mfe_med
          else f"  实盘 capture 中位={_fmt(live_med)}")

    # 变体名从数据里取 (与 exit_shadow.EXIT_VARIANTS 对齐)
    names = []
    for r in usable:
        for k in r.get("variants", {}):
            if k not in names:
                names.append(k)

    print("\n── 各变体 capture vs 实盘 ──")
    print(f"  {'变体':<22}{'触发':>5}{'cap中位':>9}{'cap均值':>9}"
          f"{'实现中位':>9}{'vs实盘中位':>11}")
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
        print(f"  {name:<22}{ntrig:>5}{_fmt(cm):>9}{_fmt(_mean(caps)):>9}"
              f"{_fmt(_med(reals), pct=True):>9}{_fmt(um):>11}")
        summary.append((name, ntrig, cm, um))

    print(f"\n注: capture=已实现有利%/最大浮盈%; 未触发变体随实盘退出 (capture 同实盘)。")
    print(f"    样本门槛 n≥{MIN_N} 且变体 cap中位 稳定 > baseline 才提议调参 (需确认)。")
    print("=" * 72)

    if push:
        parts = [f"📊 退出影子 capture (可用n={len(usable)}, MIN_N={MIN_N})",
                 f"实盘 cap中位 {_fmt(live_med)}"]
        for name, ntrig, cm, um in summary:
            if name.startswith("baseline"):
                continue
            parts.append(f"{name}: cap中位{_fmt(cm)} vs实盘{_fmt(um)} (触发{ntrig})")
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
    import argparse
    ap = argparse.ArgumentParser(description="退出参数影子 capture 分布报告")
    ap.add_argument("--push", action="store_true", help="推送飞书摘要")
    main(push=ap.parse_args().push)
