#!/usr/bin/env python3
"""
Voting Predictions 批量回填脚本

问题: voting_predictions 有 1660 条 pending (actual_direction=NULL),
因为 update_outcome 只在平仓时触发, 而大多数投票预测的股票并未实际下单。

方案: 用 T+1 收盘价计算实际方向和收益, 不依赖交易记录。
- 如果 prediction_date 是交易日 D, 取 D 收盘价和 D+1 收盘价
- actual_direction = LONG if T+1 close > T close, else SHORT
- actual_pnl = (T+1 close - T close) / T close * 100 (百分比)
- outcome_match = 1 if predicted_direction == actual_direction

用法:
  python scripts/backfill_voting_outcomes.py           # 回填所有 pending
  python scripts/backfill_voting_outcomes.py --dry-run  # 只看不执行
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
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

_DATA_ROUTER = None

def _get_data_router():
    global _DATA_ROUTER
    if _DATA_ROUTER is None:
        try:
            from app.data.market_data_router import MarketDataRouter
            _DATA_ROUTER = MarketDataRouter(
                twelvedata_api_key=os.environ.get("TWELVEDATA_API_KEY"),
                finnhub_api_key=os.environ.get("FINNHUB_API_KEY"),
                alphavantage_api_key=os.environ.get("ALPHAVANTAGE_API_KEY"),
                cache_ttl_seconds=900,
                futu_opend_host=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"),
                futu_opend_port=int(os.environ.get("FUTU_OPEND_PORT", "11111")),
                enable_futu_lv3=os.environ.get("ENABLE_FUTU_LV3", "true").lower() in ("true", "1", "yes"),
            )
        except Exception as e:
            logger.warning(f"MarketDataRouter 初始化失败: {e}")
            _DATA_ROUTER = False
    return _DATA_ROUTER or None


def get_kline_close(symbol: str, date_str: str, router) -> Optional[float]:
    """获取某交易日收盘价"""
    try:
        # 用 router 获取日K
        bars = router.get_daily_bars(symbol, days=5, end_date=date_str)
        if bars and len(bars) > 0:
            # 返回 end_date 当天或之前的收盘价
            for bar in reversed(bars):
                bar_date = str(bar.get('date', bar.get('time', '')))[:10]
                if bar_date <= date_str:
                    return float(bar.get('close', 0))
            # 如果没找到精确日期, 取最后一个
            return float(bars[-1].get('close', 0))
    except Exception as e:
        logger.debug(f"获取 {symbol} {date_str} 收盘价失败: {e}")
    return None


def backfill_voting_outcomes(dry_run: bool = False):
    """回填所有 pending voting_predictions"""
    db_path = PROJECT_ROOT / "data" / "ftseek_us.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # 获取所有 pending 预测
    pending = conn.execute("""
        SELECT id, decision_id, symbol, predicted_direction, predicted_strength,
               prediction_date, model_votes
        FROM voting_predictions
        WHERE actual_direction IS NULL
        ORDER BY prediction_date ASC
    """).fetchall()

    logger.info(f"待回填: {len(pending)} 条")

    if dry_run:
        for r in pending[:20]:
            logger.info(f"  [DRY] {r['prediction_date']} {r['symbol']} pred={r['predicted_direction']}")
        logger.info(f"  ... 共 {len(pending)} 条")
        conn.close()
        return

    router = _get_data_router()
    if not router:
        logger.error("MarketDataRouter 不可用, 无法获取收盘价")
        conn.close()
        return

    # 按日期分组, 批量获取收盘价
    by_date: Dict[str, List[sqlite3.Row]] = {}
    for r in pending:
        d = r["prediction_date"]
        if d not in by_date:
            by_date[d] = []
        by_date[d].append(r)

    updated = 0
    skipped = 0
    errors = 0

    for pred_date in sorted(by_date.keys()):
        predictions = by_date[pred_date]
        logger.info(f"处理 {pred_date}: {len(predictions)} 条预测")

        # 解析日期
        try:
            dt = datetime.strptime(pred_date, "%Y-%m-%d")
        except ValueError:
            logger.warning(f"  日期解析失败: {pred_date}, 跳过")
            errors += len(predictions)
            continue

        # T+1 日期 (下一个交易日)
        t1_date = (dt + timedelta(days=1)).strftime("%Y-%m-%d")

        for pred in predictions:
            symbol = pred["symbol"]
            # 修复: 部分symbol有 "US.US.MU" 格式
            if symbol.startswith("US.US."):
                symbol = "US." + symbol[7:]
            if not symbol.startswith("US."):
                symbol = "US." + symbol

            try:
                # 获取 T 和 T+1 收盘价
                close_t0 = get_kline_close(symbol, pred_date, router)
                close_t1 = get_kline_close(symbol, t1_date, router)

                if close_t0 is None or close_t1 is None or close_t0 == 0:
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

                if updated % 50 == 0:
                    logger.info(f"  已回填 {updated}/{len(pending)}")

            except Exception as e:
                logger.debug(f"  {symbol} 回填失败: {e}")
                errors += 1

    conn.close()
    logger.info(f"=== 回填完成 ===")
    logger.info(f"  成功: {updated}")
    logger.info(f"  跳过(无数据): {skipped}")
    logger.info(f"  错误: {errors}")
    logger.info(f"  总计: {len(pending)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只看不执行")
    args = parser.parse_args()
    backfill_voting_outcomes(dry_run=args.dry_run)
