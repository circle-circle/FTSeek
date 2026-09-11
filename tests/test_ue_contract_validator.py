"""U4 契约校验器单测 — 违约检测逻辑 (不触 LLM, mock call_llm)."""
import hashlib
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.llm_universe_scan import (  # noqa: E402
    PROMPT_HASH_V2,
    PROMPT_VERSION,
    SYSTEM_PROMPT_V2,
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


class TestPromptVersionContract:
    def test_version_and_hash_derive_from_current_prompt(self):
        # v2.11 (2026-09-11): 负反馈表 U1 反号修 + U1b 零收益三态, 均在 brief/user 侧。
        # 升版者必须同时改这里 —— 这条钉子变红是它的设计意图, 不是回归。
        assert PROMPT_VERSION == "v2.11"
        expected = hashlib.sha1(SYSTEM_PROMPT_V2.encode("utf-8")).hexdigest()[:12]
        assert PROMPT_HASH_V2 == expected

    def test_brief_only_bump_leaves_hash_identical_to_v210(self):
        """v2.10 → v2.11 未动 SYSTEM_PROMPT_V2 ⇒ 哈希必须**完全不变**。这是盲区, 不是 bug。

        实测: v2.10 与 v2.11 的 `PROMPT_HASH_V2` 同为 `70e4680597af`、`SYSTEM_PROMPT_V2`
        同为 2582 字符 (v2.10 值见 `docs/analysis/2026-09-09-core-chain-brief-for-claude.md`
        §Prompt 版本锚)。⇒ 任何按 `prompt_hash` 切分样本的口径都**无法区分这两个行为版本**,
        例如 `docs/CONTROL-LOOP-LEDGER.md` thinking-eval 预注册判据「跨 prompt_hash 不得混合」
        在 9/11 这次 brief 变更上**不会触发序列归零**。只能按 `prompt_version` 切分。
        指纹扩到 brief 侧属另立项 (DEB-20260910 §11.6-6, 用户裁「缺口另立项」), 本钉不代其实现。
        """
        assert PROMPT_HASH_V2 == "70e4680597af", (
            "SYSTEM_PROMPT_V2 被改动了 — 若属有意, 需同时升 PROMPT_VERSION 并更新本钉与 "
            "DEB-20260910 §11.6-6 的盲区记录; 若属无意, 即回归")
        assert len(SYSTEM_PROMPT_V2) == 2582

    def test_prompt_and_parser_share_trigger_metrics(self):
        expected_metrics = {"收盘价", "量比", "5日涨跌", "盘前涨跌幅"}
        for metric in expected_metrics:
            assert metric in SYSTEM_PROMPT_V2
        assert "当日累计成交量÷最近20个完整交易日平均成交量" in SYSTEM_PROMPT_V2

        raw = [{
            "symbol": "TEST",
            "direction": "LONG",
            "confidence": 0.5,
            "invalidation_triggers": [
                {"metric": metric, "operator": "<", "value": 1}
                for metric in sorted(expected_metrics)
            ] + [{"metric": "成交量", "operator": ">", "value": 1000000}],
        }]
        out = _normalize_surfaced(raw, with_tier=True)
        parsed_metrics = {t["metric"] for t in out[0]["invalidation_triggers"]}

        assert parsed_metrics == expected_metrics
        assert "成交量" not in parsed_metrics
