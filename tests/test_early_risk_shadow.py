"""RFC-203 早期风险 record-only 影子 — 单测钉子。

守四件事:
  1. 候选条件本身 (grace extreme+2%/5min, anchor 方向性 3%, queue latency>0);
  2. record-only 纪律 (would_skip/不拦截/不退出, TEST 符号不写, 去重, 异常不抛);
  3. JSONL 字段足以离线复核 (planned/effective/current/drift, vol_bucket/atr/mfe);
  4. evaluator 只有 OK/NO_DATA 两态且明示非反事实 PnL; registry 为永久测量项。
"""
import importlib.util
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.playbook import early_risk_shadow as ers  # noqa: E402
from app.playbook.shadow_registry import REGISTRY, ShadowStatus  # noqa: E402

NOW = datetime(2026, 11, 13, 10, 0, 0, tzinfo=timezone.utc)
LOG = logging.getLogger("test_early_risk_shadow")


class _Cfg:
    """最小 PlaybookConfig 替身: 扁平 get + horizon vol_buckets。"""

    def __init__(self, data=None, vol_buckets=None):
        self._data = dict(data or {})
        self._vol_buckets = dict(vol_buckets or {})

    def get(self, key, default=None):
        return self._data.get(key, default)

    def get_horizon_params(self, horizon):
        if self._vol_buckets:
            return {"vol_buckets": self._vol_buckets}
        return {}


@pytest.fixture(autouse=True)
def _isolated_dedup():
    ers.clear_dedup_for_tests()
    yield
    ers.clear_dedup_for_tests()


