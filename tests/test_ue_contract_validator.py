"""U4 契约校验器单测 — 违约检测逻辑 (不触 LLM, mock call_llm)."""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.llm_universe_scan import (  # noqa: E402
    _normalize_surfaced,
    _validate_and_repair_surfaced,
)


def _mk(direction="LONG", invalidation="跌破支撑", skip_reason="", short_check=None):
    s = {"symbol": "TEST", "direction": direction,
         "invalidation": invalidation, "skip_reason": skip_reason}
    if direction == "SHORT":
        s["short_check"] = short_check or {"provided": True,
                                           "downside_catalyst_expiry_hours": 8,
                                           "squeeze_risk": 2}
    return s


class TestViolationDetection:
    def test_clean_long(self):
        surfaced = [_mk("LONG", invalidation="跌破前低")]
        with mock.patch("scripts.llm_universe_scan._cfg", return_value=False):
            st = _validate_and_repair_surfaced(surfaced)
        assert st.get("clean") is True
        assert st["violations"] == []

    def test_clean_short(self):
        surfaced = [_mk("SHORT")]
        with mock.patch("scripts.llm_universe_scan._cfg", return_value=False):
            st = _validate_and_repair_surfaced(surfaced)
        assert st.get("clean") is True

    def test_clean_skip_with_reason(self):
        surfaced = [_mk("SKIP", invalidation="", skip_reason="已充分反映")]
        with mock.patch("scripts.llm_universe_scan._cfg", return_value=False):
            st = _validate_and_repair_surfaced(surfaced)
        assert st.get("clean") is True

    def test_long_missing_invalidation(self):
        surfaced = [_mk("LONG", invalidation="")]
        with mock.patch("scripts.llm_universe_scan._cfg", return_value=False):
            st = _validate_and_repair_surfaced(surfaced)
        assert len(st["violations"]) == 1
        assert st["violations"][0]["missing"] == ["invalidation"]

    def test_short_missing_short_check(self):
        s = _mk("SHORT")
        s["short_check"] = {"provided": False}
        with mock.patch("scripts.llm_universe_scan._cfg", return_value=False):
            st = _validate_and_repair_surfaced([s])
        assert len(st["violations"]) == 1
        assert st["violations"][0]["missing"] == ["short_check"]

    def test_short_zero_expiry(self):
        s = _mk("SHORT", short_check={"provided": True,
                                      "downside_catalyst_expiry_hours": 0,
                                      "squeeze_risk": 1})
        with mock.patch("scripts.llm_universe_scan._cfg", return_value=False):
            st = _validate_and_repair_surfaced([s])
        assert len(st["violations"]) == 1
        assert "downside_catalyst_expiry_hours" in st["violations"][0]["missing"][0]

    def test_skip_missing_reason(self):
        surfaced = [_mk("SKIP", invalidation="", skip_reason="")]
        with mock.patch("scripts.llm_universe_scan._cfg", return_value=False):
            st = _validate_and_repair_surfaced(surfaced)
        assert len(st["violations"]) == 1
        assert st["violations"][0]["missing"] == ["skip_reason"]


class TestRepairWhitelist:
    """repair 只接受白名单字段; direction/confidence/thesis 拒收."""

    def _run_with_mock_repair(self, surfaced, repair_payload):
        fake_resp = mock.MagicMock()
        fake_resp.success = True
        fake_resp.response = repair_payload
        with mock.patch("scripts.llm_universe_scan._cfg", return_value=False), \
             mock.patch("app.llm.model_call.call_llm", return_value=fake_resp):
            return _validate_and_repair_surfaced(surfaced)

    def test_repair_invalidation_applied(self):
        s = _mk("LONG", invalidation="")
        st = self._run_with_mock_repair([s], {"repairs": [
            {"symbol": "TEST", "invalidation": "跌破前收盘价"}]})
        assert s["invalidation"] == "跌破前收盘价"
        assert st["repaired"] == 1
        assert st["still_bad"] == 0

    def test_repair_direction_patch_rejected(self):
        s = _mk("LONG", invalidation="")
        st = self._run_with_mock_repair([s], {"repairs": [
            {"symbol": "TEST", "invalidation": "跌破前收盘价", "direction": "SHORT"}]})
        # direction 补丁被拒收 (标的仍是 LONG)
        assert s["direction"] == "LONG"
        assert st["direction_patch_rejected"] == 1

    def test_repair_confidence_not_applied(self):
        s = _mk("LONG", invalidation="")
        s["confidence"] = 0.6
        st = self._run_with_mock_repair([s], {"repairs": [
            {"symbol": "TEST", "invalidation": "x", "confidence": 0.99}]})
        assert s["confidence"] == 0.6  # 白名单外, 不动

    def test_repair_short_check_applied(self):
        s = _mk("SHORT")
        s["short_check"] = {"provided": False}
        st = self._run_with_mock_repair([s], {"repairs": [
            {"symbol": "TEST", "short_check": {"borrow_feasible": True,
                                               "downside_catalyst_expiry_hours": 6,
                                               "squeeze_risk": 3}}]})
        assert s["short_check"]["provided"] is True
        assert s["short_check"]["downside_catalyst_expiry_hours"] == 6
        assert s["short_check"]["repaired"] is True

    def test_enforce_drops_unrepairable(self):
        s = _mk("LONG", invalidation="")
        good = _mk("LONG", invalidation="跌破支撑")
        good["symbol"] = "OK"
        batch = [s, good]
        fake_resp = mock.MagicMock()
        fake_resp.success = True
        fake_resp.response = {"repairs": []}  # repair 没补上
        def _cfg_enforce(key, default=None):
            return True if "enforce" in key else default
        with mock.patch("scripts.llm_universe_scan._cfg", side_effect=_cfg_enforce), \
             mock.patch("app.llm.model_call.call_llm", return_value=fake_resp):
            st = _validate_and_repair_surfaced(batch)
        assert st["dropped"] == 1
        assert st["enforce"] is True
        # 原列表被原地裁剪: 只剩合格标的
        assert [x["symbol"] for x in batch] == ["OK"]

    def test_observe_mode_never_drops(self):
        s = _mk("LONG", invalidation="")
        fake_resp = mock.MagicMock()
        fake_resp.success = True
        fake_resp.response = {"repairs": []}
        with mock.patch("scripts.llm_universe_scan._cfg", return_value=False), \
             mock.patch("app.llm.model_call.call_llm", return_value=fake_resp):
            st = _validate_and_repair_surfaced([s])
        assert st["dropped"] == 0
        assert st["still_bad"] == 1


class TestNormalizeSkipReason:
    def test_skip_reason_parsed(self):
        raw = [{"symbol": "X", "direction": "SKIP", "skip_reason": "已price-in",
                "confidence": 0.3}]
        out = _normalize_surfaced(raw, with_tier=False)
        assert out[0]["skip_reason"] == "已price-in"

    def test_skip_reason_truncated(self):
        raw = [{"symbol": "X", "direction": "SKIP", "skip_reason": "a" * 200,
                "confidence": 0.3}]
        out = _normalize_surfaced(raw, with_tier=False)
        assert len(out[0]["skip_reason"]) <= 120
