#!/opt/ftseek/FTSeek-US/venv/bin/python3
# -*- coding: utf-8 -*-
"""OKR-2026-11 KR1/KR2 判分 + §12 三窗披露 (纯函数模块, 只读)。

为什么要有这个文件 (2026-11-18 实测): `scripts/okr_status.py` **没有 OKR-2026-11 判分块** —
它停在 OKR-2026-10, 且自注「判分模块 okr_2026_10_scoring 未建 (落地债#2)」。⇒ 11 月的**主 KR**
(DA-only 净 bps/笔 > 0) 目前只能手工复算。手工复算已在同一会话内实测出两次口径/算术错误
(交易日数按工作日粗估漏掉感恩节与周六; 把 open_positions 合成 TP 记成 94.07 而日志实证 114.18),
而 §12.3 现在**预注册要求** 11-28 / 11-30 两个时点各出三行读数、每行 8 个统计量 —— 手算不可持续。

设计承 `scripts/okr_2026_09_scoring.py` 同一套规矩:
  · **纯函数 + 零副作用**: 判分逻辑只吃 list[dict] / 标量, 不碰 DB、不写盘 ⇒ 可被单测钉死
    (`okr_status.py` 是模块级执行体, import 即跑 SQL, 契约测试无法 import)。
  · **fail-close**: 空分母 / 缺列 / 读库失败 → `None` + `NOT_MEASURABLE`, **绝不退化成 0**。
    (0 与"没测到"在判分里是两个结论; 把没测到写成 0 就是造假达标。)
  · **待裁项不自评**: §12.4 已由用户裁决 (方案 B), 故本模块按 §12.5 细则执行; 但 §12.6 明确
    **§5 (11-30 结算) 未被顺延** ⇒ 本模块对 §5 仍按原日期判, 不得类推顺延。
  · **判据常量全部出自 `docs/OKR-2026-11.md` 原文, 改这里 = 改判据 = 须先改 OKR + 用户裁。**

口径声明 (量纲):
  · `realized_pnl` = 毛 (USD), `fees_est` = 费 (USD, 模型估计 `fees_source='est'`, 非逐笔对账),
    `pnl_net` = 净 (USD)。实证 33.82 − 4.36 = 29.46 ✓。
  · bps = 金额 / 名义额 × 1e4 (1bp = 0.01%); 名义额 = `entry_price × quantity` (USD)。
  · `mfe_pct` 单位是**百分数** (0.6279 = 0.6279%), A11 死单判据 = 落库 `mfe_pct` < 0.3。
  · 日聚类: 按 `trade_date` 聚合成"每日净 bps"序列, t = mean/(sd/√日数); MDE80 = 2.8016×SE;
    80%CI 下界 = mean − 1.2816×SE。日数 <2 ⇒ 全部 `None` (不可算, 不写 0)。
  · A9 双口径 (含/不含 `exclusion_reason` 过滤) + A10 谓词 (`db_utils.era_clean_where`) 缺一即
    KR5 WARNING — 这是 OKR §KR5 与判据字典 A9/A10 的硬要求。

回滚: 删本文件即可 (只读判分, 零交易行为、零退出参数、零四锁、不写生产库)。
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]

# ── 判据常量: 全部出自 docs/OKR-2026-11.md 原文。改这里 = 改判据 = 须先改 OKR + 用户裁 ──
OKR_DOC = "docs/OKR-2026-11.md"
COHORT_START = "2026-11-01"          # §8: 11 月 cohort 起点
SETTLEMENT_DATE = "2026-11-30"       # §5: 结算判据日 (**未被 §12.4 顺延**, 见 §12.6)
PIVOT_DATE_ORIGINAL = "2026-11-28"   # §6 原判日 (方案 B 已顺延, 见 PIVOT_RULE)
KR1_GATE_N = 120                     # KR1 样本门: n≥120
KR1_NO_REGRESS_BPS = -2.25           # KR1 不回归线 (§2.2 前进基线 DA -2.25bps)
KR2_GATE_DEAD_PCT = 23.0             # KR2: 死单率 ≤23.0% (盈亏平衡线)
A11_DEAD_MFE_PCT = 0.3               # A11: 死单唯一口径 = 落库 mfe_pct < 0.3 (百分数)
MIN_N_FOR_ANY_CLAIM = 30             # 宪章: n<30 不下结论
PRIMARY_SOURCE = "DA"                # KR1 主 KR 只看 DA 来源

# §12.1: 模型切换 = 行为变更 ⇒ cohort 在 11-17 重置, pre/post 必须分开报 (§8)
POST_WINDOW_START = "2026-11-17"     # qwen4-max 首个生效交易日 (含当日)
MODEL_PRE = "qwen3.7-max"
MODEL_POST = "qwen4-max"

# §12.4 裁决 = 方案 B (用户 2026-11-18 07:44 CST)
PIVOT_RULE = {
    "ruled": "B",
    "ruled_at": "2026-11-18T07:44+08:00",
    "pivot_population": "post",       # §6 判据人群 = post 子窗
    "defer_to": "post_n_ge_120_or_dec_kickoff_whichever_first",
    "threshold_changed": False,       # n≥120 样本门与 §6 判据文字一字未动
    "settlement_date_deferred": False,  # §12.6: §5 (11-30) **未**被顺延
}

# 美股 2026 休市/半日市 (感恩节 11-26 休、11-27 半日、圣诞 12-25 休)。
# 日历必须显式列举: 11-28 是**周六**, 按工作日粗估会把 post 子窗多算 2 天 (实测 9→7)。
US_HOLIDAYS_2026 = frozenset({date(2026, 11, 26), date(2026, 12, 25)})
US_HALF_DAYS_2026 = frozenset({date(2026, 11, 27)})


# ── 通用小工具 ──────────────────────────────────────────────────────────────


def _num(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _bps(amount: Optional[float], notional: Optional[float]) -> Optional[float]:
    """空分母 ⇒ None (绝不写 0)。"""
    if amount is None or notional is None or notional <= 0:
        return None
    return round(amount / notional * 1e4, 4)


def _row_net(row: Dict[str, Any]) -> Optional[float]:
    net = _num(row.get("pnl_net"))
    if net is not None:
        return net
    gross = _num(row.get("realized_pnl"))
    fees = _num(row.get("fees_est"))
    if gross is None or fees is None:
        return None
    return gross - fees


def classify_source(decision_id: Any) -> str:
    """按 decision_id 前缀分源 (与 `execution_tca_shadow._source` / C18 复算同一约定)。"""
    text = str(decision_id or "")
    if text.startswith("DA-"):
        return "DA"
    if text.startswith("EXT"):
        return "EXT"
    if text.startswith("MOM"):
        return "MOM"
    if text.startswith("universe_expansion"):
        return "UE"
    return "OTHER"


def is_trading_day(day: date, holidays: Iterable[date] = US_HOLIDAYS_2026) -> bool:
    return day.weekday() < 5 and day not in set(holidays)


def trading_days(start: Any, end: Any,
                 holidays: Iterable[date] = US_HOLIDAYS_2026) -> List[date]:
    """[start, end] 闭区间内的美股交易日 (半日市计入 — 仍有成交)。"""
    begin = _as_date(start)
    finish = _as_date(end)
    if begin is None or finish is None or finish < begin:
        return []
    out, cursor = [], begin
    while cursor <= finish:
        if is_trading_day(cursor, holidays):
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def project_gate_date(daily_rate: Optional[float], needed_n: int, start: Any,
                      holidays: Iterable[date] = US_HOLIDAYS_2026) -> Optional[date]:
    """按日均笔数外推 n 达标日 (含 start 当日)。速率缺失/非正 ⇒ None。"""
    if not daily_rate or daily_rate <= 0 or needed_n <= 0:
        return None
    need_days = math.ceil(needed_n / daily_rate)
    cursor = _as_date(start)
    if cursor is None:
        return None
    counted = 0
    while counted < need_days:
        if is_trading_day(cursor, holidays):
            counted += 1
            if counted == need_days:
                return cursor
        cursor += timedelta(days=1)
    return None


# ── 核心统计 ────────────────────────────────────────────────────────────────


def window_stats(rows: Sequence[Dict[str, Any]], label: str) -> Dict[str, Any]:
    """一个窗口 × 一个来源的完整读数 (§12.3 要求的 8 个统计量)。

    n=0 ⇒ 所有金额/bps/统计量一律 None, verdict 交给上层判 NOT_MEASURABLE。
    """
    n = len(rows)
    out: Dict[str, Any] = {
        "window": label, "n": n,
        "notional": None, "gross": None, "fees": None, "net": None,
        "gross_bps": None, "fee_bps": None, "net_bps": None,
        "net_per_trade": None,
        "gross_missing_n": 0, "fee_missing_n": 0, "net_missing_n": 0,
        "n_days": 0, "day_clustered_mean_bps": None, "day_clustered_sd_bps": None,
        "day_clustered_t": None, "mde80_bps": None,
        "ci80_lower_bps": None, "ci80_upper_bps": None, "ci80_contains_zero": None,
        "underpowered_n_lt_30": n < MIN_N_FOR_ANY_CLAIM if n else None,
    }
    if not n:
        return out
    notional = gross = fees = net = 0.0
    gross_known = fees_known = net_known = True
    days: Dict[str, List[float]] = defaultdict(lambda: [0.0, 0.0])
    for row in rows:
        entry = _num(row.get("entry_price"))
        qty = _num(row.get("quantity"))
        gross_value = _num(row.get("realized_pnl"))
        fee_value = _num(row.get("fees_est"))
        net_row = _row_net(row)
        if gross_value is None:
            gross_known = False
            out["gross_missing_n"] += 1
        if fee_value is None:
            fees_known = False
            out["fee_missing_n"] += 1
        if net_row is None:
            net_known = False
            out["net_missing_n"] += 1
        row_notional = (entry or 0.0) * (qty or 0.0)
        notional += row_notional
        if gross_value is not None:
            gross += gross_value
        if fee_value is not None:
            fees += fee_value
        if net_row is not None:
            net += net_row
        day = str(row.get("trade_date") or "")[:10]
        if day and net_row is not None:
            days[day][0] += net_row
            days[day][1] += row_notional
    out.update({
        "notional": round(notional, 2),
        "gross": round(gross, 2) if gross_known else None,
        "fees": round(fees, 2) if fees_known else None,
        "net": round(net, 2) if net_known else None,
        "gross_bps": _bps(gross if gross_known else None, notional),
        "fee_bps": _bps(fees if fees_known else None, notional),
        "net_bps": _bps(net if net_known else None, notional),
        "net_per_trade": round(net / n, 4) if net_known and n else None,
        "n_days": len(days),
    })
    daily = [v[0] / v[1] * 1e4 for v in days.values() if v[1] > 0] if net_known else []
    if len(daily) >= 2:
        mean = statistics.mean(daily)
        sd = statistics.stdev(daily)
        se = sd / math.sqrt(len(daily))
        out["day_clustered_mean_bps"] = round(mean, 4)
        out["day_clustered_sd_bps"] = round(sd, 4)
        out["day_clustered_t"] = round(mean / se, 4) if se > 0 else None
        out["mde80_bps"] = round(2.8016 * se, 4) if se > 0 else None
        if se > 0:
            lower = mean - 1.2816 * se
            upper = mean + 1.2816 * se
            out["ci80_lower_bps"] = round(lower, 4)
            out["ci80_upper_bps"] = round(upper, 4)
            out["ci80_contains_zero"] = bool(lower <= 0.0 <= upper)
    return out


def kr1_verdict(stats: Dict[str, Any], gate_n: int = KR1_GATE_N,
                no_regress_bps: float = KR1_NO_REGRESS_BPS) -> Dict[str, Any]:
    """KR1 判定 (原文: 点估计 >0; 样本门 n≥120; 不回归线 -2.25bps; CI 含 0 只写不可分辨)。"""
    n = stats.get("n") or 0
    net_bps = stats.get("net_bps")
    if not n or net_bps is None:
        return {"verdict": "NOT_MEASURABLE", "gate_met": None,
                "point_estimate_positive": None, "below_no_regress_line": None,
                "note": "空分母/无读数 ⇒ fail-close, 不判 0 也不判达标"}
    gate_met = n >= gate_n
    positive = net_bps > 0
    below = net_bps < no_regress_bps
    # CI 含 0 = 下界 ≤0≤ 上界 (两端都由 window_stats 算出; 只用下界会把"点估计为负且
    # 上界仍 >0"误判成不含 0)。日数 <2 ⇒ None ⇒ 判"不可分辨"而非"不含 0"。
    ci_contains_zero = stats.get("ci80_contains_zero")
    if not gate_met:
        verdict = "UNDERPOWERED"
        note = (f"n={n} < 样本门 {gate_n} ⇒ 按 KR1 原文如实报实际 n + MDE "
                f"(MDE80={stats.get('mde80_bps')}bps), 判「未达, 功效不足」; "
                f"点估计 {net_bps:+.2f}bps 不作达标/证伪用")
    elif positive:
        verdict = "MET_POINT_ESTIMATE"
        ci_lower = stats.get("ci80_lower_bps")
        ci_upper = stats.get("ci80_upper_bps")
        if ci_contains_zero is None or ci_lower is None or ci_upper is None:
            ci_note = "日等权 CI 不可算, 只能判「不可分辨」"
        elif ci_contains_zero:
            ci_note = (f"日等权 CI 含 0 (CI=[{ci_lower}, {ci_upper}]) ⇒ 按 §5「不算失败: 证据不足」, "
                       f"不得宣称稳定 edge; 统计对象=日等权净 bps, 非名义加权点估计")
        elif ci_lower > 0:
            ci_note = (f"日等权 CI 下界 >0 (CI=[{ci_lower}, {ci_upper}]); "
                       f"统计对象=日等权净 bps, 非名义加权点估计")
        elif ci_upper < 0:
            ci_note = (f"日等权 CI 整体 <0 (CI=[{ci_lower}, {ci_upper}]), 与名义加权点估计方向不一致; "
                       f"不得宣称稳定 edge")
        else:
            ci_note = (f"日等权 CI 不含 0 但边界异常 (CI=[{ci_lower}, {ci_upper}]); "
                       f"不得宣称稳定 edge")
        note = f"点估计 {net_bps:+.2f}bps >0; {ci_note}"
    else:
        verdict = "NOT_MET"
        note = f"点估计 {net_bps:+.2f}bps ≤0 且 n={n}≥{gate_n}"
    if below:
        note += (f"; ⚠️ 低于不回归线 {no_regress_bps}bps 达 "
                 f"{abs(net_bps - no_regress_bps):.2f}bps")
        if stats.get("mde80_bps") is not None and abs(net_bps - no_regress_bps) < stats["mde80_bps"]:
            note += " (但差异 < MDE80 ⇒ **不可分辨**, 不得宣称已回归)"
    return {"verdict": verdict, "gate_met": gate_met,
            "point_estimate_positive": positive, "below_no_regress_line": below,
            "ci80_contains_zero": ci_contains_zero, "note": note}


def kr2_dead_rate(rows: Sequence[Dict[str, Any]],
                  gate_pct: float = KR2_GATE_DEAD_PCT,
                  threshold_pct: float = A11_DEAD_MFE_PCT,
                  gate_n: int = KR1_GATE_N) -> Dict[str, Any]:
    """KR2 死单率 (A11 唯一口径 = 落库 mfe_pct < 0.3%, 单位百分数)。

    tick / 闸门 / 决策时刻 peak 三口径**不参与判定** (A11 原文), 本模块连读都不读,
    避免误用。mfe_pct 缺失的行单列计数, 不进分母 (缺列 fail-close, 不当 0)。
    """
    have = [r for r in rows if _num(r.get("mfe_pct")) is not None]
    missing = len(rows) - len(have)
    if not have:
        return {"verdict": "NOT_MEASURABLE", "n": len(rows), "n_with_mfe": 0,
                "mfe_missing": missing, "dead_n": None, "dead_pct": None,
                "note": "无 mfe_pct ⇒ 不可测 (不以 0 冒充)"}
    dead = [r for r in have if float(r["mfe_pct"]) < threshold_pct]
    alive = [r for r in have if float(r["mfe_pct"]) >= threshold_pct]
    pct = len(dead) / len(have) * 100.0

    def _per_trade(group: List[Dict[str, Any]]) -> Optional[float]:
        if not group:
            return None
        total = 0.0
        for row in group:
            value = _row_net(row)
            if value is None:
                return None
            total += value
        return round(total / len(group), 4)

    underpowered = len(have) < gate_n
    return {
        "verdict": ("UNDERPOWERED" if underpowered else
                    "MET" if pct <= gate_pct else "NOT_MET"),
        "caliber": "A11: 落库 mfe_pct < 0.3 (百分数)",
        "n": len(rows), "n_with_mfe": len(have), "mfe_missing": missing,
        "dead_n": len(dead), "dead_pct": round(pct, 4), "gate_pct": gate_pct,
        "margin_pp": round(gate_pct - pct, 4),
        "dead_net_per_trade": _per_trade(dead), "alive_net_per_trade": _per_trade(alive),
        "dead_net_bps": _bps(_sum_net(dead), _sum_notional(dead)),
        "alive_net_bps": _bps(_sum_net(alive), _sum_notional(alive)),
        "note": (f"死单率 {pct:.2f}% vs 门 ≤{gate_pct}% (差 {gate_pct - pct:+.2f}pp)"
                 + (f"; n={len(have)} < {gate_n} ⇒ 功效不足, 只报实测" if underpowered else "")),
    }


def _sum_net(rows: Sequence[Dict[str, Any]]) -> Optional[float]:
    total = 0.0
    for row in rows:
        value = _row_net(row)
        if value is None:
            return None
        total += value
    return total


def _sum_notional(rows: Sequence[Dict[str, Any]]) -> float:
    return sum((_num(r.get("entry_price")) or 0.0) * (_num(r.get("quantity")) or 0.0)
               for r in rows)


def exit_reason_breakdown(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 exit_reason 拆净贡献 (亏损结构定位; 只报事实, 不据此开门 — C11~C18 已封)。"""
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get("exit_reason") or "(none)")].append(row)
    out = []
    for reason, group in groups.items():
        net = _sum_net(group)
        notional = _sum_notional(group)
        out.append({"exit_reason": reason, "n": len(group),
                    "net": round(net, 2) if net is not None else None,
                    "net_per_trade": round(net / len(group), 4) if net is not None and group else None,
                    "net_bps": _bps(net, notional)})
    return sorted(out, key=lambda item: (item["net"] is None,
                                         item["net"] if item["net"] is not None else 0.0))


