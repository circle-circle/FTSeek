#!/usr/bin/env python3
"""alpha-backfill (SPY alpha + 论据级IC) — no_agent cron script (6af0648e3eb3 转 script).

原任务: LLM agent 跑「交易日检查 + alpha_backfill.py --backfill/--claim-ic + 一行摘要」.
确定性执行 → no_agent (输出即推送: 一行摘要; 失败时输出错误).

用法 (cronjob):
  script: alpha_backfill_run.py
  workdir: /root/.hermes/skills/deep-stock-analysis/scripts
"""
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta

REPO = '/opt/ftseek/FTSeek-US'
SKILL_SCRIPTS = '/root/.hermes/skills/deep-stock-analysis/scripts'
VENV = f'{REPO}/venv/bin/python3'
TRADING_DAY_CHECK = f'{REPO}/scripts/trading_day_check.py'

# ── 交易日检查 ──
try:
    r = subprocess.run([VENV, TRADING_DAY_CHECK, '--market', 'US'],
                       capture_output=True, text=True, timeout=10, cwd=REPO)
    if r.returncode != 0:
        sys.exit(0)  # 非交易日: 静默
except Exception:
    pass

def run(cmd, timeout=600):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or '') + (r.stderr or '')
    except subprocess.TimeoutExpired:
        return 124, 'TIMEOUT'
    except Exception as e:
        return 1, str(e)

# ── 回填 (幂等, 失败可重跑) ──
if os.environ.get('ALPHA_BACKFILL_DRY'):
    print("DRY: alpha_backfill --backfill / --claim-ic (跳过执行)")
    sys.exit(0)

rc1, out1 = run([VENV, os.path.join(SKILL_SCRIPTS, 'alpha_backfill.py'), '--backfill'])
rc2, out2 = run([VENV, os.path.join(SKILL_SCRIPTS, 'alpha_backfill.py'), '--claim-ic'])

if rc1 == 0 and rc2 == 0:
    # 一行摘要 (从输出提取 filled 数字)
    import re
    fills = re.findall(r'filled[:\s]*(\d+)', out1 + out2) or re.findall(r'(\d+)\s*(?:rows|symbols)', out1 + out2)
    print(f"alpha-backfill OK: --backfill rc=0, --claim-ic rc=0"
          + (f", filled={fills[:3]}" if fills else ""))
else:
    print(f"alpha-backfill FAIL: backfill rc={rc1} | claim-ic rc={rc2}\n{out1[-500:]}\n{out2[-500:]}")
    sys.exit(1)