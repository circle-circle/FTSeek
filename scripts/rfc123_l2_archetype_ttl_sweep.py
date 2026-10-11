#!/usr/bin/env python3
"""[RFC-123 第2层 · 补充回测] archetype 分条件锚 + TTL 扫参 (只读, 不动他人 WIP backtest).

为什么单独成脚本 (归属纪律):
  scripts/rfc123_planned_entry_anchor_backtest.py 现有另一 agent 未提交 WIP (+286 行),
  直接改会卷入他人并行行 ⇒ 本脚本自包含、只读、独立提交。已内化其 P0 跨日修复
  (窗口终点 = min(exit_time, 入场日16:00), 防 start>end 倒挂恒 miss)。

回答用户 2026-10-11「需要」(建议2): 在 G 节观察轨基础上补两件事 —
  ① archetype 分条件: 现有 or_low(多)/or_high(空) 只对均值回归型 (PULLBACK_LIMIT/
     FADE_SHORT) 正确; BREAKOUT 应锚 or_high(多)/or_low(空) = 追强势/破位, 不是等回踩。
     ⚠️ 实测样本 BREAKOUT 仅 1/21 ⇒ 分条件锚【无法回测】, 只单例示意 + 设计注记。
  ② TTL 扫参 {1,2,3,5,10,15,30}min: 诊断3 已示 TTL5 净+79 / TTL15 净−19 ⇒ 扫全曲线
     找「限价等回踩 vs 过期回退市价」的最优 TTL。

纪律: 只读 (DB mode=ro); 防未来函数 (锚只用首评估轮 features); 主指标 fill_rate/entry_imp
  不依赖退出假设; PnL=held-constant-exit 近似 + 路径选择偏差 ⇒ 只筛方向不裁决;
  n=21/3交易日, 冻结窗 --until 2026-10-09。
运行: ./venv/bin/python scripts/rfc123_l2_archetype_ttl_sweep.py
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict, Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ETS = REPO / "data" / "entry_timing_shadow.jsonl"
BAR = REPO / "data" / "replay_bars"
DB = REPO / "data" / "ftseek_us.db"
UNTIL = "2026-10-09"  # 冻结样本窗 (防数据漂移)
TTLS = [1, 2, 3, 5, 10, 15, 30]  # 分钟


def _load():
    rows = [json.loads(l) for l in ETS.read_text(errors="replace").splitlines() if l.strip()]
    anch = [r for r in rows if r.get("anchor_recorded") is True
            and isinstance(r.get("dual_anchor"), dict)]
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    fills, arch_of = {}, {}
    for r in con.execute(
            """SELECT decision_id,symbol,direction,entry_price,exit_price,pnl_net,exit_reason,
                      quantity,entry_time,exit_time,fees_est FROM trade_records
               WHERE decision_id!='' AND exit_reason IS NOT NULL AND exit_reason!=''
               AND exit_time IS NOT NULL AND pnl_net IS NOT NULL"""):
        fills[r["decision_id"]] = dict(r)
    for d in fills:
        raw = con.execute("SELECT raw_instruction_json FROM playbook_instructions "
                          "WHERE decision_id=? LIMIT 1", (d,)).fetchone()
        arch = None
        if raw and raw[0]:
            rj = json.loads(raw[0]) if isinstance(raw[0], str) else raw[0]
            arch = rj.get("entry_archetype")
        arch_of[d] = arch
    con.close()
    by_dec = defaultdict(list)
    for r in anch:
        by_dec[r.get("decision_id")].append(r)
    matched = [d for d in by_dec if d in fills
               and str(fills[d]["entry_time"])[:10] <= UNTIL]
    return anch, fills, arch_of, by_dec, matched


def _bars(symbol, et_date):
    p = BAR / f"{symbol.replace('.', '_')}_{et_date}.json"
    if not p.exists():
        return None
    try:
        return [(str(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]))
                for x in json.loads(p.read_text())]
    except Exception:
        return None


def _hhmm_to_min(hm):
    return int(hm[:2]) * 60 + int(hm[3:5])


def _limit_fill(bars, anchor, is_long, start_min, end_min):
    """限价 anchor 在 [start,end] 分钟窗内首次成交 → (min_idx, price) 或 None。"""
    if not anchor or anchor <= 0:
        return None
    for i, (t, o, h, l, c) in enumerate(bars):
        m = _hhmm_to_min(t[11:16])
        if m < start_min:
            continue
        if m > end_min:
            break
        if is_long and l <= anchor:
            return (i, min(anchor, o) if o <= anchor else anchor)
        if (not is_long) and h >= anchor:
            return (i, max(anchor, o) if o >= anchor else anchor)
    return None


def _price_at_min(bars, target_min):
    """target_min 时刻(或之后首根)的收盘价 — 市价回退成交价。"""
    for t, o, h, l, c in bars:
        if _hhmm_to_min(t[11:16]) >= target_min:
            return c
    return None


def main():
    anch, fills, arch_of, by_dec, matched = _load()
    act_total = sum(float(fills[d]["pnl_net"]) for d in matched)
    print("═══ RFC-123 第2层补充: archetype 分条件锚 + TTL 扫参 (只读) ═══")
    days = sorted({r["dual_anchor"].get("date") for r in anch if r["dual_anchor"].get("date")})
    print(f"样本: {len(matched)} 笔 / {len(days)} 交易日 {days} (冻结 --until {UNTIL})")
    print(f"实际(全追高) {len(matched)} 笔 pnl_net 合计 = {act_total:+.2f} ← baseline")
    print(f"archetype 分布 = {dict(Counter(arch_of[d] for d in matched))}\n")

    # ── ① archetype 分组: or_low(多)/or_high(空) 方案的分组表现 ──
    print("【① archetype 分条件锚 — 现有 or_low/or_high 方案分组表现】")
    print(f"  {'archetype':16}{'向':6}{'n':>3}{'锚':>8}{'成交':>5}{'成交率':>7}{'改善$/股':>9}  锚是否正确")
    grp = defaultdict(lambda: {"n": 0, "fill": 0, "imp": 0.0, "imp_n": 0})
    for d in matched:
        f = fills[d]
        srows = sorted(by_dec[d], key=lambda x: x.get("ts", ""))
        fr = srows[0]
        feat = fr.get("features", {}) or {}
        is_long = f["direction"].upper() in ("LONG", "BUY")
        oh = float(feat.get("opening_high") or 0)
        ol = float(feat.get("opening_low") or 0)
        anchor = (ol if is_long else oh)  # 现方案: 多=or_low, 空=or_high
        bars = _bars(f["symbol"], str(f["entry_time"])[:10])
        if not bars:
            continue
        start = _hhmm_to_min(str(fr.get("ts", ""))[11:16] or "09:30")
        # 跨日修复: 窗口终点 = min(exit, 入场日16:00)
        ex_min = _hhmm_to_min(str(f["exit_time"])[11:16])
        end = min(ex_min, 16 * 60) if str(f["exit_time"])[:10] != str(f["entry_time"])[:10] else ex_min
        if end < start:
            end = 16 * 60
        g = grp[(arch_of[d], f["direction"])]
        g["n"] += 1
        ff = _limit_fill(bars, anchor, is_long, start, end)
        if ff:
            g["fill"] += 1
            imp = (float(f["entry_price"]) - ff[1]) if is_long else (ff[1] - float(f["entry_price"]))
            g["imp"] += imp
            g["imp_n"] += 1
    for (arch, dirn), g in sorted(grp.items(), key=lambda kv: -kv[1]["n"]):
        anchor_name = "or_low" if dirn in ("LONG", "BUY") else "or_high"
        # 锚正确性: 均值回归型(PULLBACK_LIMIT多/FADE_SHORT空)用 or_low/or_high 正确;
        #           BREAKOUT 应反向 (多=or_high追强势, 空=or_low追破位)
        correct = "✓ 正确(均值回归)" if arch in ("PULLBACK_LIMIT", "FADE_SHORT") else \
            ("✗ 应反向用 " + ("or_high" if dirn in ("LONG", "BUY") else "or_low") if arch == "BREAKOUT" else "?")
        fr_ = g["fill"] / max(g["n"], 1)
        imp = g["imp"] / max(g["imp_n"], 1)
        print(f"  {str(arch):16}{dirn:6}{g['n']:3}{anchor_name:>8}{g['fill']:5}"
              f"{fr_*100:6.0f}%{imp:+9.2f}  {correct}")
    print("  ⇒ 20/21 是均值回归型, or_low/or_high 已正确; BREAKOUT 仅 1 笔(TTI) ⇒ 分条件锚【无法回测】, 见③单例\n")

    # ── ② TTL 扫参 (or_low 锚 + TTL 过期回退市价) ──
    print("【② TTL 扫参 — or_low 限价 + TTLmin 过期回退市价 (所有笔都进场)】")
    print(f"  {'TTL(min)':>9}{'限价成交':>9}{'市价回退':>9}{'TTL>持仓':>9}{'改善$/股':>9}{'cfPnL':>10}{'净vs实际':>9}")
    for ttl in TTLS:
        n_lim = n_mkt = n_skip = 0
        imp_sum = imp_n = 0
        cf = 0.0
        for d in matched:
            f = fills[d]
            srows = sorted(by_dec[d], key=lambda x: x.get("ts", ""))
            fr = srows[0]
            feat = fr.get("features", {}) or {}
            is_long = f["direction"].upper() in ("LONG", "BUY")
            qty = float(f["quantity"] or 0)
            fees = float(f["fees_est"] or 0)
            act_e = float(f["entry_price"])
            act_x = float(f["exit_price"] or 0)
            oh = float(feat.get("opening_high") or 0)
            ol = float(feat.get("opening_low") or 0)
            anchor = (ol if is_long else oh)
            bars = _bars(f["symbol"], str(f["entry_time"])[:10])
            if not bars:
                continue
            start = _hhmm_to_min(str(fr.get("ts", ""))[11:16] or "09:30")
            ex_min = _hhmm_to_min(str(f["exit_time"])[11:16])
            end = min(ex_min, 16 * 60) if str(f["exit_time"])[:10] != str(f["entry_time"])[:10] else ex_min
            if end < start:
                end = 16 * 60
            ttl_deadline = start + ttl
            # TTL 过期晚于实际平仓 ⇒ 反事实不可定义, 跳过 (按0收益计机会成本)
            if ttl_deadline > end:
                n_skip += 1
                continue
            ff = _limit_fill(bars, anchor, is_long, start, min(ttl_deadline, end))
            if ff:
                n_lim += 1
                entry_px = ff[1]
            else:
                n_mkt += 1
                entry_px = _price_at_min(bars, ttl_deadline)  # TTL 过期市价回退
                if entry_px is None:
                    continue
            imp = (act_e - entry_px) if is_long else (entry_px - act_e)
            imp_sum += imp
            imp_n += 1
            if act_x > 0:
                cf += ((act_x - entry_px) if is_long else (entry_px - act_x)) * qty - fees
        imp_avg = imp_sum / max(imp_n, 1)
        print(f"  {ttl:>9}{n_lim:>9}{n_mkt:>9}{n_skip:>9}{imp_avg:+9.2f}{cf:+10.2f}{cf-act_total:+9.2f}")
    print("  ⇒ 找净vs实际最高的 TTL; 诊断3 已示 TTL5(+79) > TTL15(−19), 本扫参补全曲线\n")

    # ── ③ BREAKOUT 单例 (TTI): or_low(错) vs or_high(对) ──
    print("【③ BREAKOUT 单例示意 (n=1, 仅说明问题, 非回测)】")
    for d in matched:
        if arch_of[d] != "BREAKOUT":
            continue
        f = fills[d]
        srows = sorted(by_dec[d], key=lambda x: x.get("ts", ""))
        fr = srows[0]
        feat = fr.get("features", {}) or {}
        is_long = f["direction"].upper() in ("LONG", "BUY")
        oh = float(feat.get("opening_high") or 0)
        ol = float(feat.get("opening_low") or 0)
        bars = _bars(f["symbol"], str(f["entry_time"])[:10])
        start = _hhmm_to_min(str(fr.get("ts", ""))[11:16] or "09:30")
        end = _hhmm_to_min(str(f["exit_time"])[11:16])
        print(f"  {f['symbol']} {f['direction']} BREAKOUT: 开盘区间[{ol:.2f},{oh:.2f}] 实际成交={f['entry_price']:.2f} pnl={f['pnl_net']:+.2f}")
        ff_low = _limit_fill(bars, ol, is_long, start, end) if bars else None
        ff_high = _limit_fill(bars, oh, is_long, start, end) if bars else None
        print(f"    or_low(现方案,错): 成交={'有@'+format(ff_low[1],'.2f') if ff_low else '无(等回踩没等到)'}")
        print(f"    or_high(分条件,对): 成交={'有@'+format(ff_high[1],'.2f') if ff_high else '无'}")
        print(f"    ⇒ BREAKOUT 应追强势(or_high), 现方案让它等回踩(or_low) = 语义错配; 但 n=1 不足裁, 须观察轨攒样")

    print(f"\n⚠️ 局限: n={len(matched)}/{len(days)}天; held-constant-exit; 路径选择偏差; BREAKOUT n=1 不可回测")
    print(f"   ⇒ 只筛方向不裁决; 第2层须等阶段0攒满10交易日(~10-20)+独立预注册")


if __name__ == "__main__":
    main()
