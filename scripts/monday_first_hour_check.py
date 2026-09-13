#!/usr/bin/env python3
"""周一开盘首小时双验证 (2026-09-14, 用户裁C后的一次性检查).

验证两条今夏最大变更在生产开盘时段的真实行为:
  A. 退出链修复 (工作区WIP, 9/11半部署事故根因修复): exit_identity_required 零拒绝
     + 服务无 ERROR/Traceback + 持仓对账正常。
  B. RFC-082 伪证门 DA enforce 转正 (9/12 生效): trigger_miss_telemetry 中
     date>=2026-09-12 且 reason=falsification_vwap_slope 的行 = **真拦截**
     (不再是 would-block 对比组); 列出 symbol/decision_id 供人工核对拦截是否合理。

用法: venv/bin/python scripts/monday_first_hour_check.py [--since 21:30]
挂载: Hermes cron 一次性 2026-09-14 22:40 CST (开盘首小时 09:30-10:30 ET 结束后)。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "logs" / "ftseek_us.log"
TELEM = ROOT / "data" / "trigger_miss_telemetry.jsonl"
ENFORCE_SINCE = "2026-09-12"   # RFC-082 生效日 (含)


def _tail_lines(path: Path, n: int = 20000) -> list[str]:
    out = subprocess.run(["tail", "-n", str(n), str(path)],
                         capture_output=True, text=True)
    return out.stdout.splitlines()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="21:30", help="CST HH:MM 只看此后日志")
    args = ap.parse_args()
    today = datetime.now().strftime("%Y-%m-%d")
    issues: list[str] = []
    lines_ok: list[str] = []

    # --- A. 退出链 ---
    lines = _tail_lines(LOG, 40000)
    cutoff = f"{today} {args.since}"
    recent = [l for l in lines if l[:16] >= cutoff]
    ident = [l for l in recent if "exit_identity_required" in l]
    errs = [l for l in recent if re.search(r"\bERROR\b|Traceback", l)]
    svc = subprocess.run(["systemctl", "is-active", "ftseek-us"],
                         capture_output=True, text=True).stdout.strip()
    if svc != "active":
        issues.append(f"服务非active: {svc}")
    else:
        lines_ok.append(f"服务 active (启动 {datetime.now():%H:%M} 检查)")
    if ident:
        issues.append(f"exit_identity_required 拒绝 {len(ident)} 条 (退出链回归!) 首条: {ident[0][:160]}")
    else:
        lines_ok.append(f"退出链: 开盘时段 exit_identity_required 零拒绝 (窗口 {args.since} 起 {len(recent)} 行)")
    if errs:
        # 过滤已知无害 (持仓对账差异=playbook-sim自管)
        hard = [e for e in errs if "持仓对账完成" not in e]
        if hard:
            issues.append(f"ERROR/Traceback {len(hard)} 条, 首条: {hard[0][:200]}")
        else:
            lines_ok.append(f"无硬ERROR ({len(errs)} 条均为对账信息行)")
    else:
        lines_ok.append("日志零 ERROR/Traceback")

    # --- A2. 回执账本权威核查 (比日志grep更硬: 非BOOKED=真卡住) ---
    exits_db = ROOT / "data" / "playbook_exits.db"
    if exits_db.exists():
        import sqlite3
        ee = sqlite3.connect(f"file:{exits_db}?mode=ro", uri=True)
        ee.row_factory = sqlite3.Row
        try:
            stuck = [dict(r) for r in ee.execute(
                "SELECT intent_id, symbol, state, quantity, last_error, updated_at "
                "FROM exit_intents WHERE state != 'BOOKED' ORDER BY updated_at DESC LIMIT 10")]
            total = ee.execute("SELECT COUNT(*) FROM exit_intents").fetchone()[0]
        finally:
            ee.close()
        if stuck:
            issues.append(f"exit_intents 非BOOKED {len(stuck)} 条 (仓位可能卡住!): {stuck}")
        else:
            lines_ok.append(f"回执账本: {total} 条 intent 全部 BOOKED, 零卡住")
    else:
        lines_ok.append("回执账本: playbook_exits.db 不存在 (退出链未产生intent或旧版运行中——需人工确认!)")

    # --- B. RFC-082 DA enforce ---
    da_rows = []
    if TELEM.exists():
        for l in TELEM.read_text(encoding="utf-8").splitlines():
            l = l.strip()
            if not l:
                continue
            try:
                r = json.loads(l)
            except Exception:
                continue
            if r.get("reason") != "falsification_vwap_slope":
                continue
            if str(r.get("date", "")) >= ENFORCE_SINCE:
                da_rows.append(r)
    # enforce 生效后 DA 行 = 真拦 (旧版 DA 只记不拦)。按 decision_id 前缀分组呈报。
    from collections import Counter
    pref = Counter(str(r.get("decision_id", "")).split("-")[0] or "?" for r in da_rows)
    if da_rows:
        lines_ok.append(f"RFC-082: 生效日后伪证门拦截 {len(da_rows)} 行 (按decision_id前缀: {dict(pref)})")
        for r in da_rows[-8:]:
            lines_ok.append(f"  · {r.get('date')} {r.get('symbol')} {r.get('direction')} "
                            f"did={str(r.get('decision_id'))[:40]} gap={r.get('gap_pct')}")
    else:
        lines_ok.append(f"RFC-082: 生效日后暂无伪证门拦截行 (门未触发≠故障; 遥测最后一行 date 需人工瞄一眼)")

    print(f"【周一开盘首小时双验证 {today}】")
    for s in lines_ok:
        print("✓", s)
    if issues:
        print("✗ 问题:")
        for s in issues:
            print("  ✗", s)
        return 1
    print("判定: 双验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
