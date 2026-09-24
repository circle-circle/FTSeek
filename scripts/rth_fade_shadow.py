#!/usr/bin/env python3
"""RTH fade 影子 (D 项转正前的攒样器, 2026-09-23 用户批「按建议执行」) —— **零下单, 只记录**.

## 预注册判据 (读前写死; 读数出来前不得改本段)

规则 R: **财报票 + RTH 开盘跳空 ≥ +3.0% ⇒ 开盘虚拟做空, 当日 RTH 收盘虚拟平仓** (单价 $5,000, 费 0.53%).
  为什么是这条: 回放 (scripts/rth_fade_replay.py, 2026-08-28..09-22, 12 RTH 日) ——
    顺跳空(延续) 净 −$73.16/笔 (n=84, 胜 39%) vs **fade 做空侧 +$48.01/笔 (n=50, 胜 62%)**,
    安慰剂 (同标的非财报日, 同 |跳空| 档) −$18.06/笔 ⇒ 增量 +$66.06; 市场基准日内均值 −0.00% (非 β).
  为什么只做空侧: 做多侧 (跳空跌 → 做多) 回放 −$32.22/笔 ⇒ 现役纯多头通道**不可做**。
  为什么不直接上线: 50 笔/12 日、剔最强日收益砍半 (+$48→+$23.5)、未计流动性/价差、
    3 个强化趋势日单笔 −$300~−$420 (尾部在空侧) ⇒ 先攒真数据。

判据 (攒样 → 转正):
  - 样本门槛: **≥ 15 个 RTH 交易日 且 ≥ 30 笔** (达不到就继续攒, 不提前裁决)
  - 转正条件 (三条全满足): ① 净/笔 > 0 ② 胜率 ≥ 55% ③ **留一口** (剔除最强单日) 净/笔 ≥ 0
  - 否决条件 (任一即否): 净/笔 < 0 / 留一口翻负 / 空侧单日 ≤ −$400 的日数 ≥ 3 (尾部失控)
  - 期间**不下任何真单**; 只写 data/rth_fade_shadow.jsonl (本脚本唯一产物)
  - 记录字段含 gap/entry/exit/ret/net + 市值/价格上下文 ⇒ 事后可加闸复算, 不需重抓

## 口径
入场价 = 当日 RTH **开盘价** (09:30 即可得, 无未来信息); 出场价 = 当日 RTH **收盘价**。
跳空 = 开盘/前收 − 1 (前收取日K上一交易日收盘)。做空收益 = (开盘−收盘)/开盘 (分母=卖出价)。
票源 = Finnhub 当日日历 (bmo/amc) ∪ futu 日历经 rows; bmo → **当日** RTH 日, amc → **次日** RTH 日。
跑法: 每日 ET 收盘后一次 (cron 04:10 CST = 16:10 ET), `--date` 默认最近已收盘 ET 日。
     `--dry` 只打印不落盘; `--push` 附一行摘要 (默认静默, 避免刷屏)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env.us", override=True)

OUT = ROOT / "data" / "rth_fade_shadow.jsonl"
SIZE, FEE, GAP_MIN = 5000.0, float(os.getenv("FEE_PCT", "0.53")), 3.0
# 借券费参数 (2026-09-24 RFC-060 净额口径补全):
#   BORROW_FEE_RATE_PCT: 年化借券费率 (%). 缺省 1.0 = ESB (easy-to-borrow) 保守值;
#     HTB 票通常 5–50+%, 可 env 覆写做敏感性分析.
#   BORROW_FEE_BASIS: 计息基准天数, 缺省 365 (与 fee_model 一致).
#   BORROW_FEE_MIN_DAYS: 最小计费天数, 缺省 0 (券商自然日惯例: 日内=0天=不收费).
#     设 1 可模拟「最低 1 日借券费」的经纪商口径.
# 注意: RTH fade 影子严格日内 (开盘→收盘, ~6.5h), int(6.5/24)=0 ⇒ 默认口径下
#   borrow_fee_est 恒为 $0; 此字段为毕业裁决净额口径完整性 + 未来多日持仓扩展预留.
BORROW_FEE_RATE_PCT = float(os.getenv("BORROW_FEE_RATE_PCT", "1.0"))
BORROW_FEE_BASIS = int(os.getenv("BORROW_FEE_BASIS", "365"))
BORROW_FEE_MIN_DAYS = int(os.getenv("BORROW_FEE_MIN_DAYS", "0"))
RTH_HOLD_HOURS = 6.5  # 09:30→16:00 ET
# 2026-09-24 时钟病灶变种1收口: 原 timezone(timedelta(hours=-4)) 是永久EDT假钟,
# 11/1 转 EST 后 et_today() 归日错位。zoneinfo 纯本地 (不触 futu/OpenD), DST 正确,
# 与 app/utils/timezone.py R-13 fallback 链同款正例。
ET = ZoneInfo("America/New_York")


def estimate_borrow_fee_usd(size_usd: float, entry_price: float, direction: str,
                             hold_hours: float = RTH_HOLD_HOURS) -> dict:
    """影子回放借券费估算 (纯本地, 不触 futu).

    复用 app.execution.fee_model.accrue_borrow_fee 的计算内核, 但费率走参数化
    (影子回放无法查历史实时借券费率, 用 env/常量保守值; 输出字段 `borrow_fee_rate_used`
    留痕, 毕业裁决可据此做敏感性分析).

    日内持仓 (hold_hours < 24 ⇒ int(hold_days)=0) 按券商自然日惯例收费 $0;
    BORROW_FEE_MIN_DAYS 可覆写下限 (模拟最低 1 日收费口径).
    """
    if str(direction or "").upper() not in ("SHORT", "SELL"):
        return {"borrow_fee_est": 0.0, "borrow_fee_rate_used": None,
                "borrow_fee_hold_days": 0, "borrow_fee_min_days_applied": False}
    if entry_price <= 0 or size_usd <= 0:
        return {"borrow_fee_est": 0.0, "borrow_fee_rate_used": BORROW_FEE_RATE_PCT,
                "borrow_fee_hold_days": 0, "borrow_fee_min_days_applied": False}
    qty = size_usd / entry_price
    hold_days_raw = hold_hours / 24.0
    hold_days = max(hold_days_raw, BORROW_FEE_MIN_DAYS / 1.0)
    # int 截断: 日内=0, 隔夜从 1 起算 (fee_model 内核同语义)
    days_int = int(hold_days)
    min_applied = bool(BORROW_FEE_MIN_DAYS > 0 and days_int > int(hold_days_raw))
    if days_int <= 0 or BORROW_FEE_RATE_PCT <= 0:
        return {"borrow_fee_est": 0.0, "borrow_fee_rate_used": BORROW_FEE_RATE_PCT,
                "borrow_fee_hold_days": round(hold_days, 4),
                "borrow_fee_min_days_applied": min_applied}
    fee_usd = round(qty * entry_price * (BORROW_FEE_RATE_PCT / 100.0) * days_int / BORROW_FEE_BASIS, 4)
    return {"borrow_fee_est": fee_usd, "borrow_fee_rate_used": BORROW_FEE_RATE_PCT,
            "borrow_fee_hold_days": round(hold_days, 4),
            "borrow_fee_min_days_applied": min_applied}


def et_today() -> str:
    return datetime.now(ET).date().isoformat()


def prev_trading_day(d: str, days) -> str | None:
    older = [x for x in days if x < d]
    return older[-1] if older else None


def day_bars(ctx, code: str, start: str, end: str) -> dict:
    from futu import KLType
    from scripts.replay_ext_entry_sources import _kline_cached
    out = {}
    for b in _kline_cached(ctx, code, start, end, KLType.K_DAY) or []:
        t = str(b.get("t") or "")[:10]
        if t and b.get("o") and b.get("c"):
            out[t] = {"o": float(b["o"]), "c": float(b["c"]), "h": float(b.get("h") or 0), "l": float(b.get("l") or 0)}
    return out


def finnhub_rows(day: str) -> list:
    import requests
    key = os.getenv("FINNHUB_API_KEY")
    if not key:
        return []
    try:
        r = requests.get("https://finnhub.io/api/v1/calendar/earnings",
                         params={"from": day, "to": day, "token": key}, timeout=30)
        return r.json().get("earningsCalendar", []) or []
    except Exception as exc:
        print(f"[rth_fade] finnhub 拉取失败 {day}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return []


def futu_rows(start: str, end: str) -> list:
    from futu import OpenQuoteContext, RET_OK
    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPEND_PORT", "11111")))
    try:
        ret, data = ctx.get_earnings_calendar(market="US", begin_date=start, end_date=end)
        if ret != RET_OK or data is None:
            return []
        return [{"symbol": str(r.get("security")).replace("US.", ""), "date": str(r.get("earnings_date"))[:10],
                 "hour": {"BEFORE": "bmo", "AFTER": "amc"}.get(str(r.get("pub_type")), None),
                 "src": "futu"} for _, r in data.iterrows()]
    finally:
        ctx.close()


def collect_tickets(day: str) -> list:
    """当日 + 前一日的财报票 (bmo/amc) —— 合并两源, 按 (symbol, date) 去重, hora 取首个非空。"""
    prev = (datetime.strptime(day, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
    acc: dict = {}
    for r in finnhub_rows(prev) + finnhub_rows(day):
        sym = str(r.get("symbol") or "").upper()
        if not sym:
            continue
        d10 = str(r.get("date"))[:10]
        acc.setdefault((sym, d10), {"symbol": sym, "date": d10,
                                                  "hour": str(r.get("hour") or "").lower() or None, "src": "finnhub"})
    for r in futu_rows(prev, day):
        k = (r["symbol"], r["date"])
        cur = acc.setdefault(k, {"symbol": r["symbol"], "date": r["date"], "hour": None, "src": "futu"})
        if not cur.get("hour") and r.get("hour"):
            cur["hour"] = r["hour"]
            cur["src"] = "futu"
    return [v for v in acc.values() if v["hour"] in ("bmo", "amc")]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="RTH fade 影子 (零下单)")
    ap.add_argument("--date", default=None, help="ET 日期 YYYY-MM-DD (默认最近已收盘 ET 日)")
    ap.add_argument("--dry", action="store_true", help="只打印不落盘")
    ap.add_argument("--push", action="store_true", help="附一行摘要推送 (默认静默)")
    args = ap.parse_args(argv)

    day = args.date or et_today()
    tickets = collect_tickets(day)
    if not tickets:
        print(f"[rth_fade] {day} 无财报票 (两源皆空) ⇒ 不记录")
        return 0

    from futu import OpenQuoteContext
    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPEND_PORT", "11111")))
    recs, skipped = [], []
    try:
        for t in tickets:
            start = (datetime.strptime(t["date"], "%Y-%m-%d").date() - timedelta(days=8)).isoformat()
            end = (datetime.strptime(day, "%Y-%m-%d").date() + timedelta(days=2)).isoformat()
            bars = day_bars(ctx, f"US.{t['symbol']}", start, end)
            if not bars:
                skipped.append((t["symbol"], "无日K"))
                continue
            days = sorted(bars)
            if t["hour"] == "bmo":
                rth = t["date"] if t["date"] in bars else None
            else:
                rth = next((d for d in days if d > t["date"]), None)
            if not rth or rth > day:
                skipped.append((t["symbol"], f"RTH日未到({rth})"))
                continue
            pc = prev_trading_day(rth, days)
            if not pc:
                skipped.append((t["symbol"], "无前收"))
                continue
            o, c, prev_close = bars[rth]["o"], bars[rth]["c"], bars[pc]["c"]
            gap = (o / prev_close - 1) * 100
            if gap < GAP_MIN:
                skipped.append((t["symbol"], f"跳空 {gap:+.2f}% < {GAP_MIN}%"))
                continue
            ret = (o - c) / o * 100                      # 做空: 开盘卖 → 收盘买回 (主口径)
            # 平行臂 (2026-09-23 用户裁「同意」): 同票同日加一条 −3.5% 止损读数。
            #   依据 scripts/rth_fade_protection_arms.py: 止损 −3.5% 净/笔 +$101.65 (vs 无保护 +$75.80),
            #   而 −1.5% 把胜率打到 35% (被噪音打掉) ⇒ 若将来上保险, 宽止损才对。
            #   **决策口径不变** (主口径仍是收盘平), 这条只作对照, 防事后挑臂。
            sl = 3.5
            sl_hit = bars[rth]["h"] >= o * (1 + sl / 100)
            ret_sl = -sl if sl_hit else ret
            # 借券费估算 (2026-09-24 RFC-060: 净额口径补全, 空侧毕业裁决前置项):
            #   纯本地, 费率参数化 (BORROW_FEE_RATE_PCT env); 日内 int(hold_days)=0 ⇒
            #   默认口径下 borrow_fee_est 恒 $0 (券商自然日计息惯例).
            #   向后兼容: 不改既有字段语义, 只新增字段; 多侧(若未来扩展)恒 0 但字段在.
            bf = estimate_borrow_fee_usd(SIZE, o, "SHORT", RTH_HOLD_HOURS)
            bf_est = bf["borrow_fee_est"]
            net_main = round(SIZE * (ret - FEE) / 100, 2)
            net_sl35 = round(SIZE * (ret_sl - FEE) / 100, 2)
            # 止损臂借券费: 若触发, 持仓更短 (开盘→触价), 但保守仍用全日口径 (不减少费用)
            # ⇒ 净额口径不会高估收益. 若未触发, 与主口径同.
            recs.append({"date": day, "rth_day": rth, "symbol": t["symbol"], "hour": t["hour"],
                         "src": t["src"], "gap_pct": round(gap, 3), "entry": o, "exit": c,
                         "prev_close": prev_close, "ret_pct": round(ret, 4),
                         "net_usd": net_main,
                         "ret_pct_sl35": round(ret_sl, 4), "sl35_hit": bool(sl_hit),
                         "net_usd_sl35": net_sl35,
                         "high": bars[rth]["h"], "low": bars[rth]["l"], "size_usd": SIZE, "fee_pct": FEE,
                         # --- 新增字段 (RFC-060 借券费净额口径) ---
                         "borrow_fee_est": bf_est,
                         "borrow_fee_rate_used": bf["borrow_fee_rate_used"],
                         "borrow_fee_hold_days": bf["borrow_fee_hold_days"],
                         "borrow_fee_min_days_applied": bf["borrow_fee_min_days_applied"],
                         "pnl_net_after_borrow": round(net_main - bf_est, 2),
                         "pnl_net_after_borrow_sl35": round(net_sl35 - bf_est, 2)})
    finally:
        ctx.close()

    print(f"[rth_fade] {day} 票 {len(tickets)} ⇒ 入账 {len(recs)} 笔 | 略过 {len(skipped)}")
    for s, why in skipped[:12]:
        print(f"   略过 {s}: {why}")
    for r in recs:
        bf_note = f" 借券${r['borrow_fee_est']:.2f}" if r['borrow_fee_est'] > 0 else ""
        print(f"   {r['symbol']:<6} [{r['hour']}] 跳空 {r['gap_pct']:+6.2f}% 开盘 {r['entry']:.2f} → "
              f"收盘 {r['exit']:.2f} 收益 {r['ret_pct']:+6.2f}% 净 ${r['net_usd']:+7.2f}"
              f" 含借券${r['borrow_fee_est']:.2f}⇒净净额${r['pnl_net_after_borrow']:+7.2f}"
              f"  ‖ 平行臂(−3.5%止) 净 ${r['net_usd_sl35']:+7.2f}"
              f"{' [触发]' if r['sl35_hit'] else ''}")
    if recs and not args.dry:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        # 幂等 (2026-09-23 复查批 P1-5): 按 (symbol, rth_day) 去重 —— 同日重跑若直接
        # 追加会翻倍样本, 下游毕业裁定器 (rth_fade_graduation_check.py) load 全量行,
        # 笔数/胜率/净笔全部被污染。已在册的键跳过, 只追加新键。
        seen = set()
        if OUT.exists():
            with OUT.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                        seen.add((row.get("symbol"), row.get("rth_day")))
                    except json.JSONDecodeError:
                        continue
        fresh = [r for r in recs if (r["symbol"], r["rth_day"]) not in seen]
        if fresh:
            with OUT.open("a") as f:
                for r in fresh:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            print(f"[rth_fade] 已追加 {len(fresh)} 行 → {OUT}")
        if len(fresh) < len(recs):
            print(f"[rth_fade] 幂等去重跳过 {len(recs) - len(fresh)} 行 (已在册)")
    if args.push and recs:
        try:
            from app.push import push_text_safe
            from app.push.message import Level, Category
            tot = sum(r["net_usd"] for r in recs)
            body = (f"**RTH fade 影子 · {day}**\n\n入账 {len(recs)} 笔 虚拟净 ${tot:+.0f} "
                    f"(口径: 跳空≥+{GAP_MIN}% 财报票 开盘做空→收盘, 费 {FEE}%)\n\n"
                    + "\n".join(f"- {r['symbol']} 跳空 {r['gap_pct']:+.2f}% → 净 ${r['net_usd']:+.2f}"
                                for r in recs)
                    + "\n\n_零下单, 仅记录; 转正判据见 scripts/rth_fade_shadow.py docstring_")
            ok = push_text_safe(body, level=Level.NORMAL, category=Category.REVIEW_DAILY,
                                title=f"📉 RTH fade 影子 · {day}")
            print(f"[rth_fade] push {'已推送' if ok else '失败'} · 双发/主通道由 PushHub 路由")
        except Exception as exc:
            print(f"[rth_fade][warn] push 失败(不阻断): {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())