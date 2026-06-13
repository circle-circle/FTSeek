"""One-shot migration: add trade_mode column to signal_snapshots.db"""
import sqlite3
from pathlib import Path

db_path = Path(__file__).resolve().parent.parent / "data" / "signal_snapshots.db"
conn = sqlite3.connect(str(db_path))

# 1. Add column (idempotent)
try:
    conn.execute('ALTER TABLE signal_snapshots ADD COLUMN trade_mode TEXT DEFAULT "simulate"')
    print("Added trade_mode column")
except sqlite3.OperationalError as e:
    if "duplicate column" in str(e).lower():
        print("Column already exists, skipping ALTER")
    else:
        raise

# 2. Mark live-epoch records (2026-06-10 is when ftseek_us_live.db started)
conn.execute("UPDATE signal_snapshots SET trade_mode = 'live' WHERE trade_date >= '2026-06-10'")
conn.commit()

# 3. Verify
for row in conn.execute("SELECT trade_mode, COUNT(*) FROM signal_snapshots GROUP BY trade_mode").fetchall():
    print(f"  {row[0]}: {row[1]} rows")

conn.close()
print("Migration complete")
