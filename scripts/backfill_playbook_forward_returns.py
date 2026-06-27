#!/usr/bin/env python3
"""
一次性全量回填 playbook_instructions 的 T+1/T+3/T+5 前瞻收益。

ic_tracker.backfill_forward_returns 为了防止拉取过多历史行情，只回溯 30 天；
本脚本无视时间窗口，对所有已执行/已 closed 的指令回填 forward returns。

Usage:
    python scripts/backfill_playbook_forward_returns.py [--dry-run]
"""
import argparse
import os
import sqlite3
import sys
from datetime import date as dt_date
from pathlib import Path

# 加载 .env.us，让 MarketDataRouter 能读到 API key
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parent.parent / ".env.us"
    load_dotenv(_env_path)
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.playbook.db import PlaybookDB
from app.data.market_data_router import MarketDataRouter
from app.utils.holiday_calendar import nth_trading_day_after


def _init_market_data():
    """初始化 MarketDataRouter，优先走 TwelveData/Finnhub，避免 FutuLV3Provider 挂死。"""
    try:
        return MarketDataRouter(
            twelvedata_api_key=os.getenv("TWELVEDATA_API_KEY"),
            finnhub_api_key=os.getenv("FINNHUB_API_KEY"),
            alphavantage_api_key=os.getenv("ALPHAVANTAGE_API_KEY"),
            enable_futu_lv3=False,
        )
    except Exception as e:
        print(f"[backfill] MarketDataRouter 初始化失败: {e}")
        return None


def backfill_all(dry_run: bool = False):
    db = PlaybookDB()
    conn = db._conn()
    rows = conn.execute(
        """
        SELECT id, date, symbol, direction, entry_price, execution_price
        FROM playbook_instructions
        WHERE direction != 'SKIP'
        ORDER BY date DESC
        """
    ).fetchall()

    # 按 symbol 分组，同一 symbol 只拉一次行情，减少 API 调用
    by_symbol: Dict[str, List[Dict]] = {}
    for row in rows:
        symbol = row["symbol"]
        if symbol not in by_symbol:
            by_symbol[symbol] = []
        by_symbol[symbol].append(dict(row))

    print(f"[backfill] Found {len(rows)} instructions, {len(by_symbol)} unique symbols")

    market_data = _init_market_data()
    if market_data is None:
        print("[backfill] MarketDataRouter 不可用，退出")
        return
    print("[backfill] MarketDataRouter initialized")

    updated = 0
    processed_symbols = 0
    for symbol, instructions in by_symbol.items():
        processed_symbols += 1
        print(f"[backfill] [{processed_symbols}/{len(by_symbol)}] {symbol} ...")

        # 跳过明显不是真实 symbol 的记录（如测试数据）
        if not symbol or not symbol.startswith("US.") or len(symbol) <= 4:
            print(f"[backfill]   skip invalid symbol: {symbol}")
            continue

        try:
            bars = market_data.get_daily_bars(symbol, count=30)
        except Exception as e:
            print(f"[backfill] {symbol}: skip (bars error {e})")
            continue

        if not bars:
            print(f"[backfill] {symbol}: no bars")
            continue

        bars_by_date = {b.get("time", ""): b for b in bars if b.get("time", "")}

        for inst in instructions:
            inst_id = inst["id"]
            inst_date = inst["date"]
            direction = inst["direction"]
            entry_price = inst["execution_price"] or inst["entry_price"] or 0

            if not entry_price or not inst_date:
                continue

            try:
                inst_dt = dt_date.fromisoformat(inst_date)
            except ValueError:
                continue

            sign = 1 if direction == "BUY" else -1
            updates = {}
            for horizon, field_name in [(1, "fwd_return_t1"), (3, "fwd_return_t3"), (5, "fwd_return_t5")]:
                target = nth_trading_day_after("US", inst_dt, horizon)
                if target is None:
                    continue
                target_str = target.isoformat()
                if target_str in bars_by_date:
                    close = bars_by_date[target_str].get("close", 0)
                    if close > 0:
                        fwd = sign * (close - entry_price) / entry_price
                        updates[field_name] = round(fwd, 6)

            if updates:
                if dry_run:
                    print(f"[dry-run] {symbol} {inst_date}: {updates}")
                else:
                    db.update_instruction_tracking(inst_id, updates)
                updated += 1

    print(f"[backfill] Updated {updated} instructions")

    # 显式断开数据源连接，避免 Futu 后台线程拖住进程不退出
    try:
        if market_data and hasattr(market_data, "futu") and market_data.futu:
            market_data.futu.disconnect()
    except Exception:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="试运行，不写入 DB")
    args = parser.parse_args()
    backfill_all(dry_run=args.dry_run)
    # os._exit 避免某些 provider 后台非 daemon 线程拖住进程不退出
    os._exit(0)
