"""守卫: 出场意图冻结期影子 (2026-10-17 审计 P0-1 ④).

钉住三件事:
  1. 写方快照字段/节流/永不抛异常 (零行为变更的 record-only 承诺);
  2. 聚合器的方向感知不利位移与 SL 兜底触发口径 (定 N/M 的证据面);
  3. 注册表登记 + pipeline 侧 blocked 明细/年龄告警去重。
"""
from __future__ import annotations

import importlib.util
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import app.playbook.blocked_exit_opportunity as shadow
import app.playbook.pipeline as pipeline

REPO = Path(__file__).resolve().parents[1]
AGG_PATH = REPO / "scripts" / "blocked_exit_opportunity_shadow.py"


@pytest.fixture(autouse=True)
def _clear_throttle():
    shadow._LAST_EMIT.clear()
    yield
    shadow._LAST_EMIT.clear()


def _load_aggregator():
    spec = importlib.util.spec_from_file_location("blocked_exit_agg", AGG_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(**overrides):
    row = {
        "symbol": "US.ACN",
        "direction": "LONG",
        "entry_price": 200.0,
        "metadata_json": json.dumps({"stop_loss": 196.0}),
    }
    row.update(overrides)
    return row


def _blocked(**overrides):
    blocked = {
        "target_env": "sim",
        "symbol": "US.ACN",
        "intent_id": "i-1",
        "state": "HELD",
        "error": "exit_eth_partial_fill_held",
        "created_at": 1_000.0,
    }
    blocked.update(overrides)
    return blocked


# ── 写方: build_snapshot ────────────────────────────────────────────────────

def test_snapshot_captures_price_stop_and_age():
    event = shadow.build_snapshot(
        _row(), _blocked(), {"last": 190.5}, now=1_600.0)
    assert event["schema_version"] == "blocked_exit_opportunity_v1"
    assert event["symbol"] == "US.ACN"
    assert event["direction"] == "LONG"
    assert event["price"] == 190.5
    assert event["stop_loss"] == 196.0
    assert event["intent_id"] == "i-1"
    assert event["intent_state"] == "HELD"
    assert event["intent_age_s"] == 600.0
    assert event["date"] == datetime.fromtimestamp(
        1_600.0, timezone.utc).strftime("%Y-%m-%d")


def test_snapshot_price_accepts_price_key_and_missing_quote():
    assert shadow.build_snapshot(
        _row(), _blocked(), {"price": 123.0})["price"] == 123.0
    event = shadow.build_snapshot(_row(), _blocked(), None)
    assert event["price"] is None
    assert event["intent_age_s"] is None or event["intent_age_s"] >= 0


def test_snapshot_bad_metadata_does_not_raise():
    event = shadow.build_snapshot(
        _row(metadata_json="{not json"), _blocked(), None)
    assert event["stop_loss"] is None


# ── 写方: observe 节流/落盘/容错 ─────────────────────────────────────────────

def test_observe_throttles_same_intent_and_writes_jsonl(tmp_path):
    out = tmp_path / "shadow.jsonl"
    assert shadow.observe(_row(), _blocked(), {"last": 190.0},
                          now=1_000.0, path=out) is True
    assert shadow.observe(_row(), _blocked(), {"last": 189.0},
                          now=1_030.0, path=out) is False
    assert shadow.observe(_row(), _blocked(), {"last": 188.0},
                          now=1_061.0, path=out) is True
    lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert [ln["price"] for ln in lines] == [190.0, 188.0]
    assert all(ln["schema_version"] == "blocked_exit_opportunity_v1"
               for ln in lines)


def test_observe_keys_throttle_by_intent(tmp_path):
    out = tmp_path / "shadow.jsonl"
    assert shadow.observe(_row(), _blocked(), None, now=1_000.0, path=out)
    assert shadow.observe(_row(), _blocked(intent_id="i-2"), None,
                          now=1_000.0, path=out)
    assert len(out.read_text(encoding="utf-8").splitlines()) == 2


def test_observe_never_raises_when_path_unwritable(tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    assert shadow.observe(_row(), _blocked(), None, path=blocker / "a.jsonl") is False


# ── 聚合器 ─────────────────────────────────────────────────────────────────

def _snap(ts, price, *, intent_id="i-1", direction="LONG", stop=96.0,
          created=1_000.0, state="HELD", error=""):
    return {
        "ts": ts, "symbol": "US.ACN", "target_env": "sim",
        "direction": direction, "entry_price": 100.0, "price": price,
        "stop_loss": stop, "intent_id": intent_id, "intent_state": state,
        "intent_error": error, "intent_created_at": created,
        "intent_age_s": 60.0,
    }


def test_aggregate_long_adverse_and_stop_breach():
    agg = _load_aggregator()
    rows = [
        _snap("2026-10-16T13:30:00+00:00", 100.0),
        _snap("2026-10-16T13:32:00+00:00", 95.0),
    ]
    episodes = agg.aggregate(rows)
    assert len(episodes) == 1
    ep = episodes[0]
    assert ep["n_observations"] == 2
    assert ep["blocked_seconds"] == 120.0
    assert ep["first_price"] == 100.0
    assert ep["last_price"] == 95.0
    assert ep["min_price"] == 95.0
    assert ep["adverse_move_pct"] == -5.0
    assert ep["stop_breached"] is True


def test_aggregate_short_uses_high_extreme():
    agg = _load_aggregator()
    rows = [
        _snap("2026-10-16T13:30:00+00:00", 100.0, direction="SELL_SHORT", stop=103.0),
        _snap("2026-10-16T13:31:00+00:00", 104.0, direction="SELL_SHORT", stop=103.0),
    ]
    ep = agg.aggregate(rows)[0]
    assert ep["adverse_move_pct"] == -4.0
    assert ep["stop_breached"] is True


def test_aggregate_no_breach_when_stop_not_crossed():
    agg = _load_aggregator()
    rows = [
        _snap("2026-10-16T13:30:00+00:00", 100.0, direction="SHORT", stop=105.0),
        _snap("2026-10-16T13:31:00+00:00", 104.0, direction="SHORT", stop=105.0),
    ]
    ep = agg.aggregate(rows)[0]
    assert ep["stop_breached"] is False


def test_aggregate_groups_intents_and_skips_bad_rows():
    agg = _load_aggregator()
    rows = [
        _snap("2026-10-16T13:30:00+00:00", 100.0),
        _snap("2026-10-16T13:31:00+00:00", 99.0, intent_id="i-2"),
        {"ts": "not-a-ts", "intent_id": "bad"},
    ]
    episodes = agg.aggregate(rows)
    assert {e["intent_id"] for e in episodes} == {"i-1", "i-2"}
    assert len(episodes) == 2


def test_aggregate_main_writes_latest(tmp_path, monkeypatch):
    agg = _load_aggregator()
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps(_snap("2026-10-16T13:30:00+00:00", 100.0)) + "\n"
        + "{bad json\n",
        encoding="utf-8",
    )
    out = tmp_path / "latest.json"
    monkeypatch.setattr(agg, "LEDGER", ledger)
    monkeypatch.setattr(agg, "OUT", out)
    monkeypatch.setattr(agg, "ROOT", tmp_path)
    assert agg.main() == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["n_observations"] == 1
    assert payload["n_bad_lines"] == 1
    assert payload["n_episodes"] == 1
    assert payload["stop_breach"]["n_episodes"] == 0
    assert payload["stop_breach"]["ratio"] == 0.0


# ── 注册表 ─────────────────────────────────────────────────────────────────

def test_registry_entry_registered_and_axis_pinned():
    from app.playbook.shadow_registry import REGISTRY, ShadowStatus

    item = next((s for s in REGISTRY if s.id == "blocked_exit_opportunity"), None)
    assert item is not None, "blocked_exit_opportunity 未注册 ⇒ 周检/哨兵看不见"
    assert item.status == ShadowStatus.OBSERVING
    assert item.data_path == "data/shadow/blocked_exit_opportunity.jsonl"
    assert item.count_mode == "distinct:intent_id"
    assert item.min_n == 30
    assert item.eval_script == "scripts/blocked_exit_opportunity_shadow.py"
    assert item.eval_on_demand is True
    assert (REPO / item.eval_script).exists()


# ── pipeline: blocked 明细 + 年龄告警 ───────────────────────────────────────

class _RecorderHub:
    def __init__(self):
        self.calls = []

    def push_text(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return True


@pytest.fixture
def push_recorder(monkeypatch):
    hub = _RecorderHub()
    import app.push
    monkeypatch.setattr(app.push, "get_push_hub", lambda: hub)
    monkeypatch.setattr(pipeline, "_blocked_exit_age_alerted", set())
    return hub


def test_age_alert_under_30min_is_silent(push_recorder):
    pipeline._alert_blocked_exit_age(
        {"intent_id": "i-1", "symbol": "US.ACN", "created_at": 1_000.0},
        {"exit_state": "HELD"}, now_ts=1_000.0 + 29 * 60)
    assert push_recorder.calls == []


def test_age_alert_tiers_and_dedup(push_recorder):
    intent = {"intent_id": "i-1", "symbol": "US.ACN", "target_env": "sim",
              "created_at": 1_000.0}
    pipeline._alert_blocked_exit_age(intent, {"exit_state": "HELD"},
                                     now_ts=1_000.0 + 31 * 60)
    pipeline._alert_blocked_exit_age(intent, {"exit_state": "HELD"},
                                     now_ts=1_000.0 + 32 * 60)
    assert len(push_recorder.calls) == 1
    assert push_recorder.calls[0][1]["level"].name == "HIGH"

    pipeline._alert_blocked_exit_age(intent, {"exit_state": "HELD"},
                                     now_ts=1_000.0 + 5 * 3600)
    pipeline._alert_blocked_exit_age(intent, {"exit_state": "HELD"},
                                     now_ts=1_000.0 + 6 * 3600)
    assert len(push_recorder.calls) == 2
    assert push_recorder.calls[1][1]["level"].name == "CRITICAL"


def test_age_alert_never_raises_without_created_at(push_recorder):
    pipeline._alert_blocked_exit_age({"intent_id": "i-1"}, {})
    assert push_recorder.calls == []


def test_resume_returns_blocked_detail_for_oldest_intent(monkeypatch):
    old = {"intent_id": "i-old", "symbol": "US.ACN", "target_env": "sim",
           "state": "HELD", "created_at": 1_000.0, "last_error": "held"}
    new = {"intent_id": "i-new", "symbol": "US.ACN", "target_env": "sim",
           "state": "PENDING", "created_at": 2_000.0, "last_error": ""}

    class FakeDual:
        def pending_closes(self):
            return [new, old]

        def resume_close(self, intent_id, allow_submit=True):
            # 非 success ⇒ 两者都进 blocked; state/error 应被结果刷新
            return {"success": False, "exit_state": "HELD", "dealt_qty": 0.0,
                    "error": "exit_eth_partial_fill_held"}

    import sys
    from types import SimpleNamespace
    monkeypatch.setitem(
        sys.modules, "app.execution.dual_broker",
        SimpleNamespace(get_dual_broker=lambda: FakeDual()))
    monkeypatch.setattr(pipeline, "_book_playbook_exit_result", lambda d, r: 0)
    monkeypatch.setattr(pipeline, "_alert_close_failure", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_release_eth_held_exit_for_rth_takeover",
                        lambda dual, intent, result: False)
    alerted = []
    monkeypatch.setattr(pipeline, "_alert_blocked_exit_age",
                        lambda intent, result, **k: alerted.append(intent["intent_id"]))

    closed, blocked, ok = pipeline._resume_playbook_exits(allow_submit=False)
    assert (closed, ok) == (0, True)
    assert set(blocked) == {("sim", "US.ACN")}
    detail = blocked[("sim", "US.ACN")]
    assert detail["intent_id"] == "i-old"      # 同标的保留最早意图做明细
    assert detail["state"] == "HELD"
    assert detail["error"] == "exit_eth_partial_fill_held"
    assert alerted == ["i-new", "i-old"]        # 每个失败意图各一次 (去重在告警内部)


def test_resume_empty_returns_empty_mapping(monkeypatch):
    class FakeDual:
        def pending_closes(self):
            return []

    import sys
    from types import SimpleNamespace
    monkeypatch.setitem(
        sys.modules, "app.execution.dual_broker",
        SimpleNamespace(get_dual_broker=lambda: FakeDual()))
    closed, blocked, ok = pipeline._resume_playbook_exits(allow_submit=False)
    assert (closed, blocked, ok) == (0, {}, True)
    assert isinstance(blocked, dict)
