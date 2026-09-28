#!/usr/bin/env python3
"""亏损离场后的价格回翻分析 (2026-09-28, 用户问: 原本亏损退出的, 是否回撤/多久翻转盈利).

问题: 止损/亏损离场后, 股价有没有涨回入场价 (翻正)? 距离多久?
口径:
  · 人群 = trade_records 8/1 起已平仓且 pnl_net<0 的真仓 (按 exit_reason 分组)
  · **方向感知** (2026-09-28 修, DEB-20260928 §16.3 实锤旧版 bug): "回翻"= 价格朝**该仓盈利方向**
    越过入场价 —— LONG 用 high/close ≥ entry, SHORT 用 low/close ≤ entry。旧版对 SHORT 也用
    high ≥ entry ⇒ SHORT 天然偏真 (本人群 132 笔含 22 SHORT=17%; 日内 SL_INITIAL 16 笔含 6 SHORT=37.5%)。
  · ttr_close = 首个收盘同向越过 entry 的交易日序号 (离场次日=1); ttr_touch = 首个盘中触碰 entry
  · ttr_be_arm = 首个 BE 可武装日 (LONG high ≥ entry×1.004 / SHORT low ≤ entry×0.996, config 实值 0.4%)
  · bar 源 = 富途日K (K_DAY autype=NONE, 与 e1_preview_replay/b19 回放同配方), 缓存 data/replay_bars/
    ⚠️ 缓存**每晨刷新** (postexit_*_<start>_<today>.json) ⇒ 同一脚本不同日期跑出不同读数 (右删失窗
    每天变长)。**引用本脚本读数必须同时报缓存快照日期** (产物内 cache_snapshot 字段), 否则第三套数字。
  · 右删失: 离场太近窗口不足 k 日的笔, 在 "≤k日" 桶里只算可判定的 (印 n_eff)
只读: sqlite mode=ro, 只写 bar 缓存 + 产物 json。零交易行为变更。
已知偏差 (印在输出里, 不藏): ①9月大盘上行期回翻率天然偏高 ②BE/费用级小亏离场的回翻是平凡的
(价格本来就在入场价附近) ⇒ 按亏损幅度分桶: small(<1%) / mid(1-3%) / deep(>3%)。
③LONG-only 偏差: 上行期 LONG 回翻天然高, SHORT 回翻天然低 ⇒ 聚合读数须按 direction 分层看。
"""
import datetime as dt
import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "data/ftseek_us.db"
CACHE = REPO / "data/replay_bars"
SINCE = "2026-08-01"
TODAY = dt.date(2026, 9, 28)
WINDOWS = (5, 10, 20)          # 交易日桶
NET_PAD = 0.001                # 净口径垫 0.1% (往返费用线量级)
BE_ARM_PAD = 0.004             # BE 武装垫: breakeven_at_pct=0.4 (config 实值) ⇒ LONG ×1.004 / SHORT ×0.996
# 已禁用策略标注 (config/unified_config.yaml 实值 + DB 最后出现日双源核):
#   mfe_stall_exit_enabled: false (L1282, DEB-20260815 P0-B §15 重放否决), 最后离场 8/11
DEAD_REASONS = {"MFE_STALL_EXIT": "已禁用 8/15 (config L1282=false), 仅历史样本"}
_N_BAD = [0]                   # R5: 坏缓存/坏行计数留痕


def fetch_daily(symbol, start, end):
    """富途日K, 与 b19_wweek_miskill_replay.fetch_daily 同配方 (K_DAY/autype=NONE/1.2s限流)。"""
    fp = CACHE / f"postexit_{symbol.replace('.', '_')}_{start}_{end}.json"
    if fp.exists():
        try:
            b = json.loads(fp.read_text())
            if b and (b[-1][0] >= end or len(b) > 5):
                return b
        except Exception as e:
            _N_BAD[0] += 1
            print(f"    [bad-cache] {fp.name}: {e}")
    from futu import AuType, KLType, RET_OK, OpenQuoteContext
    q = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        ret, df, _ = q.request_history_kline(
            symbol, start=start, end=end, ktype=KLType.K_DAY,
            autype=AuType.NONE, max_count=1000)
        if ret != RET_OK:
            print(f"    [kline-fail] {symbol}: {df}")
            return None
        bars = [[r["time_key"][:10], r["open"], r["high"], r["low"], r["close"]]
                for _, r in df.iterrows()]
    finally:
        q.close()
        time.sleep(1.2)
    if bars:
        fp.write_text(json.dumps(bars))
    return bars


