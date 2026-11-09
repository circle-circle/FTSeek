#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""代理 vs 真值：阈值/regime 判别的翻转量化 (RFC-190 第三轮 · 只读)

回答一个问题: 执行门/regime 用的是 VIXY×0.85 代理, 而真值是 CBOE 现货收盘。
**同一张阈值表被两把不同的尺子读**, 到底会错开多少天?

口径 (逐条照抄, 防前视):
  - 代理 = 账本 `vix_proxy_premarket` (当日盘前, DA 生料聚合众数);
  - 决策时可见的真值 = **上一交易日** `vix_close` (盘前可得);
  - 只用两者都齐的交易日; 缺一天就不算 (不插值、不补)。
  - regime 的 VIX 评分表照抄 `app/playbook/market_regime.py`:
      <15 +3 / 15..20 +1 / 20..30 -2 / >=30 -5

输出: 阈值翻转表 + regime 分项差分布 + 最坏日清单。

用法: venv/bin/python scripts/vix_caliber_divergence.py [--json OUT]
"""
import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "data" / "market_env_daily.jsonl"

THRESHOLDS = (15.0, 18.0, 20.0, 22.0, 25.0, 28.0, 30.0, 35.0)


def load_rows() -> List[dict]:
    if not LEDGER.exists():
        raise SystemExit("环境账本不存在, 先跑 scripts/market_env_ledger.py build")
    return [json.loads(ln) for ln in
            LEDGER.read_text(encoding="utf-8").splitlines() if ln.strip()]


def regime_vix_score(vix: Optional[float]) -> Optional[int]:
    if vix is None:
        return None
    if vix < 15:
        return 3
    if vix <= 20:
        return 1
    if vix < 30:
        return -2
    return -5


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    rows = load_rows()
    by_date = {r["date"]: r for r in rows}
    dates = sorted(by_date)

    pairs = []
    for i, day in enumerate(dates):
        if i == 0:
            continue
        proxy = by_date[day].get("vix_proxy_premarket")
        truth = by_date[dates[i - 1]].get("vix_close")   # 盘前可见 = 上一交易日收盘
        if proxy is None or truth is None:
            continue
        pairs.append({
            "date": day,
            "proxy": round(float(proxy), 2),
            "truth_prev": round(float(truth), 2),
            "truth_prev_date": dates[i - 1],
            "gap_pct": round((float(proxy) / float(truth) - 1) * 100, 2),
        })

    if not pairs:
        print("无同时具备代理与前置真值的交易日")
        return 0

    print("样本: %d 个交易日 (%s .. %s)" % (len(pairs), pairs[0]["date"], pairs[-1]["date"]))
    gaps = sorted(p["gap_pct"] for p in pairs)
    mid = gaps[len(gaps) // 2]
    print("代理 vs 前置真值: 中位 %+.1f%% / 最差 %+.1f%% / 最好 %+.1f%%"
          % (mid, gaps[0], gaps[-1]))

    print("\n① 阈值判据翻转 (代理与真值分处阈值两侧的天数)")
    print("%-8s %6s %6s %6s" % ("thr", "flip", "total", "flip%"))
    flips = {}
    for t in THRESHOLDS:
        n = sum(1 for p in pairs if (p["proxy"] >= t) != (p["truth_prev"] >= t))
        flips[t] = n
        print("%-8.0f %6d %6d %6.1f" % (t, n, len(pairs), n / len(pairs) * 100))

    print("\n② regime 的 VIX 分项 (照抄 market_regime 评分表)")
    print("%-12s %s" % ("date", "proxy(score) → truth(score)  delta"))
    deltas = []
    for p in pairs:
        sp = regime_vix_score(p["proxy"])
        st = regime_vix_score(p["truth_prev"])
        if sp != st:
            deltas.append((p, sp, st))
    for p, sp, st in deltas:
        print("%-12s %6.2f(%+d) → %6.2f(%+d)  %+d"
              % (p["date"], p["proxy"], sp, p["truth_prev"], st, st - sp))
    n3 = sum(1 for _, sp, st in deltas if abs(st - sp) >= 3)
    print("分项不同: %d/%d 天; 其中 |差| >= 3 分: %d 天" % (len(deltas), len(pairs), n3))

    if args.json:
        Path(args.json).write_text(json.dumps({
            "n": len(pairs), "pairs": pairs,
            "threshold_flips": {str(k): v for k, v in flips.items()},
            "regime_component_diff_days": len(deltas),
            "regime_component_diff_ge3": n3,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n已写入 %s" % args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())