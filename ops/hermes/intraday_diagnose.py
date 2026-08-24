#!/usr/bin/env python3
"""FTSeek-US 盘中诊断 (no_agent cron script) — 36cc015242ee 的 script 化版本.

原任务: LLM agent 每 30 分钟跑一个"交易日检查 + 全量诊断输出"的会话 (8+ 次/晚,
每次完整 agent 环 = 高 token 成本). 全部逻辑为确定性 DB 查询 + 格式化, 零推理 →
按 2026-07-28 纪律 (纯确定性任务必须 no_agent) 转 script.

输出语义 (no_agent):
  - 非交易日: 空输出 = 静默 (不推送)
  - 交易日:  输出完整诊断报告 (与原 agent 版推送内容等价)

来源: /root/.hermes/skills/devops/ftseek-intraday-cron-diagnosis/scripts/intraday_diagnose.py
  上游编辑请改 skill 副本, 本文件为 cron 直接调用的稳定快照.
"""
import sqlite3
import os
import sys
import subprocess
from datetime import datetime, timezone, timedelta

DB_PATH = '/opt/ftseek/FTSeek-US/data/ftseek_us.db'
SIGNAL_DB_PATH = '/opt/ftseek/FTSeek-US/data/signal_snapshots.db'
LOG_PATH = '/opt/ftseek/FTSeek-US/logs/ftseek_us.log'
VENV_PYTHON = '/opt/ftseek/FTSeek-US/venv/bin/python3'
TRADING_DAY_CHECK = '/opt/ftseek/FTSeek-US/scripts/trading_day_check.py'

et = timezone(timedelta(hours=-4))
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
        if r.returncode != 0:
            sys.exit(0)
    except Exception:
        pass

lines = []
def add(s):
    lines.append(s)

add(f"=== 盘中诊断 {now_et.strftime('%Y-%m-%d %H:%M:%S %Z')} ===")

# 1. 今日指令
try:
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    # direction='SKIP' 是投票失败的占位行 (pipeline.py 1128), 非真实指令, 不计入"今日指令"
    rows = db.execute("SELECT * FROM playbook_instructions WHERE date = ? AND direction != 'SKIP'", (today,)).fetchall()
    add(f"今日指令: {len(rows)}条")
    trig = exec_ = 0
    for r in rows:
        if r['triggered']:
            trig += 1
        if r['executed']:
            exec_ += 1
        add(f"  {r['symbol']:8s} {r['direction']:10s} conf={r['confidence'] or 0}% "
            f"trig={'Y' if r['triggered'] else 'N'} exec={'Y' if r['executed'] else 'N'} "
            f"outcome={r['outcome'] or '-'} pnl={r['pnl'] or 0}")
    add(f"触发={trig} 执行={exec_}")
except Exception as e:
    add(f"指令查询失败: {e}")

# 2. 当前持仓 (trade_records, 不调 Futu API — cron 子进程 30s 超时坑)
try:
    rows = db.execute("""
        SELECT symbol, direction, quantity, entry_price, entry_time FROM trade_records
        WHERE source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')
          AND (exit_price IS NULL OR exit_price=0 OR exit_time='' OR exit_time IS NULL)
    """).fetchall()
    add(f"当前持仓: {len(rows)}只")
    for r in rows:
        add(f"  {r['symbol']:8s} dir={r['direction']} qty={r['quantity']} entry={r['entry_price']} t={r['entry_time']}")
    db.close()
except Exception as e:
    add(f"持仓查询失败: {e}")

# 3. 日志异常 (近 1 小时)
try:
    r = subprocess.run(['grep', '-c', f'ERROR|Traceback', LOG_PATH],
                       capture_output=True, text=True, timeout=10)
    err = r.stdout.strip()
    add(f"日志 ERROR/Traceback: {err}")
except Exception:
    add("日志异常计数: N/A")

# 4. 服务状态
try:
    r = subprocess.run(['systemctl', 'is-active', 'ftseek-us'], capture_output=True, text=True, timeout=10)
    add(f"服务: {r.stdout.strip()}")
except Exception:
    add("服务: N/A")

print('\n'.join(lines))