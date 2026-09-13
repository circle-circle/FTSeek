"""[DEB-20260829 第12轮·用户裁"同意"] scan 失败飞书告警契约测试。

9/1 事故: 双臂 8000 tok 截断 → scan exit 1 → cron && 链静默断, inject/push 未跑,
次日人工才发现。契约: ①LLM 失败/意外异常两分支均推飞书且返回 1 ②告警
best-effort — 发送失败不影响主链 ③午间影子消息不带"链已断"误报。
"""
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scripts.llm_universe_scan as scan_mod  # noqa: E402


@pytest.fixture
def feishu_out(monkeypatch):
    sent = []
    monkeypatch.setattr("app.notifier.feishu.send_text", lambda c: sent.append(c) or True)
    return sent


def _fail_result():
    r = mock.MagicMock()
    r.success = False
    r.error = "all_models_failed"
    return r


class TestScanFailAlert:
    def test_alert_message_evening_mentions_broken_chain(self, feishu_out):
        scan_mod._scan_fail_alert("LLM 调用失败: all_models_failed", midday=False)
        assert len(feishu_out) == 1
        msg = feishu_out[0]
        assert "UE scan 失败" in msg
        assert "inject/push 未跑" in msg
        assert "all_models_failed" in msg

    def test_alert_message_midday_no_false_chain_warning(self, feishu_out):
        # 午间影子独立运行无 && 链 — 消息不得谎报"链已断"
        scan_mod._scan_fail_alert("LLM 调用失败: x", midday=True)
        msg = feishu_out[0]
        assert "无下游" in msg
        assert "inject/push 未跑" not in msg

    def test_alert_send_failure_never_raises(self, monkeypatch, capsys):
        def _boom(content):
            raise RuntimeError("feishu down")

        monkeypatch.setattr("app.notifier.feishu.send_text", _boom)
        scan_mod._scan_fail_alert("x")  # 不得抛出
        assert "告警发送失败" in capsys.readouterr().out


class TestCmdScanFailureBranches:
    def test_llm_failure_sends_alert_and_returns_1(self, monkeypatch, feishu_out):
        monkeypatch.setattr(scan_mod, "_is_us_trading_day_today", lambda: True)

        def _stub_brief(manual=""):
            # 桩必须履行真 `_build_clue_brief` 的**状态契约**: 入口清空材料索引与指纹。
            #   否则 P2.6 空转守卫读到的是**别的测试留下的**模块态, 本例成败就取决于测试
            #   执行顺序 (索引非空 → 守卫放行 → 调到 LLM → 告警; 索引空且指纹非空 →
            #   守卫跳过 LLM → rc=0 无告警 → 本例红)。真函数把这三条 reset 放在第一句,
            #   正是为了"构建失败/被绕过也不留旧值"。
            scan_mod._BRIEF_EVIDENCE = []
            scan_mod._LAST_BRIEF_MATERIALS = []
            scan_mod._LAST_BRIEF_FINGERPRINTS = {}
            return "brief"

        monkeypatch.setattr(scan_mod, "_build_clue_brief", _stub_brief)
        monkeypatch.setattr("app.llm.model_call.call_llm", mock.Mock(return_value=_fail_result()))
        rc = scan_mod.cmd_scan()
        assert rc == 1
        assert len(feishu_out) == 1
        assert "all_models_failed" in feishu_out[0]

    @pytest.mark.parametrize(
        ("command", "args", "label"),
        [
            (scan_mod.cmd_scan_eval, ("manual clue",), "[scan-eval]"),
            (scan_mod.cmd_thinking_eval, (), "[thinking-eval]"),
        ],
    )
    def test_eval_holiday_has_zero_side_effects(
        self, monkeypatch, capsys, command, args, label
    ):
        forbidden = mock.Mock(side_effect=AssertionError("holiday guard ran too late"))
        monkeypatch.setattr(scan_mod, "_is_us_trading_day_today", lambda: False)
        monkeypatch.setattr(scan_mod, "_build_clue_brief", forbidden)
        monkeypatch.setattr(scan_mod, "_build_router", forbidden)
        monkeypatch.setattr(scan_mod, "_batch_quotes", forbidden)
        monkeypatch.setattr(scan_mod, "_append_jsonl", forbidden)
        monkeypatch.setattr("app.llm.model_call.call_llm", forbidden)

        assert command(*args) == 0
        assert label in capsys.readouterr().out
        forbidden.assert_not_called()

    def test_main_scan_exception_sends_alert(self, monkeypatch, feishu_out):
        # main() 的 scan 意外异常同样断 && 链 — 与 LLM 失败分支同告警 (第三接线点)
        def _boom(manual_clue="", midday=False):
            raise RuntimeError("unexpected crash in scan")

        monkeypatch.setattr(scan_mod, "cmd_scan", _boom)
        monkeypatch.setattr(sys, "argv", ["llm_universe_scan.py", "scan"])
        rc = scan_mod.main()
        assert rc == 1
        assert len(feishu_out) == 1
        assert "执行异常" in feishu_out[0]
        assert "inject/push 未跑" in feishu_out[0]

    def test_main_scan_exception_midday_no_chain_warning(self, monkeypatch, feishu_out):
        def _boom(manual_clue="", midday=False):
            raise RuntimeError("crash")

        monkeypatch.setattr(scan_mod, "cmd_scan", _boom)
        monkeypatch.setattr(sys, "argv", ["llm_universe_scan.py", "scan", "--midday"])
        rc = scan_mod.main()
        assert rc == 1
        assert "inject/push 未跑" not in feishu_out[0]
