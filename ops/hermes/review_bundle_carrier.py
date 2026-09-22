#!/usr/bin/env python3.11
"""复盘判决书 (`scripts/review_bundle.py`) 的 Hermes no_agent 载体 (2026-09-22, RFC-088 P5 补齐).

为什么建: DEB-20260919 §16 已**白纸黑字**把载体定为 "Hermes cron no_agent
(`ops/hermes/review_bundle_carrier.py` + 线上副本), 日环 06:35 CST"; 但 2026-09-22 核查实测:
本文件、线上副本、Hermes job **三者都不存在** ⇒ 台账声称的载体是纸面的, 产物停在
`bundle_2026-09-18` (9/19 21:27 那次手工跑)。判决书是"待裁决清单"的证据面, 断头 =
通知里点名的判据没有可读证据。本次按声称形态补齐 (不新设语义)。

日环时刻: 06:35 CST (06:30 与 alpha-backfill 同分钟, §16.3 实测) ⇒ ET 18:35 同日,
故 `--date` = **ET 当日** (刚刚收盘的那天), 不是 "ET 昨日" (§7 的 `date -d yesterday`
在 06:35 这个时刻会算到周末, 实测 2026-09-22: ET 9/21 18:35 → yesterday=9/20 非交易日)。

**与 §7 的一处显式偏离 (2026-09-22 用户裁定, 已闭合)**: 本载体默认 **不推送** (`--push` 关), 只产 bundle + vault 页。
理由: bundle 的 headline 与 04:07 盘后综合复盘的头条同源, 日推一条属重复刷屏 (用户注意力面);
待裁决提醒已由 `pending_adjudication_carrier.py` (周六 09:15) 承担。
**2026-09-22 用户令「如果有推送的机制就不重复推了」** ⇒ 当日一度开启的日推已撤, 回到本默认;
判决书的**白话注释**改由既有 04:07 盘后综合复盘的 `Step 12` 原样转贴 (`scripts/review_bundle.py --print-push`),
本载体继续只做「证据面」, 不抢推送面。
开关: 环境变量 `REVIEW_BUNDLE_PUSH=1` 即恢复 §7 的 `--push` 行为, 无需改码。

stdout 契约 (静默类, 与告警类载体相反):
  · rc == 0 且 stdout 为空 → 静默 (产物已落盘: bundle_<date>.json / REVIEW_<date>.md / vault 页)
  · rc == 0 且有 stdout → 原样投递 (子脚本自己要说的话)
  · rc != 0  → 先输出一行载体级异常 (含尾部证据), 再投递子进程输出, 并以同 rc 退出
               ⇒ Hermes 另记 failed (判决书断更必须与"没有待裁项"可区分)
  · 超时 → 杀子进程, rc 2; 子脚本缺失 → rc 3; rc=0 但产物未落盘/未刷新 → rc 4
    (与告警类载体同契约, 另加一条"零产物"实弹判据)
  · rc == 0 ⇒ **一字不出** (严格静默; 子脚本的 JSON 摘要只进 logs/review_bundle.log 取证面,
    否则每天 06:35 会推一行裸 JSON 给用户 = 把静默契约做成日推)
取证面: 子进程输出**追加**到 `logs/review_bundle.log`, 另加一行
`[carrier …] rc=… dur=… out_lines=…` 头 (便于多载体对账)。
回滚: 删 Hermes job + 删本文件与线上副本 (无 crontab 行依赖, 删除即静默)。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(os.environ.get("FTSEEK_REPO", "/opt/ftseek/FTSeek-US"))
CHILD = REPO / "scripts" / "review_bundle.py"
PYTHON = REPO / "venv" / "bin" / "python"
CHILD_LOG = REPO / "logs" / "review_bundle.log"
TIMEOUT = int(os.environ.get("CARRIER_TIMEOUT_S", "300"))


def _append_log(path: Path, text: str) -> None:
    try:
        with path.open("a", encoding="utf-8", errors="replace") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except Exception as e:                                       # noqa: BLE001
        print(f"[review_bundle_carrier] 取证日志写入失败 ({type(e).__name__}: {e})")


def _report_date() -> str:
    """ET 当日 (06:35 CST = ET 18:35 同日 ⇒ 当日即刚收盘的那天)。"""
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def main() -> int:
    if not CHILD.exists():
        print(f"[review_bundle_carrier] 载体级异常: 子脚本不存在 {CHILD} ⇒ 判决书断更 (不得读成'无待裁项')")
        return 3
    date = _report_date()
    argv = [str(PYTHON) if PYTHON.exists() else sys.executable, str(CHILD), "--date", date, "--vault"]
    if os.environ.get("REVIEW_BUNDLE_PUSH") == "1":
        argv.append("--push")
    env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONUNBUFFERED="1")
    t0 = time.time()
    try:
        p = subprocess.run(argv, cwd=str(REPO), env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=TIMEOUT)
        out, rc = p.stdout or "", p.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        rc = 2
        _append_log(CHILD_LOG, f"[carrier {datetime.now():%Y-%m-%d %H:%M:%S}] TIMEOUT >{TIMEOUT}s (已杀) date={date}\n{out}")
        print(f"[review_bundle_carrier] 载体级异常: 构建超时 >{TIMEOUT}s (已杀) ⇒ 本轮判决书未产出")
        print(out[-800:] if out else "")
        return rc
    dur = time.time() - t0
    lines = [ln for ln in (out or "").splitlines() if ln.strip()]
    _append_log(CHILD_LOG, f"[carrier {datetime.now():%Y-%m-%d %H:%M:%S}] rc={rc} dur={dur:.1f}s "
                           f"out_lines={len(lines)} date={date}\n{out}")
    if rc != 0:
        print(f"[review_bundle_carrier] 载体级异常: 子脚本 rc={rc} date={date} dur={dur:.1f}s ⇒ 判决书构建失败")
        print(out[-800:] if out else "")
        return rc
    # 静默契约 (严格): rc0 ⇒ 不输出任何字 (子脚本 stdout 已进取证日志)。
    # 证据: 产物必须真的落盘 (rc0 但零产物 = 静默坏掉, 必须与"没有待裁项"可区分)。
    produced = REPO / "data" / "reviews" / f"bundle_{date}.json"
    if not produced.exists() or produced.stat().st_mtime < t0 - 5:
        print(f"[review_bundle_carrier] 载体级异常: rc=0 但产物未落盘/未刷新 {produced} "
              f"⇒ 判决书断更 (不得读成'无待裁项')")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())