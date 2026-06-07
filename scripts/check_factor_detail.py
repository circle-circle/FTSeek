"""Check if _factor_detail exists in voting_result column"""
import sqlite3, json

conn = sqlite3.connect('data/ftseek_us.db')

# Check voting_result column
rows = conn.execute("""
    SELECT symbol, trade_date, voting_result
    FROM trade_records 
    WHERE trade_date >= '2026-06-02' AND owner_strategy = 'main'
    AND voting_result IS NOT NULL AND voting_result != ''
    LIMIT 8
""").fetchall()

print(f"Found {len(rows)} trades with voting_result")
for row in rows:
    symbol, date, vr_json = row
    try:
        vr = json.loads(vr_json)
    except:
        print(f"{date} {symbol}: INVALID JSON")
        continue
    
    fd = vr.get('_factor_detail', {})
    if fd:
        print(f"\n{date} {symbol}: HAS factor_detail")
        for k, v in fd.items():
            print(f"  {k}: raw={v.get('raw',0):.3f} conf={v.get('conf',0):.2f} w={v.get('weight',0):.2f}")
    else:
        print(f"\n{date} {symbol}: NO _factor_detail (keys: {list(vr.keys())[:8]})")

# Also check signal_details column
print("\n\n=== signal_details column ===")
rows2 = conn.execute("""
    SELECT symbol, trade_date, signal_details
    FROM trade_records 
    WHERE trade_date >= '2026-06-02' AND owner_strategy = 'main'
    AND signal_details IS NOT NULL AND signal_details != ''
    LIMIT 5
""").fetchall()

print(f"Found {len(rows2)} trades with signal_details")
for row in rows2:
    symbol, date, sd_json = row
    try:
        sd = json.loads(sd_json)
        print(f"\n{date} {symbol}: keys={list(sd.keys())}")
        fd = sd.get('factor_detail', sd.get('_factor_detail', {}))
        if fd:
            print(f"  HAS factor_detail: {list(fd.keys())}")
    except:
        print(f"{date} {symbol}: INVALID JSON")
