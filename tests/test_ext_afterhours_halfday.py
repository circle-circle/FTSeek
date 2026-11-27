"""RFC-218 §20: EXT 盘后半日市窗口感知契约 (影子先行, 守卫默认 OFF = 逐字节旧窗口).

钉子:
  - 开关语义 (守卫默认 OFF / truthy / 未知值 fail-closed; 影子默认 ON / 显式 falsy 关);
  - 口径换算: 日历分钟数 (960/780) → HHMM (1959/1659), end_min=close_min+239;
  - 常规日: OFF/ON 均与静态窗逐字一致 (ON 零变化);
  - 半日市 OFF: 逐字节旧行为 (17:00-19:59 ET 仍放行 = 病灶保留, 影子只记录);
  - 半日市 ON: session/A/B 窗结束 cap 到 1659, 起点不动, 盘前不受影响;
  - 日历异常/bool/NaN/inf/越界 → fail-safe 回退静态 1959 (绝不扩窗);
  - 影子台账: 仅分歧区间写盘, 分模式 provenance 从 tick1, 写盘失败不上抛;
  - collect 集成: OFF 过窗闸 (旧行为) + shadow 落盘; ON 拦窗闸 + blocked 落盘。
"""
import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import scripts.extended_hours_entry_executor as mod
import app.utils.holiday_calendar as cal

ET = ZoneInfo("America/New_York")
HALF_DAY = datetime(2026, 11, 27, 17, 0, tzinfo=ET)   # 感恩节次日, 真实日历 780
REGULAR_DAY = datetime(2026, 11, 25, 17, 0, tzinfo=ET)  # 常规交易日, 真实日历 960


def _at(dt):
    return lambda: dt


def _hhmm(dt):
    return dt.hour * 100 + dt.minute


@pytest.fixture(autouse=True)
def _clean_flags(monkeypatch):
    monkeypatch.delenv("EXT_AFTERHOURS_HALF_DAY_GUARD", raising=False)
    monkeypatch.delenv("EXT_AFTERHOURS_HALF_DAY_SHADOW", raising=False)


@pytest.fixture(autouse=True)
def _fixed_et_now(monkeypatch):
    monkeypatch.setattr(mod, "_now_et", _at(HALF_DAY))


@pytest.fixture(autouse=True)
def ledger_dir(tmp_path, monkeypatch):
    d = tmp_path / "ext_afterhours_halfday"
    monkeypatch.setattr(mod, "EXT_AFTERHOURS_HALF_DAY_DIR", d)
    return d


def _read_ledger(ledger_dir, day="2026-11-27"):
    path = ledger_dir / (day + ".json")
    if not path.exists():
        return None
    return json.loads(path.read_text())


# ── 开关语义 ──────────────────────────────────────────────────────────────

def test_guard_flag_default_off(monkeypatch):
    assert mod._ext_afterhours_halfday_guard_enabled() is False


@pytest.mark.parametrize("v", ["1", "true", "yes", "on", "TRUE", " On "])
def test_guard_flag_truthy(monkeypatch, v):
    monkeypatch.setenv("EXT_AFTERHOURS_HALF_DAY_GUARD", v)
    assert mod._ext_afterhours_halfday_guard_enabled() is True


@pytest.mark.parametrize("v", ["0", "false", "no", "off", "", "2", "garbage"])
def test_guard_flag_falsy_unknown_fail_closed(monkeypatch, v):
    monkeypatch.setenv("EXT_AFTERHOURS_HALF_DAY_GUARD", v)
    assert mod._ext_afterhours_halfday_guard_enabled() is False


def test_shadow_flag_default_on():
    assert mod._ext_afterhours_halfday_shadow_enabled() is True


@pytest.mark.parametrize("v", ["0", "false", "no", "off", "OFF"])
def test_shadow_flag_explicit_falsy(monkeypatch, v):
    monkeypatch.setenv("EXT_AFTERHOURS_HALF_DAY_SHADOW", v)
    assert mod._ext_afterhours_halfday_shadow_enabled() is False


@pytest.mark.parametrize("v", ["1", "true", "garbage", ""])
def test_shadow_flag_non_falsy_stays_on(monkeypatch, v):
    monkeypatch.setenv("EXT_AFTERHOURS_HALF_DAY_SHADOW", v)
    assert mod._ext_afterhours_halfday_shadow_enabled() is True


