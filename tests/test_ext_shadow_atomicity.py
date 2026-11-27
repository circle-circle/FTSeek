"""RFC-218 §23 (§22.9 #4): EXT 观测台账/通知去重 原子性契约。

钉子:
  - 重叠调用 (线程模拟并发 tick) 读-改-写不丢 ticks / 不丢 mode 记录;
  - 唯一 tmp (mkstemp) 替代共享 .json.tmp — 并发后无残留 tmp、无共享名互踩;
  - 首命中通知去重 state 在锁内串行 — N 并发只推一次, attempts 不丢;
  - 文档格式/去重合同与 §19/§20/§21/§22 逐字兼容 (既有 136 契约另钉)。

dry 是否必须静默 = 独立裁决项, 本批不改 dry 语义 (collect 入口行为逐字保留)。
回滚: revert 本批 commit + 删本文件。
"""
import json
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import scripts.extended_hours_entry_executor as mod

ET = ZoneInfo("America/New_York")
HALF_DAY = datetime(2026, 11, 27, 17, 2, tzinfo=ET)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.delenv("EXT_AFTERHOURS_HALF_DAY_GUARD", raising=False)
    monkeypatch.delenv("EXT_AFTERHOURS_HALF_DAY_SHADOW", raising=False)
    monkeypatch.delenv("EXT_BLACKLIST_GUARD", raising=False)
    monkeypatch.delenv("EXT_BLACKLIST_SHADOW", raising=False)
    monkeypatch.setattr(mod, "_now_et", lambda: HALF_DAY)
    d = tmp_path / "ext_afterhours_halfday"
    monkeypatch.setattr(mod, "EXT_AFTERHOURS_HALF_DAY_DIR", d)
    b = tmp_path / "ext_blacklist"
    monkeypatch.setattr(mod, "EXT_BLACKLIST_DIR", b)
    return tmp_path


def _run_threads(fn, n=16):
    errs = []

    def _wrap():
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=_wrap) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    assert not errs, errs


class TestHalfDayLedgerAtomicity:
    def test_concurrent_ticks_no_lost_updates(self, _env):
        d = mod.EXT_AFTERHOURS_HALF_DAY_DIR
        _run_threads(lambda: mod._record_afterhours_halfday_divergence("afterhours"), 20)
        doc = json.loads((d / "2026-11-27.json").read_text())
        assert doc["ticks"] == 20
        assert doc["modes"]["shadow"]["ticks"] == 20
        assert doc["mode"] == "shadow"

    def test_no_shared_tmp_residue(self, _env):
        d = mod.EXT_AFTERHOURS_HALF_DAY_DIR
        _run_threads(lambda: mod._record_afterhours_halfday_divergence("afterhours"), 12)
        assert list(d.glob("*.tmp")) == []
        assert not (d / "2026-11-27.json.tmp").exists()

    def test_mode_upgrade_under_concurrency_keeps_both_modes(self, _env, monkeypatch):
        d = mod.EXT_AFTERHOURS_HALF_DAY_DIR
        mod._record_afterhours_halfday_divergence("afterhours")   # shadow tick1
        monkeypatch.setenv("EXT_AFTERHOURS_HALF_DAY_GUARD", "1")
        _run_threads(lambda: mod._record_afterhours_halfday_divergence("afterhours"), 10)
        doc = json.loads((d / "2026-11-27.json").read_text())
        assert doc["ticks"] == 11
        assert doc["modes"]["shadow"]["ticks"] == 1
        assert doc["modes"]["blocked"]["ticks"] == 10
        assert doc["mode"] == "blocked"


class TestBlacklistLedgerAtomicity:
    def test_concurrent_hits_no_lost_ticks(self, _env):
        b = mod.EXT_BLACKLIST_DIR
        item = {"chg": 5.0, "px": 100.0, "vol": 1000000, "_source": "rank"}
        _run_threads(
            lambda: mod._record_ext_blacklist("US.QCOM", "afterhours", item, False), 20)
        doc = json.loads((b / "2026-11-27.json").read_text())
        row = doc["rows"]["afterhours:US.QCOM"]
        assert row["ticks"] == 20
        assert row["modes"]["shadow"]["ticks"] == 20
        assert list(b.glob("*.tmp")) == []


class TestFirstHitNotifyAtomicity:
    @pytest.fixture
    def canon(self, _env, monkeypatch):
        monkeypatch.setattr(
            mod, "_EXT_AFTERHOURS_HALF_DAY_CANON", mod.EXT_AFTERHOURS_HALF_DAY_DIR)
        import app.push.hub as hub
        calls = []
        lock = threading.Lock()

        def _spy(text, *a, **kw):
            with lock:
                calls.append((text, kw))
            return True

        monkeypatch.setattr(hub, "push_text_safe", _spy)
        return calls

    def test_concurrent_first_hit_pushes_once(self, canon):
        _run_threads(
            lambda: mod._notify_ext_afterhours_halfday_first_hit("2026-11-27", False), 12)
        assert len(canon) == 1
        state = json.loads(
            (mod.EXT_AFTERHOURS_HALF_DAY_DIR.parent
             / "ext_afterhours_halfday_first_hit_notified.json").read_text())
        assert isinstance(state["2026-11-27"], str)

    def test_concurrent_failure_retries_attempts_not_lost(self, _env, monkeypatch):
        monkeypatch.setattr(
            mod, "_EXT_AFTERHOURS_HALF_DAY_CANON", mod.EXT_AFTERHOURS_HALF_DAY_DIR)
        import app.push.hub as hub
        monkeypatch.setattr(hub, "push_text_safe", lambda *a, **kw: False)
        # 串行 3 次失败 → attempts 封顶 (并发下锁保证不丢计数)
        _run_threads(
            lambda: mod._notify_ext_afterhours_halfday_first_hit("2026-11-27", False), 5)
        state = json.loads(
            (mod.EXT_AFTERHOURS_HALF_DAY_DIR.parent
             / "ext_afterhours_halfday_first_hit_notified.json").read_text())
        entry = state["2026-11-27"]
        assert entry["notified"] is False
        assert entry["attempts"] == mod._FIRST_HIT_PUSH_MAX_ATTEMPTS

    def test_blacklist_concurrent_first_hit_pushes_once(self, _env, monkeypatch):
        monkeypatch.setattr(mod, "_EXT_BLACKLIST_CANON", mod.EXT_BLACKLIST_DIR)
        import app.push.hub as hub
        calls = []
        lock = threading.Lock()

        def _spy(text, *a, **kw):
            with lock:
                calls.append(text)
            return True

        monkeypatch.setattr(hub, "push_text_safe", _spy)
        _run_threads(
            lambda: mod._notify_ext_blacklist_first_hit("US.QCOM", "afterhours", False), 12)
        assert len(calls) == 1
        state = json.loads(
            (mod.EXT_BLACKLIST_DIR.parent
             / "ext_blacklist_first_hit_notified.json").read_text())
        assert isinstance(state["US.QCOM"], str)