def _read(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


# ── 开关 / TEST 符号 ───────────────────────────────────────────────────


def test_enabled_defaults_true_and_false_switch(monkeypatch):
    monkeypatch.setattr(ers, "_get_pb_cfg", lambda pb_cfg=None: _Cfg({}))
    assert ers.enabled() is True

    monkeypatch.setattr(
        ers, "_get_pb_cfg",
        lambda pb_cfg=None: _Cfg({"early_risk_shadow_enabled": False}))
    assert ers.enabled() is False


@pytest.mark.parametrize("symbol", ["US.TEST", "TEST", "us.test.abc"])
def test_test_symbols_never_written(tmp_path, symbol):
    target = tmp_path / "shadow.jsonl"
    wrote = ers.record_grace_candidate(
        meta={"vol_bucket": "extreme", "atr_pct": 0.09},
        symbol=symbol, direction="LONG", entry_price=100, current_price=97,
        stop_loss=95, held_minutes=1, now_et=NOW,
        pb_cfg=_Cfg({}), logger=LOG, path=target)
    assert wrote is False
    assert not target.exists()


def test_mom_swing_positions_do_not_pollute_da_grace_cohort(tmp_path):
    target = tmp_path / "shadow.jsonl"
    wrote = ers.record_grace_candidate(
        meta={"vol_bucket": "extreme", "atr_pct": 0.09},
        symbol="US.QQQ", direction="LONG", entry_price=100, current_price=97,
        stop_loss=95, held_minutes=1, now_et=NOW,
        decision_id="MOM-ETF-V1-20261113-US.QQQ",
        pb_cfg=_Cfg({}), logger=LOG, path=target)
    assert wrote is False
    assert not target.exists()


# ── grace_candidate ────────────────────────────────────────────────────


def test_grace_extreme_disaster_writes_and_dedups(tmp_path):
    target = tmp_path / "shadow.jsonl"
    meta = {"vol_bucket": "extreme", "atr_pct": 0.09, "mfe_pct": 0.3, "mae_pct": 0.4}
    kwargs = dict(
        meta=meta, symbol="US.NVDA", direction="LONG", entry_price=100.0,
        current_price=97.5, stop_loss=95.0, held_minutes=2.5, now_et=NOW,
        trade_id=123, inst_id=456, decision_id="DA-1",
        pb_cfg=_Cfg({}), logger=LOG, path=target)

    assert ers.record_grace_candidate(**kwargs) is True
    assert ers.record_grace_candidate(**kwargs) is False  # 进程内 (date,id,rule) 去重

    rows = _read(target)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "grace_candidate"
    assert row["vol_bucket"] == "extreme"
    assert row["extreme_verified"] is True
    assert row["vol_bucket_source"] == "meta"
    assert row["entry_price"] == 100.0
    assert row["current_price"] == 97.5
    assert row["stop_loss"] == 95.0
    assert row["held_minutes"] == 2.5
    assert row["atr_pct"] == 9.0
    assert row["mfe_pct"] == 0.3
    assert row["adverse_pct"] == 2.5
    assert row["disaster_line_pct"] == 2.0
    assert row["would_exit"] is False
    assert row["execution_blocked"] is False
    assert row["decision_id"] == "DA-1"
    assert "不是生产规则" in row["caveat"]


def test_grace_requires_extreme_and_five_minutes_and_two_percent(tmp_path):
    target = tmp_path / "shadow.jsonl"
    base = dict(symbol="US.AMD", direction="LONG", entry_price=100.0,
                current_price=97.0, stop_loss=95.0, now_et=NOW,
                pb_cfg=_Cfg({}), logger=LOG, path=target)

    assert ers.record_grace_candidate(
        meta={"vol_bucket": "high", "atr_pct": 0.05}, held_minutes=1, **base) is False
    assert ers.record_grace_candidate(
        meta={"vol_bucket": "extreme", "atr_pct": 0.09}, held_minutes=5.0, **base) is False
    assert ers.record_grace_candidate(
        meta={"vol_bucket": "extreme", "atr_pct": 0.09}, held_minutes=1,
        **{**base, "current_price": 98.1}) is False  # 1.9% < 2%
    assert not target.exists()


def test_grace_missing_bucket_is_unknown_not_extreme(tmp_path):
    target = tmp_path / "shadow.jsonl"
    bucket, source = ers.resolve_vol_bucket({}, entry_price=100.0, pb_cfg=_Cfg({}))
    assert (bucket, source) == ("unknown", "missing")

    wrote = ers.record_grace_candidate(
        meta={}, symbol="US.AMD", direction="LONG", entry_price=100.0,
        current_price=97.0, stop_loss=95.0, held_minutes=1, now_et=NOW,
        pb_cfg=_Cfg({}), logger=LOG, path=target)
    assert wrote is False
    assert not target.exists()


def test_grace_bucket_can_be_derived_from_existing_atr_config(tmp_path):
    """缺 meta.vol_bucket 时按现有 vol_buckets 配置从 ATR 定桶 (复用生产口径)。"""
    target = tmp_path / "shadow.jsonl"
    cfg = _Cfg({}, vol_buckets={
        "low": {"atr_pct_max": 0.015},
        "medium": {"atr_pct_max": 0.035},
        "high": {"atr_pct_max": 0.08},
        "extreme": {"atr_pct_max": 999.0},
    })
    wrote = ers.record_grace_candidate(
        meta={"attribution": {"atr_pct": 0.09}}, symbol="US.MU", direction="SHORT",
        entry_price=100.0, current_price=102.5, stop_loss=105.0, held_minutes=1.5,
        now_et=NOW, pb_cfg=cfg, logger=LOG, path=target)
    assert wrote is True
    row = _read(target)[0]
    assert row["vol_bucket"] == "extreme"
    assert row["vol_bucket_source"] == "derived_from_atr"
    assert row["direction"] == "SHORT"
    assert row["adverse_pct"] == 2.5


# ── anchor_candidate ───────────────────────────────────────────────────


def test_anchor_drift_is_direction_aware():
    assert ers.compute_anchor_drift_pct("LONG", 100, 103.1) == 3.1
    assert ers.compute_anchor_drift_pct("LONG", 100, 96.9) == -3.1
    assert ers.compute_anchor_drift_pct("SHORT", 100, 96.9) == 3.1
    assert ers.compute_anchor_drift_pct("SHORT", 100, 103.1) == -3.1
    assert ers.compute_anchor_drift_pct("LONG", 0, 103.1) is None


def test_anchor_candidate_writes_only_above_three_percent(tmp_path):
    target = tmp_path / "shadow.jsonl"
    common = dict(symbol="US.GS", direction="LONG", planned_entry=100.0,
                  planned_entry_source="planned_entry", current_price=103.5,
                  now_et=NOW, inst_id=77, decision_id="DA-9",
                  pb_cfg=_Cfg({}), logger=LOG, path=target)

    assert ers.record_anchor_candidate(effective_entry=103.0, **common) is False  # =3% 不写
    assert ers.record_anchor_candidate(effective_entry=103.01, **common) is True
    assert ers.record_anchor_candidate(effective_entry=104.0, **common) is False  # 去重

    rows = _read(target)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "anchor_candidate"
    assert row["planned_entry"] == 100.0
    assert row["effective_entry"] == 103.01
    assert row["current_price"] == 103.5
    assert row["drift_pct"] == 3.01
    assert row["threshold_pct"] == 3.0
    assert row["would_skip"] is True
    assert row["execution_blocked"] is False
    assert row["decision_id"] == "DA-9"


def test_anchor_short_side_drift_writes(tmp_path):
    target = tmp_path / "shadow.jsonl"
    wrote = ers.record_anchor_candidate(
        symbol="US.TSLA", direction="SHORT", planned_entry=100.0,
        planned_entry_source="band_edge", effective_entry=96.5,
        current_price=96.6, now_et=NOW, inst_id=88,
        pb_cfg=_Cfg({}), logger=LOG, path=target)
    assert wrote is True
    row = _read(target)[0]
    assert row["direction"] == "SHORT"
    assert row["drift_pct"] == 3.5
    assert row["planned_entry_source"] == "band_edge"


# ── queue_candidate ────────────────────────────────────────────────────


def test_queue_candidate_records_positive_latency_only(tmp_path):
    target = tmp_path / "shadow.jsonl"
    base = dict(symbol="US.DPC", direction="BUY", inst_id=91, decision_id="DA-2",
                now_et=NOW, pb_cfg=_Cfg({}), logger=LOG, path=target)

    assert ers.record_queue_candidate(
        confirmed_at="2026-11-13T09:59:55+00:00",
        execution_time="2026-11-13T10:00:00+00:00",
        arm_delay_actual_sec=120, **base) is True
    assert ers.record_queue_candidate(
        confirmed_at="2026-11-13T10:00:00+00:00",
        execution_time="2026-11-13T10:00:00+00:00", **base) is False  # 0s 不写
    assert ers.record_queue_candidate(
        confirmed_at="", execution_time="2026-11-13T10:00:00+00:00", **base) is False

    rows = _read(target)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "queue_candidate"
    assert row["latency_sec"] == 5.0
    assert row["arm_delay_actual_sec"] == 120
    assert row["would_defer_order"] is False
    assert row["execution_blocked"] is False
    assert row["direction"] == "LONG"


def test_queue_candidate_accepts_explicit_latency(tmp_path):
    target = tmp_path / "shadow.jsonl"
    assert ers.record_queue_candidate(
        symbol="US.UBER", direction="LONG", confirmed_at="x", execution_time="y",
        latency_sec=12.5, now_et=NOW, inst_id=92,
        pb_cfg=_Cfg({}), logger=LOG, path=target) is True
    assert _read(target)[0]["latency_sec"] == 12.5


# ── 异常纪律 / 接线钉子 ───────────────────────────────────────────────


def test_append_failure_warns_with_traceback_and_does_not_raise(monkeypatch, tmp_path, caplog):
    class _BrokenOS:
        @staticmethod
        def open(*args, **kwargs):
            raise OSError("disk full")

    monkeypatch.setattr(ers, "os", _BrokenOS)
    caplog.set_level(logging.WARNING, logger="test_early_risk_shadow")
    ok = ers.append_row({"kind": "anchor_candidate", "symbol": "US.X"}, path=tmp_path / "x.jsonl",
                        logger=LOG)
    assert ok is False
    assert any("JSONL 写盘失败" in rec.message for rec in caplog.records)
    assert any(rec.exc_info for rec in caplog.records)


def test_writers_are_wired_into_production_paths():
    exit_src = (ROOT / "app" / "playbook" / "pipeline_parts" /
                "exit_monitor_flow.py").read_text(encoding="utf-8")
    exec_src = (ROOT / "app" / "playbook" / "pipeline_parts" /
                "auto_execute_flow.py").read_text(encoding="utf-8")
    assert "record_grace_candidate" in exit_src
    assert "record_anchor_candidate" in exec_src
    assert "record_queue_candidate" in exec_src


# ── registry / evaluator ───────────────────────────────────────────────


def test_registry_entry_is_permanent_measure_and_on_demand():
    item = next(s for s in REGISTRY if s.id == "early_risk_shadow")
    assert item.status is ShadowStatus.OBSERVING
    assert item.data_path == "data/early_risk_shadow.jsonl"
    assert item.min_n == 30
    assert item.eval_on_demand is True
    assert item.eval_script == "scripts/early_risk_shadow_eval.py"
    assert "PERMANENT_MEASURE" in item.no_adjudication_reason
    assert "PERMANENT_MEASURE" in item.graduation_criteria
    assert "10 个 ET 交易日" in item.graduation_criteria
    assert "停摆检查" in item.notes


def _load_eval_module():
    spec = importlib.util.spec_from_file_location(
        "early_risk_shadow_eval_under_test",
        ROOT / "scripts" / "early_risk_shadow_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_evaluator_summarizes_by_day_rule_symbol(tmp_path):
    ev = _load_eval_module()
    target = tmp_path / "shadow.jsonl"
    rows = [
        {"date": "2026-11-13", "kind": "grace_candidate", "symbol": "NVDA"},
        {"date": "2026-11-13", "kind": "grace_candidate", "symbol": "AMD"},
        {"date": "2026-11-13", "kind": "anchor_candidate", "symbol": "GS"},
        {"date": "2026-11-12", "kind": "queue_candidate", "symbol": "NVDA"},
    ]
    target.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    result = ev.evaluate(target)
    assert result["state"] == ev.STATE_OK
    assert result["n_rows"] == 4
    assert result["n_days"] == 2
    assert result["by_rule"]["grace_candidate"]["n"] == 2
    assert result["by_rule"]["grace_candidate"]["affected_symbols"] == ["AMD", "NVDA"]
    assert result["by_symbol"]["NVDA"]["n"] == 2
    day = next(d for d in result["days"] if d["date"] == "2026-11-13")
    assert day["by_rule"] == {"anchor_candidate": 1, "grace_candidate": 2}
    assert any("反事实" in c for c in result["caveats"])


def test_evaluator_no_data_states(tmp_path):
    ev = _load_eval_module()
    missing = ev.evaluate(tmp_path / "missing.jsonl")
    assert missing["state"] == ev.STATE_NO_DATA
    assert missing["n_rows"] == 0

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert ev.evaluate(empty)["state"] == ev.STATE_NO_DATA

    bad_only = tmp_path / "bad.jsonl"
    bad_only.write_text("{not json}\n", encoding="utf-8")
    result = ev.evaluate(bad_only)
    assert result["state"] == ev.STATE_NO_DATA
    assert result["n_bad_lines"] == 1

    valid = tmp_path / "valid.jsonl"
    valid.write_text('{"date":"2026-11-13","kind":"anchor_candidate","symbol":"GS"}\n',
                     encoding="utf-8")
    assert ev.evaluate(valid, date="2026-11-12")["state"] == ev.STATE_NO_DATA
    assert ev.evaluate(valid, date="2026-11-13")["state"] == ev.STATE_OK