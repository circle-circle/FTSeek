# -*- coding: utf-8 -*-
"""RFC-218 §16 动态黑名单统一守卫契约钉 (2026-11-27).

病灶 (§13.2-B/§14.4 确认): risk.global_blacklist 5 条 (US.QCOM/US.LCID/US.TME/
HK.00700/US.HK.00700), 静态侧 get_stock_codes 已滤, 但三条新候选路径无复核:
① scheduler_core._load_watchlist 合并 daily_active_pool; ② runtime_context
纯加法合并动态池; ③ topup roles.topup_eligible 派生 (QCOM 被禁仍 topup_eligible)。

本钉锁死: PLAYBOOK_BLACKLIST_GUARD 默认 OFF 逐字节旧行为; ON 时合并/fallback/
topup 边界均复核; 只消费 global_blacklist, 不误伤普通 disabled/self_managed;
持仓管理不受影响; 配置失败 fail-open。
"""
import logging
from types import SimpleNamespace

import pytest

import app.core.config_container as cc
import app.playbook.pipeline_parts.generation_flow as gf
from app.playbook.pipeline_parts.runtime_context import (
    _get_positions_as_dicts,
    _get_watchlist,
)
from app.utils.watchlist_manager import filter_global_blacklist

BLACKLIST = ["US.QCOM", "US.LCID", "US.TME", "HK.00700", "US.HK.00700"]


@pytest.fixture(autouse=True)
def _ensure_real_watchlist_singleton():
    """先在真实配置下固化名单单例 — 本文件 fake config 只应影响黑名单活读,
    不得借单例首次初始化把空 us_stocks 缓存进真源 (污染后续测试)."""
    from app.utils.watchlist_manager import get_watchlist_manager
    get_watchlist_manager()
    yield


def _patch_cfg(monkeypatch, blacklist=BLACKLIST, self_managed=(), topup=None,
               fail=False):
    def get_dotted(k, d=None):
        if fail:
            raise RuntimeError("config unavailable")
        if k == "risk.global_blacklist":
            return blacklist
        if k == "risk.self_managed_symbols":
            return list(self_managed)
        if k == "playbook.topup":
            return topup if topup is not None else d
        return d
    monkeypatch.setattr(cc, "get_config",
                        lambda: SimpleNamespace(get_dotted=get_dotted))


def _flag(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("PLAYBOOK_BLACKLIST_GUARD", raising=False)
    else:
        monkeypatch.setenv("PLAYBOOK_BLACKLIST_GUARD", value)


# ── 中央谓词 ──────────────────────────────────────────────────────────


def test_flag_unset_default_off_returns_same_object(monkeypatch):
    _patch_cfg(monkeypatch)
    _flag(monkeypatch, None)
    syms = ["US.QCOM", "US.NVDA"]
    assert filter_global_blacklist(syms) is syms


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "maybe", ""])
def test_flag_non_truthy_keeps_old_behavior(monkeypatch, value):
    _patch_cfg(monkeypatch)
    _flag(monkeypatch, value)
    syms = ["US.QCOM", "US.NVDA"]
    assert filter_global_blacklist(syms) is syms


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_flag_truthy_blocks_all_five_config_entries(monkeypatch, value):
    _patch_cfg(monkeypatch)
    _flag(monkeypatch, value)
    syms = ["US.QCOM", "US.LCID", "US.TME", "HK.00700", "US.HK.00700", "US.NVDA"]
    assert filter_global_blacklist(syms) == ["US.NVDA"]


def test_flag_on_bare_and_double_prefix_variants_normalized(monkeypatch):
    _patch_cfg(monkeypatch)
    _flag(monkeypatch, "1")
    # 与 _apply_exclusions 同口径: str.replace("US.","") 剥全部 US. 前缀
    assert filter_global_blacklist(["QCOM", "US.US.LCID", "US.TME"]) == []


def test_disabled_but_not_banned_not_hurt(monkeypatch):
    """US.NFLX 静态 enabled=false 但不在黑名单 — 守卫不得误伤普通暂停."""
    _patch_cfg(monkeypatch)
    _flag(monkeypatch, "1")
    assert filter_global_blacklist(["US.NFLX", "US.QCOM"]) == ["US.NFLX"]


def test_self_managed_not_consumed_by_guard(monkeypatch):
    """self_managed 仍由静态侧 _apply_exclusions 负责, 守卫只消费 global_blacklist."""
    _patch_cfg(monkeypatch, self_managed=["US.AAPL"])
    _flag(monkeypatch, "1")
    assert filter_global_blacklist(["US.AAPL", "US.QCOM"]) == ["US.AAPL"]


def test_empty_blacklist_fail_open(monkeypatch):
    _patch_cfg(monkeypatch, blacklist=[])
    _flag(monkeypatch, "1")
    syms = ["US.QCOM", "US.NVDA"]
    assert filter_global_blacklist(syms) is syms


def test_config_failure_fail_open(monkeypatch):
    _patch_cfg(monkeypatch, fail=True)
    _flag(monkeypatch, "1")
    syms = ["US.QCOM", "US.NVDA"]
    assert filter_global_blacklist(syms) is syms


# ── 合并边界 (runtime_context._get_watchlist) ─────────────────────────


def _sched(wl):
    return SimpleNamespace(watchlist=list(wl))


def _patch_dynamic(monkeypatch, pool):
    import app.plan.factor_engine as fe
    monkeypatch.setattr(fe, "_get_stock_pool",
                        lambda: (list(pool), {"final_pool_size": len(pool)}))


