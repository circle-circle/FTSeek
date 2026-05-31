#!/usr/bin/env python3
"""
Signal IC 盘后回填脚本

职责:
  1. 从 decisions.db 镜像历史信号到 signal_snapshots (一次性)
  2. 用 yfinance 拉日 K 线, 回填前瞻收益 (T+1/T+5/T+20)
  3. 每日 ET 17:00 由 cron 触发

数据源优先级:
  1. backtest_cache CSV (已有数据, 零 API 开销)
  2. yfinance (免费, 无 token)
  3. AlphaVantage (需 API key, 作 fallback)

用法:
  python scripts/backfill_forward_returns.py              # 全量回填
  python scripts/backfill_forward_returns.py --mirror     # 仅镜像历史决策 (不拉行情)
  python scripts/backfill_forward_returns.py --forward    # 仅回填前瞻收益
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 路径设置
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_ic")


# ═══════════════════════════════════════════════════════════════════
#  Phase 1A: 镜像历史决策到 signal_snapshots
# ═══════════════════════════════════════════════════════════════════

def mirror_historical_decisions() -> int:
    """
    从 decisions.db 读取所有历史决策, 写入 signal_snapshots.db
    
    Returns: 成功写入条数
    """
    from app.core.paths import get_decisions_db
    
    decisions_db = str(get_decisions_db())
    logger.info(f"读取历史决策: {decisions_db}")
    
    # 读取所有决策
    conn_dec = sqlite3.connect(decisions_db)
    conn_dec.row_factory = sqlite3.Row
    rows = conn_dec.execute("""
        SELECT decision_id, symbol, direction, final_strength,
               consensus_level, created_at
        FROM decisions
        ORDER BY created_at ASC
    """).fetchall()
    conn_dec.close()
    
    logger.info(f"共 {len(rows)} 条历史决策")
    
    # 写入 signal_snapshots
    from app.analysis.signal_ic_tracker import record_signal_snapshot
    
    count = 0
    for row in rows:
        # 计算 trade_date (从 created_at 推导, 而非 now())
        created_at = row["created_at"]
        try:
            dt = datetime.fromisoformat(created_at)
            # created_at 是 CST (服务器本地时间), 减 12h 得到 ET 日期
            # 例: CST 2026-05-14T22:24 → ET 2026-05-14 10:24
            et_dt = dt - timedelta(hours=12)
            trade_date = et_dt.strftime("%Y-%m-%d")
        except Exception:
            # fallback: 直接取日期部分
            trade_date = created_at[:10]
        
        record_signal_snapshot(
            decision_id=row["decision_id"],
            symbol=row["symbol"],
            direction=row["direction"],
            strength=row["final_strength"],
            confidence=row["consensus_level"],
            consensus_level=row["consensus_level"],
            strategy_id=None,  # 历史数据无 strategy_id
            created_at=created_at,
            trade_date=trade_date,
        )
        count += 1
    
    logger.info(f"✅ 镜像完成: {count}/{len(rows)} 条写入 signal_snapshots")
    return count


# ═══════════════════════════════════════════════════════════════════
#  日 K 线数据获取
# ═══════════════════════════════════════════════════════════════════

def _symbol_to_yf_ticker(symbol: str) -> str:
    """US.TSLA → TSLA, US.BABA → BABA"""
    parts = symbol.split(".")
    if len(parts) >= 2:
        return parts[-1]
    return symbol


def _load_backtest_cache(symbol: str) -> Optional[Dict[str, float]]:
    """
    从 backtest_cache CSV 加载日收盘价
    
    Returns: {date_str: close_price} 或 None
    """
    ticker = _symbol_to_yf_ticker(symbol)
    cache_dir = PROJECT_ROOT / "data" / "backtest_cache"
    
    # 查找匹配的 CSV
    pattern = f"{ticker}_*_1day.csv"
    matches = list(cache_dir.glob(pattern))
    if not matches:
        return None
    
    csv_path = matches[0]
    result = {}
    try:
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                date_str = row.get("datetime", "")
                close = row.get("Close")
                if date_str and close:
                    result[date_str] = float(close)
    except Exception as e:
        logger.debug(f"backtest_cache 读取失败 {csv_path}: {e}")
        return None
    
    logger.debug(f"backtest_cache: {ticker} 加载 {len(result)} 天")
    return result if result else None


def _fetch_twelvedata_daily(symbol: str, outputsize: int = 45) -> Optional[Dict[str, float]]:
    """
    用 TwelveData API 拉取日收盘价
    
    Returns: {date_str: close_price} 或 None
    """
    import requests
    
    # 从 .env.us 或环境变量获取 key
    api_key = os.environ.get("TWELVEDATA_API_KEY", "")
    if not api_key:
        # 尝试从 .env.us 读取
        env_us = PROJECT_ROOT / ".env.us"
        if env_us.exists():
            for line in env_us.read_text().splitlines():
                if line.startswith("TWELVEDATA_API_KEY="):
                    api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not api_key:
        logger.debug("TwelveData API key 未配置")
        return None
    
    ticker = _symbol_to_yf_ticker(symbol)
    
    try:
        resp = requests.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol": ticker,
                "interval": "1day",
                "outputsize": outputsize,
                "order": "ASC",
                "apikey": api_key,
            },
            timeout=15,
        )
        data = resp.json()
        if "values" not in data:
            logger.debug(f"TwelveData: {ticker} 无数据: {data.get('message', 'unknown')}")
            return None
        
        result = {}
        for v in data["values"]:
            date_str = v.get("datetime", "")
            close = v.get("close")
            if date_str and close:
                result[date_str] = float(close)
        
        logger.debug(f"TwelveData: {ticker} 获取 {len(result)} 天")
        return result if result else None
    except Exception as e:
        logger.debug(f"TwelveData: {ticker} 失败: {e}")
        return None


def _fetch_yfinance_daily(symbol: str, days: int = 30) -> Optional[Dict[str, float]]:
    """
    用 yfinance 拉取日收盘价
    
    Returns: {date_str: close_price} 或 None
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance 未安装")
        return None
    
    ticker_str = _symbol_to_yf_ticker(symbol)
    
    try:
        ticker = yf.Ticker(ticker_str)
        # 多拉几天以覆盖周末
        hist = ticker.history(period=f"{days + 15}d", auto_adjust=True)
        if hist.empty:
            logger.warning(f"yfinance: {ticker_str} 无数据")
            return None
        
        result = {}
        for idx, row in hist.iterrows():
            date_str = idx.strftime("%Y-%m-%d")
            result[date_str] = float(row["Close"])
        
        logger.debug(f"yfinance: {ticker_str} 获取 {len(result)} 天")
        return result
    except Exception as e:
        logger.warning(f"yfinance: {ticker_str} 失败: {e}")
        return None


