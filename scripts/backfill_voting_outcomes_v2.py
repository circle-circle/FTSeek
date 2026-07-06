#!/usr/bin/env python3
"""
Voting Predictions 批量回填脚本 v2

用 Futu SDK 直接获取日K线, 计算 T+1 收盘价收益。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path("/opt/ftseek/FTSeek-US")
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("backfill_voting")

# 加载 .env.us
_ENV_FILE = PROJECT_ROOT / ".env.us"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# Futu quote context (reuse)
_futu_ctx = None

def _get_futu():
    global _futu_ctx
    if _futu_ctx is None:
        try:
            from futu import OpenQuoteContext
            _futu_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
            logger.info("Futu OpenQuoteContext 已连接")
        except Exception as e:
            logger.error(f"Futu 连接失败: {e}")
            _futu_ctx = False
    return _futu_ctx if _futu_ctx is not False else None


# Cache: symbol -> {date_str: close_price}
_close_cache: Dict[str, Dict[str, float]] = {}

def _fetch_closes(symbol: str, start_date: str, end_date: str) -> Dict[str, float]:
    """获取 symbol 在 start_date~end_date 之间的日K收盘价"""
    if symbol in _close_cache:
        return _close_cache[symbol]

    ctx = _get_futu()
    if not ctx:
        return {}

    try:
        ret, df, _ = ctx.request_history_kline(
            symbol, start=start_date, end=end_date,
            ktype='K_DAY', autype=None
        )
        if ret != 0 or df is None or len(df) == 0:
            logger.debug(f"  {symbol} 无K线数据 ret={ret}")
            return {}

        result = {}
        for _, row in df.iterrows():
            date_str = str(row.get('time_key', ''))[:10]
            close = float(row.get('close', 0))
            if date_str and close > 0:
                result[date_str] = close

        _close_cache[symbol] = result
        return result
    except Exception as e:
        logger.debug(f"  {symbol} K线获取失败: {e}")
        return {}


def backfill_voting_outcomes():
    db_path = PROJECT_ROOT / "data" / "ftseek_us.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    pending = conn.execute("""
        SELECT id, decision_id, symbol, predicted_direction, predicted_strength,
               prediction_date, model_votes
        FROM voting_predictions
        WHERE actual_direction IS NULL
        ORDER BY prediction_date ASC
    """).fetchall()

    logger.info(f"待回填: {len(pending)} 条")

    # 按 symbol 分组, 批量获取 K 线
    symbols = set()
    for r in pending:
        sym = r["symbol"]
        # 修复格式
        if sym.startswith("US.US."):
            sym = "US." + sym[7:]
        if not sym.startswith("US."):
            sym = "US." + sym
        symbols.add(sym)

    logger.info(f"涉及 {len(symbols)} 个标的, 开始获取日K线...")

    # 获取所有需要的日期范围
    dates = [r["prediction_date"] for r in pending]
    min_date = min(dates)
    max_date = max(dates)
    # 扩展2天确保T+1覆盖
    start_fetch = (datetime.strptime(min_date, "%Y-%m-%d") - timedelta(days=5)).strftime("%Y-%m-%d")
    end_fetch = (datetime.strptime(max_date, "%Y-%m-%d") + timedelta(days=5)).strftime("%Y-%m-%d")

    # 批量获取每个 symbol 的收盘价
    closes_by_symbol: Dict[str, Dict[str, float]] = {}
    failed_symbols = []
    for i, sym in enumerate(sorted(symbols)):
        closes = _fetch_closes(sym, start_fetch, end_fetch)
        if closes:
            closes_by_symbol[sym] = closes
        else:
            failed_symbols.append(sym)
        if (i + 1) % 20 == 0:
            logger.info(f"  已获取 {i+1}/{len(symbols)} 个标的")

    logger.info(f"K线获取完成: {len(closes_by_symbol)} 成功, {len(failed_symbols)} 失败")
    if failed_symbols:
        logger.info(f"  失败标的: {failed_symbols[:10]}{'...' if len(failed_symbols)>10 else ''}")

    # 按日期排序所有收盘价, 找到 T+1 (下一个有收盘价的交易日)
    updated = 0
    skipped = 0

    for pred in pending:
        sym = pred["symbol"]
        if sym.startswith("US.US."):
            sym = "US." + sym[7:]
        if not sym.startswith("US."):
            sym = "US." + sym

        pred_date = pred["prediction_date"]
        closes = closes_by_symbol.get(sym, {})

        if not closes:
            skipped += 1
            continue

        # 找 T 日收盘价
        close_t0 = closes.get(pred_date)
        if close_t0 is None:
            # 尝试找 pred_date 之前最近的交易日
            for d in sorted(closes.keys(), reverse=True):
                if d <= pred_date:
                    close_t0 = closes[d]
                    pred_date_adjusted = d
                    break
            if close_t0 is None:
                skipped += 1
                continue
        else:
            pred_date_adjusted = pred_date

        # 找 T+1 日收盘价 (pred_date 之后第一个有收盘价的交易日)
        close_t1 = None
        for d in sorted(closes.keys()):
            if d > pred_date_adjusted:
                close_t1 = closes[d]
                t1_date = d
                break

        if close_t1 is None or close_t0 == 0:
            skipped += 1
            continue

        # 计算实际方向和收益
        ret_pct = (close_t1 - close_t0) / close_t0 * 100
        actual_direction = "LONG" if close_t1 > close_t0 else "SHORT"
        match = 1 if pred["predicted_direction"] == actual_direction else 0

        conn.execute("""
            UPDATE voting_predictions
            SET actual_direction = ?, actual_pnl = ?,
                outcome_match = ?, outcome_date = ?
            WHERE id = ?
        """, (
            actual_direction,
            round(ret_pct, 4),
            match,
            t1_date,
            pred["id"],
        ))
        conn.commit()
        updated += 1

    conn.close()
    logger.info(f"=== 回填完成 ===")
    logger.info(f"  成功: {updated}")
    logger.info(f"  跳过(无数据): {skipped}")
    logger.info(f"  总计: {len(pending)}")

    # 打印汇总
    if updated > 0:
        conn2 = sqlite3.connect(str(db_path))
        conn2.row_factory = sqlite3.Row
        rows = conn2.execute("""
            SELECT predicted_direction,
                   COUNT(*) as total,
                   SUM(CASE WHEN outcome_match = 1 THEN 1 ELSE 0 END) as correct,
                   ROUND(AVG(actual_pnl), 2) as avg_pnl
            FROM voting_predictions
            WHERE outcome_match IS NOT NULL
            GROUP BY predicted_direction
        """).fetchall()
        logger.info("=== 准确率汇总 ===")
        for r in rows:
            d = dict(r)
            acc = round(d['correct'] / d['total'] * 100, 1) if d['total'] > 0 else 0
            logger.info(f"  {d['predicted_direction']}: {d['correct']}/{d['total']} = {acc}% avg_pnl={d['avg_pnl']}")
        conn2.close()


if __name__ == "__main__":
    from scripts.utils.market_guard import exit_if_not_trading_day
    exit_if_not_trading_day()
    backfill_voting_outcomes()
