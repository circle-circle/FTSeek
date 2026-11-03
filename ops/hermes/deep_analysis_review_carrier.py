#!/usr/bin/env python3.11
"""deep-analysis 盘后复盘 (`feedback_loop.py` 等 4 步) 的 Hermes no_agent 载体 (2026-11-03).

为什么建: 原 job `3a6c83bbd84c` 是 **agent 模式 + qwen3.8-max pin**, 但实测 2026-10-27~11-03
每轮 6-10 次 API 调用里只有 4 次脚本执行 + 一句 `[SILENT]` —— 零推理, 属于
`scheduled-chain-carrier-migration` §"agent 模式 job 换成 no_agent 载体" 的标准形态:
agent 层不产生判断, 只产生账单 (qwen3.8-max ¥0.3-0.4/次 × 5/周 ≈ ¥9/月) 与不确定延迟。

**只换载体, 不换判据**: 子脚本、参数、顺序、timeout 秒数、静默规则逐字照抄原 prompt:
  Step 0   trading_day_check.py --market US      (timeout -k 30 120)  rc=1 非交易日 ⇒ 静默停止
  Step 1   feedback_loop.py                      (timeout -k 30 900)
  Step 1.5 deadfetus_collector.py                (timeout -k 30 300)
  Step 1.6 dc_calibration_collector.py           (timeout -k 30 600)
  Step 1.7 deadfetus_adjudicator.py              (timeout -k 30 300)
解释器与 cwd 同原 prompt: /opt/ftseek/FTSeek-US/venv/bin/python3.11, cwd=skill scripts 目录。

stdout 契约 (静默类, 与告警类载体相反):
  · rc == 0 且 stdout 为空 → 静默 (成功 = 不推送, 记忆/样本已落盘)
  · Step 1.6 出现「达候选门槛」→ 输出该步完整报告 (原 prompt: 该标注出现当次必须输出)
  · Step 1.7 verdict = GATE_CANDIDATES_FOUND → 输出该步完整报告 (同上)
  · 任一步 rc ∉ {0,124} → 输出载体级异常行 + 该步输出尾部, 并以该 rc 退出
      ⇒ Hermes 记 failed, 失败与「无交易=静默」可区分
  · rc == 124 (timeout -k 已杀) → 只标注该步超时并继续下一步, **不重跑**
      (原 prompt 2026-09-16 超时纪律: 重跑会双烧预算并覆盖已落盘产物)
取证面: 全部子进程输出追加到 logs/deep_analysis_review.log, 每轮一行
`[carrier …] rc= dur= out_lines=` 头 (多载体对账用)。
回滚: Hermes job 恢复 agent 模式 (model pin 回 qwen3.8-max + 原 prompt), 本文件与线上副本可保留不用。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(os.environ.get("FTSEEK_REPO", "/opt/ftseek/FTSeek-US"))
SKILL_SCRIPTS = Path("/root/.hermes/skills/deep-stock-analysis/scripts")
PYTHON = REPO / "venv" / "bin" / "python3.11"
FALLBACK_PYTHON = REPO / "venv" / "bin" / "python"
CARRIER_LOG = REPO / "logs" / "deep_analysis_review.log"

# 触发「必须输出完整报告」的判据字面量 (来自子脚本自身的打印, 本载体只读不改判)
TRIGGER_MARKERS = {
    "dc_calibration_collector.py": "达候选门槛",
    "deadfetus_adjudicator.py": "GATE_CANDIDATES_FOUND",
}

# (标签, 脚本, timeout 秒) — 顺序/秒数与原 cron prompt 逐字一致
STEPS = (
    ("Step 1 盘后复盘", "feedback_loop.py", 900),
    ("Step 1.5 死胎攒样", "deadfetus_collector.py", 300),
    ("Step 1.6 dc校准", "dc_calibration_collector.py", 600),
    ("Step 1.7 死胎裁决", "deadfetus_adjudicator.py", 300),
)
TRADING_DAY_TIMEOUT = 120
TAIL_LINES = 40


def _python() -> str:
    if PYTHON.exists():
        return str(PYTHON)
    return str(FALLBACK_PYTHON) if FALLBACK_PYTHON.exists() else sys.executable


def _append_log(text: str) -> None:
    try:
        CARRIER_LOG.parent.mkdir(parents=True, exist_ok=True)
        with CARRIER_LOG.open("a", encoding="utf-8", errors="replace") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except Exception as e:                                       # noqa: BLE001
        print(f"[deep_analysis_review_carrier] 取证日志写入失败 ({type(e).__name__}: {e})")


def _tail(text: str, n: int = TAIL_LINES) -> str:
    lines = (text or "").rstrip().splitlines()
    return "\n".join(lines[-n:])


def _run(argv: list[str], timeout_s: int) -> tuple[int, str]:
    """timeout -k 30 N 包裹 (SIGTERM 后 30s SIGKILL), rc=124 = 超时。"""
    cmd = ["timeout", "-k", "30", str(timeout_s), *argv]
    env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONUNBUFFERED="1")
    try:
        p = subprocess.run(cmd, cwd=str(SKILL_SCRIPTS), env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=timeout_s + 60)
        return p.returncode, p.stdout or ""
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return 124, out
    except OSError as e:
        return 127, f"[carrier] 无法启动子进程: {type(e).__name__}: {e}"


def main() -> int:
    t0 = time.time()
    missing = [s for _, s, _ in STEPS if not (SKILL_SCRIPTS / s).exists()]
    if missing:
        print(f"[deep_analysis_review_carrier] 载体级异常: 子脚本不存在 {missing} "
              f"⇒ 记忆沉淀断更 (不得读成'无交易')")
        return 3

    py = _python()

    # Step 0: 交易日守卫 — rc=1 (非交易日) ⇒ 静默停止; 其余 rc 按原 prompt 继续
    check_argv = [py, str(REPO / "scripts" / "trading_day_check.py"), "--market", "US"]
    rc0, out0 = _run(check_argv, TRADING_DAY_TIMEOUT)
    _append_log(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Step0 trading_day_check rc={rc0}\n{out0}")
    if rc0 == 1:
        return 0

    failures: list[str] = []
    triggers: list[str] = []
    notes: list[str] = []

    for label, script, timeout_s in STEPS:
        rc, out = _run([py, script], timeout_s)
        _append_log(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {label} ({script}) rc={rc}\n{out}")
        if rc == 124:
            notes.append(f"{label} 超时(>{timeout_s}s, 已 timeout -k 杀死, 不重跑)")
            continue
        if rc != 0:
            failures.append(f"{label} rc={rc}\n{_tail(out)}")
            continue
        marker = TRIGGER_MARKERS.get(script)
        if marker and marker in out:
            triggers.append(f"【{label} 触发裁决门: {marker}】\n{out.rstrip()}")

    blocks = triggers + ([("载体级异常 (子步骤失败):\n" + "\n\n".join(failures))] if failures else [])
    if notes:
        _append_log("[carrier notes] " + "; ".join(notes))
    if failures and notes:
        blocks.append("附注: " + "; ".join(notes))

    dur = time.time() - t0
    rendered = "\n\n".join(blocks)
    out_lines = len(rendered.splitlines())
    _append_log(f"[carrier deep_analysis_review] rc={'1' if failures else '0'} dur={dur:.1f}s "
                f"out_lines={out_lines}")

    if blocks:
        print("\n\n".join(blocks))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())