def get_daily_closes(symbol: str, extra_days: int = 25) -> Optional[Dict[str, float]]:
    """
    获取日收盘价 (优先级: backtest_cache > TwelveData > yfinance)
    
    Returns: {date_str: close_price} 按日期升序
    """
    # 1. backtest_cache (零 API 开销, 但需验证日期覆盖)
    data = _load_backtest_cache(symbol)
    if data and len(data) >= 20:
        # 检查最新日期是否足够新 (不超过 5 天前)
        latest_date = max(data.keys())
        days_ago = (datetime.now() - datetime.strptime(latest_date, "%Y-%m-%d")).days
        if days_ago < 10:
            return dict(sorted(data.items()))
        # 缓存太旧, 跳过, 走 API
    
    # 2. TwelveData (生产环境首选)
    data = _fetch_twelvedata_daily(symbol, outputsize=extra_days + 20)
    if data:
        return dict(sorted(data.items()))
    
    # 3. yfinance (fallback)
    data = _fetch_yfinance_daily(symbol, days=extra_days)
    if data:
        return dict(sorted(data.items()))
    
    return None


def _get_trading_dates_sorted(all_dates: List[str]) -> List[str]:
    """排序交易日列表"""
    return sorted(all_dates)


# ═══════════════════════════════════════════════════════════════════
#  Phase 1B: 回填前瞻收益
# ═══════════════════════════════════════════════════════════════════

