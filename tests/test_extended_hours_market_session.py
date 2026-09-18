import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.extended_hours_market_session import (
    FIELD_UNITS,
    atr_metrics,
    cheap_extended_hours_session_for,
    extended_hours_session_for,
    net_executable_pnl_pct,
    previous_session_volume,
    quote_metrics,
    risk_r_pct,
    round_trip_fee_metrics,
    session_volume_delta,
)


ET = ZoneInfo("America/New_York")


def test_extended_hours_session_uses_et_calendar_and_half_day():
    assert extended_hours_session_for("2026-09-17", "0415") == "premarket"
    assert extended_hours_session_for("2026-09-17", "0929") == "premarket"
    assert extended_hours_session_for("2026-09-17", "0930") is None
    assert extended_hours_session_for("2026-09-17", "1800") == "afterhours"
    assert extended_hours_session_for("2026-09-17", "2000") is None
    assert extended_hours_session_for("2026-11-27", "1259") is None
    assert extended_hours_session_for("2026-11-27", "1300") == "afterhours"
    assert extended_hours_session_for("2026-11-27", "1959") == "afterhours"


def test_cheap_session_gate_skips_non_candidate_hours_and_weekends():
    assert cheap_extended_hours_session_for("2026-09-18", "0415") == "premarket"
    assert cheap_extended_hours_session_for("2026-09-18", "1800") == "afterhours"
    assert cheap_extended_hours_session_for("2026-09-18", "1200") is None
    assert cheap_extended_hours_session_for("2026-09-19", "0415") is None
    assert cheap_extended_hours_session_for("2026-11-27", "1300") == "afterhours"


def test_quote_metrics_use_direction_aware_executable_price_and_units():
    now = datetime(2026, 9, 17, 9, 15, 30, tzinfo=ET)
    snapshot = {
        "bid_price": 9.98,
        "ask_price": 10.02,
        "update_time": "2026-09-17 09:15:00",
    }
    metrics = quote_metrics(snapshot, "LONG", now)
    assert metrics["bid_price"] == 9.98
    assert metrics["ask_price"] == 10.02
    assert metrics["mid_price"] == 10.0
    assert abs(metrics["spread"] - 0.04) < 1e-12
    assert metrics["spread_pct"] == 0.4
    assert metrics["executable_price"] == 9.98
    assert metrics["quote_age_seconds"] == 30.0
    assert quote_metrics(snapshot, "SHORT", now)["executable_price"] == 10.02
    assert FIELD_UNITS["spread_pct"] == "percent"
    assert FIELD_UNITS["quote_age_seconds"] == "seconds"


def test_quote_metrics_accepts_pandas_series_snapshot_and_none():
    """回归钉 (2026-09-19 P0): futu 快照行是 pandas Series, 旧实现 `if snapshot` 走
    Series.__bool__ → ValueError, 令延长时段退出链**只要有持仓**就每分钟崩在 collect 首行。
    断言: ①Series 入力不得抛 (值正确) ②None 入力全 None 不抛 ③dict 与 Series 同值。"""
    import pandas as pd

    row = pd.Series({"bid_price": 9.98, "ask_price": 10.02,
                     "update_time": "2026-09-17 09:15:00", "last_price": 10.0})
    now = datetime(2026, 9, 17, 9, 15, 30, tzinfo=ET)
    series_metrics = quote_metrics(row, "LONG", now)          # 旧实现此处 ValueError
    assert series_metrics["bid_price"] == 9.98
    assert series_metrics["ask_price"] == 10.02
    assert series_metrics["executable_price"] == 9.98
    assert series_metrics["quote_age_seconds"] == 30.0

    assert quote_metrics(None, "LONG", now) == {
        "bid_price": None, "ask_price": None, "mid_price": None, "spread": None,
        "spread_pct": None, "executable_price": None, "quote_age_seconds": None,
    }

    # 缺字段/NaN 的 Series 行 (盘前盘后盘口可能为空) 必须静默降级为 None, 不得抛
    sparse = pd.Series({"bid_price": float("nan"), "ask_price": float("nan")})
    sparse_metrics = quote_metrics(sparse, "SHORT", now)
    assert sparse_metrics["bid_price"] is None
    assert sparse_metrics["ask_price"] is None
    assert sparse_metrics["executable_price"] is None

    # dict 与 Series 同值 ⇒ Series 不是"另一种口径"
    assert series_metrics["mid_price"] == quote_metrics(
        {"bid_price": 9.98, "ask_price": 10.02, "update_time": "2026-09-17 09:15:00"},
        "LONG", now)["mid_price"]


