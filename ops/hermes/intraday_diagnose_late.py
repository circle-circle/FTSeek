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
import json
import re
from datetime import datetime, timezone, timedelta

DB_PATH = '/opt/ftseek/FTSeek-US/data/ftseek_us.db'
SIGNAL_DB_PATH = '/opt/ftseek/FTSeek-US/data/signal_snapshots.db'
LOG_PATH = '/opt/ftseek/FTSeek-US/logs/ftseek_us.log'
HEALTHCHECK_LOG = '/opt/ftseek/FTSeek-US/logs/healthcheck.log'
VENV_PYTHON = '/opt/ftseek/FTSeek-US/venv/bin/python3'
TRADING_DAY_CHECK = '/opt/ftseek/FTSeek-US/scripts/trading_day_check.py'
PNL_CONCERN_USD = 100.0  # 小额负 PnL 只展示，不升级成「关注大亏」事件

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
            if "NON_TRADING_DAY" in (result.stdout or "").upper():  # 2026-09-07 修: 原判据 returncode!=0 恒不成立 — trading_day_check.py 契约是 exit 恒 0 / 按 stdout 判断 ⇒ 假日守卫曾退化为只排周末
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

# 2b. 授权止损/止盈 (2026-10-31, 用户裁"要") — 展示口径改用 trade_records.metadata_json
#   的 stop_loss / take_profit。open_positions.current_sl / initial_tp 是**建仓镜像**:
#   position_sync 通用路径按 ATR×exit_policy.max_loss_pct(2%) 夹逼写入, playbook 退出链
#   从不回写 (授权值只在 metadata_json)。两源不同源, 10-30 MRK 实锤:
#     镜像 SL 147.5145 (-2.0%) vs 授权 SL 143.90 (-4.40%, 到位亏 -$258)
#     镜像 TP 190.285        vs 授权 TP 162.89 (RR=1.87 已过 min_tp_rr 1.5, 真会触发)
#   镜像未回落时报告=高估保护, 故优先授权值; 授权值缺失才回落镜像 (fail-open:
#   展示不得因取数失败而整行消失)。
auth_sl_tp = {}
try:
    for _r in db.execute('''
        SELECT symbol, metadata_json FROM trade_records
        WHERE exit_time IS NULL
          AND COALESCE(source_type, '') IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE')
        ORDER BY rowid
    '''):
        _sym = (_r['symbol'] or '').strip().upper()
        _md = _r['metadata_json']
        if not _sym or not _md:
            continue
        try:
            _m = json.loads(_md)
        except (ValueError, TypeError):
            _m = None
        if not isinstance(_m, dict):
            continue
        _sl = _m.get('stop_loss')
        _tp = _m.get('take_profit')
        if _sl or _tp:
            auth_sl_tp[_sym] = (_sl, _tp)   # 同标的多笔: 后写覆盖 = 以最新一笔为准
except Exception as e:
    add(f"[授权SL/TP查询失败, 已回落镜像列] {e}")

# 3. Today's playbook instructions
instr_total = 0
instr_trig = 0
instr_exec = 0
instr_rows = []
reentry_instr = []
instr_blocked = {}      # gate -> [symbol]: 已触发但被门拦下的 (2026-10-31 加)
try:
    instr_rows = db.execute('''
        SELECT symbol, direction, confidence, triggered, executed, outcome, pnl,
               decision_id, skip_reason
        FROM playbook_instructions WHERE date = ? AND direction != 'SKIP'
        ORDER BY rowid
    ''', (today,)).fetchall()
    instr_total = len(instr_rows)
    for r in instr_rows:
        d = dict(r)
        if d.get('triggered'): instr_trig += 1
        if d.get('executed'): instr_exec += 1
        # 触发未执行 ≠ 未触发: 归因到拦它的门 (chase_cap / vwap_gate / 预算帽 …)
        if d.get('triggered') and not d.get('executed'):
            _sr = (d.get('skip_reason') or '').strip()
            _gate = _sr.split('(')[0].split('—')[0].strip() or 'pending(无skip_reason)'
            instr_blocked.setdefault(_gate, []).append(d.get('symbol', '?'))
        if (d.get('decision_id') or '').startswith('intraday_reentry'):
            reentry_instr.append(d)
except Exception as e:
    add(f"[playbook_instructions查询失败] {e}")

