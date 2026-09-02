#!/usr/bin/env python3
"""赢家隔夜影子结算器 (RFC-074, 2026-09-02, record-only).

对 data/shadow/winner_overnight_shadow.jsonl 中未结算的 arm 行, 用次日 1min
replay_bars 结算"带 SL 持一夜"反事实臂:
  - 次日开盘跳空越过 SL → 开盘价出场 (gap-through)
  - 盘中 SL 触碰 → SL 价出场 (悲观序, 不假设更优出场)
  - 未触 → 持到次日收盘 (EOD 落袋, 与既有赢家臂语义一致)
结算 = 反事实出场价相对 arm_price 的增量 × 数量 × 方向符号 (净口径基线:
费用结构同实际平仓 — 多持一夜不增加往返费, delta 为纯价差)。

B16 判据样本口径 (2026-09-02 核查补): arm 点挂 PRE_CLOSE_EXIT 触发处,
非豁免亏损臂 (LIVE 水下/到期 SIM 仓) 也会落行 (近30日实测 11赢/1输≈8%污染) —
DEB 问题定义是 "cohort A 盈利仓持夜", 汇总的 n/均值/胜率/by_outcome 只按
盈利臂 (arm_profit_pct>0, 毛口径, 同 PRE_CLOSE skip 判定) 取样; 亏损臂照
结算仅留对照 (loser_n), 不入判据。min_n=20 = 盈利臂结算行数。

SL 口径注记: stop_loss_at_arm = 初始 SL (meta.stop_loss, 退出链不随
breakeven/trail 收紧更新) — 真实赢家仓 arm 时刻生效 SL 更紧, 反事实"带SL
持夜"的 SL_touch 读数因此偏少 (初始 SL 口径), 判据侧知悉。

数据源: data/replay_bars/{SYM}_{date}.json (position_path_collector 盘后落).
次日交易日 = holiday_calendar.nth_trading_day_after; bar 缺失 → 保留
unsettled 待补算 (静默死亡族纪律: 每次运行打印未结算积压数)。

用法: venv/bin/python scripts/settle_winner_overnight_shadow.py [--days N]
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BAR_DIR = ROOT / "data" / "replay_bars"
SHADOW = ROOT / "data" / "shadow" / "winner_overnight_shadow.jsonl"
OUT = ROOT / "data" / "shadow" / "winner_overnight_shadow_latest.json"


def _atomic_write(path: Path, text: str):
    """temp+rename 原子写 — 06:25 cron 与 15:52 arm 追加写无并发窗口, 防半写."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def next_trading_day(d: str):
    from app.utils.holiday_calendar import nth_trading_day_after
    from datetime import date
    y, m, dd = d.split("-")
    nd = nth_trading_day_after("US", date(int(y), int(m), int(dd)), 1)
    return nd.isoformat() if nd else None


def load_1m(symbol: str, day: str):
    """1min bars: list of [ts, o, h, l, c]; None 表示缺失."""
    p = BAR_DIR / f"{symbol.replace('.', '_')}_{day}.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        bars = data.get("bars", []) if isinstance(data, dict) else data
    except Exception:
        return None
    return bars if len(bars) >= 30 else None


def settle_row(row):
    """返回 (ok, fields). ok=False 表示 bar 缺失."""
    sym = row["symbol"]
    nxt = next_trading_day(row["arm_date"])
    if not nxt:
        return False, {}
    bars = load_1m(sym, nxt)
    if not bars:
        return False, {}
    entry = float(row["entry_price"])
    sl = float(row.get("stop_loss_at_arm") or 0)
    q = float(row.get("quantity") or 0)
    sign = 1.0 if row["direction"] == "LONG" else -1.0
    ap = float(row["arm_price"])
    fact_price = None
    tag = ""
    if sl > 0:
        o0 = bars[0][1]
        if (row["direction"] == "LONG" and o0 <= sl) or \
                (row["direction"] == "SHORT" and o0 >= sl):
            fact_price, tag = o0, "SL_gap"
        else:
            for b in bars:
                if row["direction"] == "LONG" and b[3] <= sl:
                    fact_price, tag = sl, "SL_touch"
                    break
                if row["direction"] == "SHORT" and b[2] >= sl:
                    fact_price, tag = sl, "SL_touch"
                    break
    if fact_price is None:
        fact_price, tag = bars[-1][4], "EOD_next_day"
    delta = (fact_price - ap) * q * sign
    return True, {
        "settle_date": nxt,
        "settle_outcome": tag,
        "settle_price": round(fact_price, 4),
        "overnight_delta": round(delta, 2),
        "settled": True,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="只结算近 N 天 arm 行 (0=全部)")
    args = ap.parse_args(argv)
    if not SHADOW.exists():
        print("无影子文件, 退出 (尚未产生 arm 行)")
        return 0
    rows = [json.loads(l) for l in SHADOW.open() if l.strip()]
    pending = [r for r in rows if not r.get("settled") and r.get("type") == "arm"]
    if args.days > 0:
        from datetime import date, timedelta
        cut = (date.today() - timedelta(days=args.days)).isoformat()
        pending = [r for r in pending if r.get("arm_date", "") >= cut]
    n_set = 0
    for r in rows:
        if r.get("settled") or r.get("type") != "arm":
            continue
        if args.days > 0 and r.get("arm_date", "") < cut:
            continue
        ok, fields = settle_row(r)
        if ok:
            r.update(fields)
            n_set += 1
    _atomic_write(SHADOW, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    settled = [r for r in rows if r.get("settled")]
    # B16 判据样本 = 盈利臂 (docstring 口径注记); 亏损臂仅对照
    winners = [r for r in settled if float(r.get("arm_profit_pct", 0) or 0) > 0]
    losers = [r for r in settled if float(r.get("arm_profit_pct", 0) or 0) <= 0]
    tot = sum(r.get("overnight_delta", 0) for r in winners)
    wins = sum(1 for r in winners if r.get("overnight_delta", 0) > 0)
    summary = {
        "settled_n": len(settled),
        "winner_n": len(winners),
        "loser_n": len(losers),
        "pending_n": len([r for r in rows if not r.get("settled")]),
        "sum_overnight_delta": round(tot, 2),
        "win_rate": round(wins / len(winners), 3) if winners else None,
        "by_outcome": {},
    }
    from collections import Counter
    summary["by_outcome"] = dict(Counter(r.get("settle_outcome") for r in winners))
    _atomic_write(OUT, json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"本次结算 {n_set} 笔 | 盈利臂 {len(winners)} (B16样本) / 亏损臂 {len(losers)} (对照) "
          f"| 未结算积压 {summary['pending_n']} | 盈利臂合计delta {tot:+.2f} | 胜率 {summary['win_rate']}")
    print(f"by_outcome(盈利臂): {summary['by_outcome']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
