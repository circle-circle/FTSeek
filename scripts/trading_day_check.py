#!/usr/bin/env python3
"""
FTSeek Trading Day Checker — uses Futu API to check market status.

Usage:
  python3 scripts/trading_day_check.py --market HK
  python3 scripts/trading_day_check.py --market US
  python3 scripts/trading_day_check.py --market HK --json
  python3 scripts/trading_day_check.py --market US --next

Exit codes:
  0 = trading day
  1 = non-trading day
  2 = API error

Output (default): TRADING / CLOSED / ERROR
Output (--json): {"trading": true/false, "market": "HK/US", "date": "...", "next_trading": "...", "reason": "..."}
"""

import argparse
import json
import sys
from datetime import datetime, timedelta

try:
    from futu import OpenQuoteContext, RET_OK
except ImportError:
    print("ERROR")
    sys.exit(2)


# Known holidays that Futu may not report ahead of time
KNOWN_US_HOLIDAYS_2026 = {
    "2026-01-01": "New Year's Day",
    "2026-01-19": "Martin Luther King Jr. Day",
    "2026-02-16": "Presidents' Day",
    "2026-04-03": "Good Friday",
    "2026-05-25": "Memorial Day",
    "2026-06-19": "Juneteenth",
    "2026-07-03": "Independence Day (observed)",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving",
    "2026-12-25": "Christmas",
}

KNOWN_HK_HOLIDAYS_2026 = {
    "2026-01-01": "New Year",
    "2026-02-17": "Chinese New Year (Day 1)",
    "2026-02-18": "Chinese New Year (Day 2)",
    "2026-02-19": "Chinese New Year (Day 3)",
    "2026-04-03": "Ching Ming",
    "2026-04-06": "Easter Monday",
    "2026-05-01": "Labour Day",
    "2026-05-24": "Tuen Ng Festival",
    "2026-07-01": "HKSAR Day",
    "2026-10-01": "National Day",
    "2026-10-07": "Chung Yeung",
    "2026-12-25": "Christmas",
    "2026-12-26": "Boxing Day",
}


def check_trading_day(market: str, target_date: str = None, get_next: bool = False) -> dict:
    """Check if a given date is a trading day via Futu API."""
    if target_date is None:
        target_date = datetime.now().strftime("%Y-%m-%d")

    # Weekend check (fast path)
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    if dt.weekday() >= 5:  # Saturday=5, Sunday=6
        # Find next Monday as start for next_trading lookup
        days_ahead = 7 - dt.weekday()
        next_start = (dt + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        return {
            "trading": False,
            "market": market,
            "date": target_date,
            "reason": "Weekend",
            "next_trading": next_start,  # approximate, verify with API below
            "weekday": dt.strftime("%A"),
        }

    # Known holiday check (fast path) — still query API for next_trading
    holidays = KNOWN_US_HOLIDAYS_2026 if market == "US" else KNOWN_HK_HOLIDAYS_2026
    if target_date in holidays:
        holiday_name = holidays[target_date]
        # Query API to find next trading day
        next_td = None
        try:
            ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
            start = (dt + timedelta(days=1)).strftime("%Y-%m-%d")
            end = (dt + timedelta(days=14)).strftime("%Y-%m-%d")
            ret, data = ctx.request_trading_days(market=market, start=start, end=end)
            ctx.close()
            if ret == RET_OK and data:
                next_td = data[0]["time"]
        except Exception:
            pass
        return {
            "trading": False,
            "market": market,
            "date": target_date,
            "reason": holiday_name,
            "next_trading": next_td,
            "weekday": dt.strftime("%A"),
        }

    # Futu API check
    try:
        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
        start = target_date
        end_dt = dt + timedelta(days=14)
        end = end_dt.strftime("%Y-%m-%d")

        ret, data = ctx.request_trading_days(market=market, start=start, end=end)
        ctx.close()

        if ret != RET_OK:
            return {"trading": None, "market": market, "date": target_date, "reason": f"API error: {data}", "error": True}

        trading_dates = [d["time"] for d in data]
        is_trading = target_date in trading_dates

        # Find next trading day
        next_td = None
        for td in trading_dates:
            if td > target_date:
                next_td = td
                break

        reason = "Trading day" if is_trading else "Non-trading day (not in Futu calendar)"

        return {
            "trading": is_trading,
            "market": market,
            "date": target_date,
            "reason": reason,
            "next_trading": next_td,
            "trading_dates_this_week": [d for d in trading_dates if d <= end_dt.strftime("%Y-%m-%d")],
            "weekday": dt.strftime("%A"),
        }

    except Exception as e:
        return {"trading": None, "market": market, "date": target_date, "reason": f"Connection error: {e}", "error": True}


def main():
    parser = argparse.ArgumentParser(description="Check if market is open today via Futu API")
    parser.add_argument("--market", required=True, choices=["HK", "US"], help="Market to check")
    parser.add_argument("--date", default=None, help="Date to check (default: today)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--next", action="store_true", help="Also show next trading day")
    args = parser.parse_args()

    result = check_trading_day(args.market, args.date)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if result.get("error"):
            print(f"ERROR: {result['reason']}")
            sys.exit(2)
        elif result["trading"]:
            print(f"TRADING")
        else:
            print(f"CLOSED: {result['reason']}")
            if result.get("next_trading"):
                print(f"NEXT: {result['next_trading']}")

    # Exit code: 0=trading, 1=closed, 2=error
    if result.get("error"):
        sys.exit(2)
    elif result["trading"]:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
