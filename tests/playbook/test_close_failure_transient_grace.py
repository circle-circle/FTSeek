"""瞬态 pending 与终态平仓失败的分流守卫 (2026-09-14 复核方修复批)

病灶与实证见 app/playbook/pipeline.py::_alert_close_failure 头注释
(US.FCX intent 959d5f30c267: 平仓成功净 +$157.77, 却在 34s 回报窗内被误报
 "仓位可能卡住/无止损保护" 飞书 HIGH; 同类误报 9/11 20 条 / 9/12 12 条 / 9/14 6 条)。

本文件钉住 4 条不可回归的语义:
  ① 瞬态 *_pending 首次 → 只记 INFO, 不升级 ERROR, 不推飞书
  ② 瞬态 *_pending 超宽限窗 → 升级 ERROR + 推飞书 (真卡单仍必须可见)
  ③ 终态 error (identity_conflict 等) → **零延迟**立即 ERROR + 推送
     (这条是安全底线: 9/11 事故根因 exit_identity_required 不带 _pending,
      宽限分流绝不能延迟它 — 宁可多告不可漏告)
  ④ error 为空 / 未传 error= → 按终态处理 (向后兼容未改的调用点)
"""
import importlib
import time

import pytest


@pytest.fixture()
def pipeline(monkeypatch):
    """每次给一份干净的模块级去重状态 (跨用例不串味)。"""
    from app.playbook import pipeline as mod

    importlib.reload(mod)
    mod._close_fail_alerted.clear()
    mod._close_fail_first_seen.clear()

    pushed = []
    monkeypatch.setattr(mod.logger, "error", lambda *a, **k: pushed.append(("error", a[0])))
    monkeypatch.setattr(mod.logger, "info", lambda *a, **k: pushed.append(("info", a[0])))

    class _Hub:
        def push_text(self, text, level=None):
            pushed.append(("push", text, level))

    import app.push as push_mod
    monkeypatch.setattr(push_mod, "get_push_hub", lambda: _Hub())
    mod._TEST_pushed = pushed
    yield mod
    mod._close_fail_alerted.clear()
    mod._close_fail_first_seen.clear()


def _errors(pushed):
    return [e[1] for e in pushed if e[0] == "error"]


def _pushes(pushed):
    return [e[1] for e in pushed if e[0] == "push"]


def _infos(pushed):
    return [e[1] for e in pushed if e[0] == "info"]


class TestTransientPendingGrace:
    """① 瞬态 pending 在宽限窗内不升级"""

    @pytest.mark.parametrize("err", [
        "exit_order_evidence_pending",      # 本次生产实证的那个
        "exit_order_pending",
        "exit_evidence_pending",
        "exit_fill_price_pending",
        "exit_cancel_pending",
        "exit_position_evidence_pending",
        "exit_reconciliation_pending",
        "exit_legacy_order_evidence_pending",
    ])
    def test_first_pending_is_info_only_no_push(self, pipeline, err):
        """全部 8 个 *_pending error 首次都只记 INFO, 零 ERROR 零飞书"""
        pipeline._alert_close_failure("US.FCX", "sim", "EXIT_HELD",
                                      "intent=abc " + err, error=err)
        p = pipeline._TEST_pushed
        assert _pushes(p) == [], f"{err} 首次不该推飞书"
        assert _errors(p) == [], f"{err} 首次不该记 ERROR"
        assert len(_infos(p)) == 1 and "平仓待回报" in _infos(p)[0]

    def test_repeated_pending_within_grace_stays_quiet(self, pipeline, monkeypatch):
        """快环 10s 一轮, 34s 回报窗内 3 轮命中 = 本次事故形态 → 全程静默"""
        err = "exit_order_evidence_pending"
        base = 1_000_000.0
        for k in range(3):                                    # 21:33:12 / :22 / :32
            monkeypatch.setattr(pipeline.time, "time", lambda b=base, k=k: b + 10 * k)
            pipeline._alert_close_failure("US.FCX", "sim", "EXIT_HELD", "intent=abc " + err,
                                          error=err)
        p = pipeline._TEST_pushed
        assert _pushes(p) == [] and _errors(p) == []
        assert len(_infos(p)) == 3, "三轮各记一条 INFO (可观测但不告警)"

    def test_pending_beyond_grace_escalates(self, pipeline, monkeypatch):
        """② 真卡单: pending 持续超宽限窗 → 必须升级 ERROR + 飞书"""
        err = "exit_order_evidence_pending"
        base = 1_000_000.0
        monkeypatch.setattr(pipeline.time, "time", lambda: base)
        pipeline._alert_close_failure("US.XYZ", "sim", "EXIT_HELD", "intent=x " + err, error=err)
        assert _pushes(pipeline._TEST_pushed) == []

        monkeypatch.setattr(pipeline.time, "time",
                            lambda: base + pipeline._TRANSIENT_PENDING_GRACE_S + 1)
        pipeline._alert_close_failure("US.XYZ", "sim", "EXIT_HELD", "intent=x " + err, error=err)
        p = pipeline._TEST_pushed
        assert len(_errors(p)) == 1, "超窗必须升级 ERROR"
        assert len(_pushes(p)) == 1, "超窗必须推飞书"
        assert "真卡单" in _errors(p)[0]


