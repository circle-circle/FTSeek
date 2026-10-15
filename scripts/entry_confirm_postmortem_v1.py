#!/usr/bin/env python3
"""RFC-141 v1 弃单事后分解 (record-only, 只读) — 回答「v1 拦的对不对」。

缘起: 2026-10-15 用户问「今日生成的未交易股能否设置状态参与 v2 验证」。
直改状态塞 v2 裁决样本 = 两源混算 + 无真实成交盈亏 ⇒ 不可行; 但可以用
data/replay_bars_1m (本环境自有 1min bar, 与 SIM 撮合同源) 对 v1 全部
ABANDONED 行做事后分解, 立刻得到 v2 正确性的**间接证据**:

  · +5m/+10m/+30m 仍逆向 (方向归一 move 仍 <0) → 真避免 (v1 拦对, v2 grace 也会拦)
  · +10m 内回正 (move>0) → 误杀 (v1 拦错; v2 grace「任一同侧即确认」恰好救这类)

⚠️ 口径披露 (读数前须知):
  - 数据源 = replay_bars_1m (2026-01-02~10-14, **不含今日 10-15**), 与 SIM 撮合同源;
    futu request_history_kline 返回真实历史 (与本环境模拟价格水位差 ~32%) **不可用**,
    2026-10-15 首跑已实测证伪该路径。
  - bar 粒度 1min ⇒ 是「弃单后价格走向」分解, 不是 v2 判定回放 (mid-mid 需历史
    bid/ask 不存在; grace 10s 粒度 < 1min)。
  - 只分解价格路径, 不仿真成交盈亏 (bar 反事实出场一致率仅 60-63%, RFC §28.8)。
  - 每行打印口径校验: bar@判定时刻 close vs jsonl px_at_confirm 偏差, >2% 该笔
    降为「口径存疑」不计入分类。

用法: venv/bin/python scripts/entry_confirm_postmortem_v1.py [--date 2026-10-14]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FWD = ROOT / "data" / "shadow" / "entry_confirm_forward.jsonl"
BARS_DIR = ROOT / "data" / "replay_bars_1m"
MARKS = (("+5m", 5), ("+10m", 10), ("+30m", 30))
CALIB_WARN = 0.02                      # 口径校验阈值: bar 价 vs jsonl 价偏差


def load_abandoned(date: str | None):
    rows = []
    for line in FWD.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if (r.get("type") == "entry_confirm" and r.get("arm") == "treatment"
                and r.get("still_triggers") is False
                and (date is None or r.get("date") == date)):
            rows.append(r)
    return rows


def load_bars(sym: str):
    """replay_bars_1m/US_X.json → [(dt_et_naive, close)]; 不存在 ⇒ None。"""
    p = BARS_DIR / f"{sym.replace('.', '_')}.json"
    if not p.exists():
        return None
    raw = json.load(p.open(encoding="utf-8"))
    return [(datetime.strptime(b[0], "%Y-%m-%d %H:%M:%S"), float(b[4])) for b in raw]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="只看某天 (默认全 v1 时代)")
    args = ap.parse_args()

    rows = load_abandoned(args.date)
    if not rows:
        print("无 v1 ABANDONED 行")
        return 0
    print(f"v1 ABANDONED 行: {len(rows)} 笔 (date={args.date or '全窗'})")

    cache: dict[str, list | None] = {}
    classified = {"真避免": [], "误杀": [], "中性": [], "口径存疑": [], "无数据": []}

    for r in rows:
        sym = r["symbol"]
        if sym not in cache:
            cache[sym] = load_bars(sym)
        bars = cache[sym]
        if not bars:
            classified["无数据"].append(r)
            continue

        judge_dt = datetime.fromisoformat(str(r["ts"])).replace(tzinfo=None)
        anchor = float(r["anchor"])
        px_judge = float(r.get("px_at_confirm") or 0)
        direction = r["direction"]

        # 口径校验: bar 在判定时刻的 close 应 ≈ jsonl 记录的复核价 (同一价格宇宙)
        at_judge = [b for b in bars if abs((b[0] - judge_dt).total_seconds()) <= 60]
        calib = (abs(at_judge[0][1] / px_judge - 1)
                 if (at_judge and px_judge > 0) else None)

        def move_at(minutes: int):
            tgt = judge_dt + timedelta(minutes=minutes)
            fut = [b for b in bars if b[0] >= tgt]
            if not fut:
                return None
            mv = (fut[0][1] - anchor) / anchor * 100
            return round(mv if direction == "LONG" else -mv, 3)  # 方向归一: 正=顺向

        moves = {label: move_at(m) for label, m in MARKS}
        rec = {**r, **{f"move{k}": v for k, v in moves.items()},
               "calib_dev": (round(calib, 4) if calib is not None else None)}
        if calib is None or calib > CALIB_WARN:
            classified["口径存疑"].append(rec)
            continue
        m5, m10 = moves["+5m"], moves["+10m"]
        if m5 is None or m10 is None:
            classified["无数据"].append(rec)
        elif m5 < 0 and m10 < 0:
            classified["真避免"].append(rec)
        elif m10 > 0:
            classified["误杀"].append(rec)
        else:
            classified["中性"].append(rec)

    n = len(rows)
    print(f"\n═══ 分解 (方向归一: 正=顺交易方向) ═══")
    for k in ("真避免", "误杀", "中性", "口径存疑", "无数据"):
        v = classified[k]
        print(f"  {k}: {len(v)} 笔 ({len(v)/n*100:.0f}%)")
    print(f"\n判定规则: +5m∧+10m 均逆向=真避免; +10m 回正=误杀; 其余=中性; "
          f"bar价vs复核价偏差>2%=口径存疑")
    for k in ("误杀", "真避免", "口径存疑"):
        if not classified[k]:
            continue
        print(f"\n═══ {k}明细 ═══")
        for r in classified[k]:
            print(f"  {r['date']} {r['symbol']:>10} {r['direction']:<5} 锚${r['anchor']:<9} "
                  f"+5m={r.get('move+5m')}% +10m={r.get('move+10m')}% "
                  f"+30m={r.get('move+30m')}% 口径差={r.get('calib_dev')}")

    out = ROOT / "data" / "shadow" / "entry_confirm_v1_postmortem.json"
    out.write_text(json.dumps({
        "generated_at": datetime.now().astimezone().isoformat(),
        "caliber": ("replay_bars_1m close (与 SIM 撮合同源, 截至 2026-10-14, 不含今日); "
                    "无历史 bid/ask ⇒ 非 v2 判定回放, 仅弃单后价格分解; "
                    "futu 真实历史水位差~32%已证伪不可用"),
        "date_filter": args.date, "n": n,
        "counts": {k: len(v) for k, v in classified.items()},
        "detail": {k: v for k, v in classified.items()},
    }, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n产物: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
