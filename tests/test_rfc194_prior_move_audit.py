#!/usr/bin/env python3
"""RFC-194 契约测试: T-1 走势审计字段 (只记不判)。

钉住五件事:
  1. `_prior_move_context` 从本地日K缓存算 t1/t5/t20 (as-of 明确, 取 < 今日 的最后一根);
  2. 缓存断档 (as_of 早于 SPY 上一交易日) → stale=True, 且**不再**当 T-1 用 (分析面剔除);
  3. 缓存缺失/断档时走 `_live_prior_bars` 兜底 (零额度), source 标 "live";
  4. 两条路都拿不到 → None, **绝不抛异常** (审计字段不阻断下单);
  5. 成交后 `_write_open_position` 把 prior_move 落 metadata_json (只增键)。
"""
import json
import pathlib
import sqlite3
import sys
from datetime import date, timedelta

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scripts.extended_hours_entry_executor as executor  # noqa: E402

TODAY = "2026-11-09"
START = date(2026, 10, 1)


def _bars(closes):
    """v1 缓存形态: 从 2026-10-01 起逐日一根, close 用给定序列。"""
    return [{"time": (START + timedelta(days=i)).isoformat() + " 00:00:00", "close": float(c)}
            for i, c in enumerate(closes)]


def _spy(n=30):
    """SPY 序列: 平缓上行的 30 根 (as_of 与 30 根的标的对齐)。"""
    return _bars([100 + 0.5 * i for i in range(n)])


@pytest.fixture()
def cache(tmp_path, monkeypatch):
    def _install(per_symbol):
        payload = {"version": 1, "symbols": {
            sym: {"bars": bars, "fetched_at": 0} for sym, bars in per_symbol.items()}}
        path = tmp_path / "daily_kline_v1.json"
        path.write_text(json.dumps(payload))
        monkeypatch.setattr(executor, "DAILY_BARS_CACHE", path)
        monkeypatch.setattr(executor, "_PRIOR_MOVE_CACHE",
                            {"loaded": False, "bars": {}, "spy_as_of": "",
                             "spy_rv20_pct": None, "spy_rv20_rank": None})
        return path
    monkeypatch.setattr(executor, "_live_prior_bars", lambda *a, **k: None)
    return _install


def test_context_computes_t1_t5_t20_from_cache(cache):
    closes = [100 + i for i in range(30)]      # 100..129 逐日 +1
    closes[-1] = 132.0                         # 末日跳升 → 可精确断言
    cache({"US.SPY": _spy(), "US.AAA": _bars(closes)})
    ctx = executor._prior_move_context("US.AAA", TODAY)
    assert ctx["source"] == "cache"
    assert ctx["as_of"] == "2026-10-30"
    assert ctx["t1_ret_pct"] == round((132.0 / 128.0 - 1) * 100, 2)
    assert ctx["t5_ret_pct"] == round((132.0 / 124.0 - 1) * 100, 2)
    assert ctx["t20_ret_pct"] == round((132.0 / 109.0 - 1) * 100, 2)


def test_context_excludes_today_bar(cache):
    """as-of 必须是 < 今日 的最后一根 —— 今日未收盘的 bar 不许进 T-1。"""
    bars = _bars([100 + i for i in range(30)])
    bars.append({"time": TODAY + " 00:00:00", "close": 999.0})
    cache({"US.SPY": _spy(), "US.AAA": bars})
    ctx = executor._prior_move_context("US.AAA", TODAY)
    assert ctx["as_of"] == "2026-10-30"
    assert ctx["t1_ret_pct"] == round((129 / 128 - 1) * 100, 2)


def test_stale_flag_when_symbol_lags_spy(cache):
    cache({"US.SPY": _spy(), "US.AAA": _bars([100 + i for i in range(30)])[:-3]})
    ctx = executor._prior_move_context("US.AAA", TODAY)
    assert ctx["stale"] is True
    assert ctx["as_of"] == "2026-10-27"


def test_live_fallback_used_when_cache_missing(cache, monkeypatch):
    cache({"US.SPY": _spy()})
    live_rows = [((START + timedelta(days=d)).isoformat(), 100.0 + d) for d in range(30)]
    monkeypatch.setattr(executor, "_live_prior_bars", lambda *a, **k: live_rows)
    ctx = executor._prior_move_context("US.BBB", TODAY)
    assert ctx["source"] == "live"
    assert ctx["as_of"] == "2026-10-30"


