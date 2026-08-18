#!/usr/bin/env python3
"""股性画像 L0-B 构建器 (symbol-profile P0-B, 只读)。

与 symbol_profile_builder (L0-A 交互史) 互补: 本脚本刻画「票自身的脾气」,
不依赖交易记录 — data/replay_bars_daily/ 110 票 × 5 年日K, 全池无冷启动。

核心方法论: 扁平统计必平庸 (全样本均值≈随机游走), 个性藏在条件切片里。
特征全部按「条件 → 倾向」组织, 为 L1 (LLM 认知卡) 提供弹药:

  volatility   波动结构: 日波幅中位 / 近20日波幅 regime / 大波幅日占比
  gap_behavior 跳空性格: 高开/低开回补率, 大缺口回补, 缺口延续 vs 回补
  trend_character 趋势性格: 连跌续跌率 (动量 vs 均值回归), 大涨/大跌次日行为, 20日效率比
  intraday_habit 日内习惯 (日线近似): 收盘距日高, 收盘在日内区间位置, 开盘惯性
  drawdown     回撤结构: 距20日高回撤, 历史最大回撤及恢复天数
  archetype    初步票型标签 (gap_filler/momentum/mean_revert/calm/neutral, 供 L1 归纳)

落盘: data/market_profiles/{symbol}.json (schema market_profile/L0@0.1)
横向: 默认输出全池个性排名 (对 0.5 的最大偏离 = 个性强度)。
用法: PYTHONPATH=. ./venv/bin/python scripts/market_profile_builder.py \
        [--symbols US.TSM,US.QCOM] [--no-json]
"""
from __future__ import annotations

import argparse
import json
import statistics as st
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "data" / "replay_bars_daily"
OUT_DIR = REPO / "data" / "market_profiles"

MIN_BARS = 120          # 至少半年数据
GAP_SMALL, GAP_BIG = 0.005, 0.015
BIG_MOVE = 0.02         # ±2% 视为大涨/大跌


def _med(xs, nd=3):
    xs = [x for x in xs if x is not None]
    return round(st.median(xs), nd) if xs else None


def _rate(n_hit, n_tot, nd=3):
    return round(n_hit / n_tot, nd) if n_tot else None


