#!/usr/bin/env python3
"""E1 前瞻影子臂采集器 (DEB-20260815 §11.2) — 每日盘后跑, 幂等.

职责 (单脚本两阶段):
  阶段1 回填: 对昨日及之前已记录、尚无 next_open_price 的信号, 补齐"次一交易日开盘价"。
  阶段2 采集: 记录当日 DA+UE 信号 (symbol/direction/strength/signal_day_open)。
结算口径 (§14.4.1): 影子成交价 = 信号次一交易日开盘价 (不可得→首根可成交bar close);
  本采集器只记价, 不结算 PnL; 结算在 GATE 1 由裁决脚本统一做 (防中途改口径)。
存储: data/e1_shadow_signals.jsonl, 按 (symbol, trade_date, direction) 去重幂等。
回滚: 删本文件 + jsonl + cron 步骤。
"""
import json, sqlite3, sys, time
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path("/opt/ftseek/FTSeek-US")
SNAP_DB = REPO / "data" / "signal_snapshots.db"
OUT = REPO / "data" / "e1_shadow_signals.jsonl"
CACHE_DIR = REPO / "data" / "replay_bars"


def load_records():
    recs = {}
    if OUT.exists():
        for line in OUT.read_text().splitlines():
            try:
                r = json.loads(line)
                recs[(r["symbol"], r["trade_date"], r["direction"])] = r
            except Exception:
                pass
    return recs


def save_records(recs):
    with OUT.open("w") as f:
        for r in recs.values():
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def get_daily(sym, date):
    """拉某symbol覆盖date的日线缓存."""
    for p in CACHE_DIR.glob(f"daily_{sym.replace('.', '_')}_*.json"):
        try:
            bars = json.loads(p.read_text())
            days = {b[0]: b for b in bars}
            if date in days:
                return days, [b[0] for b in bars]
        except Exception:
            continue
    # 未命中缓存 → 现拉
    start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=15)).strftime("%Y-%m-%d")
    end = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=15)).strftime("%Y-%m-%d")
    cache = CACHE_DIR / f"daily_{sym.replace('.', '_')}_{start}_{end}.json"
    try:
        from futu import OpenQuoteContext, KLType, RET_OK, AuType
        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
        try:
            for _ in (1, 2):
                ret, data, _ = ctx.request_history_kline(
                    sym, ktype=KLType.K_DAY, start=start, end=end,
                    max_count=400, autype=AuType.NONE)
                if ret == RET_OK and data is not None and len(data) > 0:
                    bars = [[str(r["time_key"])[:10], float(r["open"]), float(r["high"]),
                             float(r["low"]), float(r["close"])] for _, r in data.iterrows()]
                    bars.sort(key=lambda b: b[0])
                    cache.write_text(json.dumps(bars))
                    days = {b[0]: b for b in bars}
                    return days, [b[0] for b in bars]
                time.sleep(3)
        finally:
            ctx.close()
    except Exception:
        pass
    return None, None


def main():
    recs = load_records()

    # 阶段1: 回填 next_open_price
    filled = 0
    for key, r in recs.items():
        if r.get("next_open_price") is not None:
            continue
        days, day_list = get_daily(r["symbol"], r["trade_date"])
        if not days or r["trade_date"] not in day_list:
            continue
        idx = day_list.index(r["trade_date"])
        if idx + 1 < len(day_list):
            next_day = day_list[idx + 1]
            r["next_open_price"] = days[next_day][1]
            r["next_open_date"] = next_day
            filled += 1

    # 阶段2: 采集当日信号
    con = sqlite3.connect(f"file:{SNAP_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    today = datetime.now().strftime("%Y-%m-%d")
    sigs = con.execute(f"""
        SELECT symbol, trade_date, direction, strength, strategy_id
        FROM signal_snapshots
        WHERE trade_date='{today}' AND strategy_id IN ('deep_analysis','universe_expansion')
          AND direction IN ('LONG','SHORT')
    """).fetchall()
    con.close()

    added = 0
    seen_today = set()
    for s in sigs:
        k = (s["symbol"], s["trade_date"], s["direction"])
        if k in seen_today:
            continue
        seen_today.add(k)
        if k in recs:
            continue
        days, day_list = get_daily(s["symbol"], s["trade_date"])
        sig_open = days[s["trade_date"]][1] if days and s["trade_date"] in days else None
        recs[k] = {
            "symbol": s["symbol"], "trade_date": s["trade_date"],
            "direction": s["direction"], "strength": s["strength"],
            "strategy_id": s["strategy_id"], "signal_day_open": sig_open,
            "next_open_price": None, "next_open_date": None,
            "collected_at": datetime.now().isoformat(timespec="seconds"),
        }
        added += 1

    save_records(recs)
    print(f"[E1影子臂] 累计{len(recs)}条信号, 今日新增{added}, 本次回填次日开盘{filled}条")


if __name__ == "__main__":
    main()
