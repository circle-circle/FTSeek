#!/usr/bin/env python3
"""FTSeek-US 重启 (RFC-066审计续: shadow_tracker 触发归因修复生效).

时机: CST 04:05 = ET 16:05 (收盘后), 赶在 8/25 EOD run_playbook_track 之前.
输出: 重启结果摘要 (stdout 即推送内容).
"""
import subprocess
import time

def run(cmd, timeout=60):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip()

lines = []

# 1. 重启
rc, out = run(['systemctl', 'restart', 'ftseek-us'])
if rc != 0:
    print(f"❌ ftseek-us 重启失败 (rc={rc}): {out}")
    raise SystemExit(1)
time.sleep(10)

# 2. 状态
rc, out = run(['systemctl', 'is-active', 'ftseek-us'])
if out != 'active':
    print(f"❌ ftseek-us 重启后未 active: {out}")
    raise SystemExit(1)

rc, since = run(['systemctl', 'show', 'ftseek-us', '-p', 'ActiveEnterTimestamp', '--value'])
lines.append(f"🔄 ftseek-us 已重启生效 (RFC-066审计续: shadow_tracker 触发归因修复)")
lines.append(f"• 服务: active, 启动于 {since[:25]}")

# 3. 启动日志健康检查
try:
    r = subprocess.run(['tail', '-60', '/opt/ftseek/FTSeek-US/logs/ftseek_us.log'],
                       capture_output=True, text=True, timeout=10)
    errs = [l for l in r.stdout.split('\n') if 'ERROR' in l or 'Traceback' in l]
    lines.append(f"• 启动日志: {'无ERROR' if not errs else f'{len(errs)}个ERROR, 首条: {errs[0][:100]}'}")
except Exception:
    lines.append("• 启动日志: 检查失败 (不影响重启本身)")

lines.append("• 生效范围: 今晚 8/25 EOD 追踪起, '盘中已触发未成交'不再误标 no_trigger")
print('\n'.join(lines))
