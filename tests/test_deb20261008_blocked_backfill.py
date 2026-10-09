"""DEB-20261008 F4 被拦行结局回填 — 方向取证与口径契约测试。"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.deb20261008_blocked_outcome_backfill import (  # noqa: E402
    _compute_fills,
    _recover_direction,
    _sign,
    _write_provenance,
)


class TestDirectionRecovery:
    def test_g4_majority_parse_buy(self):
        row = {"skip_reason": "方向偏斜超限 guardrail 降级 (多数方 BUY, 原size=88/564)",
               "date": "2026-10-07", "symbol": "US.NOK"}
        direction, source = _recover_direction(row)
        assert direction == "BUY"
        assert source == "skip_reason:majority"

    def test_g4_majority_parse_short(self):
        row = {"skip_reason": "方向偏斜超限 guardrail 降级 (多数方 SELL_SHORT, 原size=1/2)",
               "date": "2026-10-07", "symbol": "US.XYZ"}
        direction, _ = _recover_direction(row)
        assert direction == "SELL_SHORT"

    def test_g6_falls_back_to_checkpoint(self, monkeypatch, tmp_path):
        ckpt_dir = tmp_path / "data" / "da_checkpoint" / "2026-10-07"
        ckpt_dir.mkdir(parents=True)
        (ckpt_dir / "US.MRVL.json").write_text(json.dumps({
            "_llm_output": {"direction": "SELL_SHORT"}}), encoding="utf-8")
        monkeypatch.setattr(
            "scripts.deb20261008_blocked_outcome_backfill.PROJECT_ROOT", tmp_path)
        row = {"skip_reason": "sector 集中度 guardrail 降级 (Semiconductors)",
               "date": "2026-10-07", "symbol": "US.MRVL"}
        direction, source = _recover_direction(row)
        assert direction == "SELL_SHORT"
        assert source == "da_checkpoint"

    def test_unrecoverable_is_honest(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "scripts.deb20261008_blocked_outcome_backfill.PROJECT_ROOT", tmp_path)
        row = {"skip_reason": "sector 集中度 guardrail 降级 (X)",
               "date": "2026-10-07", "symbol": "US.NOPE"}
        direction, source = _recover_direction(row)
        assert direction is None
        assert source == "unrecoverable"


class TestFillMath:
    BARS = {
        "2026-10-07": {"time": "2026-10-07", "open": 100.0, "close": 104.0},
        "2026-10-08": {"time": "2026-10-08", "open": 104.0, "close": 98.0},
    }

    def test_long_intraday_and_t1(self):
        fills = _compute_fills("BUY", 100.0, "2026-10-07", self.BARS)
        assert fills["fwd_return_intraday"] == pytest.approx(0.04)
        assert fills["fwd_return_intraday_open"] == pytest.approx(0.04)
        # T+1 close=98: entry 锚 98/100-1=-2%; open 锚 98/100-1=-2%
        assert fills["fwd_return_t1"] == pytest.approx(-0.02)

    def test_short_sign_flip(self):
        fills = _compute_fills("SELL_SHORT", 100.0, "2026-10-07", self.BARS)
        assert fills["fwd_return_intraday"] == pytest.approx(-0.04)
        assert fills["fwd_return_t1"] == pytest.approx(0.02)

    def test_missing_day0_yields_no_intraday(self):
        fills = _compute_fills("BUY", 100.0, "2026-10-05", self.BARS)
        assert "fwd_return_intraday" not in fills


class TestSign:
    def test_sign_mapping(self):
        assert _sign("BUY") == 1
        assert _sign("LONG") == 1
        assert _sign("SELL_SHORT") == -1
        assert _sign("SHORT") == -1


class TestProvenance:
    def test_dry_run_never_writes_or_overwrites(self, tmp_path):
        path = tmp_path / "provenance.jsonl"
        existing = json.dumps({"id": 1, "status": "filled"}) + "\n"
        path.write_text(existing, encoding="utf-8")

        _write_provenance([{"id": 1, "status": "dry"}], path, dry=True)

        assert path.read_text(encoding="utf-8") == existing
