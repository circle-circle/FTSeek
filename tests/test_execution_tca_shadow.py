"""RFC-152 Phase 0: 执行 TCA 台账 + 点差/报价年龄只读影子契约测试。

钉住:
  ① 单位/方向归一 (正=不利) 与点差计算 (proxy/交叉/缺失不伪造);
  ② 计划锚优先级 (raw.planned_entry > confirmation.price_level > missing);
  ③ 影子关闭 / TEST 标的零写入; 正常路径 append-only 一行;
  ④ 度量失败绝不上抛 (不得把度量故障变成交易故障);
  ⑤ 报表分桶 + 日聚类 t 口径 (n_days<3 不可判)。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import app.playbook.execution_tca_shadow as tca
import scripts.execution_tca_report as report_mod

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 20, 10, 5, 0, tzinfo=ET)


def _inst(**overrides):
    inst = {
        "id": 101,
        "decision_id": "DA-20261020-abc",
        "symbol": "US.AAPL",
        "direction": "BUY",
        "entry_price": 100.0,
        "raw_instruction_json": json.dumps({
            "planned_entry": 99.0,
            "entry_archetype": "pullback_limit",
            "confirmation": {"price_level": "98.5"},
        }),
    }
    inst.update(overrides)
    return inst


def _ctx(**overrides):
    ctx = {
        "price": 100.0,
        "lv3_bid": 99.9,
        "lv3_ask": 100.1,
        "lv3_quote_age_ms": 120,
        "lv3_book_is_proxy": False,
    }
    ctx.update(overrides)
    return ctx


def _row(**overrides):
    kwargs = dict(
        inst=_inst(),
        direction="BUY",
        planned_entry=99.0,
        planned_entry_source="raw.planned_entry",
        execution_entry=100.0,
        ctx=_ctx(),
        quote_source="reprice_ctx",
        now_et=NOW,
        size=10,
        notional=1000.0,
        atr_pct=2.5,
        cfg={"enabled": True, "spread_block_bps": 20.0,
             "quote_age_block_ms": 2000},
    )
    kwargs.update(overrides)
    return tca.build_attempt_row(**kwargs)


# ── ① 点差 / 方向归一 / 报价提取 ──────────────────────────────────────

def test_spread_bps_valid_and_invalid():
    assert tca.compute_spread_bps(99.9, 100.1) == pytest.approx(20.0)
    assert tca.compute_spread_bps(100.0, 100.0) == 0.0
    assert tca.compute_spread_bps(None, 100.0) is None
    assert tca.compute_spread_bps(101.0, 100.0) is None      # 交叉
    assert tca.compute_spread_bps(0, 100.0) is None
    assert tca.compute_spread_bps(99.9, 100.1, book_is_proxy=True) is None


def test_deviation_bps_direction_normalized():
    # 正 = 对交易更不利
    assert tca.deviation_bps("BUY", 100.0, 101.0) == pytest.approx(100.0)
    assert tca.deviation_bps("SHORT", 100.0, 99.0) == pytest.approx(100.0)
    assert tca.deviation_bps("BUY", 100.0, 99.0) == pytest.approx(-100.0)
    assert tca.deviation_bps("SHORT", 100.0, 101.0) == pytest.approx(-100.0)
    assert tca.deviation_bps("BUY", None, 101.0) is None


def test_extract_quote_proxy_and_missing():
    proxy = tca.extract_quote(_ctx(lv3_book_is_proxy=True), "reprice_ctx")
    assert proxy["book_is_proxy"] is True
    assert proxy["spread_bps"] is None
    assert proxy["mid"] is None
    missing = tca.extract_quote(None, "missing")
    assert missing["quote_source"] == "missing"
    assert missing["bid"] is None and missing["quote_age_ms"] is None


# ── ② 计划锚优先级 ──────────────────────────────────────────────────

def test_planned_entry_priority_and_missing():
    value, source = tca.resolve_planned_entry(_inst())
    assert (value, source) == (99.0, "raw.planned_entry")
    inst = _inst(raw_instruction_json=json.dumps({"confirmation": {"price_level": "1,234.5"}}))
    value, source = tca.resolve_planned_entry(inst)
    assert (value, source) == (1234.5, "confirmation.price_level")
    value, source = tca.resolve_planned_entry(
        _inst(raw_instruction_json="{}", entry_price=100.0))
    assert (value, source) == (None, "missing")


# ── ③ 开关 / TEST 守卫 / 行字段与单位 ─────────────────────────────────

def test_disabled_and_test_symbol_do_not_build():
    disabled = _row(cfg={"enabled": False, "spread_block_bps": 20.0,
                         "quote_age_block_ms": 2000})
    assert disabled is None
    test_symbol = _row(inst=_inst(symbol="US.TESTAAPL"))
    assert test_symbol is None


def test_build_row_units_and_provisional_labels():
    row = _row(ctx=_ctx(lv3_bid=99.8, lv3_ask=100.2, lv3_quote_age_ms=2500))
    assert row is not None
    assert row["schema"] == "execution_tca_v1"
    assert row["source"] == "DA"
    assert row["direction"] == "LONG"
    assert row["entry_archetype"] == "PULLBACK_LIMIT"
    assert row["spread_bps"] == pytest.approx(40.0)       # bps
    assert row["quote_age_ms"] == 2500                    # ms
    assert row["session_min"] == 35                       # 距 09:30
    assert row["atr_pct"] == 2.5                          # 百分数, 非分数
    assert row["planned_to_exec_bps"] == pytest.approx(101.0101, rel=1e-4)
    assert row["provisional_would_block"] is True
    assert row["provisional_block_reasons"] == ["spread", "quote_age"]


def test_finalize_writes_one_line_and_fill_deviation(tmp_path, monkeypatch):
    monkeypatch.setattr(tca, "_TCA_PATH", tmp_path / "execution_tca.jsonl")
    row = _row()
    assert row["success"] is None
    ok = tca.finalize_and_record(
        row, result={"success": True, "execution_price": 101.0})
    assert ok is True
    lines = (tmp_path / "execution_tca.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    saved = json.loads(lines[0])
    assert saved["success"] is True and saved["executed"] is True
    assert saved["fill_price"] == 101.0
    assert saved["exec_to_fill_bps"] == pytest.approx(100.0)       # LONG 更高 = 不利
    assert saved["planned_to_fill_bps"] == pytest.approx(202.0202, rel=1e-4)


def test_finalize_failure_and_exception_never_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(tca, "_TCA_PATH", tmp_path / "execution_tca.jsonl")
    row = _row()
    assert tca.finalize_and_record(
        row, result={"success": False, "message": "no fill"}) is True
    saved = json.loads((tmp_path / "execution_tca.jsonl").read_text().splitlines()[0])
    assert saved["success"] is False and saved["message"] == "no fill"
    # 写盘失败只 warn, 不上抛
    monkeypatch.setattr(tca, "_TCA_PATH", tmp_path / "no_dir" / "x.jsonl")
    (tmp_path / "no_dir").write_text("not a directory")
    assert tca.finalize_and_record(_row(), error="boom") is False


# ── ④ 报表分桶 / 日聚类口径 ──────────────────────────────────────────

def test_report_buckets_and_day_clustered():
    assert report_mod._spread_bucket(4.9) == "[0,5)"
    assert report_mod._spread_bucket(5.0) == "[5,10)"
    assert report_mod._spread_bucket(None) == "missing"
    assert report_mod._age_bucket(0) == "<500ms"
    assert report_mod._age_bucket(500) == "500-2000ms"
    assert report_mod._age_bucket(2000) == ">=2000ms"
    assert report_mod._day_clustered({"a": [1.0], "b": [2.0]}) is None
    clustered = report_mod._day_clustered({"a": [1.0], "b": [2.0], "c": [3.0]})
    assert clustered["n_days"] == 3
    assert clustered["day_mean_bps"] == pytest.approx(2.0)
    assert clustered["t"] == pytest.approx(3.464, rel=1e-2)


def _write_synthetic(tmp_path):
    jsonl = tmp_path / "execution_tca.jsonl"
    rows = [
        {"schema": "execution_tca_v1", "date": "2026-10-20", "decision_id": "d1",
         "success": True, "fill_price": 100.0, "spread_bps": 3.0,
         "quote_age_ms": 100, "planned_entry_source": "raw.planned_entry",
         "book_is_proxy": False, "provisional_would_block": False,
         "planned_to_exec_bps": 10.0, "exec_to_fill_bps": 5.0,
         "planned_to_fill_bps": 15.0, "source": "DA",
         "entry_archetype": "PULLBACK_LIMIT"},
        {"schema": "execution_tca_v1", "date": "2026-10-21", "decision_id": "d2",
         "success": True, "fill_price": 100.0, "spread_bps": 12.0,
         "quote_age_ms": 800, "planned_entry_source": "raw.planned_entry",
         "book_is_proxy": False, "provisional_would_block": False,
         "planned_to_exec_bps": 20.0, "exec_to_fill_bps": 10.0,
         "planned_to_fill_bps": 30.0, "source": "UE",
         "entry_archetype": "FADE_SHORT"},
        {"schema": "execution_tca_v1", "date": "2026-10-22", "decision_id": "d3",
         "success": True, "fill_price": 100.0, "spread_bps": 60.0,
         "quote_age_ms": 3000, "planned_entry_source": "missing",
         "book_is_proxy": False, "provisional_would_block": True,
         "planned_to_exec_bps": 30.0, "exec_to_fill_bps": 15.0,
         "planned_to_fill_bps": 45.0, "source": "OTHER",
         "entry_archetype": None},
    ]
    jsonl.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    db = tmp_path / "tca.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE trade_records (decision_id TEXT, trade_date TEXT, symbol TEXT, "
        "direction TEXT, pnl_net REAL, realized_pnl REAL, entry_price REAL, "
        "quantity REAL, exit_reason TEXT)")
    con.executemany(
        "INSERT INTO trade_records VALUES (?,?,?,?,?,?,?,?,?)",
        [("d1", "2026-10-20", "US.AAPL", "LONG", 10.0, None, 100.0, 10.0, "TP"),
         ("d2", "2026-10-21", "US.MSFT", "SHORT", -20.0, None, 100.0, 10.0, "SL"),
         ("d3", "2026-10-22", "US.NVDA", "LONG", 30.0, None, 100.0, 10.0, "EOD")])
    con.commit()
    con.close()
    return jsonl, db


def test_report_end_to_end_synthetic(tmp_path):
    jsonl, db = _write_synthetic(tmp_path)
    rows = report_mod.load_rows(jsonl, "2026-10-20")
    assert len(rows) == 3
    outcomes = report_mod.load_outcomes(db, {r["decision_id"] for r in rows})
    assert outcomes["d1"]["net_bps"] == pytest.approx(100.0)
    assert outcomes["d2"]["net_bps"] == pytest.approx(-200.0)
    result = report_mod.compute(rows, outcomes)
    assert result["sample"]["attempts_n"] == 3
    assert result["sample"]["filled_n"] == 3
    assert result["sample"]["joined_n"] == 3
    assert result["sample"]["gate_passed"] is False       # 3 日 < 10
    assert result["spread_buckets"]["[0,5)"]["n"] == 1
    assert result["spread_buckets"]["[10,20)"]["n"] == 1
    assert result["spread_buckets"][">=50"]["n"] == 1
    assert result["provisional_gate"]["would_block"]["n"] == 1
    assert result["provisional_gate"]["pass"]["n"] == 2
    assert result["implementation_shortfall"]["exec_to_fill_bps"]["mean_bps"] == pytest.approx(10.0)
