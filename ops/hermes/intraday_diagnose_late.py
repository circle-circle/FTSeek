#!/usr/bin/env python3
"""FTSeek-US Intraday Diagnosis (no_agent cron script)

Runs during US market hours (schedule: */30 0-4 * * 2-6 CST = 12:00-16:30 ET).
Outputs a formatted Chinese status report to stdout.
Empty output on non-trading days (= SILENT, no delivery).

Usage (via cronjob no_agent=True):
  script: intraday_diagnose_late.py

No LLM needed — all logic is deterministic DB queries + formatting.
"""
import sqlite3
import os
import sys
import subprocess
from datetime import datetime, timezone, timedelta

DB_PATH = '/opt/ftseek/FTSeek-US/data/ftseek_us.db'
SIGNAL_DB_PATH = '/opt/ftseek/FTSeek-US/data/signal_snapshots.db'
LOG_PATH = '/opt/ftseek/FTSeek-US/logs/ftseek_us.log'
HEALTHCHECK_LOG = '/opt/ftseek/FTSeek-US/logs/healthcheck.log'
VENV_PYTHON = '/opt/ftseek/FTSeek-US/venv/bin/python3'
TRADING_DAY_CHECK = '/opt/ftseek/FTSeek-US/scripts/trading_day_check.py'

# 8/27 时钟统一: 硬编码 -4 → zoneinfo 真实 ET (DST 自适); 极端环境落固定偏移兜底
try:
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
except Exception:
    et = timezone(timedelta(hours=-4))  # 兜底: 无 DST 感知
now_et = datetime.now(et)
today = now_et.strftime('%Y-%m-%d')
weekday = now_et.weekday()

# ============================================================
# Trading Day Check — exit 0 (SILENT) if non-trading
# ============================================================
is_trading = True

# Quick ET weekday check (Mon-Fri = 0-4)
if weekday >= 5:
    is_trading = False
else:
    # Try holiday-aware check
    if os.path.exists(TRADING_DAY_CHECK) and os.path.exists(VENV_PYTHON):
        try:
            result = subprocess.run(
                [VENV_PYTHON, TRADING_DAY_CHECK, '--market', 'US'],
                capture_output=True, text=True, timeout=10,
                cwd='/opt/ftseek/FTSeek-US'
            )
            if result.returncode != 0:
                is_trading = False
        except Exception:
            pass  # If check fails, assume trading (better to report than miss)

if not is_trading:
    sys.exit(0)  # Empty stdout = SILENT

# ============================================================
# Diagnostics
# ============================================================
lines = []
def add(s):
    lines.append(s)

# --- Service status ---
svc_active = False
svc_uptime = ''
try:
    result = subprocess.run(
        ['systemctl', 'status', 'ftseek-us', '--no-pager'],
        capture_output=True, text=True, timeout=5
    )
    for line in result.stdout.split('\n')[:5]:
        if 'Active:' in line:
            svc_active = 'active (running)' in line
            # Extract uptime
            m = line.split('since')
            if len(m) > 1:
                svc_uptime = m[1].strip()[:60]
            break
except Exception:
    pass

# --- DB queries ---
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row

# 1. Capital state
equity = '?'
cash = '?'
market_val = '?'
drawdown = '?'
peak = '?'
water_level = '?'
try:
    row = db.execute('SELECT * FROM capital_state ORDER BY rowid DESC LIMIT 1').fetchone()
    if row:
        d = dict(row)
        equity = d.get('current_equity', '?')
        cash = d.get('cash', '?')
        market_val = d.get('market_value', '?')
        drawdown = d.get('drawdown_pct', '?')
        peak = d.get('peak_equity', '?')
        water_level = d.get('water_level', '?')
except Exception as e:
    add(f"[capital_state查询失败] {e}")

# 2. Open positions
positions = []
try:
    positions = db.execute('''
        SELECT symbol, direction, size, avg_entry_price, current_sl, initial_tp,
               entry_time, mode
        FROM open_positions WHERE size > 0
        ORDER BY entry_time DESC
    ''').fetchall()
except Exception as e:
    add(f"[持仓查询失败] {e}")

