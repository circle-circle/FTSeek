#!/usr/bin/env python3
"""DA 方向 vs 大盘 regime 背离哨兵 (record-only, 零交易行为变更).

用途: DA 每日 08:00 产出方向后, 对照当日大盘(QQQ/NVDA/SMH)涨跌;
  若 DA 净做多 (BUY - SELL_SHORT > 0) 而 QQQ 当日跌幅 <= 阈值 (默认 -2%)
  => 打 FLAG: "方向未随大盘防守", 作为复盘重点 (11-05 崩盘日 DA 仍 BUY 11 / SHORT 3 即此型).

纪律:
  1. 纯只读: 只读 playbook_instructions + 富途日K, 不写任何交易/风控/下单表, 不改任何状态.
  2. 非门禁: 只打印提示, 绝不拦截/blocks 交易.
  3. 阈值可配 (--dump-threshold), 默认 -2% 属观测口径, 非裁决阈值.
  4. 无指令(DA未运行) => 跳过, 不误报.

用法:
  PYTHONPATH=. venv/bin/python scripts/da_regime_divergence_check.py            # 今日
  PYTHONPATH=. venv/bin/python scripts/da_regime_divergence_check.py --date 2026-11-05
"""
import argparse
import datetime
import sqlite3
import sys
from zoneinfo import ZoneInfo

from app.core.paths import get_trade_records_db


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="ET 交易日 YYYY-MM-DD, 缺省今日")
    ap.add_argument("--dump-threshold", type=float, default=-2.0,
                    help="大盘跌幅触发阈值%% (默认 -2.0, 观测口径非裁决)")
    args = ap.parse_args()
    date = args.date or datetime.datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    db_path = get_trade_records_db()
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    # 只算真实指令行 (排除 supply_placeholder 占位行 — 它们恒为 SKIP, 会稀释方向)
    rows = {r["direction"]: r["n"] for r in c.execute(
        "SELECT direction, COUNT(*) n FROM playbook_instructions "
        "WHERE date=? AND row_kind='instruction' GROUP BY direction", (date,))}
    buy = rows.get("BUY", 0)
    short = rows.get("SELL_SHORT", 0)
    skip = rows.get("SKIP", 0)
    net = buy - short
    if buy + short + skip == 0:
        print(f"[DA背离哨兵] {date}: 无指令 (DA 未运行/无产出) → 跳过")
        return 0

    chg = {}
    try:
        from futu import KLType, OpenQuoteContext
        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
        for sym in ("US.QQQ", "US.NVDA", "US.SMH"):
            res = ctx.request_history_kline(sym, start=date, end=date,
                                            ktype=KLType.K_DAY, max_count=2)
            r = res[0]
            df = res[1] if len(res) > 1 else None
            if r == 0 and df is not None and len(df):
                try:
                    chg[sym] = float(df.iloc[-1]["change_rate"])
                except Exception as exc:
                    print(f"[da_regime_divergence_check] 取涨跌幅失败 {sym}: {type(exc).__name__}: {exc}", file=sys.stderr)
        ctx.close()
    except Exception as e:  # noqa: BLE001 - 观测脚本, 取数失败不致命
        print(f"[DA背离哨兵] 大盘取数失败 (仅报方向): {e}")

    qqq = chg.get("US.QQQ")
    flag = qqq is not None and qqq <= args.dump_threshold and net > 0

    print(f"[DA背离哨兵] {date} | DA方向 BUY={buy} SHORT={short} SKIP={skip} (net={net:+d})")
    if chg:
        print("  大盘: " + "  ".join(f"{k.replace('US.','')}{v:+.1f}%" for k, v in chg.items()))
    if flag:
        print(f"  🔴 FLAG: DA 净做多(net={net:+d}) 而 QQQ {qqq:+.1f}% <= {args.dump_threshold:+.0f}%"
              f" → 方向未随大盘防守 (复盘重点; 对应 regime/系统性风险联动缺口)")
    else:
        print("  ⚪ 无背离 (已防守 或 大盘未大跌 / 取数缺失)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())