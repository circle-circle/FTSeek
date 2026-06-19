#!/usr/bin/env python3
"""Analyze trigger rate with old vs new threshold"""
import sys
sys.path.insert(0, '/opt/ftseek/FTSeek-US')

from app.playbook.db import PlaybookDB

db = PlaybookDB()
db.ensure_tables()

insts = db.get_recent_instructions(limit=20)
print(f"Total instructions: {len(insts)}")
print(f"\n{'Symbol':<12} {'Entry':>10} {'Old ±2%':>20} {'New ±3%':>20} {'Current':>10}")
print("-" * 75)

for inst in insts:
    ep = inst.get('entry_price')
    if not ep or ep <= 0:
        continue
    old_low = ep * 0.98
    old_high = ep * 1.02
    new_low = ep * 0.97
    new_high = ep * 1.03
    triggered = inst.get('triggered', 0)
    print(f"{inst['symbol']:<12} ${ep:>9.2f} [{old_low:>7.2f}-{old_high:>7.2f}] [{new_low:>7.2f}-{new_high:>7.2f}] {triggered:>10}")
