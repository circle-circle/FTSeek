#!/usr/bin/env python3
"""UE 午间影子扫描评估 (DEB-20260814 §13, 2026-08-14).

预注册判据 (§13.3, 用户裁 2026-08-14):
  样本: ≥10 个交易日 且 午间浮现 n≥30 (tradeable, 去重按 symbol+date)
  方向调整后前瞻收益: 12:30 ET → 当日收盘
    LONG: ret = (close - scan_price) / scan_price
    SHORT: ret = (scan_price - close) / scan_price
  通过 (开启午间 inject, 带全套既有守卫):
    命中率 (ret ≥ 0.5%) ≥ 55% 且 均值 ≥ +0.5%
  否则: 退役 (删 crontab 行 + midday_shadow_enabled=false), 零残留。

用法:
  python3 scripts/ue_midday_shadow_eval.py [--days 14] [--min-days 10] [--min-n 30]
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
_MIDDAY_PATH = _ROOT / "data" / "shadow" / "llm_universe_scan_midday.jsonl"

HIT_THRESHOLD = 0.005   # 命中: 方向调整后收益 ≥ +0.5%
PASS_HIT_RATE = 0.55    # 命中率 ≥ 55%
PASS_MEAN = 0.005       # 均值 ≥ +0.5%


def _fetch_close_prices(symbols_date):
    """批量取指定日期的收盘价 (Futu request_history_kline, 传 start/end 防旧数据陷阱)."""
    out = {}
    try:
        from futu import OpenQuoteContext, KLType, RET_OK
        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
        for (sym, date), _ in symbols_date.items():
            try:
                start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
                ret, df, _ = ctx.request_history_kline(
                    f"US.{sym}", ktype=KLType.K_DAY, max_count=60, start=start, end=date)
                if ret == RET_OK and len(df):
                    row = df[df["time_key"].str.startswith(date)]
                    if len(row):
                        out[(sym, date)] = float(row.iloc[-1]["close"])
            except Exception:
                pass
        ctx.close()
    except Exception as e:
        print(f"[warn] Futu 收盘价拉取失败: {e}", file=sys.stderr)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--min-days", type=int, default=10)
    ap.add_argument("--min-n", type=int, default=30)
    args = ap.parse_args()

    if not _MIDDAY_PATH.exists():
        print("午间 jsonl 不存在 (尚未有午间扫描)")
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    scans = []
    for line in _MIDDAY_PATH.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("type") != "scan" or r.get("scan_window") != "midday":
            continue
        try:
            if datetime.fromisoformat(r["scanned_at"]) < cutoff:
                continue
        except Exception:
            continue
        scans.append(r)

    if not scans:
        print(f"近{args.days}天无午间扫描记录")
        return 0

    # 汇总 tradeable 候选 (去重 symbol+date)
    seen = set()
    cands = []
    for sc in scans:
        for s in sc.get("surfaced", []):
            if s.get("direction") not in ("LONG", "SHORT"):
                continue
            key = (s.get("symbol"), sc.get("date"))
            if key in seen or not s.get("scan_price"):
                continue
            seen.add(key)
            cands.append({"symbol": s["symbol"], "date": sc["date"],
                          "direction": s["direction"], "scan_price": s["scan_price"],
                          "conf": s.get("confidence"),
                          "tier": s.get("self_tier"),
                          "verdict": (s.get("critique") or {}).get("verdict"),
                          "scanned_at": sc.get("scanned_at")})

    n_days = len({sc.get("date") for sc in scans})

    # 盘中守卫 (2026-08-14 Qoder): K_DAY 当日 bar 未成形时 "close"=最新价 → 判读偏早。
    #   ET 16:00 前运行时剔除当日候选, 收盘后重跑即可评。
    # 2026-09-20 时钟台账变种1清剿: 原裸 UTC-4 假钟冬令时快 1 小时 ⇒ ET 15:00-16:00
    #   被读成 ≥16 ⇒ 守卫失效 (当日未成形 bar 混入评估, 正是守卫要防的)。改 now_et()。
    try:
        from app.utils.timezone import now_et as _now_et_fn
        _now_et = _now_et_fn()
    except Exception:  # pragma: no cover - app 不可导入时的 ET 兜底
        from zoneinfo import ZoneInfo
        _now_et = datetime.now(ZoneInfo("America/New_York"))
    _today_et = _now_et.strftime("%Y-%m-%d")
    if _now_et.hour < 16:
        _skip = [c for c in cands if c["date"] == _today_et]
        if _skip:
            print(f"[注] 盘中运行: 剔除当日未成形样本 {len(_skip)} 笔 (收盘后重跑可评)")
            cands = [c for c in cands if c["date"] != _today_et]

    print(f"午间影子: {n_days}个交易日, tradeable候选 n={len(cands)}")

    if n_days < args.min_days or len(cands) < args.min_n:
        print(f"样本不足 (需 ≥{args.min_days}天 且 n≥{args.min_n}), 继续攒样")
        return 0

    closes = _fetch_close_prices({(c["symbol"], c["date"]): 1 for c in cands})
    rets = []
    for c in cands:
        close = closes.get((c["symbol"], c["date"]))
        if not close:
            continue
        base = c["scan_price"]
        ret = (close - base) / base if c["direction"] == "LONG" else (base - close) / base
        rets.append(ret)

    if not rets:
        print("无收盘价可评 (Futu 数据未就绪, 盘后再跑)")
        return 0

    hit = sum(1 for r in rets if r >= HIT_THRESHOLD)
    hit_rate = hit / len(rets)
    mean = sum(rets) / len(rets)
    print(f"可评 n={len(rets)} | 命中率(≥+0.5%)={hit_rate:.1%} | 均值={mean:+.2%}")
    print(f"判据: 命中率≥{PASS_HIT_RATE:.0%} 且 均值≥+{PASS_MEAN:.1%}")

    if hit_rate >= PASS_HIT_RATE and mean >= PASS_MEAN:
        print("✅ 判据通过 → 可开启午间 inject (带全套既有守卫: $5地板/借券检查/DROP过滤)")
    else:
        print("❌ 判据未通过 → 退役: 删 crontab 午间行 + midday_shadow_enabled=false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
