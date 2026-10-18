"""entry_anchor_dual 只读读数契约 (2026-10-18)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.entry_anchor_dual_eval import compute_metrics, load_rows  # noqa: E402


def test_compute_metrics_basic():
    rows = [
        {"ts": "2026-10-07T09:35:00-04:00", "inst_id": 1, "anchor_recorded": True,
         "gate_block": False,
         "dual_anchor": {"gen_source": "planned_entry", "divergence_pct": 2.0,
                         "gen_triggered": True, "live_triggered": False,
                         "trigger_divergent": True}},
        {"ts": "2026-10-07T09:40:00-04:00", "inst_id": 2, "anchor_recorded": True,
         "gate_block": True,
         "dual_anchor": {"gen_source": "missing", "divergence_pct": 0.0,
                         "gen_triggered": False, "live_triggered": False,
                         "trigger_divergent": False}},
        {"ts": "2026-10-08T09:35:00-04:00", "inst_id": 1, "anchor_recorded": True,
         "gate_block": False,
         "dual_anchor": {"gen_source": "planned_entry", "divergence_pct": 6.0,
                         "gen_triggered": True, "live_triggered": True,
                         "trigger_divergent": False}},
        {"ts": "2026-10-08T09:40:00-04:00", "inst_id": 3, "anchor_recorded": False,
         "gate_block": True, "dual_anchor": {"gen_source": "planned_entry"}},
    ]
    m = compute_metrics(rows)
    assert m["total_rows"] == 4
    assert m["anchor_rows"] == 3
    assert m["distinct_days"] == 2
    assert m["distinct_inst_ids"] == 2
    assert m["missing"] == 1
    assert m["missing_rate"] == 0.3333
    assert m["divergence"]["n"] == 3
    assert m["divergence"]["median_pct"] == 2.0
    assert m["divergence"]["ge_5pct"] == 1
    assert m["trigger_divergent"] == 1
    assert m["trigger_divergent_rate"] == 0.3333
    assert m["gate_block"]["all_rate"] == 0.3333
    assert m["gate_block"]["divergent_rate"] == 0.0
    assert m["gate_block"]["non_divergent_rate"] == 0.5


def test_load_rows_skips_bad_json(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text(json.dumps({"anchor_recorded": True}) + "\nnot-json\n", encoding="utf-8")
    assert len(load_rows(p)) == 1
