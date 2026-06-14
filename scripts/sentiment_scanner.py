#!/usr/bin/env python3
"""
Social Sentiment Scanner — pre-populates sentiment cache for FTSeek positions.
Run by cron every 4 hours during market days.
"""
import sys
import os
sys.path.insert(0, '/opt/ftseek/FTSeek-US')
os.chdir('/opt/ftseek/FTSeek-US')

# Load env
env_file = '/opt/ftseek/FTSeek-US/.env.us'
if os.path.exists(env_file):
    for line in open(env_file):
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip())

from app.data.social_sentiment import collect_batch, _init_db

# All current position symbols
POSITIONS = ["BABA", "NVDA", "AAPL", "MU", "SNDK"]

# Also scan key sector peers for context
SECTOR_PEERS = ["AMD", "TSM", "MSFT", "GOOG", "PDD", "JD"]

def main():
    _init_db()
    
    all_symbols = list(set(POSITIONS + SECTOR_PEERS))
    print(f"[sentiment-scanner] Scanning {len(all_symbols)} symbols...")
    
    results = collect_batch(all_symbols, force_refresh=True)
    
    total_signals = 0
    for sym, data in results.items():
        summary = data.get("summary", {})
        signals = summary.get("total_signals", 0)
        total_signals += signals
        topics = summary.get("hot_topics", [])[:2]
        if signals > 0:
            print(f"  {sym}: {signals} signals — {', '.join(t[:50] for t in topics)}")
        else:
            print(f"  {sym}: no data")
    
    print(f"[sentiment-scanner] Done: {total_signals} total signals across {len(all_symbols)} symbols")

if __name__ == "__main__":
    main()
