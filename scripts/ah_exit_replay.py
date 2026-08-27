#!/usr/bin/env python3
"""盘后出场配对回放: 隔夜持仓在盘后时段(16:00-20:00 ET)触发出场 vs 实际留到次日.

动议 (2026-08-27 用户): 隔夜持股基于夜盘/盘后数据监控, 有盈利机会提前出仓.
先决条件已验证 (2026-08-27): Futu request_history_kline 传 extended_time=True 可取
盘后/盘前历史1分K (旧回放缓存 0 根盘后bar 是因为没传该参数, 非数据不可得).

样本: trade_records 已结算过夜持仓 (exit_date > entry_date), 过滤:
  - entry/exit 时间戳必须带时区 (naive 行为 6-7 月回填/ghost 混杂, 持仓夜不可靠重建)
  - entry 在 RTH (09:30-16:00), exit 在可成交时段 (09:30-20:00; 01:42/23:27 类
    mark-time 结算行出局 — 基线必须是真实成交)
  - exit_reason 排除 BROKER_GHOST_CLOSE / RECONCILE_GHOST_CLEANUP
臂 (基线=实际成交价, 配对):
  A_TP     : 盘后价触及 TP (metadata.take_profit) → 限价出场. 限价单成交价=触发价
             (bar.open 已越过则按 open, 更优); 限价无滑点, 不做折减 (乐观界, 已知风险:
             AH 流动性薄, 穿越型单边bar可能部分不成交 — 报告如实标注)
  A_PROTECT: 盘后价触及 SL (metadata.stop_loss) → 防护出场, 市价滑点 AH_SLIP_PCT
             (open 越线按 open, 否则按 SL, 均再扣滑点)
  A_COMB   : 两规则并行, 时间序先到先出; 同一bar内歧义保守取 PROTECT 先
  ── 盘后收盘决策臂 (2026-08-27 首跑实证: 静态TP/SL在AH 12/12未触发, AH最利区间
      -2.95%~+2.81% 够不到入场时定的TP(+3.7%~+8%)/SL(-2.2%~-5%) → 需贴近价格的锚):
  A_GREEN_TAKE : 任一持仓夜盘后末bar收盘 ≥ entry*(1+BE_PAD) → 以收盘价出 (扣滑点).
                 "夜盘已够到费用垫的盈利 → 落袋" (用户动议的止盈侧, 锚=生产BE_PAD)
  A_RED_CUT    : 任一持仓夜盘后末bar收盘 < entry → 出. "夜色发绿不持这夜" (翻本/防跌侧:
                 4笔大亏单MU/FIX/CAT/BABA盘后已明显发绿, 实际全拖到次日-5%级)
  A_RED_CUT_BE : 收盘 < entry*(1-BE_PAD) 才出 (收紧变体: 绿得超过费用垫才割)
  A_RED_CUT_1PCT: 收盘 < entry*(1-1.0%) 才出. ⚠ 样本内推导臂 (2026-08-27 n=12 首跑
                 发现: 全部'+delta'单的AH收盘深度≤-1.05%, 全部'-delta'复原单≥-1.07%,
                 取整1%为锚) — 现有12行即为其训练集, 裁决只能看后续新增样本
                 (8/26三仓8/27结算起), 判据仍 n>=15 总样本 + p<0.05 + 净差>0.
  未触发 → 回落实际成交 (delta=0, tag=no_trigger).
口径: delta = 价格盈亏差 ((出场-入场)*方向*qty). 费用两侧各一次出场相消, 不再单算.
判据 (DEB-20260805 预注册格式): n>=15 配对 + 符号置换双侧 p<0.05 且净差>0 才许灰度;
  当前为攒样期中期报告 (8/26 三笔隔夜仓 8/27 结算后 n 应补足).
盘后时段: 16:00-20:00 ET (Futu 美股无 20:00-04:00 真夜盘; 盘前 04:00-09:30 为 v2 扩展).
缓存: data/replay_1m_ah/ (与 RTH 回放缓存 data/replay_1m/ 分离, 防污染 overnight_1m_replay).
零执行路径改动, 纯回放. 回滚 = 删本脚本.
"""
import json
import sqlite3
import sys
import time
from datetime import date, timedelta
from math import comb
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "ftseek_us.db"
CACHE = REPO / "data" / "replay_1m_ah"
CACHE.mkdir(exist_ok=True)

