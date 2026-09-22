#!/usr/bin/env python3.11
"""复盘覆盖扫描 (`scripts/review_coverage_scan.py`) 的 Hermes no_agent 载体 (2026-09-22, RFC-088 P5 补齐).

为什么建: DEB-20260919 §7 的周环行 (`周六 06:45 … review_coverage_scan.py`) 与 §16 声称的载体
均**未落地** (2026-09-22 核查: crontab/jobs.json 双无, 产物只有 9/19、9/20 两次手工跑)。
本载体的形态选择有据: 该扫描 stdout 是**整块 JSON** (60 producer / 169 push-exit 产物 / 19 死产出,
体量数 KB) — 原样投递 = 每周刷屏, 而"只 >> logs 零读者"又是本项目实测过的僵尸尺子病灶
(见 gate_liveness 审计)。故取**签名变化才出声**的静默契约 (与 `pending_adjudication_carrier.py` 同形):

stdout 契约:
  · rc == 0 且签名未变 → 一字不出 (产物照落 `data/reviews/coverage_<date>.json`)
  · rc == 0 且签名变化 → ≤10 行摘要 (计数 + 新增/消失条目各前 5), 原样投递
  · rc != 0 → 先输出载体级异常行 (含尾部证据), 再投递子进程输出, 同 rc 退出
  · 超时 → 杀子进程 rc 2; 子脚本缺失 rc 3; rc0 但产物未落盘/未刷新 → rc 4
签名 (state `data/reviews/coverage_carrier_state.json`): sha1 over 排序后的
(dead id 集, 未登记/无引用 id 集, producer_count). **只取状态维度** — 产物 mtime 不进签名
(每跑一次 mtime 都变 ⇒ 每周都推, 静默契约形同虚设)。
取证面: 子进程输出 + `[carrier …] rc= dur= out_lines= sig=` 头追加到 `logs/review_coverage_scan.log`。
回滚: 删 Hermes job + 删本文件与线上副本 + 删 state (无 crontab 行依赖, 删除即静默)。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(os.environ.get("FTSEEK_REPO", "/opt/ftseek/FTSeek-US"))
CHILD = REPO / "scripts" / "review_coverage_scan.py"
PYTHON = REPO / "venv" / "bin" / "python"
CHILD_LOG = REPO / "logs" / "review_coverage_scan.log"
STATE = REPO / "data" / "reviews" / "coverage_carrier_state.json"
TIMEOUT = int(os.environ.get("CARRIER_TIMEOUT_S", "300"))


def _append_log(path: Path, text: str) -> None:
    try:
        with path.open("a", encoding="utf-8", errors="replace") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except Exception as e:                                       # noqa: BLE001
        print(f"[review_coverage_carrier] 取证日志写入失败 ({type(e).__name__}: {e})")


def _load_summary(out: str) -> dict:
    """子脚本 stdout 是单行 JSON (summary)。取不到就 fail-loud, 不许当成'无变化'。"""
    for line in reversed((out or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:                                    # noqa: BLE001
                continue
    raise ValueError("子脚本 stdout 未含 JSON summary")


def _signature(summary: dict) -> tuple:
    dead = sorted(str(e.get("artifact") or e.get("id")) for e in (summary.get("dead_producers") or []))
    unreg = sorted(str(e.get("artifact") or e.get("id")) for e in (summary.get("unregistered_shadows") or []))
    # 子脚本当前 schema 只给 dead_producers; 未登记影子面若日后并入 summary 会自动生效。
    counts = (summary.get("producer_count"), summary.get("dead_producer_count"),
              summary.get("push_exit_file_count"))
    raw = json.dumps([dead, unreg, counts], ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:12], dead, unreg, counts


def _render(summary: dict, sig: str, dead: list, unreg: list, prev: dict | None) -> list:
    lines = [f"[复盘覆盖扫描] {datetime.now():%Y-%m-%d} | 生产者 {summary.get('producer_count')} "
             f"| push-exit 产物 {summary.get('push_exit_file_count')} "
             f"| 无代码引用(死产出) {len(dead)}" + (f" | 未登记影子 {len(unreg)}" if unreg else "")]
    if prev:
        old_dead = set(prev.get("dead") or [])
        old_unreg = set(prev.get("unreg") or [])
        new_dead, gone_dead = sorted(set(dead) - old_dead), sorted(old_dead - set(dead))
        new_unreg, gone_unreg = sorted(set(unreg) - old_unreg), sorted(old_unreg - set(unreg))
        for label, new, gone in (("死产出", new_dead, gone_dead), ("未登记影子", new_unreg, gone_unreg)):
            if new:
                lines.append(f"🆕 {label} 新增 {len(new)}: " + ", ".join(p.split('/')[-1] for p in new[:5]))
            if gone:
                lines.append(f"✅ {label} 消失 {len(gone)}: " + ", ".join(p.split('/')[-1] for p in gone[:5]))
        if not (new_dead or gone_dead or new_unreg or gone_unreg):
            lines.append("计数变化但条目集不变 (口径变动?) — 请人工扫一眼产物")
    else:
        lines.append("首跑: 全量基线已建 (此后只在条目集变化时出声)")
    lines.append(f"产物: {summary.get('out') or '?'} | sig={sig}")
    return lines[:10]


def main() -> int:
    if not CHILD.exists():
        print(f"[review_coverage_carrier] 载体级异常: 子脚本不存在 {CHILD} ⇒ 每周自审断更 (不得读成'无死产出')")
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
        _append_log(CHILD_LOG, f"[carrier {datetime.now():%Y-%m-%d %H:%M:%S}] TIMEOUT >{TIMEOUT}s (已杀)\n{out}")
        print(f"[review_coverage_carrier] 载体级异常: 扫描超时 >{TIMEOUT}s (已杀) ⇒ 本周自审未完成")
        print(out[-600:])
        return 2
    dur = time.time() - t0
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if rc != 0:
        _append_log(CHILD_LOG, f"[carrier {datetime.now():%Y-%m-%d %H:%M:%S}] rc={rc} dur={dur:.1f}s "
                               f"out_lines={len(lines)}\n{out}")
        print(f"[review_coverage_carrier] 载体级异常: 子脚本 rc={rc} dur={dur:.1f}s ⇒ 本周自审未完成")
        print(out[-600:])
        return rc
    try:
        summary = _load_summary(out)
        sig, dead, unreg, _ = _signature(summary)
    except Exception as exc:                                     # noqa: BLE001
        _append_log(CHILD_LOG, f"[carrier {datetime.now():%Y-%m-%d %H:%M:%S}] rc={rc} dur={dur:.1f}s "
                               f"out_lines={len(lines)} sig=UNPARSED\n{out}")
        print(f"[review_coverage_carrier] 载体级异常: 摘要不可解析 ({type(exc).__name__}: {exc}) "
              f"⇒ 无变化/有变化不可区分, 按失败处理")
        return 4
    produced = Path(str(summary.get("out") or "")) if summary.get("out") else None
    if produced is None or not produced.exists() or produced.stat().st_mtime < t0 - 5:
        _append_log(CHILD_LOG, f"[carrier {datetime.now():%Y-%m-%d %H:%M:%S}] rc={rc} dur={dur:.1f}s "
                               f"out_lines={len(lines)} sig={sig}\n{out}")
        print(f"[review_coverage_carrier] 载体级异常: rc=0 但产物未落盘/未刷新 {produced} ⇒ 自审断更")
        return 4
    prev = None
    try:
        prev = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:                                            # noqa: BLE001
        prev = None
    _append_log(CHILD_LOG, f"[carrier {datetime.now():%Y-%m-%d %H:%M:%S}] rc={rc} dur={dur:.1f}s "
                           f"out_lines={len(lines)} sig={sig}\n{out}")
    changed = (prev or {}).get("sig") != sig
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"sig": sig, "ts": datetime.now().isoformat(timespec="seconds"),
                                 "dead": dead, "unreg": unreg,
                                 "counts": summary.get("counts") or {}}, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    if not changed:
        return 0                                    # 静默契约: 无变化一字不出
    print("\n".join(_render(summary, sig, dead, unreg, prev)))
    return 0


if __name__ == "__main__":
    sys.exit(main())