def bucket(loss_pct):
    a = abs(loss_pct)
    return "small(<1%)" if a < 1 else ("mid(1-3%)" if a <= 3 else "deep(>3%)")


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id,symbol,direction,entry_price,exit_price,exit_time,exit_reason,"
        "COALESCE(pnl_net,realized_pnl,0) net FROM trade_records "
        "WHERE date(exit_time)>=? AND COALESCE(pnl_net,realized_pnl,0)<0 "
        "AND entry_price>0 ORDER BY exit_time", (SINCE,)).fetchall()
    print(f"═══ 亏损离场人群: n={len(rows)} ({SINCE} 起) ═══")

    # 每 symbol 一次取 bar (最早离场日 → 今天)
    need = defaultdict(list)
    for r in rows:
        need[r["symbol"]].append(r)
    bars_by_sym = {}
    for sym, rs in sorted(need.items()):
        start = min(str(x["exit_time"])[:10] for x in rs)
        bars = fetch_daily(sym, start, TODAY.isoformat())
        if bars:
            bars_by_sym[sym] = bars
    print(f"  bar 覆盖: {len(bars_by_sym)}/{len(need)} symbols (R5 坏缓存={_N_BAD[0]})")

    out = []
    for r in rows:
        bars = bars_by_sym.get(r["symbol"])
        if not bars:
            continue
        ex_d = str(r["exit_time"])[:10]
        post = [b for b in bars if b[0] > ex_d]           # 离场次日起
        # ⚠️ 方向感知 (2026-09-28 修, DEB-20260928 §16.3 实锤): SHORT 的"回翻"= 价【跌】回入场价
        # (空头盈利方向), 旧实现一律用 high/close >= entry ⇒ SHORT 系统性误判为触碰 (天然偏真)。
        # 本 8/1 起亏损人群 132 笔含 22 笔 SHORT (17%); 日内 SL_INITIAL 16 笔含 6 笔 SHORT (37.5%)。
        sgn = -1.0 if str(r["direction"]).upper().startswith("S") else 1.0
        ep = float(r["entry_price"])
        net_line = ep * (1 + sgn * NET_PAD)
        loss_pct = sgn * (float(r["exit_price"]) / ep - 1) * 100 if r["exit_price"] else None
        ttr_c = ttr_t = ttr_be = None
        for i, b in enumerate(post, 1):
            hi, lo, cl = b[2], b[3], b[4]
            if ttr_c is None and sgn * (cl - ep) >= 0:
                ttr_c = i
            if ttr_t is None and (lo <= ep if sgn < 0 else hi >= ep):
                ttr_t = i
            # BE 武装口径: 若扛着不动, 现行 BE (breakeven_at_pct=0.4, config 实值) 何时可武装
            if ttr_be is None and (lo <= ep * (1 - BE_ARM_PAD) if sgn < 0 else hi >= ep * (1 + BE_ARM_PAD)):
                ttr_be = i
            if ttr_c is not None and ttr_t is not None and ttr_be is not None:
                break
        # 方向感知极值: LONG 取窗内最高收盘(有利方向), SHORT 取最低收盘
        ext_close = (min((b[4] for b in post), default=None) if sgn < 0
                     else max((b[4] for b in post), default=None))
        out.append(dict(
            tid=r["id"], symbol=r["symbol"], exit_date=ex_d,
            direction="SHORT" if sgn < 0 else "LONG",
            reason=r["exit_reason"] or "?", net=round(float(r["net"]), 2),
            loss_pct=round(loss_pct, 2) if loss_pct is not None else None,
            bkt=bucket(loss_pct) if loss_pct is not None else "?",
            win_avail=len(post), ttr_close=ttr_c, ttr_touch=ttr_t, ttr_be_arm=ttr_be,
            max_close_gain_pct=round(sgn * (ext_close / ep - 1) * 100, 2) if ext_close else None,
            net_recov=bool(ttr_c is not None and any(
                sgn * (b[4] - net_line) >= 0 for b in post[:ttr_c + 5])) if ttr_c else False,
        ))

    # ── 聚合: 全体 / 按亏损幅度桶 / 按 exit_reason ──
    def agg(group, label):
        n = len(group)
        if not n:
            return
        line = [f"\n  【{label}】 n={n}"]
        # 三口径 (方向感知): touch=回到入场价(LONG high≥ep / SHORT low≤ep) /
        #   close=收盘同向越过入场价 / be_arm=BE可武装(LONG ×1.004, SHORT ×0.996)
        # (用户 9/28 指正: 回翻不一定等收盘 — touch 口径上轮算了未聚合, be_arm 是"扛着的话
        #  现行 BE 何时能武装净保本出场"的反事实口径)
        for crit, cname in (("ttr_touch", "触碰 long:hi≥ep/short:lo≤ep"),
                            ("ttr_close", "收盘同向越过入场价       "),
                            ("ttr_be_arm", "BE可武装 long×1.004/short×0.996")):
            parts = []
            for k in WINDOWS:
                det = [x for x in group if x["win_avail"] >= k]   # 右删失: 只算窗口够的
                rec = [x for x in det if x[crit] is not None and x[crit] <= k]
                parts.append(f"≤{k}日 {len(rec)}/{len(det)}={len(rec)/len(det)*100:.0f}%"
                             if det else f"≤{k}日 -")
            tt = sorted(x[crit] for x in group if x[crit] is not None)
            line.append(f"    {cname}  " + "  ".join(parts)
                        + f"  中位{tt[len(tt)//2] if tt else '-'}日(n={len(tt)})")
        never = [x for x in group if x["ttr_close"] is None and x["win_avail"] >= 20]
        if never:
            mg = sorted(x["max_close_gain_pct"] for x in never if x["max_close_gain_pct"] is not None)
            line.append(f"    ≥20日窗口从未收盘回翻 {len(never)} 笔, 窗内最有利收盘中位 {mg[len(mg)//2]:+.2f}%")
        print("\n".join(line))

    print("\n═══ ① 全体亏损离场 ═══")
    agg(out, "全部")
    print("\n═══ ② 按亏损幅度分桶 (BE/费用级小亏的回翻是平凡的, 分开看) ═══")
    byb = defaultdict(list)
    for x in out:
        byb[x["bkt"]].append(x)
    for b in ("small(<1%)", "mid(1-3%)", "deep(>3%)"):
        agg(byb.get(b, []), b)
    print("\n═══ ③ 按 exit_reason (⚠️先查现役状态: config 开关 + DB 最后出现日) ═══")
    byr = defaultdict(list)
    for x in out:
        byr[x["reason"]].append(x)
    for rs, g in sorted(byr.items(), key=lambda kv: -len(kv[1])):
        tag = f" ⚠️{DEAD_REASONS[rs]}" if rs in DEAD_REASONS else ""
        if len(g) >= 3:
            agg(g, f"{rs} (n={len(g)}){tag}")
        else:
            print(f"\n  【{rs}】 n={len(g)} <3 不聚合{tag} (逐笔: "
                  + ", ".join(f"{x['symbol']} ttr={x['ttr_close']}" for x in g) + ")")

    print("\n═══ ④ 按 direction 分层 (⚠️上行期 LONG 回翻天然高, 混层读数会被 LONG 主导) ═══")
    byd = defaultdict(list)
    for x in out:
        byd[x["direction"]].append(x)
    for dd in ("LONG", "SHORT"):
        if byd.get(dd):
            agg(byd[dd], dd)

    print("\n═══ ⚠️ 判读边界 ═══")
    print("  · 9月窗口大盘上行, 回翻率天然偏高 — 这是描述统计, 不构成'该扛不砍'的裁决依据")
    print("  · 三口径均**方向感知**: touch=盘中触碰入场价(long hi≥ep/short lo≤ep) /")
    print("    close=收盘同向越过 / be_arm=BE可武装(long ×1.004, short ×0.996)")
    print("    (be_arm 是'扛着的话现行 BE 何时能武装净保本出场'的反事实, 0.4%=config breakeven_at_pct)")
    print("  · SHORT 样本小 (本人群 22/132) ⇒ SHORT 层读数只作方向参考, 不做裁决依据")
    print("  · MFE_STALL_EXIT 已禁用(config L1282=false, 8/15 DEB-20260815否决) ⇒ 其行仅历史, 不代表现行系统")
    print("  · 右删失已处理: ≤k日桶只算窗口≥k日的笔 (n_eff 印在分母)")
    print(f"  · ⚠️引用本产物读数必须同报缓存快照日期 = {TODAY.isoformat()} (bar 每晨刷新, 删失窗每天变长)")
    fp = REPO / f"data/loss_exit_recovery_{TODAY.isoformat()}.json"
    fp.write_text(json.dumps(dict(generated_at=dt.datetime.now().isoformat(timespec="seconds"),
                                  n=len(out), windows=WINDOWS, net_pad=NET_PAD,
                                  be_arm_pad=BE_ARM_PAD, direction_aware=True,
                                  cache_snapshot=TODAY.isoformat(),
                                  n_short=sum(1 for x in out if x["direction"] == "SHORT"),
                                  r5_bad=_N_BAD[0], trades=out),
                             ensure_ascii=False, indent=1))
    print(f"\n[saved] {fp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
