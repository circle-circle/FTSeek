#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E2 入场动量确认毕业门 (DEB-20260807-grilling §5.1 预注册判据 + 宪章第9律).

预注册条文 (2026-08-07, 见数前):
  判据: "累计 20-30 笔后若'拦截组全亏、放行组盈利'仍成立, 提请用户裁决是否转硬门"
  定义: 入场后 15min 窗口 MFE ≥ θ(0.5%) = 有动能(放行); < θ = 无动能(拦截候选)
  人群: 前向增量影子 (data/e2_momentum_shadow.jsonl, 8/3 起增量口径) —
        判据预注册后产生的样本。历史回放 (e2_momentum_replay_latest.json, n=136)
        是【设计集】, 只作上下文对照, 不进判决 (防见数后换边/双重计数)。
  统计: 宪章第9律 — 按日聚类配对差 t + MDE; |效应|≤MDE 一律"不可分辨"。

执行语义诚实披露 (预注册未覆盖, 见数前声明):
  本影子的"拦截组净亏"= 那些交易【走完全程退出链】的真实净额。若转硬门,
  实际机制是"入场后 15min 无动能 → 提前出场", 届时实现的是 15min 时刻价
  而非最终退出价 — 影子数字是方向证据, 不是硬门实现后的精确期望。

