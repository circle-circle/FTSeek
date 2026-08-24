#!/usr/bin/env python3
"""观察信号四关门复跑触发 (B2, 2026-08-22). no_agent wrapper."""
import subprocess
import sys

REPO = "/opt/ftseek/FTSeek-US"
r = subprocess.run([f"{REPO}/venv/bin/python3", f"{REPO}/scripts/watch_signal_gate_eval.py"],
                   capture_output=True, text=True, timeout=300, cwd=REPO)
print(r.stdout[-2000:] if r.stdout else "")
if r.returncode != 0:
    print(f"❌ rc={r.returncode}\n{r.stderr[-600:]}")
    sys.exit(1)
