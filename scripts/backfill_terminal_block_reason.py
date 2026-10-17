#!/usr/bin/env python3
"""终态门拦截原因回填 (测量面, 零交易行为变更).

背景: 被门拦下且当日未成交的指令, EOD 兜底把 skip_reason 统一写成
  "未触发 (no_trigger: 盘中价格未突破入场确认条件)" (shadow_tracker.py:282),
而多数门 (low_atr_block / streak_ban / vwap_gate_block / after_cutoff / extreme_gap ...)
只在 data/trigger_miss_telemetry.jsonl 留行、不写指令级 skip_reason ⇒ 库里
**分不清"门拦截"与"价格未到"**, 任何以终态标签分组的门级读数 (避坑/杀机会、误杀率、
漏斗) 归属未定。代码三处注释自认此黑洞: pipeline.py:80 / shadow_tracker.py:258 /
trigger_flow.py:879。

实测反证 (2026-10-16): US.COIN 终态"价格未到", 日志 43 行连败封禁 + 当日 35 根 bar
触及入场价; US.TSM 终态"价格未到", 日志 低ATR门 + `#2 触发窗截止` + 147/390 根 bar 触及 415。

本脚本把**当日拦截原因分布**浅合并进 playbook_instructions.metadata_json
(键: terminal_block_reason / terminal_block_reasons / terminal_block_reason_last /
terminal_block_reason_ts), 走 db.merge_instruction_metadata (零 schema 迁移,
且该函数保证既有 metadata 注记一个不丢)。**不改 skip_reason 字面** —— 它有 6+ 消费方
按字面解析 (dashboard chain / plan_vs_actual / short_supply_funnel ...)。

口径: 读遥测复用现役函数 app.playbook.shadow_tracker._terminal_block_reason_for
(主因 = 剔除 after_cutoff 后的最高频原因; 只有 after_cutoff 时 = after_cutoff,
诚实表达"到截止都没触发")。写入侧幂等: 已有 terminal_block_reason 的行跳过。

用法:
  PYTHONPATH=. venv/bin/python scripts/backfill_terminal_block_reason.py --dry
  PYTHONPATH=. venv/bin/python scripts/backfill_terminal_block_reason.py --apply --since 2026-09-01

回滚: 键为纯注记, 不影响任何判决; 撤销可按 data/shadow/terminal_block_reason_backfill.jsonl
逐行把 metadata_json 里这四个键删掉 (merge 语义 = 浅合并, 需手工删键)。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger("ftseek.scripts.tbr_backfill")
PROVENANCE = PROJECT_ROOT / "data" / "shadow" / "terminal_block_reason_backfill.jsonl"
_GENERIC_LABEL = "未触发 (no_trigger: 盘中价格未突破入场确认条件)"
_KEYS = (
    "terminal_block_reason",
    "terminal_block_reasons",
    "terminal_block_reason_last",
    "terminal_block_reason_ts",
)


def _already_tagged(metadata_json: Any) -> bool:
    if not metadata_json:
        return False
    try:
        meta = json.loads(metadata_json)
    except Exception:
        return False
    return isinstance(meta, dict) and bool(meta.get("terminal_block_reason"))


def _append_provenance(records: List[Dict[str, Any]]) -> None:
    PROVENANCE.parent.mkdir(parents=True, exist_ok=True)
    existing: Dict[int, Dict[str, Any]] = {}
    if PROVENANCE.exists():
        bad = 0
        for line in PROVENANCE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                existing[int(rec.get("id") or 0)] = rec
            except (json.JSONDecodeError, ValueError):
                bad += 1
        if bad:
            logger.warning("取证台账 %s 有 %d 行坏 JSON 已跳过", PROVENANCE, bad)
    for rec in records:
        existing[int(rec["id"])] = rec
    with PROVENANCE.open("w", encoding="utf-8") as handle:
        for rec in sorted(existing.values(), key=lambda item: int(item["id"])):
            handle.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="真写 (默认只算不写)")
    parser.add_argument("--dry", action="store_true", help="显式预览 (同默认行为)")
    parser.add_argument("--since", default="2026-09-01", help="起始交易日 (默认 2026-09-01)")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 行 (0=全量, 供试跑)")
    args = parser.parse_args()
    write = args.apply and not args.dry

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    from app.playbook.db import PlaybookDB
    from app.playbook.shadow_tracker import _terminal_block_reason_for

    db = PlaybookDB()
    rows = [
        dict(r)
        for r in db._conn().execute(
            """
            SELECT id, date, symbol, direction, skip_reason, metadata_json
            FROM playbook_instructions
            WHERE date >= ? AND COALESCE(executed, 0) = 0
            ORDER BY date, id
            """,
            (args.since,),
        ).fetchall()
    ]
    if args.limit:
        rows = rows[: args.limit]
    logger.info("非成交指令 (since %s): n=%d | 模式=%s", args.since, len(rows), "APPLY" if write else "DRY")

    n_ok = n_no_tel = n_tagged = 0
    n_mislabeled = 0
    by_reason: Dict[str, int] = {}
    provenance: List[Dict[str, Any]] = []
    for row in rows:
        got = _terminal_block_reason_for(row["id"], row["date"])
        if not got or not got.get("primary"):
            n_no_tel += 1
            continue
        if _already_tagged(row.get("metadata_json")):
            n_tagged += 1
            continue
        primary = got["primary"]
        by_reason[primary] = by_reason.get(primary, 0) + 1
        mislabeled = str(row.get("skip_reason") or "") == _GENERIC_LABEL
        n_mislabeled += int(mislabeled)
        applied = False
        if write:
            applied = bool(
                db.merge_instruction_metadata(
                    row["id"],
                    {
                        "terminal_block_reason": primary,
                        "terminal_block_reasons": got.get("counts"),
                        "terminal_block_reason_last": got.get("last"),
                        "terminal_block_reason_ts": got.get("ts"),
                    },
                )
            )
        if applied:
            n_ok += 1
        provenance.append(
            {
                "id": row["id"],
                "date": row["date"],
                "symbol": row["symbol"],
                "direction": row["direction"],
                "primary": primary,
                "counts": got.get("counts"),
                "skip_reason_before": row.get("skip_reason") or "",
                "was_generic_label": mislabeled,
                "applied": applied,
                "batch": "2026-10-17_tbr_backfill",
            }
        )

    logger.info(
        "候选 n=%d | 已回填(幂等跳过) n=%d | 无遥测行(标签诚实) n=%d | 写入成功 n=%d",
        len(rows), n_tagged, n_no_tel, n_ok,
    )
    logger.info("其中终态标签为通用'未触发' (= 本次修复的失真行) n=%d", n_mislabeled)
    if by_reason:
        logger.info(
            "主因分布: %s",
            ", ".join(f"{k}={v}" for k, v in sorted(by_reason.items(), key=lambda kv: -kv[1])),
        )
    if not write:
        logger.info("DRY 模式未写库/未写台账; 加 --apply 落 metadata_json (键: %s)", ",".join(_KEYS))
    elif provenance:
        _append_provenance(provenance)
        logger.info("取证台账: %s (%d 行)", PROVENANCE, len(provenance))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())