class TestTerminalZeroDelay:
    """③ 安全底线: 终态失败零延迟告警 (宽限分流绝不能吃掉真事故)"""

    @pytest.mark.parametrize("err", [
        "exit_identity_required",            # 9/11 P0 事故根因
        "exit_order_identity_conflict",
        "exit_identity_key_mismatch",
        "exit_terminal_shortfall",
        "exit_account_mismatch",
        "exit_fill_amount_regression",
        "exit_ledger_error",
        "exit_order_parameters_invalid",
    ])
    def test_terminal_error_alerts_immediately(self, pipeline, err):
        """非 _pending 的终态 error 首次即 ERROR + 飞书, 无任何宽限"""
        pipeline._alert_close_failure("US.ABC", "sim", "EXIT_HELD", "intent=z " + err,
                                      error=err)
        p = pipeline._TEST_pushed
        assert len(_errors(p)) == 1, f"{err} 属终态, 必须立即 ERROR"
        assert len(_pushes(p)) == 1, f"{err} 属终态, 必须立即推飞书"
        assert _infos(p) == [], "终态不走宽限路径"

    def test_empty_error_treated_as_terminal(self, pipeline):
        """④ error 为空 = 未知 → 保守按终态处理 (宁可多告)"""
        pipeline._alert_close_failure("US.ABC", "sim", "EXIT_HELD", "intent=z", error="")
        p = pipeline._TEST_pushed
        assert len(_errors(p)) == 1 and len(_pushes(p)) == 1

    def test_omitted_error_kwarg_backward_compatible(self, pipeline):
        """④ 未传 error= 的调用点 (EXIT_ROW_ERROR / BROKER_EXCEPTION) 行为逐字不变"""
        pipeline._alert_close_failure("US.ABC", "sim", "EXIT_ROW_ERROR", "trade_id=9 boom")
        p = pipeline._TEST_pushed
        assert len(_errors(p)) == 1 and len(_pushes(p)) == 1
        assert _infos(p) == []

    def test_terminal_reason_without_error_still_pushes(self, pipeline):
        """异常路径 (BROKER_EXCEPTION / EXIT_RECOVERY_ERROR) 不传 error 也必须告"""
        for reason in ("BROKER_EXCEPTION", "EXIT_RECOVERY_ERROR", "CLOSE_FAILED"):
            pipeline._close_fail_alerted.clear()
            pipeline._alert_close_failure("US.QQ", "sim", reason, "detail")
        assert len(_pushes(pipeline._TEST_pushed)) == 3


class TestDedupAndHygiene:
    """去重键与长跑卫生不因本次改动回归"""

    def test_same_symbol_day_reason_pushed_once(self, pipeline):
        """终态重复命中的 ERROR 与飞书都按 symbol+day+reason 去重"""
        for _ in range(4):
            pipeline._alert_close_failure("US.DUP", "sim", "EXIT_HELD", "d",
                                          error="exit_identity_required")
        assert len(_pushes(pipeline._TEST_pushed)) == 1
        assert len(_errors(pipeline._TEST_pushed)) == 1

    def test_never_raises(self, pipeline, monkeypatch):
        """永不抛异常 (record-only 契约): 推送炸了也不影响平仓主流程"""
        import app.push as push_mod

        def _boom():
            raise RuntimeError("push hub down")

        monkeypatch.setattr(push_mod, "get_push_hub", _boom)
        pipeline._alert_close_failure("US.BOOM", "sim", "EXIT_HELD", "d",
                                      error="exit_identity_required")   # 不应抛

    def test_first_seen_map_bounded(self, pipeline, monkeypatch):
        """长跑卫生: _close_fail_first_seen 不无界增长 (跨日残留被清)"""
        monkeypatch.setattr(pipeline.time, "time", lambda: 1_000_000.0)
        for i in range(600):
            pipeline._close_fail_first_seen[f"US.S{i}:2026-01-01:EXIT_HELD"] = 1.0
        pipeline._alert_close_failure("US.NEW", "sim", "EXIT_HELD", "d",
                                      error="exit_order_pending")
        stale = [k for k in pipeline._close_fail_first_seen if "2026-01-01" in k]
        assert not stale, "非今日残留键应被清掉"