# 3. Today's playbook instructions
instr_total = 0
instr_trig = 0
instr_exec = 0
instr_rows = []
reentry_instr = []
try:
    instr_rows = db.execute('''
        SELECT symbol, direction, confidence, triggered, executed, outcome, pnl, decision_id
        FROM playbook_instructions WHERE date = ? AND direction != 'SKIP'
        ORDER BY rowid
    ''', (today,)).fetchall()
    instr_total = len(instr_rows)
    for r in instr_rows:
        d = dict(r)
        if d.get('triggered'): instr_trig += 1
        if d.get('executed'): instr_exec += 1
        if (d.get('decision_id') or '').startswith('intraday_reentry'):
            reentry_instr.append(d)
except Exception as e:
    add(f"[playbook_instructions查询失败] {e}")

# 4. Today's trade records (净口径 pnl_net — 8/20 裁决)
trades = []
total_pnl = 0.0
wins = 0
losses = 0
reentry_trades = []
try:
    trades = db.execute('''
        SELECT symbol, direction, realized_pnl, pnl_net, pnl_pct, exit_reason, entry_session,
               entry_price, exit_price, decision_id
        FROM trade_records WHERE trade_date LIKE ?
        ORDER BY rowid
    ''', (f'{today}%',)).fetchall()
    for r in trades:
        d = dict(r)
        # COALESCE(pnl_net, realized_pnl) 净口径
        pnl = d.get('pnl_net')
        if pnl is None:
            pnl = d.get('realized_pnl', 0) or 0
        d['pnl_report'] = pnl
        total_pnl += pnl
        if pnl > 0: wins += 1
        elif pnl < 0: losses += 1
        if (d.get('decision_id') or '').startswith('intraday_reentry'):
            reentry_trades.append(d)
except Exception as e:
    add(f"[trade_records查询失败] {e}")

# 5. VIX
vix = '?'
vix_src = ''
try:
    row = db.execute('''
        SELECT entry_vix FROM trade_records
        WHERE entry_vix IS NOT NULL AND entry_vix > 0
        ORDER BY rowid DESC LIMIT 1
    ''').fetchone()
    if row:
        vix = row[0]
        vix_src = 'trade_records'
    else:
        row2 = db.execute('''
            SELECT vix_at_generation FROM playbook_instructions
            WHERE vix_at_generation IS NOT NULL AND vix_at_generation > 0
            ORDER BY rowid DESC LIMIT 1
        ''').fetchone()
        if row2:
            vix = row2[0]
            vix_src = 'playbook_instructions'
except Exception:
    pass

# 6. Regime (from signal_snapshots.db)
regime = '?'
regime_trigger = ''
regime_rationale = ''
regime_vix_zero = False
try:
    if os.path.exists(SIGNAL_DB_PATH):
        sdb = sqlite3.connect(SIGNAL_DB_PATH)
        sdb.row_factory = sqlite3.Row
        rh = sdb.execute('SELECT * FROM regime_history ORDER BY rowid DESC LIMIT 1').fetchone()
        if rh:
            d = dict(rh)
            regime = f"{d.get('old_regime','?')}→{d.get('new_regime','?')}"
            regime_trigger = d.get('trigger', '')
            regime_rationale = d.get('rationale', '')
            if d.get('vix', 0) == 0:
                regime_vix_zero = True
        sdb.close()
except Exception:
    pass

# 7. Today's signal snapshots
sig_count = 0
sig_top = []
try:
    if os.path.exists(SIGNAL_DB_PATH):
        sdb = sqlite3.connect(SIGNAL_DB_PATH)
        sdb.row_factory = sqlite3.Row
        rows = sdb.execute('''
            SELECT symbol, direction, strength, confidence, trade_date
            FROM signal_snapshots WHERE trade_date LIKE ?
            ORDER BY id DESC LIMIT 10
        ''', (f'{today}%',)).fetchall()
        sig_count = len(rows)
        sig_top = [dict(r) for r in rows]
        sdb.close()
except Exception:
    pass

# 8. Error log scan (last 5 min)
error_count = 0
recent_errors = []
try:
    if os.path.exists(LOG_PATH):
        result = subprocess.run(
            ['grep', '-c', 'ERROR', LOG_PATH],
            capture_output=True, text=True, timeout=5
        )
        error_count = int(result.stdout.strip()) if result.stdout.strip().isdigit() else 0
        # Get last 5 ERROR lines
        result2 = subprocess.run(
            ['grep', 'ERROR', LOG_PATH],
            capture_output=True, text=True, timeout=10
        )
        all_errors = result2.stdout.strip().split('\n') if result2.stdout.strip() else []
        recent_errors = all_errors[-5:] if all_errors else []
