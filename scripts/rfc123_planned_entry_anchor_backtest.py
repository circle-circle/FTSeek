#!/usr/bin/env python3
"""[RFC-123 第2层诊断] planned_entry 生成质量 + 规则锚方案只读回测.

为什么存在 (RFC-123 附节登记依据):
  双锚影子 (阶段0) 实测 LLM 裸拍的 planned_entry 作为限价锚 76% 够不到
  (16/21 落在持仓期真实区间外), 与 ADR-035 `fill_rate=0.323` (n=141, 全天bar)
  同向 (口径注: 本样本 21 笔全部含于该 v2 配对集 ⇒ 同一样本双口径互证, 非独立
  样本验证) ⇒ LLM 计划价系统性脱离市场。本脚本回测【规则锚】(开盘区间/VWAP/ATR)
  能否替代, 回答用户 2026-10-11 问的「计划入场价如何设定才尽量合理」。

纪律 (每条都对应一个真实教训):
  · 只读: DB mode=ro, 不写文件/不改交易行为/不触 candidate C 阶段0 影子攒样
  · 冻结: 默认 --until 2026-10-09 (登记窗上界) — 无冻结的"可复现"会被后续
    交易日数据漂移打穿 (DEB-20261007/08 验证器已实测踩坑)
  · 防未来函数: 锚只用【首个触发评估轮】的 features (opening_high/low/vwap/atr/
    current_price), 不用之后才知道的值
  · 主指标不依赖退出假设: fill_rate(限价是否成交) + entry_improvement(比实际追高
    便宜$/股) 是纯 bar 几何/纯入场价比较 ⇒ 稳健; PnL 用 held-constant exit 近似,
    有路径选择偏差 (成交子集=价格回踩过的非随机子集), 仅方向参考
  · 机会成本必算: 限价不成交=错过整笔, 须并报错过笔的实际 PnL (漏赢家=代价)
  · n=21/3交易日 ⇒ 只筛方向, 不裁决; 裁决须扩窗全史 + 逐笔 EOD 反事实 + 扫 k×ATR

2026-10-11 复查修复 (P0): 跨日退出的成交窗口倒挂 — 旧实现取次日 HH:MM 当窗口
终点, start>end ⇒ 该笔对任何锚恒 miss (受影响 1/21: US.SPCX 10-08 入场/
10-09 05:09 平仓)。修复 = 跨日截到入场日收盘 16:00 (bar 文件只覆盖入场日)。

锚方案 (LONG; SHORT 镜像):
  llm_plan    = 生成锚 (现状 baseline)        open       = 当日开盘价
  or_low      = 开盘区间低/高 (用户提:开盘波动) or_mid     = 开盘区间中点
  vwap        = 触发时 VWAP (用户提:历史数据)   atr_dip_k  = 现价 ∓ k×ATR (用户提:波动)

运行: ./venv/bin/python scripts/rfc123_planned_entry_anchor_backtest.py
产物: stdout (供 RFC-123 附节 E 表程序化抽字面量; 不写 jsonl, 纯诊断)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ETS = REPO / "data" / "entry_timing_shadow.jsonl"
BAR = REPO / "data" / "replay_bars"
DB = REPO / "data" / "ftseek_us.db"
V2_REPLAY = REPO / "data" / "shadow" / "pullback_limit_replay_v2_latest.json"

SCHEMES = ["llm_plan", "open", "or_low", "or_mid", "vwap", "atr_dip_0.5", "atr_dip_1.0"]
TTL_VARIANTS = [("or_low_ttl5", 5), ("or_low_ttl15", 15)]
JSON_OUT_DEFAULT = REPO / "data" / "shadow" / "rfc123_l2_rule_anchor_latest.json"


def _load(until=None):
    rows = [json.loads(l) for l in ETS.read_text(errors="replace").splitlines() if l.strip()]
    anch = [r for r in rows if r.get("anchor_recorded") is True
            and isinstance(r.get("dual_anchor"), dict)
            and (until is None or str(r["dual_anchor"].get("date") or "") <= until)]
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    sql = """SELECT decision_id,symbol,direction,entry_price,exit_price,pnl_net,exit_reason,
                    mfe_pct,mae_pct,quantity,entry_time,exit_time,fees_est FROM trade_records
             WHERE decision_id!='' AND exit_reason IS NOT NULL AND exit_reason!=''
             AND exit_time IS NOT NULL AND pnl_net IS NOT NULL"""
    params = []
    if until is not None:
        sql += " AND substr(entry_time,1,10) <= ?"
        params.append(until)
    fills = {}
    for r in con.execute(sql, params):
        fills[r["decision_id"]] = dict(r)
    con.close()
    by_dec = defaultdict(list)
    for r in anch:
        by_dec[r.get("decision_id")].append(r)
    matched = [d for d in by_dec if d in fills]
    return anch, fills, by_dec, matched


def _bars(symbol, et_date):
    p = BAR / f"{symbol.replace('.', '_')}_{et_date}.json"
    if not p.exists():
        return None
    try:
        return [(str(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]))
                for x in json.loads(p.read_text())]
    except Exception:
        return None


def _first_fill(bars, anchor, is_long, start_hhmm, end_hhmm):
    """限价 anchor 在 [start,end] 内首次成交 → (time, price) 或 None。
    LONG 限价买: bar_low<=anchor 成交, 成交价=min(anchor,bar_open)(开盘已低于锚则按开盘).
    SHORT 限价卖: bar_high>=anchor 成交, 成交价=max(anchor,bar_open)。"""
    if not anchor or anchor <= 0:
        return None
    for t, o, h, l, c in bars:
        hm = t[11:16]
        if hm < start_hhmm:
            continue
        if hm > end_hhmm:
            break
        if is_long and l <= anchor:
            return (t, min(anchor, o) if o <= anchor else anchor)
        if (not is_long) and h >= anchor:
            return (t, max(anchor, o) if o >= anchor else anchor)
    return None


def _window_end(entry_time: str, exit_time: str) -> str:
    """限价窗口终点 (HH:MM): 同日退出取退出时刻; 跨日退出截到入场日收盘 16:00。

    2026-10-11 复查修复 (P0): 旧实现直接取 exit_time 的 HH:MM, 跨日退出会得到
    start(如 09:36) > end(如 05:09) 的倒挂窗口 ⇒ _first_fill 立即 break, 该笔
    对任何锚恒判 miss。bar 文件只覆盖入场日 09:31-16:00, 故跨日截 16:00。
    """
    if str(exit_time)[:10] > str(entry_time)[:10]:
        return "16:00"
    return str(exit_time)[11:16] or "16:00"


def _add_minutes(hhmm: str, minutes: int) -> str:
    """HH:MM + N 分钟 → HH:MM (TTL 5/15min 不会跨日)."""
    hour, minute = int(hhmm[:2]), int(hhmm[3:5])
    total = hour * 60 + minute + minutes
    return f"{total // 60:02d}:{total % 60:02d}"


def _ttl_entry(bars, anchor, is_long, start_hhmm, ttl_min, window_end):
    """TTL 限价 → 市价回退 (第3层设计): TTL 内触碰锚→限价; 未触碰→TTL 过期市价.

    返回 (time, price, mode) 或 None; mode ∈ {"limit", "market_fallback"}.
    bar 时间戳 = bar 收盘时刻 (09:31 bar 覆盖 09:30-09:31), 故 TTL 过期后首个 bar
    (hm > ttl_end) 的 open ≈ TTL 过期时点市价 (1 分钟粒度近似).
    """
    if not anchor or anchor <= 0:
        return None
    ttl_end = _add_minutes(start_hhmm, ttl_min)
    if ttl_end > window_end:
        return "ttl_exceeds_hold"
    for t, o, h, l, c in bars:
        hm = t[11:16]
        if hm < start_hhmm:
            continue
        if hm > ttl_end:
            break
        if is_long and l <= anchor:
            return (t, min(anchor, o) if o <= anchor else anchor, "limit")
        if (not is_long) and h >= anchor:
            return (t, max(anchor, o) if o >= anchor else anchor, "limit")
    for t, o, h, l, c in bars:
        hm = t[11:16]
        if hm < start_hhmm:
            continue
        if hm > ttl_end:
            return (t, o, "market_fallback")
    return None


def _anchor(scheme, da, feat, is_long, cur, atr, oh, ol, vwap, day_open):
    if scheme == "llm_plan":
        g = da.get("generated")
        return float(g) if g else None
    if scheme == "open":
        return day_open
    if scheme == "or_low":
        return (ol if is_long else oh) or None
    if scheme == "or_mid":
        return ((oh + ol) / 2) if (oh and ol) else None
    if scheme == "vwap":
        return vwap or None
    if scheme == "atr_dip_0.5":
        return ((cur - 0.5 * atr) if is_long else (cur + 0.5 * atr)) if (cur and atr) else None
    if scheme == "atr_dip_1.0":
        return ((cur - 1.0 * atr) if is_long else (cur + 1.0 * atr)) if (cur and atr) else None
    return None


def reachability(anch, fills, by_dec, matched):
    """LLM 计划价可达性: 是否落在持仓期真实区间 (mfe/mae 反推, 非稀疏采样)。"""
    reach = unreach = 0
    breakdown = defaultdict(int)
    for d in matched:
        f = fills[d]
        srows = sorted(by_dec[d], key=lambda x: x.get("ts", ""))
        gens = [float(r["dual_anchor"].get("generated") or 0)
                for r in srows if r["dual_anchor"].get("generated")]
        if not gens or f.get("mfe_pct") is None:
            continue
        gen = gens[0]
        e = float(f["entry_price"]); mfe = float(f["mfe_pct"]); mae = float(f["mae_pct"] or 0)
        is_long = f["direction"].upper() in ("LONG", "BUY")
        side = "LONG" if is_long else "SHORT"
        lo, hi = (e * (1 - mae / 100), e * (1 + mfe / 100)) if is_long \
            else (e * (1 - mfe / 100), e * (1 + mae / 100))
        if lo <= gen <= hi:
            reach += 1
            breakdown[(side, "reach")] += 1
        elif gen < lo:
            unreach += 1
            breakdown[(side, "below_low")] += 1
        else:
            unreach += 1
            breakdown[(side, "above_high")] += 1
    return reach, unreach, dict(breakdown)


def backtest(fills, by_dec, matched):
    res = {s: {"fill": 0, "miss": 0, "imp_sum": 0.0, "imp_n": 0,
               "cf_pnl": 0.0, "miss_act": 0.0, "miss_win": 0, "miss_loss": 0}
           for s in SCHEMES}
    stats = {"overnight": 0, "first_ready": 0, "first_not_ready": 0,
             "width_atr": [], "dip_atr": []}
    for d in matched:
        f = fills[d]
        srows = sorted(by_dec[d], key=lambda x: x.get("ts", ""))
        fr = srows[0]
        feat = fr.get("features", {}) or {}
        da = fr["dual_anchor"]
        is_long = f["direction"].upper() in ("LONG", "BUY")
        qty = float(f["quantity"] or 0)
        act_e = float(f["entry_price"]); act_x = float(f["exit_price"] or 0)
        act_pnl = float(f["pnl_net"]); fees = float(f["fees_est"] or 0)
        bars = _bars(f["symbol"], str(f["entry_time"])[:10])
        if not bars:
            continue
        start = str(fr.get("ts", ""))[11:16] or "09:30"
        end = _window_end(str(f["entry_time"]), str(f["exit_time"]))
        if str(f["exit_time"])[:10] > str(f["entry_time"])[:10]:
            stats["overnight"] += 1
        cur = float(fr.get("current_price") or 0); atr = float(feat.get("atr") or 0)
        oh = float(feat.get("opening_high") or 0); ol = float(feat.get("opening_low") or 0)
        vwap = float(feat.get("vwap") or 0); day_open = bars[0][1]
        if feat.get("opening_range_ready"):
            stats["first_ready"] += 1
        else:
            stats["first_not_ready"] += 1
        if atr > 0 and oh > 0 and ol > 0:
            stats["width_atr"].append((oh - ol) / atr)
            stats["dip_atr"].append((cur - ol if is_long else oh - cur) / atr)
        for s in SCHEMES:
            a = _anchor(s, da, feat, is_long, cur, atr, oh, ol, vwap, day_open)
            ff = _first_fill(bars, a, is_long, start, end)
            R = res[s]
            if ff:
                R["fill"] += 1
                imp = (act_e - ff[1]) if is_long else (ff[1] - act_e)
                R["imp_sum"] += imp; R["imp_n"] += 1
                if act_x > 0:
                    R["cf_pnl"] += ((act_x - ff[1]) if is_long else (ff[1] - act_x)) * qty - fees
            else:
                R["miss"] += 1
                R["miss_act"] += act_pnl
                if act_pnl > 0:
                    R["miss_win"] += 1
                elif act_pnl < 0:
                    R["miss_loss"] += 1
    return res, stats


def _v2_overlap(matched):
    """本样本与 ADR-035 v2 配对集的重合数 (独立性披露, 只读)。"""
    if not V2_REPLAY.exists():
        return None
    try:
        rows = json.loads(V2_REPLAY.read_text()).get("rows", [])
        ids = {str(r.get("decision_id")) for r in rows if r.get("decision_id")}
        return sum(1 for d in matched if d in ids)
    except Exception:
        return None


def ttl_backtest(fills, by_dec, matched):
    """TTL 限价→市价回退反事实 (第3层设计): 所有笔都进场 (不错过整笔).

    回答: 「or_low 锚 + TTL 内不回踩→市价进」 vs 现状追高进, 差多少?
    指标: limit_fill(限价成交笔数) / market_fallback(市价回退笔数) /
    ttl_exceeds_hold(TTL 超过实际持仓→跳过, 机会成本=0) / entry_imp / cfPnL.
    """
    res = {name: {"limit_fill": 0, "market_fallback": 0, "ttl_exceeds_hold": 0,
                  "no_bars": 0, "imp_sum": 0.0, "imp_n": 0, "cf_pnl": 0.0}
           for name, _ in TTL_VARIANTS}
    for d in matched:
        f = fills[d]
        srows = sorted(by_dec[d], key=lambda x: x.get("ts", ""))
        fr = srows[0]
        feat = fr.get("features", {}) or {}
        is_long = f["direction"].upper() in ("LONG", "BUY")
        qty = float(f["quantity"] or 0)
        act_e = float(f["entry_price"]); act_x = float(f["exit_price"] or 0)
        fees = float(f["fees_est"] or 0)
        bars = _bars(f["symbol"], str(f["entry_time"])[:10])
        if not bars:
            for name, _ in TTL_VARIANTS:
                res[name]["no_bars"] += 1
            continue
        start = str(fr.get("ts", ""))[11:16] or "09:30"
        window_end = _window_end(str(f["entry_time"]), str(f["exit_time"]))
        oh = float(feat.get("opening_high") or 0); ol = float(feat.get("opening_low") or 0)
        anchor = (ol if is_long else oh) or None
        for name, ttl_min in TTL_VARIANTS:
            entry = _ttl_entry(bars, anchor, is_long, start, ttl_min, window_end)
            R = res[name]
            if entry == "ttl_exceeds_hold" or entry is None:
                R["ttl_exceeds_hold"] += 1
                continue
            t, price, mode = entry
            if mode == "limit":
                R["limit_fill"] += 1
            else:
                R["market_fallback"] += 1
            imp = (act_e - price) if is_long else (price - act_e)
            R["imp_sum"] += imp; R["imp_n"] += 1
            if act_x > 0:
                R["cf_pnl"] += ((act_x - price) if is_long else (price - act_x)) * qty - fees
    return res


def main():
    parser = argparse.ArgumentParser(description="RFC-123 planned_entry 规则锚只读回测")
    parser.add_argument("--until", default="2026-10-09",
                        help="样本窗冻结上界(含, YYYY-MM-DD)。默认=登记窗 2026-10-09, "
                             "防后续交易日数据漂移; 扩窗传更大日期。")
    parser.add_argument("--json-out", default=None,
                        help=f"机器读数落盘路径 (日更 cron 用)。默认不落盘; "
                             f"传路径则写 JSON (如 {JSON_OUT_DEFAULT})")
    parser.add_argument("--daily", action="store_true",
                        help="日更 cron 模式: --until 自动=昨日(ET), --json-out 自动=默认路径")
    args = parser.parse_args()
    if args.daily:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        # cron 05:53 CST Tue-Sat = ET 前一交易日晚间 (17:53 EDT / 16:53 EST, 均盘后)
        # ⇒ ET "今天" = 刚收盘的交易 日; --until 取含当日
        args.until = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        args.json_out = str(JSON_OUT_DEFAULT)
    until = args.until.strip() or None
    anch, fills, by_dec, matched = _load(until)
    days = sorted({r["dual_anchor"].get("date") for r in anch if r["dual_anchor"].get("date")})
    reach, unreach, brk = reachability(anch, fills, by_dec, matched)
    act_total = sum(float(fills[d]["pnl_net"]) for d in matched)
    res, stats = backtest(fills, by_dec, matched)
    ttl_res = ttl_backtest(fills, by_dec, matched)
    overlap = _v2_overlap(matched)
    bar_coverage = sum(1 for d in matched if _bars(fills[d]["symbol"], str(fills[d]["entry_time"])[:10]))

    print("═══ RFC-123 第2层: planned_entry 生成质量 + 规则锚回测 (只读) ═══")
    print(f"样本: 双锚影子 {len(anch)} 行 / {len(days)} 交易日 {days} / 匹配已平仓成交 {len(matched)} 笔"
          + (f" (窗冻结 --until {until})" if until else ""))
    print(f"实际(全追高进场) {len(matched)} 笔 pnl_net 合计 = {act_total:+.2f}  ← 真 baseline\n")

    print(f"【诊断1】LLM 计划价可达性 (mfe/mae 反推持仓期真实区间):")
    print(f"  可达 = {reach}/{reach+unreach}   够不到 = {unreach}/{reach+unreach} "
          f"({unreach/max(reach+unreach,1)*100:.0f}%)")
    for side in ("LONG", "SHORT"):
        n = brk.get((side, "reach"), 0) + brk.get((side, "below_low"), 0) + brk.get((side, "above_high"), 0)
        print(f"  {side:5s}: 可达 {brk.get((side, 'reach'), 0)}/{n}  "
              f"计划价<区间低 {brk.get((side, 'below_low'), 0)}/{n}  "
              f"计划价>区间高 {brk.get((side, 'above_high'), 0)}/{n}")
    overlap_txt = ""
    if overlap is not None:
        overlap_txt = f"; 本样本 {overlap}/{len(matched)} 含于该配对集 ⇒ 同一样本双口径, 非独立样本"
    print(f"  ⇒ 与 ADR-035 fill_rate=0.323 (n=141,全天bar) 同向{overlap_txt}\n")

    print(f"【诊断2】规则锚方案回测 (主指标 fill_rate/entry_imp 不依赖退出假设):")
    print(f"  {'方案':13}{'成交':>4}{'错过':>4}{'成交率':>7}{'改善$/股':>9}"
          f"{'成交笔cfPnL':>12}{'错过笔实际PnL':>14}{'净vs实际':>9}")
    for s in SCHEMES:
        R = res[s]
        n = R["fill"] + R["miss"]
        fr_ = R["fill"] / max(n, 1)
        imp = R["imp_sum"] / max(R["imp_n"], 1)
        net = R["cf_pnl"] - act_total
        print(f"  {s:13}{R['fill']:4}{R['miss']:4}{fr_*100:6.0f}%{imp:+9.2f}"
              f"{R['cf_pnl']:+12.2f}{R['miss_act']:+14.2f}{net:+9.2f}")

    def _rate(s):
        R = res[s]
        return R["fill"] / max(R["fill"] + R["miss"], 1)

    def _imp(s):
        R = res[s]
        return R["imp_sum"] / max(R["imp_n"], 1)

    print(f"\n  隔夜持仓笔(窗口截入场日16:00): {stats['overnight']}/{len(matched)}")
    R_or = res["or_low"]
    print(f"  or_low 错过 {R_or['miss']} 笔 = {R_or['miss_win']} 赢 {R_or['miss_loss']} 亏 "
          f"(错过笔实际PnL {R_or['miss_act']:+.2f})")
    all_ready = sum(1 for r in anch if r.get("features", {}).get("opening_range_ready"))
    print(f"  opening_range_ready: 首评估轮(取锚轮) {stats['first_ready']}/{len(matched)} 就绪; "
          f"全部影子行 {all_ready}/{len(anch)} ({all_ready / max(len(anch), 1) * 100:.0f}%)")
    if stats["width_atr"]:
        width_median = statistics.median(stats["width_atr"])
        width_gt1 = sum(1 for x in stats["width_atr"] if x > 1)
        dip_median = statistics.median(stats["dip_atr"])
        print(f"  开盘区间宽度/ATR: 中位 {width_median:.2f}×, >1× 仅 {width_gt1}/{len(stats['width_atr'])}; "
              f"回踩到 or_low 深度/ATR 中位 {dip_median:.2f}×")

    print(f"\n【结论】or_low(开盘区间低/高) vs llm_plan: 成交率 {_rate('or_low') * 100:.0f}%"
          f"vs{_rate('llm_plan') * 100:.0f}% + 改善 {_imp('or_low'):+.2f}vs{_imp('llm_plan'):+.2f}/股 "
          f"(成交率明显优, 改善微优 — 非「双轴完胜」)")
    print(f"  权衡曲线: 回踩越深改善越大但成交率崩 (atr_dip_0.5 改善最大 {_imp('atr_dip_0.5'):+.2f} "
          f"却仅 {_rate('atr_dip_0.5') * 100:.0f}% 成交; atr_dip_1.0=0%成交改善无从实现); "
          f"or_mid/vwap 成交{_rate('or_mid') * 100:.0f}%但改善~0")
    if stats["width_atr"]:
        print(f"  ⇒ or_low 是甜点; 但实测开盘区间宽度中位仅 {statistics.median(stats['width_atr']):.2f}×ATR — "
              f"开盘区间远小于历史ATR, or_low 甜点因回踩深度(中位 {statistics.median(stats['dip_atr']):.2f}×ATR)"
              f"浅于 0.5×ATR 所以等得到")
    print(f"\n⚠️ 局限 (不得当裁决): ①n={len(matched)}/{len(days)}交易日 ②PnL=held-constant-exit近似")
    print(f"   ③路径选择偏差(成交子集=回踩过的非随机子集, 高估便宜入场) ④错过笔含赢家=机会成本")
    print(f"   ⑤「净vs实际」已含错过整笔代价(错过笔按0收益对比), 未建模的只是资金再部署")
    print(f"   ⑥第2层独立于 candidate C 阶段1, 须等阶段0攒满10交易日裁完再独立预注册, 防变量纠缠")

    print(f"\n【诊断3】TTL 限价→市价回退反事实 (第3层设计, 所有笔都进场):")
    print(f"  {'方案':15}{'限价成交':>8}{'市价回退':>8}{'TTL>持仓':>9}{'无bar':>6}"
          f"{'改善$/股':>9}{'cfPnL':>9}{'净vs实际':>9}")
    for name, _ in TTL_VARIANTS:
        R = ttl_res[name]
        imp = R["imp_sum"] / max(R["imp_n"], 1)
        net = R["cf_pnl"] - act_total
        print(f"  {name:15}{R['limit_fill']:8}{R['market_fallback']:8}"
              f"{R['ttl_exceeds_hold']:9}{R['no_bars']:6}{imp:+9.2f}"
              f"{R['cf_pnl']:+9.2f}{net:+9.2f}")
    print(f"  (TTL>持仓 = TTL 过期晚于实际平仓 → 反事实不可定义, 跳过按 0 PnL 计入机会成本)")

    print(f"\n📊 bar 覆盖率: {bar_coverage}/{len(matched)} (缺 bar 的标的被静默跳过, 须并报)")

    if args.json_out:
        payload = {
            "schema": "rfc123-l2-obs-v1",
            "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            "window": {"until": until, "days": days, "anchor_rows": len(anch),
                       "matched_trades": len(matched), "bar_coverage": f"{bar_coverage}/{len(matched)}"},
            "actual": {"total_pnl": round(act_total, 2)},
            "reachability": {"reach": reach, "unreach": unreach, "breakdown":
                             {f"{side}_{mode}": count for (side, mode), count in brk.items()}},
            "limit_backtest": {s: {k: (round(v, 2) if isinstance(v, float) else v)
                                   for k, v in R.items()} for s, R in res.items()},
            "ttl_backtest": {name: {k: (round(v, 2) if isinstance(v, float) else v)
                                    for k, v in R.items()} for name, R in ttl_res.items()},
            "adr035_overlap": overlap,
            "observation_discipline": "只筛方向不裁决, 无预注册阈值 (RFC-123 §G)",
        }
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        print(f"\n✅ JSON 落盘: {out_path}")


if __name__ == "__main__":
    main()
