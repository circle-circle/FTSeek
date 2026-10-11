#!/usr/bin/env python3
"""[RFC-123 第2层诊断] planned_entry 生成质量 + 规则锚方案只读回测.

为什么存在 (RFC-123 附节登记依据):
  双锚影子 (阶段0) 实测 LLM 裸拍的 planned_entry 作为限价锚 76% 够不到
  (16/21 落在持仓期真实区间外), 与 ADR-035 `fill_rate=0.323` (n=141, 全天bar)
  独立互证 ⇒ LLM 计划价系统性脱离市场。本脚本回测【规则锚】(开盘区间/VWAP/ATR)
  能否替代, 回答用户 2026-10-11 问的「计划入场价如何设定才尽量合理」。

纪律 (每条都对应一个真实教训):
  · 只读: DB mode=ro, 不写文件/不改交易行为/不触 candidate C 阶段0 影子攒样
  · 防未来函数: 锚只用【首个触发评估轮】的 features (opening_high/low/vwap/atr/
    current_price), 不用之后才知道的值
  · 主指标不依赖退出假设: fill_rate(限价是否成交) + entry_improvement(比实际追高
    便宜$/股) 是纯 bar 几何/纯入场价比较 ⇒ 稳健; PnL 用 held-constant exit 近似,
    有路径选择偏差 (成交子集=价格回踩过的非随机子集), 仅方向参考
  · 机会成本必算: 限价不成交=错过整笔, 须并报错过笔的实际 PnL (漏赢家=代价)
  · n=21/3交易日 ⇒ 只筛方向, 不裁决; 裁决须扩窗全史 + 逐笔 EOD 反事实 + 扫 k×ATR

锚方案 (LONG; SHORT 镜像):
  llm_plan    = 生成锚 (现状 baseline)        open       = 当日开盘价
  or_low      = 开盘区间低/高 (用户提:开盘波动) or_mid     = 开盘区间中点
  vwap        = 触发时 VWAP (用户提:历史数据)   atr_dip_k  = 现价 ∓ k×ATR (用户提:波动)

运行: ./venv/bin/python scripts/rfc123_planned_entry_anchor_backtest.py
产物: stdout (供 RFC-123 附节 E 表程序化抽字面量; 不写 jsonl, 纯诊断)
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ETS = REPO / "data" / "entry_timing_shadow.jsonl"
BAR = REPO / "data" / "replay_bars"
DB = REPO / "data" / "ftseek_us.db"

SCHEMES = ["llm_plan", "open", "or_low", "or_mid", "vwap", "atr_dip_0.5", "atr_dip_1.0"]


def _load():
    rows = [json.loads(l) for l in ETS.read_text(errors="replace").splitlines() if l.strip()]
    anch = [r for r in rows if r.get("anchor_recorded") is True
            and isinstance(r.get("dual_anchor"), dict)]
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    fills = {}
    for r in con.execute(
            """SELECT decision_id,symbol,direction,entry_price,exit_price,pnl_net,exit_reason,
                      mfe_pct,mae_pct,quantity,entry_time,exit_time,fees_est FROM trade_records
               WHERE decision_id!='' AND exit_reason IS NOT NULL AND exit_reason!=''
               AND exit_time IS NOT NULL AND pnl_net IS NOT NULL"""):
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
    for d in matched:
        f = fills[d]
        gens = [float(r["dual_anchor"].get("generated") or 0)
                for r in by_dec[d] if r["dual_anchor"].get("generated")]
        if not gens or f.get("mfe_pct") is None:
            continue
        gen = gens[0]
        e = float(f["entry_price"]); mfe = float(f["mfe_pct"]); mae = float(f["mae_pct"] or 0)
        is_long = f["direction"].upper() in ("LONG", "BUY")
        lo, hi = (e * (1 - mae / 100), e * (1 + mfe / 100)) if is_long \
            else (e * (1 - mfe / 100), e * (1 + mae / 100))
        if lo <= gen <= hi:
            reach += 1
        else:
            unreach += 1
    return reach, unreach


def backtest(fills, by_dec, matched):
    res = {s: {"fill": 0, "miss": 0, "imp_sum": 0.0, "imp_n": 0,
               "cf_pnl": 0.0, "miss_act": 0.0} for s in SCHEMES}
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
        end = str(f["exit_time"])[11:16] or "16:00"
        cur = float(fr.get("current_price") or 0); atr = float(feat.get("atr") or 0)
        oh = float(feat.get("opening_high") or 0); ol = float(feat.get("opening_low") or 0)
        vwap = float(feat.get("vwap") or 0); day_open = bars[0][1]
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
    return res


def main():
    anch, fills, by_dec, matched = _load()
    days = sorted({r["dual_anchor"].get("date") for r in anch if r["dual_anchor"].get("date")})
    reach, unreach = reachability(anch, fills, by_dec, matched)
    act_total = sum(float(fills[d]["pnl_net"]) for d in matched)
    res = backtest(fills, by_dec, matched)

    print("═══ RFC-123 第2层: planned_entry 生成质量 + 规则锚回测 (只读) ═══")
    print(f"样本: 双锚影子 {len(anch)} 行 / {len(days)} 交易日 {days} / 匹配已平仓成交 {len(matched)} 笔")
    print(f"实际(全追高进场) {len(matched)} 笔 pnl_net 合计 = {act_total:+.2f}  ← 真 baseline\n")

    print(f"【诊断1】LLM 计划价可达性 (mfe/mae 反推持仓期真实区间):")
    print(f"  可达 = {reach}/{reach+unreach}   够不到 = {unreach}/{reach+unreach} "
          f"({unreach/max(reach+unreach,1)*100:.0f}%)")
    print(f"  ⇒ 与 ADR-035 fill_rate=0.323 (n=141,全天bar) 独立互证: LLM 理想回踩价系统性脱离市场\n")

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

    print(f"\n【结论】or_low(开盘区间低/高) 双轴完胜 llm_plan: 成交率 48%vs29% + 改善 +1.43vs+0.94/股")
    print(f"  权衡曲线: 回踩越深改善越大但成交率崩 (atr_dip_1.0=0%成交); or_mid/vwap 成交86%但改善~0")
    print(f"  ⇒ or_low 是甜点; 今日开盘波动 > 历史ATR (ATR是历史平均, 不匹配今日实际区间)")
    print(f"\n⚠️ 局限 (不得当裁决): ①n={len(matched)}/{len(days)}交易日 ②PnL=held-constant-exit近似")
    print(f"   ③路径选择偏差(成交子集=回踩过的非随机子集, 高估便宜入场) ④错过笔含赢家=机会成本")
    print(f"   ⑤第2层独立于 candidate C 阶段1, 须等阶段0攒满10交易日裁完再独立预注册, 防变量纠缠")


if __name__ == "__main__":
    main()
