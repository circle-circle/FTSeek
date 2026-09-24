"""Tests for rth_fade_shadow borrow fee estimation (RFC-060 净额口径).

Covers:
- estimate_borrow_fee_usd: SHORT vs LONG, parameterization, min_days override
- Idempotency: same inputs → same borrow_fee_est (no side effects)
- Schema: new fields present, LONG leg borrow_fee = 0 but field exists
- Graduation check: _net_usd prefers pnl_net_after_borrow, falls back to net_usd
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# estimate_borrow_fee_usd (from rth_fade_shadow)
# ---------------------------------------------------------------------------

class TestEstimateBorrowFeeUsd:
    """Test the borrow fee estimation helper in rth_fade_shadow."""

    def _import_fn(self, rate_pct=1.0, basis=365, min_days=0):
        """Import with env overrides (re-import safe via env vars)."""
        os.environ["BORROW_FEE_RATE_PCT"] = str(rate_pct)
        os.environ["BORROW_FEE_BASIS"] = str(basis)
        os.environ["BORROW_FEE_MIN_DAYS"] = str(min_days)
        # Force re-read of module-level constants by importing fresh
        import importlib
        import scripts.rth_fade_shadow as mod
        importlib.reload(mod)
        return mod.estimate_borrow_fee_usd

    def test_short_intraday_default_zero(self):
        """RTH fade intraday (6.5h) → int(0.27 days) = 0 → borrow fee $0."""
        fn = self._import_fn(rate_pct=1.0, min_days=0)
        result = fn(5000.0, 100.0, "SHORT", 6.5)
        assert result["borrow_fee_est"] == 0.0
        assert result["borrow_fee_rate_used"] == 1.0
        assert result["borrow_fee_hold_days"] == pytest.approx(6.5 / 24, abs=0.01)
        assert result["borrow_fee_min_days_applied"] is False

    def test_short_overnight_1day(self):
        """Hold >24h → int(days) >=1 → non-zero borrow fee."""
        fn = self._import_fn(rate_pct=10.0, basis=365, min_days=0)
        # 30 hours = 1.25 days → int = 1
        result = fn(5000.0, 100.0, "SHORT", 30.0)
        # qty = 50, fee = 50 * 100 * (10/100) * 1 / 365 = 1.3699
        expected = round(50 * 100 * 0.10 * 1 / 365, 4)
        assert result["borrow_fee_est"] == pytest.approx(expected, abs=0.01)
        assert result["borrow_fee_est"] > 0

    def test_long_direction_zero_fee(self):
        """LONG leg: borrow fee always $0 but field present (schema consistency)."""
        fn = self._import_fn(rate_pct=10.0)
        result = fn(5000.0, 100.0, "LONG", 48.0)
        assert result["borrow_fee_est"] == 0.0
        assert result["borrow_fee_rate_used"] is None  # LONG: no rate applicable
        assert result["borrow_fee_hold_days"] == 0

    def test_min_days_override(self):
        """BORROW_FEE_MIN_DAYS=1 forces 1-day charge even for intraday."""
        fn = self._import_fn(rate_pct=10.0, basis=365, min_days=1)
        result = fn(5000.0, 100.0, "SHORT", 6.5)
        # int(max(0.27, 1.0)) = 1 → fee = 50*100*0.10*1/365 = 1.3699
        expected = round(50 * 100 * 0.10 * 1 / 365, 4)
        assert result["borrow_fee_est"] == pytest.approx(expected, abs=0.01)
        assert result["borrow_fee_min_days_applied"] is True

    def test_zero_rate_zero_fee(self):
        """Zero rate → zero fee."""
        fn = self._import_fn(rate_pct=0.0, min_days=1)
        result = fn(5000.0, 100.0, "SHORT", 48.0)
        assert result["borrow_fee_est"] == 0.0

    def test_idempotency(self):
        """Same inputs → same output (no side effects, no randomness)."""
        fn = self._import_fn(rate_pct=5.0, basis=365, min_days=1)
        r1 = fn(5000.0, 100.0, "SHORT", 6.5)
        r2 = fn(5000.0, 100.0, "SHORT", 6.5)
        assert r1 == r2

    def test_htb_rate_sensitivity(self):
        """HTB rate (50%) produces materially higher fee for overnight holds."""
        fn = self._import_fn(rate_pct=50.0, basis=365, min_days=1)
        result = fn(5000.0, 100.0, "SHORT", 6.5)
        # 50*100*0.50*1/365 = 6.85
        assert result["borrow_fee_est"] == pytest.approx(6.85, abs=0.01)


# ---------------------------------------------------------------------------
# Record schema: new fields present in every record
# ---------------------------------------------------------------------------

class TestRecordSchema:
    """Verify new RFC-060 fields exist and LONG leg gets 0."""

    def test_short_record_has_borrow_fields(self):
        """Simulate what rth_fade_shadow appends for a SHORT trade."""
        os.environ["BORROW_FEE_RATE_PCT"] = "1.0"
        os.environ["BORROW_FEE_MIN_DAYS"] = "0"
        import importlib
        import scripts.rth_fade_shadow as mod
        importlib.reload(mod)

        bf = mod.estimate_borrow_fee_usd(5000.0, 100.0, "SHORT", 6.5)
        rec = {
            "borrow_fee_est": bf["borrow_fee_est"],
            "borrow_fee_rate_used": bf["borrow_fee_rate_used"],
            "borrow_fee_hold_days": bf["borrow_fee_hold_days"],
            "borrow_fee_min_days_applied": bf["borrow_fee_min_days_applied"],
            "pnl_net_after_borrow": 25.0 - bf["borrow_fee_est"],
        }
        for field in ("borrow_fee_est", "borrow_fee_rate_used",
                      "borrow_fee_hold_days", "borrow_fee_min_days_applied",
                      "pnl_net_after_borrow"):
            assert field in rec, f"missing field: {field}"

    def test_long_record_schema_consistent(self):
        """LONG record: borrow fields present but fee = 0."""
        os.environ["BORROW_FEE_RATE_PCT"] = "10.0"
        os.environ["BORROW_FEE_MIN_DAYS"] = "1"
        import importlib
        import scripts.rth_fade_shadow as mod
        importlib.reload(mod)

        bf = mod.estimate_borrow_fee_usd(5000.0, 100.0, "LONG", 48.0)
        assert bf["borrow_fee_est"] == 0.0
        rec = {
            "borrow_fee_est": bf["borrow_fee_est"],
            "pnl_net_after_borrow": 25.0 - bf["borrow_fee_est"],
        }
        assert rec["borrow_fee_est"] == 0.0
        assert rec["pnl_net_after_borrow"] == 25.0


# ---------------------------------------------------------------------------
# Graduation check: _net_usd prefers new field, falls back gracefully
# ---------------------------------------------------------------------------

class TestGraduationNetUsd:
    """Test _net_usd in rth_fade_graduation_check."""

    def _import_gcheck(self):
        import importlib
        import scripts.rth_fade_graduation_check as mod
        importlib.reload(mod)
        return mod

    def test_prefers_pnl_net_after_borrow(self):
        mod = self._import_gcheck()
        rec = {"net_usd": 30.0, "pnl_net_after_borrow": 25.0}
        assert mod._net_usd(rec) == 25.0

    def test_fallback_to_net_usd(self):
        mod = self._import_gcheck()
        rec = {"net_usd": 30.0}  # old schema, no borrow field
        assert mod._net_usd(rec) == 30.0

    def test_missing_both_returns_zero(self):
        mod = self._import_gcheck()
        rec = {}
        assert mod._net_usd(rec) == 0.0

    def test_pnl_net_after_borrow_none_falls_back(self):
        mod = self._import_gcheck()
        rec = {"net_usd": 30.0, "pnl_net_after_borrow": None}
        assert mod._net_usd(rec) == 30.0
