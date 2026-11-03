import json
import sqlite3

from app.review.actionability_diagnostics import build_actionability_diagnostics
from app.review.bundle_builder import build
from app.review.bundle_render import render_md


def _create_db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        create table trade_records (
          id integer primary key,
          trade_date text,
          symbol text not null,
          direction text not null,
          entry_time text,
          exit_time text,
          entry_price real,
          exit_price real,
          entry_session text,
          exit_reason text,
          strategy_id text,
          owner_strategy text,
          source_type text,
          trade_mode text,
          entry_reason text,
          signal_id text,
          decision_id text,
          realized_pnl real,
          pnl_net real,
          fees_est real,
          mfe_pct real,
          mae_pct real,
          quantity integer,
          data_quality text,
          metadata_json text
        );
        create table playbook_instructions (
          id integer primary key,
          date text not null,
          symbol text not null,
          direction text not null,
          skip_reason text,
          entry_price real,
          stop_loss real,
          take_profit real,
          thesis text,
          invalidation text,
          raw_instruction_json text,
          metadata_json text,
          decision_id text,
          config_version text,
          model_used text,
          decision_conf real
        );
        create table playbook_signal_context (
          id integer primary key,
          date text not null,
          symbol text not null,
          raw_data_json text,
          created_at text
        );
        """
    )
    return connection


def _trade(connection):
    metadata = {
        "_be_net_line_price": 10.2,
        "_adaptive_trailing": {"target_capture_pct": 60.0},
        "_effective_sl_at_exit": 9.8,
        "breakeven_triggered": False,
    }
    connection.execute(
        "insert into trade_records(trade_date,symbol,direction,entry_time,exit_time,"
        "entry_price,exit_price,entry_session,exit_reason,strategy_id,owner_strategy,"
        "source_type,trade_mode,entry_reason,signal_id,decision_id,realized_pnl,"
        "pnl_net,fees_est,mfe_pct,mae_pct,quantity,data_quality,metadata_json) "
        "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "2026-09-18", "US.A", "LONG",
            "2026-09-18T10:00:30-04:00", "2026-09-18T10:07:00-04:00",
            10.0, 9.8, "regular", "SL_INITIAL", "playbook_sim", "playbook_sim",
            "PLAYBOOK_SIM", "simulate", "playbook", "sig-a", "dec-a", -102.0,
            -102.0, 2.0, 1.0, 2.0, 500, "real", json.dumps(metadata),
        ),
    )


def _write_bars(path):
    bars = [
        ["2026-09-18 10:00:00", 10.0, 10.0, 9.99, 10.0],
        ["2026-09-18 10:01:00", 10.0, 10.6, 10.0, 10.5],
        ["2026-09-18 10:02:00", 10.5, 10.7, 10.45, 10.6],
        ["2026-09-18 10:03:00", 10.5, 10.55, 10.15, 10.2],
        ["2026-09-18 10:04:00", 10.2, 10.25, 10.1, 10.15],
        ["2026-09-18 10:05:00", 10.15, 10.2, 10.05, 10.4],
        ["2026-09-18 10:06:00", 10.4, 10.4, 10.0, 9.8],
        ["2026-09-18 10:07:00", 9.8, 9.9, 9.7, 9.8],
    ]
    path.write_text(json.dumps(bars), encoding="utf-8")


def _short_trade(connection):
    connection.execute(
        "insert into trade_records(trade_date,symbol,direction,entry_time,exit_time,"
        "entry_price,exit_price,entry_session,exit_reason,strategy_id,owner_strategy,"
        "source_type,trade_mode,entry_reason,signal_id,decision_id,realized_pnl,"
        "pnl_net,fees_est,mfe_pct,mae_pct,quantity,data_quality,metadata_json) "
        "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "2026-09-18", "US.B", "SHORT",
            "2026-09-18T10:00:30-04:00", "2026-09-18T10:06:00-04:00",
            20.0, 21.0, "regular", "SL_INITIAL", "playbook_sim", "playbook_sim",
            "PLAYBOOK_SIM", "simulate", "playbook", "sig-b", "dec-b", -102.0,
            -102.0, 2.0, 1.0, 5.0, 100, "real",
            json.dumps({"_be_net_line_price": 19.8, "exit_slippage_bps": 10.0}),
        ),
    )


def _write_short_bars(path):
    bars = [
        ["2026-09-18 10:00:00", 20.0, 20.1, 19.8, 19.9],
        ["2026-09-18 10:01:00", 19.9, 20.05, 19.7, 19.8],
        ["2026-09-18 10:02:00", 19.8, 19.9, 19.6, 19.8],
        ["2026-09-18 10:03:00", 19.8, 19.85, 19.7, 19.8],
        ["2026-09-18 10:04:00", 19.8, 19.85, 19.7, 19.0],
        ["2026-09-18 10:05:00", 19.0, 19.1, 18.9, 19.0],
        ["2026-09-18 10:06:00", 19.0, 21.0, 19.0, 21.0],
    ]
    path.write_text(json.dumps(bars), encoding="utf-8")


def test_actionability_mfe_regret_and_delay_use_actual_path(tmp_path):
    connection = _create_db()
    _trade(connection)
    _write_bars(tmp_path / "US_A_2026-09-18.json")

    result = build_actionability_diagnostics(connection, "2026-09-18", tmp_path)

    mfe = result["actionable_mfe"]
    assert mfe["evaluated_count"].value == 1
    assert mfe["actionable_count"].value == 1
    assert mfe["cases"].value[0]["persistent_bars"] == 6
    assert mfe["cases"].value[0]["rescue_action"] == "breakeven_lock"
    assert mfe["cases"].value[0]["estimated_rescue_net"] == 98.0
    assert mfe["cases"].value[0]["verdict"] == "candidate_not_verdict"

    regret = result["regret_price"]
    assert regret["cases"].value[0]["regret_price"] == 10.15
    assert regret["cases"].value[0]["estimated_net_at_regret"] == 23.0

    delay = result["delay_5"]
    assert delay["cases"].value[0]["delayed_entry_price"] == 10.4
    assert delay["cases"].value[0]["delayed_net"] == -302.0
    assert delay["cases"].value[0]["actual_minus_delayed"] == 200.0
    assert delay["actual_better_count"].value == 1


def test_missing_bars_do_not_crash_and_are_reported(tmp_path):
    connection = _create_db()
    _trade(connection)

    result = build_actionability_diagnostics(connection, "2026-09-18", tmp_path)

    assert result["actionable_mfe"]["evaluated_count"].value == 0
    assert result["actionable_mfe"]["bar_unavailable_count"].value == 1
    assert result["delay_5"]["bar_unavailable_count"].value == 1
    assert result["regret_price"]["bar_unavailable_count"].value == 1


def test_delay_and_regret_use_short_direction_correctly(tmp_path):
    connection = _create_db()
    _short_trade(connection)
    _write_short_bars(tmp_path / "US_B_2026-09-18.json")

    result = build_actionability_diagnostics(connection, "2026-09-18", tmp_path)

    regret_case = result["regret_price"]["cases"].value[0]
    delay_case = result["delay_5"]["cases"].value[0]
    assert regret_case["regret_price"] == 21.0
    assert regret_case["estimated_net_at_regret"] == 116.02
    assert delay_case["delayed_net"] == -204.1
    assert delay_case["actual_minus_delayed"] == 102.1


def test_pretrade_card_coverage_names_missing_fields():
    connection = _create_db()
    raw = {
        "strategy_type": "pullback_limit",
        "entry_archetype": "PULLBACK_LIMIT",
        "entry_strategy": "pullback",
    }
    connection.execute(
        "insert into playbook_instructions(date,symbol,direction,entry_price,stop_loss,"
        "take_profit,thesis,invalidation,raw_instruction_json,metadata_json,decision_id,"
        "config_version,model_used,decision_conf) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "2026-09-18", "US.A", "BUY", 10.0, 9.0, 11.0, "thesis", "invalidation",
            json.dumps(raw), json.dumps({}), "dec-a", "v1", "test-model", 0.7,
        ),
    )
    context = {
        "symbol": "US.A",
        "fundamental": {"source": "futu", "fetched_at": "2026-09-18T13:00:00Z"},
        "technical": {"source": "futu", "fetched_at": "2026-09-18T13:00:01Z"},
    }
    connection.execute(
        "insert into playbook_signal_context(date,symbol,raw_data_json,created_at) "
        "values (?,?,?,?)",
        (
            "2026-09-18", "US.A", json.dumps(context), "2026-09-18T13:01:00Z",
        ),
    )
    # 非漏斗行 (bridge 未提名: direction=SKIP, entry_price=0, 未触发未执行)
    #   — 2026-11-03 口径收窄: 这类行恒无卡片字段, 不计入审计分母
    connection.execute(
        "insert into playbook_instructions(date,symbol,direction,entry_price,stop_loss,"
        "take_profit,thesis,invalidation,raw_instruction_json,metadata_json,decision_id,"
        "config_version,model_used,decision_conf) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "2026-09-18", "US.SKIP", "SKIP", 0.0, 0.0, 0.0, "", "",
            json.dumps({}), json.dumps({}), "", "v1", "test-model", None,
        ),
    )

    coverage = build_actionability_diagnostics(connection, "2026-09-18", None)[
        "pretrade_card"
    ]

    # 2026-11-03 新语义: ①只数漏斗行 (US.SKIP 排除) ②prompt 侧未接线字段
    #   (candidate_top5/scenarios 等) 属规格缺口层 — 标注在 spec_gap_fields,
    #   不计入 missing_fields/完整率 (补齐须改 DA prompt = PROMPT_HASH 断点 ⇒ DEB)。
    assert coverage["instruction_count"].value == 1
    assert coverage["complete_card_count"].value == 1
    case = coverage["cases"].value[0]
    assert case["symbol"] == "US.A"
    assert case["missing_fields"] == []
    spec_gap = case["spec_gap_fields"]
    assert "prompt_secondary_class" in spec_gap
    assert "prompt_version" in spec_gap
    assert "candidate_top5" in spec_gap
    assert "rejection_reasons" in spec_gap
    assert "scenario_trend_continuation" in spec_gap
    assert "rescue_actions" in spec_gap
    # prompt_primary_class 可经 strategy_type 产出 ⇒ 不在缺口层
    assert "prompt_primary_class" not in spec_gap


def test_bundle_renders_actionability_summary_without_bars():
    connection = _create_db()
    _trade(connection)

    bundle = build(connection, "2026-09-18")
    full_md, _ = render_md(bundle)

    assert "## 可行动性诊断" in full_md
    assert "可行动 MFE：0 / 0（缺 bar 1）" in full_md
    assert "Pretrade 卡片完整率：0 / 0" in full_md