# 4. Today's trade records (净口径 pnl_net — 8/20 裁决)
# 口径说明 (2026-09-08 加注): 本段 = 入场日口径 (trade_date=今日, 今日开仓且已了结)。
#   心跳/盘后复盘"今日块"/周日周报/熔断器 = 出场日口径 (substr(exit_time,1,10)=今日),
#   隔夜仓在了结日计入。两口径差额 = 今日了结的隔夜仓, 见 4b 对账备注。
#   exit_price>0 过滤: 未平仓行不属于"已实现净PnL" (持仓段已单独展示)。
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
          AND exit_price > 0 AND COALESCE(pnl_net, realized_pnl) IS NOT NULL
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

# 4b. 隔夜对账 (2026-09-08 加): 出场日=今日 但 入场日≠今日 的已平仓 —
#     心跳/盘后复盘按出场日口径把它们计入"今日", 本段(入场日)不计 → 数字差异全在这里。
overnight_closed = []
overnight_net = 0.0
try:
    overnight_closed = db.execute('''
        SELECT symbol, direction, realized_pnl, pnl_net, exit_reason, trade_date, exit_time
        FROM trade_records WHERE substr(exit_time, 1, 10) = ?
          AND trade_date NOT LIKE ?
          AND exit_price > 0 AND COALESCE(pnl_net, realized_pnl) IS NOT NULL
        ORDER BY exit_time
    ''', (today, f'{today}%')).fetchall()
    for r in overnight_closed:
        d = dict(r)
        overnight_net += d['pnl_net'] if d['pnl_net'] is not None else (d['realized_pnl'] or 0)
except Exception as e:
    add(f"[隔夜对账查询失败] {e}")

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