# ── §12.3 三窗披露 ──────────────────────────────────────────────────────────


def three_window_disclosure(rows: Sequence[Dict[str, Any]],
                            as_of: Any,
                            post_start: str = POST_WINDOW_START) -> Dict[str, Any]:
    """§12.3: 整窗 / pre / post **必须同时给出**, 缺一即 KR5 WARNING。

    达标日外推**必须给双速率区间**, 不得只报一个数:
      · post 自身速率 = post 笔数 ÷ post 已实现交易日 — 口径最贴, 但 as_of 靠近切换日时
        分母只有 1~2 天, 且当日仍持仓的笔尚未结算 ⇒ **系统性偏低** (实测 11-17: 5÷1=5.0/日,
        而整窗 109÷12=9.08/日 ⇒ 单看 post 会把达标日推后 9 个交易日)。
      · 整窗速率 = 全窗笔数 ÷ 全窗已实现交易日 — 分母稳, 但混了两个模型时代的成交节奏。
    两者都报 + 取区间, 由人裁决用哪个; 脚本不替用户挑。
    """
    whole = [r for r in rows]
    pre = [r for r in rows if str(r.get("trade_date") or "")[:10] < post_start]
    post = [r for r in rows if str(r.get("trade_date") or "")[:10] >= post_start]
    as_of_date = _as_date(as_of)
    post_days = trading_days(post_start, as_of_date) if as_of_date else []
    whole_days = trading_days(COHORT_START, as_of_date) if as_of_date else []
    post_rate = (len(post) / len(post_days)) if post_days else None
    whole_rate = (len(whole) / len(whole_days)) if whole_days else None
    remaining = max(0, KR1_GATE_N - len(post))
    next_day = (as_of_date + timedelta(days=1)) if as_of_date else post_start
    gate_by_post = project_gate_date(post_rate, remaining, next_day) if post_rate else None
    gate_by_whole = project_gate_date(whole_rate, remaining, next_day) if whole_rate else None
    candidates = [d for d in (gate_by_post, gate_by_whole) if d is not None]
    return {
        "windows": {
            "whole": window_stats(whole, f"整窗 ≥{COHORT_START}"),
            "pre": window_stats(pre, f"pre {COHORT_START}→{post_start} 前 ({MODEL_PRE})"),
            "post": window_stats(post, f"post ≥{post_start} ({MODEL_POST})"),
        },
        "post_projection": {
            "as_of": str(as_of_date) if as_of_date else None,
            "post_trading_days_elapsed": len(post_days),
            "whole_trading_days_elapsed": len(whole_days),
            "post_daily_rate": round(post_rate, 4) if post_rate else None,
            "whole_daily_rate": round(whole_rate, 4) if whole_rate else None,
            "post_n_needed_for_gate": remaining,
            "gate_date_by_post_rate": str(gate_by_post) if gate_by_post else None,
            "gate_date_by_whole_rate": str(gate_by_whole) if gate_by_whole else None,
            "gate_date_range": ([str(min(candidates)), str(max(candidates))]
                                if len(candidates) == 2 else
                                [str(candidates[0]), str(candidates[0])] if candidates else None),
            # 保守口径: 取区间**较晚**的一端作规划用日 (宁可晚判, 不可早判成功效不足)
            "gate_date_planning": str(max(candidates)) if candidates else None,
            "note": ("外推含感恩节 11-26 休市与 11-27 半日市; 11-28 是周六 ⇒ §6 原判日"
                     "本身不是交易日 (§12.2)。post 速率在切换初期分母仅 1~2 天且当日持仓"
                     "未结算 ⇒ 偏低, 故必须与整窗速率并列报区间, 不得单取一个。"),
        },
        "kr5_disclosure_complete": True,
    }


