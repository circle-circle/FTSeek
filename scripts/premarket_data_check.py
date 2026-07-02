#!/usr/bin/env python3
# Crontab: 0 21 * * 1-5 cd /opt/ftseek/FTSeek-US && venv/bin/python scripts/premarket_data_check.py
"""
FTSeek-US Pre-Market Data Freshness Check
==========================================
Pre-trade guard: verifies K-line freshness for all watchlist stocks + SPY
before the trading session. Catches the bug where request_history_kline
returned 2025 stale data (missing start/end params or too-small max_count)
that went undetected for weeks.

Checks:
  1. K-line freshness for 24 watchlist stocks (latest bar date)
     - >10 days old → RED ALERT
     - >3  days old → YELLOW WARNING
  2. SPY K-line freshness (MA200 dependency)
  3. api_limit_state.json last_reset_date staleness (>1 day → warn)
  4. signal_snapshots.db pending backfill count (ic_batch_date IS NULL
     AND trade_date < today → warn)
  5. Summary with ✅/⚠️/🔴 markers
  6. Exit code 1 if any RED ALERT (cron-catchable)
     Last line "🚨 DATA_RED_ALERT" if any RED issues.
"""
import os
import sys
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

# ── Bootstrap (self-contained) ──────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent.parent  # /opt/ftseek/FTSeek-US
os.chdir(str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR))

# Load .env.us (sets FUTU_OPD_HOST etc. if present)
try:
    from dotenv import load_dotenv
    _env_path = PROJECT_DIR / ".env.us"
    if _env_path.exists():
        load_dotenv(_env_path)
except Exception:
    pass

# ── Constants ───────────────────────────────────────────────────────────
WATCHLIST = [
    "US.AAPL", "US.JPM", "US.UNH", "US.LMT", "US.NVDA", "US.MSFT",
    "US.TSLA", "US.GOOG", "US.AMZN", "US.META", "US.BWXT", "US.AVGO",
    "US.ARM", "US.MU", "US.SNDK", "US.NFLX", "US.PG", "US.CRM",
    "US.ORCL", "US.RIVN", "US.PDD", "US.LLY", "US.BA", "US.TSM",
]
SPY_SYMBOL = "US.SPY"
OPD_HOST = os.getenv("FUTU_OPD_HOST", "127.0.0.1")
OPD_PORT = int(os.getenv("FUTU_OPD_PORT", "11111"))

RED_DAYS = 10      # latest bar older than this → RED
YELLOW_DAYS = 3    # latest bar older than this → YELLOW
KLINE_START_DAYS = 400
KLINE_MAX_COUNT = 320

DATA_DIR = PROJECT_DIR / "data"
API_LIMIT_STATE_FILE = DATA_DIR / "api_limit_state.json"
SIGNAL_SNAPSHOTS_DB = DATA_DIR / "signal_snapshots.db"


