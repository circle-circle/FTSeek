"""开盘禁入窗 SSOT 化 (2026-11-12) — 唯一真源 = playbook 配置, 不再有硬编码 +5.

背景: `run_playbook_auto_execute` 原有一行硬编码 `_now_min < _market_open_min + 5`
(=09:35 floor), 与 `entry_open_blackout_hhmm` (check_intraday_trigger 的开盘禁触窗)
是同一语义的两处真源。用户令把开盘后延后到 15 分钟 (0945) 时, 两处必然漂移:
改 config ⇒ 硬编码那行变死码; 只改硬编码 ⇒ config 那行才是 binding。

本测试守三件事:
  1. 配置→floor 的纯函数语义 (0945→09:45, 关闸/非法值回落 09:35 旧行为)
  2. 真行为: 09:40 ET 不执行 / 09:45 起执行 (frozen clock, 真函数调用)
  3. 源码契约: 硬编码 `+ 5` 不得回归, 闸必须经 open_floor_minutes
"""
import datetime as _datetime_mod
import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.playbook.pipeline_parts import auto_execute_flow as aef  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SRC = (REPO / "app" / "playbook" / "pipeline_parts" /
       "auto_execute_flow.py").read_text(encoding="utf-8")
LOG = logging.getLogger("test_open_floor")


class _Cfg:
    def __init__(self, data):
        self._d = data

    def get(self, key, default=None):
        return self._d.get(key, default)


def _patch_cfg(monkeypatch, data):
    import app.playbook.config as pbcfg
    monkeypatch.setattr(pbcfg, "get_playbook_config", lambda: _Cfg(data))


# ── 1. 纯函数: 配置 → floor ──────────────────────────────────────────
@pytest.mark.parametrize("cfg,expect_min,expect_hhmm", [
    ({"entry_open_blackout_enabled": True, "entry_open_blackout_hhmm": "0945"}, 585, 945),
    ({"entry_open_blackout_enabled": True, "entry_open_blackout_hhmm": 945}, 585, 945),
    ({"entry_open_blackout_enabled": True, "entry_open_blackout_hhmm": "09:45"}, 585, 945),
    # 回滚路径: 改回 0935 = 与本闸旧硬编码 +5 逐字等价
    ({"entry_open_blackout_enabled": True, "entry_open_blackout_hhmm": 935}, 575, 935),
    # 关闸 ⇒ 仍保底 0935 (不放开成 09:30 裸执行)
    ({"entry_open_blackout_enabled": False, "entry_open_blackout_hhmm": 945}, 575, 935),
    # 非法值 ⇒ 回落 0935, 不因手误放开开盘
    ({"entry_open_blackout_enabled": True, "entry_open_blackout_hhmm": "09:75"}, 575, 935),
    ({"entry_open_blackout_enabled": True, "entry_open_blackout_hhmm": "abc"}, 575, 935),
    ({"entry_open_blackout_enabled": True, "entry_open_blackout_hhmm": 0}, 575, 935),
])
def test_open_floor_from_config(monkeypatch, cfg, expect_min, expect_hhmm):
    _patch_cfg(monkeypatch, cfg)
    assert aef.open_floor_minutes(logger=LOG) == (expect_min, expect_hhmm)


# ── 2. 真行为: 冻结时钟跑 run_playbook_auto_execute ──────────────────
@pytest.fixture
def frozen(monkeypatch):
    """把 datetime.datetime 换成可冻结的子类 (被测函数内部 `from datetime import
    datetime as _dt` 在调用时解析, 故打类本体有效; 退出时 monkeypatch 自动还原)。"""
    real = _datetime_mod.datetime
    state = {"now": None}

    class _Frozen(real):
        @classmethod
        def now(cls, tz=None):
            d = state["now"]
            return d if tz is None else d.astimezone(tz)

    monkeypatch.setattr(_datetime_mod, "datetime", _Frozen)

    def _set(y, m, d, hh, mm):
        import pytz
        from datetime import datetime as _real_dt
        frozen_at = pytz.timezone("America/New_York").localize(
            _real_dt(y, m, d, hh, mm))
        state["now"] = frozen_at
        _set.now_dt = frozen_at      # 供直接调 emit_blackout_shadow 的用例取"当前时刻"
        return frozen_at
    _set.now_dt = None
    return _set


