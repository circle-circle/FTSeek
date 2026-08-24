#!/usr/bin/env python3
"""FTSeek Insider Signal Monitor — no_agent cron script (13705b524e2a 转 script).

原任务: LLM agent 检查内部人集群买入信号链路 (服务状态/日志/sentinel/交易数),
"扫描执行但无信号" = 正常不报 (--QUIET--), 真异常才报.

判定口径 (与 agent 版一致):
  - 链路正常 + 无信号 → 空输出 (静默)
  - 服务异常 / 扫描未执行 / sentinel 失效 / 数据源失败 → 输出告警
"""
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta

REPO = '/opt/ftseek/FTSeek-US'
LOG = f'{REPO}/logs/ftseek_us.log'
SENTINEL = f'{REPO}/data/.insider_scanned_today'

# ET 周末守卫: 周六/周日直接静默 (schedule 2-6 CST 覆盖交易周, 防御性)
et_weekday = datetime.now(timezone(timedelta(hours=-4))).weekday()
if et_weekday >= 5:
    sys.exit(0)

def sh(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=True)
        return r.returncode, r.stdout.strip()
    except Exception as e:
        return 1, str(e)

lines = []
def warn(s):
    lines.append(s)

# 1. 服务状态
rc, out = sh('systemctl is-active ftseek-us')
if rc != 0 or 'active' not in out:
    warn(f"🔴 ftseek-us 服务异常: {out or rc}")
else:
    pass  # 正常

# 2. 今日扫描日志 (InsiderScan 出现即链路通)
today = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d')  # CST 日期
rc, out = sh(f"grep -E 'InsiderScan.*(开始扫描|扫描完成|集群买入)' {LOG} {LOG}-{today} 2>/dev/null | tail -5")
scanned = bool(out)
if not scanned:
    warn("🔴 今日未见 InsiderScan 日志 (扫描未执行或日志缺失)")

# 3. sentinel 文件新鲜度
if os.path.exists(SENTINEL):
    import time
    age_min = (time.time() - os.path.getmtime(SENTINEL)) / 60
    if age_min > 60 * 26:  # 超过 26h = 昨日哨兵未更新
        warn(f"⚠️ insider sentinel 过期 ({age_min:.0f}min 前)")
else:
    warn("🔴 insider sentinel 文件不存在")

# 4. 交易数 (40d 窗口)
rc, out = sh(f"grep 'insider transactions in 40d' {LOG}-{today} 2>/dev/null | tail -3")
if not out:
    pass  # 无输出正常 (低频信号)

if lines:
    print("FTSeek Insider Monitor 告警:\n" + "\n".join(lines))
    sys.exit(0)
# 正常 → 空输出 = 静默