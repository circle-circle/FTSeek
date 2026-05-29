"""Fix corrupted SQLite DB by copying table-by-table, skipping corrupted rows."""
import sqlite3
import os
import sys

SRC = "data/ftseek_us.db"
DST = "data/ftseek_us_fixed.db"

if os.path.exists(DST):
    os.remove(DST)

src = sqlite3.connect(SRC)
dst = sqlite3.connect(DST)

# Get all table schemas
schemas = src.execute("SELECT name, sql FROM sqlite_master WHERE type='table'").fetchall()
print(f"Tables: {len(schemas)}")

total_rows = 0
corrupted_tables = []

for name, sql in schemas:
    if not sql:
        continue
    try:
        dst.execute(sql)
    except Exception as e:
        print(f"  {name}: schema error - {e}")
        continue

    try:
        rows = src.execute(f'SELECT * FROM "{name}"').fetchall()
        if rows:
            placeholders = ",".join(["?"] * len(rows[0]))
            dst.executemany(f'INSERT INTO "{name}" VALUES ({placeholders})', rows)
            total_rows += len(rows)
        print(f"  {name}: {len(rows)} rows OK")
    except Exception as e:
        corrupted_tables.append((name, str(e)))
        # Try row-by-row recovery
        recovered = 0
        skipped = 0
        try:
            cursor = src.execute(f'SELECT rowid, * FROM "{name}"')
            cols = [d[0] for d in cursor.description][1:]  # skip rowid
            placeholders = ",".join(["?"] * len(cols))
            while True:
                try:
                    row = cursor.fetchone()
                    if row is None:
                        break
                    dst.execute(f'INSERT INTO "{name}" ({",".join(cols)}) VALUES ({placeholders})', row[1:])
                    recovered += 1
                except Exception:
                    skipped += 1
                    continue
        except Exception:
            pass
        print(f"  {name}: CORRUPTED - recovered {recovered}, skipped {skipped} ({e})")

# Recreate indexes
indexes = src.execute("SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL").fetchall()
for (sql,) in indexes:
    try:
        dst.execute(sql)
    except Exception:
        pass

dst.commit()
dst.close()
src.close()

# Verify
v = sqlite3.connect(DST)
ic = v.execute("PRAGMA integrity_check").fetchone()[0]
print(f"\nIntegrity: {ic}")
tc = v.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0] if "trade_records" not in [t[0] for t in corrupted_tables] else "check"
print(f"trade_records: {tc}")
v.close()
print(f"\nTotal: {total_rows} rows, corrupted: {corrupted_tables}")
