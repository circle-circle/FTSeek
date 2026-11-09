#!/usr/bin/env python3
"""Offline DA prompt A/B for session-aware prices and legacy peer calibers."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_SCRIPTS = Path("/root/.hermes/skills/deep-stock-analysis/scripts")
if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

import llm_synth
from data_collector import session_price_fields


def section(text: str, marker: str) -> str:
    if marker not in text:
        return ""
    return text.split(marker, 1)[1].split("\n## ", 1)[0]


def old_change_pct(prompt: str):
    match = re.search(r"^- 涨跌幅:\s*\+?([+-]?(?:\d+(?:\.\d*)?|\.\d+))%$",
                      section(prompt, "## 标的基本面"), re.MULTILINE)
    return float(match.group(1)) if match else None


def replay(trade_date: str):
    checkpoint_dir = ROOT / "data" / "da_checkpoint" / trade_date
    paths = sorted(checkpoint_dir.glob("*.raw.json"))
    rows = []
    hashes = []
    for path in paths:
        raw_bytes = path.read_bytes()
        hashes.append(hashlib.sha256(raw_bytes).hexdigest())
        raw = json.loads(raw_bytes)
        collected = copy.deepcopy(raw.get("collected") or {})
        collected = llm_synth._apply_prev_close_fix(collected)
        data_text = llm_synth._format_data_for_prompt(collected)
        user_prompt, user_tail = llm_synth.build_user_prompt(data_text)
        full_prompt = (user_tail or "") + user_prompt
        old_prompt = str(raw.get("user_prompt") or "")
        snapshot = collected.get("snapshot") or {}
        price = session_price_fields(snapshot)
        fundamental = section(full_prompt, "## 标的基本面")
        industry = section(full_prompt, "## 行业对比")
        peers = (collected.get("industry_peers") or {}).get("peers") or []
        peer_lines = [line for line in industry.splitlines() if line.startswith("  ") and ": " in line]
        expected_change = llm_synth._fmt_signed_pct_or_na(price["change_pct"])
        new_matches = f"涨跌幅(有效价 vs 昨收): {expected_change}" in fundamental
        old_change = old_change_pct(old_prompt)
        peer_na_lines = sum(
            "今日数据未采集(N/A) 5日数据未采集(N/A)" in line for line in peer_lines)
        rows.append({
            "symbol": raw.get("symbol") or path.name.replace(".raw.json", ""),
            "old_change_pct": old_change,
            "new_change_pct": price["change_pct"],
            "effective_price": price["price"],
            "price_as_of": price["price_as_of"],
            "new_change_matches_snapshot": new_matches,
            "old_change_was_zero": old_change is not None and abs(old_change) < 0.05,
            "peer_count_displayed": len(peers),
            "peer_lines_rendered": len(peer_lines),
            "legacy_peer_na_lines": peer_na_lines,
            "legacy_industry_marked": "旧版行业价格/涨跌缺少时点与6根日K证据" in industry,
            "full_prompt_built": bool(full_prompt.strip()),
        })

    effective_rows = [row for row in rows if row["new_change_pct"] is not None]
    return {
        "trade_date": trade_date,
        "mode": "offline_prompt_replay_no_llm",
        "input_files": len(paths),
        "input_sha256": hashlib.sha256("\n".join(hashes).encode()).hexdigest(),
        "candidate_symbols_before": len(rows),
        "candidate_symbols_after": len(rows),
        "unique_candidate_symbols": len({row["symbol"] for row in rows}),
        "full_prompts_built": sum(row["full_prompt_built"] for row in rows),
        "effective_snapshot_rows": len(effective_rows),
        "effective_change_matches": sum(row["new_change_matches_snapshot"] for row in effective_rows),
        "old_zero_change_rows": sum(row["old_change_was_zero"] for row in rows),
        "changed_change_rows": sum(
            row["old_change_pct"] is not None and row["new_change_pct"] is not None
            and abs(row["old_change_pct"] - row["new_change_pct"]) >= 0.05
            for row in rows),
        "peer_rows_before": sum(row["peer_count_displayed"] for row in rows),
        "peer_rows_after": sum(row["peer_lines_rendered"] for row in rows),
        "legacy_peer_na_rows": sum(row["legacy_peer_na_lines"] for row in rows),
        "legacy_industry_blocks_marked": sum(row["legacy_industry_marked"] for row in rows),
        "llm_calls": 0,
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = replay(args.date)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")


if __name__ == "__main__":
    main()
