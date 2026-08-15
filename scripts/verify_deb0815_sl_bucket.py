"""§35/§33.2 证据点抽查: 8月指令SL距离分布 + 9:30-10:15窗期望."""
import json
import sqlite3

con = sqlite3.connect('file:data/ftseek_us.db?mode=ro', uri=True)
con.row_factory = sqlite3.Row

# 8月指令 SL 距离 (§35: 分桶原值 2.13%~5.02%, LLY 2.13/META 5.0/AMZN 5.02)
rows = con.execute("""
    SELECT symbol, direction, entry_time, entry_price, metadata_json
    FROM trade_records
    WHERE strategy_id='playbook_sim' AND entry_time>='2026-08-01'
""").fetchall()
seen = {}
for r in rows:
    try:
        meta = json.loads(r['metadata_json'] or '{}')
    except Exception:
        continue
    sl = meta.get('stop_loss')
    ep = r['entry_price']
    if sl and ep:
        dist = abs(ep - sl) / ep * 100
        key = r['symbol']
        if key not in seen or r['entry_time'] < seen[key][0]:
            seen[key] = (r['entry_time'], dist, r['direction'])
print('8月指令 SL距离 (per symbol 最早一笔, 注意: metadata是β=0.5打折后还是原值需辨别):')
for k, (t, dist, dirn) in sorted(seen.items(), key=lambda kv: kv[1][1]):
    print(f'  {k}: {dist:.2f}% ({dirn} {t[:10]})')

# §33.2: 9:30-10:15窗 n=181 净-$1,111
rows2 = con.execute("""
    SELECT entry_time, pnl_net FROM trade_records
    WHERE strategy_id='playbook_sim' AND pnl_net IS NOT NULL
""").fetchall()
win = [r for r in rows2 if 570 <= int(r['entry_time'][11:13]) * 60 + int(r['entry_time'][14:16]) < 615]
print(f'\n9:30-10:15窗: n={len(win)} pnl={sum(r["pnl_net"] for r in win):+.0f} (expect n=181 -1111)')
con.close()
