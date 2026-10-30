#!/usr/bin/env python3
"""收盘前核查 RFC-174/176 实弹命中情况 (一次性)。只读日志, 不改任何状态。"""
import re
import subprocess
from pathlib import Path

LOG = Path("/opt/ftseek/FTSeek-US/logs/ftseek_us.log")
RESTART = "2026-10-29 23:16:23"   # RFC-176 部署后的重启时刻


def lines_after(marker):
    out = []
    for line in LOG.open(encoding="utf-8", errors="ignore"):
        m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        if m and line[:19] >= marker:
            out.append(line.rstrip())
    return out


rows = lines_after(RESTART)
print(f"日志窗口: {RESTART} (重启) → 现在; 共 {len(rows)} 行")
print(f"进程状态: {subprocess.run(['systemctl','is-active','ftseek-us'],capture_output=True,text=True).stdout.strip()}")

def show(title, pat, limit=8):
    hit = [r for r in rows if re.search(pat, r)]
    print(f"\n== {title}: {len(hit)} 条")
    for r in hit[-limit:]:
        print("  " + r[:200])

show("强制重连 (RFC-174 后应为 0, 启动竞态除外)", r"触发强制重连")
show("无数据标的冷却命中 (RFC-174 新分支)", r"无历史K线数据")
show("日K配额等待 (RFC-176 埋点)", r"K线配额")
show("K线失败", r"K 线获取失败|日K线获取失败", limit=6)
show("ERROR", r" ERROR ", limit=5)
show("轮次耗时>120s 慢轮", r"轮次耗时 \d{3,}s", limit=5)
show("TwelveData fallback 调用", r"走 TwelveData fallback", limit=6)

rounds = [r for r in rows if "自动执行完成" in r or "轮次耗时" in r]
print(f"\n== 重启后完成轮次: {len([r for r in rounds if '自动执行完成' in r])}")
for r in rounds[-6:]:
    print("  " + r[:160])