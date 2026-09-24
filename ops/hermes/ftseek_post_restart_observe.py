#!/usr/bin/env python3
"""重启后观察 (只读, 一次性载体): 抓 FTSeek-US 重启后的运行证据 + 检查 ETH 链首批实跑。

输出 = 紧凑摘要 (no_agent 载体直接投递)。只读: 不写 DB / 不下单 / 不重启 / 不改配置。
"""
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/opt/ftseek/FTSeek-US")
PY = ROOT / "venv/bin/python"
LOG = ROOT / "logs/ftseek_us.log"
lines_out = []

_log = logging.getLogger(__name__)


def sh(cmd, cwd=ROOT, timeout=120, env=None):
    e = dict(os.environ)
    e["PYTHONPATH"] = "."
    if env:
        e.update(env)
    p = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True,
                       timeout=timeout, env=e)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def add(s):
    lines_out.append(s)


cst = time.strftime("%Y-%m-%d %H:%M:%S CST", time.localtime())
add(f"⏱ 观察时刻: {cst}")

# 1) 服务活性
rc, out = sh("systemctl is-active ftseek-us && systemctl show -p MainPID -p ActiveEnterTimestamp --value ftseek-us | tr '\\n' ' '")
add(f"① 服务: {out.strip() or 'unknown'}")

# 2) 日志错误扫描 (全文件: 重启后应有 0 条)
try:
    txt = LOG.read_text(errors="ignore")
    errs = [l for l in txt.splitlines() if re.search(r"ERROR|Traceback|CRITICAL|Exception", l)]
    add(f"② 日志错误行: {len(errs)} 条" + (f" | 首条: {errs[0][:120]}" if errs else ""))
except Exception as e:
    add(f"② 日志读取失败: {type(e).__name__}")

# 3) 开关审计 (SSOT 验收门)
rc, out = sh(f"{PY} scripts/live_switch_audit.py --assert-ready", timeout=180)
blk = re.search(r"BLOCKER[^\d]*(\d+)", out)
add(f"③ 开关审计: exit={rc} BLOCKER={blk.group(1) if blk else '?'}"
    + ("  ✅" if rc == 0 else "  ⚠️ 需看输出"))

def last_json_ish(out: str, n: int = 130) -> str:
    """从(可能是多行 JSON 的)输出里挑出最有信息量的一行。"""
    cand = [l.strip() for l in out.splitlines() if l.strip()]
    for l in reversed(cand):
        if any(k in l for k in ('"ok"', '"skip"', 'skip"', 'ok":', 'error', 'Error', '{"')):
            return l[:n]
    return (cand[-1][:n] if cand else "(空)")


# 4) ETH 链首批实跑 (盘前窗口内应不再返回 outside_trading_window)
res = []
for sess in ("premarket", "afterhours"):
    rc, out = sh(f"timeout 120 {PY} scripts/extended_hours_entry_executor.py --session {sess} --dry 2>&1", timeout=180)
    res.append(f"{sess}: {last_json_ish(out)}")
add("④ ETH entry --dry: " + " ;; ".join(res))

rc, out = sh(f"timeout 120 {PY} scripts/extended_hours_exit_executor.py --symbol US.NVDA --direction LONG "
             f"--qty 1 --price 100 --session premarket --reason watch --dry 2>&1", timeout=180)
add(f"⑤ ETH exit --dry: {last_json_ish(out)}")

# 5) 状态面 (只读 DB)
rc, out = sh("""sqlite3 -separator ' | ' data/ftseek_us.db "
SELECT 'equity='||current_equity, 'cash='||cash, 'water='||water_level FROM capital_state ORDER BY id DESC LIMIT 1;" """)
add(f"⑥ capital: {out.strip()}")
rc, out = sh("""sqlite3 data/ftseek_us.db "SELECT '未平仓='||COUNT(*) FROM open_positions;" """)
add(f"⑦ {out.strip()}")
rc, out = sh("tail -1 data/extended_hours_entry_executions.jsonl 2>/dev/null")
if out.strip():
    try:
        j = json.loads(out.strip())
        add(f"⑧ ETH entry 最近审计行: {j.get('ts','?')} {j.get('symbol','?')} ok={j.get('ok')} skip={j.get('skip','')}")
    except Exception:
        _log.warning("json parse failed", exc_info=True)

# 6) 日志尾部 (看是否真有窗口内活动)
rc, out = sh("tail -3 logs/ftseek_us.log | cut -c1-150")
add("⑨ 日志尾:\n" + out.strip())

print("\n".join(lines_out))
sys.exit(0)