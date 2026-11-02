"""A1 invalidation replay contract tests (2026-10-28, review-only)."""
import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.playbook.invalidation_replay import (
    LEFT_TAIL_NET_BPS,
    classify_text_price_rule,
    daily_cluster_stats,
    normalise_bars,
    select_structured_close_trigger,
    simulate_invalidation_trade,
    summarize_arm,
)
from app.utils.timezone import ET


ROOT = Path(__file__).resolve().parent.parent


def _load_script():
    path = ROOT / "scripts" / "invalidation_replay_a1.py"
    spec = importlib.util.spec_from_file_location("invalidation_replay_a1", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M = _load_script()


def _bar(hhmm, o, h, low, c, day="2026-10-27"):
    hour, minute = map(int, hhmm.split(":"))
    return {
        "t": ET.localize(datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00")),
        "o": o, "h": h, "l": low, "c": c,
    }


def _trade(*, entry="09:36", exit_="10:00", entry_price=100.0, exit_price=100.5,
           stop=95.0, qty=10, direction="LONG"):
    day = "2026-10-27"
    return {
        "id": 1,
        "symbol": "US.TEST",
        "direction": direction,
        "entry_et": ET.localize(datetime.fromisoformat(f"{day}T{entry}:00")),
        "exit_et": ET.localize(datetime.fromisoformat(f"{day}T{exit_}:00")),
        "entry_price": entry_price,
        "exit_price": exit_price,
        "quantity": qty,
        "stop_loss": stop,
        "bucket_effective": "medium",
    }


def _bars(*rows):
    return list(rows)


def test_text_price_semantics_separates_exit_compound_and_cancel():
    simple = classify_text_price_rule(
        "若日线收盘跌破$91.48，立即止损离场", 91.48, "LONG"
    )
    assert simple["semantic_status"] == "exit"
    assert simple["condition_class"] == "simple"

    compound = classify_text_price_rule(
        "放量跌破$124.09且收阴，立即止损", 124.09, "LONG"
    )
    assert compound["semantic_status"] == "exit"
    assert compound["condition_class"] == "compound"

    cancel = classify_text_price_rule(
        "若连续3日收盘价低于$313.09，则取消所有买单并反手观望", 313.09, "LONG"
    )
    assert cancel["semantic_status"] == "non_exit"
    assert cancel["condition_class"] == "non_exit_cancel"


def test_text_price_semantics_keeps_direction_and_negated_reclaim():
    # LONG + upward break is not a downside invalidation.
    ambiguous = classify_text_price_rule(
        "若收盘站上$1100，则趋势反转", 1100.0, "LONG"
    )
    assert ambiguous["semantic_status"] == "ambiguous"

    # SHORT + upward break is the correct failure direction.
    short_ok = classify_text_price_rule(
        "若盘中放量突破$235.00并站稳，则空头逻辑失效", 235.0, "SHORT"
    )
    assert short_ok["semantic_status"] == "exit"
    assert short_ok["condition_class"] == "compound"

    # "未站回" is a downside failure, not an upward breakout.
    negated = classify_text_price_rule(
        "收盘未站回$1040，则逻辑失效", 1040.0, "LONG"
    )
    assert negated["semantic_status"] == "exit"


def test_structured_trigger_selects_first_to_trigger():
    raw = json.dumps({"invalidation_triggers": [
        {"metric": "收盘价", "operator": "<", "value": 90.0},
        {"metric": "收盘价", "operator": "<", "value": 95.0},
        {"metric": "5日涨跌", "operator": "<", "value": -8.0},
    ]})
    trigger = select_structured_close_trigger(raw, "LONG")
    assert trigger["value"] == 95.0
    assert trigger["condition_class"] == "structured_price_or_other"

    short = select_structured_close_trigger(json.dumps({"invalidation_triggers": [
        {"metric": "收盘价", "operator": ">", "value": 110.0},
        {"metric": "收盘价", "operator": ">", "value": 105.0},
    ]}), "SHORT")
    assert short["value"] == 105.0


def test_normalise_bars_uses_et_wall_clock_and_counts_bad_rows():
    bars, bad = normalise_bars([
        ["2026-10-27 09:31:00", 1, 2, 0.5, 1.5],
        ["bad", 1, 2, 0.5, 1.5],
    ])
    assert bad == 1
    assert len(bars) == 1
    assert bars[0]["t"].tzinfo is not None
    assert bars[0]["t"].hour == 9


def test_intraday_trigger_skips_entry_bar_and_fills_next_open():
    trade = _trade(exit_price=101.0)
    trigger = {"operator": "<", "value": 98.0, "price_basis": "intraday"}
    bars = _bars(
        _bar("09:36", 100, 100, 94, 99),       # entry bar: low must not arm
        _bar("09:37", 99, 99, 97, 97.5),       # trigger
        _bar("09:38", 97.2, 98, 97, 97.8),     # fill at open
        _bar("09:39", 97.8, 99, 97.5, 99),
    )
    result = simulate_invalidation_trade(
        trade=trade, bars=bars, trigger=trigger, slippage_bps=3.0,
        trigger_order="pess", close_mode="near_close",
    )
    assert result["status"] == "ok"
    assert result["trigger_applied"] is True
    assert result["fill_bar"].endswith("09:38:00-04:00")
    assert result["arm"]["exit_reason"] == "INVALIDATION_INTRADAY"
    assert result["arm"]["exit_price"] == pytest.approx(97.2 * (1 - 0.0003))
    assert result["baseline"]["exit_reason"] == "ACTUAL_EXIT"


def test_same_bar_sl_and_trigger_order_is_pre_registered():
    trade = _trade(stop=95.0)
    trigger = {"operator": "<", "value": 98.0, "price_basis": "intraday"}
    bars = _bars(
        _bar("09:37", 99, 99, 94, 96),  # both SL and invalidation touched
        _bar("09:38", 95.5, 96, 95, 95.8),
    )
    pessimistic = simulate_invalidation_trade(
        trade=trade, bars=bars, trigger=trigger, slippage_bps=0,
        trigger_order="pess", close_mode="near_close",
    )
    optimistic = simulate_invalidation_trade(
        trade=trade, bars=bars, trigger=trigger, slippage_bps=0,
        trigger_order="opt", close_mode="near_close",
    )
    assert pessimistic["trigger_applied"] is False
    assert pessimistic["baseline"]["exit_reason"] == "SL_TOUCH"
    assert optimistic["trigger_applied"] is True
    assert optimistic["arm"]["exit_price"] == 95.5


def test_close_trigger_near_close_and_next_open_sensitivity():
    trade = _trade(entry="09:36", exit_="15:52")
    trigger = {"operator": "<", "value": 98.0, "price_basis": "close"}
    near = _bars(
        _bar("09:37", 100, 101, 99, 100),
        _bar("15:50", 99, 99, 97, 97.5),
        _bar("15:51", 97.2, 98, 97, 97.8),
        _bar("16:00", 97.8, 98, 97, 97.9),
    )
    primary = simulate_invalidation_trade(
        trade=trade, bars=near, trigger=trigger, slippage_bps=0,
        trigger_order="pess", close_mode="near_close",
    )
    assert primary["trigger_applied"] is True
    assert primary["arm"]["exit_reason"] == "INVALIDATION_CLOSE"
    assert primary["arm"]["exit_price"] == 97.2

    # Official close cannot fill before a 15:52 actual exit.
    next_open = simulate_invalidation_trade(
        trade=trade, bars=near, trigger=trigger, slippage_bps=0,
        trigger_order="pess", close_mode="next_open",
    )
    assert next_open["trigger_applied"] is False
    assert next_open["trigger_observed"] is False
    assert next_open["trigger_unusable_reason"] is None


def test_early_exit_does_not_turn_last_available_bar_into_near_close():
    trade = _trade(entry="09:36", exit_="10:00")
    trigger = {"operator": "<", "value": 98.0, "price_basis": "close"}
    full_session = _bars(
        _bar("09:37", 100, 101, 99, 100),
        _bar("09:55", 99, 99, 97, 97.5),
        _bar("09:56", 97.2, 98, 97, 97.8),
        _bar("15:50", 96, 96, 95, 95.5),
        _bar("15:51", 95.5, 96, 95, 95.8),
    )
    result = simulate_invalidation_trade(
        trade=trade, bars=full_session, trigger=trigger, slippage_bps=0,
        trigger_order="pess", close_mode="near_close",
    )
    assert result["trigger_observed"] is False
    assert result["trigger_applied"] is False


def test_multi_day_close_trigger_next_session_open():
    trade = _trade(exit_="10:00", exit_price=101.0)
    trade["exit_et"] = ET.localize(datetime.fromisoformat("2026-10-28T10:00:00"))
    trigger = {"operator": "<", "value": 98.0, "price_basis": "close"}
    bars = _bars(
        _bar("09:37", 100, 101, 99, 100),
        _bar("16:00", 99, 99, 97, 97.5),
        _bar("09:31", 96.0, 97, 95.5, 96.5, day="2026-10-28"),
    )
    result = simulate_invalidation_trade(
        trade=trade, bars=bars, trigger=trigger, slippage_bps=0,
        trigger_order="pess", close_mode="next_open",
    )
    assert result["trigger_applied"] is True
    assert result["arm"]["exit_price"] == 96.0
    assert result["arm"]["exit_date"] == "2026-10-28"


def test_daily_cluster_stats_and_left_tail_summary():
    stats = daily_cluster_stats({
        "2026-10-27": [10.0, 10.0],
        "2026-10-28": [20.0, 20.0],
    })
    assert stats["days"] == 2
    assert stats["mean"] == 15.0
    assert stats["t"] == 3.0
    assert stats["mde80"] == pytest.approx(14.007926, abs=1e-4)

    rows = [
        {
            "baseline": {"net_pnl": -100.0, "gross_pnl": -90.0, "net_bps": LEFT_TAIL_NET_BPS},
            "arm": {"net_pnl": -50.0, "gross_pnl": -40.0, "net_bps": -50.0},
            "trigger_applied": True,
            "entry_time": "2026-10-27T09:36:00-04:00",
        },
        {
            "baseline": {"net_pnl": 100.0, "gross_pnl": 110.0, "net_bps": 100.0},
            "arm": {"net_pnl": -10.0, "gross_pnl": 0.0, "net_bps": -10.0},
            "trigger_applied": True,
            "entry_time": "2026-10-28T09:36:00-04:00",
        },
    ]
    summary = summarize_arm(rows, arm_label="test")
    assert summary["n"] == 2
    assert summary["trigger_rate"] == 1.0
    assert summary["avoided_loss_net_usd"] == 50.0
    assert summary["killed_winners_n"] == 1
    assert summary["left_tail_reduction_net_usd"] == 50.0
    assert summary["winner_profit_erosion_net_usd"] == 110.0
    assert summary["right_tail_n"] == 1
    assert summary["right_tail_change_net_usd"] == -110.0
    assert summary["triggered_delta_net_sum"] == -60.0


def test_summarize_arm_counts_near_close_window():
    def row(exit_hm, *, triggered, delta=0.0):
        return {
            "entry_time": "2026-10-27T09:36:00-04:00",
            "baseline": {
                "exit_time": f"2026-10-27T{exit_hm}:00-04:00",
                "net_pnl": 0.0,
                "gross_pnl": 0.0,
                "net_bps": 0.0,
            },
            "arm": {
                "net_pnl": delta,
                "gross_pnl": delta,
                "net_bps": delta,
            },
            "trigger_applied": triggered,
        }

    summary = summarize_arm([
        row("10:00", triggered=False),  # early exit: no near-close window
        row("16:00", triggered=False),  # held into the window, not triggered
        row("16:00", triggered=True, delta=10.0),  # held and triggered
    ], arm_label="test")
    assert summary["n"] == 3
    assert summary["n_triggered"] == 1
    assert summary["near_close_window_n"] == 2
    assert summary["near_close_window_trigger_rate"] == 0.5


def _make_minimal_db(path: Path):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE trade_records (id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT, "
        "entry_time TEXT, exit_time TEXT, entry_price REAL, exit_price REAL, quantity REAL, "
        "pnl_net REAL, metadata_json TEXT, source_type TEXT)"
    )
    conn.execute(
        "CREATE TABLE playbook_instructions (id INTEGER PRIMARY KEY, entry_price REAL, "
        "stop_loss REAL, invalidation TEXT, raw_instruction_json TEXT)"
    )
    meta = {
        "playbook_instruction_id": 1,
        "stop_loss": 95.0,
        "bucket_lineage": {"bucket_effective": "medium"},
    }
    conn.execute(
        "INSERT INTO playbook_instructions VALUES (?,?,?,?,?)",
        (1, 100.0, 95.0, "跌破98.0，立即止损", "{}"),
    )
    conn.execute(
        "INSERT INTO trade_records VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (1, "US.TEST", "LONG", "2026-10-27T09:36:00-04:00",
         "2026-10-27T10:00:00-04:00", 100.0, 101.0, 10.0, 9.0,
         json.dumps(meta), "PLAYBOOK_SIM"),
    )
    conn.commit()
    conn.close()


def test_cli_end_to_end_offline_no_write(tmp_path, capsys):
    db = tmp_path / "replay.db"
    _make_minimal_db(db)
    bar_dir = tmp_path / "bars"
    bar_dir.mkdir()
    raw = []
    start = datetime(2026, 10, 27, 9, 31)
    for index in range(390):
        stamp = (start + timedelta(minutes=index)).strftime("%Y-%m-%d %H:%M:%S")
        open_ = 100.0
        high = 100.5
        low = 99.5
        close = 100.0
        if index == 6:  # 09:37
            low = 97.0
        raw.append([stamp, open_, high, low, close])
    raw[-1][0] = "2026-10-27 16:00:00"
    (bar_dir / "US_TEST_2026-10-27.json").write_text(json.dumps(raw), encoding="utf-8")

    rc = M.main([
        "--db", str(db), "--bar-dir", str(bar_dir), "--no-write",
        "--since", "2026-10-27", "--until", "2026-10-27",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    summary = json.loads(out)
    assert summary["universe"]["eligible_rows"] == 1
    assert summary["arms"]["text__near_close__pess"]["pooled"]["n"] == 1
    assert summary["arms"]["text__near_close__pess"]["pooled"]["n_triggered"] == 1
    assert summary["fee_errors"]["n_rows"] == 0


def test_cli_refresh_bars_requires_fetch(tmp_path, capsys):
    db = tmp_path / "replay.db"
    _make_minimal_db(db)
    rc = M.main([
        "--db", str(db), "--bar-dir", str(tmp_path / "bars"),
        "--refresh-bars", "--no-write",
    ])
    assert rc == 2
    assert "--fetch" in capsys.readouterr().err


def test_fee_model_failure_is_recorded_and_counted(monkeypatch):
    """费用模型异常不得静默归零: 必须在 leg 留痕并被 summarize_arm 计数披露。"""
    from app.execution import fee_model

    def _boom(*args, **kwargs):
        raise RuntimeError("fee model offline")

    monkeypatch.setattr(fee_model, "estimate_round_trip", _boom)
    result = simulate_invalidation_trade(
        trade=_trade(stop=95.0),
        bars=_bars(
            _bar("09:37", 99, 99, 94, 96),
            _bar("09:38", 95.5, 96, 95, 95.8),
        ),
        trigger={"operator": "<", "value": 98.0, "price_basis": "intraday"},
        slippage_bps=0, trigger_order="pess", close_mode="near_close",
    )
    assert result["status"] == "ok"
    assert result["baseline"]["fee"] == 0.0
    assert result["baseline"]["fee_error"] is not None
    assert "fee model offline" in result["baseline"]["fee_error"]

    summary = summarize_arm([result], arm_label="fee_fail")
    assert summary["n_fee_error_rows"] == 1
    assert summary["fee_error_sample"]