def test_live_fallback_not_used_when_cache_fresh(cache, monkeypatch):
    cache({"US.SPY": _spy(), "US.AAA": _bars([100 + i for i in range(30)])})
    calls = []
    monkeypatch.setattr(executor, "_live_prior_bars",
                        lambda *a, **k: calls.append(1) or None)
    executor._prior_move_context("US.AAA", TODAY)
    assert calls == []


def test_returns_none_when_both_sources_missing(cache):
    cache({"US.SPY": _spy()})
    assert executor._prior_move_context("US.CCC", TODAY) is None


def test_returns_none_on_short_history(cache):
    cache({"US.SPY": _spy(), "US.AAA": _bars([100 + i for i in range(10)])})
    assert executor._prior_move_context("US.AAA", TODAY) is None


def test_spy_regime_fields_present(cache):
    cache({"US.SPY": _spy(), "US.AAA": _bars([100 + i for i in range(30)])})
    ctx = executor._prior_move_context("US.AAA", TODAY)
    assert ctx["spy_as_of"] == "2026-10-30"
    assert ctx["spy_rv20_pct"] is not None
    assert 0 <= ctx["spy_rv20_rank"] <= 100


def test_rv20_series_flat_is_zero_and_rank_bounded():
    rv, rank = executor._rv20_series([100.0] * 40)
    assert rv == pytest.approx(0.0, abs=1e-9)
    assert 0 <= rank <= 100


def test_rv20_series_short_history_returns_none():
    assert executor._rv20_series([100.0] * 10) == (None, None)


_OP_COLS = ("position_id TEXT PRIMARY KEY, symbol TEXT, direction TEXT, size REAL, "
            "avg_entry_price REAL, entry_time TEXT, strategy_id TEXT, source TEXT, "
            "owner_strategy TEXT, owner TEXT, mode TEXT, account_type TEXT, "
            "trader_type TEXT, is_filled INTEGER, version INTEGER, updated_at TEXT, "
            "metadata_json TEXT, sync_status TEXT, initial_sl REAL, current_sl REAL, "
            "stop_loss_price REAL")
_TR_COLS = ("id INTEGER PRIMARY KEY, trade_date TEXT, symbol TEXT, direction TEXT, "
            "entry_time TEXT, entry_price REAL, quantity REAL, source_type TEXT, "
            "trade_mode TEXT, mode TEXT, owner_strategy TEXT, strategy_id TEXT, "
            "futu_order_id TEXT, metadata_json TEXT, created_at TEXT, exit_price REAL, "
            "exit_time TEXT, config_version TEXT, entry_session TEXT, decision_id TEXT, "
            "realized_pnl REAL")


def _ext_db(tmp_path, monkeypatch):
    path = tmp_path / "ext.db"
    conn = sqlite3.connect(str(path))
    conn.execute(f"CREATE TABLE open_positions ({_OP_COLS})")
    conn.execute(f"CREATE TABLE trade_records ({_TR_COLS})")
    conn.commit()
    conn.close()
    monkeypatch.setattr(executor, "DB", str(path))
    return path


def _meta(path, order_id):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return json.loads(conn.execute(
            "SELECT metadata_json FROM trade_records WHERE futu_order_id=?",
            (order_id,)).fetchone()[0])
    finally:
        conn.close()


def test_write_open_position_persists_prior_move(tmp_path, monkeypatch):
    path = _ext_db(tmp_path, monkeypatch)
    ctx = {"as_of": "2026-11-06", "source": "live", "stale": False,
           "t1_ret_pct": -6.2, "t5_ret_pct": -11.4, "t20_ret_pct": -15.1,
           "spy_rv20_pct": 19.44, "spy_rv20_rank": 98.8, "spy_as_of": "2026-11-06"}
    executor._write_open_position(
        "US.DDD", 10, 50.0, 50.0, "premarket", {}, 3.0, 100000,
        buy_order_id="900001", gate_mode="strict", side="LONG", prior_move=ctx,
    )
    assert _meta(path, "900001")["prior_move"] == ctx


def test_write_open_position_without_prior_move_keeps_key_null(tmp_path, monkeypatch):
    path = _ext_db(tmp_path, monkeypatch)
    executor._write_open_position(
        "US.EEE", 10, 20.0, 20.0, "premarket", {}, 2.0, 50000,
        buy_order_id="900002", gate_mode="strict",
    )
    meta = _meta(path, "900002")
    assert "prior_move" in meta and meta["prior_move"] is None