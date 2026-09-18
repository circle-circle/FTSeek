#!/usr/bin/env python3
"""延长时段**候选源回放** (2026-09-17, 用户令「需要」→ 先出数再开新源).

问题: 新源(财报超预期榜 / 自选股)值不值得开? 用户 8/14 偏好 —— 历史可回放就别默认攒样。
能力(实测): `request_history_kline(ktype=K_5M, extended_time=True)` 能取到**历史盘前 bar**
  (旧默认 extended_time=False 只有 RTH ⇒ 0 根盘前, 这是此前"只能前向攒样"的根因)。

口径与限制 (如实声明):
  1. `autype=None` 取**未复权**原始价 —— 否则 qfq 复权会让"昨收"与被测日的价格不同源。
  2. 历史盘前 bar 只到 5m 粒度 ⇒ 入场价用**入场时刻那根 5m bar 的收盘价**近似 (实际是限价单成交价)。
  3. 候选重建: 财报超预期榜只返回**当前**榜单带 released_date ⇒ 只能重建其窗口内的日期 (实测约 90 日);
     自选股是**当前**名单 ⇒ 存在选择性偏差 (当时的名单可能不同)。二者都属"代理回放", 不假装是逐日快照。
  4. 退出规则**直接复用** premarket_exit_shadow.decide() (同一套阈值), 不另写一套。
  5. 不做滑点建模: 薄量盘前的真实滑点未知 (DEB §15 已记为切实盘前必须补的项)。

用法:
  PYTHONPATH=. venv/bin/python3.11 scripts/replay_ext_entry_sources.py --days 10 [--source beat|watchlist|both]
产物: data/reports/replay_ext_entry_sources_latest.json + stdout 汇总表
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.extended_hours_profit_exit_rules import (
    decide_original_profit_exit,
    prioritize_profit_exit,
)

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env.us")
except Exception:
    pass

REPORT = ROOT / "data" / "reports" / "replay_ext_entry_sources_latest.json"
FEE_PCT = 0.085          # 仅作兜底/对照的扁平往返费率 (用户口径: 7.7bps~$4.27/笔$5000 ⇒ ~0.085%)
KLINE_MIN_INTERVAL = 0.5


def _round_trip_fee_pct(entry_price, exit_price, trade_date=None, direction="LONG"):
    """逐笔真实往返费用 % (占**入场名义额**)。

    2026-09-18 修正 (Hermes 复核 §18.2c / §23 R9): 原实现全表用扁平 FEE_PCT=0.085%,
    per-share 费用模型下低价档被低估 1.2–6.2× ($5≈0.53% / $0.072≈9.2%)。
    现与现役链路**共用** extended_hours_market_session.round_trip_fee_metrics
    (内部调 app.execution.fee_model.estimate_round_trip), 数量口径与实盘一致:
    qty = int(POSITION_USD / 入场价)。
    交叉验证: 对 9/17 四笔实弹 (CTNT/GIPR/NBIS/AEHL) 复算 = DB `fees_est`
    451.26 / 100.86 / 4.22 / 10.50 (逐笔一致)。
    """
    try:
        from scripts.extended_hours_market_session import round_trip_fee_metrics
        import scripts.extended_hours_entry_executor as _E
        entry = float(entry_price)
        exit_ = float(exit_price)
        if entry <= 0 or exit_ <= 0:
            return FEE_PCT
        qty = max(1, int(_E.POSITION_USD / entry))
        metrics = round_trip_fee_metrics(qty, entry, exit_, direction, trade_date)
        return float(metrics["round_trip_fee_pct"]) if metrics else FEE_PCT
    except Exception:
        return FEE_PCT
ENTRY_HHMM = 405         # 入场时点 04:05 ET (与 cron 盘前首跑一致)
PREMARKET_END = 925      # 盘前收口 09:25 ET
_last_call = [0.0]
CHUNK_DAYS = 6            # 单次请求跨度 (日历日)
# 2026-09-17 实测修正: 开 extended_time 后每个交易日约 **192** 根 5m (04:00-20:00),
# 而 max_count=1000 ⇒ 单次只能覆盖约 **5.2 个交易日**。原设 9(*2=18 日历日≈12 交易日)
# 会**静默截断**(只回该块最早的 1000 根, 后半段与边界日丢失) — 实测 04-20→04-28 只回到 04-27 07:20。
# 现取 6 日历日(≈4 交易日 ≈ 770 根) 留安全余量。


CACHE_DIR = ROOT / "data" / "replay_cache"


def _shared_profit_exit_decider(entry_price, initial_sl, trade_date=None):
    """Wrap the live shared rule for deterministic bar replay.

    2026-09-18 (R9): fee_pct 不再传扁平常量, 而是**逐 bar 按该 bar 价**用真实
    per-share 费用模型算 (与 premarket_exit_shadow 的
    round_trip_fee_metrics(qty, entry, executable, ...) 同一口径) —— 费用直接决定
    BE 净线高度, 用错费率会让 rule 臂在低价股上系统性提前锁盈。
    """
    state = {"metadata": {}, "peak_price": entry_price}

    def decide(current_price):
        fee_pct = _round_trip_fee_pct(entry_price, current_price, trade_date)
        decision = decide_original_profit_exit(
            "LONG",
            entry_price,
            current_price,
            initial_sl,
            state["peak_price"],
            state["metadata"],
            fee_pct,
            "PLAYBOOK_SIM",
        )
        state["metadata"] = decision["metadata"]
        state["peak_price"] = decision["peak_price"]
        return prioritize_profit_exit(decision["action"])

    return decide


def _shared_profit_exit_full_decider(entry_price, initial_sl, trade_date=None):
    """同 `_shared_profit_exit_decider`, 但返回**整个 decision dict** (需要 effective_sl 等字段)。

    2026-09-19 新增 (INCIDENT 后续/用户令「盈利锁+动态回撤锁」): 现有 rule 臂触发
    SL_TRAIL_LOCK 时把成交价钉在 `_be_net_line_price`(净保本线) ⇒ "锁" 落袋的是 ~0%
    而不是锁定地板。本 decider 供 `trail_floor` 臂读取 `effective_sl` 做对照。
    """
    state = {"metadata": {}, "peak_price": entry_price}

    def decide(current_price):
        fee_pct = _round_trip_fee_pct(entry_price, current_price, trade_date)
        decision = decide_original_profit_exit(
            "LONG",
            entry_price,
            current_price,
            initial_sl,
            state["peak_price"],
            state["metadata"],
            fee_pct,
            "PLAYBOOK_SIM",
        )
        state["metadata"] = decision["metadata"]
        state["peak_price"] = decision["peak_price"]
        return decision

    return decide


def _kline_cached(ctx, code, start, end, ktype):
    """带磁盘缓存的取数 —— 同一窗口换配置重跑时不重复请求 (省接口预算与时间)."""
    key = CACHE_DIR / f"{code.replace('.','_')}_{start}_{end}_{ktype}.json"
    if key.exists():
        try:
            return json.loads(key.read_text())
        except Exception:
            pass
    bars = _kline_chunked(ctx, code, start, end, ktype)
    if bars:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            key.write_text(json.dumps(bars))
        except Exception:
            pass
    return bars


def _kline_chunked(ctx, code, start, end, ktype):
    """长窗口分块取数 (不这么做会静默截断到最近 1000 根 = 约 11 个交易日)."""
    from datetime import datetime as _dt, timedelta as _td
    t0 = _dt.strptime(start, "%Y-%m-%d")
    t1 = _dt.strptime(end, "%Y-%m-%d")
    out, seen = [], set()
    while t0 <= t1:
        chunk_end = min(t0 + _td(days=CHUNK_DAYS * 2), t1)   # *2 覆盖周末
        for b in _kline(ctx, code, t0.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d"), ktype):
            if b["t"] not in seen:
                seen.add(b["t"])
                out.append(b)
        t0 = chunk_end + _td(days=1)
    out.sort(key=lambda b: b["t"])
    return out


def _kline(ctx, code, start, end, ktype):
    """限频的历史K线请求 (富途约 60 次/30 秒预算, 见 DEB §14.6 教训)."""
    from futu import RET_OK
    wait = KLINE_MIN_INTERVAL - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()
    ret, kl, msg = ctx.request_history_kline(code, start=start, end=end, ktype=ktype,
                                             autype=None, max_count=1000, extended_time=True)
    if ret != RET_OK or kl is None or len(kl) == 0:
        return []
    out = []
    for _, r in kl.iterrows():
        out.append({"t": str(r["time_key"]), "o": float(r["open"]), "h": float(r["high"]),
                    "l": float(r["low"]), "c": float(r["close"]), "v": float(r["volume"])})
    return out


def _hhmm(t):
    try:
        return int(t[11:13]) * 100 + int(t[14:16])
    except Exception:
        return -1


def _price_band(price):
    """价格档 (费用分档用): per-share 费用模型下费率随价格单调下降."""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return "unknown"
    if p < 5:
        return "<$5"
    if p < 10:
        return "$5-10"
    if p < 20:
        return "$10-20"
    if p < 50:
        return "$20-50"
    return ">=$50"


def _candidate_days(rank_df, day, recent_days):
    """财报超预期榜 → 该日可见的候选 (released_date ∈ [day-recent_days, day])."""
    lo = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=recent_days)).strftime("%Y-%m-%d")
    out = []
    for _, r in rank_df.iterrows():
        sec = str(r.get("security", ""))
        if not sec.startswith("US."):
            continue
        rd = str(r.get("released_date", ""))
        if lo <= rd <= day:
            try:
                if float(r.get("market_cap", 0) or 0) < 1e9:
                    continue
            except Exception:
                continue
            out.append((sec, rd))
    return out


def _simulate(bars_day, prev_close, entry_bar_i):
    """在当日 bar 序列上模拟: 入场 → 三条退出策略. 返回 dict."""
    entry = bars_day[entry_bar_i]["c"]
    day = bars_day[entry_bar_i]["t"][:10]
    decide = _shared_profit_exit_decider(entry, entry * 0.95, day)
    res = {}
    pre = [b for b in bars_day if _hhmm(b["t"]) < 930]
    rth = [b for b in bars_day if 930 <= _hhmm(b["t"]) <= 1600]
    # A) 我们的规则: 逐 bar 调 decide() (与盘前影子同一套阈值), 触发即平
    act = "hold"
    exit_px, exit_t = None, None
    # 盘前**累计**量 (与影子 decide() 的 pre_volume 口径一致)
    cum, run = {}, 0.0
    for b in bars_day:
        run += float(b["v"])
        cum[b["t"]] = run
    for b in bars_day:
        if b["t"] <= bars_day[entry_bar_i]["t"]:
            continue
        pnl = (b["c"] / entry - 1) * 100
        a = decide(b["c"])
        if a in ("TAKE_PROFIT", "SL_BREAKEVEN", "SL_TRAIL_LOCK"):
            act, exit_px, exit_t = a, b["c"], b["t"]
            break
    res["rule_action"] = act
    res["rule_exit_time"] = exit_t
    # A2) 只用止损、不锁小利 (用户哲学: 留空间, 不被波动扫出): tighten 触发才平, 否则盘前收口平
    ex2 = None
    cum2, run2 = {}, 0.0
    for b in bars_day:
        run2 += float(b["v"])
        cum2[b["t"]] = run2
    for b in bars_day:
        if b["t"] <= bars_day[entry_bar_i]["t"]:
            continue
        if (b["c"] / entry - 1) * 100 <= -1.5:
            ex2 = b["c"]
            break
    res["sl_only_px"] = ex2
    # ── 2026-09-19 新增: 盘前入场仓的「锁」臂 (基线 = pre_close 09:25 收口 = §16.6 A +0.78%) ──
    #  窗口 = 入场后至盘前收口 09:25; 未触发即落到收口价, 故每条臂读作"带上这把锁的全平"。
    _win = [b for b in bars_day
            if b["t"] > bars_day[entry_bar_i]["t"] and _hhmm(b["t"]) <= PREMARKET_END]
    for _k in (2.0, 3.0, 4.0):
        _hit = None
        for b in _win:
            if (b["c"] / entry - 1) * 100 >= _k:
                _hit = b["c"]
                break
        res[f"lock_{int(_k)}pct_px"] = _hit
    for _dd in (1.5, 2.5):
        _peak, _hit = entry, None
        for b in _win:
            _peak = max(_peak, b["c"])
            if _peak > entry and b["c"] <= _peak * (1 - _dd / 100.0):
                _hit = b["c"]
                break
        res[f"dd_{str(_dd).replace('.', 'p')}pct_px"] = _hit
    # A3) 只用止损 + 让盈利跑到当日收盘 (对比"是否该带进盘中")
    ex3 = ex2
    if ex3 is None:
        ex3 = rth[-1]["c"] if rth else None
    res["sl_only_rth_px"] = ex3
    # B) 持有到盘前收口 (09:25)
    e2 = pre[-1]["c"] if pre else None
    # C) 持有到当日 RTH 收盘
    e3 = rth[-1]["c"] if rth else (bars_day[-1]["c"] if bars_day else None)
    # D) 盘中兜底臂: 延用现役 initial_sl(-5%) —— RTH 内触及即按止损价出, 否则持有到收盘
    e4 = None
    sl = entry * 0.95
    for b in rth:
        if b["l"] <= sl:
            e4 = sl
            break
    if e4 is None:
        e4 = e3
    # F) 灾难止损臂 (2026-09-17 用户「需要」): 只在 pnl ≤ -K% 时提前平, 其余仍持到 09:25 收口。
    #   动机: AEHL 实弹反例 (盘前 +126% 跳空票, 单笔 -12%) 说明"无止损"臂的样本尾部 (-6.86%) 不够,
    #   需要一个硬底; 同时保留"去掉 -1.5% 噪声止损"的收益。
    for _k in (4.0, 6.0, 8.0):
        _cp = None
        for b in bars_day:
            if b["t"] <= bars_day[entry_bar_i]["t"]:
                continue
            if _hhmm(b["t"]) > PREMARKET_END:
                break
            if (b["c"] / entry - 1) * 100 <= -_k:
                _cp = b["c"]
                break
        res[f"cat_{int(_k)}pct_px"] = _cp if _cp else e2
    # E) 收口分流: 强平 pnl < K 的仓, 只让 ≥K 的强票进盘中 (用户 9/17「要」)
    for _k in (1.0, 2.0):
        _lp = pre[-1] if pre else None
        if _lp:
            _pnl = (_lp["c"] / entry - 1) * 100
            res[f"keep_{int(_k)}pct_px"] = e3 if _pnl >= _k else _lp["c"]
        else:
            res[f"keep_{int(_k)}pct_px"] = None
    for tag, px in (("rule", exit_px if exit_px else (e2 if e2 else entry)),
                    ("pre_close", e2), ("rth_close", e3), ("intraday_sl", e4),
                    ("sl_only", res.get("sl_only_px") or e2),
                    ("sl_only_rth", res.get("sl_only_rth_px") or e3),
                    ("keep_1pct", res.get("keep_1pct_px") or e2),
                    ("keep_2pct", res.get("keep_2pct_px") or e2),
                    ("cat_4pct", res.get("cat_4pct_px") or e2),
                    ("cat_6pct", res.get("cat_6pct_px") or e2),
                    ("cat_8pct", res.get("cat_8pct_px") or e2),
                    ("lock_2pct", res.get("lock_2pct_px") or e2),
                    ("lock_3pct", res.get("lock_3pct_px") or e2),
                    ("lock_4pct", res.get("lock_4pct_px") or e2),
                    ("dd_1p5pct", res.get("dd_1p5pct_px") or e2),
                    ("dd_2p5pct", res.get("dd_2p5pct_px") or e2)):
        if px:
            g = (px / entry - 1) * 100
            fee = _round_trip_fee_pct(entry, px, day)
            res[tag] = {"ret_pct": round(g, 3), "fee_pct": round(fee, 3),
                        "net_pct": round(g - fee, 3)}
    # MFE (入场后至盘前收口的最高冲高)
    if pre:
        peak = max(b["h"] for b in pre if b["t"] > bars_day[entry_bar_i]["t"]) if len(pre) > entry_bar_i else None
        res["mfe_pct"] = round((peak / entry - 1) * 100, 3) if peak else None
    res["entry_px"] = round(entry, 4)
    res["prev_close"] = round(prev_close, 4)
    return res


def _simulate_ah(bars_day, next_day_bars, entry_bar_i):
    """盘后入场模拟 (2026-09-17 用户「要」): 入场 = 16:05 那根 5m bar 收盘;
    退出臂 = 现役规则(afterhours decide) / 盘后收口 19:55 / 次日开盘 09:35 / 次日收盘 / 仅止损."""
    entry = bars_day[entry_bar_i]["c"]
    day = bars_day[entry_bar_i]["t"][:10]
    decide = _shared_profit_exit_decider(entry, entry * 0.95, day)
    res = {}
    ah = [b for b in bars_day if _hhmm(b["t"]) >= 1600]
    cum, run = {}, 0.0
    for b in bars_day:
        run += float(b["v"])
        cum[b["t"]] = run
    act, ex = "hold", None
    for b in bars_day:
        if b["t"] <= bars_day[entry_bar_i]["t"]:
            continue
        a = decide(b["c"])
        if a in ("TAKE_PROFIT", "SL_BREAKEVEN", "SL_TRAIL_LOCK"):
            act, ex = a, b["c"]
            break
    res["rule_action"] = act
    ex_sl = None
    for b in bars_day:
        if b["t"] <= bars_day[entry_bar_i]["t"]:
            continue
        if (b["c"] / entry - 1) * 100 <= -1.5:
            ex_sl = b["c"]
            break
    sc = ah[-1]["c"] if ah else None
    no, nc = None, None
    for b in next_day_bars:
        if _hhmm(b["t"]) >= 935:
            no = b["c"]
            break
    nx = [b for b in next_day_bars if 930 <= _hhmm(b["t"]) <= 1600]
    if nx:
        nc = nx[-1]["c"]

    # ── 2026-09-19 新增: carry 仓(盘后进场→次日开盘兑现)的「锁」臂 ─────────────
    #  用户令「以现有最新策略实现盈利锁以及动态回撤锁, 确保利润确定性落袋, 不是干看着
    #  死等到开盘」。基线 = next_open (§16.6 B: 均值 +3.47%, n=25) —— 全部锁臂同表对比。
    #  路径口径 = **真实 carry 路径**: 当日盘后(入场后) + 次日盘前(<09:30); 未触发即落到
    #  next_open 价, 因此每条锁臂都可读成"带上这把锁的 hold_to_next_open"。
    carry = [b for b in bars_day
             if b["t"] > bars_day[entry_bar_i]["t"] and _hhmm(b["t"]) >= 1600]
    carry += [b for b in next_day_bars if _hhmm(b["t"]) < 930]
    # C1) 落袋价修正臂: 与 rule 同判定链, 但 TRAIL 触发时按**锁定地板**成交(非净保本线)
    full = _shared_profit_exit_full_decider(entry, entry * 0.95, day)
    tf_px, tf_act = None, "hold"
    for b in carry:
        d = full(b["c"])
        if d["action"] in ("TAKE_PROFIT", "SL_BREAKEVEN", "SL_TRAIL_LOCK"):
            tf_act = d["action"]
            tf_px = float(d["effective_sl"]) if d["action"] == "SL_TRAIL_LOCK" else b["c"]
            break
    res["trail_floor_px"] = tf_px
    res["trail_floor_action"] = tf_act

    # ── C5) 生产同形"分层动态锁利"档位臂 (2026-09-19 用户: ACN MFE 只拿到 57%, 如何捕获更高) ──
    #  生产机制 = clamp(让出比例 × MFE, min_dd, max_dd); 让出 40% ⇒ 设计捕获 60%。
    #  本组按**同一形式**换让出比例 (及上限), 度量捕获率 = 成交收益/峰值收益, 与净收益并看。
    def _adapt_arm(give_back, min_dd=0.4, cap=1.2, be_at=0.4, initial_sl_pct=5.0, cert=None):
        """生产阶梯同形: 峰值未达 BE 线 ⇒ 地板仍是初始 SL(-5%); 达线后地板 = 峰值 - 回撤。
        (首版漏了这条 ⇒ 地板一开始就在 -0.4% ⇒ 把亏损单砍在噪声里, 数字不可用; 已修正。)
        """
        _peak, _hit = entry, None
        for b in carry:
            _peak = max(_peak, b["c"])
            _pp = (_peak / entry - 1) * 100
            if cert and (b["c"] / entry - 1) * 100 >= cert:   # +3% 确定性落袋优先
                _hit = b["c"]
                break
            if _pp < be_at:                      # BE 未武装 ⇒ 保护线 = 初始 SL
                _floor = entry * (1 - initial_sl_pct / 100.0)
            else:
                _dd = min(max(give_back * _pp, min_dd), cap)
                _floor = entry * (1 + (_pp - _dd) / 100.0)
            if b["c"] <= _floor:                 # 可成交价跌破保护线才出场 (与生产同)
                _hit = b["c"]
                break
        return _hit

    def _adapt_layered(_pp):
        """分层: 小 MFE 维持 40% 让出 (噪声区不动), 中/大 MFE 逐级收紧到 30%/20%。"""
        if _pp < 3.0:
            return 0.40
        if _pp < 6.0:
            return 0.30
        return 0.20

    res["adapt60_px"] = _adapt_arm(0.40)                       # = 现役生产
    res["adapt70_px"] = _adapt_arm(0.30)                        # 让出 30% ⇒ 捕获 70%
    res["adapt75_px"] = _adapt_arm(0.25)                        # 让出 25% ⇒ 捕获 75%
    res["adapt80_px"] = _adapt_arm(0.20, cap=2.0)               # 让出 20% ⇒ 捕获 80%
    _peak, _hit = entry, None
    for b in carry:
        _peak = max(_peak, b["c"])
        _pp = (_peak / entry - 1) * 100
        if _pp < 0.4:
            _dd_floor_px = entry * 0.95
        else:
            _dd_floor_px = entry * (1 + (_pp - min(max(_adapt_layered(_pp) * _pp, 0.4), 2.0)) / 100.0)
        if b["c"] <= _dd_floor_px:
            _hit = b["c"]
            break
    res["adapt_layered_px"] = _hit

    # C6) 峰值口径改为**时段真实 high** (2026-09-19 第二把杠杆): 现役 peak 只跟可成交价序列,
    #   而延长时段有独立 high (实锤: NOK 盘前 last 卡 9.84 不动, pre_high 10.35→10.47;
    #   premarket_exit_shadow 已读 pre_high_price 但**未喂给 peak**) ⇒ 用 high 抬峰值 = 地板
    #   随真实高点抬升 ⇒ 捕获率↑。代价: 地板更高 ⇒ 可能更早出场 (须回放裁决, 故列入本组)。
    def _adapt_arm_hi(give_back, cap=1.2, be_at=0.4, initial_sl_pct=5.0, cert=None):
        _peakh, _hit = entry, None
        for b in carry:
            _peakh = max(_peakh, b["h"])
            _pp = (_peakh / entry - 1) * 100
            if cert and (b["c"] / entry - 1) * 100 >= cert:   # +3% 确定性落袋优先
                _hit = b["c"]
                break
            if _pp < be_at:
                _floor = entry * (1 - initial_sl_pct / 100.0)
            else:
                _dd = min(max(give_back * _pp, 0.4), cap)
                _floor = entry * (1 + (_pp - _dd) / 100.0)
            if b["c"] <= _floor:
                _hit = b["c"]
                break
        return _hit

    res["adapt60_hi_px"] = _adapt_arm_hi(0.40)
    res["adapt75_hi_px"] = _adapt_arm_hi(0.25)
    # **生产真实组合** (2026-09-19 上线后): 现役 adaptive 地板 + +3% 确定性落袋 (cert)
    res["live_prod_px"] = _adapt_arm(0.40, cert=3.0)
    res["live_prod_hi_px"] = _adapt_arm_hi(0.40, cert=3.0)
    res["carry_high_pct"] = round((max((b["h"] for b in carry), default=entry) / entry - 1) * 100, 3)

    # ── C7) 绝对地板阶梯 (2026-09-19 用户核心目标: 每笔净收益不亏损 > 更高更精准收益 > 回撤守住) ──
    #  现役 = 让出 MFE 的 40% (相对), 小 MFE 时地板很低 (MFE 1% ⇒ 地板 +0.6%, 仅够覆盖费>;
    #  MFE 0.5% ⇒ 地板 +0.1% 已被 0.4% 下限截断 ⇒ 事实上"不亏损"得不到保证)。
    #  阶梯 = 按**已见峰值**给**绝对地板**, 保证: 有肉必锁死不亏, 肉越厚地板越高 (单调只上不下)。
    _BE_NET = 0.088  # 往返费(%) ⇒ 净保本地板
    def _stair_arm(bands):
        _peak, _hit = entry, None
        for b in carry:
            _peak = max(_peak, b["c"])
            _pp = (_peak / entry - 1) * 100
            _floor_pct = -5.0                       # 未武装 ⇒ 初始 SL
            for _th, _lk in bands:
                if _pp >= _th:
                    _floor_pct = max(_floor_pct, _lk)
            if b["c"] <= entry * (1 + _floor_pct / 100.0):
                _hit = b["c"]
                break
        return _hit

    # 阶梯A (保守·广覆盖): 0.4%⇒净保本, 1.5%⇒锁+1%, 3%⇒锁+2.5%, 6%⇒锁+4%
    _stA = ((0.4, _BE_NET), (1.5, 1.0), (3.0, 2.5), (6.0, 4.0))
    # 阶梯B (激进·更紧): 0.4%⇒净保本+0.3%, 1.5%⇒+1.5%, 3%⇒+3%, 6%⇒+5%
    _stB = ((0.4, _BE_NET + 0.3), (1.5, 1.5), (3.0, 3.0), (6.0, 5.0))
    res["stair_A_px"] = _stair_arm(_stA)
    res["stair_B_px"] = _stair_arm(_stB)
    res["be_only_px"] = _stair_arm(((0.4, _BE_NET),))      # 只保本, 不锁利 (隔离"不亏损"收益)

    # ── C8) 带缓冲地板阶梯 + 与生产已上线机制的**组合臂** (2026-09-19 用户令第 3 项) ──
    #  实测教训 (本批): "贴成本保本锁"净赚占比仅 31% < 不做(49%) —— 带宽=往返费 ⇒ 必被噪声
    #  打穿, 打穿即净亏损。故本组每一档地板都是**正收益**(buffered), 并测与 +3% 确定性落袋锁、
    #  "进场后 high 峰值口径" 的组合 (= **生产当前真实形态**)。
    def _stair_arm2(bands, use_high=False, cert=None):
        _peak, _hit = entry, None
        _peakh = entry
        for b in carry:
            _peak = max(_peak, b["c"])
            _peakh = max(_peakh, b["h"])
            _pp = ((_peakh if use_high else _peak) / entry - 1) * 100
            if cert and (b["c"] / entry - 1) * 100 >= cert:      # +3% 确定性落袋优先
                _hit = b["c"]
                break
            _floor_pct = -5.0
            for _th, _lk in bands:
                if _pp >= _th:
                    _floor_pct = max(_floor_pct, _lk)
            if b["c"] <= entry * (1 + _floor_pct / 100.0):
                _hit = b["c"]
                break
        return _hit

    _stC = ((0.9, 0.5), (1.5, 0.9), (2.5, 1.8), (4.0, 3.0), (6.0, 4.5))       # 每档地板都是正的
    _stD = ((0.7, 0.45), (1.2, 0.9), (2.0, 1.6), (3.0, 2.5), (5.0, 4.2), (8.0, 7.0))
    res["stair_C_px"] = _stair_arm2(_stC)
    res["stair_D_px"] = _stair_arm2(_stD)
    res["live_combo_px"] = _stair_arm2(_stC, cert=3.0)                 # 落袋锁 +3% + 阶梯C
    res["live_combo_hi_px"] = _stair_arm2(_stC, use_high=True, cert=3.0)  # = 生产现状(含 high 口径)
    res["carry_peak_pct"] = res.get("carry_peak_pct")
    res["cap_pct60"] = round((max((b["c"] for b in carry), default=entry) / entry - 1) * 100, 3)
    # C2) 盈利锁臂: 触及 entry×(1+k%) 即以该价落袋(确定性), 否则落到 next_open
    for _k in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0):
        _hit = None
        for b in carry:
            if (b["c"] / entry - 1) * 100 >= _k:
                _hit = b["c"]
                break
        res[f"lock_{int(_k)}pct_px"] = _hit
    # C3) 动态回撤锁臂: 运行峰值回撤 dd% 即落袋, 否则落到 next_open
    for _dd in (1.5, 2.5):
        _peak, _hit = entry, None
        for b in carry:
            _peak = max(_peak, b["c"])
            if _peak > entry and b["c"] <= _peak * (1 - _dd / 100.0):
                _hit = b["c"]
                break
        res["dd_" + str(_dd).replace('.', 'p') + "pct_px"] = _hit

    # C4) 分批落袋臂 (2026-09-19): 用户要"确定性落袋", 但回放显示全量止盈锁牺牲均值
    #   (砍 best +26.5%) ⇒ 测"锁一半 + 余半持到开盘": 确定性落袋一部分, 保留赢家尾部。
    #   费用口径: 3 条腿 (1 进场 + 2 出场) ⇒ 取单次往返费的 1.5× (per-share 模型线性近似)。
    def _so_pct(lock_px, fraction):
        if not lock_px:
            return None
        return (fraction * (lock_px / entry - 1) * 100
                + (1 - fraction) * (((no / entry - 1) * 100) if no else 0))

    for _k in (3.0, 4.0, 5.0):
        _hit = None
        for b in carry:
            if (b["c"] / entry - 1) * 100 >= _k:
                _hit = b["c"]
                break
        res[f"so50_lock{int(_k)}pct_px"] = _hit
        if _hit is not None:
            res[f"so50_lock{int(_k)}pct_ret"] = _so_pct(_hit, 0.5)
            res[f"so50_lock{int(_k)}pct_legs"] = 2
        else:
            # ⚠️ 自纠 (2026-09-19): 未触发时必须**仍入全样本** (= 全量持到开盘), 否则
            #   聚合只统计触发子集 ⇒ 抽样偏差把均值虚高 (首跑 so50_lock4 报 +5.58%/n=20,
            #   实际全样本应 ≈ 0.5×lock4 + 0.5×next_open)。承坑 49② 同型, 本臂自身踩中。
            res[f"so50_lock{int(_k)}pct_ret"] = ((no / entry - 1) * 100) if no else None
            res[f"so50_lock{int(_k)}pct_legs"] = 1
    for _dd in (1.5, 2.5):
        _peak, _hit = entry, None
        for b in carry:
            _peak = max(_peak, b["c"])
            if _peak > entry and b["c"] <= _peak * (1 - _dd / 100.0):
                _hit = b["c"]
                break
        res[f"so50_dd{str(_dd).replace('.', 'p')}pct_px"] = _hit
        if _hit is not None:
            res[f"so50_dd{str(_dd).replace('.', 'p')}pct_ret"] = _so_pct(_hit, 0.5)
            res[f"so50_dd{str(_dd).replace('.', 'p')}pct_legs"] = 2
        else:
            res[f"so50_dd{str(_dd).replace('.', 'p')}pct_ret"] = ((no / entry - 1) * 100) if no else None
            res[f"so50_dd{str(_dd).replace('.', 'p')}pct_legs"] = 1

    for tag, px in (("rule", ex or sc), ("session_close", sc), ("next_open", no),
                    ("next_rth_close", nc), ("sl_only", ex_sl or sc),
                    ("trail_floor", res.get("trail_floor_px") or no),
                    ("lock_3pct", res.get("lock_3pct_px") or no),
                    ("lock_4pct", res.get("lock_4pct_px") or no),
                    ("lock_5pct", res.get("lock_5pct_px") or no),
                    ("lock_6pct", res.get("lock_6pct_px") or no),
                    ("dd_1p5pct", res.get("dd_1p5pct_px") or no),
                    ("dd_2p5pct", res.get("dd_2p5pct_px") or no),
                    ("adapt60", res.get("adapt60_px") or no),
                    ("adapt70", res.get("adapt70_px") or no),
                    ("adapt75", res.get("adapt75_px") or no),
                    ("adapt80", res.get("adapt80_px") or no),
                    ("adapt_layered", res.get("adapt_layered_px") or no),
                    ("adapt60_hi", res.get("adapt60_hi_px") or no),
                    ("adapt75_hi", res.get("adapt75_hi_px") or no),
                    ("stair_A", res.get("stair_A_px") or no),
                    ("stair_B", res.get("stair_B_px") or no),
                    ("be_only", res.get("be_only_px") or no),
                    ("lock_1pct", res.get("lock_1pct_px") or no),
                    ("lock_2pct", res.get("lock_2pct_px") or no),
                    ("stair_C", res.get("stair_C_px") or no),
                    ("stair_D", res.get("stair_D_px") or no),
                    ("live_combo", res.get("live_combo_px") or no),
                    ("live_combo_hi", res.get("live_combo_hi_px") or no),
                    ("live_prod", res.get("live_prod_px") or no),
                    ("live_prod_hi", res.get("live_prod_hi_px") or no),
                    ("lock_3pct_c", res.get("lock_3pct_px") or no),
                    ("trail_floor_c", res.get("trail_floor_px") or no)):
        if px:
            g = (px / entry - 1) * 100
            fee = _round_trip_fee_pct(entry, px, day)
            res[tag] = {"ret_pct": round(g, 3), "fee_pct": round(fee, 3),
                        "net_pct": round(g - fee, 3)}
    res["carry_peak_pct"] = round((max((b["c"] for b in carry), default=entry) / entry - 1) * 100, 3)
    # C4 结算: 分批落袋臂 — 混合收益 - 1.5×单次往返费 (3 条腿: 1 进场 + 2 出场)
    for _tag, _ret, _legs in (("so50_lock3pct", res.get("so50_lock3pct_ret"), res.get("so50_lock3pct_legs")),
                       ("so50_lock4pct", res.get("so50_lock4pct_ret"), res.get("so50_lock4pct_legs")),
                       ("so50_lock5pct", res.get("so50_lock5pct_ret"), res.get("so50_lock5pct_legs")),
                       ("so50_dd1p5pct", res.get("so50_dd1p5pct_ret"), res.get("so50_dd1p5pct_legs")),
                       ("so50_dd2p5pct", res.get("so50_dd2p5pct_ret"), res.get("so50_dd2p5pct_legs"))):
        if _ret is not None:
            _mult = 1.5 if (_legs or 1) == 2 else 1.0     # 2 条出场腿 = 1.5×单次往返费
            _fee = _round_trip_fee_pct(entry, entry * (1 + _ret / 100.0), day) * _mult
            res[_tag] = {"ret_pct": round(_ret, 3), "fee_pct": round(_fee, 3),
                         "net_pct": round(_ret - _fee, 3)}
    peak = max((b["h"] for b in ah if b["t"] > bars_day[entry_bar_i]["t"]), default=None)
    res["mfe_pct"] = round((peak / entry - 1) * 100, 3) if peak else None
    res["entry_px"] = round(entry, 4)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10, help="回看交易日数")
    ap.add_argument("--source", default="both", choices=["beat", "watchlist", "both"])
    ap.add_argument("--recent-days", type=int, default=7, help="财报披露近因窗口")
    ap.add_argument("--min-chg", type=float, default=None, help="覆盖涨幅门槛(默认用现役 2.0)")
    ap.add_argument("--min-vol", type=float, default=None, help="覆盖量门槛(默认用现役 200000)")
    ap.add_argument("--session", default="premarket", choices=["premarket", "afterhours"])
    ap.add_argument("--end", default=None, help="窗口结束日 YYYY-MM-DD (默认今天) —— 用于换一段窗口做样本外")
    args = ap.parse_args()

    import scripts.extended_hours_entry_executor as E
    _rule_arm_ok = True
    from futu import OpenQuoteContext, KLType, BeatType

    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPD_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPD_PORT", "11111")))
    try:
        rank_df = None
        if args.source in ("beat", "both"):
            r = ctx.get_earnings_beat_rank("US", BeatType.EPS, count=200)
            rank_df = r[1][1] if r[0] == 0 and isinstance(r[1], tuple) else None
        watch = []
        if args.source in ("watchlist", "both"):
            watch = E._watchlist_symbols(ctx)

        # 交易日序列: 以宽基 SPY 的日线为准 (最近 N+1 个交易日)
        end = args.end or datetime.now().strftime("%Y-%m-%d")
        start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=args.days * 2 + 15)).strftime("%Y-%m-%d")
        days_bars = _kline(ctx, "US.SPY", start, end, KLType.K_DAY)
        days = [b["t"][:10] for b in days_bars][-(args.days + 1):]
        print(f"[replay] 交易日窗口: {days[0]} → {days[-1]} ({len(days)} 日); 源={args.source} "
              f"(beat={len(rank_df) if rank_df is not None else 0} 行, watchlist={len(watch)} 只)")

        # 全窗口一次性预取 (原逐日取数 = days×symbols 次调用, 会被限频; 见 DEB §14.6)
        universe = {}
        for di, day in enumerate(days[1:], start=1):
            if rank_df is not None:
                for sec, rd in _candidate_days(rank_df, day, args.recent_days):
                    universe.setdefault(sec, f"beat:{rd}")
            for sec in watch:
                universe.setdefault(sec, "watchlist")
        print(f"[replay] 候选宇宙 {len(universe)} 只 → 预取 5m 全窗口 bars …")
        bars_by_sym = {}
        for sec in universe:
            bars_by_sym[sec] = _kline_cached(ctx, sec, days[0], days[-1], KLType.K_5M)
        rows = []
        for di, day in enumerate(days[1:], start=1):
            prev_day = days[di - 1]
            for sec, src in universe.items():
                if rank_df is not None:
                    # 事件源按日重建: 只有 released_date 落在该日近因窗内才算当日候选
                    cand_days = {s2 for s2, _ in _candidate_days(rank_df, day, args.recent_days)}
                    if src.startswith("beat") and sec not in cand_days:
                        continue
                    if not src.startswith("beat") and args.source == "beat":
                        continue
                bars = bars_by_sym.get(sec) or []
                if not bars:
                    continue
                today = [b for b in bars if b["t"][:10] == day]
                if not today:
                    continue
                if args.session == "afterhours":
                    # 盘后: 分母 = **当日** RTH 收盘 (盘后涨跌幅口径); 入场 = 16:05 那根 bar
                    rth_today = [b for b in today if 930 <= _hhmm(b["t"]) <= 1600]
                    if not rth_today:
                        continue
                    prev_close = rth_today[-1]["c"]
                    idxs = [i for i, b in enumerate(today) if _hhmm(b["t"]) >= 1605]
                    if not idxs:
                        continue
                    ei = idxs[0]
                else:
                    pc = [b for b in bars if b["t"][:10] == prev_day and 930 <= _hhmm(b["t"]) <= 1600]
                    if not pc:
                        continue
                    prev_close = pc[-1]["c"]
                    pre = [b for b in today if _hhmm(b["t"]) <= PREMARKET_END]
                    if not pre:
                        continue
                    idxs = [i for i, b in enumerate(today) if _hhmm(b["t"]) <= ENTRY_HHMM]
                    if not idxs:
                        continue
                    ei = idxs[-1]
                entry_px = today[ei]["c"]
                chg = (entry_px / prev_close - 1) * 100 if prev_close > 0 else 0
                vol = sum(b["v"] for b in today[:ei + 1])
                min_chg = args.min_chg if args.min_chg is not None else E.ENTRY_CHANGE_PCT
                min_vol = args.min_vol if args.min_vol is not None else E.MIN_VOLUME
                passed = E._price_in_range(entry_px) and chg >= min_chg and vol >= min_vol
                rec = {"day": day, "symbol": sec, "source": src, "entry_px": round(entry_px, 4),
                       "prev_close": round(prev_close, 4), "chg_pct": round(chg, 2),
                       "pre_vol": vol, "gates_passed": bool(passed)}
                if passed:
                    try:
                        if args.session == "afterhours":
                            nxt = days[di + 1] if di + 1 < len(days) else None
                            nb = [b for b in bars if b["t"][:10] == nxt] if nxt else []
                            rec.update(_simulate_ah(today, nb, ei))
                        else:
                            rec.update(_simulate(today, prev_close, ei))
                    except Exception as exc:
                        rec["sim_error"] = str(exc)[:80]
                rows.append(rec)

        passed = [r for r in rows if r["gates_passed"] and "ret_pct" in r.get("rule", {})]
        src_dist = {}
        for r in passed:
            k = r["source"].split(":")[0]
            src_dist[k] = src_dist.get(k, 0) + 1
        summary = {"generated_at": datetime.utcnow().isoformat() + "Z", "rule_arm_available": _rule_arm_ok,
                   "rule_arm_source": "shared_original_profit_exit", "days": len(days) - 1,
                   "source": args.source, "session": args.session, "end": end, "min_chg": args.min_chg, "min_vol": args.min_vol,
                   "candidates_evaluated": len(rows), "passed_by_source": src_dist,
                   "gates_passed": len(passed)}
        if passed:
            # 2026-09-17 修: 原实现按 (tag,label) 元组顺序打印且**存在重复 tag** ⇒ 行数/标签会错位
            # (盘后臂与盘前臂共用同一循环)。现按 tag **键名**输出, 并带中位/最差/最好 ⇒ 可看图辨尾部。
            arms = {}
            for r in passed:
                # 2026-09-19: 改为**通用聚合** — 凡形如 {"ret_pct","fee_pct","net_pct"} 的臂
                # 一律入表 (原实现硬编码 tag 列表 ⇒ 新增锁臂会被静默漏报, 旧 bug 同型病灶)。
                for tag, v in r.items():
                    if isinstance(v, dict) and "net_pct" in v and isinstance(v.get("net_pct"), (int, float)):
                        arms.setdefault(tag, []).append(v["net_pct"])
            summ_arms = {}
            for tag, v in arms.items():
                vs = sorted(v)
                summ_arms[tag] = {"n": len(v), "mean": round(sum(v) / len(v), 3),
                                  "median": round(vs[len(vs) // 2], 3),
                                  "worst": round(vs[0], 3), "best": round(vs[-1], 3),
                                  "win_rate_pct": round(sum(1 for x in v if x > 0) / len(v) * 100, 1),
                                  "total": round(sum(v), 1)}
            summary["arms"] = summ_arms
            # ── 捕获率 (2026-09-19): 成交收益/峰值收益, 只在峰值 >0.5% 的笔上算 (比值才有意义) ──
            cap = {}
            for tag in ("adapt60", "adapt70", "adapt75", "adapt80", "adapt_layered",
                        "next_open", "rule", "lock_4pct"):
                ratios = []
                for r in passed:
                    pk = r.get("carry_peak_pct")
                    v = (r.get(tag) or {}).get("ret_pct")
                    if pk and pk > 0.5 and v is not None:
                        ratios.append(v / pk)
                if ratios:
                    cap[tag] = {"n": len(ratios),
                                "mean_capture_pct": round(sum(ratios) / len(ratios) * 100, 1)}
            summary["capture_ratio"] = cap
            # ── 费用分档 (2026-09-18 R9) ────────────────────────────────
            # 动机: 均值里看不出"费用在低价股直接吃掉全部毛利"(CTNT 类 ≈9%/笔)。
            # 每档给 n / 平均费率 / rule 与盘前收口净均值, 便于 §4.1/§17.2 归因。
            bands = {}
            for r in passed:
                fee = (r.get("rule") or {}).get("fee_pct")
                if fee is None:
                    continue
                b = _price_band(r["entry_px"])
                d = bands.setdefault(b, {"n": 0, "_fee": 0.0, "_rule": 0.0, "_pre": 0.0, "_pre_n": 0})
                d["n"] += 1
                d["_fee"] += fee
                if "net_pct" in (r.get("rule") or {}):
                    d["_rule"] += r["rule"]["net_pct"]
                if "net_pct" in (r.get("pre_close") or {}):
                    d["_pre"] += r["pre_close"]["net_pct"]
                    d["_pre_n"] += 1
            summary["fee_bands"] = {
                b: {"n": d["n"], "mean_fee_pct": round(d["_fee"] / d["n"], 3),
                    "rule_net_mean": round(d["_rule"] / d["n"], 3),
                    "pre_close_net_mean": round(d["_pre"] / d["_pre_n"], 3) if d["_pre_n"] else None}
                for b, d in bands.items()
            }
            # 扁平 0.085% 对照 (旧口径, 只用于量化"低估了多少")
            flat = {}
            for r in passed:
                for tag in ("rule", "pre_close", "rth_close", "next_open"):
                    if "ret_pct" in (r.get(tag) or {}):
                        flat.setdefault(tag, []).append(r[tag]["ret_pct"] - FEE_PCT)
            summary["arms_flat_fee_net"] = {
                t: round(sum(v) / len(v), 3) for t, v in flat.items() if v
            }
        mfe = [r["mfe_pct"] for r in passed if r.get("mfe_pct") is not None]
        if mfe:
            summary["avg_mfe_pct"] = round(sum(mfe) / len(mfe), 3)
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps({"summary": summary, "trades": passed,
                                      "all_candidates": rows}, ensure_ascii=False, indent=1))
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"[replay] 明细 → {REPORT}")
        for r in passed[:12]:
            print(f"   {r['day']} {r['symbol']:10s} {r['source']:22s} 入场{r['entry_px']:>9} "
                  f"chg{r['chg_pct']:>7}% 规则={r['rule']['net_pct']:>7}% "
                  f"盘前收口={r.get('pre_close',{}).get('net_pct')}% 收盘={r.get('rth_close',{}).get('net_pct')}%")
    finally:
        ctx.close()


if __name__ == "__main__":
    main()
