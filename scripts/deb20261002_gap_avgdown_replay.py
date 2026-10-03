"""隔夜跳空仓「扩大SL / 补仓」能否回本 — 精确反事实回放 (修 net_recov bool 求和错误)

口径修正: 上一版把 net_recov(bool=是否回翻)当金额求和 ⇒ Σ持有=97.0 实为"回翻笔数", 作废。
本版用日K bars 逐笔算三个可落地情景的**美元**净结局:
  A) 实际: 出场日按 exit_price 平 (地面真值, 从 DB 取)
  B) 扩大SL = 不出场继续持有到 T+k 收盘 (k=1/3/5/10/20), 方向感知净结局
  C) 补仓摊平 = 出场日开盘再买同名义一份 (成本摊半), 持到 T+k 收盘后全平

只读: DB mode=ro + 本地 bar 缓存。零副作用。
右删失必报: win_avail 短的笔标 CENSORED, 不计入聚合 (skill 纪律)。
"""
import json
import sqlite3
from app.storage.db_pool import get_db_path
from pathlib import Path

BASE = Path("/opt/ftseek/FTSeek-US")
FEE_RT = 0.0015   # 往返费 0.15% (与 trail_lock_pyramid 结算同口径)
SLIP = 0.0010     # 滑点 0.10% 方向感知

c = sqlite3.connect(f"file:{get_db_path('ftseek_research')}?mode=ro", uri=True)
c.row_factory = sqlite3.Row

rows = list(c.execute("""
    SELECT symbol, direction, entry_time, exit_time, entry_price, exit_price, quantity,
           COALESCE(pnl_net, realized_pnl) AS net, mfe_pct, mae_pct, exit_reason, metadata_json
    FROM trade_records
    WHERE exit_reason = 'OVERNIGHT_GAP_STOP'
      AND entry_time>='2026-06-27' AND entry_time<'2026-10-02 00:00:00'
    ORDER BY entry_time
"""))  # 钉结算窗 (2026-10-03 复查 P3 修复): 与本族 12 脚本同口径, 重跑读数不随新交易漂移
print(f"═══ 地面真值: OVERNIGHT_GAP_STOP n={len(rows)} ═══")


def _sign(direction) -> int:
    """SHORT/SELL_SHORT → −1 (2026-10-03 复查 P3 修复: B/C 情景原文档自称方向感知
    实则全按 LONG 算 — 现症为零 (7 笔全 LONG), SHORT 出现时结论会反)。"""
    return -1 if str(direction or "").upper() in ("SHORT", "SELL_SHORT") else 1


def load_bars(sym, start):
    """从 postexit_* 缓存取日K (起点=出场日附近); 返回 [(date, o,h,l,c)]"""
    cands = sorted((BASE / "data/replay_bars").glob(f"postexit_US_{sym.split('.')[-1]}_*.json"))
    out = []
    for p in cands:
        try:
            for b in json.load(open(p)):
                if b[0] >= start[:10]:
                    out.append((b[0], b[1], b[2], b[3], b[4]))
        except Exception as e:
            print(f"  ⚠️ {p.name}: {e}")
    # 去重按日期, 保序
    seen, uniq = set(), []
    for b in sorted(out, key=lambda x: x[0]):
        if b[0] not in seen:
            seen.add(b[0])
            uniq.append(b)
    return uniq


recs = []
for r in rows:
    sym = r["symbol"]
    ep, xp, q = r["entry_price"], r["exit_price"], r["quantity"]
    notional = ep * q
    bars = load_bars(sym, r["exit_time"])
    d = dict(sym=sym, dir=r["direction"], sign=_sign(r["direction"]), ep=ep, xp=xp, q=q,
             notional=notional,
             net=r["net"], mfe=r["mfe_pct"] or 0.0, mae=r["mae_pct"] or 0.0,
             exit_date=r["exit_time"][:10], bars=bars, n_bars=len(bars),
             entry_date=r["entry_time"][:10])
    recs.append(d)
    print(f"  {sym:<9} {r['direction']:<5} 入{d['entry_date']} 出{d['exit_date']} "
          f"价{ep:>9.2f}→{xp:>9.2f} q={q:>4} 名义={notional:>9.0f} 净={r['net']:>8.1f} "
          f"MFE={d['mfe']:.2f}% MAE={d['mae']:.2f}%  bars可用={len(bars)}")