# ── Helpers ─────────────────────────────────────────────────────────────
def parse_kline_date(time_key: str):
    """Parse Futu kline 'time_key' (e.g. '2026-06-30 00:00:00' or '2026-06-30')
    into a date object. Returns None on failure."""
    if not time_key:
        return None
    try:
        return datetime.strptime(time_key[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def check_kline_freshness(quote_ctx, symbol, today):
    """Fetch a single symbol's latest daily K-line bar date.
    Returns (status, latest_date_or_None, detail_msg).
    status ∈ {'RED','YELLOW','OK','ERROR'}."""
    from futu import KLType, RET_OK

    start = (today - timedelta(days=KLINE_START_DAYS)).isoformat()
    end = today.isoformat()
    try:
        # NOTE: autype intentionally omitted — causes ImportError in some
        # SDK versions (see auto_position_reconcile.py which uses AuType).
        ret, data, _ = quote_ctx.request_history_kline(
            code=symbol,
            ktype=KLType.K_DAY,
            max_count=KLINE_MAX_COUNT,
            start=start,
            end=end,
        )
    except Exception as e:
        return ("ERROR", None, f"request_history_kline exception: {e}")

    if ret != RET_OK:
        return ("ERROR", None, f"API ret={ret}, err={data}")

    if data is None or len(data) == 0:
        return ("ERROR", None, "no kline rows returned")

    # Last row = most recent bar. time_key holds the bar date.
    try:
        last_row = data.iloc[-1]
        time_key = str(last_row.get("time_key", ""))
    except Exception as e:
        return ("ERROR", None, f"failed to read last row: {e}")

    latest_date = parse_kline_date(time_key)
    if latest_date is None:
        return ("ERROR", None, f"unparseable time_key='{time_key}'")

    age_days = (today - latest_date).days
    n_bars = len(data)

    if age_days > RED_DAYS:
        return ("RED", latest_date,
                f"latest={latest_date.isoformat()} ({age_days}d old, {n_bars} bars)")
    if age_days > YELLOW_DAYS:
        return ("YELLOW", latest_date,
                f"latest={latest_date.isoformat()} ({age_days}d old, {n_bars} bars)")
    return ("OK", latest_date,
            f"latest={latest_date.isoformat()} ({age_days}d old, {n_bars} bars)")


def check_api_limit_state(today):
    """Check api_limit_state.json freshness. Returns (status, detail)."""
    if not API_LIMIT_STATE_FILE.exists():
        return ("YELLOW", f"{API_LIMIT_STATE_FILE.name} not found")
    try:
        with open(API_LIMIT_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception as e:
        return ("RED", f"failed to parse {API_LIMIT_STATE_FILE.name}: {e}")

    last_reset = state.get("last_reset_date", "")
    if not last_reset:
        return ("YELLOW", "last_reset_date missing/empty")
    try:
        reset_date = datetime.strptime(last_reset[:10], "%Y-%m-%d").date()
    except Exception:
        return ("YELLOW", f"unparseable last_reset_date='{last_reset}'")

    age = (today - reset_date).days
    src = state.get("current_source", "?")
    td_calls = state.get("twelvedata_calls", "?")
    if age > 1:
        return ("YELLOW",
                f"last_reset_date={last_reset} ({age}d old, src={src}, td={td_calls})")
    return ("OK", f"last_reset_date={last_reset} (today, src={src}, td={td_calls})")


def check_signal_backfill(today):
    """Count pending backfills in signal_snapshots.db.
    Returns (status, detail). Pending = ic_batch_date IS NULL AND trade_date < today."""
    if not SIGNAL_SNAPSHOTS_DB.exists():
        return ("YELLOW", f"{SIGNAL_SNAPSHOTS_DB.name} not found")
    today_str = today.isoformat()
    try:
        conn = sqlite3.connect(str(SIGNAL_SNAPSHOTS_DB), timeout=10)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM signal_snapshots "
                "WHERE ic_batch_date IS NULL AND trade_date < ?",
                (today_str,),
            ).fetchone()
        finally:
            conn.close()
        pending = int(row[0]) if row else 0
    except Exception as e:
        return ("YELLOW", f"DB query failed: {e}")

    if pending > 50:
        return ("YELLOW", f"{pending} snapshots pending backfill (ic_batch_date IS NULL)")
    return ("OK", f"{pending} snapshots pending backfill")


# ── Main ────────────────────────────────────────────────────────────────
def main():
    from futu import OpenQuoteContext, RET_OK

    today = date.today()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*64}")
    print(f"📊 FTSeek-US Pre-Market Data Freshness Check — {now_str}")
    print(f"   today={today.isoformat()}  OpenD={OPD_HOST}:{OPD_PORT}")
    print(f"{'='*64}")

    red_count = 0
    yellow_count = 0
    lines = []  # (marker, symbol, detail)

    quote_ctx = OpenQuoteContext(host=OPD_HOST, port=OPD_PORT)
    try:
        # 1+2. K-line freshness for watchlist + SPY
        all_symbols = WATCHLIST + [SPY_SYMBOL]
        print(f"\n[K-line] Checking {len(all_symbols)} symbols "
              f"({len(WATCHLIST)} watchlist + SPY)...")
        for sym in all_symbols:
            status, latest_date, detail = check_kline_freshness(
                quote_ctx, sym, today)
            if status == "RED":
                marker = "🔴"
                red_count += 1
            elif status == "YELLOW":
                marker = "⚠️"
                yellow_count += 1
            elif status == "ERROR":
                marker = "🔴"  # treat fetch errors as RED — data is unavailable
                red_count += 1
            else:
                marker = "✅"
            tag = " [SPY/MA200]" if sym == SPY_SYMBOL else ""
            lines.append((marker, f"{sym}{tag}", detail))
            print(f"  {marker} {sym}{tag}: {detail}")
    finally:
        try:
            quote_ctx.close()
        except Exception:
            pass

    # 3. api_limit_state.json
    print("\n[API limit state]")
    status, detail = check_api_limit_state(today)
    if status == "RED":
        marker = "🔴"; red_count += 1
    elif status == "YELLOW":
        marker = "⚠️"; yellow_count += 1
    else:
        marker = "✅"
    lines.append((marker, "api_limit_state.json", detail))
    print(f"  {marker} api_limit_state.json: {detail}")

    # 4. signal_snapshots.db pending backfill
    print("\n[Signal backfill]")
    status, detail = check_signal_backfill(today)
    if status == "RED":
        marker = "🔴"; red_count += 1
    elif status == "YELLOW":
        marker = "⚠️"; yellow_count += 1
    else:
        marker = "✅"
    lines.append((marker, "signal_snapshots pending", detail))
    print(f"  {marker} signal_snapshots pending: {detail}")

    # 5. Summary
    print(f"\n{'─'*64}")
    print("📋 SUMMARY")
    print(f"{'─'*64}")
    total = len(lines)
    print(f"   ✅ OK:      {total - red_count - yellow_count}")
    print(f"   ⚠️ YELLOW:  {yellow_count}")
    print(f"   🔴 RED:     {red_count}")
    print(f"   Total checks: {total}")

    if red_count > 0:
        print(f"\n{'='*64}")
        print(f"🔴 RED ALERT — {red_count} data freshness issue(s):")
        for marker, name, detail in lines:
            if marker == "🔴":
                print(f"   • {name}: {detail}")
        print(f"{'='*64}")
        print("🚨 DATA_RED_ALERT")
        sys.exit(1)
    elif yellow_count > 0:
        print(f"\n{'='*64}")
        print(f"⚠️  {yellow_count} warning(s) — review before open.")
        print(f"{'='*64}")
    else:
        print(f"\n{'='*64}")
        print("✅ ALL DATA FRESH — clear to trade.")
        print(f"{'='*64}")

    sys.exit(0)


if __name__ == "__main__":
    main()