def build(rows: list[list]) -> dict:
    """rows: [[date,o,h,l,c],...] 升序。全部条件切片, 输出「条件→倾向」结构。"""
    o = [r[1] for r in rows]; h = [r[2] for r in rows]
    l = [r[3] for r in rows]; c = [r[4] for r in rows]
    n = len(rows)

    # ── 波动结构 ──
    rng = [(h[i] - l[i]) / c[i] * 100 for i in range(n)]
    rng20 = rng[-20:]
    vol = {"daily_range_pct_median": _med(rng),
           "range_regime_20d": round(st.median(rng20) / st.median(rng), 2),
           "big_range_day_pct": _rate(sum(1 for r in rng if r > 1.5 * st.median(rng)), n),
           "last_close_pct": round((c[-1] / c[-2] - 1) * 100, 2)}

    # ── 跳空性格 (条件切片: 缺口方向 × 大小) ──
    gaps = {"up_small": [0, 0], "up_big": [0, 0], "dn_small": [0, 0], "dn_big": [0, 0]}
    gap_cont = [0, 0]  # 高开后收盘仍高于昨收 (延续性格)
    for i in range(1, n):
        g = o[i] / c[i - 1] - 1
        filled_up = l[i] <= c[i - 1]
        filled_dn = h[i] >= c[i - 1]
        if g >= GAP_BIG:
            gaps["up_big"][0] += filled_up; gaps["up_big"][1] += 1
        elif g >= GAP_SMALL:
            gaps["up_small"][0] += filled_up; gaps["up_small"][1] += 1
        elif g <= -GAP_BIG:
            gaps["dn_big"][0] += filled_dn; gaps["dn_big"][1] += 1
        elif g <= -GAP_SMALL:
            gaps["dn_small"][0] += filled_dn; gaps["dn_small"][1] += 1
        if g >= GAP_SMALL:
            gap_cont[0] += c[i] > c[i - 1]; gap_cont[1] += 1
    gb = {"gap_up_fill_rate": _rate(gaps["up_small"][0], gaps["up_small"][1]),
          "gap_up_big_fill_rate": _rate(gaps["up_big"][0], gaps["up_big"][1]),
          "gap_dn_fill_rate": _rate(gaps["dn_small"][0], gaps["dn_small"][1]),
          "gap_dn_big_fill_rate": _rate(gaps["dn_big"][0], gaps["dn_big"][1]),
          "gap_up_continue_rate": _rate(gap_cont[0], gap_cont[1])}

    # ── 趋势性格 ──
    up2 = dn2 = tot_up2 = tot_dn2 = 0
    bu_c = bu_t = bd_c = bd_t = 0
    for i in range(2, n - 1):
        if c[i] < c[i - 1] < c[i - 2]:
            tot_dn2 += 1; dn2 += c[i + 1] < c[i]
        if c[i] > c[i - 1] > c[i - 2]:
            tot_up2 += 1; up2 += c[i + 1] > c[i]
    for i in range(n - 1):
        chg = c[i] / c[i - 1] - 1
        nxt = c[i + 1] / c[i] - 1
        if chg >= BIG_MOVE:
            bu_t += 1; bu_c += nxt > 0
        elif chg <= -BIG_MOVE:
            bd_t += 1; bd_c += nxt < 0
    ers = []  # 20日效率比: |净位移|/路径长度, 高=走得顺
    for i in range(20, n, 5):
        net = abs(c[i] - c[i - 20])
        path = sum(abs(c[j] - c[j - 1]) for j in range(i - 19, i + 1))
        if path > 0:
            ers.append(net / path)
    trend = {"down2_next_down_rate": _rate(dn2, tot_dn2),
             "up2_next_up_rate": _rate(up2, tot_up2),
             "big_up_next_up_rate": _rate(bu_c, bu_t),
             "big_down_next_down_rate": _rate(bd_c, bd_t),
             "efficiency_ratio_20d_median": _med(ers, 2)}

    # ── 日内习惯 (日线近似) ──
    off_high = [(h[i] - c[i]) / c[i] * 100 for i in range(n)]
    loc = [((c[i] - l[i]) / (h[i] - l[i])) if h[i] > l[i] else 0.5 for i in range(n)]
    beta = st.median([o[i] / c[i - 1] - 1 for i in range(1, n)])  # 开盘惯性别 (>0 高开癖)
    habit = {"close_off_high_pct_median": _med(off_high, 2),
             "close_loc_in_range_median": _med(loc),   # >0.6 收强 / <0.4 收弱
             "open_gap_median_pct": round(beta * 100, 3)}

    # ── 回撤结构 ──
    peak = c[0]; max_dd = 0.0; dd_start = None
    for i in range(n):
        if c[i] > peak:
            peak = c[i]
        d = (peak - c[i]) / peak
        if d > max_dd:
            max_dd, dd_start = d, rows[i][0]
    hi20 = max(c[-20:]); lo20 = min(c[-20:])
    dd = {"from_20d_high_pct": round((hi20 - c[-1]) / hi20 * 100, 2),
          "pos_in_20d_range": round((c[-1] - lo20) / (hi20 - lo20), 3) if hi20 > lo20 else None,
          "historical_max_dd_pct": round(max_dd * 100, 1), "max_dd_date": dd_start}

    # ── 初步票型 (供 L1 归纳, 不做最终裁决) ──
    tags, why = [], []
    gu = gb["gap_up_fill_rate"]; dn2r = trend["down2_next_down_rate"]
    if gu is not None and gu >= 0.55:
        tags.append("gap_filler"); why.append(f"高开回补率 {gu}")
    if dn2r is not None and dn2r >= 0.55:
        tags.append("momentum"); why.append(f"连跌续跌率 {dn2r}")
    elif dn2r is not None and dn2r <= 0.45:
        tags.append("mean_revert"); why.append(f"连跌续跌率 {dn2r}")
    if vol["daily_range_pct_median"] is not None and vol["daily_range_pct_median"] >= 3.0:
        tags.append("high_beta"); why.append(f"日波幅中位 {vol['daily_range_pct_median']}%")
    elif vol["daily_range_pct_median"] is not None and vol["daily_range_pct_median"] <= 1.2:
        tags.append("calm"); why.append(f"日波幅中位 {vol['daily_range_pct_median']}%")
    if not tags:
        tags, why = ["neutral"], ["各项倾向均不显著 (个性平庸, 只配通用策略)"]

    return {"volatility": vol, "gap_behavior": gb, "trend_character": trend,
            "intraday_habit": habit, "drawdown": dd,
            "archetype": {"tags": tags, "basis": why},
            "n_bars": n, "date_range": [rows[0][0], rows[-1][0]]}