输出: stdout 判决 + data/e2_momentum_graduation_report.json
零交易路径. 回滚: 删本脚本 + report.
"""
import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
SHADOW = REPO / "data" / "e2_momentum_shadow.jsonl"
REPLAY = REPO / "data" / "e2_momentum_replay_latest.json"  # 设计集, 仅上下文
OUT = REPO / "data" / "e2_momentum_graduation_report.json"
THETA = 0.5  # 预注册 θ (%)

# ── 退役开关 (2026-09-13 用户裁「无效就停止，退役」) ──────────────────
# 裁决证据 (裁决前复核, /tmp/e2_deep.py 全量重算):
#   ① 统计不可分辨: 日聚类 t=-2.897 显著但 |diff|=29.91 < MDE=40.88 (宪章第9律);
#   ② OOS 衰减归零: IS(7/28-8/19) 拦截组均-$25.15 → OOS(8/20-9/11) 均+$0.21 —
#      现役门 (DA v2.4 禁接飞刀/RFC-082 等) 已把最烂入场提前拦掉, E2 残差价值趋零;
#   ③ 误杀率 50.7%: 拦截组 138 笔中 70 笔盈利(+$1791, 含 SL_TRAIL_LOCK 赢家 21 笔),
#      转硬门=砍在利润引擎起涨前;
#   ④ SHORT 样本不足 (6拦/9放) — 宪章方向对称硬伤无法补验;
#   ⑤ 实现语义差距: 影子=全程退出净额, 硬门=15min 提前出场价, 数字不可平移。
# 数据留档不删 (data/e2_momentum_shadow.jsonl 242 行), 裁决恒 RETIRED_NO_PROMOTION。
# 回滚 = 退役须另开辩论改判, 不得仅删本开关 (同 premarket_invalidation A/C 先例)。
RETIRED = True


def load_shadow():
    rows = []
    seen = set()
    for line in SHADOW.open(encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("pnl_net") is None or r.get("window_mfe_pct") is None:
            continue
        k = r.get("decision_id") or (r.get("symbol"), r.get("trade_date"))
        if k in seen:
            continue
        seen.add(k)
        rows.append(r)
    return rows


def clustered_test(blocked, passed):
    """按日聚类: 同日两组净额均值差, 配对 t + MDE (宪章第9律)."""
    by_day_b, by_day_p = defaultdict(list), defaultdict(list)
    for r in blocked:
        by_day_b[r["trade_date"]].append(float(r["pnl_net"]))
    for r in passed:
        by_day_p[r["trade_date"]].append(float(r["pnl_net"]))
    diffs, pairs = [], 0
    for d in sorted(set(by_day_b) & set(by_day_p)):
        diffs.append(np.mean(by_day_b[d]) - np.mean(by_day_p[d]))
        pairs += 1
    diffs = np.array(diffs)
    n = len(diffs)
    if n < 2:
        return None, None, None, pairs
    m = float(diffs.mean())
    sd = float(diffs.std(ddof=1))
    t = m / (sd / math.sqrt(n)) if sd > 0 else None
    mde = 2.8 * sd * math.sqrt(2 / n) if sd > 0 else None
    return m, t, mde, pairs


def main():
    if not SHADOW.exists():
        print("无影子数据")
        return 1
    rows = load_shadow()
    blocked = [r for r in rows if r["window_mfe_pct"] < THETA]
    passed = [r for r in rows if r["window_mfe_pct"] >= THETA]
    n_b, n_p = len(blocked), len(passed)
    net_b = sum(float(r["pnl_net"]) for r in blocked)
    net_p = sum(float(r["pnl_net"]) for r in passed)
    wr_b = sum(1 for r in blocked if float(r["pnl_net"]) > 0) / max(n_b, 1)
    wr_p = sum(1 for r in passed if float(r["pnl_net"]) > 0) / max(n_p, 1)
    mean_b = net_b / max(n_b, 1)
    mean_p = net_p / max(n_p, 1)

    # 预注册字面判据: "拦截组全亏、放行组盈利"
    literal_pattern = (wr_b == 0.0) and (net_p > 0)
    # 净口径版 (更稳健的表述): 拦截组净均<0 且 放行组净均>0
    net_pattern = (mean_b < 0) and (mean_p > 0)

    diff_mean, t_cl, mde, n_pairs = clustered_test(blocked, passed)
    significant = (t_cl is not None and mde is not None
                   and t_cl <= -2.0 and abs(diff_mean) > mde)

    # 设计集上下文 (不进判决)
    ctx = {}
    if REPLAY.exists():
        try:
            rp = json.loads(REPLAY.read_text(encoding="utf-8"))
            tr = rp.get("trades", [])
            rb = [t for t in tr if (t.get("window_mfe") or 0) < THETA]
            rp_ = [t for t in tr if (t.get("window_mfe") or 0) >= THETA]
            ctx = {"note": "设计集(历史回放), 仅上下文不作判决",
                   "n": len(tr),
                   "blocked_net": round(sum(t.get("pnl_net") or 0 for t in rb), 1),
                   "passed_net": round(sum(t.get("pnl_net") or 0 for t in rp_), 1)}
        except Exception:
            ctx = {"note": "设计集读取失败"}

    if RETIRED:
        # 恒判退役 (2026-09-13 用户裁): 读数照常结算呈报 (历史留档+趋势可见),
        # 但裁决永为 RETIRED_NO_PROMOTION, 任何后续读数不得再提请转硬门。
        verdict = ("🪦 E2 已退役 (2026-09-13 用户裁「无效就停止，退役」): 统计不可分辨"
                   "(|diff|<MDE) + OOS 拦截组衰减归零(-$25.15→+$0.21, 现役门已覆盖其人群) "
                   "+ 误杀率50.7% (70/138 盈利单含 trailing 赢家) — 恒判 RETIRED_NO_PROMOTION, "
                   "读数仅留档。回滚须另开辩论。")
        decision = "RETIRED_NO_PROMOTION"
    elif n_b + n_p < 20:
        verdict = f"样本不足 (n={n_b + n_p} < 20 预注册门槛), 继续攒样"
        decision = "INSUFFICIENT"
    elif significant:
        verdict = ("✅ 模式成立且统计可分辨 (拦截组显著更差且超MDE) — "
                   "按预注册判据提请用户裁决是否转硬门")
        decision = "PATTERN_CONFIRMED_SIGNIFICANT"
    elif net_pattern:
        verdict = ("⚠️ 净口径模式成立但统计不可分辨 (未超MDE或t不足) — "
                   "按预注册判据提请用户裁决(证据等级: 方向性); 或继续攒样")
        decision = "PATTERN_CONFIRMED_DIRECTIONAL"
    else:
        verdict = "❌ 模式不成立 — 不建议转硬门, 影子继续观察"
        decision = "PATTERN_FAILED"

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "pre_registered": "DEB-20260807-grilling §5.1 (2026-08-07, 见数前)",
        "population": "前向增量影子 8/3 起 (判据预注册后样本)",
        "theta_pct": THETA, "window_min": 15,
        "n_blocked": n_b, "n_passed": n_p,
        "blocked": {"net": round(net_b, 1), "mean": round(mean_b, 2),
                    "win_rate": round(wr_b, 3)},
        "passed": {"net": round(net_p, 1), "mean": round(mean_p, 2),
                   "win_rate": round(wr_p, 3)},
        "literal_pattern_blocked_all_loss_passed_profit": literal_pattern,
        "net_pattern_blocked_neg_passed_pos": net_pattern,
        "daily_clustered": {"n_paired_days": n_pairs,
                            "diff_mean": (round(diff_mean, 2) if diff_mean is not None else None),
                            "t": (round(t_cl, 3) if t_cl is not None else None),
                            "mde": (round(mde, 2) if mde is not None else None),
                            "significant_beyond_mde": significant},
        "design_set_context": ctx,
        "execution_semantics_caveat": ("影子拦截组净亏=全程退出链真实净额; 转硬门后实现的是"
                                       "15min时刻提前出场价, 影子数字是方向证据非精确期望"),
        "retired": RETIRED,
        "decision": decision,
        "verdict": verdict,
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
