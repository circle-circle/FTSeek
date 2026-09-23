#!/usr/bin/env python3
"""财报事件**全量路径**影子 (beat + miss 都记) —— 补齐"做空侧数据盲区", 零下单.

## 为什么单独一个 (2026-09-23 用户「盘前和盘后 富途可以执行做空, 按建议执行」)
`scripts/rth_fade_shadow.py` 只记 **gap≥+3%** 的票, 而它的票源又只来自"通过超预期判据"的池
⇒ 项目里**没有任何 miss(不及预期) 票的价格路径** (实测: 118 条事件全是 beat)。
要为空侧财报规则取证, 缺的正是这批数据。

## 预注册 (写死; 读数出来前不得改)
- 样本: 当日 + 前一日的**全部**财报事件 (bmo/amc, 两源并集 futu ∪ Finnhub), **不看 beat/miss**
- 记录: 超预期幅度(有则记) / 前收 / RTH 开高低收 / 跳空% / **计划方向** / 虚拟净额 (每 $5,000, 费 0.53%)
- **计划方向判据 (幅度判据, 2026-09-23 实证: 财报好坏与方向零相关 r=-0.006)**
    gap ≥ +3.0%               ⇒ SHORT  (依据: +$75.80/笔, 胜 65%, n=26)
    +0.5% ≤ gap ≤ +2.0%       ⇒ LONG   (依据: +$23.09/笔, n=19)
    其它                       ⇒ NONE   (无证据, 只记录)
- **零下单**: 唯一产物 `data/earnings_direction_path_shadow.jsonl`; 不进任何交易路径
- 判据读数门槛 (转正需另立 RFC): 每方向 ≥30 笔 且 每方向净/笔 > 0
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env.us", override=True)

from scripts.rth_fade_shadow import (  # noqa: E402  复用取数 (不新增一套)
    day_bars, et_today, finnhub_rows, futu_rows, prev_trading_day)

OUT = ROOT / "data" / "earnings_direction_path_shadow.jsonl"
SIZE = 5000.0
FEE = float(os.getenv("FEE_PCT", "0.53"))
SHORT_MIN, LONG_LO, LONG_HI = 3.0, 0.5, 2.0


def events(day: str) -> list:
    """当日 + 前一日的全部财报事件 (两源并集, 带 epsActual/epsEstimate)。"""
    prev = (datetime.strptime(day, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
    acc: dict = {}
    for r in finnhub_rows(prev) + finnhub_rows(day):
        sym = str(r.get("symbol") or "").upper()
        d10 = str(r.get("date") or "")[:10]
        if not sym or not d10:
            continue
        acc[(sym, d10)] = {
            "symbol": sym, "date": d10, "hour": (str(r.get("hour") or "").lower() or None),
            "src": "finnhub", "eps_actual": r.get("epsActual"), "eps_estimate": r.get("epsEstimate"),
        }
    for r in futu_rows(prev, day):
        k = (r["symbol"], r["date"])
        cur = acc.setdefault(k, {"symbol": r["symbol"], "date": r["date"], "hour": None,
                                 "src": "futu", "eps_actual": None, "eps_estimate": None})
        if not cur.get("hour") and r.get("hour"):
            cur["hour"] = r["hour"]
    return [v for v in acc.values() if v["hour"] in ("bmo", "amc")]


def plan_direction(gap: float) -> str:
    if gap >= SHORT_MIN:
        return "SHORT"
    if LONG_LO <= gap <= LONG_HI:
        return "LONG"
    return "NONE"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="财报事件全量路径影子 (零下单)")
    ap.add_argument("--date", default=None, help="ET 日期 YYYY-MM-DD (默认今日 ET)")
    ap.add_argument("--dry", action="store_true", help="只打印不落盘")
    args = ap.parse_args(argv)

    day = args.date or et_today()
    evs = events(day)
    if not evs:
        print(f"[ern_dir] {day} 无财报事件 (两源皆空) ⇒ 不记录")
        return 0

    from futu import OpenQuoteContext
    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPEND_PORT", "11111")))
    recs, skipped = [], []
    try:
        for e in evs:
            start = (datetime.strptime(e["date"], "%Y-%m-%d").date() - timedelta(days=8)).isoformat()
            end = (datetime.strptime(day, "%Y-%m-%d").date() + timedelta(days=1)).isoformat()
            bars = day_bars(ctx, f"US.{e['symbol']}", start, end)
            if not bars:
                skipped.append((e["symbol"], "无日K"))
                continue
            days = sorted(bars)
            rth = e["date"] if (e["hour"] == "bmo" and e["date"] in bars) else \
                next((d for d in days if d > e["date"]), None)
            if not rth or rth > day:
                skipped.append((e["symbol"], f"RTH日未到({rth})"))
                continue
            pc = prev_trading_day(rth, days)
            if not pc:
                skipped.append((e["symbol"], "无前收"))
                continue
            o, c, prev_close = bars[rth]["o"], bars[rth]["c"], bars[pc]["c"]
            gap = (o / prev_close - 1) * 100
            long_ret = (c / o - 1) * 100                 # 做多: 开盘买→收盘卖
            short_ret = (1 - c / o) * 100                # 做空: 开盘卖→收盘买回
            act, est = e.get("eps_actual"), e.get("eps_estimate")
            try:
                _est = abs(float(est))
                # |est| < 0.01 时百分比无意义 (实测 ANAB est≈0 ⇒ surp +164738% 假象) ⇒ 记 None
                surp = ((float(act) - float(est)) / _est * 100) if _est >= 0.01 else None
            except (TypeError, ValueError):
                surp = None
            label = None if surp is None else ("beat" if surp > 0 else ("miss" if surp < 0 else "inline"))
            d = plan_direction(gap)
            ret = short_ret if d == "SHORT" else (long_ret if d == "LONG" else None)
            recs.append({
                "date": day, "rth_day": rth, "symbol": e["symbol"], "hour": e["hour"], "src": e["src"],
                "surp_pct": None if surp is None else round(surp, 2), "beat_miss": label,
                "prev_close": prev_close, "open": o, "high": bars[rth]["h"], "low": bars[rth]["l"],
                "close": c, "gap_pct": round(gap, 3), "plan_dir": d,
                "long_ret_pct": round(long_ret, 4), "short_ret_pct": round(short_ret, 4),
                "plan_net_usd": None if ret is None else round(SIZE * (ret - FEE) / 100, 2),
                "size_usd": SIZE, "fee_pct": FEE,
            })
    finally:
        ctx.close()

    for s, why in sorted(skipped):
        print(f"   略过 {s}: {why}")
    for r in sorted(recs, key=lambda x: -x["gap_pct"]):
        surp = "—" if r["surp_pct"] is None else f"{r['surp_pct']:+.1f}%"
        net = "—" if r["plan_net_usd"] is None else f"${r['plan_net_usd']:+.0f}"
        print(f"   {r['symbol']:<6} [{r['hour']}] {str(r['beat_miss'] or '?'):<5} "
              f"超预期 {surp:>7} 跳空 {r['gap_pct']:+6.2f}% 计划 {r['plan_dir']:<5} 净 {net}")
    if recs and not args.dry:
        seen = set()
        if OUT.exists():                                    # 幂等: 同日同票不重复落盘
            for line in OUT.read_text().splitlines():
                if line.strip():
                    try:
                        x = json.loads(line)
                        seen.add((x.get("rth_day"), x.get("symbol")))
                    except Exception:
                        pass
        new = [r for r in recs if (r["rth_day"], r["symbol"]) not in seen]
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with OUT.open("a") as f:
            for r in new:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"[ern_dir] 新增 {len(new)} 行 (已存在跳过 {len(recs) - len(new)})")
    nb = sum(1 for r in recs if r["beat_miss"] == "beat")
    nm = sum(1 for r in recs if r["beat_miss"] == "miss")
    ns = sum(1 for r in recs if r["plan_dir"] == "SHORT")
    nl = sum(1 for r in recs if r["plan_dir"] == "LONG")
    print(f"[ern_dir] {day} 事件 {len(recs)} (beat {nb}/miss {nm}/未知 {len(recs)-nb-nm}) "
          f"⇒ 计划 SHORT {ns} / LONG {nl} | 略过 {len(skipped)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())