def personality(p: dict) -> float:
    """个性强度 = 各倾向指标对 0.5 的最大偏离 (高=有脾气, 低=平庸)."""
    cands = [abs(p["gap_behavior"].get(k) - 0.5) for k in
             ("gap_up_fill_rate", "gap_dn_fill_rate", "gap_up_continue_rate")
             if p["gap_behavior"].get(k) is not None]
    cands += [abs(p["trend_character"].get(k) - 0.5) for k in
              ("down2_next_down_rate", "up2_next_up_rate",
               "big_up_next_up_rate", "big_down_next_down_rate")
              if p["trend_character"].get(k) is not None]
    return round(max(cands), 3) if cands else 0.0


def render(symbol: str, p: dict) -> str:
    gb, tc, hb, dd, vol = p["gap_behavior"], p["trend_character"], p["intraday_habit"], p["drawdown"], p["volatility"]
    return "\n".join([
        f"\n{'=' * 62}",
        f"◆ {symbol} 股性 (L0-B, {p['n_bars']} 根日K, {p['date_range'][0]} → {p['date_range'][1]}), "
        f"票型: {'/'.join(p['archetype']['tags'])} (个性强度 {personality(p)})",
        f"  波动: 日波幅中位 {vol['daily_range_pct_median']}% | 近20日regime {vol['range_regime_20d']} | "
        f"大波幅日 {vol['big_range_day_pct']}",
        f"  跳空: 高开回补 {gb['gap_up_fill_rate']} (大缺口 {gb['gap_up_big_fill_rate']}) | "
        f"低开回补 {gb['gap_dn_fill_rate']} | 高开延续率 {gb['gap_up_continue_rate']}",
        f"  趋势: 连跌续跌 {tc['down2_next_down_rate']} | 连涨续涨 {tc['up2_next_up_rate']} | "
        f"大涨次日涨 {tc['big_up_next_up_rate']} | 大跌次日跌 {tc['big_down_next_down_rate']} | ER20 {tc['efficiency_ratio_20d_median']}",
        f"  习惯: 收盘距日高 {hb['close_off_high_pct_median']}% | 收盘位置 {hb['close_loc_in_range_median']} | "
        f"开盘惯性 {hb['open_gap_median_pct']}%",
        f"  回撤: 距20日高 {dd['from_20d_high_pct']}% | 20日区间位置 {dd['pos_in_20d_range']} | "
        f"历史最大 {dd['historical_max_dd_pct']}% ({dd['max_dd_date']})",
        f"  票型依据: {'; '.join(p['archetype']['basis'])}",
    ])


def main() -> int:
    ap = argparse.ArgumentParser(description="股性画像 L0-B 构建器 (只读, 全池)")
    ap.add_argument("--symbols", help="逗号分隔 (US.TSM 形式); 缺省全池 + 横向个性排名")
    ap.add_argument("--no-json", action="store_true")
    args = ap.parse_args()

    if args.symbols:
        files = {s: SRC / f"{s.replace('.', '_')}.json" for s in
                 (x.strip() for x in args.symbols.split(","))}
    else:
        files = {fp.stem.replace("_", ".", 1): fp for fp in sorted(SRC.glob("*.json"))}
    if not args.no_json:
        OUT_DIR.mkdir(parents=True, exist_ok=True)

    ranked = []
    for sym, fp in files.items():
        if not fp.exists():
            print(f"[skip] {sym} 无日K ({fp.name})")
            continue
        rows = [r for r in json.loads(fp.read_text()) if r[4]]
        if len(rows) < MIN_BARS:
            print(f"[skip] {sym} 日K不足 ({len(rows)} < {MIN_BARS})")
            continue
        p = build(rows)
        p.update({"schema": "market_profile/L0@0.1", "symbol": sym,
                  "generated_at": datetime.now().isoformat(timespec="seconds")})
        ranked.append((sym, p))
        if args.symbols:
            print(render(sym, p))
        if not args.no_json:
            (OUT_DIR / f"{sym.replace('.', '_')}.json").write_text(
                json.dumps(p, ensure_ascii=False, indent=2))

    if not args.symbols and ranked:
        ranked.sort(key=lambda kv: -personality(kv[1]))
        print(f"\n全池 {len(ranked)} 票, 个性强度排名 (对0.5最大偏离; <0.08 视为平庸):")
        for sym, p in ranked[:12]:
            print(f"  {sym:<12} {personality(p):<6} {'/'.join(p['archetype']['tags'])}")
        flat = [s for s, p in ranked if personality(p) < 0.08]
        print(f"  平庸票 ({len(flat)}): {', '.join(s.replace('US.', '') for s in flat[:15])}{'...' if len(flat) > 15 else ''}")
        print(f"\n单票详情: --symbols US.TSM | 产物: data/market_profiles/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