def collect_and_score(rows_a9: Sequence[Dict[str, Any]],
                      rows_no_a9: Optional[Sequence[Dict[str, Any]]] = None,
                      rows_no_a10: Optional[Sequence[Dict[str, Any]]] = None,
                      as_of: Any = None,
                      era_where: str = "") -> Dict[str, Any]:
    """汇总判分。rows_a9 = 主口径 (A9 干净 + A10 谓词); 另两个用于双口径并列 (A9/A10 律)。"""
    da = [r for r in rows_a9 if classify_source(r.get("decision_id")) == PRIMARY_SOURCE]
    by_source: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows_a9:
        by_source[classify_source(row.get("decision_id"))].append(row)

    warnings: List[str] = []
    if rows_no_a9 is None or rows_no_a10 is None:
        warnings.append("KR5: A9/A10 双口径未并列 (缺一即 WARNING)")
    if not era_where:
        warnings.append("KR5: A10 谓词 era_clean_where() 返回空 ⇒ 未确认现役时代口径")

    primary = window_stats(da, f"KR1 主口径 · {PRIMARY_SOURCE} · ≥{COHORT_START}")
    if primary.get("fee_missing_n"):
        warnings.append(f"KR5: DA fees_est 缺失 {primary['fee_missing_n']} 笔 ⇒ 费用不可测, 不以 0 冒充")
    if primary.get("net_missing_n"):
        warnings.append(f"KR5: DA 净额缺失 {primary['net_missing_n']} 笔 ⇒ KR1 不可测, 不以毛收益冒充")
    disclosure = three_window_disclosure(da, as_of)
    post_stats = disclosure["windows"]["post"]
    post_gate = kr1_verdict(post_stats)
    post_gate_met = (post_stats["n"] or 0) >= KR1_GATE_N
    post_net_bps = post_stats.get("net_bps")
    post_ci_contains_zero = post_stats.get("ci80_contains_zero")
    post_pivot_triggered = bool(
        post_gate_met
        and post_net_bps is not None and post_net_bps <= 0
        and post_ci_contains_zero is True
    )

    projection = disclosure["post_projection"]
    pivot_date = (projection.get("gate_date_planning")
                  if post_stats["n"] < KR1_GATE_N else None)
    return {
        "schema": "okr_2026_11_scoring_v1",
        "okr_doc": OKR_DOC,
        "as_of": str(_as_date(as_of)) if _as_date(as_of) else None,
        "caliber": {
            "a9_exclusion_filter": True,
            "a10_era_where": era_where or None,
            "a9_excluded_n": (len(rows_no_a9) - len(rows_a9)) if rows_no_a9 is not None else None,
            "a10_excluded_n": (len(rows_no_a10) - len(rows_a9)) if rows_no_a10 is not None else None,
            "source_type": "PLAYBOOK_SIM", "exit_price_gt_0": True,
            "sample_scope": ("当前数据库已平行; cohort 仅设 trade_date 下界, as_of 仅供评估/外推, "
                             "不设 trade_date 上界, 不是历史快照; 新已平行进入重跑"),
            "net_bps_estimator": "10000 × Σ净额 / Σ入场名义额 (名义加权)",
            "day_cluster_estimator": ("每日净额/每日名义额后日等权; CI80/MDE 描述日等权均值, "
                                      "不是名义加权净 bps 的严格区间"),
            "fee_coverage": ("fees_est 为佣金/监管费估算; 借券/资金/数据/模型运营成本未统一计入, "
                             "成交价已含滑点不得重复扣"),
        },
        "kr1": {
            "title": "KR1 主 KR: DA 主通道净 bps/笔 > 0",
            "primary": primary,
            "verdict": kr1_verdict(primary),
            "by_source": {k: window_stats(v, k) for k, v in sorted(by_source.items())},
            "unit": "bps (1bp=0.01%); 金额 USD; 名义额=entry_price×quantity",
            "source": OKR_DOC + " §4 KR1 / §2.1 谓词 / §12.3",
        },
        "kr2": {
            "title": "KR2: DA 死单率 ≤23.0% (A11 口径)",
            **kr2_dead_rate(da),
            "source": OKR_DOC + " §4 KR2; 判据字典 A11",
        },
        "section12": {
            "disclosure": disclosure,
            "pivot_ruling": PIVOT_RULE,
            "pivot_evaluation": {
                "population": "post",
                "post_n": post_stats["n"],
                "post_gate_met": post_gate_met,
                "post_verdict": post_gate,
                "net_bps": post_net_bps,
                "ci_contains_zero": post_ci_contains_zero,
                "triggered": post_pivot_triggered,
                "deferred_to": pivot_date,
                "note": ("§12.5: post n<120 ⇒ 判「功效不足」, 本次不触发 Kill/Pivot, "
                         "判日顺延至 post n≥120 或 12 月开跑会取先到者; §6 判据文字与 "
                         "n≥120 样本门一字未动" if not post_gate_met else
                         "post n≥120, 净 bps≤0 且日等权 CI 含 0 ⇒ 按 §6 呈裁 Kill/Pivot; "
                         "本报告不自动停单" if post_pivot_triggered else
                         "post n≥120, 但未同时满足净 bps≤0 且日等权 CI 含 0 "
                         "(含 CI 不可算) ⇒ 本次不触发 Kill/Pivot; 只报实测, 不自动停单"),
            },
            "settlement": {
                "date": SETTLEMENT_DATE,
                "deferred": False,
                "note": "§12.6: §5 (11-30 结算) **未被 §12.4 裁决顺延**, agent 不得类推",
            },
        },
        "structure": {
            "da_exit_reason_breakdown": exit_reason_breakdown(da),
            "note": "只报事实; 尾部族的入场/出场侧修法已被 C11/C12/C14/C15/C16/C17 终局证伪",
        },
        "warnings": warnings,
    }


