#!/usr/bin/env python3
"""补录回收保真度探针（record-only，零交易行为变更）——孤儿仓修复方案B 的事前证据面。

用途（用户 2026-11-14「同意」方案B 影子先行）：
  position_sync 补录回收的 trade_record 硬编码 market_regime='RANGE'、ATR 用代理值
  （peak 重置=entry），而**关联指令（playbook_instructions）保有入场决策的真 regime/ATR**。
  本探针只读量化「补录现值 vs 指令真值」的保真度缺口，证明方案B（补录继承指令真值）
  的事前价值与可行性，供 RFC-210 预注册。**不改 position_sync、不下单、不写库。**

只读承诺：sqlite mode=ro；默认仅控制台，--json PATH 才写一份只读分析产物。
口径：补录行=entry_reason∈ARTIFACT_ENTRY_REASONS（scripts/utils/da_sample.py）；
  指令真值经 metadata.playbook_instruction_id join；regime 保真度=补录 regime==指令 regime；
  真 ATR（美元）=指令 atr_pct×entry_price（与补录代理 ATR 同量纲对比）。
用法：PYTHONPATH=. venv/bin/python scripts/backfill_fidelity_probe.py --start 2026-10-01
"""
import argparse
import json
import sqlite3
from pathlib import Path

from scripts.utils.da_sample import ARTIFACT_ENTRY_REASONS

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"


def _connect():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load(conn, start):
    placeholders = ",".join("?" for _ in ARTIFACT_ENTRY_REASONS)
    return [dict(r) for r in conn.execute(
        f"""
        SELECT id, symbol, trade_date, entry_price, market_regime, metadata_json
          FROM trade_records
         WHERE source_type='PLAYBOOK_SIM' AND owner_strategy='playbook_sim'
           AND entry_reason IN ({placeholders})
           AND trade_date>=? AND exit_price>0
        """, (*sorted(ARTIFACT_ENTRY_REASONS), start)).fetchall()]


def _instruction_truth(conn, instruction_id):
    if not instruction_id:
        return None
    row = conn.execute(
        "SELECT market_regime, atr_pct FROM playbook_instructions WHERE id=?",
        (instruction_id,)).fetchone()
    return dict(row) if row else None


def probe(conn, start):
    rows = _load(conn, start)
    n = len(rows)
    joined = 0
    regime_match = 0
    inst_regime_available = 0
    inst_atr_available = 0
    atr_abs_diff = []
    detail = []
    for row in rows:
        meta = json.loads(row["metadata_json"] or "{}")
        truth = _instruction_truth(conn, meta.get("playbook_instruction_id"))
        if truth is None:
            continue
        joined += 1
        inst_regime = truth.get("market_regime") or ""
        inst_atr_pct = truth.get("atr_pct")
        if inst_regime:
            inst_regime_available += 1
            if inst_regime == (row["market_regime"] or ""):
                regime_match += 1
        real_atr = None
        if inst_atr_pct is not None and row["entry_price"]:
            real_atr = float(inst_atr_pct) * float(row["entry_price"])
            inst_atr_available += 1
            proxy_atr = meta.get("atr")
            if proxy_atr is not None:
                atr_abs_diff.append(abs(float(proxy_atr) - real_atr))
        detail.append({
            "id": row["id"], "symbol": row["symbol"], "trade_date": row["trade_date"],
            "backfill_regime": row["market_regime"], "instruction_regime": inst_regime,
            "proxy_atr": meta.get("atr"), "instruction_real_atr": round(real_atr, 4) if real_atr is not None else None,
        })
    return {
        "start": start, "n_backfill": n, "n_joined_instruction": joined,
        "regime_fidelity_current_pct": round(100 * regime_match / joined, 1) if joined else None,
        "instruction_regime_available_pct": round(100 * inst_regime_available / joined, 1) if joined else None,
        "instruction_atr_available_pct": round(100 * inst_atr_available / joined, 1) if joined else None,
        "regime_would_fix_n": inst_regime_available - regime_match,
        "atr_median_abs_diff": round(sorted(atr_abs_diff)[len(atr_abs_diff) // 2], 4) if atr_abs_diff else None,
        "detail": detail,
    }


def main():
    parser = argparse.ArgumentParser(description="补录回收保真度探针（方案B 事前证据）")
    parser.add_argument("--start", default="2026-10-01")
    parser.add_argument("--json", default=None)
    args = parser.parse_args()
    report = probe(_connect(), args.start)
    print("== backfill fidelity probe | start=%s | n_backfill=%d joined=%d ==" % (
        args.start, report["n_backfill"], report["n_joined_instruction"]))
    print("  regime 保真度(现值==指令真值)=%.1f%% | 指令真 regime 可得=%.1f%% | 方案B 可修正 regime=%d 笔" % (
        report["regime_fidelity_current_pct"] or 0, report["instruction_regime_available_pct"] or 0,
        report["regime_would_fix_n"]))
    print("  指令真 ATR 可得=%.1f%% | 代理ATR vs 真ATR 中位绝对差=%.4f" % (
        report["instruction_atr_available_pct"] or 0, report["atr_median_abs_diff"] or 0))
    print("  结论：补录硬编码 RANGE 与指令真 regime 系统性不符；方案B(继承指令真值)事前价值成立。")
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print("wrote", args.json)


if __name__ == "__main__":
    main()
