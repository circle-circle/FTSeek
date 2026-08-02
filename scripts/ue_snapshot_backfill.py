#!/usr/bin/env python3
"""R5 (2026-08-02): UE 候选级快照历史回填 — 从 scan jsonl 重放全量 surfaced 候选.

背景: signal_snapshots 此前仅 DA, UE 覆盖 0% (接线见 llm_universe_scan.py R5 段,
  只对未来生效)。本脚本把 data/shadow/llm_universe_scan.jsonl 历史 scan 记录的
  surfaced 候选逐条回放进 signal_snapshots — 立即获得"质量分→结局"样本深度,
  不等 N 天积累。fwd 回填链 (run_signal_ic_backfill, ±1 日容差) 自动接历史日期。

幂等: decision_id UPSERT (COALESCE 不覆盖已有 factors)。零 LLM 调用零成本。
用法: ./venv/bin/python scripts/ue_snapshot_backfill.py [--dry-run]
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCAN_JSONL = ROOT / "data" / "shadow" / "llm_universe_scan.jsonl"


def main() -> int:
    dry = "--dry-run" in sys.argv
    from app.analysis.signal_ic_tracker import record_signal_snapshot

    n_scan = n_cand = n_written = 0
    for line in SCAN_JSONL.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") != "scan":
            continue
        n_scan += 1
        date = rec.get("date") or ""
        scanned_at = rec.get("scanned_at")
        for s in rec.get("surfaced") or []:
            if s.get("direction") not in ("LONG", "SHORT") or not s.get("symbol"):
                continue
            n_cand += 1
            if dry:
                n_written += 1
                continue
            try:
                record_signal_snapshot(
                    decision_id=f"universe_expansion:{date}:{s['symbol']}",
                    symbol=f"US.{s['symbol']}",
                    direction=s["direction"],
                    strength=int(round(float(s.get("confidence") or 0) * 100)),
                    confidence=float(s.get("confidence") or 0),
                    strategy_id="universe_expansion",
                    created_at=scanned_at,
                    trade_date=date,
                    factors={
                        "rubric_score": s.get("rubric_score"),
                        "evidence_checklist": s.get("evidence_checklist"),
                        "self_tier": s.get("self_tier"),
                        "verdict": (s.get("critique") or {}).get("verdict"),
                        "info_channel": s.get("info_channel"),
                        "scan_price": s.get("scan_price"),
                        "backfilled": True,
                    },
                )
                n_written += 1
            except Exception as e:
                print(f"  [skip] {date} {s.get('symbol')}: {e}")

    print(f"scan 记录 {n_scan} 条 · 候选 {n_cand} 只 · 写入 {n_written}"
          f"{' (dry-run)' if dry else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