def render_lines(block: Dict[str, Any]) -> List[str]:
    if not isinstance(block, dict) or "kr1" not in block:
        return ["[OKR-2026-11] 判分不可用 (block 缺 kr1)"]
    lines = [f"🎯 OKR-2026-11 (现行, 窗 ≥{COHORT_START}, as_of {block.get('as_of')})"]
    caliber = block.get("caliber", {})
    for key in ("sample_scope", "net_bps_estimator", "day_cluster_estimator", "fee_coverage"):
        if caliber.get(key):
            lines.append(f"  口径: {caliber[key]}")
    primary = block["kr1"]["primary"]
    verdict = block["kr1"]["verdict"]
    lines.append(
        f"  KR1 DA: n={primary['n']} 净 {primary['net_bps']}bps "
        f"(毛 {primary['gross_bps']} / 费 {primary['fee_bps']}) 净${primary['net']} "
        f"日聚类 t={primary['day_clustered_t']}/{primary['n_days']}日 "
        f"MDE80={primary['mde80_bps']} → {verdict['verdict']}")
    lines.append(f"      └ {verdict['note']}")
    kr2 = block["kr2"]
    lines.append(f"  KR2 DA 死单率: {kr2.get('dead_pct')}% vs 门 ≤{KR2_GATE_DEAD_PCT}% "
                 f"({kr2.get('dead_n')}/{kr2.get('n_with_mfe')}) → {kr2.get('verdict')}")
    sec12 = block.get("section12", {})
    windows = sec12.get("disclosure", {}).get("windows", {})
    if windows:
        lines.append("  §12.3 三窗并列 (缺一即 KR5 WARNING):")
        for key in ("whole", "pre", "post"):
            stat = windows.get(key, {})
            lines.append(
                f"      {key:5} n={stat.get('n')} 净 {stat.get('net_bps')}bps "
                f"净${stat.get('net')} t={stat.get('day_clustered_t')} "
                f"MDE80={stat.get('mde80_bps')} CI80=[{stat.get('ci80_lower_bps')}, "
                f"{stat.get('ci80_upper_bps')}] 含0={stat.get('ci80_contains_zero')}"
                + ("  ⚠️ n<30 不可判定" if stat.get("underpowered_n_lt_30") else ""))
    pivot = sec12.get("pivot_evaluation", {})
    if pivot:
        lines.append(f"  §6 Kill/Pivot (方案 B 已裁): 判据人群=post, post n={pivot.get('post_n')}, "
                     f"触发={pivot.get('triggered')}, 顺延至={pivot.get('deferred_to')}")
        lines.append(f"      └ {pivot.get('note')}")
        projection = sec12.get("disclosure", {}).get("post_projection", {})
        if projection:
            lines.append(
                f"      └ 达标日双速率外推: post 速率 {projection.get('post_daily_rate')} 笔/日"
                f" (已实现 {projection.get('post_trading_days_elapsed')} 日) → "
                f"{projection.get('gate_date_by_post_rate')} | 整窗速率 "
                f"{projection.get('whole_daily_rate')} 笔/日 → "
                f"{projection.get('gate_date_by_whole_rate')} | 区间 "
                f"{projection.get('gate_date_range')} ⇒ 规划用 "
                f"{projection.get('gate_date_planning')} (取较晚端, 宁晚判不早判)")
    settlement = sec12.get("settlement", {})
    if settlement:
        lines.append(f"  §5 结算日 {settlement.get('date')} 顺延={settlement.get('deferred')} "
                     f"— {settlement.get('note')}")
    for warning in block.get("warnings", []):
        lines.append(f"  ⚠️ {warning}")
    return lines