print("\n═══ 情景B: 扩大SL = 不出场, 持到 T+k 收盘 (方向感知: sign×(close−entry)/entry) ═══")
print(f"{'sym':<9} {'T+1':>9} {'T+3':>9} {'T+5':>9} {'T+10':>9} {'T+20':>9}  窗长")
HOLD = (1, 3, 5, 10, 20)
for d in recs:
    cells = []
    for k in HOLD:
        if len(d["bars"]) >= k:
            cl = d["bars"][k - 1][4]               # (date,o,h,l,c) 第5元 = 收盘
            pct = 100 * d["sign"] * (cl - d["ep"]) / d["ep"]
            usd = pct / 100 * d["notional"] - d["notional"] * (FEE_RT + SLIP)
            cells.append(f"{usd:>+9.0f}")
        else:
            cells.append(f"{'CENS':>9}")
    print(f"{d['sym']:<9} " + " ".join(cells) + f"  {d['n_bars']}")

print("\n═══ 情景C: 补仓摊平 = 出场日开盘再买同名义一份, 持到 T+k 收盘全平 ═══")
print("  新均价 = (原名义 + 补仓名义) / (原股数 + 补股数); 补仓价 = 出场日开盘价")
print(f"{'sym':<9} {'补仓价':>9} {'新均价':>9} {'需涨幅':>8} {'T+1':>9} {'T+3':>9} {'T+5':>9} {'T+10':>9}")
for d in recs:
    if not d["bars"]:
        print(f"{d['sym']:<9} 无 bars")
        continue
    add_px = d["bars"][0][1]                       # 出场日开盘
    add_q = d["notional"] / add_px                 # 同名义一份
    tot_q = d["q"] + add_q
    avg = (d["ep"] * d["q"] + add_px * add_q) / tot_q
    need = 100 * d["sign"] * (avg - d["ep"]) / d["ep"]  # 新均价相对原入场的不利抬升(LONG=涨,SHORT=跌)
    tot_notional = d["notional"] * 2
    cells = []
    for k in (1, 3, 5, 10):
        if len(d["bars"]) >= k:
            cl = d["bars"][k - 1][4]
            pnl = d["sign"] * (cl - avg) * tot_q - tot_notional * (FEE_RT + SLIP)
            cells.append(f"{pnl:>+9.0f}")
        else:
            cells.append(f"{'CENS':>9}")
    print(f"{d['sym']:<9} {add_px:>9.2f} {avg:>9.2f} {need:>+7.2f}% " + " ".join(cells))

print("\n═══ 聚合 (只计窗口足够的笔, 右删失剔除) ═══")
for k in HOLD:
    ok = [d for d in recs if len(d["bars"]) >= k]
    if not ok:
        print(f"  T+{k}: 无可用样本 (全部右删失)")
        continue
    # B: 持有
    b_sum = 0.0
    for d in ok:
        cl = d["bars"][k - 1][4]
        b_sum += d["sign"] * (cl - d["ep"]) * d["q"] - d["notional"] * (FEE_RT + SLIP)
    a_sum = sum(d["net"] for d in ok)
    print(f"  T+{k:<2} n={len(ok)}  A实际离场Σ={a_sum:>+9.1f}   B扩大SL持有Σ={b_sum:>+9.1f}   "
          f"Δ(B−A)={b_sum - a_sum:>+9.1f}")

print("\n═══ 关键判据: 出场后价格是否继续朝不利方向走 (max close vs entry) ═══")
for d in recs:
    if not d["bars"]:
        continue
    closes = [b[4] for b in d["bars"]]
    best = max(closes) if d["dir"] == "LONG" else min(closes)
    worst = min(closes) if d["dir"] == "LONG" else max(closes)
    bp = 100 * (best - d["ep"]) / d["ep"]
    wp = 100 * (worst - d["ep"]) / d["ep"]
    hit_be = best >= d["ep"] if d["dir"] == "LONG" else best <= d["ep"]
    print(f"  {d['sym']:<9} 窗{d['n_bars']:>2}日  窗内最好收盘={bp:>+6.2f}%  最差={wp:>+6.2f}%  "
          f"{'✅ 曾回到入场价(可回本)' if hit_be else '🔴 全程未回到入场价'}")