def _run(monkeypatch, reached):
    import pytz

    def _mark(name):
        def _fn(*a, **kw):
            reached.append(name)
            return None
        return _fn

    return aef.run_playbook_auto_execute(
        scheduler=object(),
        force=False,
        datetime_cls=_datetime_mod.datetime,
        _now_et=lambda: pytz.timezone("America/New_York"),
        _today_et_str=lambda: "2026-11-12",
        module_file=__file__,
        logger=LOG,
        INVALIDATED_SKIP_REASONS=set(),
        _CC_NORM_LOGGED=set(),
        _CC_NORM_LOGGED_MAX=10,
        _auto_execute_preflight=lambda today, logger=None: {"preflight": "stub"} if reached.append("preflight") is None else None,
        _chase_cap_round_bump=_mark("bump"),
        _check_bridge_health_and_alert=_mark("bridge"),
        _check_intraday_trigger=_mark("trigger"),
        _check_same_day_hard_stop_cooldown=_mark("cooldown"),
        _compute_volatility_based_sl_tp=_mark("vol"),
        _emit_exec_gate_audit=_mark("audit"),
        _is_trading_day_today=lambda: True,
        _live_engineering_probe_context=lambda: None,
        _load_execution_budget=_mark("budget"),
        _mirror_instruction_to_live_if_allowed=_mark("mirror"),
        _normalize_regime_str=lambda x: x,
        _playbook_cfg=lambda k, d=None: (True if k == "auto_execute" else d),
        _positive_finite_atr=_mark("atr"),
        _resolve_sizing_base=_mark("sizing"),
        _shadow_dedup_claim=_mark("sc"),
        _shadow_dedup_commit=_mark("sk"),
        _vol_bucket_sl_bounds=_mark("bounds"),
    )


@pytest.mark.parametrize("hh,mm,should_block", [
    (9, 30, True), (9, 40, True), (9, 44, True),
    (9, 45, False), (9, 46, False), (9, 35, True),
])
def test_gate_blocks_until_configured_floor(monkeypatch, frozen, hh, mm, should_block):
    _patch_cfg(monkeypatch, {"entry_open_blackout_enabled": True,
                             "entry_open_blackout_hhmm": "0945"})
    frozen(2026, 11, 12, hh, mm)
    reached = []
    out = _run(monkeypatch, reached)
    if should_block:
        assert out is None
        assert "bridge" not in reached and "preflight" not in reached, reached
    else:
        assert reached[:2] == ["bridge", "preflight"], reached


def test_rollback_0935_reproduces_legacy_floor(monkeypatch, frozen):
    """回滚 = 把 hhmm 改回 0935 ⇒ 09:35 起就执行 (与旧 `+5` 逐字等价)。"""
    _patch_cfg(monkeypatch, {"entry_open_blackout_enabled": True,
                             "entry_open_blackout_hhmm": 935})
    frozen(2026, 11, 12, 9, 35)
    reached = []
    _run(monkeypatch, reached)
    assert reached[:2] == ["bridge", "preflight"], reached

    frozen(2026, 11, 12, 9, 34)
    reached2 = []
    _run(monkeypatch, reached2)
    assert "preflight" not in reached2


# ── 3. 源码契约: 硬编码不得回归 ─────────────────────────────────────
def test_source_has_no_hardcoded_open_floor():
    assert "_market_open_min + 5" not in SRC
    assert "_market_open_min" not in SRC
    assert "open_floor_minutes(" in SRC


# ── 4. 黑窗只读观测 (2026-11-12 用户裁 A) ────────────────────────────
class _FakeRouter:
    def __init__(self, px):
        self._px = px

    def get_full_market_context(self, symbol):
        if symbol not in self._px:
            raise RuntimeError("no quote")
        return {"price": self._px[symbol]}


class _FakeDB:
    """只暴露 get_instructions_by_date — 任何写口调用都会 AttributeError ⇒ 测试即红。"""

    def __init__(self, rows):
        self._rows = rows

    def get_instructions_by_date(self, today):
        return self._rows


def _inst(iid, symbol, direction, entry, **kw):
    d = {"id": iid, "symbol": symbol, "direction": direction,
         "entry_price": entry, "decision_id": f"DA-x-{symbol}",
         "triggered": 0, "executed": 0, "skip_reason": "", "confidence": 40}
    d.update(kw)
    return d


def _shadow_rows(tmp_path):
    p = tmp_path / "entry_blackout_shadow.jsonl"
    if not p.exists():
        return []
    import json as _j
    return [_j.loads(x) for x in p.read_text().splitlines() if x.strip()]