# ── 口径换算与 fail-safe ──────────────────────────────────────────────────

def test_end_hhmm_regular_day_real_calendar(monkeypatch):
    monkeypatch.setattr(mod, "_now_et", _at(REGULAR_DAY))
    assert mod._afterhours_end_hhmm() == 1959  # = 静态窗, 零变化


def test_end_hhmm_half_day_real_calendar():
    # 不桩日历: 钉死真实 US_EARLY_CLOSE_DATES 含 2026-11-27 且 780→1659 换算正确
    assert mod._afterhours_end_hhmm() == 1659


def test_end_hhmm_conversion_pinned_via_stub(monkeypatch):
    monkeypatch.setattr(cal, "get_us_close_minutes_et", lambda d=None: 780)
    assert mod._afterhours_end_hhmm() == 1659
    monkeypatch.setattr(cal, "get_us_close_minutes_et", lambda d=None: 960)
    assert mod._afterhours_end_hhmm() == 1959


def test_end_hhmm_calendar_exception_falls_back_static(monkeypatch):
    def _boom(d=None):
        raise RuntimeError("calendar down")
    monkeypatch.setattr(cal, "get_us_close_minutes_et", _boom)
    assert mod._afterhours_end_hhmm() == 1959


def test_end_hhmm_now_et_exception_falls_back_static(monkeypatch):
    def _boom():
        raise RuntimeError("clock down")
    monkeypatch.setattr(mod, "_now_et", _boom)
    assert mod._afterhours_end_hhmm() == 1959


@pytest.mark.parametrize("bad", [0, -10, 961, 100000, True, False, float("nan"), float("inf"), None])
def test_end_hhmm_invalid_values_fail_safe(monkeypatch, bad):
    monkeypatch.setattr(cal, "get_us_close_minutes_et", lambda d=None: bad)
    assert mod._afterhours_end_hhmm() == 1959  # 绝不因兜底扩窗/缩窗到非法值


def test_end_hhmm_passes_et_date_not_local(monkeypatch):
    seen = {}
    def _spy(d=None):
        seen["d"] = d
        return 960
    monkeypatch.setattr(cal, "get_us_close_minutes_et", _spy)
    mod._afterhours_end_hhmm()
    assert seen["d"] == HALF_DAY.date()  # 显式 ET 日期, 防 cron CST 日错位


# ── session 窗口: 常规日 OFF/ON 均零变化 ──────────────────────────────────

@pytest.mark.parametrize("guard", ["0", "1"])
@pytest.mark.parametrize("hhmm,expect", [(1559, False), (1600, True), (1959, True), (2000, False)])
def test_session_regular_day_unchanged(monkeypatch, guard, hhmm, expect):
    monkeypatch.setenv("EXT_AFTERHOURS_HALF_DAY_GUARD", guard)
    monkeypatch.setattr(mod, "_now_et", _at(REGULAR_DAY))
    monkeypatch.setattr(mod, "_now_hhmm_et", lambda: hhmm)
    assert mod._in_session_window("afterhours") is expect


# ── session 窗口: 半日市 OFF 逐字节旧行为 / ON cap ────────────────────────

@pytest.mark.parametrize("hhmm,expect", [(1600, True), (1659, True), (1700, True), (1959, True), (2000, False)])
def test_session_half_day_off_byte_identical_old(monkeypatch, hhmm, expect):
    monkeypatch.setattr(mod, "_now_hhmm_et", lambda: hhmm)
    assert mod._in_session_window("afterhours") is expect  # 病灶保留: 17:00+ 仍放行


@pytest.mark.parametrize("hhmm,expect", [(1559, False), (1600, True), (1659, True), (1700, False), (1959, False)])
def test_session_half_day_on_caps_end_only(monkeypatch, hhmm, expect):
    monkeypatch.setenv("EXT_AFTERHOURS_HALF_DAY_GUARD", "1")
    monkeypatch.setattr(mod, "_now_hhmm_et", lambda: hhmm)
    assert mod._in_session_window("afterhours") is expect


