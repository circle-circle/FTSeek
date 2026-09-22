#!/usr/bin/env python3
"""壳体 (shim): 用 FTSeek-US venv 跑仓库内的 W7 抓包探针 — 线上 /root/.hermes/scripts/ 由 cron 调度.

真身在 `scripts/w7_denominator_probe.py` (需 futu SDK ⇒ 必须 venv 解释器);
本文件与线上副本 sha256 必须一致 (tests/test_ops_scripts_sync.py 双向对账).
回滚: 删 cron job + 删两处本文件 (真身与诊断档 data/audit/w7_denominator_probe.jsonl 可留作证据).
"""
import os
import subprocess
import sys

REPO = os.environ.get("FTSEEK_REPO", "/opt/ftseek/FTSeek-US")
PY = os.path.join(REPO, "venv", "bin", "python")
SCRIPT = os.path.join(REPO, "scripts", "w7_denominator_probe.py")

if not os.path.exists(PY) or not os.path.exists(SCRIPT):
    print(f"[W7 抓包] 环境缺失: PY={PY} SCRIPT={SCRIPT}")
    sys.exit(3)
# futu SDK 会把连接日志打到 stdout/logger ⇒ 只回吐本探针自己的行, 免得 cron 推送里混进 SDK 噪声
proc = subprocess.run([PY, SCRIPT], cwd=REPO, capture_output=True, text=True)
for line in (proc.stdout or "").splitlines():
    if line.startswith("[W7 抓包]") or line.startswith("  "):  # 探针结论行 + 明细行
        print(line)
for line in (proc.stderr or "").splitlines():
    if line.startswith("[W7 抓包]") or line.startswith("  "):  # 探针结论行 + 明细行
        print(line)
if proc.returncode != 0 and "[W7 抓包]" not in (proc.stdout or ""):
    print(f"[W7 抓包] 子进程 rc={proc.returncode}: {(proc.stderr or '')[-200:]}")
sys.exit(proc.returncode)