def test_risk_r_pct_is_direction_aware_and_rejects_invalid_stop():
    assert risk_r_pct(100, 95, "LONG") == 5.0
    assert risk_r_pct(100, 105, "SHORT") == 5.0
    assert risk_r_pct(100, 105, "LONG") is None
    assert risk_r_pct(100, 95, "SHORT") is None
    assert risk_r_pct(100, 0, "LONG") is None


def test_atr_metrics_declares_absolute_source_and_percent_unit():
    result = atr_metrics(100.0, '{"atr": 2.0}')
    assert result == {"atr_abs": 2.0, "atr_pct": 2.0, "atr_source": "trade_metadata_atr"}
    assert FIELD_UNITS["atr_abs"] == "absolute_price"
    assert FIELD_UNITS["atr_pct"] == "percent"
    assert FIELD_UNITS["atr_source"] == "enum"
    assert atr_metrics(100.0, None) == {"atr_abs": None, "atr_pct": None, "atr_source": None}


def test_round_trip_fee_and_net_executable_pnl():
    fees = round_trip_fee_metrics(100, 100, 101, "LONG", "2026-09-17")
    assert fees["round_trip_fee_usd"] > 0
    assert fees["round_trip_fee_pct"] > 0
    net = net_executable_pnl_pct(100, 101, "LONG", fees["round_trip_fee_pct"])
    assert net == 0.9519


def test_session_volume_delta_reads_latest_same_symbol_row(tmp_path):
    path = tmp_path / "obs.jsonl"
    rows = [
        {"symbol": "US.A", "trade_date": "2026-09-17", "session": "premarket", "session_volume": 100},
        {"symbol": "US.B", "trade_date": "2026-09-17", "session": "premarket", "session_volume": 300},
        {"symbol": "US.A", "trade_date": "2026-09-17", "session": "premarket", "session_volume": 150},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows))
    assert previous_session_volume(path, "US.A", "2026-09-17", "premarket") == 150
    assert session_volume_delta(path, "US.A", "2026-09-17", "premarket", 175) == 25
    assert session_volume_delta(path, "US.C", "2026-09-17", "premarket", 100) is None


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_premarket_collector_writes_observation_fields(tmp_path, monkeypatch):
    import subprocess as subprocess_module
    import types

    module = _load("pm_observation_test", "scripts/premarket_exit_shadow.py")
    calls = []
    futu = types.ModuleType("futu")
    futu.RET_OK = 0

    class Snapshot:
        def iterrows(self):
            yield 0, {
                "code": "US.FAKE", "last_price": 10.2, "pre_price": 10.2,
                "pre_volume": 125000.0, "prev_close_price": 10.0,
                "pre_high_price": 10.3, "bid_price": 10.18, "ask_price": 10.22,
                "update_time": "2026-09-18 09:15:00",
            }

    futu.OpenQuoteContext = lambda **kwargs: types.SimpleNamespace(
        get_market_snapshot=lambda symbols: (0, Snapshot()),
        close=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "futu", futu)
    monkeypatch.setattr(module, "_today_et", lambda: "2026-09-18")
    monkeypatch.setattr(module, "_now_et_hhmm", lambda: 915)
    monkeypatch.setattr(module, "_load_open_positions", lambda: [
        ("US.FAKE", "LONG", 100, 10.0, 9.5, "2026-09-18 02:30:00", "signal-1", '{"atr":0.5}')
    ])
    monkeypatch.setattr(module, "OUTPUT", tmp_path / "out.jsonl")
    monkeypatch.setattr(
        subprocess_module, "run",
        lambda command, **kwargs: calls.append(command) or types.SimpleNamespace(
            returncode=0, stdout="", stderr=""
        ),
    )
    module.collect(dry=False)
    row = json.loads((tmp_path / "out.jsonl").read_text())
    assert row["session"] == "premarket"
    assert row["bid_price"] == 10.18
    assert row["ask_price"] == 10.22
    assert row["executable_price"] == 10.18
    assert row["spread_pct"] > 0
    assert row["risk_r_pct"] == 5.0
    assert row["atr_abs"] == 0.5
    assert row["atr_pct"] == 5.0
    assert row["atr_source"] == "trade_metadata_atr"
    assert row["session_volume_delta"] is None
    assert row["round_trip_fee_pct"] > 0
    assert row["net_executable_pnl_pct"] == 1.3379
