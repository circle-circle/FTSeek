#!/usr/bin/env python3
"""FTSeek-US 盘中诊断 (no_agent cron script) — 事件门控版 (2026-10-09 用户裁方案A).

Job 36cc015242ee, */30 21-23 * * 1-5 (CST) = ET 09:00-11:30 每 30min 一轮。

旧版病灶 (2026-10-09 实测):
  · 每轮无条件推全量诊断, 一晚 6 条, 10 行指令表原样重播; 21:00/21:30 两条除
    时间戳外全同。而成交/平仓富卡已实时双发飞书 (broadcast_trade_events=on),
    outcome/pnl/持仓块全是迟到的二手信息 ⇒ 轮间信息增量≈0。
  · `grep -c 'ERROR|Traceback'` 少 -E ⇒ 匹配字面量, 该行自上线起恒 0 (死指标;
    同一日志 -E 真数 21 行)。
  · 当日唯一有信息量的事件 (US.ARM 触发未执行, 因 chase_cap_enforced) 躺在
    skip_reason 里不可见; 水位变化/退出卡单/ERROR 突发同样不推。

新输出契约 (no_agent: stdout=投递内容, 空 stdout=SILENT):
  1. 交易日首轮 (ET 09:00): 推一条「交易日计划」— 指令表(入场/SL/TP) +
     账户水位 + 隔夜持仓, 当日仅一次 (state 门控, 跨日必重推)。
  2. 其后各轮: 仅白名单事件才推, 每事件当日去重 (state 落盘, 跨进程持久):
     E1 ftseek-us 服务未运行 — 每轮重复 (重复提醒是特性, 不去重)
     E2 触发未执行超10min (带 skip_reason/execution_state) — 每(symbol,原因)一次
     E3 水位变非 green / is_locked 新出现 — 每次变化一次
     E4 退出账本 intent 非 BOOKED 超5min (卡单) — 每 intent_id 一次; 账本缺失也告警
     E5 日志 ERROR 突增 (本轮新增 ≥3 条) 或任意 CRITICAL — 每轮至多一条
        (旧版 grep 死指标改 Python 增量扫; ERROR ≥1 的细粒度告警在 error_alert.py
         钉钉侧已有, 此处只补飞书侧的突发可见性, 阈值更宽避免双通道同噪)
     E6 新增指令 (盘中再入场/UE 晚批) — 每 (symbol, decision_id) 一次
  3. 无事件 ⇒ 空 stdout = 静默。
  4. fail-open: state 读写任何异常 ⇒ 照发 (宁多推一条, 不吞事件)。
  5. 成交/持仓/逐笔 PnL 不再重播 — 实时富卡已双发飞书, 这里不做二手信息。

守卫契约 (勿破坏, 有测试钉住):
  · 三份副本字节一致: skill scripts/ ↔ ops/hermes/ ↔ ~/.hermes/scripts/
    (test_trading_day_check_contract.py::test_intraday_script_copies_are_identical)
  · 假日判据按 stdout '"NON_TRADING_DAY" in', 禁用返回码判据 (同上测试)
  · ZoneInfo 主路径 + timezone(timedelta(hours=-4)) 兜底恰一处
    (test_hardcoded_utc4_ratchet.py 白名单)
  · 无新增裸 except:pass (test_except_pass_ratchet.py 棘轮, 本文件恰 1 处存量)

来源: /root/.hermes/skills/devops/ftseek-intraday-cron-diagnosis/scripts/intraday_diagnose.py
  上游编辑请改 skill 副本, 本文件为 cron 直接调用的稳定快照。
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

DB_PATH = '/opt/ftseek/FTSeek-US/data/ftseek_us.db'
EXIT_DB_PATH = '/opt/ftseek/FTSeek-US/data/playbook_exits.db'
LOG_PATH = '/opt/ftseek/FTSeek-US/logs/ftseek_us.log'
VENV_PYTHON = '/opt/ftseek/FTSeek-US/venv/bin/python3'
TRADING_DAY_CHECK = '/opt/ftseek/FTSeek-US/scripts/trading_day_check.py'
STATE_PATH = '/opt/ftseek/FTSeek-US/data/.intraday_diagnose_events.json'

ERR_PUSH_MIN = 3        # 单轮新增 ERROR ≥ 此数才推 (日常 0-5 条/日; 突发如 PEP 案 269 条/30min)
STALE_EXEC_S = 600      # 触发未执行超 10min 才算事件 (避开正常执行延迟)
STUCK_INTENT_S = 300    # intent 非 BOOKED 超 5min = 卡单

# 8/27 时钟统一: 硬编码 -4 → zoneinfo 真实 ET (DST 自适); 极端环境落固定偏移兜底
try:
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
except Exception:
    et = timezone(timedelta(hours=-4))  # 兜底: 无 DST 感知
now_et = datetime.now(et)
today = now_et.strftime('%Y-%m-%d')
weekday = now_et.weekday()

# ── 交易日检查: 非交易日 exit 0 (空输出 = SILENT) ──
if weekday >= 5:
    sys.exit(0)
if os.path.exists(TRADING_DAY_CHECK) and os.path.exists(VENV_PYTHON):
    try:
        r = subprocess.run([VENV_PYTHON, TRADING_DAY_CHECK, '--market', 'US'],
                           capture_output=True, text=True, timeout=10,
                           cwd='/opt/ftseek/FTSeek-US')
        # 2026-09-07 修: 判据按 stdout 非 returncode — trading_day_check.py 契约 exit 恒 0
        if "NON_TRADING_DAY" in (r.stdout or "").upper():
            sys.exit(0)
    except Exception as _e:
        # 守卫自身故障 → fail-open 照报 (不用 returncode 判, 见契约测试)
        print(f"[warn] trading_day_check 异常, 按交易日继续: {_e}", file=sys.stderr)


# ── state: fail-open — 读不出/损坏 ⇒ 空 state (当日首轮语义, 宁重复不吞事件) ──
def _load_state():
    try:
        with open(STATE_PATH, encoding='utf-8') as f:
            s = json.load(f)
        if isinstance(s, dict) and s.get('date') == today:
            return s
    except FileNotFoundError:
        return {}  # 首日无 state 属正常; 显式 return 而非 pass (2026-10-28 清剿)
    except Exception as _e:
        print(f"[warn] state 读取失败 (fail-open 照发): {_e}", file=sys.stderr)
    return {}


def _save_state(s):
    try:
        tmp = STATE_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(s, f, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)  # 原子替换
    except Exception as _e:
        print(f"[warn] state 写入失败 (不影响本次投递): {_e}", file=sys.stderr)


state = _load_state()
is_first_round = not state.get('morning_done')

lines = []
events = []          # 盘中事件行
def add(s):
    lines.append(s)


db = None
try:
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
except Exception as _e:
    events.append(f"⚠️ 主库只读连接失败: {_e}")

# ══════════════════════ 首轮: 交易日计划快照 ══════════════════════
if is_first_round and db is not None:
    add(f"📋 交易日计划 {now_et.strftime('%Y-%m-%d %H:%M %Z')}")
    try:
        # direction='SKIP' 是组合门占位行, 非真实指令 (pipeline.py)
        rows = db.execute(
            "SELECT symbol, direction, confidence, entry_price, stop_loss, take_profit, "
            "position_size, decision_id FROM playbook_instructions "
            "WHERE date = ? AND direction != 'SKIP' ORDER BY direction, confidence DESC, id",
            (today,)).fetchall()
        n_long = sum(1 for r in rows if r['direction'] == 'BUY')
        n_short = sum(1 for r in rows if r['direction'] == 'SELL_SHORT')
        re_n = sum(1 for r in rows if (r['decision_id'] or '').startswith('intraday_reentry'))
        add(f"今日指令 {len(rows)} 条: LONG {n_long} / SHORT {n_short}"
            + (f" (含盘中再入场 {re_n})" if re_n else ""))
        for r in rows:
            tag = ' [再入场]' if (r['decision_id'] or '').startswith('intraday_reentry') else ''
            sz = f"{r['position_size']}股" if r['position_size'] else '?股'
            add(f"· {r['symbol']} {r['direction']} {r['confidence'] or 0}% "
                f"入场≈{r['entry_price'] or '?'} SL={r['stop_loss'] or '?'} TP={r['take_profit'] or '?'} "
                f"{sz}{tag}")
    except Exception as _e:
        add(f"⚠️ 指令查询失败: {_e}")
    try:
        cap = db.execute("SELECT current_equity, drawdown_pct, water_level, is_locked, lock_reason "
                         "FROM capital_state ORDER BY rowid DESC LIMIT 1").fetchone()
        if cap:
            dd = (cap['drawdown_pct'] or 0) * 100
            lock = f" · 🔒锁定({cap['lock_reason']})" if cap['is_locked'] else ""
            add(f"账户 ${cap['current_equity']:,.0f} · 回撤 {dd:+.2f}% · 水位 {cap['water_level'] or '?'}{lock}")
    except Exception as _e:
        add(f"⚠️ 账户查询失败: {_e}")
    try:
        pos = db.execute(
            "SELECT symbol, direction, quantity, entry_price FROM trade_records "
            "WHERE source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') "
            "AND (exit_price IS NULL OR exit_price=0 OR exit_time='' OR exit_time IS NULL)"
        ).fetchall()
        if pos:
            add(f"隔夜持仓 {len(pos)} 只:")
            for p in pos:
                add(f"· {p['symbol']} {p['direction']} {p['quantity']}股 @{p['entry_price']}")
        else:
            add("隔夜持仓: 0")
    except Exception as _e:
        add(f"⚠️ 持仓查询失败: {_e}")
    add("—— 此后仅事件推送 (触发未执行/错误突发/水位变化/退出卡单/服务宕机/新增指令), 无事静默 ——")

# ══════════════════════ 每轮事件扫描 ══════════════════════

# E1. 服务存活 (宕机每轮重复推 — 白名单语义, 不参与去重)
svc_active = True
try:
    r = subprocess.run(['systemctl', 'is-active', 'ftseek-us'],
                       capture_output=True, text=True, timeout=10)
    svc_active = r.stdout.strip() == 'active'
except Exception as _e:
    events.append(f"⚠️ systemctl 探测失败: {_e}")
if not svc_active:
    events.append("🔴 ftseek-us 服务未运行!")

if db is not None:
    # E2. 触发未执行持续超 10min。
    #     ⚠️ 不用 updated_at 判龄 (2026-10-09 实测: chase_cap 每轮重定价会刷新它,
    #     ARM 挂起 2h 而 updated_at 龄恒 ~2min ⇒ 该判据对最典型的挂单永不触发)。
    #     改用 state 记「连续见到 pending 的首次时刻」, 消失(被执行)即清除。
    try:
        rows = db.execute(
            "SELECT symbol, direction, skip_reason, execution_state "
            "FROM playbook_instructions "
            "WHERE date=? AND direction!='SKIP' AND triggered=1 AND executed=0",
            (today,)).fetchall()
        seen_tf = set(state.get('trig_fail_seen', []))
        first_seen = {k: v for k, v in (state.get('trig_pending_first_seen') or {}).items()
                      if isinstance(v, (int, float))}
        now_ts = time.time()
        pending_now = set()
        for r in rows:
            sym = r['symbol']
            pending_now.add(sym)
            if sym not in first_seen:
                first_seen[sym] = now_ts
                continue  # 本轮首次见到, 下轮起算龄
            if (now_ts - first_seen[sym]) < STALE_EXEC_S:
                continue
            reason = (r['skip_reason'] or '').strip() or f"无原因·state={r['execution_state'] or '?'}"
            key = f"{sym}|{reason.split('(')[0]}"
            if key in seen_tf:
                continue
            seen_tf.add(key)
            events.append(f"⛔ {sym} {r['direction']} 触发后持续未执行 "
                          f"{int((now_ts - first_seen[sym])//60)}min: {reason}")
        # 已离开 pending 集 (执行掉/撤掉) 的清除计时
        state['trig_pending_first_seen'] = {k: v for k, v in first_seen.items() if k in pending_now}
        state['trig_fail_seen'] = sorted(seen_tf)
    except Exception as _e:
        events.append(f"⚠️ 触发未执行扫描失败: {_e}")

    # E3. 水位变化 / 锁定
    try:
        cap = db.execute("SELECT water_level, is_locked, lock_reason, drawdown_pct "
                         "FROM capital_state ORDER BY rowid DESC LIMIT 1").fetchone()
        if cap:
            wl = cap['water_level'] or '?'
            prev_wl = state.get('water_level')
            if wl != 'green' and prev_wl != wl:
                events.append(f"🟡 水位变化: {prev_wl or '?'} → {wl} "
                              f"(回撤 {(cap['drawdown_pct'] or 0)*100:+.2f}%)")
            if cap['is_locked'] and not state.get('locked'):
                events.append(f"🔒 账户锁定: {cap['lock_reason'] or '未给原因'}")
            state['water_level'] = wl
            state['locked'] = 1 if cap['is_locked'] else 0
    except Exception as _e:
        events.append(f"⚠️ 水位扫描失败: {_e}")

    # E6. 新增指令 (盘中再入场武装 / UE 晚批; 首轮只播种不报 — 晨间快照已印全表)
    try:
        rows = db.execute(
            "SELECT symbol, direction, confidence, entry_price, stop_loss, take_profit, "
            "position_size, decision_id FROM playbook_instructions "
            "WHERE date=? AND direction!='SKIP'", (today,)).fetchall()
        seen_plan = set(state.get('plan_seen', []))
        if seen_plan:
            for r in rows:
                key = f"{r['symbol']}|{r['decision_id'] or ''}"
                if key in seen_plan:
                    continue
                seen_plan.add(key)
                tag = ' [再入场]' if (r['decision_id'] or '').startswith('intraday_reentry') else ''
                events.append(f"➕ 新增指令: {r['symbol']} {r['direction']} {r['confidence'] or 0}% "
                              f"入场≈{r['entry_price'] or '?'} SL={r['stop_loss'] or '?'} "
                              f"TP={r['take_profit'] or '?'}{tag}")
        else:
            seen_plan = {f"{r['symbol']}|{r['decision_id'] or ''}" for r in rows}
        state['plan_seen'] = sorted(seen_plan)
    except Exception as _e:
        events.append(f"⚠️ 指令增量扫描失败: {_e}")

# E4. 退出卡单 (playbook_exits.db; 账本缺失本身是告警 — 退出链未产生 intent 或旧版在跑)
try:
    if os.path.exists(EXIT_DB_PATH):
        edb = sqlite3.connect(f"file:{EXIT_DB_PATH}?mode=ro", uri=True, timeout=5)
        edb.row_factory = sqlite3.Row
        stuck = edb.execute(
            "SELECT intent_id, symbol, state, last_error, updated_at FROM exit_intents "
            "WHERE state != 'BOOKED'").fetchall()
        edb.close()
        seen_si = set(state.get('stuck_intents_seen', []))
        now_ts = time.time()
        for s in stuck:
            try:
                age = now_ts - float(s['updated_at'])
            except (TypeError, ValueError):
                age = STUCK_INTENT_S + 1  # 不可解析 → 宁可报
            if age < STUCK_INTENT_S:
                continue
            if s['intent_id'] in seen_si:
                continue
            seen_si.add(s['intent_id'])
            err = (s['last_error'] or '')[:60]
            events.append(f"🚨 退出卡单: {s['symbol']} intent={str(s['intent_id'])[:8]} "
                          f"state={s['state']} 已 {int(age//60)}min {err}")
        state['stuck_intents_seen'] = sorted(seen_si)[-200:]  # 只留最近, 防无限膨胀
    elif not state.get('exit_db_warned'):
        state['exit_db_warned'] = 1
        events.append("⚠️ 退出账本 playbook_exits.db 不存在 — 退出链未产生 intent 或旧版在跑")
except Exception as _e:
    events.append(f"⚠️ 退出账本扫描失败: {_e}")

# E5. 日志 ERROR/CRITICAL 增量 (旧版 grep 少 -E 恒 0 的死指标, 改 Python 增量扫)
#     跨日/首轮: 以当前文件尾为基线 (窗外交给 error_alert/late 档, 不回放历史)
try:
    size = os.path.getsize(LOG_PATH)
    offset = int(state.get('err_offset', 0))
    if is_first_round or offset > size:
        offset = size  # 新交易日/日志已轮转 → 重置基线
    n_err = n_crit = 0
    groups = {}
    if size > offset:
        with open(LOG_PATH, 'r', encoding='utf-8', errors='replace') as f:
            f.seek(offset)
            for ln in f:
                if '] CRITICAL:' in ln:
                    n_crit += 1
                elif '] ERROR:' in ln:
                    n_err += 1
                    body = re.sub(r'^\d{4}-\d\d-\d\d [\d:,]+ ', '', ln).strip()
                    key = body[:90]
                    groups[key] = groups.get(key, 0) + 1
    state['err_offset'] = size
    if n_crit > 0:
        events.append(f"🔴 日志 CRITICAL +{n_crit} 条 (30min 窗)")
    if n_err >= ERR_PUSH_MIN:
        top = sorted(groups.items(), key=lambda kv: -kv[1])[:3]
        detail = ' | '.join(f"{k} ×{v}" if v > 1 else k for k, v in top)
        events.append(f"⚠️ 日志 ERROR +{n_err} 条 (30min 窗): {detail[:300]}")
except Exception as _e:
    events.append(f"⚠️ 日志扫描失败: {_e}")

if db is not None:
    db.close()

# ── 输出: 首轮快照 + 本轮事件; 两者皆空 ⇒ 空 stdout = SILENT ──
state['date'] = today
state['morning_done'] = True

if events:
    if lines:
        add("")
    else:
        add(f"⚡ 盘中事件 {now_et.strftime('%H:%M %Z')}")
    lines.extend(events)

if not lines:
    _save_state(state)
    sys.exit(0)  # Empty stdout = SILENT

_save_state(state)
print('\n'.join(lines))
