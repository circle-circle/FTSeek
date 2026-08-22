#!/usr/bin/env python3
"""观察信号四关门裁决 (B2, 2026-08-22).

对挂账观察信号复跑四关门 — 预注册纪律 (DEB §57 同源):
  ① 同日截面市场调整 (消除 regime 漂移, T8 教训)
  ② IS/OOS 对半同号
  ③ |t| >= 2
  ④ n >= 30
  ⑤ 效应量地板: |超额均值| >= 0.3%
信号清单:
  - insider_net_sell × SELL_SHORT (8/21 挂账, 当时 n=26)
  - rsi_oversold × BUY (8/21 挂账, 当时 n=16)
PASS → 提案升级为入场门候选 (人工裁决); FAIL/WEAK → 继续攒样或剔除.
用法: python3 scripts/watch_signal_gate_eval.py [--signal insider_net_sell --direction SELL_SHORT]
输出: data/watch_signal_gate_eval_latest.json + stdout
"""
import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
DB = _ROOT / "data" / "ftseek_us.db"
OUT = _ROOT / "data" / "watch_signal_gate_eval_latest.json"

WATCH = [
    ("insider_net_sell", "SELL_SHORT"),
    ("rsi_oversold", "BUY"),
]
MIN_N = 30
T_GATE = 2.0
EFFECT_FLOOR = 0.003  # 0.3%


def _t_stat(vals):
    n = len(vals)
    if n < 3:
        return None
    m = sum(vals) / n
    var = sum((v - m) ** 2 for v in vals) / (n - 1)
    if var <= 0:
        return None
    return m / math.sqrt(var / n)


def eval_signal(db, cond, direction):
    # 信号行 + 同日全部已结算行 (截面基准, 排除信号自身行)
    rows = db.execute(
        "SELECT id, date, fwd_return_t1 FROM playbook_instructions "
        "WHERE direction = ? AND fwd_return_t1 IS NOT NULL "
        "AND conditions_json LIKE ?",
        (direction, f'%"{cond}"%')).fetchall()
    sig = [(r["id"], r["date"], r["fwd_return_t1"]) for r in rows]
    if not sig:
        return {"signal": cond, "direction": direction, "n": 0,
                "verdict": "NO_SAMPLE"}
    dates = sorted(set(d for _, d, _ in sig))
    mkt = {}  # date -> (sum, n) 排除信号行的同日截面
    placeholders = ",".join("?" * len(sig))
    ids = [s[0] for s in sig]
    for r in db.execute(
            "SELECT date, SUM(fwd_return_t1) s, COUNT(*) n FROM playbook_instructions "
            f"WHERE direction != 'SKIP' AND fwd_return_t1 IS NOT NULL "
            f"AND date IN ({','.join('?' * len(dates))}) "
            f"AND id NOT IN ({placeholders}) GROUP BY date",
            dates + ids):
        mkt[r["date"]] = (r["s"], r["n"])
    excess = []
    for _, d, ret in sig:
        s, n = mkt.get(d, (0.0, 0))
        if n > 0:
            excess.append(ret - s / n)
        else:
            excess.append(ret)  # 无同日基准: 裸收益 (占比报告)
    n = len(excess)
    mean_ex = sum(excess) / n if n else 0.0
    t = _t_stat(excess)
    # IS/OOS 对半 (按日期排序切)
    sig_sorted = sorted(sig, key=lambda x: x[1])
    half = len(sig_sorted) // 2
    def _bucket(vals):
        ex = []
        for _, d, ret in vals:
            s, nn = mkt.get(d, (0.0, 0))
            ex.append(ret - s / nn if nn > 0 else ret)
        return ex
    e1, e2 = _bucket(sig_sorted[:half]), _bucket(sig_sorted[half:])
    m1 = sum(e1) / len(e1) if e1 else 0.0
    m2 = sum(e2) / len(e2) if e2 else 0.0
    same_sign = (m1 * m2 > 0) if e1 and e2 else False
    n_gate = n >= MIN_N
    t_gate = t is not None and abs(t) >= T_GATE
    effect_gate = abs(mean_ex) >= EFFECT_FLOOR
    gates = {"n>=30": n_gate, "|t|>=2": t_gate,
             "IS/OOS同号": same_sign, "效应量>=0.3%": effect_gate}
    passed = all(gates.values())
    return {
        "signal": cond, "direction": direction, "n": n,
        "dates": f"{dates[0]}~{dates[-1]}",
        "raw_mean_t1": round(sum(r for _, _, r in sig) / n, 5),
        "excess_mean_t1": round(mean_ex, 5), "t_stat": round(t, 3) if t else None,
        "is_mean": round(m1, 5), "oos_mean": round(m2, 5),
        "gates": gates,
        "verdict": "PASS" if passed else
                   ("WEAK(继续攒样)" if sum(gates.values()) >= 2 else "FAIL"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signal")
    ap.add_argument("--direction")
    args = ap.parse_args()
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    targets = WATCH
    if args.signal:
        targets = [(args.signal, args.direction or "")]
    results = [eval_signal(db, c, d) for c, d in targets]
    out = {"generated_at": datetime.now().isoformat(),
           "prereg": "四关门: 同日截面调整+IS/OOS同号+|t|>=2+n>=30+效应量>=0.3%; "
                     "PASS→提案入场门候选(人工裁决); record-only 零行为变更",
           "results": results}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    for r in results:
        g = r.get("gates", {})
        gs = " ".join(f"{k}:{'✓' if v else '✗'}" for k, v in g.items())
        print(f"{r['signal']}×{r['direction']}: n={r['n']} {r.get('dates','')} "
              f"超额={r.get('excess_mean_t1')} t={r.get('t_stat')} | {gs} → {r['verdict']}")
    print(f"落盘: {OUT}")


if __name__ == "__main__":
    main()
