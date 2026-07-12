#!/usr/bin/env python3
"""触发未命中遥测分析 (2026-07-11, 测量优先).

读 data/trigger_miss_telemetry.jsonl (由 _check_intraday_trigger 埋点产出),
按未触发原因分类 + 量化偏离幅度, 回答核心问题:
  "73 单 never-trigger 里, 多少是追高/接飞刀被正确拒绝,
   多少是盘前参考价过期但盘中价其实在合理区间?"

用法:
    ./venv/bin/python scripts/trigger_miss_analysis.py [--days 14] [--close-call-pct 2.0]

输出:
    1. 按 reason 的计数 + 偏离幅度分布
    2. "close-call" 占比 (gap < threshold → 参考价稍微过期就能成交)
    3. 每个 inst_id 的 min gap (closest approach) → 真实的"几乎成交"数

不改任何数据/配置。纯只读。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
from collections import defaultdict
from datetime import datetime, timedelta

# ── 参数 ─────────────────────────────────────────────────
_days = 14
_close_call_pct = 2.0  # gap_pct 低于此值视为 "close call" (参考价稍过期)

for i, arg in enumerate(sys.argv[1:], 1):
    if arg == "--days" and i < len(sys.argv) - 1:
        _days = int(sys.argv[i + 1])
    elif arg == "--close-call-pct" and i < len(sys.argv) - 1:
        _close_call_pct = float(sys.argv[i + 1])

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "trigger_miss_telemetry.jsonl"

if not DATA_FILE.exists():
    print(f"⚠️  遥测文件不存在: {DATA_FILE}")
    print("   埋点已部署, 等下一个交易日开盘后自动产出数据.")
    sys.exit(0)

cutoff = (datetime.now() - timedelta(days=_days)).strftime("%Y-%m-%d")
rows = []
with open(DATA_FILE, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            # 跳过单测 artifact (test_playbook_pipeline 会调用触发函数写入 US.TEST)
            if r.get("inst_id") is None or r.get("symbol") == "US.TEST":
                continue
            if r.get("date", "") >= cutoff:
                rows.append(r)
        except json.JSONDecodeError:
            pass

if not rows:
    print(f"近 {_days} 天无数据 (cutoff={cutoff}). 等交易日自动积累.")
    sys.exit(0)

# ── 1. 按 reason 分组 ──────────────────────────────────────
reason_stats = defaultdict(lambda: {"n": 0, "gaps": [], "close": 0})
for r in rows:
    reason = r.get("reason", "unknown")
    gap = r.get("gap_pct", 0.0)
    s = reason_stats[reason]
    s["n"] += 1
    s["gaps"].append(gap)
    if gap < _close_call_pct:
        s["close"] += 1

print(f"\n{'=' * 65}")
print(f"  触发未命中遥测 (近 {_days} 天, 共 {len(rows)} 条观测)")
print(f"  close-call 阈值: gap < {_close_call_pct}% 视为 '参考价稍过期可填'")
print(f"{'=' * 65}\n")

print(f"{'reason':<16} {'观测':>6} {'avg_gap%':>9} {'med_gap%':>9} {'close%':>7}")
print("-" * 52)
for reason in sorted(reason_stats, key=lambda k: -reason_stats[k]["n"]):
    s = reason_stats[reason]
    gaps = sorted(s["gaps"])
    avg_gap = sum(gaps) / len(gaps) if gaps else 0
    med_gap = gaps[len(gaps) // 2] if gaps else 0
    close_pct = 100.0 * s["close"] / s["n"] if s["n"] else 0
    print(f"{reason:<16} {s['n']:>6} {avg_gap:>8.2f}% {med_gap:>8.2f}% {close_pct:>6.1f}%")

# ── 2. 每 inst_id 最小偏离 (closest approach) ────────────────
inst_min = {}  # inst_id → {min_gap, reason, symbol, direction}
for r in rows:
    iid = r.get("inst_id")
    if iid is None:
        continue
    gap = r.get("gap_pct", 999)
    if iid not in inst_min or gap < inst_min[iid]["min_gap"]:
        inst_min[iid] = {
            "min_gap": gap,
            "reason": r.get("reason"),
            "symbol": r.get("symbol"),
            "direction": r.get("direction"),
        }

if inst_min:
    close_insts = [v for v in inst_min.values() if v["min_gap"] < _close_call_pct]
    far_insts = [v for v in inst_min.values() if v["min_gap"] >= _close_call_pct]
    print(f"\n{'=' * 65}")
    print(f"  每条指令的 closest approach (unique inst_id: {len(inst_min)})")
    print(f"{'=' * 65}\n")
    print(f"  close-call (min_gap < {_close_call_pct}%): {len(close_insts)} ({100*len(close_insts)/len(inst_min):.0f}%)")
    print(f"     → 这些 = '参考价稍过期, 重锚后大概率能填' (低风险回收)")
    print(f"  far miss   (min_gap >= {_close_call_pct}%): {len(far_insts)} ({100*len(far_insts)/len(inst_min):.0f}%)")
    print(f"     → 这些 = '价格确实跑远, 系统正确拒绝追/接' (不该回收)")

    # direction 拆分
    for d in ("BUY", "SHORT"):
        _d = [v for v in inst_min.values() if v["direction"] == d]
        _dc = [v for v in _d if v["min_gap"] < _close_call_pct]
        if _d:
            print(f"\n  [{d}] {len(_d)} 指令 | close-call {len(_dc)} ({100*len(_dc)/len(_d):.0f}%)")
            # top reasons for close-call
            reason_cnt = defaultdict(int)
            for v in _dc:
                reason_cnt[v["reason"]] += 1
            for rr, cnt in sorted(reason_cnt.items(), key=lambda x: -x[1])[:3]:
                print(f"      {rr}: {cnt}")

print(f"\n{'=' * 65}")
print("  结论模板:")
print(f"  若 close-call 占比 > 30%: 入场价重锚有实证支撑 → 可推进 Fix A1.")
print(f"  若 close-call 占比 < 15%: 系统正确拒绝为主 → 维持现状, 不动触发带.")
print(f"{'=' * 65}\n")
