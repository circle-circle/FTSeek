import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.extended_hours_profit_exit_rules import (
    decide_original_profit_exit,
    prioritize_profit_exit,
    persist_profit_exit_state,
)


META = {
    "stop_loss": 95.0,
    "take_profit": 110.0,
    "conditions": [],
    "peak_price": 100.0,
}


def test_profit_exit_signal_has_priority_over_fallback():
    assert prioritize_profit_exit("TAKE_PROFIT", "pre_close_flatten") == "TAKE_PROFIT"
    assert prioritize_profit_exit("SL_BREAKEVEN", "hold_to_next_open") == "SL_BREAKEVEN"
    assert prioritize_profit_exit("SL_TRAIL_LOCK", "hold_to_next_open") == "SL_TRAIL_LOCK"
    assert prioritize_profit_exit("hold", "pre_close_flatten") == "pre_close_flatten"
    assert prioritize_profit_exit("hold") == "hold"


def test_breakeven_exits_only_after_arming_and_net_floor_breach():
    unearned = decide_original_profit_exit(
        "LONG", 100.0, 99.0, 95.0, 100.0, {**META, "peak_price": 100.0}, 0.15
    )
    assert unearned["action"] == "hold_no_stop"

    armed = decide_original_profit_exit(
        "LONG", 100.0, 100.8, 95.0, 101.0, {**META, "peak_price": 101.0}, 0.15
    )
    assert armed["action"] == "hold"
    assert armed["breakeven_triggered"] is True

    breached = decide_original_profit_exit(
        "LONG", 100.0, 100.3, 95.0, 101.0,
        {**armed["metadata"], "peak_price": 101.0}, 0.15,
    )
    assert breached["action"] == "SL_BREAKEVEN"
    assert breached["metadata"]["_be_net_line_price"] == pytest.approx(100.15)


def test_trail_lock_ratchets_from_favorable_peak():
    decision = decide_original_profit_exit(
        "LONG", 100.0, 102.0, 95.0, 103.0,
        {**META, "peak_price": 103.0, "breakeven_triggered": True}, 0.15,
    )
    assert decision["action"] == "SL_TRAIL_LOCK"
    assert decision["effective_sl"] == pytest.approx(102.176, abs=0.001)
    assert decision["peak_profit_pct"] == pytest.approx(3.0)


def test_take_profit_uses_original_rr_gate_and_executable_price():
    decision = decide_original_profit_exit(
        "LONG", 100.0, 110.2, 95.0, 110.0, META, 0.15
    )
    assert decision["action"] == "TAKE_PROFIT"
    assert decision["take_profit_rr"] == pytest.approx(2.0)

    low_rr = decide_original_profit_exit(
        "LONG", 100.0, 110.2, 109.0, 110.0,
        {**META, "stop_loss": 109.0}, 0.15,
    )
    assert low_rr["action"] == "hold"
    assert low_rr["detail"] == "take_profit_rr_gate"


def test_take_profit_rejects_gross_profit_below_round_trip_fee():
    decision = decide_original_profit_exit(
        "LONG", 100.0, 100.2, 95.0, 100.0,
        {**META, "stop_loss": 99.9, "take_profit": 100.2}, 0.25,
    )
    assert decision["action"] == "hold"
    assert decision["detail"] == "take_profit_below_net_fee"
    assert decision["net_profit_pct"] == pytest.approx(-0.05)


def test_short_uses_ask_and_favorable_low_peak():
    decision = decide_original_profit_exit(
        "SHORT", 100.0, 97.7, 105.0, 97.0,
        {**META, "stop_loss": 105.0, "take_profit": 90.0, "peak_price": 97.0},
        0.15,
    )
    assert decision["action"] == "hold"
    assert decision["peak_price"] == pytest.approx(97.0)
    assert decision["peak_profit_pct"] == pytest.approx(3.0)


def test_peak_state_persists_without_overwriting_unrelated_metadata(tmp_path):
    db_path = tmp_path / "trades.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE trade_records (id INTEGER PRIMARY KEY, metadata_json TEXT)"
    )
    conn.execute(
        "INSERT INTO trade_records VALUES (1, ?)",
        (json.dumps({"unrelated": "keep", "peak_price": 100.0}),),
    )
    conn.commit()
    conn.close()

    decision = decide_original_profit_exit(
        "LONG", 100.0, 103.0, 95.0, 100.0,
        {"stop_loss": 95.0, "peak_price": 100.0}, 0.15,
    )
    assert persist_profit_exit_state(db_path, 1, decision, "LONG")

    conn = sqlite3.connect(db_path)
    metadata = json.loads(conn.execute(
        "SELECT metadata_json FROM trade_records WHERE id=1"
    ).fetchone()[0])
    conn.close()
    assert metadata["unrelated"] == "keep"
    assert metadata["peak_price"] == pytest.approx(103.0)
    assert metadata["breakeven_triggered"] is True


# ── 2026-09-19 确定性落袋锁 (SL_CERT_LOCK): 用户令「首要=每笔净收益不亏损」 ──

def test_cert_lock_off_by_default_keeps_behaviour(monkeypatch):
    """env 未设 ⇒ 关闭 (生产默认行为不变): +3% 浮盈仍只是 hold。"""
    monkeypatch.delenv("EXT_CERT_LOCK_PCT", raising=False)
    d = decide_original_profit_exit(
        "LONG", 100.0, 103.5, 95.0, 103.5,
        {"stop_loss": 95.0, "peak_price": 103.5}, 0.15,
    )
    assert d["action"] == "hold"
    assert d["cert_lock_pct"] is None