def test_session_premarket_unaffected_on_half_day(monkeypatch):
    monkeypatch.setenv("EXT_AFTERHOURS_HALF_DAY_GUARD", "1")
    monkeypatch.setattr(mod, "_now_hhmm_et", lambda: 929)
    assert mod._in_session_window("premarket") is True
    monkeypatch.setattr(mod, "_now_hhmm_et", lambda: 930)
    assert mod._in_session_window("premarket") is False


# ── entry A/B 窗口 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("guard", ["0", "1"])
def test_entry_regular_day_unchanged(monkeypatch, guard):
    monkeypatch.setenv("EXT_AFTERHOURS_HALF_DAY_GUARD", guard)
    monkeypatch.setattr(mod, "_now_et", _at(REGULAR_DAY))
    monkeypatch.setattr(mod, "_now_hhmm_et", lambda: 1830)
    assert mod._entry_window_reason("afterhours", "A") is None
    monkeypatch.setattr(mod, "_now_hhmm_et", lambda: 1831)
    assert mod._entry_window_reason("afterhours", "A") == "entry_window_not_mature"
    monkeypatch.setattr(mod, "_now_hhmm_et", lambda: 1730)
    assert mod._entry_window_reason("afterhours", "B") is None
    monkeypatch.setattr(mod, "_now_hhmm_et", lambda: 1731)
    assert mod._entry_window_reason("afterhours", "B") == "entry_window_not_mature"


def test_entry_half_day_off_old_behavior(monkeypatch):
    monkeypatch.setattr(mod, "_now_hhmm_et", lambda: 1700)
    assert mod._entry_window_reason("afterhours", "A") is None
    assert mod._entry_window_reason("afterhours", "B") is None


@pytest.mark.parametrize("grade", ["A", "B"])
def test_entry_half_day_on_caps_end_keeps_start(monkeypatch, grade):
    monkeypatch.setenv("EXT_AFTERHOURS_HALF_DAY_GUARD", "1")
    monkeypatch.setattr(mod, "_now_hhmm_et", lambda: 1619)
    assert mod._entry_window_reason("afterhours", grade) == "entry_window_not_mature"
    monkeypatch.setattr(mod, "_now_hhmm_et", lambda: 1620)
    assert mod._entry_window_reason("afterhours", grade) is None
    monkeypatch.setattr(mod, "_now_hhmm_et", lambda: 1659)
    assert mod._entry_window_reason("afterhours", grade) is None
    monkeypatch.setattr(mod, "_now_hhmm_et", lambda: 1700)
    assert mod._entry_window_reason("afterhours", grade) == "entry_window_not_mature"


def test_entry_premarket_unaffected_on_half_day(monkeypatch):
    monkeypatch.setenv("EXT_AFTERHOURS_HALF_DAY_GUARD", "1")
    monkeypatch.setattr(mod, "_now_hhmm_et", lambda: 800)
    assert mod._entry_window_reason("premarket", "A") is None


# ── 影子台账 ──────────────────────────────────────────────────────────────

def test_shadow_regular_day_zero_footprint(monkeypatch, ledger_dir):
    monkeypatch.setattr(mod, "_now_et", _at(REGULAR_DAY))
    mod._record_afterhours_halfday_divergence("afterhours")
    assert not ledger_dir.exists()


def test_shadow_half_day_before_divergence_no_write(monkeypatch, ledger_dir):
    # 1659 = 半日窗末分钟, 不在 (1659, 1959] 分歧区间
    monkeypatch.setattr(mod, "_now_et", _at(datetime(2026, 11, 27, 16, 59, tzinfo=ET)))
    assert _read_ledger(ledger_dir) is None
    mod._record_afterhours_halfday_divergence("afterhours")
    assert _read_ledger(ledger_dir) is None


def test_shadow_half_day_divergence_off_records_shadow(ledger_dir):
    mod._record_afterhours_halfday_divergence("afterhours")  # now=1700 ∈ (1659,1959]
    doc = _read_ledger(ledger_dir)
    assert doc is not None
    assert doc["et_date"] == "2026-11-27"
    assert doc["session"] == "afterhours"
    assert doc["static_end_hhmm"] == 1959
    assert doc["half_day_end_hhmm"] == 1659
    assert doc["mode"] == "shadow"
    assert doc["ticks"] == 1
    assert doc["modes"]["shadow"]["ticks"] == 1
    assert doc["first_ts"] == doc["last_ts"] == HALF_DAY.isoformat()


