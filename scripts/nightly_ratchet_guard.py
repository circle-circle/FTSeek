#!/usr/bin/env python3
"""夜间棘轮守卫 (2026-10-15, 全链路审计 P3: 棘轮双执法面合并).

背景: 棘轮家族分两个互不重叠的执法面 —— naive-now / except-pass / utc4 /
pipeline-size / W456 只在 CI (push 才跑), W2/W5/W6 只在夜间 invariant cron。
任一面停摆都无人知 (审计原话: 「不 push 的工作树漂移不会被任何自动面捕获」)。

本环 = 把 CI 侧的 5 个棘轮测试文件每晚跑一遍 (测试即判据单一真源, 不复制逻辑):
- 全绿 → 一行 INFO 落盘 (静默);
- 任一红 → 走 app/push hub 告警 (fail-visible, 含失败摘要) + exit 1
  (裸 traceback/失败行同被 error_alert 崩溃尾检测覆盖)。

这 5 个文件全是静态代码扫描 (AST/grep), 不碰活体数据 ⇒ 与全量套件并行无假红风险。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RATCHET_FILES = [
    "tests/test_naive_datetime_now_ratchet.py",
    "tests/test_except_pass_ratchet.py",
    "tests/test_hardcoded_utc4_ratchet.py",
    "tests/test_pipeline_size_ratchet.py",
    "tests/test_w456_log_hygiene_ratchet.py",
]


def main() -> int:
    cmd = [str(REPO / "venv/bin/python"), "-m", "pytest", "-q",
           "--tb=short", "-p", "no:cacheprovider", *RATCHET_FILES]
    proc = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True,
                          timeout=600)
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-25:])
    if proc.returncode == 0:
        print(f"[ratchet_guard] 全绿 ({proc.stdout.strip().splitlines()[-1]})")
        return 0

    msg = (f"🚨 夜间棘轮守卫: CI 面棘轮在夜间复跑见红 (rc={proc.returncode})\n"
           f"—— 白昼无 push 时这类漂移原本无人知 (审计 P3)。\n{tail}")
    print(msg, file=sys.stderr)
    try:
        from app.push import get_push_hub
        from app.push.message import Category, Level, PushMessage
        hub = get_push_hub()
        try:
            hub.push(PushMessage(title="夜间棘轮守卫见红", body=msg,
                                 level=Level.HIGH, category=Category.SYSTEM,
                                 dedup_key="nightly_ratchet_guard"),
                     sync=True)
        finally:
            hub.stop()
    except Exception as exc:  # noqa: BLE001 — 推送失败不掩盖测试红 (exit 1 已可见)
        print(f"[ratchet_guard] hub 推送失败 (exit 1 仍报错): {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
