#!/usr/bin/env python3
"""门A 前置·任务一: 台账对账 (trade_id 对齐) — DEB-20260805 门A 裁决前置.

队列: trade_date 2026-07-27~2026-08-04, exit_reason='SL_BREAKEVEN', MFE≥$8 (美元口径)
的已平仓单, 预期 n=17.

口径说明 (勘察实测):
  * 队列行 source_type 实为 'PLAYBOOK_SIM' (futu_simulate 主管线模拟臂);
    全库 MAIN_PIPELINE×SL_BREAKEVEN = 0 行. 任务书所写 source_type='MAIN_PIPELINE'
    与库内实际标记不符 — 按 exit_reason+窗口+MFE$≥8 复现的 17 笔与 DEB §十二 台账基线
    (ΣMFE $354.77 / Σ落袋毛 $69.11 / Σfees $89.84 / 捕获率 19.5%) 逐位吻合, 故以此为准,
    并如实记录该标记差异.
  * join 键: trade_records.metadata_json.playbook_instruction_id → playbook_instructions.id
    (17/17 存在); 并用 decision_id 两侧相等做交叉验证. 禁止 symbol+date 裸 join —
    但本脚本同时复算旧 symbol+date join 口径以便定位差异.
  * MFE 美元 = mfe_pct/100 × entry_price × quantity (mfe_pct 为方向调整后正数:
    LONG=(peak-entry)/entry×100, SHORT=(entry-trough)/entry×100; entry 为
    broker_deal_backfill 校正后成交价). 逐笔与 metadata_json.peak_price/trough_price
    反推值交叉验证.
  * 落袋毛 = sign × (exit_price-entry_price) × quantity, 与 realized_pnl 对账.

产出 (幂等覆盖):
  data/gate_a_reconciliation_2026-08-05/detail.csv   逐笔对账明细
  data/gate_a_reconciliation_2026-08-05/summary.json 三口径汇总 + 错配行定位

只读 DB (ro mode), 不触碰任何交易链路/配置.
用法: venv/bin/python scripts/gate_a_reconciliation.py
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB_PATH = REPO / "data" / "ftseek_us.db"
OUT_DIR = REPO / "data" / "gate_a_reconciliation_2026-08-05"

WINDOW_START = "2026-07-27"
WINDOW_END = "2026-08-04"
MFE_USD_MIN = 8.0

# DEB §七/§十二 台账基线 (trade_id 对齐口径) 与旧 join 口径, 用于断言比对
LEDGER_BASELINE = {"n": 17, "sum_mfe_usd": 354.77, "sum_gross": 69.11,
                   "sum_fees": 89.84, "capture_pct": 19.5}
OLD_JOIN_BASELINE = {"sum_mfe_usd": 332.25, "sum_gross": 48.10}


def load_queue(con: sqlite3.Connection) -> list[dict]:
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT id, trade_date, symbol, direction, entry_time, exit_time,
               entry_price, exit_price, quantity, realized_pnl, pnl_net,
               fees_est, mfe_pct, exit_reason, source_type, decision_id,
               metadata_json
        FROM trade_records
        WHERE exit_reason = 'SL_BREAKEVEN'
          AND trade_date BETWEEN ? AND ?
          AND exit_time IS NOT NULL
        ORDER BY id
        """, (WINDOW_START, WINDOW_END)).fetchall()
    out, excluded = [], []
    for r in rows:
        sign = 1.0 if r["direction"].upper() == "LONG" else -1.0
        entry, qty = float(r["entry_price"]), float(r["quantity"])
        mfe_usd = float(r["mfe_pct"] or 0.0) / 100.0 * entry * qty
        gross = sign * (float(r["exit_price"]) - entry) * qty
        rec = {
            "trade_id": r["id"], "trade_date": r["trade_date"], "symbol": r["symbol"],
            "direction": r["direction"].upper(), "entry_time": r["entry_time"],
            "exit_time": r["exit_time"], "entry_price": entry,
            "exit_price": float(r["exit_price"]), "quantity": qty,
            "sign": sign, "mfe_pct": float(r["mfe_pct"] or 0.0), "mfe_usd": mfe_usd,
            "gross_usd": gross, "realized_pnl": float(r["realized_pnl"] or 0.0),
            "pnl_net": float(r["pnl_net"] or 0.0), "fees_est": float(r["fees_est"] or 0.0),
            "source_type": r["source_type"], "decision_id": r["decision_id"],
            "meta": json.loads(r["metadata_json"] or "{}"),
        }
        (out if mfe_usd >= MFE_USD_MIN else excluded).append(rec)
    return out, excluded


