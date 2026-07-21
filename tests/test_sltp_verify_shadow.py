"""L1 SL/TP 验证影子单测 — 纯逻辑 + record-only 安全性。"""
import app.playbook.sltp_verify_shadow as sv


def test_compute_agree_within_tolerance():
    r = sv.compute_sltp_divergence(entry=100, llm_sl=98, llm_tp=104, code_sl=98.05, code_tp=104.1)
    assert r["sl_agree"] is True   # 0.05% ≤ 0.2%
    assert r["tp_agree"] is True   # 0.1%  ≤ 0.3%
    assert r["llm_sl_missing"] is False
    assert r["llm_tp_missing"] is False


def test_compute_diverge_sl():
    r = sv.compute_sltp_divergence(entry=100, llm_sl=95, llm_tp=110, code_sl=98, code_tp=110)
    assert r["sl_div_pct"] == 3.0
    assert r["sl_agree"] is False
    assert r["tp_agree"] is True


def test_compute_missing_llm_sl():
    r = sv.compute_sltp_divergence(entry=100, llm_sl=0, llm_tp=0, code_sl=98, code_tp=104)
    assert r["llm_sl_missing"] is True
    assert r["llm_tp_missing"] is True
    assert r["sl_agree"] is None
    assert r["sl_div_pct"] is None


def test_compute_zero_entry_no_divergence():
    r = sv.compute_sltp_divergence(entry=0, llm_sl=98, llm_tp=104, code_sl=98, code_tp=104)
    assert r["sl_agree"] is None
    assert r["tp_agree"] is None


def test_record_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(sv, "is_enabled", lambda f: False)
    calls = []
    monkeypatch.setattr(sv, "_append_jsonl", lambda rec: calls.append(rec))
    out = sv.record_sltp_shadow(entry=100, llm_sl=98, code_sl=97, symbol="X")
    assert out is None
    assert calls == []


def test_record_force_bypasses_flag_and_writes(monkeypatch):
    monkeypatch.setattr(sv, "is_enabled", lambda f: False)
    calls = []
    monkeypatch.setattr(sv, "_append_jsonl", lambda rec: calls.append(rec))
    out = sv.record_sltp_shadow(
        inst_id=1, symbol="AAPL", direction="BUY", entry=100,
        llm_sl=98, llm_tp=104, code_sl=97, code_tp=105,
        sl_pct=3.0, bucket="medium", atr_pct=0.03, regime="RISK_ON", force=True,
    )
    assert out is not None
    assert len(calls) == 1
    assert out["symbol"] == "AAPL"
    assert out["bucket"] == "medium"
    assert out["sl_div_pct"] == 1.0
    assert out["sl_agree"] is False


def test_record_never_raises_on_write_error(monkeypatch):
    def boom(_rec):
        raise RuntimeError("disk full")
    monkeypatch.setattr(sv, "_append_jsonl", boom)
    out = sv.record_sltp_shadow(entry=100, llm_sl=98, code_sl=97, force=True)
    assert out is None  # 异常被吞, 不影响调用方
