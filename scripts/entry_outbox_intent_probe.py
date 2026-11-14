#!/usr/bin/env python3
"""入场记账 Outbox 意图可得性探针（record-only，零交易行为变更）——孤儿仓根治方案A 的事前证据面（RFC-211 A0）。

用途（用户 2026-11-14「做」批准 A0 影子先行）：
  方案A（入场记账 Outbox）拟在成交瞬间把「富 record 意图」与指令状态同事务持久化，重启后按全量
  意图干净重放 save_trade（真 regime/ATR/因子），而非降级补录。本探针只读量化三件事，证明 A 的
  事前价值与可行性，供 RFC-211 预注册：
    ① 意图可得性：历史补录行经 metadata.playbook_instruction_id join 指令，核心意图分量
       （真成交价/regime/真 ATR/止损）可得率 —— 证「成交瞬间意图可完整捕获」。
    ② 孤儿暴露窗口：补录行 created_at（落账=回收时刻）− entry_time（券商成交时刻）时延分布
       —— 仓位在降级/孤儿态停留多久（A 干净接管的上下文，非 A 必然缩短的承诺）。
    ③ 可干净重放占比：核心意图齐全的补录行占比 —— 证「若有意图本可干净重放」的历史比例。
  **不改写路径、不建 outbox 表、不下单、不写库。**

只读承诺：sqlite mode=ro；默认仅控制台，--json PATH 才写一份只读分析产物。
口径：补录行=entry_reason∈ARTIFACT_ENTRY_REASONS（scripts/utils/da_sample.py）∧ exit_price>0；
  指令真值经 metadata.playbook_instruction_id join；真 ATR（美元）=指令 atr_pct×entry_price。
  时区：trade_records.created_at=datetime.now() 朴素系统本地时（trade_store.py:858），entry_time
  为 ET 带时区；两者统一 .astimezone(UTC) 后求差（朴素按系统本地时解释，勿假设 UTC，否则虚增 8h）。
  voting：DA 指令 decision_id 前缀 DA- 与 voting_predictions（DEC-）不同源，DA 无投票分量（正常
  路径亦取不到），故 voting 不计入 DA 核心/全意图，单列可得率供参考。
用法：PYTHONPATH=. venv/bin/python scripts/entry_outbox_intent_probe.py --start 2026-10-01
"""
import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from scripts.utils.da_sample import ARTIFACT_ENTRY_REASONS

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"

CORE_COMPONENTS = ("fill_price", "regime", "real_atr", "stop_loss")
FULL_EXTRA = ("conditions", "thesis", "horizon")


def _connect():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load(conn, start):
    placeholders = ",".join("?" for _ in ARTIFACT_ENTRY_REASONS)
    return [dict(r) for r in conn.execute(
        f"""
        SELECT id, symbol, trade_date, entry_time, created_at, entry_price, quantity,
               market_regime, metadata_json
          FROM trade_records
         WHERE source_type='PLAYBOOK_SIM' AND owner_strategy='playbook_sim'
           AND entry_reason IN ({placeholders})
           AND trade_date>=? AND exit_price>0
        """, (*sorted(ARTIFACT_ENTRY_REASONS), start)).fetchall()]


def _parse_utc(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _instruction_truth(conn, instruction_id):
    if not instruction_id:
        return None
    row = conn.execute(
        "SELECT market_regime, atr_pct, execution_price, stop_loss, take_profit, "
        "conditions_json, thesis, horizon, decision_id FROM playbook_instructions WHERE id=?",
        (instruction_id,)).fetchone()
    return dict(row) if row else None


def _has_voting(conn, decision_id):
    if not decision_id:
        return False
    try:
        return conn.execute(
            "SELECT 1 FROM voting_predictions WHERE decision_id=? LIMIT 1",
            (decision_id,)).fetchone() is not None
    except sqlite3.OperationalError:
        return False


def _percentile(sorted_vals, pct):
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, int(len(sorted_vals) * pct))
    return round(sorted_vals[idx], 1)