def test_shadow_ticks_accumulate_first_ts_preserved(monkeypatch, ledger_dir):
    mod._record_afterhours_halfday_divergence("afterhours")
    later = datetime(2026, 11, 27, 17, 5, tzinfo=ET)
    monkeypatch.setattr(mod, "_now_et", _at(later))
    mod._record_afterhours_halfday_divergence("afterhours")
    doc = _read_ledger(ledger_dir)
    assert doc["ticks"] == 2
    assert doc["modes"]["shadow"]["ticks"] == 2
    assert doc["first_ts"] == HALF_DAY.isoformat()
    assert doc["last_ts"] == later.isoformat()


def test_shadow_mode_provenance_survives_guard_flip(monkeypatch, ledger_dir):
    mod._record_afterhours_halfday_divergence("afterhours")          # shadow tick1
    monkeypatch.setenv("EXT_AFTERHOURS_HALF_DAY_GUARD", "1")
    mod._record_afterhours_halfday_divergence("afterhours")          # blocked tick2
    monkeypatch.setenv("EXT_AFTERHOURS_HALF_DAY_GUARD", "0")
    mod._record_afterhours_halfday_divergence("afterhours")          # shadow tick3 (回滚)
    doc = _read_ledger(ledger_dir)
    assert doc["mode"] == "shadow" and doc["ticks"] == 3
    assert doc["modes"]["shadow"]["ticks"] == 2
    assert doc["modes"]["blocked"]["ticks"] == 1
    assert doc["modes"]["blocked"]["first_ts"] == HALF_DAY.isoformat()


def test_shadow_boundary_1959_inclusive_2000_excluded(monkeypatch, ledger_dir):
    monkeypatch.setattr(mod, "_now_hhmm_et", lambda: 1959)
    mod._record_afterhours_halfday_divergence("afterhours")
    assert _read_ledger(ledger_dir)["ticks"] == 1
    monkeypatch.setattr(mod, "_now_hhmm_et", lambda: 2000)
    mod._record_afterhours_halfday_divergence("afterhours")
    assert _read_ledger(ledger_dir)["ticks"] == 1  # 区间外不累加


def test_shadow_disabled_no_write(monkeypatch, ledger_dir):
    monkeypatch.setenv("EXT_AFTERHOURS_HALF_DAY_SHADOW", "0")
    mod._record_afterhours_halfday_divergence("afterhours")
    assert not ledger_dir.exists()


def test_shadow_premarket_session_no_write(ledger_dir):
    mod._record_afterhours_halfday_divergence("premarket")
    assert not ledger_dir.exists()


def test_shadow_write_failure_never_raises(monkeypatch, ledger_dir):
    def _boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(mod.os, "replace", _boom)
    mod._record_afterhours_halfday_divergence("afterhours")  # 不得上抛


def test_shadow_corrupt_ledger_rebuilt(monkeypatch, ledger_dir):
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "2026-11-27.json").write_text("[not a dict]")
    mod._record_afterhours_halfday_divergence("afterhours")
    doc = _read_ledger(ledger_dir)
    assert doc["ticks"] == 1 and doc["mode"] == "shadow"


# ── collect 集成 (窗闸 + 影子联动) ────────────────────────────────────────

@pytest.fixture
def _trading_day(monkeypatch):
    monkeypatch.setattr(mod, "_is_trading_day", lambda: True)


def test_collect_half_day_off_passes_window_records_shadow(monkeypatch, ledger_dir, _trading_day):
    # OFF: 窗闸放行 (旧行为) ⇒ 走到下一道 SIM fail-closed 闸, 证明未被窗闸拦
    monkeypatch.setattr(mod, "ALLOW_SIM_ENTRY", False)
    monkeypatch.setattr(mod, "_mode", lambda: SimpleNamespace(is_simulate=True))
    out = mod.collect("afterhours")
    assert out == [{"ok": False, "skip": "simulate_stop_unsupported_fail_closed"}]
    doc = _read_ledger(ledger_dir)
    assert doc["mode"] == "shadow" and doc["ticks"] == 1


