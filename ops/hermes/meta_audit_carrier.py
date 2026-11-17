#!/usr/bin/env python3.11
"""元环 findings 生命周期环 (`scripts/meta_audit_loop.py`) 的 Hermes no_agent 载体
(2026-09-20 用户令「按建议执行」= Loop Engineering 试点第 1 个 loop, RFC-091)。

判据分层: 先运行 `scripts/intraday_health_snapshot.py` 生成结构化 ERROR/CRITICAL 快照,
再运行 `scripts/meta_audit_loop.py` 本体 (只读消费结构化探测器产物、写自己的状态文件)。

stdout 契约 (**静默成功型**, 与门活性探针那种"输出本体即产物"相反):
  · rc == 0 且子进程无输出 → 本载体**也零输出** = 不推送 (状态无变化, 不刷屏)。
  · rc == 0 且有输出       → 仅当内容指纹较上次变化才投递；相同未决清单跨日静默。
  · rc != 0 / 探测器异常   → 不受指纹静默影响，必须投递载体/环级异常。
                             ⇒ 环自身崩溃必须与"零 finding"可区分 (元环规则 5)。
  · 超时 → 杀子进程, rc 2; 子脚本缺失 → rc 3。
环境变量: META_AUDIT_FORCE=1 透传 `--force-print` (人工查验用; cron 不带)。
取证面: 子进程输出**追加**到 `logs/meta_audit.log`, 另加一行 `[carrier …] rc/dur` 头,
        子进程每跑一轮必落盘 ⇒ "无推送"不等于"没跑"(心跳可外部回读)。
回滚: 删 Hermes job + 删本文件与线上副本 (data/loops/meta_audit/ 可一并删)。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

REPO = Path(os.environ.get("FTSEEK_REPO", "/opt/ftseek/FTSeek-US"))
SNAPSHOT = REPO / "scripts" / "intraday_health_snapshot.py"
CHILD = REPO / "scripts" / "meta_audit_loop.py"
PYTHON = REPO / "venv" / "bin" / "python"
CHILD_LOG = REPO / "logs" / "meta_audit.log"
TIMEOUT = int(os.environ.get("CARRIER_TIMEOUT_S", "180"))
PUSH_STATE = REPO / "data" / ".meta_audit_push_fingerprint.json"


def _fingerprint(text: str) -> str:
    """只抹首行运行时刻；finding/级别/owner/due/LOOP_DONE 全部参与去重。"""
    normalized = re.sub(
        r"^\[meta_audit\]\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}",
        "[meta_audit] <RUN_TS>", text.strip(), count=1)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _same_as_last_delivery(text: str) -> bool:
    """状态缺失/损坏 fail-open 不静默；META_AUDIT_FORCE 人工查验绕过。"""
    if os.environ.get("META_AUDIT_FORCE") == "1":
        return False
    try:
        state = json.loads(PUSH_STATE.read_text(encoding="utf-8"))
        return state.get("fingerprint") == _fingerprint(text)
    except (OSError, ValueError, TypeError):
        return False


def _mark_delivered(text: str) -> None:
    try:
        tmp = PUSH_STATE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "fingerprint": _fingerprint(text),
            "pushed_at": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, PUSH_STATE)
    except Exception as exc:
        print(f"[meta_audit_carrier] 推送指纹写入失败（下轮可能重复）: {exc}", file=sys.stderr)


def _append_log(path: Path, text: str) -> None:
    try:
        with path.open("a", encoding="utf-8", errors="replace") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except Exception as e:                                       # noqa: BLE001
        print(f"[meta_audit_carrier] 取证日志写入失败 ({type(e).__name__}: {e})")


def _run(command: list[str], timeout: int):
    t0 = time.time()
    completed = subprocess.run(
        command, cwd=str(REPO), env=dict(os.environ, PYTHONPATH=str(REPO), PYTHONUNBUFFERED="1"),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout,
    )
    return completed.stdout or "", completed.returncode, time.time() - t0


def main() -> int:
    if not CHILD.exists() or not SNAPSHOT.exists():
        missing = CHILD if not CHILD.exists() else SNAPSHOT
        print(f"[meta_audit_carrier] 载体级异常: 子脚本不存在 {missing} ⇒ findings 归宿环失明")
        return 3
    py = str(PYTHON) if PYTHON.exists() else sys.executable
    t0 = time.time()
    snapshot_rc = 0
    snapshot_out = ""
    try:
        snapshot_out, snapshot_rc, snapshot_dur = _run([py, str(SNAPSHOT)], TIMEOUT)
        _append_log(CHILD_LOG, f"[snapshot {datetime.now():%Y-%m-%d %H:%M:%S}] "
                               f"rc={snapshot_rc} dur={snapshot_dur:.1f}s "
                               f"out_lines={len(snapshot_out.splitlines())}\n{snapshot_out}")
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        _append_log(CHILD_LOG, f"[snapshot {datetime.now():%Y-%m-%d %H:%M:%S}] "
                               f"TIMEOUT >{TIMEOUT}s (已杀)\n{out}")
        print(f"[meta_audit_carrier] 载体级异常: intraday_health_snapshot 超时 >{TIMEOUT}s")
        snapshot_rc = 2

    argv = [py, str(CHILD)]
    if os.environ.get("META_AUDIT_FORCE") == "1":
        argv.append("--force-print")
    try:
        out, rc, child_dur = _run(argv, TIMEOUT)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        rc = 2
        child_dur = time.time() - t0
        _append_log(CHILD_LOG, f"[carrier {datetime.now():%Y-%m-%d %H:%M:%S}] "
                               f"TIMEOUT >{TIMEOUT}s (已杀)\n{out}")
        print(f"[meta_audit_carrier] 载体级异常: 环超时 >{TIMEOUT}s (已杀) ⇒ 本轮未完成 "
              f"(不得读成'零 finding')")
        return rc if rc >= snapshot_rc else snapshot_rc

    _append_log(CHILD_LOG, f"[carrier {datetime.now():%Y-%m-%d %H:%M:%S}] "
                           f"rc={rc} dur={child_dur:.1f}s out_lines={len(out.splitlines())}\n{out}")
    if rc != 0:
        tail = " ¶ ".join(ln.strip() for ln in out.splitlines() if ln.strip())[-400:]
        print(f"[meta_audit_carrier] 载体级异常: 环 rc={rc} ⇒ findings 归宿环自身异常 | 尾部: {tail}")
    elif snapshot_rc != 0:
        print(f"[meta_audit_carrier] 载体级异常: intraday_health_snapshot rc={snapshot_rc} "
              f"⇒ 盘中异常源不可用")
    if out.strip():
        healthy = rc == 0 and snapshot_rc == 0
        if not healthy or not _same_as_last_delivery(out):
            print(out.rstrip())
            if healthy:
                _mark_delivered(out)
    return rc if rc >= snapshot_rc else snapshot_rc


if __name__ == "__main__":
    sys.exit(main())
