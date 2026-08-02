#!/usr/bin/env python3
"""SSR 影子日更 (RFC-062 在建清单④, 2026-08-02) — 只记录, 不拦截.

为什么: SSR (Short Sale Restriction) 是 SEC 市场级规则 — 个股盘中跌 ≥10% (相对昨收)
  后做空仅可 uptick。SIM 不拦截, 实盘会拒单/限单。本脚本盘后统计"SHORT 指令标的中,
  有多少当日盘中曾触及 SSR 线", 给 RFC-062 门2 G2-2 (合规拒单) 提供先验分布。

口径 (保守上界): playbook_instructions direction='SELL_SHORT', 当日 bar 最低 ≤ 昨收×0.90
  即计 would_trigger=1。盘中某刻触及 ≠ 指令时刻必处于 SSR 状态 — 读数偏保守, 用于估上限。
标记: raw_instruction_json 合并 ssr_shadow 块。幂等: 已含 ssr_shadow 的行跳过。
日期定位: 默认自动找最近 7 天内最新一个"有未标记 SHORT 指令"的交易日 (cron 每日跑即可,
  周末/假日自动空转); --date 手动指定。零行为影响。

用法: venv/bin/python scripts/ssr_shadow_daily.py [--date 2026-07-31] [--dry-run]
"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB = ROOT / "data" / "ftseek_us.db"
SSR_DROP = 0.90  # 昨收 ×0.90 = 跌 10% 触发线 (SEC 规则)


def _pick_date(conn) -> str:
    row = conn.execute("""
        SELECT date FROM playbook_instructions
        WHERE direction='SELL_SHORT' AND date >= date('now','-7 days')
          AND (raw_instruction_json IS NULL OR raw_instruction_json NOT LIKE '%ssr_shadow%')
        ORDER BY date DESC LIMIT 1
    """).fetchone()
    return row[0] if row else ""


def main() -> int:
    dry = "--dry-run" in sys.argv
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row

    if "--date" in sys.argv:
        trade_date = sys.argv[sys.argv.index("--date") + 1]
    else:
        trade_date = _pick_date(conn)
    if not trade_date:
        print("无待标记的 SHORT 指令 (近7天) — 空转")
        return 0

    rows = conn.execute("""
        SELECT id, symbol, raw_instruction_json FROM playbook_instructions
        WHERE date=? AND direction='SELL_SHORT'
          AND (raw_instruction_json IS NULL OR raw_instruction_json NOT LIKE '%ssr_shadow%')
    """, (trade_date,)).fetchall()
    if not rows:
        print(f"[{trade_date}] 无 SHORT 指令")
        return 0

    from app.data.market_data_router import MarketDataRouter
    router = MarketDataRouter()
    triggered = checked = no_bars = 0

    for r in rows:
        iid, symbol, raw_str = r["id"], r["symbol"], r["raw_instruction_json"]
        try:
            bars = router.get_daily_bars(symbol, count=10)
        except Exception:
            bars = None
        day_bar = prev_bar = None
        if bars:
            for i, b in enumerate(bars):
                if str(b.get("time", ""))[:10] == trade_date and i > 0:
                    day_bar, prev_bar = b, bars[i - 1]
                    break
        if not day_bar or not prev_bar:
            no_bars += 1
            continue

        prev_close = float(prev_bar.get("close", 0) or 0)
        day_low = float(day_bar.get("low", 0) or 0)
        if prev_close <= 0 or day_low <= 0:
            no_bars += 1
            continue
        drop_pct = round((day_low - prev_close) / prev_close * 100, 2)
        shadow = {
            "would_trigger": day_low <= prev_close * SSR_DROP,
            "prev_close": round(prev_close, 4),
            "day_low": round(day_low, 4),
            "day_low_drop_pct": drop_pct,
            "caliber": "day_low<=prev_close*0.90 (保守上界, 非指令时刻状态)",
        }
        checked += 1
        triggered += int(shadow["would_trigger"])
        if dry:
            continue
        try:
            raw = json.loads(raw_str) if raw_str else {}
            if not isinstance(raw, dict):
                raw = {}
            raw["ssr_shadow"] = shadow
            conn.execute("UPDATE playbook_instructions SET raw_instruction_json=? WHERE id=?",
                         (json.dumps(raw, ensure_ascii=False), iid))
            conn.commit()
        except Exception as e:
            print(f"  ⚠️ {symbol} 标记失败: {e}")
    conn.close()

    print(f"[{trade_date}] SHORT {len(rows)} 条 · 检查 {checked} · 触及 SSR {triggered} "
          f"· 无bar {no_bars}{' (dry)' if dry else ''}")
    return 0


if __name__ == "__main__":
    import os
    _rc = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_rc)  # futu SDK 后台线程阻解释器退出 → 硬退出 (DB 已 commit)
