#!/usr/bin/env python3.11
"""周六记分牌合集载体: `exit_scorecard.py` + `direction_hit_scorecard.py` → Hermes no_agent
(2026-09-19 用户裁「同意」, 见 docs/CONTROL-LOOP-LEDGER.md 对应行).

为什么: 两个记分牌都是 `crontab … >> logs/*_cron.log 2>&1` 形态 —— 台账自己记过这类
"一律只 >> logs 零读者 → 判据在文件里静默成熟 (僵尸尺子)"。这里把两张表**送到眼前**。

判据零变更: 两个子脚本逐字未改 (口径/切片/头条制服全部原样), 只换载体。
**唯一新增行为 = 投递形态** (预先声明, 便于事后审计):
  · 每个脚本一块; 单块 ≤ MAX_LINES 行时原样输出; 超长则保留 头 HEAD_LINES + 尾 TAIL_LINES
    行并在中间插一行 `… [截断 N 行, 全文见 logs/<name>] …` —— 防 10KB 级报告刷屏,
    同时不留任何"看不见的口径" (截断点显式标出, 全文仍在原日志)。
  · 任一脚本 rc≠0 → 该块首行插载体级异常行, 整轮以最大 rc 退出 ⇒ Hermes 记 failed。
取证面: 各子脚本输出仍**追加**到它原本的 `logs/<name>_cron.log` (与 crontab 载体同文件),
另加 `[carrier …]` 头行供两载体对账。
并行期: crontab 原两行 (06:20 / 06:22) 保留不动 ⇒ 任一侧静默都有另一侧兜底。
回滚: 删 Hermes job + 删本文件与线上副本 (crontab 两行未动)。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(os.environ.get("FTSEEK_REPO", "/opt/ftseek/FTSeek-US"))
PYTHON = REPO / "venv" / "bin" / "python"
TIMEOUT = int(os.environ.get("CARRIER_TIMEOUT_S", "300"))
MAX_LINES = 60
HEAD_LINES = 42
TAIL_LINES = 10

# (脚本, 取证日志) —— 固定清单, 与 crontab 行的日志逐字同源
JOBS = (
    ("exit_scorecard.py", REPO / "logs" / "exit_scorecard_cron.log"),
    ("direction_hit_scorecard.py", REPO / "logs" / "direction_scorecard_cron.log"),
)


def _clip(text: str) -> str:
    lines = text.rstrip().splitlines()
    if len(lines) <= MAX_LINES:
        return "\n".join(lines)
    dropped = len(lines) - HEAD_LINES - TAIL_LINES
    return "\n".join(lines[:HEAD_LINES] + [f"… [截断 {dropped} 行, 全文见原日志] …"] + lines[-TAIL_LINES:])


def main() -> int:
    py = str(PYTHON) if PYTHON.exists() else sys.executable
    env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONUNBUFFERED="1")
    worst, blocks = 0, []
    for name, log in JOBS:
        child = REPO / "scripts" / name
        stamp = f"[carrier {datetime.now():%Y-%m-%d %H:%M:%S}] {name}"
        if not child.exists():
            blocks.append(f"── {name} ──\n[载体级异常] 子脚本不存在 {child} ⇒ 本表本轮缺失")
            worst = max(worst, 3)
            continue
        t0 = time.time()
        try:
            p = subprocess.run([py, str(child)], cwd=str(REPO), env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, timeout=TIMEOUT)
            out, rc = p.stdout or "", p.returncode
        except subprocess.TimeoutExpired as e:
            out, rc = (e.stdout or "") if isinstance(e.stdout, str) else "", 2
        try:
            with log.open("a", encoding="utf-8", errors="replace") as f:
                f.write(f"{stamp} rc={rc} dur={time.time() - t0:.1f}s\n{out}\n")
        except Exception as e:                                # noqa: BLE001
            blocks.append(f"[载体级异常] {name} 取证日志写入失败 ({type(e).__name__})")
        worst = max(worst, rc)
        body = _clip(out) if out.strip() else "(空输出)"
        blocks.append(f"── {name} (rc={rc}) ──\n{body}")
    print("\n\n".join(blocks))
    if worst:
        print(f"\n[载体级异常] 本轮最大 rc={worst} ⇒ 至少一张记分牌未正常产出, 数字不可直接引用")
    return worst


if __name__ == "__main__":
    sys.exit(main())