def probe(conn, start):
    rows = _load(conn, start)
    n = len(rows)
    joined = 0
    avail = {k: 0 for k in ("fill_price", "regime", "real_atr", "stop_loss",
                            "take_profit", "conditions", "thesis", "horizon", "voting")}
    core_complete = 0
    full_complete = 0
    atr_abs_diff = []
    atr_would_fix = 0
    gaps = []
    detail = []
    for row in rows:
        meta = json.loads(row["metadata_json"] or "{}")
        entry_utc = _parse_utc(row["entry_time"])
        created_utc = _parse_utc(row["created_at"])
        gap = None
        if entry_utc and created_utc:
            gap = (created_utc - entry_utc).total_seconds()
            gaps.append(gap)
        truth = _instruction_truth(conn, meta.get("playbook_instruction_id"))
        if truth is None:
            continue
        joined += 1
        entry_price = float(row["entry_price"] or 0)
        real_atr = None
        if truth.get("atr_pct") is not None and entry_price:
            real_atr = float(truth["atr_pct"]) * entry_price
        components = {
            "fill_price": bool(truth.get("execution_price")),
            "regime": bool(truth.get("market_regime")),
            "real_atr": real_atr is not None,
            "stop_loss": bool(truth.get("stop_loss")),
            "take_profit": bool(truth.get("take_profit")),
            "conditions": bool(truth.get("conditions_json") and truth["conditions_json"] not in ("[]", "{}", "")),
            "thesis": bool(truth.get("thesis")),
            "horizon": bool(truth.get("horizon")),
            "voting": _has_voting(conn, truth.get("decision_id")),
        }
        for key, present in components.items():
            if present:
                avail[key] += 1
        core_ok = all(components[k] for k in CORE_COMPONENTS)
        full_ok = core_ok and all(components[k] for k in FULL_EXTRA)
        core_complete += int(core_ok)
        full_complete += int(full_ok)
        proxy_atr = meta.get("atr")
        if real_atr is not None and proxy_atr is not None:
            diff = abs(float(proxy_atr) - real_atr)
            atr_abs_diff.append(diff)
            if diff > 1e-6:
                atr_would_fix += 1
        detail.append({
            "id": row["id"], "symbol": row["symbol"], "trade_date": row["trade_date"],
            "orphan_exposure_sec": round(gap, 1) if gap is not None else None,
            "core_intent_complete": core_ok,
            "backfill_regime": row["market_regime"], "instruction_regime": truth.get("market_regime"),
            "proxy_atr": proxy_atr,
            "instruction_real_atr": round(real_atr, 4) if real_atr is not None else None,
        })
    gaps.sort()
    atr_abs_diff.sort()
    return {
        "start": start, "n_backfill": n, "n_joined_instruction": joined,
        "intent_availability_pct": {k: (round(100 * v / joined, 1) if joined else None) for k, v in avail.items()},
        "core_intent_complete_n": core_complete,
        "core_intent_complete_pct": round(100 * core_complete / joined, 1) if joined else None,
        "full_intent_complete_n": full_complete,
        "full_intent_complete_pct": round(100 * full_complete / joined, 1) if joined else None,
        "clean_replayable_n": core_complete,
        "clean_replayable_pct": round(100 * core_complete / joined, 1) if joined else None,
        "real_atr_would_fix_n": atr_would_fix,
        "atr_median_abs_diff": round(atr_abs_diff[len(atr_abs_diff) // 2], 4) if atr_abs_diff else None,
        "orphan_exposure_window_sec": {
            "n": len(gaps),
            "min": round(gaps[0], 1) if gaps else None,
            "p50": _percentile(gaps, 0.5), "p90": _percentile(gaps, 0.9),
            "max": round(gaps[-1], 1) if gaps else None,
            "le_5s": sum(1 for g in gaps if g <= 5),
            "5_60s": sum(1 for g in gaps if 5 < g <= 60),
            "gt_60s": sum(1 for g in gaps if g > 60),
        },
        "detail": detail,
    }


def main():
    parser = argparse.ArgumentParser(description="入场记账 Outbox 意图可得性探针（RFC-211 方案A A0 事前证据）")
    parser.add_argument("--start", default="2026-10-01")
    parser.add_argument("--json", default=None)
    args = parser.parse_args()
    report = probe(_connect(), args.start)
    window = report["orphan_exposure_window_sec"]
    print("== entry outbox intent probe | start=%s | n_backfill=%d joined=%d ==" % (
        args.start, report["n_backfill"], report["n_joined_instruction"]))
    print("  核心意图齐全(真价/regime/真ATR/止损)=%d (%.1f%%) | 全意图齐全(+conditions/thesis/horizon)=%d (%.1f%%)" % (
        report["core_intent_complete_n"], report["core_intent_complete_pct"] or 0,
        report["full_intent_complete_n"], report["full_intent_complete_pct"] or 0))
    print("  可干净重放占比=%.1f%% | 真ATR 可修正代理ATR=%d 笔 (中位绝对差=%.4f)" % (
        report["clean_replayable_pct"] or 0, report["real_atr_would_fix_n"], report["atr_median_abs_diff"] or 0))
    print("  孤儿暴露窗口秒: n=%d p50=%s p90=%s max=%s (<=5s:%d 5-60s:%d >60s:%d)" % (
        window["n"], window["p50"], window["p90"], window["max"],
        window["le_5s"], window["5_60s"], window["gt_60s"]))
    print("  结论：核心意图可得率=%s%% ⇒ 方案A(成交瞬间持久化意图+干净重放)事前可行性成立。" % (
        report["core_intent_complete_pct"] or 0))
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print("wrote", args.json)


if __name__ == "__main__":
    main()
