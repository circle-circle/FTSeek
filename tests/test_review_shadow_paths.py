"""日报影子路径: entry_path chase/pullback + 出场后 close_gap 只读聚合。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.review.bundle_builder import build_shadow_paths  # noqa: E402


def test_shadow_paths_aggregate_entry_and_post_exit(tmp_path):
    pullback = tmp_path / "pullback.jsonl"
    pullback.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"date": "2026-10-19", "type": "trigger", "symbol": "US.A",
                 "entry_path": "chase"},
                {"date": "2026-10-19", "type": "trigger", "symbol": "US.B",
                 "entry_path": "pullback"},
                {"date": "2026-10-19", "type": "chase_cap_violation", "symbol": "US.C",
                 "entry_path": "chase"},
                {"date": "2026-10-18", "type": "trigger", "symbol": "US.D",
                 "entry_path": "chase"},
            ]
        ),
        encoding="utf-8",
    )
    post_exit = tmp_path / "post_exit.jsonl"
    post_exit.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"date": "2026-10-19", "symbol": "US.A", "close_gap_pct": 1.5},
                {"date": "2026-10-19", "symbol": "US.B", "close_gap_pct": -0.5},
                {"date": "2026-10-19", "symbol": "US.C", "close_gap_pct": None},
            ]
        ),
        encoding="utf-8",
    )
    out = build_shadow_paths(
        "2026-10-19", pullback_path=pullback, post_exit_path=post_exit
    )
    assert out["entry_trigger_count"].value == 2
    assert out["entry_chase_count"].value == 1
    assert out["entry_pullback_count"].value == 1
    assert out["entry_chase_symbols"].value == ["US.A"]
    assert out["entry_pullback_symbols"].value == ["US.B"]
    assert out["post_exit_n"].value == 2
    assert out["post_exit_close_gap_mean"].value == 0.5
    assert out["post_exit_close_gap_median"].value == 0.5
    assert out["post_exit_miss_gt1_count"].value == 1
    assert out["post_exit_miss_symbols"].value == ["US.A"]
