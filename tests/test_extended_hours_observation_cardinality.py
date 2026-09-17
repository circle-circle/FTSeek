import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.extended_hours_observation_cardinality import collect


def test_cardinality_collect_reads_jsonl_and_declares_units(tmp_path, monkeypatch):
    import scripts.extended_hours_observation_cardinality as module

    path = tmp_path / "obs.jsonl"
    rows = []
    for index in range(30):
        rows.append({
            "trade_date": "2026-09-17", "spread_pct": index / 100,
            "quote_age_seconds": index,
        })
    path.write_text("\n".join(str(row).replace("'", '"') for row in rows))
    monkeypatch.setitem(module.SOURCES, "premarket_exit", path)
    monkeypatch.setitem(module.SOURCES, "afterhours_exit", tmp_path / "missing.jsonl")
    results = collect(30)
    spread = next(result for result in results if result["field"] == "spread_pct")
    quote_age = next(result for result in results if result["field"] == "quote_age_seconds")
    assert spread["n"] == 30
    assert spread["unique"] == 30
    assert spread["unit"] == "percent"
    assert quote_age["unit"] == "seconds"
