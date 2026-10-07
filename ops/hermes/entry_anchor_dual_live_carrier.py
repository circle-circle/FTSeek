#!/usr/bin/env python3.11
"""[RFC-123 阶段0 / DEB-20261007 F1] 双锚影子接线活体核验 · 一次性 Hermes no_agent 载体.

为什么设 (2026-10-07):
  F1 埋点改的是 `app/playbook/*` ⇒ HotReloader 穿不透, 已 `systemctl restart ftseek-us`
  (16:26:16 CST, 晚于提交 16:24:16)。但**重启 + 测试全绿 ≠ 生产真在落行**:
  `_check_intraday_trigger` 只在盘中 auto_execute 循环被调用 (pipeline:4335), 重启时刻
  是 ET 04:26 盘前 ⇒ 生产 ETS 的 `anchor_recorded` 行数当时 = 0, **接线状态不可判**。
  同型事故实锤: 2026-09-01 archetype 影子首日全灭 (`json.loads(dict)` TypeError 被静默吞,
  攒样实际从未开始而无人察觉) ⇒ 必须开盘后读真值验一次, 不得以「fixture 全绿」声称生效。

调度: 一次性 (repeat=1), 本文件所在 job 跑完自动 disabled; 台账 §2.4 已预登记。
  时刻 = CST 22:10 ≡ **ET 10:10** (夏令时; ⚠️ 11 月冬令时后 CST↔ET 右移 1h, 若届时重用
  须重算 — 同 crontab 既有纪律)。选 ET 10:10 而非 09:40: 开盘禁触窗 09:35 起, 留 35min
  让 auto_execute 至少跑若干轮, 避免「太早零行」被误读成接线死。

stdout 契约 (静默类): rc=0 且 stdout 空 → 不推送; 有 stdout → 原样投递。
本载体总是输出核验读数 (一次性核验必须留痕, 不适用「无变化即静默」)。
取证面: 输出追加到 `logs/entry_anchor_dual_live.log`。
回滚: 删 Hermes job + 删本文件与线上副本 (无 crontab 行依赖, 删除即静默)。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(os.environ.get("FTSEEK_REPO", "/opt/ftseek/FTSeek-US"))
CHILD = REPO / "scripts" / "verify_entry_anchor_dual_live.py"
PYTHON = REPO / "venv" / "bin" / "python"
CHILD_LOG = REPO / "logs" / "entry_anchor_dual_live.log"
TIMEOUT = int(os.environ.get("CARRIER_TIMEOUT_S", "300"))


def _append_log(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", errors="replace") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except Exception as e:                                          # noqa: BLE001
        print(f"[anchor_dual_carrier] 取证日志写入失败 ({type(e).__name__}: {e})")


def main() -> int:
    if not CHILD.exists():
        print(f"[anchor_dual_carrier] 载体级异常: 核验脚本不存在 {CHILD} "
              f"⇒ 双锚影子接线状态**未知** (不得读成「没问题」)")
        return 3
    py = str(PYTHON) if PYTHON.exists() else sys.executable
    env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONUNBUFFERED="1")
    t0 = time.time()
    try:
        p = subprocess.run([py, str(CHILD)], cwd=str(REPO), env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=TIMEOUT)
        out, rc = p.stdout or "", p.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        _append_log(CHILD_LOG, f"[carrier {datetime.now():%Y-%m-%d %H:%M:%S}] "
                               f"TIMEOUT >{TIMEOUT}s (已杀)\n{out}")
        print(f"[anchor_dual_carrier] 载体级异常: 核验超时 >{TIMEOUT}s (已杀) ⇒ 接线状态未知")
        return 2

    _append_log(CHILD_LOG, f"[carrier {datetime.now():%Y-%m-%d %H:%M:%S}] "
                           f"rc={rc} dur={time.time() - t0:.1f}s "
                           f"lines={len(out.splitlines())}\n{out}")
    hdr = ("🔬 **双锚影子接线核验**（RFC-123 阶段0 / DEB-20261007 F1，一次性）\n"
           "_为什么跑这条：埋点改 `app/playbook/*`，热重载穿不透须重启；重启在盘前 ⇒ "
           "当时生产零行、接线不可判。本条读**生产 jsonl 真值**定三态 "
           "（LIVE / DEAD / TOO_EARLY），不以「测试全绿」代替活体验证。_\n")
    if rc != 0:
        tail = " ¶ ".join(ln.strip() for ln in out.splitlines() if ln.strip())[-400:]
        print(hdr + f"🔴 核验判 **DEAD/异常** (rc={rc}) ⇒ 埋点未在生产落行，须按脚本内排查顺序处理\n"
                    f"```\n{out.rstrip()}\n```\n尾部: {tail}")
    elif out.strip():
        print(hdr + f"```\n{out.rstrip()}\n```")
    return rc


if __name__ == "__main__":
    sys.exit(main())
