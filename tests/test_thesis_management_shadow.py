"""R5/R7 PULLBACK_LIMIT 承接失败：无未来函数、方向、STALE 与 record-only 契约。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.playbook import pullback_failure as pf
from app.playbook import thesis_manager_shadow as shadow
from app.playbook.invalidation_contract import ACTION_REVIEW_ONLY, compile_pullback_failure_rule
from scripts import thesis_management_shadow_report as report

ET = pf.ET


def _window(end: str, *, high: float, low: float, close: float, open_: float | None = None):
    end_dt = datetime.strptime(end, "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    open_ = close if open_ is None else open_
    bars = []
    for index in range(5):
        ts = end_dt - timedelta(minutes=4 - index)
        bars.append({
            "time_key": ts.strftime("%Y-%m-%d %H:%M:00"),
            "open": open_ if index == 0 else close,
            "high": high,
            "low": low,
            "close": close,
        })
    return bars


def _long_bars():
    return (
        _window("2026-11-18 10:05", high=100.20, low=99.70, close=99.80)
        + _window("2026-11-18 10:10", high=99.95, low=99.50, close=99.60)
    )


def _evaluate(**overrides):
    kwargs = dict(
        direction="LONG", pivot=100.0, fill_price=100.4,
        entry_time="2026-11-18T09:55:00-05:00", bars_1m=_long_bars(),
        observed_at="2026-11-18T10:12:00-05:00",
    )
    kwargs.update(overrides)
    return pf.evaluate_pullback(**kwargs)


def test_long_failed_reclaim_uses_complete_windows_and_actual_known_at():
    result = _evaluate()
    assert result["decision"] == pf.FAILED_RECLAIM
    assert result["plan_status"] == pf.ACTIVE
    assert result["bar1"]["end"] == "2026-11-18 10:05:00"
    assert result["bar2"]["end"] == "2026-11-18 10:10:00"
    assert result["known_at"] == "2026-11-18T10:12:00-05:00"
    assert result["bar1"]["n_1m"] == result["bar2"]["n_1m"] == 5


def test_reclaim_or_no_lower_low_is_wait():
    reclaimed = _long_bars()
    for bar in reclaimed[-5:]:
        bar["high"] = 100.05
    assert _evaluate(bars_1m=reclaimed)["decision"] == pf.WAIT
    no_lower_low = _long_bars()
    for bar in no_lower_low[-5:]:
        bar["low"] = 99.75
    assert _evaluate(bars_1m=no_lower_low)["decision"] == pf.WAIT


def test_fill_window_is_fully_excluded_and_only_first_two_windows_are_used():
    bars = (
        _window("2026-11-18 09:40", high=99.95, low=99.50, close=99.60)
        + _window("2026-11-18 09:45", high=99.90, low=99.40, close=99.50)
        + _window("2026-11-18 09:50", high=99.80, low=99.30, close=99.40)
    )
    mid = _evaluate(entry_time="2026-11-18T09:36:10-05:00",
                    observed_at="2026-11-18T09:52:00-05:00", bars_1m=bars)
    assert mid["bar1"]["end"] == "2026-11-18 09:45:00"
    assert mid["bar2"]["end"] == "2026-11-18 09:50:00"
    # 09:40 整窗含 fill 前 tick，即使形态吻合也不能成为 bar1。
    only_contaminated = bars[:10]
    insufficient = _evaluate(entry_time="2026-11-18T09:36:10-05:00",
                             observed_at="2026-11-18T09:47:00-05:00", bars_1m=only_contaminated)
    assert insufficient["decision"] == pf.ABSTAIN
    assert insufficient["reason"] == "insufficient_complete_5m"
    boundary = _evaluate(entry_time="2026-11-18T09:35:00-05:00",
                         observed_at="2026-11-18T09:52:00-05:00", bars_1m=bars)
    assert boundary["bar1"]["end"] == "2026-11-18 09:40:00"


def test_later_rolling_windows_never_replace_fill_first_two():
    late_bars = (
        _window("2026-11-18 09:40", high=99.95, low=99.50, close=99.60)
        + _window("2026-11-18 09:45", high=99.90, low=99.40, close=99.50)
        + _window("2026-11-18 15:40", high=99.95, low=99.70, close=99.60)
        + _window("2026-11-18 15:45", high=99.90, low=99.50, close=99.50)
    )
    result = _evaluate(entry_time="2026-11-18T09:36:10-05:00",
                       observed_at="2026-11-18T15:47:00-05:00", bars_1m=late_bars)
    assert result["decision"] == pf.ABSTAIN
    assert result["reason"] == "nonconsecutive_5m"
    assert result["bar1"]["end"] == "2026-11-18 09:45:00"
    assert result["bar2"]["end"] == "2026-11-18 15:40:00"


def test_short_is_exact_mirror():
    bars = (
        _window("2026-11-18 10:05", high=100.30, low=99.90, close=100.20)
        + _window("2026-11-18 10:10", high=100.60, low=100.05, close=100.40)
    )
    result = _evaluate(direction="SHORT", bars_1m=bars)
    assert result["decision"] == pf.FAILED_RECLAIM
    # SHORT 只在 pivot 上方被接受才算失败；回落下去不属于本规则。
    for bar in bars[-5:]:
        bar["low"] = 99.95
    assert _evaluate(direction="SHORT", bars_1m=bars)["decision"] == pf.WAIT


def test_plan_gap_cross_day_and_stale_bars_are_abstain():
    assert _evaluate(fill_price=101.2)["reason"] == "plan_gap_stale"
    assert _evaluate(entry_time="2026-11-17T09:55:00-05:00")["reason"] == "cross_rth_day"
    assert _evaluate(observed_at="2026-11-18T10:40:00-05:00")["reason"] == "stale_5m"


def test_incomplete_window_never_becomes_a_decision_bar():
    bars = _long_bars()
    del bars[-1]  # 第二窗口只有 4 根，不能用前两个旧窗口凑信号。
    result = _evaluate(bars_1m=bars)
    assert result["decision"] == pf.ABSTAIN
    assert result["reason"] == "insufficient_complete_5m"


def test_future_bars_cannot_change_historical_decision():
    observed = "2026-11-18T10:12:00-05:00"
    before = _evaluate(observed_at=observed)
    future = _window("2026-11-18 11:00", high=120.0, low=119.0, close=120.0)
    after = _evaluate(bars_1m=_long_bars() + future, observed_at=observed)
    assert before == after


def test_fingerprint_excludes_quote_and_decision():
    result = _evaluate()
    assert pf.decision_fingerprint(result) == "ACTIVE|0.4|2026-11-18 10:05:00|2026-11-18 10:10:00"
    changed = dict(result, decision="OTHER", quote_price=88.0)
    assert pf.decision_fingerprint(changed) == pf.decision_fingerprint(result)


class _Cursor:
    def __init__(self, raw):
        self.raw = raw

    def fetchone(self):
        return [self.raw]


class _Conn:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, _sql, _params):
        if _sql.lstrip().upper().startswith("SELECT"):
            return _Cursor(self.raw)
        return None

    def commit(self):
        return None


def _manager_kwargs(tmp_path, raw=None):
    shadow._INSTRUCTION_CACHE.clear()
    raw = raw or {
        "entry_archetype": "PULLBACK_LIMIT",
        "confirmation": {"price_level": 100.0},
    }
    return dict(
        conn=_Conn(json.dumps(raw)),
        row={
            "id": 123, "symbol": "US.TEST", "direction": "LONG",
            "entry_price": 100.4, "entry_time": "2026-11-18T09:55:00-05:00",
        },
        meta={"playbook_instruction_id": 7, "stop_loss": 98.5},
        data_router=None,
        current_price=99.6,
        quote_source="test",
        old_exit_reason="",
        observed_at="2026-11-18T10:12:00-05:00",
        bars_1m=_long_bars(),
        path=tmp_path / "shadow.jsonl",
        forward_path=tmp_path / "path.jsonl",
    )


def test_record_only_writes_and_dedupes_across_metadata_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(shadow, "get_config", lambda: {"enabled": True, "max_plan_gap_pct": 1.0,
                                                        "max_bar_age_minutes": 15.0})
    kwargs = _manager_kwargs(tmp_path)
    first = shadow.record(**kwargs)
    assert first and first["decision"] == "FAILED_RECLAIM"
    assert first["execution"] == "RECORD_ONLY"
    assert first["contract_rule"]["action"] == ACTION_REVIEW_ONLY
    assert first["quote_age_s"] is None
    persisted = kwargs["meta"]["_thesis_shadow_last"]
    path_marker = kwargs["meta"]["_thesis_path_last_bar"]
    # 模拟重启：只带原 metadata，不能重复写同一决策或同一前向 bar。
    kwargs["meta"] = {"playbook_instruction_id": 7, "stop_loss": 98.5,
                      "_thesis_shadow_last": persisted,
                      "_thesis_path_last_bar": path_marker}
    assert shadow.record(**kwargs) is None
    rows = [json.loads(line) for line in (tmp_path / "shadow.jsonl").read_text().splitlines()]
    assert len(rows) == 1


def test_disabled_or_non_pullback_never_writes(monkeypatch, tmp_path):
    monkeypatch.setattr(shadow, "get_config", lambda: {"enabled": False})
    kwargs = _manager_kwargs(tmp_path)
    assert shadow.record(**kwargs) is None
    assert not (tmp_path / "shadow.jsonl").exists()
    monkeypatch.setattr(shadow, "get_config", lambda: {"enabled": True, "max_plan_gap_pct": 1.0,
                                                        "max_bar_age_minutes": 15.0})
    kwargs = _manager_kwargs(tmp_path, raw={"entry_archetype": "BREAKOUT"})
    assert shadow.record(**kwargs) is None


def test_forward_path_appends_only_new_post_fill_bars_and_persists_marker(monkeypatch, tmp_path):
    monkeypatch.setattr(shadow, "get_config", lambda: {"enabled": True, "max_plan_gap_pct": 1.0,
                                                        "max_bar_age_minutes": 15.0})
    kwargs = _manager_kwargs(tmp_path)
    first = shadow.record(**kwargs)
    assert first and not first.get("path_only")
    path = tmp_path / "path.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 10
    assert rows[0]["ingest"] == "initial_snapshot"
    assert rows[0]["known_at"] == "2026-11-18T10:12:00-05:00"
    # 新 bar 到达时，即使决策不变，也返回 path_only 以让外层持久化去重游标。
    kwargs["bars_1m"] = kwargs["bars_1m"] + _window(
        "2026-11-18 10:15", high=99.90, low=99.40, close=99.50)
    kwargs["observed_at"] = "2026-11-18T10:17:00-05:00"
    second = shadow.record(**kwargs)
    assert second and second.get("path_only") is True
    assert kwargs["meta"]["_thesis_path_last_bar"] == "2026-11-18 10:15:00"
    assert len(path.read_text().splitlines()) == 15
    # 同一批数据重放不会重复写。
    assert shadow.record(**kwargs) is None
    assert len(path.read_text().splitlines()) == 15


def test_blocked_exit_is_explicit_censored_and_deduped(monkeypatch, tmp_path):
    monkeypatch.setattr(shadow, "get_config", lambda: {"enabled": True})
    conn = _Conn("{}")
    row = {"id": 99, "symbol": "US.TEST", "direction": "LONG",
           "metadata_json": json.dumps({"playbook_instruction_id": 7})}
    blocked = {"intent_id": "intent-1", "state": "HELD", "created_at": 123.0}
    kwargs = dict(conn=conn, row=row, blocked=blocked,
                  observed_at=datetime(2026, 11, 18, 10, 12, tzinfo=ET),
                  path=tmp_path / "censored.jsonl")
    first = shadow.record_censored(**kwargs)
    assert first and first["decision"] == "CENSORED"
    assert first["reason"] == "blocked_exits"
    assert shadow.record_censored(**kwargs) is None
    assert len((tmp_path / "censored.jsonl").read_text().splitlines()) == 1
    assert "censored:intent-1" in row["metadata_json"]


def test_report_counts_mechanism_sample_without_pnl_claim(tmp_path):
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(shadow, "get_config", lambda: {"enabled": True, "max_plan_gap_pct": 1.0,
                                                        "max_bar_age_minutes": 15.0})
    kwargs = _manager_kwargs(tmp_path)
    shadow.record(**kwargs)
    summary = report.summarize(tmp_path / "shadow.jsonl")
    assert summary["failed_reclaim"]["unique_trades"] == 1
    assert summary["failed_reclaim"]["mechanism_sample_met"] is False
    assert "不得作盈利宣称" in summary["scope"]


def test_registry_counts_failed_reclaim_with_actual_key(tmp_path):
    from app.playbook.shadow_registry import REGISTRY, _count_samples
    item = next(item for item in REGISTRY if item.id == "thesis_management_shadow")
    assert item.count_mode == "distinct_where:decision=FAILED_RECLAIM"
    assert item.config_key == "playbook.thesis_management_shadow.enabled"
    target = tmp_path / "data" / "shadow" / "thesis_management_shadow.jsonl"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps({"schema_version": "thesis_management_shadow_v1",
                    "decision": "FAILED_RECLAIM", "inst_id": 7}) + "\n"
        + json.dumps({"schema_version": "thesis_management_shadow_v1",
                      "decision": "FAILED_RECLAIM", "inst_id": 7}) + "\n"
        + json.dumps({"schema_version": "thesis_management_shadow_v1",
                      "decision": "WAIT", "inst_id": 8}) + "\n",
        encoding="utf-8",
    )
    assert _count_samples(item, tmp_path) == 1


def test_pullback_contract_rule_is_review_only_and_direction_aware():
    long_rule = compile_pullback_failure_rule(
        direction="LONG", pivot=100, known_at="2026-11-18T10:12:00-05:00")
    short_rule = compile_pullback_failure_rule(
        direction="SHORT", pivot=100, known_at="2026-11-18T10:12:00-05:00")
    assert long_rule["operator"] == "<" and short_rule["operator"] == ">"
    assert long_rule["unit"] == "price" and long_rule["timeframe"] == "5m"
    assert long_rule["action"] == short_rule["action"] == ACTION_REVIEW_ONLY
    with pytest.raises(ValueError):
        compile_pullback_failure_rule(direction="LONG", pivot=-1, known_at="x")