def test_cert_lock_fires_at_line_without_rr_gate(monkeypatch):
    """env=3.0 ⇒ 触及 +3% 即 SL_CERT_LOCK; 初始 SL -5% 的 RR=0.6 不得拦死它。"""
    monkeypatch.setenv("EXT_CERT_LOCK_PCT", "3.0")
    d = decide_original_profit_exit(
        "LONG", 100.0, 103.2, 95.0, 103.2,
        {"stop_loss": 95.0, "peak_price": 103.2}, 0.15,
    )
    assert d["action"] == "SL_CERT_LOCK"
    assert d["detail"] == "certainty_lock_line_reached"
    assert d["cert_lock_pct"] == pytest.approx(3.0)
    assert d["profit_pct"] == pytest.approx(3.2)

    below = decide_original_profit_exit(
        "LONG", 100.0, 102.9, 95.0, 102.9,
        {"stop_loss": 95.0, "peak_price": 102.9}, 0.15,
    )
    assert below["action"] == "hold"


def test_cert_lock_respects_explicit_take_profit_precedence(monkeypatch):
    """显式 take_profit 优先: 有 TP 的仓 (DA) 行为不被本机制改写。"""
    monkeypatch.setenv("EXT_CERT_LOCK_PCT", "3.0")
    d = decide_original_profit_exit(
        "LONG", 100.0, 110.2, 95.0, 110.0,
        {**META, "take_profit": 110.0}, 0.15,
    )
    assert d["action"] == "TAKE_PROFIT"


def test_cert_lock_requires_net_above_fee(monkeypatch):
    """阈值低于往返费时不得落袋 (防"纸面落袋")。"""
    monkeypatch.setenv("EXT_CERT_LOCK_PCT", "0.05")
    d = decide_original_profit_exit(
        "LONG", 100.0, 100.1, 95.0, 100.1,
        {"stop_loss": 95.0, "peak_price": 100.1}, 0.15,
    )
    assert d["action"] == "hold"
    assert d["detail"] == "cert_lock_below_net_fee"


def test_cert_lock_metadata_override_and_priority(monkeypatch):
    """按仓覆盖 metadata.cert_lock_pct; 且它必须能压过 hold_to_next_open 兜底。"""
    monkeypatch.delenv("EXT_CERT_LOCK_PCT", raising=False)
    d = decide_original_profit_exit(
        "LONG", 100.0, 102.5, 95.0, 102.5,
        {"stop_loss": 95.0, "peak_price": 102.5, "cert_lock_pct": 2.0}, 0.15,
    )
    assert d["action"] == "SL_CERT_LOCK"
    assert prioritize_profit_exit("SL_CERT_LOCK", "hold_to_next_open") == "SL_CERT_LOCK"


def test_cert_lock_zero_metadata_overrides_env_default(monkeypatch):
    """A 级写入 0.0 必须覆盖 cron 环境 3.0, 不得因 falsy-zero 回退."""
    monkeypatch.setenv("EXT_CERT_LOCK_PCT", "3.0")
    d = decide_original_profit_exit(
        "LONG", 100.0, 103.5, 95.0, 103.5,
        {"stop_loss": 95.0, "peak_price": 103.5, "cert_lock_pct": 0.0}, 0.15,
    )

    assert d["action"] == "hold"
    assert d["cert_lock_pct"] is None


# ── 2026-09-19 峰值口径: 只能用**进场之后**的真实 high (用户令) ──

def _bars(spec):
    def fn(start, end):
        return [{"t": t, "h": h} for t, h in spec]
    return fn


def test_post_entry_high_excludes_pre_entry_bars():
    """进场前的高点必须被丢弃 (用户令: 否则是假峰值, 保护线落在不可成交位置)。"""
    from scripts.extended_hours_market_session import post_entry_high
    spec = [
        ("2026-09-18 16:05:00", 30.90),   # 进场前 (虚高峰)
        ("2026-09-18 18:00:00", 30.55),   # 进场前
        ("2026-09-18 18:35:00", 29.80),   # 进场当刻
        ("2026-09-18 18:52:00", 29.94),   # 进场后真实高点
        ("2026-09-18 19:03:00", 29.86),
    ]
    v = post_entry_high("US.WBD", "2026-09-18T18:35:26-04:00", kline_fn=_bars(spec))
    assert v == 29.94, f"应为进场后最高 29.94, 实得 {v} (30.90 = 进场前高点泄漏)"


def test_post_entry_high_none_when_no_post_entry_data():
    from scripts.extended_hours_market_session import post_entry_high
    only_pre = [("2026-09-18 16:05:00", 30.90)]
    assert post_entry_high("US.WBD", "2026-09-18T18:35:26-04:00", kline_fn=_bars(only_pre)) is None
    assert post_entry_high("US.WBD", None, kline_fn=_bars(only_pre)) is None


def test_decider_folds_post_entry_high_only_when_enabled(monkeypatch):
    """env 关 ⇒ 忽略 (行为不变); env 开 ⇒ 峰值含进场后 high ⇒ 地板同步抬升。"""
    meta = {"stop_loss": 95.0, "peak_price": 100.0, "session_high_post_entry": 103.0}
    monkeypatch.delenv("EXT_PEAK_USE_SESSION_HIGH", raising=False)
    off = decide_original_profit_exit("LONG", 100.0, 100.2, 95.0, 100.0, dict(meta), 0.088)
    assert off["peak_price"] == pytest.approx(100.2)

    monkeypatch.setenv("EXT_PEAK_USE_SESSION_HIGH", "1")
    on = decide_original_profit_exit("LONG", 100.0, 100.2, 95.0, 100.0, dict(meta), 0.088)
    assert on["peak_price"] == pytest.approx(103.0)
    assert on["peak_profit_pct"] == pytest.approx(3.0)
    assert on["effective_sl"] > off["effective_sl"]
