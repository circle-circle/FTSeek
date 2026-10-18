"""财报日盘前影子: 研究臂专用源/涨幅带/漏斗契约."""
import json

import scripts.extended_hours_entry_executor as mod


def test_research_chg_band_is_independent(monkeypatch):
    monkeypatch.setattr(mod, "RESEARCH_CHG_BAND_RAW", "2.0,10.0")
    assert mod._research_chg_ok(2.5) is True
    assert mod._research_chg_ok(10.0) is True
    assert mod._research_chg_ok(1.0) is False
    assert mod._research_chg_ok(11.0) is False


def test_research_sources_are_research_only_and_prefiltered(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "RESEARCH_ARM_ON", True)
    monkeypatch.setattr(mod, "RESEARCH_SOURCES_ON", True)
    monkeypatch.setattr(mod, "RESEARCH_CHG_BAND_RAW", "2.0,10.0")
    monkeypatch.setattr(mod, "RESEARCH_MIN_PRICE", 5.0)
    monkeypatch.setattr(mod, "RESEARCH_MAX_SYMBOLS", 80)
    monkeypatch.setattr(mod, "RESEARCH_FUNNEL", tmp_path / "funnel.jsonl")
    monkeypatch.setattr(mod, "_research_arm_active", lambda: True)
    monkeypatch.setattr(mod, "_beat_rank_symbols", lambda q: ["US.BEAT1", "US.BEAT2", "US.DUP"])
    monkeypatch.setattr(mod, "_watchlist_symbols", lambda q: ["US.WATCH1", "US.DUP"])

    seen = {}

    def _snapshot(session, qctx, symbols):
        seen["symbols"] = list(symbols)
        return [
            {"symbol": "US.BEAT1", "px": 10.0, "chg": 5.0, "vol": 1000.0},
            {"symbol": "US.BEAT2", "px": 3.0, "chg": 5.0, "vol": 1000.0},
            {"symbol": "US.DUP", "px": 10.0, "chg": 1.0, "vol": 1000.0},
            {"symbol": "US.WATCH1", "px": 20.0, "chg": 8.0, "vol": 1000.0},
        ]

    monkeypatch.setattr(mod, "_snapshot_candidates", _snapshot)
    out = mod._research_source_candidates(
        "premarket", object(), existing={"US.BEAT2"})
    assert seen["symbols"] == ["US.BEAT1", "US.DUP", "US.WATCH1"]
    assert [(x["symbol"], x["_source"]) for x in out] == [
        ("US.BEAT1", "research_beat"),
        ("US.WATCH1", "research_watchlist"),
    ]


def test_research_funnel_appends_jsonl(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "RESEARCH_SOURCES_ON", True)
    path = tmp_path / "funnel.jsonl"
    monkeypatch.setattr(mod, "RESEARCH_FUNNEL", path)
    item = {"symbol": "US.TEST", "_source": "research_beat",
            "px": 10.0, "chg": 5.0, "vol": 1000.0}
    mod._record_research_funnel("premarket", "2026-10-16", item, "rejected", "chg")
    row = json.loads(path.read_text().strip())
    assert row["symbol"] == "US.TEST"
    assert row["source"] == "research_beat"
    assert row["stage"] == "rejected"
    assert row["gate_mode"] == mod.RESEARCH_GATE_MODE
