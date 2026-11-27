"""RFC-218 §17: EXT 候选边界全局黑名单守卫契约 (默认 OFF = 逐字节旧供给面 + record-only 影子).

钉子:
  - 开关语义 (默认 OFF / truthy / 未知值 fail-closed); 影子默认 ON / 显式 falsy 关;
  - OFF: 被禁标的仍在候选 (供给面不变), 只落 mode=shadow 台账;
  - ON: 汇合口拦截 + mode=blocked 审计, 非黑名单零误伤;
  - 谓词与 Playbook §16 守卫同源同口径 (_get_global_blacklist + 剥 US.);
  - 空黑名单/读取失败 fail-open; 台账写盘失败绝不影响候选/下单路径;
  - 研究臂边界同开关 (到期臂零流量不受影响)。
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import scripts.extended_hours_entry_executor as mod
from app.utils.watchlist_manager import get_watchlist_manager


@pytest.fixture(autouse=True)
def _fixed_et_now(monkeypatch):
    monkeypatch.setattr(
        mod, "_now_et",
        lambda: datetime(2026, 11, 27, 8, 0, tzinfo=ZoneInfo("America/New_York")))


@pytest.fixture
def ledger_dir(tmp_path, monkeypatch):
    d = tmp_path / "ext_blacklist"
    monkeypatch.setattr(mod, "EXT_BLACKLIST_DIR", d)
    return d


@pytest.fixture
def real_blacklist(monkeypatch):
    """单一真源桩: 与生产 risk.global_blacklist 同归一化形态 (已剥 US.)."""
    wm = get_watchlist_manager()
    monkeypatch.setattr(wm, "_get_global_blacklist",
                        lambda: ["QCOM", "LCID", "TME", "HK.00700"])
    return {"QCOM", "LCID", "TME", "HK.00700"}


def _rank_items(*syms):
    return [{"symbol": s, "name": "Corp", "px": 100.0, "chg": 4.0, "vol": 500000.0}
            for s in syms]


@pytest.fixture
def rank_only(monkeypatch):
    """只开 rank 源, 其余源关闭; _scan_movers 由各测试自行桩."""
    monkeypatch.setattr(mod, "SOURCE_RANK_BOARD", True)
    monkeypatch.setattr(mod, "SOURCE_BEAT_RANK", False)
    monkeypatch.setattr(mod, "SOURCE_WATCHLIST", False)
    monkeypatch.setattr(mod, "SOURCE_CALENDAR", False)
    monkeypatch.setattr(mod, "SOURCE_FINNHUB_CALENDAR", False)


def _ledger_rows(ledger_dir):
    path = ledger_dir / "2026-11-27.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text()).get("rows") or {}


# ── 开关语义 ──────────────────────────────────────────────────────────────

def test_guard_flag_default_off(monkeypatch):
    monkeypatch.delenv("EXT_BLACKLIST_GUARD", raising=False)
    assert mod._ext_blacklist_guard_enabled() is False


@pytest.mark.parametrize("v", ["1", "true", "yes", "on", "TRUE", " On "])
def test_guard_flag_truthy(monkeypatch, v):
    monkeypatch.setenv("EXT_BLACKLIST_GUARD", v)
    assert mod._ext_blacklist_guard_enabled() is True


@pytest.mark.parametrize("v", ["0", "2", "maybe", "", "off"])
def test_guard_flag_unknown_fail_closed(monkeypatch, v):
    monkeypatch.setenv("EXT_BLACKLIST_GUARD", v)
    assert mod._ext_blacklist_guard_enabled() is False


def test_shadow_default_on(monkeypatch):
    monkeypatch.delenv("EXT_BLACKLIST_SHADOW", raising=False)
    assert mod._ext_blacklist_shadow_enabled() is True


@pytest.mark.parametrize("v", ["0", "false", "no", "off", "OFF"])
def test_shadow_explicit_falsy_off(monkeypatch, v):
    monkeypatch.setenv("EXT_BLACKLIST_SHADOW", v)
    assert mod._ext_blacklist_shadow_enabled() is False


def test_shadow_unknown_stays_on(monkeypatch):
    monkeypatch.setenv("EXT_BLACKLIST_SHADOW", "maybe")
    assert mod._ext_blacklist_shadow_enabled() is True


# ── 谓词真源与 fail-open ─────────────────────────────────────────────────

def test_blacklist_set_from_single_source(real_blacklist):
    assert mod._ext_blacklist_set() == real_blacklist


def test_blacklist_set_fail_open_on_reader_error(monkeypatch):
    wm = get_watchlist_manager()

    def _boom():
        raise RuntimeError("config down")
    monkeypatch.setattr(wm, "_get_global_blacklist", _boom)
    assert mod._ext_blacklist_set() == set()


def test_blacklist_set_fail_open_on_import_error(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "app.utils.watchlist_manager", None)
    assert mod._ext_blacklist_set() == set()


def test_predicate_parity_with_playbook_guard(monkeypatch, real_blacklist):
    """EXT 成员判定与 Playbook filter_global_blacklist 同口径 (同源同剥前缀)."""
    from app.utils.watchlist_manager import filter_global_blacklist
    monkeypatch.setenv("PLAYBOOK_BLACKLIST_GUARD", "1")
    syms = ["US.QCOM", "US.AAPL", "US.HK.00700", "HK.00700", "US.LCID", "US.NVDA"]
    bl = mod._ext_blacklist_set()
    ext_kept = [s for s in syms if str(s).replace("US.", "") not in bl]
    assert ext_kept == filter_global_blacklist(syms)


# ── 严格臂汇合口: OFF = 供给面不变 + 影子 ─────────────────────────────────

def test_scan_off_keeps_banned_and_records_shadow(monkeypatch, rank_only,
                                                  real_blacklist, ledger_dir):
    monkeypatch.delenv("EXT_BLACKLIST_GUARD", raising=False)
    monkeypatch.delenv("EXT_BLACKLIST_SHADOW", raising=False)
    monkeypatch.setattr(mod, "_scan_movers",
                        lambda s, q: _rank_items("US.QCOM", "US.AAPL"))
    out = mod._scan_candidates("premarket", object())
    assert [m["symbol"] for m in out] == ["US.QCOM", "US.AAPL"]  # 供给面逐字节不变
    rows = _ledger_rows(ledger_dir)
    assert set(rows) == {"premarket:US.QCOM"}
    assert rows["premarket:US.QCOM"]["mode"] == "shadow"
    assert rows["premarket:US.QCOM"]["source"] == "rank"


def test_scan_on_blocks_banned_zero_false_positive(monkeypatch, rank_only,
                                                   real_blacklist, ledger_dir):
    monkeypatch.setenv("EXT_BLACKLIST_GUARD", "1")
    monkeypatch.setattr(mod, "_scan_movers",
                        lambda s, q: _rank_items("US.QCOM", "US.AAPL", "US.HK.00700",
                                                 "US.NVDA"))
    out = mod._scan_candidates("afterhours", object())
    assert [m["symbol"] for m in out] == ["US.AAPL", "US.NVDA"]
    rows = _ledger_rows(ledger_dir)
    assert set(rows) == {"afterhours:US.QCOM", "afterhours:US.HK.00700"}
    assert all(r["mode"] == "blocked" for r in rows.values())


def test_scan_off_shadow_kill_no_ledger(monkeypatch, rank_only,
                                        real_blacklist, ledger_dir):
    monkeypatch.delenv("EXT_BLACKLIST_GUARD", raising=False)
    monkeypatch.setenv("EXT_BLACKLIST_SHADOW", "0")
    monkeypatch.setattr(mod, "_scan_movers",
                        lambda s, q: _rank_items("US.QCOM", "US.AAPL"))
    out = mod._scan_candidates("premarket", object())
    assert [m["symbol"] for m in out] == ["US.QCOM", "US.AAPL"]
    assert _ledger_rows(ledger_dir) == {}


def test_scan_empty_blacklist_no_ledger(monkeypatch, rank_only, ledger_dir):
    wm = get_watchlist_manager()
    monkeypatch.setattr(wm, "_get_global_blacklist", lambda: [])
    monkeypatch.setenv("EXT_BLACKLIST_GUARD", "1")
    monkeypatch.setattr(mod, "_scan_movers",
                        lambda s, q: _rank_items("US.QCOM", "US.AAPL"))
    out = mod._scan_candidates("premarket", object())
    assert [m["symbol"] for m in out] == ["US.QCOM", "US.AAPL"]  # fail-open
    assert _ledger_rows(ledger_dir) == {}


def test_scan_reader_failure_fail_open(monkeypatch, rank_only, ledger_dir):
    wm = get_watchlist_manager()

    def _boom():
        raise RuntimeError("config down")
    monkeypatch.setattr(wm, "_get_global_blacklist", _boom)
    monkeypatch.setenv("EXT_BLACKLIST_GUARD", "1")
    monkeypatch.setattr(mod, "_scan_movers",
                        lambda s, q: _rank_items("US.QCOM", "US.AAPL"))
    out = mod._scan_candidates("premarket", object())
    assert [m["symbol"] for m in out] == ["US.QCOM", "US.AAPL"]
    assert _ledger_rows(ledger_dir) == {}


def test_scan_test_and_etf_skips_precede_blacklist(monkeypatch, rank_only,
                                                   real_blacklist, ledger_dir):
    monkeypatch.setenv("EXT_BLACKLIST_GUARD", "1")
    items = _rank_items("US.TEST", "US.QCOM")
    items.append({"symbol": "US.GRAG", "name": "Some ETF", "px": 50.0,
                  "chg": 4.0, "vol": 100000.0})
    monkeypatch.setattr(mod, "_scan_movers", lambda s, q: items)
    out = mod._scan_candidates("premarket", object())
    assert out == []
    rows = _ledger_rows(ledger_dir)
    assert set(rows) == {"premarket:US.QCOM"}  # test/ETF 不进黑名单台账


def test_scan_ledger_dedup_across_ticks(monkeypatch, rank_only,
                                        real_blacklist, ledger_dir):
    monkeypatch.delenv("EXT_BLACKLIST_GUARD", raising=False)
    monkeypatch.setattr(mod, "_scan_movers",
                        lambda s, q: _rank_items("US.QCOM"))
    mod._scan_candidates("premarket", object())
    mod._scan_candidates("premarket", object())
    rows = _ledger_rows(ledger_dir)
    assert len(rows) == 1
    assert rows["premarket:US.QCOM"]["ticks"] == 2


def test_scan_ledger_write_failure_never_breaks_candidates(monkeypatch, rank_only,
                                                           real_blacklist, tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    monkeypatch.setattr(mod, "EXT_BLACKLIST_DIR", blocker)  # mkdir 必炸
    monkeypatch.setenv("EXT_BLACKLIST_GUARD", "1")
    monkeypatch.setattr(mod, "_scan_movers",
                        lambda s, q: _rank_items("US.QCOM", "US.AAPL"))
    out = mod._scan_candidates("premarket", object())
    assert [m["symbol"] for m in out] == ["US.AAPL"]  # 拦截仍生效, 写盘失败不抛


def test_scan_on_all_banned_empty_merged(monkeypatch, rank_only,
                                         real_blacklist, ledger_dir):
    monkeypatch.setenv("EXT_BLACKLIST_GUARD", "1")
    monkeypatch.setattr(mod, "_scan_movers",
                        lambda s, q: _rank_items("US.QCOM", "US.LCID", "US.TME"))
    assert mod._scan_candidates("premarket", object()) == []


def test_blocked_ledger_written_even_if_shadow_off(monkeypatch, rank_only,
                                                   real_blacklist, ledger_dir):
    monkeypatch.setenv("EXT_BLACKLIST_GUARD", "1")
    monkeypatch.setenv("EXT_BLACKLIST_SHADOW", "0")
    monkeypatch.setattr(mod, "_scan_movers", lambda s, q: _rank_items("US.QCOM"))
    mod._scan_candidates("premarket", object())
    rows = _ledger_rows(ledger_dir)
    assert rows["premarket:US.QCOM"]["mode"] == "blocked"  # 审计不受影子开关影响


# ── 研究臂边界 ────────────────────────────────────────────────────────────

@pytest.fixture
def research_env(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "RESEARCH_ARM_ON", True)
    monkeypatch.setattr(mod, "RESEARCH_SOURCES_ON", True)
    monkeypatch.setattr(mod, "RESEARCH_CHG_BAND_RAW", "2.0,10.0")
    monkeypatch.setattr(mod, "RESEARCH_MIN_PRICE", 5.0)
    monkeypatch.setattr(mod, "RESEARCH_MAX_SYMBOLS", 80)
    monkeypatch.setattr(mod, "RESEARCH_FUNNEL", tmp_path / "funnel.jsonl")
    monkeypatch.setattr(mod, "_research_sources_active", lambda: True)
    monkeypatch.setattr(mod, "_beat_rank_symbols", lambda q: ["US.QCOM", "US.BEAT1"])
    monkeypatch.setattr(mod, "_watchlist_symbols", lambda q: ["US.WATCH1"])

    def _snapshot(session, qctx, symbols):
        return [{"symbol": s, "px": 10.0, "chg": 5.0, "vol": 1000.0}
                for s in symbols]
    monkeypatch.setattr(mod, "_snapshot_candidates", _snapshot)


def test_research_off_keeps_banned_and_records_shadow(monkeypatch, research_env,
                                                      real_blacklist, ledger_dir):
    monkeypatch.delenv("EXT_BLACKLIST_GUARD", raising=False)
    out = mod._research_source_candidates("premarket", object(), existing=set())
    assert [x["symbol"] for x in out] == ["US.QCOM", "US.BEAT1", "US.WATCH1"]
    rows = _ledger_rows(ledger_dir)
    assert rows["premarket:US.QCOM"]["mode"] == "shadow"
    assert rows["premarket:US.QCOM"]["source"] == "research"


def test_research_on_filters_before_snapshot(monkeypatch, research_env,
                                             real_blacklist, ledger_dir):
    monkeypatch.setenv("EXT_BLACKLIST_GUARD", "1")
    seen = {}
    orig = mod._snapshot_candidates

    def _spy(session, qctx, symbols):
        seen["symbols"] = list(symbols)
        return orig(session, qctx, symbols)
    monkeypatch.setattr(mod, "_snapshot_candidates", _spy)
    out = mod._research_source_candidates("premarket", object(), existing=set())
    assert seen["symbols"] == ["US.BEAT1", "US.WATCH1"]  # 被禁标的不再浪费快照请求
    assert [x["symbol"] for x in out] == ["US.BEAT1", "US.WATCH1"]
    assert _ledger_rows(ledger_dir)["premarket:US.QCOM"]["mode"] == "blocked"


def test_research_expired_arm_zero_traffic(monkeypatch, research_env,
                                           real_blacklist, ledger_dir):
    monkeypatch.setenv("EXT_BLACKLIST_GUARD", "1")
    monkeypatch.setattr(mod, "_research_sources_active", lambda: False)
    assert mod._research_source_candidates("premarket", object(), existing=set()) == []
    assert _ledger_rows(ledger_dir) == {}