except Exception:
    pass

# 9. Healthcheck
healthcheck_ok = False
try:
    if os.path.exists(HEALTHCHECK_LOG):
        result = subprocess.run(
            ['tail', '-1', HEALTHCHECK_LOG],
            capture_output=True, text=True, timeout=5
        )
        healthcheck_ok = '✅' in result.stdout
except Exception:
    pass

db.close()

# ============================================================
# Format Report
# ============================================================
time_str = now_et.strftime('%Y-%m-%d %H:%M ET')

add(f"📊 盘中状态 {time_str}")
add("")
add(f"• 持仓: {len(positions)}只, equity=${equity}, cash=${cash}")
if positions:
    for p in positions:
        d = dict(p)
        add(f"  {d['symbol']:8s} size={d['size']} dir={d.get('direction','?')} entry=${d.get('avg_entry_price','?')} SL={d.get('current_sl','?')} TP={d.get('initial_tp','?')}")

add(f"• 今日指令: {instr_total}条 → 触发{instr_trig}条✅, 执行{instr_exec}条✅, {instr_total - instr_trig}条未触发")
if reentry_instr:
    re_trig = sum(1 for d in reentry_instr if d.get('triggered'))
    re_exec = sum(1 for d in reentry_instr if d.get('executed'))
    add(f"  └ 其中盘中再入场: {len(reentry_instr)}条 (触发{re_trig}/执行{re_exec})")
    for d in reentry_instr:
        add(f"    {d['symbol']:8s} {d.get('direction','?'):9s} trig={'✅' if d.get('triggered') else '—'} exec={'✅' if d.get('executed') else '—'}")

if trades:
    add(f"• 今日交易: {len(trades)}笔 → {wins}胜{losses}负, 已实现净PnL = ${total_pnl:.2f}")
    for t in trades:
        d = dict(t)
        pnl = d.get('pnl_net')
        if pnl is None:
            pnl = d.get('realized_pnl', 0) or 0
        mark = ' [再入场]' if (d.get('decision_id') or '').startswith('intraday_reentry') else ''
        add(f"  {d['symbol']:8s} {d.get('direction','?'):8s} pnl=${pnl:>8.2f} reason={d.get('exit_reason','-')}{mark}")
    if reentry_trades:
        re_pnl = 0.0
        re_w = re_l = 0
        for d in reentry_trades:
            p = d.get('pnl_net')
            if p is None:
                p = d.get('realized_pnl', 0) or 0
            re_pnl += p
            if p > 0: re_w += 1
            elif p < 0: re_l += 1
        add(f"  └ 再入场小计: {len(reentry_trades)}笔 {re_w}胜{re_l}负 净${re_pnl:.2f}")
else:
    add(f"• 今日交易: 0笔")

add(f"• 盘面: regime={regime} trigger={regime_trigger}, VIX={vix}({vix_src})")
if regime_vix_zero:
    add(f"  ⚠️ regime_history.vix=0.0 (数据未注入, rationale: {regime_rationale[:80]})")

add(f"• 信号: 今日{sig_count}条 snapshots")
if sig_top:
    for s in sig_top[:5]:
        add(f"  {s.get('symbol','?'):8s} dir={s.get('direction','?')} strength={s.get('strength','?')} conf={s.get('confidence','?')}")

add(f"• 异常: {'无' if error_count == 0 else f'{error_count}个ERROR'}")
if recent_errors and error_count > 0:
    for e in recent_errors[-3:]:
        add(f"  {e[:120]}")

# Notable findings
findings = []
if total_pnl < 0:
    findings.append(f"今日PnL为负(${total_pnl:.2f}), 关注大亏交易")
if drawdown != '?' and float(drawdown) > 5:
    findings.append(f"回撤{drawdown}%超过5%")
if regime_vix_zero:
    findings.append("regime_history vix/spy_price=0.0数据未注入")
if not svc_active:
    findings.append("⚠️ ftseek-us服务未运行!")
if float(vix) > 25 if vix not in ('?', None) else False:
    findings.append(f"VIX={vix}>25, 市场波动加剧")

if findings:
    add("")
    add("⚠️ 关注点:")
    for i, f in enumerate(findings, 1):
        add(f"{i}. {f}")

# Output to stdout — delivered verbatim by no_agent cron
print('\n'.join(lines))