# 6. Regime — 活源: market_regime.py 日志 "[Regime] Assessment" 行 (~5分钟一次, 直接驱动仓位乘数)
# 2026-09-05 根因核查后弃用 regime_history 表: 唯一写者是旧链
#   signal_stage._get_regime_verdict → evaluate_regime_with_hysteresis → record_regime_change,
#   playbook 化后该链停跑 (8/23-9/5 全部轮转日志 RegimeHysteresis=0 行), 最后一行永远停在
#   2026-07-29 且 vix/spy=0.0; 生产代码零读者 (仅本诊断在读) → 死表, 两条常驻告警均为误报。
regime = '?'
regime_detail = ''
regime_stale = True
_ts_s = ''
_ass_age = 0
try:
    import re as _re
    _ass_pat = _re.compile(
        r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*\[Regime\] Assessment: (\w+) (\([^)]*\)) \| (.*?) \| VIX=([\d.]+)')
    _line = None
    _now_cst = datetime.now()  # 服务器= CST, 日志时间戳同
    # 先查活日志, 再查当日 CST 轮转件 (轮转后头几分钟活日志可能还没有 Assessment)
    for _lf in (LOG_PATH, LOG_PATH + '-' + _now_cst.strftime('%Y%m%d')):
        if not os.path.exists(_lf):
            continue
        try:
            _p = subprocess.run(['grep', '-aF', '[Regime] Assessment', _lf],
                                capture_output=True, text=True, timeout=15)
            _tail = _p.stdout.strip().splitlines()
            if _tail:
                _line = _tail[-1]
                break
        except Exception:
            pass
    if _line:
        _m = _ass_pat.search(_line)
        if _m:
            _ts_s, regime, _score, _spy_part, _vix_log = _m.groups()
            regime_detail = f"{_score} {_spy_part.strip()} VIX={_vix_log}"
            try:
                _age = (_now_cst - datetime.strptime(_ts_s, '%Y-%m-%d %H:%M:%S')).total_seconds()
                _ass_age = int(_age // 60)
                # 16:00 ET 收盘后 playbook 循环停跑, Assessment 不再产出 → 陈旧是预期, 非故障
                # (9/15 确诊: 每日最后一轮 04:30 CST=16:30 ET 必误报, 9/12 同样复现)
                regime_stale = _age > 20 * 60 and now_et.hour < 16
            except Exception:
                regime_stale = True
except Exception:
    regime_stale = True

# 7. Today's signal snapshots
sig_count = 0
sig_top = []
try:
    if os.path.exists(SIGNAL_DB_PATH):
        sdb = sqlite3.connect(SIGNAL_DB_PATH)
        sdb.row_factory = sqlite3.Row
        # 计数与展示分离: COUNT(*) 全量口径 (原 len(LIMIT 10 行) 少报总数)
        sig_count = sdb.execute(
            'SELECT COUNT(*) FROM signal_snapshots WHERE trade_date LIKE ?',
            (f'{today}%',)).fetchone()[0]
        rows = sdb.execute('''
            SELECT symbol, direction, strength, confidence, trade_date
            FROM signal_snapshots WHERE trade_date LIKE ?
            ORDER BY id DESC LIMIT 10
        ''', (f'{today}%',)).fetchall()
        sig_top = [dict(r) for r in rows]
        sdb.close()
except Exception:
    pass

# 8. Error log scan — 只算当前 ET 交易日的 ERROR, 并按「事件」去重 (2026-10-31 修)
#   旧形态两个病灶 (10-30 12:00 报告实锤):
#   ① 无日期过滤: `grep -c ERROR <当前文件>` ⇒ 轮转前 (03:30 CST 起) 会把上一 ET 交易日的
#      ERROR 当"今日异常"反复推送 (实锤: 文件首两行 2026-10-30 03:31:29 CST = 10-29 15:31 ET,
#      llm_strategy_loop 停摆重启事件, 在 10-30 全天每轮推送里被重复报告)
#   ② 同一次故障的告警行 + [Watchdog.heal] 动作行被计成 2 个 ERROR
#   口径: 日志时间戳是宿主机 CST 墙钟 ⇒ naive 解析后 astimezone() 挂本机时区再转 ET
#        (不硬编码偏移); 无时间戳的裸 print 行归到最近的上一条带时间戳行 (同日志锚定纪律);
#        定位不到时间的行不计入 (宁少报不虚报)。
error_count = 0
recent_errors = []
try:
    if os.path.exists(LOG_PATH):
        _ts_re = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
        _lg_re = re.compile(r'^.*?\[([^\]\s]+)\] \[([^\]]+)\] (ERROR|CRITICAL):\s*(.*)$')
        _cur_et = None
        _events = {}   # (ET秒级时刻, logger) -> 展示行; 同秒同 logger = 同一事件
        with open(LOG_PATH, 'r', encoding='utf-8', errors='replace') as _lf:
            for _line in _lf:
                _m = _ts_re.match(_line)
                if _m:
                    try:
                        _cur_et = datetime.strptime(
                            _m.group(1), '%Y-%m-%d %H:%M:%S'
                        ).astimezone().astimezone(et)
                    except (ValueError, OSError):
                        _cur_et = None
                if 'ERROR:' not in _line and 'CRITICAL:' not in _line:
                    continue
                if _cur_et is None or _cur_et.strftime('%Y-%m-%d') != today:
                    continue
                _g = _lg_re.match(_line.rstrip('\n'))
                if _g:
                    _lvl, _logger, _msg = _g.group(3), _g.group(2), _g.group(4)
                    _thread = _g.group(1)
                else:
                    _lvl, _logger, _msg, _thread = 'ERROR', '?', _line.strip(), '?'
                _key = (_cur_et.replace(microsecond=0).isoformat(), _logger)
                if _key not in _events:
                    _events[_key] = (
                        f"{_cur_et.strftime('%Y-%m-%d %H:%M')} ET [{_thread}] "
                        f"{_lvl}: {_msg[:100]}"
                    )
        error_count = len(_events)
        recent_errors = list(_events.values())
except Exception as _ee:
    add(f"[日志ERROR扫描失败] {_ee}")

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
        # 授权值优先 (见 2b), 缺失才回落 open_positions 镜像列
        _a = auth_sl_tp.get((d.get('symbol') or '').strip().upper())
        _sl = _a[0] if (_a and _a[0] is not None) else d.get('current_sl', '?')
        _tp = _a[1] if (_a and _a[1] is not None) else d.get('initial_tp', '?')
        add(f"  {d['symbol']:8s} size={d['size']} dir={d.get('direction','?')} entry=${d.get('avg_entry_price','?')} SL={_sl} TP={_tp}")

add(f"• 今日指令: {instr_total}条 → 触发{instr_trig}条✅, 执行{instr_exec}条✅, {instr_total - instr_trig}条未触发")
if instr_blocked:
    _nb = sum(len(v) for v in instr_blocked.values())
    _detail = '; '.join(f"{_g} {len(_v)}: {'/'.join(_v)}" for _g, _v in instr_blocked.items())
    add(f"  └ 触发未执行: {_nb}条 ({_detail})")
if reentry_instr:
    re_trig = sum(1 for d in reentry_instr if d.get('triggered'))
    re_exec = sum(1 for d in reentry_instr if d.get('executed'))
    add(f"  └ 其中盘中再入场: {len(reentry_instr)}条 (触发{re_trig}/执行{re_exec})")
    for d in reentry_instr:
        add(f"    {d['symbol']:8s} {d.get('direction','?'):9s} trig={'✅' if d.get('triggered') else '—'} exec={'✅' if d.get('executed') else '—'}")

if trades:
    add(f"• 今日交易(入场日口径): {len(trades)}笔 → {wins}胜{losses}负, 已实现净PnL = ${total_pnl:.2f}")
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
    add(f"• 今日交易(入场日口径): 0笔")
# 隔夜对账备注: 与心跳/盘后复盘(出场日口径)数字对账
if overnight_closed:
    add(f"  🌙 另: 隔夜仓今日了结 {len(overnight_closed)}笔 净${overnight_net:.2f} (出场日口径计入今日, 本段不含)")
    for r in overnight_closed:
        d = dict(r)
        p = d['pnl_net'] if d['pnl_net'] is not None else (d['realized_pnl'] or 0)
        add(f"    {d['symbol']:8s} {d.get('direction','?'):8s} 入场{d.get('trade_date','?')[:10]} pnl=${p:>8.2f} reason={d.get('exit_reason','-')}")
    add(f"  → 出场日口径合计(=心跳'已平盈亏今日'): ${total_pnl + overnight_net:.2f}")

if regime_stale:
    add(f"• 盘面: ⚠️ regime 实时评估缺失/陈旧 (market_regime Assessment 超20分钟未见), VIX={vix}({vix_src})")
elif regime != '?' and now_et.hour >= 16 and _ass_age > 20:
    add(f"• 盘面: regime={regime} {regime_detail} (收盘定格值, 末次评估{_ass_age}分钟前), VIX={vix}({vix_src})")
else:
    add(f"• 盘面: regime={regime} {regime_detail}, VIX={vix}({vix_src})")

add(f"• 信号: 今日{sig_count}条 snapshots")
if sig_top:
    for s in sig_top[:5]:
        add(f"  {s.get('symbol','?'):8s} dir={s.get('direction','?')} strength={s.get('strength','?')} conf={s.get('confidence','?')}")

add(f"• 异常: {'无' if error_count == 0 else f'{error_count}个ERROR事件(ET今日, 已按事件去重)'}")
if recent_errors and error_count > 0:
    for e in recent_errors[-3:]:
        add(f"  {e[:130]}")

# Notable findings
findings = []
if total_pnl + overnight_net <= -PNL_CONCERN_USD:
    findings.append(f"今日PnL显著为负(入场日口径${total_pnl:.2f}/出场日口径${total_pnl + overnight_net:.2f}), 关注大亏交易")
if drawdown != '?' and float(drawdown) > 5:
    findings.append(f"回撤{drawdown}%超过5%")
if regime_stale:
    findings.append("regime 实时评估缺失/陈旧 (market_regime Assessment 超20分钟未见) — 盘面判定可能失准")
if not svc_active:
    findings.append("⚠️ ftseek-us服务未运行!")
if float(vix) > 25 if vix not in ('?', None) else False:
    findings.append(f"VIX={vix}>25, 市场波动加剧")

if findings:
    add("")
    add("⚠️ 关注点:")
    for i, f in enumerate(findings, 1):
        add(f"{i}. {f}")

# ── B2/项3 (2026-10-06 推送治理批): 语义无变化则静默 ──────────────────
# 病灶: 本载体每 30min 无条件投递, 交易结束后内容语义冻结 (仅时间戳 + equity/cash
#   浮点漂移 + SPY/VIX 微变), 仍照发。实测冗余率 **59%** (近8天 22 条投递 →
#   语义去重后 9 条): 10-06 场景 01:00 CST 起 10 笔全平 PnL=$77.15 后, 01:30–04:30
#   共 8 条投递的逐笔明细/指令计数/持仓结构完全相同。
# 归一化只剔除**被证明易变的量**, 交易数据一字不动:
#   · 时间戳行 (📊 盘中状态 ... ET)
#   · equity=/cash= 浮点 (资金漂移不是交易事件; 大额出入金会改其它行, 仍会触发)
#   · SPY=/MA20=/MA50=/MA200=/VIX= 浮点 (含 VIX=14.0(trade_records) 那处)
#   · "末次评估N分钟前" 的 N
#   刻意**不**归一化: 持仓 size/dir/entry/SL/TP (trailing 移动 SL 是真事件)、
#   regime 名与 score (盘面翻转必须推)、指令/交易/信号计数与逐笔 pnl/reason、
#   异常段、关注点段。
# fail-open: 状态文件读写任何异常 ⇒ 照发, 绝不因去重逻辑吞掉一次真投递。
_LAST_HASH_PATH = '/opt/ftseek/FTSeek-US/data/.intraday_diagnose_late_last'
# 常驻严重告警不参与静默 — 服务未运行时重复提醒是特性不是噪音
_ALWAYS_DELIVER_MARKERS = ('服务未运行',)


def _semantic_fingerprint(text):
    """归一化后取 sha1 — 只抹掉被证明易变的量, 交易语义全保留."""
    import hashlib
    import re as _re
    t = text
    # ⚠️ 先抽出交易日 (标题行的 ET 日期) 作指纹前缀, **再**抹掉时间戳行 —
    #   顺序不可颠倒: 若整行直接删掉, 两个「零持仓零交易」的连续交易日会得到
    #   相同指纹 ⇒ 第二个交易日被静默吞掉 (实测抓到, 非纸面推演)。
    #   时间戳的**时分**仍是易变量 (要抹), 日期是语义 (要留)。
    m_day = _re.search(r'盘中状态\s+(\d{4}-\d{2}-\d{2})', t)
    day_prefix = (m_day.group(1) + '|') if m_day else ''
    # 时间戳行整行剔除 (日期已进前缀)
    t = _re.sub(r'^📊 盘中状态 .*$', '', t, flags=_re.M)
    # equity/cash 浮点
    t = _re.sub(r'(equity|cash)=\$[\d.,]+', r'\1=<F>', t)
    # 行情浮点 (SPY/MA20/MA50/MA200/VIX, 含尾随的 (trade_records) 变体)
    t = _re.sub(r'(SPY|MA20|MA50|MA200|VIX)=[\d.]+', r'\1=<F>', t)
    # 相对时长
    t = _re.sub(r'末次评估\d+分钟前', '末次评估<N>分钟前', t)
    t = _re.sub(r'\d+\s*(分钟|min)前', '<AGE>', t)
    return hashlib.sha1((day_prefix + _re.sub(r'\s+', '', t)).encode('utf-8')).hexdigest()


_out_text = '\n'.join(lines)

try:
    _fp = _semantic_fingerprint(_out_text)
    _prev = None
    if os.path.exists(_LAST_HASH_PATH):
        with open(_LAST_HASH_PATH, encoding='utf-8') as _f:
            _prev = _f.read().strip()
    _force = any(m in _out_text for m in _ALWAYS_DELIVER_MARKERS)
    if _prev and _prev == _fp and not _force:
        # 语义与上次投递完全一致 ⇒ 空 stdout = SILENT (no_agent 契约, 同非交易日分支)
        # 状态文件不重写 (值相同), 下次仍与它比对 — 长静默期不会因写失败而漏判
        sys.exit(0)
    _tmp = _LAST_HASH_PATH + '.tmp'
    with open(_tmp, 'w', encoding='utf-8') as _f:
        _f.write(_fp)
    os.replace(_tmp, _LAST_HASH_PATH)   # 原子替换, 不留半截文件
except Exception as _dedup_e:
    # fail-open: 去重逻辑坏了就照发, 绝不吞真投递; 2026-10-08 棘轮清剿: 留痕到 stderr
    # (stdout 是 no_agent 投递载体, 不能污染)
    print(f"[warn] 去重指纹写入失败 (本次照发): {_dedup_e}", file=sys.stderr)

# Output to stdout — delivered verbatim by no_agent cron
print(_out_text)
