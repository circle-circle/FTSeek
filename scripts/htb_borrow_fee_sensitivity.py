#!/usr/bin/env python3
"""[一次性·裁决前置] HTB 借券费敏感性分析 (2026-10-15, 审计 P2-5).

背景: 2026-09-24 借券费口径落地时明写「裁决前须跑 HTB 敏感性」(exec-batch 待办),
至今未履行 —  EXT 空侧生产已开 (SIM), rth_fade 影子 26 行 borrow_fee_est 全 $0,
隔夜空单 ACN 的 fee_breakdown 无 borrow 项。本脚本把「若收费」情景量化:

  borrow_fee = size_usd × rate_pct/100 × 计费天数/365
  计费天数: 日内单 (同日进出) = 0 天 (券商自然日惯例, BORROW_FEE_MIN_DAYS=0);
            隔 N 夜 = N 天; min_days 情景把下限抬到 1。

两个样本面 (只读):
  ① data/rth_fade_shadow.jsonl (rth_fade 空侧影子, size_usd=5000/笔, 全日内);
  ② trade_records 里 EXT 臂实盘 SHORT (entry_time/exit_time 跨夜数 = 计费天数)。

情景网格: rate ∈ {1%, 10%, 30%} (ESB 保守 / HTB / 硬 HTB) × min_days ∈ {0, 1}。
判定 (写死防购物): 若「min_days=1 ∧ rate=10%」情景下合计费 > 合计净额 50% ⇒ 费用结构
是 EXT 空侧毕业的实质门槛 (须立项); 否则记「费用非约束」。

零写库/零推送/零交易; 产物 data/htb_borrow_fee_sensitivity.json。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SHADOW = REPO / "data" / "rth_fade_shadow.jsonl"
DB = REPO / "data" / "ftseek_us.db"
OUT = REPO / "data" / "htb_borrow_fee_sensitivity.json"

RATES = [1.0, 10.0, 30.0]
MIN_DAYS_GRID = [0, 1]


def _days_held(entry_iso: str, exit_iso: str) -> int:
    """计费天数 = 跨夜数 (不同 ET 日历日); 同日=0。"""
    d0 = (entry_iso or "")[:10]
    d1 = (exit_iso or "")[:10]
    if not d0 or not d1:
        return 0
    return max(0, (datetime.fromisoformat(d1) - datetime.fromisoformat(d0)).days)


def _scenario(rows, rate, min_days):
    """rows: [(size_usd, days_held, net_usd)] → (费合计, 费后净合计)。"""
    fee = sum(sz * rate / 100 * max(days, min_days) / 365 for sz, days, _ in rows)
    net = sum(n for _, _, n in rows)
    return round(fee, 2), round(net - fee, 2)


def main() -> int:
    # ① rth_fade 影子 (全日内: days=0)
    shadow_rows = []
    if SHADOW.exists():
        for ln in SHADOW.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            r = json.loads(ln)
            shadow_rows.append((float(r.get("size_usd") or 0),
                                0, float(r.get("net_usd") or 0)))

    # ② EXT 臂实盘 SHORT (trade_records)
    ext_rows = []
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        for sym, direction, entry, exit_, qty, eprice, xprice, net in conn.execute(
                "SELECT symbol, direction, entry_time, exit_time, quantity, entry_price,"
                " exit_price, COALESCE(pnl_net, realized_pnl) FROM trade_records"
                " WHERE direction IN ('SHORT','SELL_SHORT') AND exit_time IS NOT NULL"
                " AND exit_time != '' AND trade_date >= '2026-09-23'"):
            days = _days_held(entry, exit_)
            size = abs(float(qty or 0) * float(eprice or 0))
            ext_rows.append((size, days, float(net or 0),
                             f"{sym} {days}夜"))
    finally:
        conn.close()

    out = {"generated_at": datetime.now().isoformat(timespec="seconds"),
           "scenarios": {}, "surfaces": {}}

    for label, rows in (("rth_fade_shadow", shadow_rows),
                        ("ext_arm_shorts", ext_rows)):
        if not rows:
            out["surfaces"][label] = {"n": 0, "note": "无样本"}
            continue
        surf = {"n": len(rows),
                "net_usd_before_borrow": round(sum(r[2] for r in rows), 2),
                "overnight_legs": [r[3] for r in rows if len(r) > 3 and r[1] > 0],
                "grid": {}}
        for rate in RATES:
            for md in MIN_DAYS_GRID:
                fee, net_after = _scenario([(r[0], r[1], r[2]) for r in rows], rate, md)
                surf["grid"][f"rate{rate}%_min{md}d"] = {
                    "borrow_fee_total": fee, "net_after_borrow": net_after}
        out["surfaces"][label] = surf

    # 判定 (预写死): min_days=1 ∧ rate=10% 下 费 > 净额50% ⇒ 费用是实质门槛
    verdicts = {}
    for label, surf in out["surfaces"].items():
        g = (surf.get("grid") or {}).get("rate10.0%_min1d")
        if not g:
            continue
        base = surf["net_usd_before_borrow"]
        verdicts[label] = (
            "费用是实质门槛 (费>净额50%)" if base > 0 and g["borrow_fee_total"] > 0.5 * base
            else ("费用非约束" if base > 0 else "净额为负, 费用不是主因"))
    out["verdict"] = verdicts
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"═══ HTB 借券费敏感性 ({out['generated_at']}) ═══")
    for label, surf in out["surfaces"].items():
        print(f"\n[{label}] n={surf.get('n')} 费前净额={surf.get('net_usd_before_borrow')}")
        for k, v in (surf.get("grid") or {}).items():
            print(f"  {k}: 费合计=${v['borrow_fee_total']} 费后净=${v['net_after_borrow']}")
        if surf.get("overnight_legs"):
            print(f"  隔夜腿: {surf['overnight_legs']}")
    print(f"\n判定: {json.dumps(verdicts, ensure_ascii=False)}")
    print(f"产物: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