def mfe_meta_check(rec: dict) -> tuple[float | None, float]:
    """用 metadata peak_price 反推 MFE$ 并与 mfe_pct 列对比.

    metadata 命名约定 (实测): peak_price = 有利极值 (LONG 的最高价 / SHORT 的最低价),
    trough_price = 不利极值; 即按 MFE/MAE 语义命名而非价格高低. 已用 SHORT 行
    (MU: entry 786.55, peak_price 780.35 → 0.7883% = mfe_pct 列值) 验证."""
    meta = rec["meta"]
    entry, qty = rec["entry_price"], rec["quantity"]
    ref = meta.get("peak_price")
    if ref is None:
        return None, float("nan")
    implied = abs(ref - entry) * qty
    col = rec["mfe_usd"]
    return implied, (implied - col)


def attach_instructions(con: sqlite3.Connection, queue: list[dict]) -> None:
    """trade_id 对齐: metadata.playbook_instruction_id → instructions.id;
    同时取旧 symbol+date join 行 (本队列恰为 1:1, 仅用于口径复算)."""
    con.row_factory = sqlite3.Row
    for rec in queue:
        iid = rec["meta"].get("playbook_instruction_id")
        rec["instruction_id"] = iid
        ins = by_dec = None
        if iid is not None:
            ins = con.execute(
                "SELECT id, date, symbol, direction, pnl, pnl_pct, execution_price, "
                "exit_price, exit_reason, max_favorable_excursion, decision_id, executed "
                "FROM playbook_instructions WHERE id=?", (iid,)).fetchone()
        rec["ins_by_id"] = dict(ins) if ins else None
        dec_ok = None
        if ins and rec["decision_id"]:
            dec_ok = (ins["decision_id"] == rec["decision_id"])
        rec["decision_id_match"] = dec_ok
        # 旧口径: symbol+date join (US. 前缀全名)
        alt = con.execute(
            "SELECT id, pnl, max_favorable_excursion, execution_price, exit_price "
            "FROM playbook_instructions WHERE date=? AND symbol=?",
            (rec["trade_date"], rec["symbol"])).fetchall()
        rec["olddup_count"] = len(alt)
        rec["old_join"] = dict(alt[0]) if len(alt) == 1 else None