AH_SLIP_PCT = 0.2   # 防护/收盘决策出场市价滑点 (生产 slip_cushion_pct 口径)
AH_SENS_SLIP = 0.5  # 滑点敏感性 (AH 点差恶化情形)
BE_PAD = 0.6        # 生产保本垫 fee 0.4 + slip 0.2 (unified_config 口径, 同 overnight_1m_replay)
GHOST_REASONS = {"BROKER_GHOST_CLOSE", "RECONCILE_GHOST_CLEANUP"}


def load_samples():
    """过夜持仓样本: aware时间戳 + 真实成交时段 + 非ghost. 返回 (kept, skipped原因)."""
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT trade_date, symbol, direction, entry_time, exit_time, entry_price,
               exit_price, quantity, pnl_net, exit_reason, metadata_json
        FROM trade_records
        WHERE exit_time IS NOT NULL AND pnl_net IS NOT NULL
          -- 过夜判据用 ET 墙钟日 (substr) 而非 date(): 后者把 -04:00 归一到
          -- UTC (+4h), 当日 20:00 ET 后出场的日内单 UTC 已是次日 → 误判为过夜。
          AND substr(exit_time, 1, 10) > substr(entry_time, 1, 10)
          AND trade_date >= '2026-07-01'
          AND exit_reason NOT IN ('BROKER_GHOST_CLOSE', 'RECONCILE_GHOST_CLEANUP')
        ORDER BY entry_time
    """).fetchall()
    con.close()
    kept, skipped = [], []
    for r in rows:
        et, xt = str(r["entry_time"]), str(r["exit_time"])
        if not (et[-6:] in ("-04:00", "-05:00") and xt[-6:] in ("-04:00", "-05:00")):
            skipped.append((r["trade_date"], r["symbol"], "naive时间戳(6-7月回填混杂)"))
            continue
        if not ("09:30" <= et[11:16] <= "16:00"):
            skipped.append((r["trade_date"], r["symbol"], f"入场非RTH({et[11:16]})"))
            continue
        if not ("09:30" <= xt[11:16] <= "20:00"):
            skipped.append((r["trade_date"], r["symbol"], f"出场时戳非成交时段({xt[11:16]}), 疑mark结算"))
            continue
        md = json.loads(r["metadata_json"] or "{}")
        tp = md.get("take_profit") or md.get("tp")
        sl = md.get("stop_loss") or md.get("sl")
        if not (tp and sl):
            skipped.append((r["trade_date"], r["symbol"], "TP/SL元数据缺失"))
            continue
        kept.append(dict(date=r["trade_date"], symbol=r["symbol"],
                         direction=r["direction"], entry_date=et[:10], exit_date=xt[:10],
                         entry=float(r["entry_price"]), actual_exit=float(r["exit_price"]),
                         qty=float(r["quantity"] or 1), pnl=float(r["pnl_net"]),
                         exit_reason=r["exit_reason"], tp=float(tp), sl=float(sl)))
    return kept, skipped


def fetch_1m_ah(qctx, futu_symbol, start_date, end_date):
    """盘后含扩展时段 1M bars → list[{ts,o,h,l,c}]. 幂等缓存."""
    ck = CACHE / f"{futu_symbol.replace('.', '_')}_{start_date}_{end_date}.json"
    if ck.exists():
        try:
            return json.loads(ck.read_text())
        except Exception:
            pass
    from futu import KLType, AuType, RET_OK
    bars, page_key = [], None
    while True:
        kwargs = dict(code=futu_symbol, ktype=KLType.K_1M, autype=AuType.NONE,
                      start=start_date, end=end_date, max_count=1000,
                      extended_time=True)
        if page_key is not None:
            kwargs["page_req_key"] = page_key
        ret, data, page_key = qctx.request_history_kline(**kwargs)
        if ret != RET_OK:
            raise RuntimeError(f"history_kline {futu_symbol}: {data}")
        for _, row in data.iterrows():
            bars.append({"ts": str(row["time_key"]), "o": float(row["open"]),
                         "h": float(row["high"]), "l": float(row["low"]),
                         "c": float(row["close"])})
        if page_key is None:
            break
        time.sleep(1.2)
    ck.write_text(json.dumps(bars))
    return bars


def ah_bars_by_date(bars):
    """{date: [bar]} 仅盘后 16:00-20:00 (含20:00收尾bar)."""
    out = {}
    for b in bars:
        hhmm = b["ts"][11:16]
        if "16:00" <= hhmm <= "20:00":
            out.setdefault(b["ts"][:10], []).append(b)
    return out


def nights_between(entry_date, exit_date):
    d0, d1 = date.fromisoformat(entry_date), date.fromisoformat(exit_date)
    out = []
    while d0 < d1:
        out.append(d0.isoformat())
        d0 += timedelta(days=1)
    return out


def replay_arm(ah_by_date, nights, direction, entry, tp, sl, slip_pct, mode):
    """单臂回放. 返回 (exit_price|None, tag, night, bar_ts).

    触发类 (tp/protect/comb): 逐bar扫描, 同bar歧义保守取 PROTECT 先.
    收盘决策类 (green_take/red_cut/red_cut_be): 每夜以盘后末bar收盘判定, 先到的夜先出.
    """
    for d in nights:
        bars = ah_by_date.get(d, [])
        if mode in ("green_take", "red_cut", "red_cut_be", "red_cut_1pct"):
            if not bars:
                continue
            c = bars[-1]["c"]
            if mode == "green_take":
                hit = c >= entry * (1 + BE_PAD / 100) if direction == "LONG" \
                    else c <= entry * (1 - BE_PAD / 100)
            elif mode == "red_cut":
                hit = c < entry if direction == "LONG" else c > entry
            elif mode == "red_cut_1pct":
                hit = c < entry * (1 - 1.0 / 100) if direction == "LONG" \
                    else c > entry * (1 + 1.0 / 100)
            else:
                hit = c < entry * (1 - BE_PAD / 100) if direction == "LONG" \
                    else c > entry * (1 + BE_PAD / 100)
            if hit:
                px = c * (1 - slip_pct / 100) if direction == "LONG" else c * (1 + slip_pct / 100)
                return px, f"AH_{mode.upper()}", d, bars[-1]["ts"]
            continue
        for b in bars:
            o, h, l = b["o"], b["h"], b["l"]
            triggers = []
            if mode in ("protect", "comb"):
                if direction == "LONG" and (o <= sl or l <= sl):
                    px = o * (1 - slip_pct / 100) if o <= sl else sl * (1 - slip_pct / 100)
                    triggers.append(("AH_PROTECT", px))
                elif direction == "SHORT" and (o >= sl or h >= sl):
                    px = o * (1 + slip_pct / 100) if o >= sl else sl * (1 + slip_pct / 100)
                    triggers.append(("AH_PROTECT", px))
            if mode in ("tp", "comb") and tp:
                if direction == "LONG" and (o >= tp or h >= tp):
                    triggers.append(("AH_TP", o if o >= tp else tp))
                elif direction == "SHORT" and (o <= tp or l <= tp):
                    triggers.append(("AH_TP", o if o <= tp else tp))
            if triggers:
                # 同bar歧义: PROTECT 先入列 → 保守; 跨bar自然时间序
                tag, px = triggers[0]
                return px, tag, d, b["ts"]
    return None, "no_trigger", None, None


def sign_test_p(deltas):
    nz = [d for d in deltas if abs(d) > 1e-9]
    m, k = len(nz), sum(1 for d in nz if d > 0)
    if m == 0:
        return None, k, m
    tail = sum(comb(m, i) for i in range(0, min(k, m - k) + 1)) / 2 ** m
    return min(1.0, 2 * tail), k, m


def main():
    samples, skipped = load_samples()
    print(f"样本 n={len(samples)} (过滤 {len(skipped)} 行: " +
          "; ".join(f"{s[1]}:{s[2]}" for s in skipped[:6]) +
          ("..." if len(skipped) > 6 else "") + ")")
    print("拉取盘后1分K (extended_time=True, 缓存优先)...")

    from futu import OpenQuoteContext
    qctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    ah_cache = {}
    try:
        for s in samples:
            key = (s["symbol"], s["entry_date"], s["exit_date"])
            if key in ah_cache:
                continue
            try:
                bars = fetch_1m_ah(qctx, s["symbol"], s["entry_date"], s["exit_date"])
                ah_cache[key] = ah_bars_by_date(bars)
                n_ah = sum(len(v) for v in ah_cache[key].values())
                print(f"  {s['symbol']:<9} {s['entry_date']}→{s['exit_date']}: "
                      f"{len(bars)} bars / 盘后{n_ah}根 / {len(ah_cache[key])}夜")
            except Exception as e:
                print(f"  {s['symbol']}: 拉取失败 {e}")
                ah_cache[key] = None
            time.sleep(1.2)
    finally:
        qctx.close()

    arms = ("A_TP", "A_PROTECT", "A_COMB", "A_COMB_sens",
            "A_GREEN_TAKE", "A_RED_CUT", "A_RED_CUT_BE", "A_RED_CUT_1PCT")
    detail = []
    arm_deltas = {a: [] for a in arms}
    for s in samples:
        key = (s["symbol"], s["entry_date"], s["exit_date"])
        ah = ah_cache.get(key)
        if not ah:
            print(f"  [skip] {s['symbol']} 无盘后K")
            continue
        nights = nights_between(s["entry_date"], s["exit_date"])
        sgn = 1.0 if s["direction"] == "LONG" else -1.0
        base_pnl = sgn * (s["actual_exit"] - s["entry"]) * s["qty"]
        # 盘后最有利漂移 (首夜口径外全夜max, 供解读)
        ah_fav = None
        for d in nights:
            for b in ah.get(d, []):
                ext = max(b["h"], b["l"]) if sgn > 0 else min(b["h"], b["l"])
                fav = sgn * (ext - s["entry"]) / s["entry"] * 100
                ah_fav = fav if ah_fav is None else max(ah_fav, fav)
        row = dict(date=s["date"], symbol=s["symbol"], direction=s["direction"],
                   entry=s["entry"], actual_exit=s["actual_exit"], qty=s["qty"],
                   pnl=s["pnl"], exit_reason=s["exit_reason"], tp=s["tp"], sl=s["sl"],
                   nights=len(nights), ah_best_fav_pct=None if ah_fav is None else round(ah_fav, 2),
                   arms={})
        variants = {
            "A_TP": ("tp", AH_SLIP_PCT),
            "A_PROTECT": ("protect", AH_SLIP_PCT),
            "A_COMB": ("comb", AH_SLIP_PCT),
            "A_COMB_sens": ("comb", AH_SENS_SLIP),
            "A_GREEN_TAKE": ("green_take", AH_SLIP_PCT),
            "A_RED_CUT": ("red_cut", AH_SLIP_PCT),
            "A_RED_CUT_BE": ("red_cut_be", AH_SLIP_PCT),
            "A_RED_CUT_1PCT": ("red_cut_1pct", AH_SLIP_PCT),
        }
        for arm, (mode, slip) in variants.items():
            px, tag, night, ts = replay_arm(ah, nights, s["direction"], s["entry"],
                                            s["tp"], s["sl"], slip, mode)
            if px is None:
                delta = 0.0
            else:
                delta = sgn * (px - s["actual_exit"]) * s["qty"]
            arm_deltas[arm].append(delta)
            row["arms"][arm] = dict(delta=round(delta, 1), tag=tag,
                                    night=night, exit_price=None if px is None else round(px, 2))
        detail.append(row)

    n = len(detail)
    print(f"\n═══ 盘后出场配对回放 n={n} (攒样期中期, 正式判据 n>=15) ═══")
    print(f"{'臂':<12}{'触发':>6}{'Σdelta':>10}{'均笔':>9}{'胜/负':>8}{'p(符号)':>9}")
    for arm in arms:
        v = arm_deltas[arm]
        trig = sum(1 for r in detail if r["arms"][arm]["tag"] != "no_trigger")
        p, k, m = sign_test_p(v)
        nz = [x for x in v if abs(x) > 1e-9]
        wins = sum(1 for x in nz if x > 0)
        pstr = f"{p:.3f}" if p is not None else "n/a"
        print(f"{arm:<12}{trig:>4}/{n}{sum(v):>+10.1f}{sum(v)/n:>+9.2f}"
              f"{wins:>4}/{len(nz)-wins:<3}{pstr:>9}")

    print("\n逐笔 (delta=盘后规则相对实际成交的价差盈亏):")
    for r in detail:
        print(f"  {r['date']} {r['symbol'][3:]:<6} {r['direction']:<5} entry={r['entry']:<9.2f}"
              f" 实际={r['actual_exit']:<9.2f} pnl={r['pnl']:>+8.1f} [{r['exit_reason']}]"
              f" 持{r['nights']}夜 AH最利={r['ah_best_fav_pct']}% TP={r['tp']:.2f} SL={r['sl']:.2f}")
        for arm in arms:
            a = r["arms"][arm]
            extra = f" @ {a['exit_price']}" if a["exit_price"] else ""
            print(f"      {arm:<11} {a['delta']:>+8.1f} [{a['tag']}{extra}]")

    out = REPO / "data" / "ah_exit_replay_2026-08-27.json"
    out.write_text(json.dumps(
        dict(generated="2026-08-27", n=n,
             criteria="n>=15配对 符号置换双侧p<0.05 且净差>0 才许灰度 (DEB-20260805格式)",
             arms={a: dict(total=round(sum(arm_deltas[a]), 1),
                           mean=round(sum(arm_deltas[a]) / n, 2)) for a in arms},
             detail=detail),
        indent=1, ensure_ascii=False))
    print(f"\n落盘: {out}")


if __name__ == "__main__":
    main()
