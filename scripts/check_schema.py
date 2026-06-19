#!/usr/bin/env python3
"""Check trade_records schema and sample data"""
import sys
sys.path.insert(0, '/opt/ftseek/FTSeek-US')

from app.storage.db_pool import get_db

conn = get_db('ftseek_us').connection()

print("=== trade_records schema ===")
cursor = conn.execute('PRAGMA table_info(trade_records)')
for col in cursor.fetchall():
    print(f"  {col['name']} ({col['type']})")

print("\n=== Sample records (latest 3) ===")
cursor2 = conn.execute('SELECT * FROM trade_records ORDER BY id DESC LIMIT 3')
for row in cursor2.fetchall():
    d = dict(row)
    print(f"  id={d.get('id')} symbol={d.get('symbol')} side={d.get('side')} qty={d.get('quantity')} price={d.get('price')} date={d.get('trade_date')}")

print(f"\n=== Total records ===")
cursor3 = conn.execute('SELECT COUNT(*) as cnt FROM trade_records')
print(f"  {cursor3.fetchone()['cnt']} records")
