#!/usr/bin/env python3
"""入场成本分解 (record-only, 2026-10-31) —— 回答「限价还是市价 / 钱漏在哪一段」。

为什么建 (用户 2026-10-31 裁「都做」): 现役 TCA 报表只给 `计划→重定价` 一个合计数
(均值 +111bps)。这个数被误读成「下单方式浪费的钱」, 进而引出「改用限价单」的提案。
本脚本把它按**可回收性**拆成四段, 每段单独定责:

    plan ──①── prev_close ──②── open ──③── exec(重定价锚) ──④── fill(成交)
      ①计划锚 vs 前收盘   ②隔夜跳空   ③开盘→执行锚漂移   ④成交滑点

口径 (全部**方向感知**, 与 execution_tca 同尺):
  cost = +1 × (x/a − 1) for LONG; −1 × (x/a − 1) for SHORT ⇒ **正 = 对我们更贵**。
  (方向盲版本会把 SHORT 的「卖得更低」记成改善, 2026-10-17 已踩过。)

预注册读法 (读数前写死):
  ④ exec→fill            |均值| < 5bps  ⇒ 成交环节已达最优, 「限价 vs 市价」无油水 ⇒ 该提案证伪
  ② prev_close→open      隔夜跳空        ⇒ **不可回收** (无法以昨收成交), 只作成本披露
  ③ open→exec            开盘→执行锚      ⇒ **可回收** (信号→下单的滞后), 是唯一能动的一段
  ① plan→prev_close      |均值| < 30bps  ⇒ 计划锚≈前收 (隔夜前定), 与实际成交无关, 不得当成本
裁决阈值改动须另立 DEB; 本脚本只印读数, 不改任何下单方式。

只读: 输入 data/shadow/execution_tca.jsonl + data/replay_bars_daily/; 产物 latest.json。
用法: PYTHONPATH=. venv/bin/python scripts/entry_cost_decomposition.py [--since 2026-10-20]
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "shadow" / "execution_tca.jsonl"
BARS = ROOT / "data" / "replay_bars_daily"
OUT = ROOT / "data" / "shadow" / "entry_cost_decomposition_latest.json"

REACHABLE_THRESHOLD_BPS = 5.0     # ④ 判「已最优」
PLAN_ANCHOR_THRESHOLD_BPS = 30.0  # ① 判「计划锚≈前收」


def _bars(symbol: str) -> dict:
    p = BARS / f"{symbol.replace('.', '_')}.json"
    if not p.exists():
        return {}
    try:
        rows = json.loads(p.read_text())
    except Exception:                                             # noqa: BLE001
        return {}
    out = {}
    for r in rows:
        try:
            out[str(r[0])] = (float(r[1]), float(r[4]))            # (open, close)
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _cost_bps(a: float, x: float, direction: str) -> float:
    sgn = 1.0 if direction == "LONG" else -1.0
    return sgn * (x / a - 1.0) * 10000.0


def decompose(since: str) -> dict:
    segs = {"plan_to_prev_close": [], "prev_close_to_open": [],
            "open_to_exec": [], "exec_to_fill": []}
    skipped = 0
    for line in SRC.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("date", "") < since or not r.get("executed"):
            continue
        if not r.get("planned_entry") or not r.get("execution_entry") or not r.get("fill_price"):
            continue
        b = _bars(r["symbol"])
        dates = sorted(b)
        d = r["date"]
        if d not in b or dates.index(d) == 0:
            skipped += 1
            continue
        i = dates.index(d)
        prev_close = b[dates[i - 1]][1]
        open_ = b[d][0]
        dirn = r["direction"]
        segs["plan_to_prev_close"].append(_cost_bps(prev_close, r["planned_entry"], dirn))
        segs["prev_close_to_open"].append(_cost_bps(prev_close, open_, dirn))
        segs["open_to_exec"].append(_cost_bps(open_, r["execution_entry"], dirn))
        segs["exec_to_fill"].append(_cost_bps(r["execution_entry"], r["fill_price"], dirn))

    summary = {}
    for k, v in segs.items():
        if not v:
            summary[k] = {"n": 0}
            continue
        s = sorted(v)
        summary[k] = {"n": len(s), "mean_bps": round(statistics.mean(s), 1),
                      "median_bps": round(s[len(s) // 2], 1),
                      "total_bps": round(sum(s), 1)}
    return {"n_skipped_no_bars": skipped, "segments": summary}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-10-20")
    a = ap.parse_args()
    d = decompose(a.since)
    s = d["segments"]
    print("=" * 74)
    print("入场成本分解 (方向感知; 正 = 对我们更贵)  since", a.since)
    print("=" * 74)
    labels = {
        "plan_to_prev_close": "① 计划锚 → 前收盘",
        "prev_close_to_open": "② 前收盘 → 当日开盘 (隔夜跳空)",
        "open_to_exec": "③ 开盘 → 执行锚 (信号→下单滞后)",
        "exec_to_fill": "④ 执行锚 → 成交 (滑点)",
    }
    for k, lab in labels.items():
        v = s.get(k) or {}
        if not v.get("n"):
            print(f"  {lab:36s} 无样本")
            continue
        print(f"  {lab:36s} n={v['n']:3d} 均值 {v['mean_bps']:+7.1f}bps "
              f"中位 {v['median_bps']:+7.1f}bps 合计 {v['total_bps']:+8.1f}bps")

    fill = s.get("exec_to_fill") or {}
    plan = s.get("plan_to_prev_close") or {}
    open_exec = s.get("open_to_exec") or {}
    gap = s.get("prev_close_to_open") or {}
    verdict = {}
    if fill.get("n"):
        verdict["execution_optimal"] = abs(fill["mean_bps"]) < REACHABLE_THRESHOLD_BPS
    if plan.get("n"):
        verdict["plan_anchor_is_prev_close"] = abs(plan["mean_bps"]) < PLAN_ANCHOR_THRESHOLD_BPS
    print()
    print("── 定责 (预注册读法) ──")
    if verdict.get("execution_optimal"):
        print(f"  ④ 成交滑点 {fill['mean_bps']:+.1f}bps ⇒ **已达最优**, "
              f"「改用限价单省钱」在这条链上无油水 (证伪)")
    else:
        print(f"  ④ 成交滑点 {fill['mean_bps']:+.1f}bps ⇒ 仍有可回收空间, 值得单独评估")
    if gap.get("n"):
        print(f"  ② 隔夜跳空 {gap['mean_bps']:+.1f}bps ⇒ **不可回收** "
              f"(无法以昨收成交), 只作成本披露; 报价里不得与 ③ 混算")
    if open_exec.get("n"):
        print(f"  ③ 开盘→执行 {open_exec['mean_bps']:+.1f}bps ⇒ **可回收**: "
              f"这是「发现信号 → 真正下单」的滞后, 唯一能动的一段")
    if plan.get("n") and not verdict.get("plan_anchor_is_prev_close", True):
        print(f"  ① 计划锚与前收盘差 {plan['mean_bps']:+.1f}bps ⇒ 计划锚**不等于**前收, "
              f"不得把 计划→成交 的差额整包记为执行成本")
    print("\n⚠️ 只印不判 (不改下单方式/不改门槛): 本脚本只回答「哪一段可回收」。")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"since": a.since, **d, "verdict": verdict,
                               "reachable_threshold_bps": REACHABLE_THRESHOLD_BPS,
                               "caliber": "direction-aware cost bps (正=更贵); 只印不判"},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"结果落盘: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())