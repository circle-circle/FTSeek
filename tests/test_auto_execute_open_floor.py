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
        state["now"] = pytz.timezone("America/New_York").localize(
            real(y, m, d, hh, mm))
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