def _wire_shadow(monkeypatch, tmp_path, rows, px, enabled=True):
    import app.playbook.db as pbdb
    monkeypatch.setattr(pbdb, "route_playbook_db", lambda mode: _FakeDB(rows))
    monkeypatch.setattr(aef, "_blackout_shadow_path",
                        lambda: tmp_path / "entry_blackout_shadow.jsonl")
    monkeypatch.setattr(aef, "_BLACKOUT_SHADOW_SEEN", set())
    cfg = {"entry_open_blackout_enabled": True, "entry_open_blackout_hhmm": 945,
           "entry_blackout_shadow_enabled": enabled,
           "entry_band_vol_widen_enabled": False,
           "intraday_vol_gate_grace_min": 15}
    _patch_cfg(monkeypatch, cfg)
    return cfg


def test_blackout_shadow_records_band_hits_only(monkeypatch, frozen, tmp_path):
    rows = [
        _inst(1, "US.AAA", "BUY", 100.0),     # 99.5 → pullback (带内) ⇒ 应记
        _inst(2, "US.BBB", "BUY", 100.0),     # 106.5 → 出带 ⇒ 不记
        _inst(3, "US.CCC", "SHORT", 100.0),   # 101.0 → pullback ⇒ 应记
        _inst(4, "US.DDD", "BUY", 100.0, triggered=1),   # 已触发 ⇒ 不评估
        _inst(5, "US.EEE", "SKIP", 100.0),               # SKIP ⇒ 不评估
        _inst(6, "US.FFF", "BUY", 0),                    # 无锚 ⇒ 跳过
        _inst(7, "US.TEST_X", "BUY", 100.0),             # 测试标的 ⇒ 跳过
    ]
    px = {"US.AAA": 99.5, "US.BBB": 106.5, "US.CCC": 101.0,
          "US.DDD": 99.0, "US.EEE": 99.0, "US.TEST_X": 99.0}
    _wire_shadow(monkeypatch, tmp_path, rows, px)
    frozen(2026, 11, 12, 9, 40)

    class _Sched:
        data_router = _FakeRouter(px)

    aef.emit_blackout_shadow(_Sched(), "2026-11-12", frozen.now_dt, LOG, set(), 945)
    got = _shadow_rows(tmp_path)
    cyc = [r for r in got if r["kind"] == "cycle"]
    hits = [r for r in got if r["kind"] == "hit"]
    assert len(cyc) == 1 and cyc[0]["n_inst"] == 7 and cyc[0]["n_priced"] == 3
    assert sorted(h["symbol"] for h in hits) == ["US.AAA", "US.CCC"]
    aaa = [h for h in hits if h["symbol"] == "US.AAA"][0]
    assert aaa["entry_path"] == "pullback" and aaa["band_would_trigger"] is True
    assert aaa["vol_assumed_ok"] is True and aaa["blackout_hhmm"] == 945
    assert "band geometry only" in aaa["caveat"]


def test_blackout_shadow_dedupes_first_hit_per_instruction(monkeypatch, frozen, tmp_path):
    rows = [_inst(1, "US.AAA", "BUY", 100.0)]
    px = {"US.AAA": 99.5}
    _wire_shadow(monkeypatch, tmp_path, rows, px)
    frozen(2026, 11, 12, 9, 38)

    class _Sched:
        data_router = _FakeRouter(px)

    aef.emit_blackout_shadow(_Sched(), "2026-11-12", frozen.now_dt, LOG, set(), 945)
    frozen(2026, 11, 12, 9, 41)
    aef.emit_blackout_shadow(_Sched(), "2026-11-12", frozen.now_dt, LOG, set(), 945)
    got = _shadow_rows(tmp_path)
    assert len([r for r in got if r["kind"] == "cycle"]) == 2      # 每轮都记 (证明活着)
    assert len([r for r in got if r["kind"] == "hit"]) == 1        # 明细只首记一次


def test_blackout_shadow_off_writes_nothing(monkeypatch, frozen, tmp_path):
    rows = [_inst(1, "US.AAA", "BUY", 100.0)]
    px = {"US.AAA": 99.5}
    _wire_shadow(monkeypatch, tmp_path, rows, px, enabled=False)
    frozen(2026, 11, 12, 9, 40)

    class _Sched:
        data_router = _FakeRouter(px)

    aef.emit_blackout_shadow(_Sched(), "2026-11-12", frozen.now_dt, LOG, set(), 945)
    assert _shadow_rows(tmp_path) == []


