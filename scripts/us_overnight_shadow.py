#!/usr/bin/env python3
"""美股夜盘影子 (record-only, 零下单, 零生产库写入).

为什么存在: Futu 已支持夜盘 (数据面 overnight_* 快照 + get_us_overnight_rank),
但 **SIM 硬拒夜盘下单** (实测 "模拟交易暂不支持夜盘时段") ⇒ 现阶段只能攒读数,
不能攒成交. 12/6 交易所级夜盘 (Nasdaq GTH / NYSE Arca 21:00-04:00 ET) 上线后,
流动性来源会从券商 ATS 变成交易所, 前 12/6 读数与后 12/6 读数不可混算 —— 本影子
按日期分文件, 天然分开.

写什么 (每 tick 每标的追加一行；消费方若需每 symbol 最新值，须按 ts 去重):
  overnight_price/high/low/volume/turnover/change_rate/amplitude,
  prev_close / RTH close / after_price / spread / 时段边界快照.

用法:
  PYTHONPATH=. venv/bin/python3.11 scripts/us_overnight_shadow.py [--top 40] [--dry]
  --dry 仅不写文件，仍会拉起 OpenD 拉取只读行情。
开关: US_OVERNIGHT_SHADOW=false 关闭. 时段边界: US_OVERNIGHT_START (默认 20:00, 12/6 后改 21:00).
回滚: 删本脚本 + cron 行.
"""
import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))
OUT = ROOT / "data" / "shadow" / "us_overnight"
ET = ZoneInfo("America/New_York")
WATCH = ["US.NVDA", "US.SPY", "US.AAPL", "US.TSLA", "US.AMD", "US.AMZN"]


def _enabled():
    return str(os.getenv("US_OVERNIGHT_SHADOW", "true")).strip().lower() not in ("0", "false", "no")


def _now_et():
    return datetime.now(ET)


def _local_holidays():
    """纯本地假日常量，不调用 Futu 日历单例。"""
    from app.playbook.shadow_tracker import US_MARKET_HOLIDAYS_2026
    from app.utils.holiday_calendar import US_HOLIDAYS
    holidays = {date.fromisoformat(str(value)) for value in US_MARKET_HOLIDAYS_2026}
    return holidays | {value for value in US_HOLIDAYS if value.year != 2026}


def _is_trading_date(value, holidays):
    return value.weekday() < 5 and value not in holidays


def in_window(now_et, start_hhmm, holidays=None):
    """周日至周四晚→下一交易日 04:00 ET；周末/假日不采。"""
    try:
        sh, sm = [int(x) for x in start_hhmm.split(":")]
    except Exception:
        sh, sm = 20, 0
    holidays = _local_holidays() if holidays is None else holidays
    m = now_et.hour * 60 + now_et.minute
    start = sh * 60 + sm
    end = 4 * 60
    if m >= start:
        return _is_trading_date(now_et.date() + timedelta(days=1), holidays)
    if m < end:
        return _is_trading_date(now_et.date(), holidays)
    return False


def _session_date(now_et, start_hhmm):
    """跨午夜样本归到启动夜盘的 ET 日期，避免同一 session 拆两个文件。"""
    try:
        sh, sm = [int(x) for x in start_hhmm.split(":")]
    except Exception:
        sh, sm = 20, 0
    if now_et.hour * 60 + now_et.minute >= sh * 60 + sm:
        return now_et.date()
    return now_et.date() - timedelta(days=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    if not _enabled():
        print("[overnight_shadow] US_OVERNIGHT_SHADOW=false, 跳过")
        return 0

    start_hhmm = os.getenv("US_OVERNIGHT_START", "20:00")
    now = _now_et()
    if not in_window(now, start_hhmm):
        print("[overnight_shadow] 当前 %.1f%% 不在夜盘窗口 (%s-04:00 ET), 跳过" %
              (now.hour + now.minute / 60.0, start_hhmm))
        return 0

    from futu import OpenQuoteContext, RET_OK

    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPD_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPD_PORT", "11111")))
    try:
        ret, data = ctx.get_us_overnight_rank(count=max(1, min(args.top, 200)))
        if ret != RET_OK:
            print("[overnight_shadow] 夜盘榜失败: %s" % str(data)[:160])
            return 1
        all_count, df = data
        syms = [s for s in df["security"].tolist() if str(s).startswith("US.")]
        syms = list(dict.fromkeys(syms + WATCH))
        ret, snap = ctx.get_market_snapshot(syms)
        if ret != RET_OK:
            print("[overnight_shadow] 快照失败: %s" % str(snap)[:160])
            return 1
    finally:
        ctx.close()

    day = _session_date(now, start_hhmm).isoformat()
    rows = {}
    for _, r in snap.iterrows():
        px = float(r.get("overnight_price") or 0)
        if px <= 0:
            continue
        rows[r["code"]] = {
            "symbol": r["code"],
            "overnight_price": px,
            "overnight_high": float(r.get("overnight_high_price") or 0),
            "overnight_low": float(r.get("overnight_low_price") or 0),
            "overnight_volume": float(r.get("overnight_volume") or 0),
            "overnight_turnover": float(r.get("overnight_turnover") or 0),
            "overnight_change_rate": float(r.get("overnight_change_rate") or 0),
            "overnight_amplitude": float(r.get("overnight_amplitude") or 0),
            "prev_close": float(r.get("prev_close_price") or 0),
            "rth_close": float(r.get("last_price") or 0),
            "after_price": float(r.get("after_price") or 0),
            "spread": float(r.get("price_spread") or 0),
            "rth_volume": float(r.get("volume") or 0),
            "ts": now.isoformat(timespec="seconds"),
            "session_date": day,
            "window_start": start_hhmm,
        }

    if args.dry:
        print("[overnight_shadow] dry: %d 标的 (榜 %d 只), 示例 %s" %
              (len(rows), all_count, json.dumps(list(rows.values())[:1], ensure_ascii=False)[:300]))
        return 0

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / (day + ".jsonl")
    seen = 0
    bad_rows = 0
    if path.exists():
        with path.open() as fh:
            for line in fh:
                try:
                    seen += 1 if json.loads(line).get("symbol") else 0
                except Exception:
                    bad_rows += 1
                    continue
    with path.open("a", encoding="utf-8") as fh:
        for rec in rows.values():
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("[overnight_shadow] 写入 %d 行 -> %s (榜共 %d 只, 历史行 %d, 坏行 %d)" %
          (len(rows), path, all_count, seen, bad_rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())