def test_merge_path_flag_off_byte_identical(monkeypatch):
    _patch_cfg(monkeypatch)
    _flag(monkeypatch, None)
    sched = _sched(["US.QCOM", "US.NVDA", "US.BA#SIM", "US.US.MU"])
    _patch_dynamic(monkeypatch, ["US.LCID", "US.TSLA", "US.QCOM"])
    got = _get_watchlist(sched)
    # 旧口径独立重算: 清双前缀/#SIM → 纯加法并集 → sorted
    wl = ["US.QCOM", "US.NVDA", "US.MU"]
    expected = sorted(set(wl) | {"US.LCID", "US.TSLA", "US.QCOM"})
    assert got == expected
    assert "US.QCOM" in got and "US.LCID" in got  # 旧行为确实泄漏 (病灶复现)


def test_merge_path_flag_on_blocks_both_sources(monkeypatch):
    _patch_cfg(monkeypatch)
    _flag(monkeypatch, "1")
    sched = _sched(["US.QCOM", "US.NVDA", "US.BA#SIM", "US.US.MU"])
    _patch_dynamic(monkeypatch, ["US.LCID", "US.TSLA", "US.QCOM", "US.HK.00700"])
    got = _get_watchlist(sched)
    assert got == ["US.MU", "US.NVDA", "US.TSLA"]
    # 不原地修改 scheduler.watchlist (持仓/监控仍读原名单)
    assert sched.watchlist == ["US.QCOM", "US.NVDA", "US.BA#SIM", "US.US.MU"]


def test_fallback_path_flag_on_filters_static(monkeypatch):
    _patch_cfg(monkeypatch)
    _flag(monkeypatch, "1")
    import app.plan.factor_engine as fe

    def _boom():
        raise RuntimeError("factor_engine down")
    monkeypatch.setattr(fe, "_get_stock_pool", _boom)
    sched = _sched(["US.QCOM", "US.NVDA"])
    assert _get_watchlist(sched) == ["US.NVDA"]


def test_fallback_path_flag_off_unchanged(monkeypatch):
    _patch_cfg(monkeypatch)
    _flag(monkeypatch, None)
    import app.plan.factor_engine as fe

    def _boom():
        raise RuntimeError("factor_engine down")
    monkeypatch.setattr(fe, "_get_stock_pool", _boom)
    sched = _sched(["US.QCOM", "US.NVDA"])
    assert _get_watchlist(sched) == ["US.QCOM", "US.NVDA"]


def test_positions_provider_untouched_by_guard(monkeypatch):
    """持仓管理不读候选守卫: 被禁标的若在仓, positions 视图仍保留."""
    _patch_cfg(monkeypatch)
    _flag(monkeypatch, "1")
    pos = SimpleNamespace(symbol="US.QCOM", sector="Semiconductors",
                          direction="LONG")
    sched = SimpleNamespace(position_store=SimpleNamespace(all=lambda: [pos]))
    assert _get_positions_as_dicts(sched) == [
        {"symbol": "US.QCOM", "sector": "Semiconductors", "direction": "LONG"}
    ]


# ── topup 边界 (generation_flow.run_playbook_topup_supplement) ────────


def _drive_topup(monkeypatch, role_pool):
    _patch_cfg(monkeypatch, topup={
        "enabled": True, "max_attempts_per_window": 2, "retry_interval_min": 15,
        "target_min_instructions": 25, "max_add_per_day": 12,
    })

    class _FakeDB:
        def ensure_tables(self):
            pass

        def get_instructions_by_date(self, today):
            return []

    monkeypatch.setattr("app.playbook.db.PlaybookDB", lambda: _FakeDB())
    monkeypatch.setattr(
        "app.playbook.market_regime.MarketRegimeGate",
        lambda: SimpleNamespace(assess=lambda: SimpleNamespace(regime="neutral")))
    seen = {}

    def _screen(watchlist, regime, positions):
        seen["watchlist"] = list(watchlist)
        return []
    monkeypatch.setattr(
        "app.playbook.screener.PlaybookScreener",
        lambda collector: SimpleNamespace(screen=_screen))
    monkeypatch.setattr("app.utils.watchlist_manager.get_symbols_by_role",
                        lambda role: list(role_pool))
    runtime = SimpleNamespace(collector=None, positions=[], data_router=None,
                              total_equity=100000.0)
    result = gf.run_playbook_topup_supplement(
        None,
        now_et_provider=lambda: __import__("zoneinfo").ZoneInfo("America/New_York"),
        today_provider=lambda: "2026-11-27",
        topup_window=lambda hhmm, cfg: "premarket",
        attempts_cache={},
        build_runtime_context=lambda s: runtime,
        build_instructions_from_votes=lambda **kw: [],
        instruction_to_db_dict=lambda inst, ctx, today: {"raw_instruction": {}},
        gather_intraday_evidence=lambda router, sym, direction: None,
        logger=logging.getLogger("test"),
    )
    return seen, result


def test_topup_flag_off_keeps_banned_in_pool(monkeypatch):
    _flag(monkeypatch, None)
    seen, result = _drive_topup(monkeypatch, ["US.QCOM", "US.NVDA"])
    assert seen["watchlist"] == ["US.QCOM", "US.NVDA"]
    assert result == {"added": 0, "reason": "no_screen_pass"}


def test_topup_flag_on_blocks_banned_from_pool(monkeypatch):
    _flag(monkeypatch, "1")
    seen, result = _drive_topup(monkeypatch, ["US.QCOM", "US.NVDA"])
    assert seen["watchlist"] == ["US.NVDA"]
    assert result == {"added": 0, "reason": "no_screen_pass"}


def test_topup_role_semantics_themselves_unchanged(monkeypatch):
    """守卫在消费边界, 不改 get_symbols_by_role 角色派生本体 (QCOM 仍 topup_eligible)."""
    _flag(monkeypatch, "1")
    from app.utils.watchlist_manager import get_symbols_by_role
    assert "US.QCOM" in get_symbols_by_role("topup_eligible")
