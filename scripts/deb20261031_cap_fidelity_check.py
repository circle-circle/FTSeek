#!/usr/bin/env python3
"""杠尺保真自检: 用生产 SSOT 函数重建 chase_cap 判定, 与 3316 行真实违例逐行对照。

目的 (skill T29-6 / T14): 任何 ATR 相对帽的反事实都必须先证明
"我的几何重建 == 生产实际判定", 否则反事实在量一条生产里不存在的规则。
只读, 不写任何生产文件。
"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.playbook.entry_band import CHASE_ZONE_PCT, normalize_chase_cap  # noqa: E402
from app.playbook.entry_routing import chase_cap_violated, parse_raw_semantics  # noqa: E402

SHADOW = ROOT / "data" / "shadow" / "pullback_limit_shadow.jsonl"
DB = ROOT / "data" / "ftseek_us.db"


def gen_entry(raw, inst, sem):
    """生产锚优先级逐字复刻 (auto_execute_flow.py:960-1000)."""
    is_short = "SHORT" in str(inst["direction"] or "").upper()
    j = raw if isinstance(raw, dict) else {}
    g = j.get("planned_entry")
    if not g:
        edge = inst["entry_low"] if is_short else inst["entry_high"]
        if edge:
            g = (float(edge) / (1 - CHASE_ZONE_PCT) if is_short
                 else float(edge) / (1 + CHASE_ZONE_PCT))
    if not g:
        conf = sem.get("confirmation") or {}
        if isinstance(conf, dict):
            g = conf.get("price_level")
    return g


def main():
    rows = [json.loads(l) for l in SHADOW.open(encoding="utf-8")]
    rows = [r for r in rows if r.get("type") == "chase_cap_violation"]
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    dids = sorted({r["decision_id"] for r in rows})
    q = ",".join("?" * len(dids))
    inst = {r["decision_id"]: dict(r) for r in con.execute(
        f"SELECT decision_id, direction, entry_price, entry_low, entry_high, "
        f"raw_instruction_json, atr_pct, stop_loss, take_profit, position_size "
        f"FROM playbook_instructions WHERE decision_id IN ({q})", dids)}
    con.close()

    n_cap_ok = n_cap_bad = n_vio_ok = n_vio_bad = n_noinst = 0
    bad = []
    for r in rows:
        i = inst.get(r["decision_id"])
        if not i:
            n_noinst += 1
            continue
        raw = i["raw_instruction_json"]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or "{}")
            except Exception:
                raw = {}
        sem = parse_raw_semantics(i["raw_instruction_json"])
        g = gen_entry(raw, i, sem)
        cap0 = sem["chase_cap_price"]
        # RFC-120 (2026-09-30 落地) 之前无倒挂归一: 旧行 cap 字段就是原值
        post_rfc120 = r.get("cap_inverted") is not None
        if post_rfc120 and cap0 is not None and g:
            cap_eff, _ = normalize_chase_cap(r["direction"], g, cap0)
        else:
            cap_eff = cap0
        if cap_eff is not None and abs(float(cap_eff) - float(r["chase_cap_price"])) < 1e-6:
            n_cap_ok += 1
        else:
            n_cap_bad += 1
            if len(bad) < 10:
                bad.append(("cap", r["date"], r["symbol"], cap_eff, r["chase_cap_price"]))
        if chase_cap_violated(r["direction"], r["repriced_entry"], cap_eff):
            n_vio_ok += 1
        else:
            n_vio_bad += 1
            if len(bad) < 20:
                bad.append(("vio", r["date"], r["symbol"], r["direction"],
                            r["repriced_entry"], cap_eff))
    print(f"rows={len(rows)} 指令缺失={n_noinst}")
    print(f"cap 重建一致 {n_cap_ok}/{n_cap_ok + n_cap_bad}")
    print(f"违例判定再现 {n_vio_ok}/{n_vio_ok + n_vio_bad}")
    for b in bad:
        print("  mismatch:", b)


if __name__ == "__main__":
    main()