def test_collect_half_day_on_blocked_at_window_records_blocked(monkeypatch, ledger_dir, _trading_day):
    monkeypatch.setenv("EXT_AFTERHOURS_HALF_DAY_GUARD", "1")
    out = mod.collect("afterhours")
    assert out == [{"ok": False, "skip": "outside_trading_window"}]
    doc = _read_ledger(ledger_dir)
    assert doc["mode"] == "blocked" and doc["ticks"] == 1


def test_collect_invalid_session_no_ledger(ledger_dir):
    out = mod.collect("bogus")
    assert out == [{"ok": False, "skip": "invalid_session"}]
    assert not ledger_dir.exists()


# ── §21 首个自然分歧日 → 待裁提醒 (提醒≠启用; canon 守卫 + ET 日去重 + 异常不上抛) ──

@pytest.fixture
def canon_ledger(ledger_dir, monkeypatch):
    """把 CANON 指向 tmp 台账 ⇒ 过「真实生产路径」守卫 (hermetic, 推送已被 spy 替换)."""
    monkeypatch.setattr(mod, "_EXT_AFTERHOURS_HALF_DAY_CANON", ledger_dir)
    return ledger_dir


@pytest.fixture
def push_spy(monkeypatch):
    import app.push.hub as hub
    calls = []

    def _spy(text, *a, **kw):
        calls.append((text, kw))
        return True

    monkeypatch.setattr(hub, "push_text_safe", _spy)
    return calls


def _state_path(ledger_dir):
    return ledger_dir.parent / "ext_afterhours_halfday_first_hit_notified.json"


def test_first_hit_non_canon_path_no_push(ledger_dir, push_spy):
    # autouse ledger_dir 已把 DIR 指 tmp, CANON 仍是生产路径 ⇒ 跳过提醒 (防测试误发)
    mod._record_afterhours_halfday_divergence("afterhours")
    assert _read_ledger(ledger_dir)["ticks"] == 1
    assert push_spy == []
    assert not _state_path(ledger_dir).exists()


def test_first_hit_canon_pushes_once_shadow(canon_ledger, push_spy):
    mod._record_afterhours_halfday_divergence("afterhours")
    assert len(push_spy) == 1
    text, kw = push_spy[0]
    assert "2026-11-27" in text and "shadow" in text and "待裁" in text
    assert kw.get("sync") is True
    state = json.loads(_state_path(canon_ledger).read_text())
    assert "2026-11-27" in state


def test_first_hit_same_day_second_tick_no_repush(canon_ledger, push_spy):
    mod._record_afterhours_halfday_divergence("afterhours")
    mod._record_afterhours_halfday_divergence("afterhours")
    assert _read_ledger(canon_ledger)["ticks"] == 2
    assert len(push_spy) == 1


def test_first_hit_dedup_state_preexisting(canon_ledger, push_spy):
    canon_ledger.mkdir(parents=True, exist_ok=True)
    _state_path(canon_ledger).write_text(json.dumps({"2026-11-27": "prior"}))
    mod._record_afterhours_halfday_divergence("afterhours")
    assert _read_ledger(canon_ledger)["ticks"] == 1
    assert push_spy == []


def test_first_hit_new_half_day_pushes_again(canon_ledger, push_spy, monkeypatch):
    mod._record_afterhours_halfday_divergence("afterhours")
    # 12-24 也是真实日历半日市 (780) ⇒ 新分歧日 = 新事件, 再推一次
    monkeypatch.setattr(mod, "_now_et", _at(datetime(2026, 12, 24, 17, 0, tzinfo=ET)))
    mod._record_afterhours_halfday_divergence("afterhours")
    assert len(push_spy) == 2
    assert "2026-12-24" in push_spy[1][0]
    state = json.loads(_state_path(canon_ledger).read_text())
    assert set(state) == {"2026-11-27", "2026-12-24"}


def test_first_hit_guard_on_text_blocked(canon_ledger, push_spy, monkeypatch):
    monkeypatch.setenv("EXT_AFTERHOURS_HALF_DAY_GUARD", "1")
    mod._record_afterhours_halfday_divergence("afterhours")
    assert len(push_spy) == 1
    assert "blocked" in push_spy[0][0]
    assert _read_ledger(canon_ledger)["mode"] == "blocked"


