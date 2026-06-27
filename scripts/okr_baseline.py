#!/usr/bin/env python3
"""OKR Baseline Assessment - 2026-06-19"""
import sys
sys.path.insert(0, '/opt/ftseek/FTSeek-US')

from app.playbook.db import PlaybookDB
from datetime import datetime, timedelta

# 统一口径: Playbook 系统 OKR 只统计 Playbook 相关来源
PLAYBOOK_SOURCE_TYPES = ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE', 'PLAYBOOK_SHADOW', 'MANUAL_PLAYBOOK')

db = PlaybookDB()
db.ensure_tables()

# 1. Playbook instructions
instructions = db.get_recent_instructions(limit=200)
total = len(instructions)
skip_count = sum(1 for i in instructions if i.get('direction') == 'SKIP')
non_skip = total - skip_count
executed = sum(1 for i in instructions if i.get('executed'))
triggered = sum(1 for i in instructions if i.get('triggered'))
has_outcome = sum(1 for i in instructions if i.get('outcome') in ('win', 'loss'))

wins = sum(1 for i in instructions if i.get('outcome') == 'win')
losses = sum(1 for i in instructions if i.get('outcome') == 'loss')
# OKR PnL 只统计已执行且终态 win/loss 的指令
total_pnl = sum((i.get('pnl') or 0) for i in instructions
                if i.get('executed') and i.get('outcome') in ('win', 'loss'))
win_rate = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0

# 2. Weekly breakdown (只统计 executed + win/loss)
weekly_stats = {}
for inst in instructions:
    if not (inst.get('executed') and inst.get('outcome') in ('win', 'loss')):
        continue
    if inst.get('date'):
        dt = datetime.strptime(inst['date'], '%Y-%m-%d')
        week_key = dt.strftime('%Y-W%W')
        if week_key not in weekly_stats:
            weekly_stats[week_key] = {'count': 0, 'wins': 0, 'losses': 0, 'pnl': 0}
        weekly_stats[week_key]['count'] += 1
        p = inst.get('pnl') or 0
        weekly_stats[week_key]['pnl'] += p
        if p > 0:
            weekly_stats[week_key]['wins'] += 1
        elif p < 0:
            weekly_stats[week_key]['losses'] += 1

# 3. Trade records (Playbook only)
from app.storage.db_pool import get_db
conn = get_db("ftseek_us").connection()
_source_filter = ", ".join(f"'{s}'" for s in PLAYBOOK_SOURCE_TYPES)
try:
    cursor = conn.execute(f"""
        SELECT
            COUNT(*) as cnt,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) as losses,
            SUM(realized_pnl) as total_pnl
        FROM trade_records
        WHERE trade_date >= date('now', '-30 days')
          AND source_type IN ({_source_filter})
    """)
    row = cursor.fetchone()
    trade_stats = dict(row) if row else {}
except Exception as e:
    trade_stats = {"error": str(e)}

# 4. By source (Playbook only)
try:
    cursor2 = conn.execute(f"""
        SELECT
            source_type,
            COUNT(*) as cnt,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(realized_pnl) as total_pnl
        FROM trade_records
        WHERE trade_date >= date('now', '-30 days')
          AND source_type IN ({_source_filter})
        GROUP BY source_type
    """)
    by_source = [dict(r) for r in cursor2.fetchall()]
except Exception as e:
    by_source = [{"error": str(e)}]

# 5. Config state
try:
    from app.core.config_container import get_config
    cfg = get_config()
    playbook_enabled = cfg.get_dotted('playbook.enabled', False)
    execute_mode = cfg.get_dotted('playbook.execute_mode', 'simulate')
    auto_execute = cfg.get_dotted('playbook.auto_execute', False)
except:
    playbook_enabled = execute_mode = auto_execute = "unknown"

print("=" * 60)
print("OKR BASELINE - 2026-06-19")
print("=" * 60)
print(f"\n【Playbook Instructions (last 200)】")
print(f"  Total: {total} (SKIP: {skip_count}, active: {non_skip})")
print(f"  Triggered: {triggered} ({triggered/non_skip*100:.1f}% of active)" if non_skip > 0 else "  Triggered: 0")
print(f"  Executed: {executed}")
print(f"  Has outcome: {has_outcome}")
print(f"  Wins/Losses: {wins}/{losses}")
if (wins+losses) > 0:
    print(f"  Win rate: {win_rate:.1f}%")
    print(f"  Total PnL: ${total_pnl:.2f}")
else:
    print(f"  Win rate: N/A (no outcomes)")
    print(f"  Total PnL: $0.00")

print(f"\n【Weekly Breakdown】")
for week in sorted(weekly_stats.keys(), reverse=True)[:5]:
    s = weekly_stats[week]
    wr = s['wins']/(s['wins']+s['losses'])*100 if (s['wins']+s['losses']) > 0 else 0
    print(f"  {week}: {s['count']} trades, WR={wr:.0f}%, PnL=${s['pnl']:.2f}")

print(f"\n【Trade Records (30d)】")
if 'error' not in trade_stats:
    print(f"  Count: {trade_stats.get('cnt', 0)}")
    print(f"  Wins: {trade_stats.get('wins', 0)}")
    print(f"  Losses: {trade_stats.get('losses', 0)}")
    print(f"  Total PnL: ${trade_stats.get('total_pnl', 0):.2f}")
else:
    print(f"  Error: {trade_stats['error']}")

print(f"\n【By Source】")
for src in by_source:
    if 'error' in src:
        print(f"  Error: {src['error']}")
    else:
        wr = src['wins']/src['cnt']*100 if src['cnt'] > 0 else 0
        print(f"  {src.get('source_type', 'unknown')}: {src['cnt']} trades, WR={wr:.0f}%, PnL=${src.get('total_pnl',0):.2f}")

print(f"\n【Config State】")
print(f"  playbook.enabled: {playbook_enabled}")
print(f"  playbook.execute_mode: {execute_mode}")
print(f"  playbook.auto_execute: {auto_execute}")
print("=" * 60)
