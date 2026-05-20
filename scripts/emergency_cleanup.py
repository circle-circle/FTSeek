
import sqlite3

print("=== US Position Emergency Cleanup ===")
try:
    conn = sqlite3.connect('data/ftseek_us.db')
    c = conn.cursor()

    # 1. Count current positions
    c.execute("SELECT symbol, direction, size, avg_entry_price FROM open_positions")
    positions = c.fetchall()
    print(f"Found {len(positions)} open positions in DB.")
    for p in positions:
        print(f"  -> {p}")

    if len(positions) > 0:
        # 2. Archive if table exists
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trade_history_positions'")
        if c.fetchone():
             c.execute("INSERT INTO trade_history_positions SELECT * FROM open_positions")
             print("Archived to trade_history_positions")
        
        # 3. Delete all open positions (Ghost positions cleanup)
        c.execute("DELETE FROM open_positions")
        conn.commit()
        print("✅ Successfully cleared open_positions table.")
    else:
        print("Table already empty.")
    
    conn.close()
except Exception as e:
    print(f"❌ Error: {e}")