def test_first_hit_push_failure_never_propagates(canon_ledger, monkeypatch):
    import app.push.hub as hub

    def _boom(text, *a, **kw):
        raise RuntimeError("push down")

    monkeypatch.setattr(hub, "push_text_safe", _boom)
    mod._record_afterhours_halfday_divergence("afterhours")  # 不得上抛
    doc = _read_ledger(canon_ledger)
    assert doc["ticks"] == 1                      # 台账已落盘 (提醒在写盘之后)
    assert not _state_path(canon_ledger).exists()  # 失败不写去重 state


def test_first_hit_corrupt_state_treated_empty(canon_ledger, push_spy):
    canon_ledger.mkdir(parents=True, exist_ok=True)
    _state_path(canon_ledger).write_text("{not json")
    mod._record_afterhours_halfday_divergence("afterhours")
    assert len(push_spy) == 1


# ── RFC-218 §22 (P1-B): 真实 False 合同 — 失败不登记 notified, 有限重试 ──────

def test_first_hit_push_false_pending_then_retry_next_tick(canon_ledger, monkeypatch):
    """hub 返回 False (不抛异常) ⇒ 不登记 notified; 当日后续分歧 tick 重试; 成功后去重。"""
    import app.push.hub as hub
    calls, results = [], [False, True]

    def _spy(text, *a, **kw):
        calls.append(text)
        return results.pop(0)
    monkeypatch.setattr(hub, "push_text_safe", _spy)
    mod._record_afterhours_halfday_divergence("afterhours")
    state = json.loads(_state_path(canon_ledger).read_text())
    assert state["2026-11-27"]["notified"] is False
    assert state["2026-11-27"]["attempts"] == 1
    mod._record_afterhours_halfday_divergence("afterhours")   # ticks=2, 非首日 tick 也重试
    assert len(calls) == 2
    state = json.loads(_state_path(canon_ledger).read_text())
    assert isinstance(state["2026-11-27"], str)               # 成功=旧 ISO 格式
    mod._record_afterhours_halfday_divergence("afterhours")   # ticks=3 → 去重
    assert len(calls) == 2
    assert _read_ledger(canon_ledger)["ticks"] == 3


def test_first_hit_push_false_exhausts_finite_attempts(canon_ledger, monkeypatch):
    """连续 False ⇒ 最多 3 次尝试, 之后不再推 (防无限刷屏), 待处理条目保留。"""
    import app.push.hub as hub
    calls = []

    def _spy(text, *a, **kw):
        calls.append(text)
        return False
    monkeypatch.setattr(hub, "push_text_safe", _spy)
    for _ in range(5):
        mod._record_afterhours_halfday_divergence("afterhours")
    assert len(calls) == mod._FIRST_HIT_PUSH_MAX_ATTEMPTS == 3
    state = json.loads(_state_path(canon_ledger).read_text())
    assert state["2026-11-27"]["notified"] is False
    assert state["2026-11-27"]["attempts"] == 3
    assert _read_ledger(canon_ledger)["ticks"] == 5           # 台账不受提醒失败影响


def test_first_hit_false_then_new_day_pushes_again(canon_ledger, monkeypatch):
    """当日重试耗尽不影响新分歧日 = 新事件 (按日去重, 不是全局噤声)。"""
    import app.push.hub as hub
    calls = []

    def _spy(text, *a, **kw):
        calls.append(text)
        return False if "2026-11-27" in text else True
    monkeypatch.setattr(hub, "push_text_safe", _spy)
    for _ in range(4):
        mod._record_afterhours_halfday_divergence("afterhours")
    assert len(calls) == 3
    monkeypatch.setattr(mod, "_now_et", _at(datetime(2026, 12, 24, 17, 0, tzinfo=ET)))
    mod._record_afterhours_halfday_divergence("afterhours")
    assert len(calls) == 4 and "2026-12-24" in calls[3]
    state = json.loads(_state_path(canon_ledger).read_text())
    assert state["2026-11-27"]["attempts"] == 3
    assert isinstance(state["2026-12-24"], str)
