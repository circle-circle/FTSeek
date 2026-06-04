#!/usr/bin/env python3
"""Clean up ghost positions from ftseek_us.db"""
import sqlite3
import os

db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'ftseek_us.db')
conn = sqlite3.connect(db_path)
cur = conn.cursor()

# Delete ghost records
cur.execute("DELETE FROM open_positions WHERE sync_status='closed_by_sync'")
deleted = cur.rowcount
conn.commit()

# Show remaining
cur.execute("SELECT symbol, sync_status FROM open_positions")
remaining = cur.fetchall()
conn.close()

print(f"Deleted {deleted} ghost records")
print(f"Remaining {len(remaining)} active positions:")
for r in remaining:
    print(f"  {r[0]} status={r[1]}")