def test_blackout_shadow_never_writes_db(monkeypatch, frozen, tmp_path):
    """写口出现即 AttributeError(_FakeDB 只有读方法) ⇒ 本用例守"零 DB 写入"。"""
    rows = [_inst(1, "US.AAA", "BUY", 100.0, skip_reason="thesis_drift_invalidate")]
    px = {"US.AAA": 99.5}
    _wire_shadow(monkeypatch, tmp_path, rows, px)
    frozen(2026, 11, 12, 9, 40)

    class _Sched:
        data_router = _FakeRouter(px)

    # skip_reason 已作废 ⇒ 连观测都不记 (与生产 trigger 的作废语义对齐)
    aef.emit_blackout_shadow(_Sched(), "2026-11-12", frozen.now_dt, LOG,
                             {"thesis_drift_invalidate"}, 945)
    assert [r for r in _shadow_rows(tmp_path) if r["kind"] == "hit"] == []


# ── 5. 触发判定单一真源 (classify_trigger) 与迁移前内联块逐字等价 ──
def _legacy_classify(is_short, e, p, knife, chase, vol_ok, tol=0.01):
    """迁移前 trigger_flow 内联块的逐字副本 — 仅用作对撞基准。"""
    if is_short:
        if p >= e:
            return (p <= e * (1 + knife)), "pullback"
        if p >= e * (1 - chase):
            return vol_ok, "chase"
        if p >= e * (1 - chase - tol):
            return vol_ok, "chase_near_miss"
        return False, "out_of_band"
    if p <= e:
        return (p >= e * (1 - knife)), "pullback"
    if p <= e * (1 + chase):
        return vol_ok, "chase"
    if p <= e * (1 + chase + tol):
        return vol_ok, "chase_near_miss"
    return False, "out_of_band"


@pytest.mark.parametrize("is_short", [False, True])
@pytest.mark.parametrize("vol_ok", [True, False])
def test_classify_trigger_matches_legacy_inline(is_short, vol_ok):
    from app.playbook.entry_band import classify_trigger
    knife, chase = 0.03, 0.05
    for pct in [-0.12, -0.081, -0.08, -0.061, -0.06, -0.051, -0.05, -0.02,
                0.0, 0.02, 0.05, 0.059, 0.06, 0.061, 0.08, 0.12]:
        p = 100.0 * (1 + pct)
        assert classify_trigger(is_short, 100.0, p, knife, chase, vol_ok) == \
            _legacy_classify(is_short, 100.0, p, knife, chase, vol_ok), (is_short, pct, vol_ok)


def test_classify_trigger_guards_bad_prices():
    from app.playbook.entry_band import classify_trigger
    assert classify_trigger(False, 0, 100, 0.03, 0.05, True) == (False, "out_of_band")
    assert classify_trigger(False, 100, 0, 0.03, 0.05, True) == (False, "out_of_band")


# ── 6. 黑窗观测故障路径 + MOM 暂时性拦截 ────────────────────────────
def test_blackout_shadow_writes_liveness_when_all_quotes_fail(monkeypatch, frozen, tmp_path):
    rows = [_inst(1, "US.AAA", "BUY", 100.0)]
    _wire_shadow(monkeypatch, tmp_path, rows, {})
    frozen(2026, 11, 12, 9, 40)

    class _Sched:
        data_router = _FakeRouter({})

    aef.emit_blackout_shadow(_Sched(), "2026-11-12", frozen.now_dt, LOG, set(), 945)
    got = _shadow_rows(tmp_path)
    assert len(got) == 1
    cycle = got[0]
    assert cycle["kind"] == "cycle" and cycle["status"] == "QUOTE_UNAVAILABLE"
    assert cycle["n_eligible"] == 1 and cycle["n_priced"] == 0
    assert cycle["n_quote_error"] >= 1 and cycle["n_price_invalid"] == 1


def test_blackout_shadow_writes_no_router_heartbeat(monkeypatch, frozen, tmp_path):
    rows = [_inst(1, "US.AAA", "BUY", 100.0)]
    _wire_shadow(monkeypatch, tmp_path, rows, {})
    frozen(2026, 11, 12, 9, 40)
    aef.emit_blackout_shadow(object(), "2026-11-12", frozen.now_dt, LOG, set(), 945)
    got = _shadow_rows(tmp_path)
    assert len(got) == 1 and got[0]["status"] == "NO_ROUTER"
    assert got[0]["n_inst"] == 1 and got[0]["n_priced"] == 0