def main() -> None:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    queue, excluded = load_queue(con)
    attach_instructions(con, queue)
    con.close()

    n = len(queue)
    print(f"队列: {WINDOW_START}~{WINDOW_END} SL_BREAKEVEN MFE≥${MFE_USD_MIN:.0f} → n={n} "
          f"(窗口内 MFE<{MFE_USD_MIN:.0f} 剔除 {len(excluded)} 笔: "
          f"{[(e['symbol'], round(e['mfe_usd'], 2)) for e in excluded]})")
    src = sorted({r["source_type"] for r in queue})
    print(f"source_type 实际值: {src} (注意: 非 MAIN_PIPELINE, 见脚本头注释)")

    # ── 逐笔校验 ──
    n_mfe_ok = n_gross_ok = n_dec_ok = 0
    for rec in queue:
        implied, diff = mfe_meta_check(rec)
        rec["mfe_usd_meta"] = implied
        rec["mfe_meta_diff"] = None if implied is None else round(diff, 4)
        if implied is not None and abs(diff) <= max(0.02 * implied, 0.5):
            n_mfe_ok += 1
        if abs(rec["gross_usd"] - rec["realized_pnl"]) <= 0.02:
            n_gross_ok += 1
        if rec["decision_id_match"]:
            n_dec_ok += 1
    print(f"校验: mfe_pct 与 metadata peak/trough 反推一致 {n_mfe_ok}/{n}; "
          f"方向调整毛= realized_pnl {n_gross_ok}/{n}; "
          f"decision_id 两侧一致 {n_dec_ok}/{n}; "
          f"instruction_id 命中 {sum(1 for r in queue if r['ins_by_id'])}/{n}")

    # ── 三口径汇总 ──
    def _s(key):
        return round(sum(r[key] for r in queue), 2)

    ledger = {
        "n": n,
        "sum_mfe_usd": _s("mfe_usd"),
        "sum_gross": round(sum(r["realized_pnl"] for r in queue), 2),
        "sum_gross_recomputed": _s("gross_usd"),
        "sum_fees": _s("fees_est"),
        "sum_net": _s("pnl_net"),
    }
    ledger["capture_pct"] = round(ledger["sum_gross"] / ledger["sum_mfe_usd"] * 100, 1)

    old_rows = [r for r in queue if r["old_join"]]
    old = {
        "n": len(old_rows),
        "sum_mfe_usd": round(sum((r["old_join"]["max_favorable_excursion"] or 0)
                                 for r in old_rows), 2),
        "sum_gross": round(sum((r["old_join"]["pnl"] or 0) for r in old_rows), 2),
    }

    print("\n=== 汇总 (三口径) ===")
    print(f"台账口径 (trade_id 对齐, trade_records 本表): n={ledger['n']} "
          f"ΣMFE=${ledger['sum_mfe_usd']} Σ落袋毛=${ledger['sum_gross']} "
          f"(重算毛 ${ledger['sum_gross_recomputed']}) Σfees=${ledger['sum_fees']} "
          f"Σnet=${ledger['sum_net']} 捕获率={ledger['capture_pct']}%")
    print(f"DEB 台账基线断言: {LEDGER_BASELINE}")
    ok_ledger = (ledger["n"] == LEDGER_BASELINE["n"]
                 and abs(ledger["sum_mfe_usd"] - LEDGER_BASELINE["sum_mfe_usd"]) < 0.05
                 and abs(ledger["sum_gross"] - LEDGER_BASELINE["sum_gross"]) < 0.05
                 and abs(ledger["sum_fees"] - LEDGER_BASELINE["sum_fees"]) < 0.05)
    print(f"→ 台账基线复现: {'PASS' if ok_ledger else 'FAIL'}")
    print(f"旧 instruction↔trade join 口径 (instruction.pnl/MFE): n={old['n']} "
          f"ΣMFE=${old['sum_mfe_usd']} Σ落袋毛=${old['sum_gross']}")
    print(f"DEB 旧口径断言: {OLD_JOIN_BASELINE}")
    ok_old = (abs(old["sum_mfe_usd"] - OLD_JOIN_BASELINE["sum_mfe_usd"]) < 0.05
              and abs(old["sum_gross"] - OLD_JOIN_BASELINE["sum_gross"]) < 0.05)
    print(f"→ 旧口径复现: {'PASS' if ok_old else 'FAIL'}")

    # ── 逐笔差异定位 ──
    print("\n=== 逐笔: 台账 vs 旧join (Δ=台账−旧) ===")
    mismatch = []
    for rec in queue:
        oj = rec["old_join"] or {}
        o_mfe = oj.get("max_favorable_excursion") or 0.0
        o_pnl = oj.get("pnl") or 0.0
        d_mfe = rec["mfe_usd"] - o_mfe
        d_pnl = rec["gross_usd"] - o_pnl
        flip = (rec["gross_usd"] > 0) != (o_pnl > 0) and o_pnl != 0
        tag = " ←符号翻转" if flip else (" ←错配" if abs(d_pnl) > 1.0 or abs(d_mfe) > 1.0 else "")
        print(f"trade {rec['trade_id']:>4} {rec['trade_date']} {rec['symbol']:<9} "
              f"MFE {rec['mfe_usd']:>6.2f} vs {o_mfe:>6.2f} (Δ{d_mfe:+.2f}) | "
              f"毛 {rec['gross_usd']:>6.2f} vs {o_pnl:>6.2f} (Δ{d_pnl:+.2f}){tag}")
        if flip or abs(d_pnl) > 1.0 or abs(d_mfe) > 1.0:
            mismatch.append({
                "trade_id": rec["trade_id"], "symbol": rec["symbol"],
                "trade_date": rec["trade_date"],
                "ledger_mfe_usd": round(rec["mfe_usd"], 2), "old_mfe_usd": round(o_mfe, 2),
                "ledger_gross": round(rec["gross_usd"], 2), "old_gross": round(o_pnl, 2),
                "sign_flip": bool(flip),
                "cause": ("entry_price 经 broker_deal_backfill 校正 "
                          f"({rec['meta'].get('entry_price_orig')}→{rec['entry_price']}), "
                          "instruction 侧 pnl 仍用旧 fill 价"
                          if rec["meta"].get("entry_corrected") else "待查"),
            })

    # NIO 费用异质点
    nio = [r for r in queue if r["symbol"].endswith("NIO")]
    fee_note = (f"NIO fees_est=${nio[0]['fees_est']:.2f} (队列内唯一 >$10 单笔, "
                f"名义 ${nio[0]['entry_price'] * nio[0]['quantity']:.0f}, 低价股按股佣金)"
                if nio else "无 NIO 行")
    print(f"\n费用异质点: {fee_note}")

    # ── 落盘 ──
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cols = ["trade_id", "trade_date", "symbol", "direction", "entry_time", "exit_time",
            "entry_price", "exit_price", "quantity", "mfe_pct", "mfe_usd", "mfe_usd_meta",
            "mfe_meta_diff", "gross_usd", "realized_pnl", "gross_minus_rp", "fees_est",
            "pnl_net", "instruction_id", "decision_id", "decision_id_match",
            "old_join_mfe", "old_join_pnl", "d_mfe", "d_pnl", "source_type"]
    with open(OUT_DIR / "detail.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for rec in queue:
            oj = rec["old_join"] or {}
            o_mfe = oj.get("max_favorable_excursion") or 0.0
            o_pnl = oj.get("pnl") or 0.0
            w.writerow([
                rec["trade_id"], rec["trade_date"], rec["symbol"], rec["direction"],
                rec["entry_time"], rec["exit_time"], rec["entry_price"], rec["exit_price"],
                int(rec["quantity"]), rec["mfe_pct"], round(rec["mfe_usd"], 4),
                rec["mfe_usd_meta"] if rec["mfe_usd_meta"] is not None else "",
                rec["mfe_meta_diff"] if rec["mfe_meta_diff"] is not None else "",
                round(rec["gross_usd"], 4), rec["realized_pnl"],
                round(rec["gross_usd"] - rec["realized_pnl"], 4), rec["fees_est"],
                rec["pnl_net"], rec["instruction_id"], rec["decision_id"],
                rec["decision_id_match"], round(o_mfe, 2), round(o_pnl, 2),
                round(rec["mfe_usd"] - o_mfe, 2), round(rec["gross_usd"] - o_pnl, 2),
                rec["source_type"],
            ])
    summary = {
        "window": [WINDOW_START, WINDOW_END],
        "queue_filter": ("exit_reason='SL_BREAKEVEN' AND MFE_usd>=8 AND trade_date in window "
                         "(实际 source_type=PLAYBOOK_SIM; MAIN_PIPELINE×SL_BREAKEVEN 全库=0)"),
        "join_key": ("trade_records.metadata_json.playbook_instruction_id → "
                     "playbook_instructions.id; decision_id 交叉验证"),
        "ledger": ledger, "ledger_baseline_deb": LEDGER_BASELINE,
        "ledger_reproduced": ok_ledger,
        "old_symbol_date_join": old, "old_join_baseline_deb": OLD_JOIN_BASELINE,
        "old_join_reproduced": ok_old,
        "excluded_mfe_below_8": [{"trade_id": e["trade_id"], "symbol": e["symbol"],
                                  "mfe_usd": round(e["mfe_usd"], 2)} for e in excluded],
        "mismatch_rows": mismatch,
        "fee_outlier": fee_note,
        "validation": {
            "mfe_pct_vs_metadata": f"{n_mfe_ok}/{n}",
            "gross_vs_realized_pnl": f"{n_gross_ok}/{n}",
            "decision_id_match": f"{n_dec_ok}/{n}",
            "instruction_id_hit": f"{sum(1 for r in queue if r['ins_by_id'])}/{n}",
        },
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"\n产出: {OUT_DIR / 'detail.csv'}\n      {OUT_DIR / 'summary.json'}")


if __name__ == "__main__":
    main()
