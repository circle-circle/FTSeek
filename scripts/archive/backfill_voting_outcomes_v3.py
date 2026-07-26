#!/usr/bin/env python3
"""Voting Predictions 批量回填 v3 - 逐标的获取K线, 带进度输出"""
import json, logging, os, sqlite3, sys, time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

PROJECT_ROOT = Path("/opt/ftseek/FTSeek-US")
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("backfill")

# Load env
_ENV_FILE = PROJECT_ROOT / ".env.us"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

def normalize_symbol(sym: str) -> str:
    if sym.startswith("US.US."):
        sym = "US." + sym[7:]
    if not sym.startswith("US."):
        sym = "US." + sym
    return sym

def main():
    from futu import OpenQuoteContext
    
    db_path = PROJECT_ROOT / "data" / "ftseek_us.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    pending = conn.execute("""
        SELECT id, symbol, predicted_direction, prediction_date
        FROM voting_predictions WHERE actual_direction IS NULL
        ORDER BY prediction_date ASC
    """).fetchall()
    logger.info(f"待回填: {len(pending)} 条")

    # Get unique symbols
    symbols = sorted(set(normalize_symbol(r["symbol"]) for r in pending))
    logger.info(f"涉及 {len(symbols)} 个标的")

    # Connect Futu
    ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    logger.info("Futu 已连接")

    # Fetch K-line for each symbol (one request per symbol)
    closes_by_symbol: Dict[str, Dict[str, float]] = {}
    for i, sym in enumerate(symbols):
        try:
            ret, df, _ = ctx.request_history_kline(
                sym, start='2026-05-15', end='2026-07-03',
                ktype='K_DAY', autype=None
            )
            if ret == 0 and hasattr(df, 'shape') and df.shape[0] > 0:
                result = {}
                for _, row in df.iterrows():
                    date_str = str(row.get('time_key', ''))[:10]
                    close = float(row.get('close', 0))
                    if date_str and close > 0:
                        result[date_str] = close
                closes_by_symbol[sym] = result
                logger.info(f"  [{i+1}/{len(symbols)}] {sym}: {len(result)} 条K线")
            else:
                logger.warning(f"  [{i+1}/{len(symbols)}] {sym}: 获取失败 ret={ret}")
        except Exception as e:
            logger.warning(f"  [{i+1}/{len(symbols)}] {sym}: 异常 {e}")
        time.sleep(0.1)  # 轻微限速

    ctx.close()
    logger.info(f"K线获取完成: {len(closes_by_symbol)}/{len(symbols)}")

    # Backfill
    updated = 0
    skipped = 0
    for pred in pending:
        sym = normalize_symbol(pred["symbol"])
        pred_date = pred["prediction_date"]
        closes = closes_by_symbol.get(sym, {})
        if not closes:
            skipped += 1
            continue

        # Find T close
        close_t0 = closes.get(pred_date)
        if close_t0 is None:
            # Find nearest date before pred_date
            for d in sorted(closes.keys(), reverse=True):
                if d <= pred_date:
                    close_t0 = closes[d]
                    pred_date_adj = d
                    break
            else:
                skipped += 1
                continue
        else:
            pred_date_adj = pred_date

        # Find T+1 close (first date after pred_date_adj)
        close_t1 = None
        t1_date = None
        for d in sorted(closes.keys()):
            if d > pred_date_adj:
                close_t1 = closes[d]
                t1_date = d
                break

        if close_t1 is None or close_t0 == 0:
            skipped += 1
            continue

        ret_pct = (close_t1 - close_t0) / close_t0 * 100
        actual_direction = "LONG" if close_t1 > close_t0 else "SHORT"
        match = 1 if pred["predicted_direction"] == actual_direction else 0

        conn.execute("""
            UPDATE voting_predictions
            SET actual_direction = ?, actual_pnl = ?,
                outcome_match = ?, outcome_date = ?
            WHERE id = ?
        """, (actual_direction, round(ret_pct, 4), match, t1_date, pred["id"]))
        conn.commit()
        updated += 1

    conn.close()
    logger.info(f"=== 回填完成 ===")
    logger.info(f"  成功: {updated}")
    logger.info(f"  跳过: {skipped}")

    # Summary
    if updated > 0:
        conn2 = sqlite3.connect(str(db_path))
        conn2.row_factory = sqlite3.Row
        rows = conn2.execute("""
            SELECT predicted_direction,
                   COUNT(*) as total,
                   SUM(CASE WHEN outcome_match = 1 THEN 1 ELSE 0 END) as correct,
                   ROUND(AVG(actual_pnl), 2) as avg_pnl
            FROM voting_predictions WHERE outcome_match IS NOT NULL
            GROUP BY predicted_direction
        """).fetchall()
        logger.info("=== 准确率汇总 ===")
        for r in rows:
            d = dict(r)
            acc = round(d['correct'] / d['total'] * 100, 1) if d['total'] > 0 else 0
            logger.info(f"  {d['predicted_direction']}: {d['correct']}/{d['total']} = {acc}% avg_pnl={d['avg_pnl']}")

        # Per-model analysis
        rows = conn2.execute("""
            SELECT model_votes, predicted_direction, actual_direction, outcome_match
            FROM voting_predictions WHERE outcome_match IS NOT NULL
        """).fetchall()
        model_stats = {}
        for r in rows:
            d = dict(r)
            try:
                votes = json.loads(d['model_votes'])
                for mid, v in votes.items():
                    if mid not in model_stats:
                        model_stats[mid] = {'total': 0, 'correct': 0, 'LONG': 0, 'SHORT': 0}
                    model_stats[mid]['total'] += 1
                    vdir = v.get('direction', v.get('dir', ''))
                    if vdir == d['actual_direction']:
                        model_stats[mid]['correct'] += 1
                    if vdir in ('LONG', 'SHORT'):
                        model_stats[mid][vdir] += 1
            except:
                pass
        logger.info("=== 模型准确率 ===")
        for mid, s in sorted(model_stats.items()):
            acc = round(s['correct'] / s['total'] * 100, 1) if s['total'] > 0 else 0
            bias = f"LONG={s.get('LONG',0)} SHORT={s.get('SHORT',0)}"
            logger.info(f"  {mid}: {s['correct']}/{s['total']} = {acc}% ({bias})")
        conn2.close()

if __name__ == "__main__":
    main()