def backfill_forward_returns(dry_run: bool = False) -> Dict[str, int]:
    """
    对所有 pending 快照回填前瞻收益
    
    Returns: {symbol: count_filled}
    """
    from app.analysis.signal_ic_tracker import get_pending_snapshots, update_forward_returns
    
    # 获取所有待回填快照
    today_str = datetime.now().strftime("%Y-%m-%d")
    pending = get_pending_snapshots(trade_date_before=today_str, limit=1000)
    
    if not pending:
        logger.info("无待回填快照")
        return {}
    
    logger.info(f"待回填快照: {len(pending)} 条")
    
    # 按 symbol 分组, 减少 API 调用
    by_symbol: Dict[str, List[Dict]] = {}
    for snap in pending:
        sym = snap["symbol"]
        if sym not in by_symbol:
            by_symbol[sym] = []
        by_symbol[sym].append(snap)
    
    logger.info(f"涉及 {len(by_symbol)} 只股票")
    
    stats: Dict[str, int] = {}
    
    for symbol, snaps in by_symbol.items():
        # 获取日 K 线
        closes = get_daily_closes(symbol, extra_days=30)
        if not closes:
            logger.warning(f"⚠️ {symbol}: 无法获取日 K 线, 跳过")
            continue
        
        # TwelveData 限流: 免费版 8 次/分钟, 每次调用后等待 8 秒
        import time
        time.sleep(8)
        
        sorted_dates = _get_trading_dates_sorted(list(closes.keys()))
        date_to_idx = {d: i for i, d in enumerate(sorted_dates)}
        
        filled = 0
        for snap in snaps:
            trade_date = snap["trade_date"]
            
            # 找到信号日在 sorted_dates 中的位置
            # 允许 ±1 天容差 (时区差异)
            snap_idx = None
            for offset in [0, 1, -1]:
                check_date = (datetime.strptime(trade_date, "%Y-%m-%d") + timedelta(days=offset)).strftime("%Y-%m-%d")
                if check_date in date_to_idx:
                    snap_idx = date_to_idx[check_date]
                    trade_date_actual = check_date
                    break
            
            if snap_idx is None:
                logger.debug(f"  {symbol} {trade_date}: 未找到交易日, 跳过")
                continue
            
            trade_date_actual = sorted_dates[snap_idx]
            
            # 获取 T+0, T+1, T+5, T+20 收盘价
            close_t0 = closes.get(trade_date_actual)
            
            # T+1: 下一个交易日
            t1_idx = snap_idx + 1
            close_t1 = closes[sorted_dates[t1_idx]] if t1_idx < len(sorted_dates) else None
            
            # T+5: 第 5 个交易日后
            t5_idx = snap_idx + 5
            close_t5 = closes[sorted_dates[t5_idx]] if t5_idx < len(sorted_dates) else None
            
            # T+20: 第 20 个交易日后
            t20_idx = snap_idx + 20
            close_t20 = closes[sorted_dates[t20_idx]] if t20_idx < len(sorted_dates) else None
            
            if dry_run:
                direction = snap["direction"]
                if close_t0 and close_t1:
                    if direction == "SHORT":
                        fwd = (close_t0 / close_t1) - 1
                    else:
                        fwd = (close_t1 / close_t0) - 1
                    logger.info(
                        f"  [DRY] {snap['decision_id']} {symbol} {direction} "
                        f"str={snap['strength']} t0={close_t0:.2f} t1={close_t1:.2f} fwd={fwd:+.3%}"
                    )
                filled += 1
            else:
                update_forward_returns(
                    decision_id=snap["decision_id"],
                    close_t0=close_t0,
                    close_t1=close_t1,
                    close_t5=close_t5,
                    close_t20=close_t20,
                )
                filled += 1
        
        stats[symbol] = filled
        logger.info(f"✅ {symbol}: {filled}/{len(snaps)} 条回填完成")
    
    return stats


# ═══════════════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Signal IC 前瞻收益回填")
    parser.add_argument("--mirror", action="store_true", help="仅镜像历史决策 (不拉行情)")
    parser.add_argument("--forward", action="store_true", help="仅回填前瞻收益 (不镜像)")
    parser.add_argument("--dry-run", action="store_true", help="试运行, 不写入 DB")
    parser.add_argument("--stats", action="store_true", help="显示统计信息")
    args = parser.parse_args()
    
    if args.stats:
        from app.analysis.signal_ic_tracker import get_stats
        stats = get_stats()
        print(f"\n📊 Signal Snapshots 统计:")
        print(f"  总快照: {stats['total_snapshots']}")
        print(f"  已回填 T1: {stats['with_t1']}")
        print(f"  完全回填: {stats['fully_realized']}")
        print(f"  待回填: {stats['pending']}")
        return
    
    # Phase 1A: 镜像历史决策
    if not args.forward:
        logger.info("=" * 50)
        logger.info("Phase 1A: 镜像历史决策到 signal_snapshots")
        logger.info("=" * 50)
        mirror_historical_decisions()
    
    # Phase 1B: 回填前瞻收益
    if not args.mirror:
        logger.info("")
        logger.info("=" * 50)
        logger.info("Phase 1B: 回填前瞻收益 (T+1/T+5/T+20)")
        logger.info("=" * 50)
        stats = backfill_forward_returns(dry_run=args.dry_run)
        
        total_filled = sum(stats.values())
        logger.info("")
        logger.info(f"📊 回填完成: {total_filled} 条, 涉及 {len(stats)} 只股票")
        for sym, cnt in sorted(stats.items(), key=lambda x: -x[1]):
            logger.info(f"  {sym}: {cnt}")
    
    # 最终统计
    if not args.dry_run:
        from app.analysis.signal_ic_tracker import get_stats
        final = get_stats()
        logger.info("")
        logger.info(f"📊 最终统计:")
        logger.info(f"  总快照: {final['total_snapshots']}")
        logger.info(f"  已回填 T1: {final['with_t1']}")
        logger.info(f"  完全回填: {final['fully_realized']}")
        logger.info(f"  待回填: {final['pending']}")


if __name__ == "__main__":
    main()
