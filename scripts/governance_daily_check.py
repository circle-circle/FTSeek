#!/usr/bin/env python3
"""
FTSeek-US 治理巡检报告 — 每日 9:00 AM 生成
由 cron job 调用，输出报告内容
"""
import sys
import os
import json
import subprocess
from datetime import datetime, timedelta

sys.path.insert(0, '/opt/ftseek/FTSeek-US')

def run_cmd(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd='/opt/ftseek/FTSeek-US')
    return result.stdout.strip(), result.returncode

def main():
    today = datetime.now().strftime('%Y-%m-%d')
    report_lines = [
        f"# FTSeek-US 治理巡检报告 — {today}",
        "",
        "---",
        "",
    ]

    # 1. 治理检查
    out, _ = run_cmd("python3 scripts/governance_check.py")
    report_lines.append("## 治理检查")
    report_lines.append("")
    report_lines.append(out)
    report_lines.append("")

    # 2. 最近提交
    out, _ = run_cmd("git log --oneline -5")
    report_lines.append("## 最近提交")
    report_lines.append("")
    report_lines.append(out)
    report_lines.append("")

    # 3. 未跟踪文件（可能是 Agent 遗留）
    out, _ = run_cmd("git status --porcelain | head -10")
    if out:
        report_lines.append("## 未跟踪文件")
        report_lines.append("")
        report_lines.append(out)
        report_lines.append("")

    # 4. 服务状态
    out, rc = run_cmd("systemctl is-active ftseek-us")
    status = "✅ 运行中" if rc == 0 else "❌ 未运行"
    report_lines.append("## 服务状态")
    report_lines.append("")
    report_lines.append(f"ftseek-us: {status}")
    report_lines.append("")

    # 5. 数据库大小
    out, _ = run_cmd("du -sh data/*.db 2>/dev/null")
    report_lines.append("## 数据库大小")
    report_lines.append("")
    report_lines.append(out if out else "无数据")
    report_lines.append("")

    # 6. 日志大小
    out, _ = run_cmd("du -sh logs/*.log 2>/dev/null | sort -hr | head -5")
    report_lines.append("## 日志大小 (Top 5)")
    report_lines.append("")
    report_lines.append(out if out else "无日志")
    report_lines.append("")

    print("\n".join(report_lines))

if __name__ == "__main__":
    main()
