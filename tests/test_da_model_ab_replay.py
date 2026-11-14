#!/usr/bin/env python3
"""Offline tests for DA model A/B replay calibration and quality metrics."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import da_model_ab_replay as replay


def _card(direction="BUY", entry=100, stop=95, target=110):
    return {
        "direction": direction,
        "executive_summary": "event -> transmission -> plan",
        "causal_chain": "event -> peer -> revenue",
        "bull_case": [{"thesis": "bull", "evidence": "x"}],
        "bear_case": [{"thesis": "bear", "evidence": "y"}],
        "execution_plan": {
            "entry_price": entry,
            "stop_loss_price": stop,
            "take_profit_price": target,
        },
    }


def test_production_budget_is_read_instead_of_stale_3000(monkeypatch):
    fake = SimpleNamespace(
        DA_THINKING_ENABLED=True,
        DA_THINKING_BUDGET=8192,
        LLM_MODEL="qwen3.7-max",
        LLM_TEMPERATURE=0.0,
    )
    monkeypatch.setitem(sys.modules, "config", fake)
    settings = replay.production_call_settings()
    assert settings["max_tokens"] == 16192
    assert settings["thinking"] is True
    assert replay.model_max_tokens("qwen3.7-max") == 16192
    assert replay.model_max_tokens("qwen3.7-max", thinking=False) == 3000


def test_collect_samples_freezes_date_symbol_and_prompt_hash(tmp_path, monkeypatch):
    ckpt = tmp_path / "checkpoint"
    bars = tmp_path / "bars"
    bars.mkdir()
    for day in ("2026-11-11", "2026-11-12"):
        (ckpt / day).mkdir(parents=True)
        for symbol in ("NVDA", "AMD"):
            (ckpt / day / f"US.{symbol}.raw.json").write_text(json.dumps({
                "symbol": f"US.{symbol}", "trade_date": day,
                "user_prompt": f"prompt-{day}-{symbol}",
            }))
            (bars / f"US_{symbol}_{day}.json").write_text(json.dumps([
                [f"{day} 09:30:00", 100, 101, 99, 100.5],
            ]))
    monkeypatch.setattr(replay, "CKPT", ckpt)
    monkeypatch.setattr(replay, "BARS_DIRS", (bars,))
    samples = replay.collect_samples(
        8, dates=["2026-11-12"], symbols=["NVDA"], per_day=8,
    )
    assert len(samples) == 1
    assert samples[0]["day"] == "2026-11-12"
    assert samples[0]["symbol"] == "US.NVDA"
    assert samples[0]["raw_path"].endswith("US.NVDA.raw.json")
    assert len(samples[0]["prompt_sha256"]) == 64


def test_collect_samples_can_keep_missing_bars_without_faking_returns(tmp_path, monkeypatch):
    day = "2026-11-12"
    ckpt = tmp_path / "checkpoint" / day
    ckpt.mkdir(parents=True)
    bars = tmp_path / "bars"
    bars.mkdir()
    (ckpt / "US.NBIS.raw.json").write_text(json.dumps({
        "symbol": "US.NBIS", "trade_date": day, "user_prompt": "NBIS event",
    }))
    monkeypatch.setattr(replay, "CKPT", ckpt.parent)
    monkeypatch.setattr(replay, "BARS_DIRS", (bars,))
    assert replay.collect_samples(1, dates=[day]) == []
    samples = replay.collect_samples(1, dates=[day], require_bars=False)
    assert len(samples) == 1
    assert replay.day_bars(samples[0]["symbol"], day) is None


def test_score_rejects_non_card_and_checks_research_and_buy_geometry():
    bars = [["2026-11-12 09:30:00", 99, 111, 94, 105]]
    assert replay.score({}, bars)["valid_card"] is False
    assert replay.score({"direction": "MAYBE"}, bars)["valid_card"] is False
    skip = replay.score({"direction": "SKIP"}, bars)
    assert skip["valid_card"] is True and skip["dir_rate"] == 0
    assert skip["direction_plan_consistent"] is True
    contradictory = replay.score({
        "direction": "SKIP",
        "execution_plan": {"entry_price": 165.5, "entry_archetype": "FADE_SHORT"},
    }, bars)
    assert contradictory["valid_card"] is True
    assert contradictory["direction_plan_consistent"] is False
    result = replay.score(_card(), bars)
    assert result["valid_card"] is True
    assert result["research_complete"] is True
    assert result["price_geometry_valid"] is True
    assert result["rr"] == 2.0
    assert result["reachable"] is True
    assert result["hit"] is True
    assert result["fwd"] == pytest.approx(5.0)


def test_score_checks_short_geometry_instead_of_absolute_rr_only():
    # For SHORT, target 90 < entry 100 < stop 110 is valid.
    good = replay.score(_card("SELL_SHORT", 100, 110, 90), None)
    assert good["price_geometry_valid"] is True
    assert good["rr"] == 1.0
    # BUY geometry accidentally copied into SHORT is not a valid instruction.
    bad = replay.score(_card("SELL_SHORT", 100, 95, 110), None)
    assert bad["price_geometry_valid"] is False
    assert bad["rr"] is None


def test_repeat_summary_reports_direction_instability():
    rows = []
    for model in ("qwen3.7-max", "deepseek-v4-pro"):
        for repeat, direction in enumerate(("BUY", "SKIP"), 1):
            row = {
                "model": model, "day": "2026-11-12", "symbol": "US.NVDA",
                "repeat": repeat, "ok": True, "cost_usd": 0.1, "latency": 1.0,
                "finish": "stop", **replay.score(_card(direction), None),
            }
            rows.append(row)
    summary = replay.summarize_records(rows, ["qwen3.7-max", "deepseek-v4-pro"], 2)
    for model in summary.values():
        assert model["repeat_groups"] == 1
        assert model["stable_direction_groups"] == 0
        assert model["stable_direction_rate"] == 0.0
        assert model["direction_counts"] == {"BUY": 1, "SKIP": 1}


def test_summary_distinguishes_unpriced_from_zero_cost():
    row = {
        "model": "kimi-k3.1-pro", "day": "2026-11-12", "symbol": "US.NVDA",
        "repeat": 1, "ok": True, "cost_usd": None, "latency": 1.0,
        "finish": "stop", **replay.score({"direction": "SKIP"}, None),
    }
    summary = replay.summarize_records([row], ["kimi-k3.1-pro"], 1)["kimi-k3.1-pro"]
    assert summary["cost_usd"] is None
    assert summary["cost_coverage"] == 0.0


def test_skip_contract_is_an_explicit_separate_prompt_variant():
    original = '{"direction":"SKIP"}'
    assert replay.build_prompt_variant(original, False) == original
    variant = replay.build_prompt_variant(original, True)
    assert variant.startswith(original)
    assert "必须全部为 null" in variant
    assert variant != original


def test_main_defaults_to_dry_run_without_key_or_output(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(replay, "collect_samples", lambda *a, **k: [{
        "day": "2026-11-12", "symbol": "US.NVDA", "prompt": "x",
        "raw_path": "x.raw.json", "prompt_sha256": "abc",
    }])
    monkeypatch.setattr(replay, "production_call_settings", lambda: {
        "model": "qwen3.7-max", "max_tokens": 16192,
    })
    monkeypatch.setattr(replay, "load_dashscope_key", lambda: (_ for _ in ()).throw(
        AssertionError("dry-run must not load credentials")
    ))
    monkeypatch.setattr(sys, "argv", ["da_model_ab_replay.py", "--n", "1"])
    assert replay.main() == 0
    assert "DRY-RUN" in capsys.readouterr().out
    assert list(tmp_path.glob("da_model_ab_*")) == []