# ── 取数 (只读) ─────────────────────────────────────────────────────────────

BASE_SELECT = """SELECT trade_date, symbol, decision_id, entry_price, quantity,
                        realized_pnl, pnl_net, fees_est, mfe_pct, exit_reason
                 FROM trade_records
                 WHERE source_type = 'PLAYBOOK_SIM'
                   AND trade_date >= ?
                   AND exit_price > 0
                   AND COALESCE(pnl_net, realized_pnl) IS NOT NULL """
A9_CLAUSE = "AND COALESCE(json_extract(metadata_json,'$.exclusion_reason'),'') = '' "


def fetch_rows(db_path: Path, cohort_start: str = COHORT_START, a9: bool = True,
               a10: bool = True, era_where: str = "") -> List[Dict[str, Any]]:
    """只读连接 (mode=ro) 取数; 读失败上抛由 CLI fail-close, 不以空列表冒充 0 笔。"""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        query = BASE_SELECT + (A9_CLAUSE if a9 else "") + (era_where if a10 else "")
        return [dict(row) for row in conn.execute(query, (cohort_start,)).fetchall()]
    finally:
        conn.close()


def resolve_era_where(db_path: Path) -> str:
    """A10 SSOT 谓词 = `app.core.db_utils.era_clean_where(conn)` (缺列 fail-open 返回空串)。"""
    try:
        from app.core.db_utils import era_clean_where
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        try:
            return era_clean_where(conn) or ""
        finally:
            conn.close()
    except Exception:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default=str(ROOT / "data" / "ftseek_us.db"))
    parser.add_argument("--as-of", default=None,
                        help="评估/外推基准日 YYYY-MM-DD (默认今天; 不限制已平取数上界)")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    parser.add_argument("--out", default=None, help="显式写 JSON 报告路径 (默认不写)")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[okr-2026-11] DB 不存在: {db_path}")
        return 2
    try:
        era_where = resolve_era_where(db_path)
        rows_a9 = fetch_rows(db_path, a9=True, a10=True, era_where=era_where)
        rows_no_a9 = fetch_rows(db_path, a9=False, a10=True, era_where=era_where)
        rows_no_a10 = fetch_rows(db_path, a9=True, a10=False, era_where="")
    except Exception as error:
        print(f"[okr-2026-11] 取数失败 (fail-close, 不以 0 笔冒充): "
              f"{type(error).__name__}: {error}")
        return 2

    as_of = args.as_of or str(date.today())
    block = collect_and_score(rows_a9, rows_no_a9, rows_no_a10, as_of=as_of,
                              era_where=era_where)
    if args.json:
        print(json.dumps(block, ensure_ascii=False, indent=1, default=str))
    else:
        for line in render_lines(block):
            print(line)
    if args.out:
        out_path = Path(args.out)
        if out_path.exists():
            print(f"[okr-2026-11] 拒绝覆盖已存在的 {out_path} (改 --out 或先删除)")
            return 2
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(block, ensure_ascii=False, indent=1, default=str),
                            encoding="utf-8")
        print(f"[okr-2026-11] 已写 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