def test_blackout_shadow_uses_production_quote_fallback(monkeypatch, frozen, tmp_path):
    rows = [_inst(1, "US.AAA", "BUY", 100.0)]
    _wire_shadow(monkeypatch, tmp_path, rows, {})
    frozen(2026, 11, 12, 9, 40)

    class _FallbackRouter:
        def get_full_market_context(self, symbol):
            raise RuntimeError("context unavailable")

        def get_quote(self, symbol):
            return {"last": 99.5}

    class _Sched:
        data_router = _FallbackRouter()

    aef.emit_blackout_shadow(_Sched(), "2026-11-12", frozen.now_dt, LOG, set(), 945)
    got = _shadow_rows(tmp_path)
    hits = [r for r in got if r["kind"] == "hit"]
    assert len(hits) == 1 and hits[0]["quote_source"] == "quote_fallback"


def test_blackout_shadow_retries_hit_after_write_failure(monkeypatch, frozen, tmp_path):
    rows = [_inst(1, "US.AAA", "BUY", 100.0)]
    px = {"US.AAA": 99.5}
    _wire_shadow(monkeypatch, tmp_path, rows, px)
    frozen(2026, 11, 12, 9, 40)

    class _Sched:
        data_router = _FakeRouter(px)

    import builtins
    real_open = builtins.open
    target = tmp_path / "entry_blackout_shadow.jsonl"
    fail = {"enabled": True}

    def _flaky_open(file, *args, **kwargs):
        if str(file) == str(target) and fail["enabled"]:
            raise OSError("injected append failure")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _flaky_open)
    aef.emit_blackout_shadow(_Sched(), "2026-11-12", frozen.now_dt, LOG, set(), 945)
    assert _shadow_rows(tmp_path) == []
    assert aef._BLACKOUT_SHADOW_SEEN == set()

    fail["enabled"] = False
    aef.emit_blackout_shadow(_Sched(), "2026-11-12", frozen.now_dt, LOG, set(), 945)
    got = _shadow_rows(tmp_path)
    assert len([r for r in got if r["kind"] == "cycle"]) == 1
    assert len([r for r in got if r["kind"] == "hit"]) == 1
    assert ("2026-11-12", 1) in aef._BLACKOUT_SHADOW_SEEN


@pytest.mark.parametrize("ctx,expected", [
    (None, 0.0),
    ({}, 0.0),
    ({"lv3_ask": "bad", "ask": 101.25}, 101.25),
    ({"lv3_ask": float("nan"), "price": 102.5}, 102.5),
    ({"last": 103.75}, 103.75),
    ({"lv3_ask": -1, "ask": 0, "price": None}, 0.0),
])
def test_mom_quote_price_rejects_missing_and_malformed(ctx, expected):
    assert aef._mom_quote_price(ctx) == expected


def test_mom_defer_is_audited_once_and_never_writes_terminal_skip_reason(monkeypatch):
    """暂时性 defer 只进审计台账, **不写 skip_reason** (2026-11-12 核查后修正)。

    `skip_reason` 是终态字段 (只在 executed=1 时清空), 下游 ≥3 处把"非空 skip_reason"
    当"已跳过" (premarket_invalidation 不作废 / shadow_tracker 不取样 / ue_batch 不打分)。
    故本用例钉住: 一次 defer ⇒ 0 次 DB 写、1 条审计 (transient=True)、指令原样不动。
    """
    monkeypatch.setattr(aef, "_MOM_DEFER_AUDIT_SEEN", set())

    class _DB:
        def __init__(self):
            self.updates = []

        def update_instruction_tracking(self, iid, patch):
            self.updates.append((iid, patch))

    db = _DB()
    inst = _inst(1, "US.QQQ", "BUY", 100.0)
    audits = []
    for _ in range(2):
        aef._record_mom_defer(
            db, inst, "2026-11-13", "MOM_QUOTE_UNAVAILABLE(test)",
            action="skipped_mom_quote_unavailable", gate="mom_quote_available",
            why="test", logger=LOG,
            emit_exec_gate_audit=lambda row, **kw: audits.append((row, kw)))
    assert inst["direction"] == "BUY"
    assert db.updates == [], "暂时态不得写终态字段 skip_reason (会污染 3 个下游读方)"
    assert not inst.get("skip_reason")
    assert len(audits) == 1 and audits[0][1]["extra"]["transient"] is True